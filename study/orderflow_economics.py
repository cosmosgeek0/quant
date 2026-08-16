#!/usr/bin/env python3
"""Measure AB25 exit economics at the OF2 order-flow cutoffs."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

try:
    from study import unconditional_exit_study as ab25
except ModuleNotFoundError as error:
    if error.name != "study":
        raise
    import unconditional_exit_study as ab25  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
OF2_WINDOWS = ROOT / "lane-logs/OF2/window-features.jsonl"
OF2_SUMMARY = ROOT / "lane-logs/OF2/orderflow-precision.json"
AB25_SUMMARY = ROOT / "lane-logs/AB25/unconditional-exit-summary.json"
HISTORY = ROOT / "study/data-1m-history"
CONTROL = ROOT / "study/data-canonical"
OUTPUT, REPORT = ROOT / "lane-logs/OF3", ROOT / "lane-logs/OF3-report.txt"
D = Decimal; PERCENTILES = (90, 95, 99); UNIVERSE = D("659")


class EconomicsError(Exception): pass


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EconomicsError(f"cannot read {path.name}: {error}") from None


def _decimal(raw: Any, label: str) -> Decimal:
    try:
        value = D(str(raw))
    except (InvalidOperation, ValueError):
        raise EconomicsError(f"{label} is not Decimal text") from None
    if not value.is_finite():
        raise EconomicsError(f"{label} is not finite")
    return value


def _inputs(windows_path: Path, summary_path: Path) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    summary = _read(summary_path)
    if not isinstance(summary, Mapping) or summary.get("schema") != "orderflow-precision-summary-v1":
        raise EconomicsError("OF2 summary schema differs")
    raw_points = summary.get("precision_at_density")
    if not isinstance(raw_points, list):
        raise EconomicsError("OF2 density points differ")
    points: dict[int, dict[str, Any]] = {}
    for raw in raw_points:
        if not isinstance(raw, Mapping) or raw.get("percentile") not in PERCENTILES:
            continue
        percentile = raw["percentile"]
        point = dict(raw)
        for key in ("cutoff", "false_alarms_per_observed_symbol_day", "true_fires_per_symbol_day_at_recall", "census_rate_standardized_precision"):
            point[key] = _decimal(raw.get(key), f"P{percentile} {key}")
        point["total_fires_per_symbol_day"] = point["false_alarms_per_observed_symbol_day"] + point["true_fires_per_symbol_day_at_recall"]
        points[percentile] = point
    if set(points) != set(PERCENTILES) or not points[90]["cutoff"] < points[95]["cutoff"] < points[99]["cutoff"]:
        raise EconomicsError("OF2 cutoff set differs")
    rows, seen = [], set()
    try:
        with open(windows_path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    raise EconomicsError(f"OF2 window line {number} is not JSON") from None
                if not isinstance(raw, Mapping) or raw.get("schema") != "orderflow-window-v1":
                    raise EconomicsError(f"OF2 window line {number} schema differs")
                window_id = raw.get("window_id")
                if not isinstance(window_id, str) or window_id in seen:
                    raise EconomicsError(f"OF2 window line {number} identity differs")
                seen.add(window_id)
                score_raw = raw.get("of1_pair_score")
                if score_raw is None:
                    continue
                score = _decimal(score_raw, f"OF2 line {number} score")
                if score < points[90]["cutoff"]:
                    continue
                group = raw.get("group")
                if group not in {"HELD", "COLLAPSED", "CONTROL"} or raw.get("status") != "MEASURED":
                    raise EconomicsError(f"OF2 firing line {number} state differs")
                try:
                    symbol = ab25.tape_pull._validate_symbol(raw.get("symbol"))
                    venue = ab25.tape_pull._validate_venue(raw.get("venue"))
                except ab25.tape_pull.TapePullError as error:
                    raise EconomicsError(f"OF2 firing line {number}: {error}") from None
                end_text = raw.get("window_end_utc_exclusive")
                entry_ms = ab25._utc_ms(end_text, f"OF2 line {number} end")
                tiers = [percentile for percentile in PERCENTILES if score >= points[percentile]["cutoff"]]
                rows.append({"schema": "orderflow-economics-trade-v1", "fire_id": window_id, "symbol": symbol,
                             "venue": venue, "source_class": "control" if group == "CONTROL" else "base",
                             "of2_group": group, "window_end_utc_exclusive": end_text, "entry_ms": entry_ms,
                             "of1_pair_score": score, "cutoff_percentiles": tiers})
    except (OSError, UnicodeError) as error:
        raise EconomicsError(f"cannot read OF2 windows: {error}") from None
    for percentile, point in points.items():
        selected = [row for row in rows if percentile in row["cutoff_percentiles"]]
        base = sum(row["source_class"] == "base" for row in selected)
        control = sum(row["source_class"] == "control" for row in selected)
        if base != point.get("base_fires") or control != point.get("control_fires"):
            raise EconomicsError(f"P{percentile} firing count differs")
    return rows, points


def _simulate_group(root: Path, rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], Counter[str], int, int]:
    bars, archives, parsed = ab25._bars((root,), rows)
    output, coverage = [], Counter()
    for source in rows:
        row = dict(source)
        entry_ms = row.pop("entry_ms")
        trigger = bars.get(entry_ms - ab25.MINUTE_MS)
        window = [bars.get(entry_ms + minute * ab25.MINUTE_MS) for minute in range(1_441)]
        complete = trigger is not None and trigger.close_ms == entry_ms - 1 and not any(bar is None for bar in window)
        status = "MEASURED" if complete else "ABSENT"
        coverage[status] += 1
        if not complete:
            row.update({"evidence_status": status, "absence_reason": "trigger or complete following 24h kline evidence is absent",
                        "trigger_price": None, "rules": {rule: None for rule in ab25.RULES}})
        else:
            proved = [bar for bar in window if bar is not None]
            rules = {rule: ab25._simulate(trigger.close, proved, rule) for rule in ab25.RULES}
            if len({item["entry_rungs_filled"] for item in rules.values()}) != 1:
                raise EconomicsError(f"{row['fire_id']} fixed entry differs")
            row.update({"evidence_status": status, "absence_reason": None, "trigger_price": trigger.close, "rules": rules})
        output.append(row)
    return output, coverage, archives, parsed


def _weighted_quantile(values: Sequence[tuple[Decimal, Decimal]], fraction: Decimal) -> Optional[Decimal]:
    if not values:
        return None
    total = sum((weight for _value, weight in values), D("0"))
    target, cumulative = total * fraction, D("0")
    for value, weight in sorted(values):
        cumulative += weight
        if cumulative >= target:
            return value
    return max(value for value, _weight in values)


def _stats(rows: Sequence[Mapping[str, Any]], rule: str, point: Mapping[str, Any]) -> dict[str, Any]:
    measured = [row for row in rows if row["evidence_status"] == "MEASURED"]
    weighted: list[tuple[Mapping[str, Any], Decimal]] = []
    densities = {"base": point["true_fires_per_symbol_day_at_recall"],
                 "control": point["false_alarms_per_observed_symbol_day"]}
    class_support = {}
    for source_class, density in densities.items():
        taken = [row for row in rows if row["source_class"] == source_class]
        found = [row for row in measured if row["source_class"] == source_class]
        class_support[source_class] = {"trades_taken": len(taken), "trades_measured": len(found), "fires_per_symbol_day": density}
        if density > 0 and not found:
            return {"trades_taken": len(rows), "trades_measured": len(measured), "class_support": class_support,
                    "win_rate": None, "median_net_return_pct": None, "mean_net_return_pct": None,
                    "p5_net_return_pct": None, "stopped_out_fraction": None, "net_pct_points_per_symbol_day": None}
        if found:
            weight = density / D(len(found))
            weighted.extend((row["rules"][rule], weight) for row in found)
    total_density = sum(densities.values(), D("0"))
    values = [(item["net_return_pct"], weight) for item, weight in weighted]
    aggregate = sum((value * weight for value, weight in values), D("0"))
    return {"trades_taken": len(rows), "trades_measured": len(measured), "class_support": class_support,
            "win_rate": sum((weight for item, weight in weighted if item["net_return_pct"] > 0), D("0")) / total_density,
            "median_net_return_pct": _weighted_quantile(values, D("0.5")),
            "mean_net_return_pct": aggregate / total_density,
            "p5_net_return_pct": _weighted_quantile(values, D("0.05")),
            "stopped_out_fraction": sum((weight for item, weight in weighted if item["stopped_out"]), D("0")) / total_density,
            "net_pct_points_per_symbol_day": aggregate}


def _short(value: Optional[Decimal], *, rate: bool = False) -> str:
    if value is None:
        return "NA"
    return format(value * D("100") if rate else value, ".8g")


def run(windows: Path, of2_summary: Path, ab25_summary: Path, history: Path, control: Path,
        output: Path, report: Path, workers: int) -> Mapping[str, Any]:
    if ab25.absorption.tape_pull.stream_verified_member is not ab25.tape_pull.stream_verified_member:
        raise EconomicsError("shared ZIP reader binding differs")
    if (ab25.ENTRY, ab25.TAKER, ab25.SPREAD, ab25.SLIPPAGE, ab25.RULES) != (
            ((D("0.40"), D("0")), (D("0.30"), D("2")), (D("0.30"), D("5"))), D("0.001"), D("0.002"), D("0.002"), ("R1", "R2", "R3")):
        raise EconomicsError("AB25 simulation contract differs")
    fires, points = _inputs(windows, of2_summary)
    grouped: dict[tuple[Path, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in fires:
        root = control if row["source_class"] == "control" else history
        grouped[(root, row["symbol"], row["venue"])].append(row)
    results, coverage, archives, parsed = [], Counter(), 0, 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_simulate_group, root, rows) for (root, _symbol, _venue), rows in grouped.items()]
        for count, future in enumerate(as_completed(futures), 1):
            found, proved, archive_count, parsed_count = future.result()
            results.extend(found); coverage.update(proved); archives += archive_count; parsed += parsed_count
            if count % 20 == 0 or count == len(futures):
                print(f"OF3 | groups={count}/{len(futures)} | fires={len(results)}", flush=True)
    results.sort(key=lambda row: (row["symbol"], row["window_end_utc_exclusive"], row["fire_id"]))
    prior = _read(ab25_summary)
    if not isinstance(prior, Mapping) or prior.get("schema") != "unconditional-exit-study-v1":
        raise EconomicsError("AB25 summary schema differs")
    comparison = {rule: _decimal(prior["tiers"]["at_least_3"][rule]["all"]["net_pct_points_per_measured_symbol_day"], f"AB25 {rule} aggregate") for rule in ab25.RULES}
    tiers = {}
    for percentile in PERCENTILES:
        selected = [row for row in results if percentile in row["cutoff_percentiles"]]
        tiers[f"p{percentile}"] = {"cutoff": points[percentile]["cutoff"],
            "fires_per_symbol_day": points[percentile]["total_fires_per_symbol_day"],
            "entries_per_659_symbol_day": points[percentile]["total_fires_per_symbol_day"] * UNIVERSE,
            "precision": points[percentile]["census_rate_standardized_precision"],
            "coverage": {"MEASURED": sum(row["evidence_status"] == "MEASURED" for row in selected),
                         "ABSENT": sum(row["evidence_status"] == "ABSENT" for row in selected)},
            "rules": {rule: _stats(selected, rule, points[percentile]) for rule in ab25.RULES}}
    summary = {"schema": "orderflow-economics-summary-v1", "basis": "MEASURED",
        "question": "do OF2 percentile fires clear zero after AB25 entry, exit, and cost rules",
        "source": {"windows": str(windows), "of2_summary": str(of2_summary), "ab25_summary": str(ab25_summary),
                   "control_root": str(control), "base_root": str(history),
                   "reader": "study.tape_pull.stream_verified_member via AB25 study.absorption._kline_rows",
                   "archives_read": archives, "bars_parsed": parsed},
        "definitions": {"firing_set": "every scored OF2 base or control window at or above its pooled cutoff",
            "trigger": "close of the final 1m bar in the OF2 window; the following minute starts AB25's 24h clock",
            "simulation": "study.unconditional_exit_study._simulate with its 40/30/30 ladder, R1/R2/R3, and per-side 10 bps taker plus 20 bps spread plus 20 bps slippage",
            "standardization": "within each source class, equal-trade covered outcomes are multiplied by OF2 true-fire or false-fire density; their sum is net percentage-points per symbol-day",
            "universe_entries": "OF2 total fires per symbol-day multiplied by 659"},
        "coverage": dict(coverage), "tiers": tiers, "ab25_aggregate_family_at_least_3": comparison,
        "limits": "absent forward outcomes remain unknown; overlapping entries are independent; 1m OHLC does not prove within-bar path, liquidity, queue, funding, or executable fills; no thresholds are proposed"}
    coverage_text = []
    for percentile in PERCENTILES:
        selected = [row for row in results if percentile in row["cutoff_percentiles"]]
        base = [row for row in selected if row["source_class"] == "base"]
        controls = [row for row in selected if row["source_class"] == "control"]
        coverage_text.append(f"P{percentile}={len(selected)}/{tiers[f'p{percentile}']['coverage']['MEASURED']} base={len(base)}/{sum(row['evidence_status'] == 'MEASURED' for row in base)} control={len(controls)}/{sum(row['evidence_status'] == 'MEASURED' for row in controls)}")
    lines = [f"SCOPE_COVERAGE | firing/measured {'; '.join(coverage_text)}; archives={archives}; bars={parsed}; shared verified ZIP reader; metrics use OF2 density-standardized base/control covered outcomes | MEASURED",
             "TRADES_PER_DAY | " + "; ".join(f"P{percentile} fires/symbol-day={_short(tiers[f'p{percentile}']['fires_per_symbol_day'])} x659={_short(tiers[f'p{percentile}']['entries_per_659_symbol_day'])} entries/day" for percentile in PERCENTILES) + " | MEASURED"]
    for percentile in PERCENTILES:
        for rule in ab25.RULES:
            item = tiers[f"p{percentile}"]["rules"][rule]
            lines.append(f"P{percentile} {rule} | trades taken/measured={item['trades_taken']}/{item['trades_measured']} win={_short(item['win_rate'], rate=True)}% net-med/mean={_short(item['median_net_return_pct'])}/{_short(item['mean_net_return_pct'])}% p5={_short(item['p5_net_return_pct'])}% stopped={_short(item['stopped_out_fraction'], rate=True)}% net-pp/symbol-day(covered)={_short(item['net_pct_points_per_symbol_day'])} | MEASURED")
    of3_text = "; ".join(f"P{percentile}=" + "/".join(_short(tiers[f'p{percentile}']['rules'][rule]["net_pct_points_per_symbol_day"]) for rule in ab25.RULES) for percentile in PERCENTILES)
    aggregate_values = [tiers[f"p{percentile}"]["rules"][rule]["net_pct_points_per_symbol_day"] for percentile in PERCENTILES for rule in ab25.RULES]
    positive = sum(value is not None and value > 0 for value in aggregate_values)
    lines.append("COMPARISON_LIMITS | R1/R2/R3 net-pp/symbol-day: AB25 aggregate-family >=3=" + "/".join(_short(comparison[rule]) for rule in ab25.RULES) + f"; OF3 {of3_text}; positive OF3 aggregates={positive}/9; absent outcomes remain unknown; overlapping entries are independent; no executable fills or thresholds are proposed | MEASURED")
    if len(lines) != 12:
        raise EconomicsError("report row count differs")
    ab25._write(output / "orderflow-economics-trades.jsonl", results, lines=True)
    ab25._write(output / "orderflow-economics-summary.json", summary)
    ab25._write(output / "orderflow-economics-report.txt", "\n".join(lines) + "\n", plain=True)
    ab25._write(report, "\n".join(lines) + "\n", plain=True)
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag, default in (("--windows", OF2_WINDOWS), ("--of2-summary", OF2_SUMMARY), ("--ab25-summary", AB25_SUMMARY),
                          ("--history", HISTORY), ("--control-data", CONTROL), ("--output-dir", OUTPUT), ("--report", REPORT)):
        parser.add_argument(flag, type=Path, default=default)
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=8)
    args = parser.parse_args(argv)
    getcontext().prec = 50
    try:
        if not args.history.is_dir() or not args.control_data.is_dir():
            raise EconomicsError("kline root is absent")
        result = run(args.windows, args.of2_summary, args.ab25_summary, args.history, args.control_data,
                     args.output_dir, args.report, args.workers)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, InvalidOperation, EconomicsError,
            ab25.StudyError, ab25.absorption.AbsorptionError, ab25.tape_pull.TapePullError) as error:
        print(f"order-flow economics: {error}", file=sys.stderr)
        return 2
    p90_count = result["tiers"]["p90"]["rules"]["R1"]["trades_taken"]
    print(f"OF3 | P90 fires={p90_count} | output={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
