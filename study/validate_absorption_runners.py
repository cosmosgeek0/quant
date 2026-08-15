#!/usr/bin/env python3
"""Measure absorption components in the 48 hours before known runners."""

from __future__ import annotations

import argparse, bisect, json, os, sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

try:
    from study import absorption, baselines
except ImportError:
    import absorption  # type: ignore
    import baselines  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = Path("/Users/cosmosgeek/Desktop/quant/study/data")
DEFAULT_OUTPUT = ROOT / "lane-logs" / "AB3"
DEFAULT_MANIFEST = ROOT / "study" / "out" / "v2" / "cohort_manifest.json"
WINDOW_MINUTES, HORIZON_HOURS = 60, 48
MIN_KLINE_DAYS, MIN_TAPE_DAYS, MINUTE_MS = 21, 7, 60_000
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
COMPONENT_NAMES = ("at_lows", "range_compression", "volume_floor",
                   "taker_imbalance_shift", "trade_size_shift")
RUNNERS = (("AAOIBUSDT", "2026-08-13T10:04:11Z", "local_program_record"), ("OPENUSDT", "2026-08-15T05:35:00Z", "dispatch_approximate_minute"))


class ValidationError(Exception): pass


def _utc_ms(text: str) -> int:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo != timezone.utc: raise ValidationError(f"timestamp is not UTC: {text}")
    delta = value - EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _write_json(path: Path, payload: Any, *, lines: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        if lines:
            for row in payload:
                handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        else:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":")); handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def _bluai_runner(path: Path) -> Tuple[Optional[Tuple[str, str, str]], Dict[str, str]]:
    if not path.is_file(): return None, {
        "label": "BLUAI", "reason": "cohort_manifest_absent", "status": "DROPPED"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError): return None, {
        "label": "BLUAI", "reason": "cohort_manifest_unreadable", "status": "DROPPED"}
    matches = sorted(
        row["symbol"] for row in payload
        if isinstance(row, Mapping)
        and isinstance(row.get("symbol"), str)
        and row["symbol"].startswith("BLUAI")
    ) if isinstance(payload, list) else []
    if len(matches) != 1:
        reason = "symbol_absent" if not matches else "symbol_ambiguous"
        return None, {"label": "BLUAI", "reason": reason, "status": "DROPPED"}
    return (matches[0], "2026-08-13T09:45:00Z", "program_record_approximate_minute"), {
        "label": "BLUAI", "reason": "exact_symbol_verified", "status": "INCLUDED"
    }


def _archive_days(data_dir: Path, symbol: str) -> set[str]:
    root = data_dir / "spot" if (data_dir / "spot" / "daily").is_dir() else data_dir
    prefix = f"{symbol}-aggTrades-"
    return {path.name[len(prefix):-4] for path in root.rglob(f"{prefix}*.zip")
            if path.is_file() and path.name.startswith(prefix) and path.name.endswith(".zip")}


def _decimal(raw: Optional[str]) -> Optional[Decimal]:
    return None if raw is None else Decimal(raw)


def _verdict(value: Optional[str], gate: bool, zero_reason: str, gate_reason: str) -> str:
    parsed = _decimal(value)
    if parsed is None: return "missing_evidence"
    if parsed == 0: return f"zero:{zero_reason}"
    if not gate: return f"nonzero:threshold_gate_failed:{gate_reason}"
    return "nonzero:gate_pass"


def _components(
    scored: Mapping[str, Any], close: Decimal, profile: absorption.HistoricalKlineBaseline
) -> Dict[str, Dict[str, Any]]:
    factors = scored["score_components"]
    feature = scored["features"]
    tape = profile.tape
    imbalance_median = None if tape is None else absorption.quantile(tape.window_taker_imbalance_samples, 1, 2)
    imbalance = _decimal(feature["taker_imbalance"])
    imbalance_gate = (imbalance is not None and imbalance_median is not None
                      and imbalance > 0 and imbalance > imbalance_median)
    imbalance_zero = "imbalance_not_positive" if imbalance is not None and imbalance <= 0 else "imbalance_not_above_median"
    median_ratio = _decimal(feature["trade_size_median_shift_ratio"])
    p90_ratio = _decimal(feature["trade_size_p90_shift_ratio"])
    size_gate = (median_ratio is not None and p90_ratio is not None
                 and (median_ratio > 1 or p90_ratio > 1))
    def item(name: str, gate: bool, zero_reason: str, gate_reason: str,
             raw: Mapping[str, Any]) -> Dict[str, Any]:
        value = factors[name]
        return {"factor": value, **raw, "gate_pass": gate,
                "verdict": _verdict(value, gate, zero_reason, gate_reason)}
    return {
        "at_lows": item("at_lows", feature["at_multiweek_lows"],
            "close_at_or_above_historical_high", "close_above_q20", {
             "raw_close": absorption.decimal_text(close), "raw_low_position": feature["low_position_in_history"],
             "threshold_close_q20": feature["low_reference_close_q20"]}),
        "range_compression": item("range_compression", feature["price_flat"],
            "range_above_all_baseline_samples", "range_above_q20", {
             "raw_range_pct": feature["window_range_pct"],
             "threshold_range_q20_pct": feature["baseline_window_range_q20_pct"],
             "reference_range_median_pct": feature["baseline_window_range_median_pct"]}),
        "volume_floor": item("volume_floor", not feature["volume_surging"],
            "quote_volume_is_zero", "volume_above_q90", {
             "raw_quote_volume": feature["window_quote_volume"],
             "threshold_volume_floor": feature["baseline_window_volume_floor"],
             "threshold_volume_q90": feature["baseline_window_volume_q90"]}),
        "taker_imbalance_shift": item("taker_imbalance_shift", imbalance_gate,
            imbalance_zero, "imbalance_gate", {
             "raw_imbalance": feature["taker_imbalance"],
             "raw_shift_vs_overall_baseline": feature["taker_imbalance_shift"],
             "threshold_window_median": None if imbalance_median is None
             else absorption.decimal_text(imbalance_median)}),
        "trade_size_shift": item("trade_size_shift", size_gate,
            "both_size_ratios_not_above_one", "size_ratio_gate", {
             "raw_median_quote": feature["trade_size_median_quote"],
             "raw_p90_quote": feature["trade_size_p90_quote"],
             "reference_median_quote": feature["baseline_trade_size_median_quote"],
             "reference_p90_quote": feature["baseline_trade_size_p90_quote"],
             "raw_median_ratio": feature["trade_size_median_shift_ratio"],
             "raw_p90_ratio": feature["trade_size_p90_shift_ratio"]}),
    }


def _score_rows(
    symbol: str, ignition: str, ignition_basis: str,
    bars: Sequence[absorption.KlineBar], trades: Sequence[absorption.AggTrade],
    profile: absorption.HistoricalKlineBaseline, archive_days: set[str],
) -> Tuple[Dict[str, Any], ...]:
    by_open = {bar.open_ms: bar for bar in bars}
    timestamps = tuple(trade.timestamp_ms for trade in trades)
    anchor = _utc_ms(ignition) // MINUTE_MS * MINUTE_MS
    first = anchor - HORIZON_HOURS * 60 * MINUTE_MS
    rows = []
    for offset in range(HORIZON_HOURS):
        start = first + offset * WINDOW_MINUTES * MINUTE_MS
        opens = tuple(start + minute * MINUTE_MS for minute in range(WINDOW_MINUTES))
        window = tuple(by_open[value] for value in opens if value in by_open)
        if len(window) != WINDOW_MINUTES: continue
        end = start + WINDOW_MINUTES * MINUTE_MS - 1
        left = bisect.bisect_left(timestamps, start)
        right = bisect.bisect_right(timestamps, end)
        tape_covered = {absorption.utc_text(value)[:10] for value in (start, end)} <= archive_days
        scored = absorption.score_absorption_window(
            symbol, (), window, (), trades[left:right] if tape_covered else (), historical_baseline=profile
        )
        components = _components(scored, window[-1].close, profile)
        values = [_decimal(components[name]["factor"]) for name in COMPONENT_NAMES]
        product: Optional[Decimal] = None
        if all(value is not None for value in values):
            product = Decimal("1")
            for value in values: product *= value  # type: ignore[operator]
        gates = {
            "all_components_known": not scored["unknown_features"],
            "at_multiweek_lows": scored["features"]["at_multiweek_lows"],
            "price_flat": scored["features"]["price_flat"],
            "volume_not_surging": not scored["features"]["volume_surging"],
            "not_pumping": not scored["features"]["pumping"],
        }
        rows.append({
            "schema": "absorption-runner-validation-v1", "symbol": symbol,
            "ignition_utc": ignition, "ignition_basis": ignition_basis,
            "window_start_utc": scored["window_start_utc"],
            "window_end_utc": scored["window_end_utc"], "status": "EVALUATED",
            "current_tape_archive_present": tape_covered,
            "window_tape_ready": tape_covered and not scored["unknown_features"],
            "trade_count": scored["features"]["trade_count"],
            "absorption_score": scored["absorption_score"],
            "component_product_before_outer_gates": None if product is None else absorption.decimal_text(product),
            "gates": gates, "components": components,
            "zero_causes": [name for name, passed in gates.items() if not passed]
            + [name for name, value in zip(COMPONENT_NAMES, values) if value == 0],
        })
    return tuple(rows)


def _rank(row: Mapping[str, Any]) -> Tuple[Any, ...]:
    values = [_decimal(row["components"][name]["factor"]) for name in COMPONENT_NAMES]
    known = [value for value in values if value is not None]
    return (_decimal(row.get("absorption_score")) or Decimal("0"),
            sum(bool(value) for value in row.get("gates", {}).values()),
            sum(value > 0 for value in known), sum(known, Decimal("0")), str(row["window_end_utc"]))


def _summary(runners: Sequence[Tuple[str, str, str]], rows: Sequence[Mapping[str, Any]],
             baseline_payload: Mapping[str, Any], manifest_result: Mapping[str, str]) -> Dict[str, Any]:
    results: Dict[str, Any] = {}
    for symbol, ignition, basis in runners:
        selected = [row for row in rows if row["symbol"] == symbol]
        tape_ready = [row for row in selected if row["window_tape_ready"]]
        candidates = tape_ready or selected
        best = max(candidates, key=_rank) if candidates else None
        tape_profile = baseline_payload.get("symbols", {}).get(symbol, {}).get("components", {}).get("tape")
        results[symbol] = {
            "ignition_utc": ignition, "ignition_basis": basis,
            "windows_scheduled": HORIZON_HOURS, "windows_evaluated": len(selected),
            "windows_missing_kline_evidence": HORIZON_HOURS - len(selected),
            "windows_tape_ready": len(tape_ready),
            "windows_nonzero_score": sum(Decimal(row["absorption_score"]) > 0 for row in selected),
            "baseline_tape": {key: tape_profile.get(key) for key in
                              ("status", "day_count", "days_found", "required_days", "reason")
                              if tape_profile is not None and key in tape_profile},
            "component_counts": {
                name: {
                    "nonzero": sum((_decimal(row["components"][name]["factor"]) or Decimal("0")) > 0 for row in selected),
                    "zero": sum(_decimal(row["components"][name]["factor"]) == 0 for row in selected),
                    "missing": sum(_decimal(row["components"][name]["factor"]) is None for row in selected),
                    "gate_pass": sum(bool(row["components"][name].get("gate_pass")) for row in selected),
                } for name in COMPONENT_NAMES
            },
            "best_window_selection": "score_then_outer_gates_then_nonzero_components_then_factor_sum_then_latest",
            "best_window": best,
        }
    return {
        "schema": "absorption-runner-validation-summary-v1",
        "parameters": {"window_minutes": WINDOW_MINUTES, "step_minutes": WINDOW_MINUTES,
                       "horizon_hours": HORIZON_HOURS, "minimum_kline_days": MIN_KLINE_DAYS,
                       "minimum_tape_days": MIN_TAPE_DAYS},
        "manifest_check": dict(manifest_result), "runners": results,
    }


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag, default in (("--data-dir", DEFAULT_DATA), ("--output-dir", DEFAULT_OUTPUT),
                          ("--cohort-manifest", DEFAULT_MANIFEST)):
        parser.add_argument(flag, type=Path, default=default)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = argument_parser().parse_args(argv)
    bluai, manifest_result = _bluai_runner(args.cohort_manifest)
    runners = RUNNERS + (() if bluai is None else (bluai,))
    symbols = tuple(item[0] for item in runners)
    baseline_path = args.output_dir / "runner-baselines.json"
    try:
        baseline_payload = baselines.run_absorption_baselines(
            args.data_dir, baseline_path, symbols=symbols, window_minutes=WINDOW_MINUTES,
            minimum_days=MIN_KLINE_DAYS, minimum_tape_days=MIN_TAPE_DAYS,
        )
        profiles = absorption.load_historical_baselines(baseline_path, expected_window_minutes=WINDOW_MINUTES)
        klines = absorption.load_klines(args.data_dir, symbols, spot_only=True)
        trades = absorption.load_agg_trades(args.data_dir, symbols, allow_empty=True, spot_only=True)
        rows = tuple(
            row for symbol, ignition, basis in runners if symbol in profiles and symbol in klines
            for row in _score_rows(symbol, ignition, basis, klines[symbol], trades.get(symbol, ()),
                                   profiles[symbol], _archive_days(args.data_dir, symbol))
        )
        if not rows: raise ValidationError("no runner windows could be scheduled")
        summary = _summary(runners, rows, baseline_payload, manifest_result)
        _write_json(args.output_dir / "runner-windows.jsonl", rows, lines=True)
        _write_json(args.output_dir / "runner-summary.json", summary)
    except (OSError, ValueError, baselines.BaselineError, absorption.AbsorptionError, ValidationError) as error:
        print(f"validate absorption runners: {error}", file=sys.stderr); return 2
    print("AB3 | " + " | ".join(
        f"{symbol}:evaluated={result['windows_evaluated']},tape_ready={result['windows_tape_ready']}"
        for symbol, result in summary["runners"].items()
    ) + f" | output={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
