#!/usr/bin/env python3
"""Compare hard-gated and continuously graded absorption lows offline."""

from __future__ import annotations

import argparse, json, os, sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNNERS = ROOT / "lane-logs" / "AB3" / "runner-windows.jsonl"
DEFAULT_QUIET = ROOT / "lane-logs" / "AB2-eval" / "absorption-2026-08-15.jsonl"
DEFAULT_OUTPUT = ROOT / "lane-logs" / "AB4"
COMPONENTS = ("at_lows", "range_compression", "volume_floor",
              "taker_imbalance_shift", "trade_size_shift")
COMMON_GATES = ("all_components_known", "price_flat", "volume_not_surging", "not_pumping")


class ValidationError(Exception): pass


def _object(raw: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping): raise ValidationError(f"{label} is not an object")
    return raw


def _text(raw: Any, label: str) -> str:
    if not isinstance(raw, str) or not raw: raise ValidationError(f"{label} is not text")
    return raw


def _bool(raw: Any, label: str) -> bool:
    if not isinstance(raw, bool): raise ValidationError(f"{label} is not boolean")
    return raw


def _decimal(raw: Any, label: str, *, optional: bool = False) -> Optional[Decimal]:
    if raw is None and optional: return None
    try: value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{label} is not a Decimal") from None
    if not value.is_finite(): raise ValidationError(f"{label} is not finite")
    return value


def _render(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text: text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _utc(raw: Any, label: str) -> datetime:
    try: value = datetime.fromisoformat(_text(raw, label).replace("Z", "+00:00"))
    except ValueError: raise ValidationError(f"{label} is not an ISO timestamp") from None
    if value.tzinfo != timezone.utc: raise ValidationError(f"{label} is not UTC")
    return value


def _load(path: Path) -> Sequence[Mapping[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip(): continue
            try: rows.append(_object(json.loads(line), f"{path}:{number}"))
            except json.JSONDecodeError as error:
                raise ValidationError(f"{path}:{number} is not JSON") from error
    if not rows: raise ValidationError(f"{path} has no evaluation rows")
    return rows


def _write(path: Path, payload: Any, *, lines: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        if lines:
            for row in payload:
                handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        else:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":")); handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def _factors(raw: Mapping[str, Any], label: str) -> Dict[str, Optional[Decimal]]:
    result: Dict[str, Optional[Decimal]] = {}
    for name in COMPONENTS:
        value = _decimal(raw.get(name), f"{label}.{name}", optional=True)
        if value is not None and not Decimal("0") <= value <= Decimal("1"):
            raise ValidationError(f"{label}.{name} is outside zero to one")
        result[name] = value
    return result


def _base(row: Mapping[str, Any], index: int, dataset: str,
          factors: Dict[str, Optional[Decimal]], gates: Dict[str, bool],
          low_position: Decimal, eligible: bool) -> Dict[str, Any]:
    known = all(value is not None for value in factors.values())
    if gates["all_components_known"] != known: raise ValidationError(f"{dataset} row {index} component readiness differs")
    lows_factor = factors["at_lows"]
    if lows_factor is None: raise ValidationError(f"{dataset} row {index} lacks lows")
    with localcontext() as context:
        context.prec = 28
        expected = Decimal("1") - min(Decimal("1"), max(Decimal("0"), low_position))
    if lows_factor != expected: raise ValidationError(f"{dataset} row {index} lows factor differs")
    start = _utc(row.get("window_start_utc"), f"{dataset} row {index} start")
    end = _utc(row.get("window_end_utc"), f"{dataset} row {index} end")
    if end <= start: raise ValidationError(f"{dataset} row {index} window is not ordered")
    return {
        "dataset": dataset, "source_row": index,
        "symbol": _text(row.get("symbol"), f"{dataset} row {index} symbol"),
        "window_start_utc": row["window_start_utc"],
        "window_end_utc": row["window_end_utc"],
        "eligible_for_distribution": eligible, "components": factors,
        "gates": gates, "low_position": low_position,
        "production_score": _decimal(row.get("absorption_score"),
                                     f"{dataset} row {index} score"),
    }


def _runner(row: Mapping[str, Any], index: int) -> Dict[str, Any]:
    raw = _object(row.get("components"), f"runner row {index} components")
    factors = _factors({name: _object(raw.get(name), name).get("factor")
                        for name in COMPONENTS}, f"runner row {index}")
    raw_gates = _object(row.get("gates"), f"runner row {index} gates")
    gates = {name: _bool(raw_gates.get(name), f"runner row {index}.{name}")
             for name in COMMON_GATES + ("at_multiweek_lows",)}
    low_position = _decimal(_object(raw.get("at_lows"), "at_lows").get(
        "raw_low_position"), f"runner row {index} low position")
    ready = _bool(row.get("window_tape_ready"), f"runner row {index} readiness")
    result = _base(row, index, "runner", factors, gates, low_position, ready)
    ignition = _utc(row.get("ignition_utc"), f"runner row {index} ignition")
    if _utc(row["window_end_utc"], "runner end") >= ignition:
        raise ValidationError(f"runner row {index} is not pre-ignition")
    result["ignition_utc"] = row["ignition_utc"]
    return result


def _quiet(row: Mapping[str, Any], index: int) -> Dict[str, Any]:
    factors = _factors(_object(row.get("score_components"), f"quiet row {index} components"),
                       f"quiet row {index}")
    features = _object(row.get("features"), f"quiet row {index} features")
    missing = {name for name, value in factors.items() if value is None}
    declared = row.get("unknown_features")
    if not isinstance(declared, list) or set(declared) != missing: raise ValidationError(f"quiet row {index} unknown components differ")
    gates = {
        "all_components_known": not missing,
        "at_multiweek_lows": _bool(features.get("at_multiweek_lows"), "lows gate"),
        "price_flat": _bool(features.get("price_flat"), "flat gate"),
        "volume_not_surging": not _bool(features.get("volume_surging"), "volume gate"),
        "not_pumping": not _bool(features.get("pumping"), "pumping gate"),
    }
    low_position = _decimal(features.get("low_position_in_history"),
                            f"quiet row {index} low position")
    return _base(row, index, "quiet", factors, gates, low_position, not missing)


def _score(row: Mapping[str, Any], *, graded: bool) -> Decimal:
    if not all(row["gates"][name] for name in COMMON_GATES): return Decimal("0")
    if not graded and not row["gates"]["at_multiweek_lows"]: return Decimal("0")
    values = row["components"]
    if any(values[name] is None for name in COMPONENTS): raise ValidationError("known component gate allowed missing evidence")
    with localcontext() as context:
        context.prec = 50; score = Decimal("100")
        for name in COMPONENTS: score *= values[name]
    return score


def _quantile(values: Sequence[Decimal], numerator: int) -> Decimal:
    ordered = sorted(values)
    if not ordered: raise ValidationError("distribution has no eligible rows")
    with localcontext() as context:
        context.prec = 50
        position = Decimal(len(ordered) - 1) * Decimal(numerator) / Decimal("10")
        lower = int(position); upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (
            position - Decimal(lower))


def _dist(values: Sequence[Decimal]) -> Dict[str, Any]:
    return {"count": len(values), "min": _render(min(values)),
            "median": _render(_quantile(values, 5)),
            "p90": _render(_quantile(values, 9)), "max": _render(max(values)),
            "nonzero_count": sum(value > 0 for value in values)}


def _lead_hours(ignition: str, window_end: str) -> Decimal:
    delta = _utc(ignition, "ignition") - _utc(window_end, "window end")
    micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    return Decimal(micros) / Decimal("3600000000")


def _public(row: Mapping[str, Any]) -> Dict[str, Any]:
    hidden = {"components", "gates", "low_position", "production_score",
              "current_score", "graded_score"}
    output = {key: value for key, value in row.items() if key not in hidden}
    output.update({
        "components": {name: None if value is None else _render(value)
                       for name, value in row["components"].items()},
        "gates": dict(row["gates"]),
        "low_position_in_history": _render(row["low_position"]),
        "lows_factor": _render(row["components"]["at_lows"]),
        "current_score": _render(row["current_score"]),
        "graded_score": _render(row["graded_score"]),
    })
    return output


def _summary(runners: Sequence[Mapping[str, Any]],
             quiet: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    ready = [row for row in runners if row["eligible_for_distribution"]]
    complete = [row for row in quiet if row["eligible_for_distribution"]]
    threshold = max(row["graded_score"] for row in complete)
    by_runner = {}
    for symbol in sorted({row["symbol"] for row in ready}):
        selected = [row for row in ready if row["symbol"] == symbol]
        exceeding = sorted((row for row in selected if row["graded_score"] > threshold),
                           key=lambda row: _utc(row["window_start_utc"], "start"))
        earliest = exceeding[0] if exceeding else None
        by_runner[symbol] = {
            "eligible_count": len(selected), "exceeding_count": len(exceeding),
            "earliest_exceeding": None if earliest is None else {
                "window_start_utc": earliest["window_start_utc"],
                "window_end_utc": earliest["window_end_utc"],
                "graded_score": _render(earliest["graded_score"]),
                "hours_before_ignition_from_window_end": _render(_lead_hours(
                    earliest["ignition_utc"], earliest["window_end_utc"])),
            }}
    total = sum(item["exceeding_count"] for item in by_runner.values())
    if total == 0:
        verdict = "GRADED fails to separate: runner windows do not clear the quiet maximum."
    elif all(item["exceeding_count"] > 0 for item in by_runner.values()):
        verdict = "GRADED separates some pre-ignition windows for every runner above the quiet maximum."
    else: verdict = "GRADED fails to separate every runner above the quiet maximum."
    distributions = {}
    for method, field in (("current", "current_score"), ("graded", "graded_score")):
        distributions[method] = {
            "runner_tape_ready": _dist([row[field] for row in ready]),
            "quiet_fully_scored": _dist([row[field] for row in complete])}
    return {
        "schema": "absorption-grading-validation-v1",
        "composition": {
            "current": "100 times all five components under all production outer gates",
            "graded": "CURRENT with only the hard at_multiweek_lows outer gate removed",
            "lows_factor": "1 minus clamp(low_position_in_history)"},
        "populations": {"runner_tape_ready": len(ready), "quiet_fully_scored": len(complete)},
        "distributions": distributions,
        "discrimination": {
            "threshold_rule": "strictly above the quiet fully-scored maximum GRADED score",
            "quiet_max_graded_score": _render(threshold),
            "runner_windows_above_threshold": total, "by_runner": by_runner,
            "verdict": verdict},
        "runner_low_positions": {
            symbol: _dist([row["low_position"] for row in ready
                           if row["symbol"] == symbol])
            for symbol in sorted({row["symbol"] for row in ready})},
        "long_history_limit": (
            "Longer price history of 90 to 365 days is required before any claim "
            "about long-term lows can be made either way; public monthly archives "
            "are available but were not pulled for this offline validation.")}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runners", type=Path, default=DEFAULT_RUNNERS)
    parser.add_argument("--quiet", type=Path, default=DEFAULT_QUIET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        runners = [_runner(row, number) for number, row in enumerate(
            _load(args.runners), 1)]
        quiet = [_quiet(row, number) for number, row in enumerate(
            _load(args.quiet), 1)]
        rows = runners + quiet
        for row in rows:
            row["current_score"] = _score(row, graded=False)
            row["graded_score"] = _score(row, graded=True)
            if row["current_score"] != row["production_score"]:
                raise ValidationError(
                    f"{row['dataset']} row {row['source_row']} CURRENT differs from production")
        summary = _summary(runners, quiet)
        _write(args.output_dir / "window-scores.jsonl", map(_public, rows), lines=True)
        _write(args.output_dir / "separation-summary.json", summary)
    except (OSError, UnicodeError, ValidationError) as error:
        print(f"validate absorption grading: {error}", file=sys.stderr); return 2
    print("AB4 | " + " | ".join((
        f"runner_ready={summary['populations']['runner_tape_ready']}",
        f"quiet_complete={summary['populations']['quiet_fully_scored']}",
        f"runner_above_quiet_max={summary['discrimination']['runner_windows_above_threshold']}",
        f"output={args.output_dir}")))
    return 0


if __name__ == "__main__": raise SystemExit(main())
