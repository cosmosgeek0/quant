#!/usr/bin/env python3
"""Compare 30-day and 365-day low gates and factors offline."""

from __future__ import annotations

import argparse, json, os, sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WINDOWS = ROOT / "lane-logs" / "AB4" / "window-scores.jsonl"
DEFAULT_LOWS = ROOT / "lane-logs" / "AB5" / "long-low-positions.json"
DEFAULT_OUTPUT = ROOT / "lane-logs" / "AB6"
COMPONENTS = ("at_lows", "range_compression", "volume_floor", "taker_imbalance_shift", "trade_size_shift")
COMMON_GATES = ("all_components_known", "price_flat", "volume_not_surging", "not_pumping")
VARIANTS = ("gate30", "graded30", "gate365", "graded365")
LOW_BAR = Decimal("0.20")


class StudyError(Exception): pass


def _object(raw: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping): raise StudyError(f"{label} is not an object")
    return raw


def _decimal(raw: Any, label: str, *, optional: bool = False) -> Optional[Decimal]:
    if raw is None and optional: return None
    try: value = Decimal(str(raw))
    except (InvalidOperation, ValueError): raise StudyError(f"{label} is not a Decimal") from None
    if not value.is_finite(): raise StudyError(f"{label} is not finite")
    return value


def _render(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text: text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _utc(raw: Any, label: str) -> datetime:
    if not isinstance(raw, str): raise StudyError(f"{label} is not text")
    try: value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError: raise StudyError(f"{label} is not ISO UTC") from None
    if value.tzinfo != timezone.utc: raise StudyError(f"{label} is not UTC")
    return value


def _load_lines(path: Path) -> Sequence[Mapping[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip(): continue
            try: rows.append(_object(json.loads(line), f"{path}:{number}"))
            except json.JSONDecodeError as error: raise StudyError(f"{path}:{number} is not JSON") from error
    if not rows: raise StudyError(f"{path} has no rows")
    return rows


def _load_object(path: Path) -> Mapping[str, Any]:
    with open(path, encoding="utf-8") as handle:
        try: return _object(json.load(handle), str(path))
        except json.JSONDecodeError as error: raise StudyError(f"{path} is not JSON") from error


def _write(path: Path, payload: Any, *, lines: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        if lines:
            for row in payload: handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        else: json.dump(payload, handle, sort_keys=True, separators=(",", ":")); handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def _factor(position: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return Decimal("1") - min(Decimal("1"), max(Decimal("0"), position))


def _score(row: Mapping[str, Any], lows: Decimal, low_gate: bool) -> Decimal:
    if not all(row["gates"][name] for name in COMMON_GATES) or not low_gate: return Decimal("0")
    values = row["components"]
    if any(values[name] is None for name in COMPONENTS): raise StudyError("known-component gate allowed missing evidence")
    with localcontext() as context:
        context.prec = 50; score = Decimal("100") * lows
        for name in COMPONENTS[1:]: score *= values[name]
    return score


def _prepare(raw: Mapping[str, Any], number: int, symbols: Mapping[str, Any]) -> Dict[str, Any]:
    dataset = raw.get("dataset")
    if dataset not in {"runner", "quiet"}: raise StudyError(f"row {number} dataset differs")
    symbol = raw.get("symbol")
    if not isinstance(symbol, str) or not symbol: raise StudyError(f"row {number} symbol differs")
    eligible = raw.get("eligible_for_distribution")
    if not isinstance(eligible, bool): raise StudyError(f"row {number} eligibility differs")
    components_raw = _object(raw.get("components"), f"row {number} components")
    components = {name: _decimal(components_raw.get(name), f"row {number}.{name}", optional=True) for name in COMPONENTS}
    if any(value is not None and not Decimal("0") <= value <= Decimal("1") for value in components.values()):
        raise StudyError(f"row {number} factor is outside zero to one")
    gates_raw = _object(raw.get("gates"), f"row {number} gates")
    gate_names = COMMON_GATES + ("at_multiweek_lows",)
    if any(not isinstance(gates_raw.get(name), bool) for name in gate_names): raise StudyError(f"row {number} gate differs")
    gates = {name: gates_raw[name] for name in gate_names}
    if gates["all_components_known"] != all(value is not None for value in components.values()): raise StudyError(f"row {number} component readiness differs")
    low30 = _decimal(raw.get("low_position_in_history"), f"row {number} 30-day low")
    if components["at_lows"] != _factor(low30): raise StudyError(f"row {number} 30-day factor differs")
    record = _object(symbols.get(symbol), f"365-day {symbol}")
    status = record.get("status")
    if status not in {"READY", "NOT-READY"}: raise StudyError(f"365-day {symbol} status differs")
    days = record.get("days_of_history")
    if not isinstance(days, int) or days < 1: raise StudyError(f"365-day {symbol} history differs")
    low365 = _decimal(record.get("low_position_365"), f"365-day {symbol} low")
    if not Decimal("0") <= low365 <= Decimal("1"): raise StudyError(f"365-day {symbol} low is outside zero to one")
    row = {"dataset": dataset, "symbol": symbol, "eligible": eligible, "source_row": raw.get("source_row"),
           "window_start_utc": raw.get("window_start_utc"), "window_end_utc": raw.get("window_end_utc"),
           "components": components, "gates": gates, "low30": low30, "low365": low365, "days": days,
           "history_status": status, "history_class": "veteran" if status == "READY" else "fresh"}
    _utc(row["window_start_utc"], f"row {number} start")
    if _utc(row["window_end_utc"], f"row {number} end") <= _utc(row["window_start_utc"], "start"): raise StudyError(f"row {number} window order differs")
    if dataset == "runner":
        row["ignition_utc"] = raw.get("ignition_utc")
        if _utc(row["window_end_utc"], "end") >= _utc(row["ignition_utc"], "ignition"): raise StudyError(f"row {number} is not pre-ignition")
    low30_factor, low365_factor = _factor(low30), _factor(low365)
    row["scores"] = {
        "gate30": _score(row, low30_factor, gates["at_multiweek_lows"]),
        "graded30": _score(row, low30_factor, True),
        "gate365": _score(row, low365_factor, low365 <= LOW_BAR),
        "graded365": _score(row, low365_factor, True)}
    for variant, source in (("gate30", "current_score"), ("graded30", "graded_score")):
        if row["scores"][variant] != _decimal(raw.get(source), f"row {number} {source}"):
            raise StudyError(f"row {number} {variant} differs from AB4")
    return row


def _quantile(values: Sequence[Decimal], tenth: int) -> Decimal:
    ordered = sorted(values)
    if not ordered: raise StudyError("distribution is empty")
    with localcontext() as context:
        context.prec = 50; position = Decimal(len(ordered) - 1) * Decimal(tenth) / Decimal("10")
        lower = int(position); upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _dist(values: Sequence[Decimal]) -> Dict[str, Any]:
    return {"count": len(values), "min": _render(min(values)),
            "median": _render(_quantile(values, 5)), "p90": _render(_quantile(values, 9)),
            "max": _render(max(values)), "nonzero_count": sum(value > 0 for value in values)}


def _lead(ignition: str, end: str) -> Decimal:
    delta = _utc(ignition, "ignition") - _utc(end, "window end")
    micros = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    return Decimal(micros) / Decimal("3600000000")


def _group(rows: Sequence[Mapping[str, Any]], history_class: str) -> Dict[str, Any]:
    chosen = [row for row in rows if row["eligible"] and row["history_class"] == history_class]
    runners = [row for row in chosen if row["dataset"] == "runner"]
    quiet = [row for row in chosen if row["dataset"] == "quiet"]
    output: Dict[str, Any] = {"populations": {"runner": len(runners), "quiet": len(quiet)},
                              "symbols": {kind: sorted({row["symbol"] for row in chosen if row["dataset"] == kind}) for kind in ("runner", "quiet")}, "variants": {}}
    for variant in VARIANTS:
        quiet_max = max(row["scores"][variant] for row in quiet)
        by_runner = {}
        for symbol in sorted({row["symbol"] for row in runners}):
            sample = [row for row in runners if row["symbol"] == symbol]
            above = sorted((row for row in sample if row["scores"][variant] > quiet_max), key=lambda row: _utc(row["window_start_utc"], "start"))
            first = above[0] if above else None
            earliest = None if first is None else {"window_start_utc": first["window_start_utc"], "window_end_utc": first["window_end_utc"],
                                                   "score": _render(first["scores"][variant]), "hours_before_ignition_from_window_end": _render(_lead(first["ignition_utc"], first["window_end_utc"]))}
            by_runner[symbol] = {"eligible_count": len(sample), "exceeding_count": len(above), "earliest_exceeding": earliest}
        output["variants"][variant] = {"distributions": {"runner": _dist([row["scores"][variant] for row in runners]),
                                                          "quiet": _dist([row["scores"][variant] for row in quiet])},
                                                "discrimination": {"quiet_max": _render(quiet_max), "runner_windows_above_quiet_max": sum(item["exceeding_count"] for item in by_runner.values()), "by_runner": by_runner}}
    return output


def _public(row: Mapping[str, Any]) -> Dict[str, Any]:
    keys = ("dataset", "symbol", "source_row", "window_start_utc", "window_end_utc", "eligible", "history_status", "history_class", "days")
    output = {key: row[key] for key in keys}
    if row["dataset"] == "runner": output["ignition_utc"] = row["ignition_utc"]
    output.update({"low_position_30": _render(row["low30"]), "low_position_365": _render(row["low365"]),
                   "gate30_pass": row["gates"]["at_multiweek_lows"], "gate365_pass": row["low365"] <= LOW_BAR,
                   "scores": {name: _render(row["scores"][name]) for name in VARIANTS}})
    return output


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", type=Path, default=DEFAULT_WINDOWS)
    parser.add_argument("--long-lows", type=Path, default=DEFAULT_LOWS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        lows = _load_object(args.long_lows)
        if lows.get("schema") != "long-low-positions-v1": raise StudyError("365-day schema differs")
        symbols = _object(lows.get("symbols"), "365-day symbols")
        rows = [_prepare(raw, number, symbols) for number, raw in enumerate(_load_lines(args.windows), 1)]
        groups = {"veteran_excluding_not_ready": _group(rows, "veteran"), "fresh_class_separate": _group(rows, "fresh")}
        totals = {variant: sum(group["variants"][variant]["discrimination"]["runner_windows_above_quiet_max"]
                               for group in groups.values()) for variant in VARIANTS}
        best = max(totals.values())
        summary = {"schema": "lows-matrix-validation-v1", "method": {
                              "gate30": "production CURRENT; window close is at or below the preceding close q20",
                              "graded30": "gate30 with only the hard lows gate removed",
                              "gate365": "substitute low_position_365 and pass at low_position_365 <= 0.20",
                              "graded365": "gate365 with only the hard lows gate removed",
                              "lows_factor": "1 minus clamp(low_position)",
                              "long_low_input": f"AB5 as_of={lows.get('as_of')}; one symbol scalar is reused across its windows",
                              "translation": "production uses close <= close q20; the inclusive scalar translation is low_position_365 <= 0.20"},
                   "input": {"all_rows": len(rows), "eligible_runner": sum(row["eligible"] and row["dataset"] == "runner" for row in rows),
                             "eligible_quiet": sum(row["eligible"] and row["dataset"] == "quiet" for row in rows)},
                   "not_ready_handling": {"excluded": "veteran group contains READY symbols only",
                                          "fresh_class_separate": "NOT-READY symbols are scored but reported only in the separate fresh group"},
                   "groups": groups, "max_separation": {"metric": "runner windows strictly above the quiet maximum in the matching history class",
                                      "counts": totals, "maximum": best,
                                      "variant": [name for name in VARIANTS if totals[name] == best],
                                      "sample_caveat": "one veteran runner and one fresh-listing runner"}}
        _write(args.output_dir / "window-scores.jsonl", map(_public, rows), lines=True)
        _write(args.output_dir / "lows-matrix-summary.json", summary)
    except (OSError, UnicodeError, StudyError) as error:
        print(f"validate lows matrix: {error}", file=sys.stderr); return 2
    print("AB6 | " + " | ".join((f"rows={len(rows)}", f"runner={summary['input']['eligible_runner']}",
                                  f"quiet={summary['input']['eligible_quiet']}",
                                  f"best={','.join(summary['max_separation']['variant'])}")))
    return 0


if __name__ == "__main__": raise SystemExit(main())
