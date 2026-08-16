#!/usr/bin/env python3
"""Measure AB21 exits at every rise-eligible AB9 alarm."""

from __future__ import annotations

import argparse, json, os, sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

try:
    from study import absorption, tape_pull
except ModuleNotFoundError as error:
    if error.name != "study": raise
    import absorption, tape_pull  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "lane-logs/AB9/signal-windows.jsonl"
POPULATION = ROOT / "lane-logs/AB9/population.json"
AB9_SUMMARY = ROOT / "lane-logs/AB9/signal-base-rate.json"
HISTORY = ROOT / "study/data-1m-history"; RECENT = ROOT / "study/data"
OUTPUT = ROOT / "lane-logs/AB25"; REPORT = ROOT / "lane-logs/AB25-report.txt"
D = Decimal; MINUTE_MS = 60_000; HOUR_MS = 3_600_000; DAY_MS = 86_400_000
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
ENTRY = ((D("0.40"), D("0")), (D("0.30"), D("2")), (D("0.30"), D("5")))
TAKER, SPREAD, SLIPPAGE = D("0.001"), D("0.002"), D("0.002")
RULES = ("R1", "R2", "R3"); GROUPS = ("all", "census", "control")
TIERS = (("at_least_3", ">=3"), ("at_least_4", ">=4"), ("exactly_5", "=5"))

class StudyError(Exception): pass

def _json(path: Path) -> Any:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error: raise StudyError(f"cannot read {path}: {error}") from None

def _utc_ms(raw: Any, label: str) -> int:
    if not isinstance(raw, str): raise StudyError(f"{label} is not canonical UTC text")
    try: moment = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError: raise StudyError(f"{label} is not canonical UTC text") from None
    delta = moment - EPOCH; value = (delta.days * 86_400 + delta.seconds) * 1_000 + delta.microseconds // 1_000
    if absorption.utc_text(value) != raw: raise StudyError(f"{label} is not canonical UTC text")
    return value

def _tier(row: Mapping[str, Any], name: str) -> bool:
    count = row["fired_component_count"]
    return count >= 3 if name == "at_least_3" else count >= 4 if name == "at_least_4" else count == 5

def _inputs(windows_path: Path, population_path: Path, summary_path: Path) -> tuple[list[dict[str, Any]], int]:
    population, summary = _json(population_path), _json(summary_path)
    if population.get("schema") != "signal-base-rate-population-v1" or summary.get("schema") != "signal-base-rate-summary-v1": raise StudyError("AB9 schema differs")
    census, control = set(population.get("census_symbols", ())), set(population.get("control_symbols", ()))
    if len(census) != population.get("unique_census_symbol_count") or len(control) != population.get("control_symbol_count") or census & control: raise StudyError("AB9 population differs")
    membership = {symbol: "census" for symbol in census} | {symbol: "control" for symbol in control}
    rows, seen, scheduled = [], set(), 0
    try:
        with open(windows_path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip(): continue
                scheduled += 1
                try: raw = json.loads(line)
                except json.JSONDecodeError: raise StudyError(f"AB9 window line {number} is not JSON") from None
                if not isinstance(raw, Mapping) or raw.get("schema") != "signal-base-rate-window-v1": raise StudyError(f"AB9 window line {number} differs")
                try: symbol = tape_pull._validate_symbol(raw.get("symbol")); venue = tape_pull._validate_venue(raw.get("venue"))
                except tape_pull.TapePullError as error: raise StudyError(f"AB9 window line {number}: {error}") from None
                group = raw.get("group"); start = _utc_ms(raw.get("window_start_utc"), f"AB9 line {number} start"); end = _utc_ms(raw.get("window_end_utc"), f"AB9 line {number} end")
                identity = (symbol, end)
                if group != membership.get(symbol) or end - start != HOUR_MS - 1 or identity in seen or not isinstance(raw.get("inside_episode_rise"), bool): raise StudyError(f"AB9 window line {number} identity differs")
                seen.add(identity)
                if raw.get("status") != "EVALUATED_TAPE_READY" or raw["inside_episode_rise"]: continue
                count = raw.get("fired_component_count"); components = raw.get("fired_components")
                if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 5 or not isinstance(components, list) or len(components) != count: raise StudyError(f"AB9 window line {number} firing differs")
                rows.append({"alarm_id": f"{symbol}|{raw['window_end_utc']}", "symbol": symbol, "venue": venue, "group": group,
                             "window_end_utc": raw["window_end_utc"], "entry_ms": end + 1, "fired_component_count": count,
                             "true_alarm_72h": bool(raw.get("following_72h_episode_ids"))})
    except (OSError, UnicodeError) as error: raise StudyError(f"cannot read AB9 windows: {error}") from None
    if set(membership) != {row["symbol"] for row in rows}: raise StudyError("AB9 eligible symbol coverage differs")
    for label in GROUPS:
        selected = rows if label == "all" else [row for row in rows if row["group"] == label]
        source = summary.get("groups", {}).get(label, {}); eligible = source.get("windows_tape_ready", -1) - source.get("rise_windows_excluded", -1)
        if len(selected) != eligible: raise StudyError(f"AB9 {label} eligible count differs")
        for name, _display in TIERS:
            if sum(_tier(row, name) for row in selected) != source.get("tiers", {}).get(name, {}).get("alarms"): raise StudyError(f"AB9 {label} {name} alarm count differs")
    if scheduled != summary.get("groups", {}).get("all", {}).get("windows_scheduled"): raise StudyError("AB9 scheduled count differs")
    return rows, scheduled

def _bars(roots: Sequence[Path], rows: Sequence[Mapping[str, Any]]) -> tuple[dict[int, absorption.KlineBar], int, int]:
    symbol, venue = rows[0]["symbol"], rows[0]["venue"]; lower = min(row["entry_ms"] for row in rows) - MINUTE_MS; upper = max(row["entry_ms"] for row in rows) + DAY_MS
    days, months, cursor = set(), set(), lower - lower % DAY_MS
    while cursor <= upper:
        day = absorption.utc_text(cursor)[:10]; days.add(day); months.add(day[:7]); cursor += DAY_MS
    prefix = Path("spot") if venue == "spot" else Path("futures/um"); paths = set()
    for root in roots:
        base = root / prefix
        paths.update(base / "monthly/klines" / symbol / "1m" / f"{symbol}-1m-{month}.zip" for month in months)
        paths.update(base / "daily/klines" / symbol / "1m" / f"{symbol}-1m-{day}.zip" for day in days)
    indexed: dict[int, absorption.KlineBar] = {}; archives = parsed = 0
    for path in sorted(path for path in paths if path.is_file()):
        archives += 1
        for bar in absorption._kline_rows(path, symbol):
            parsed += 1
            if not lower <= bar.open_ms <= upper: continue
            prior = indexed.get(bar.open_ms)
            if prior is not None and prior != bar: raise StudyError(f"{symbol} kline evidence conflicts")
            indexed[bar.open_ms] = bar
    return indexed, archives, parsed

def _simulate(trigger: Decimal, window: Sequence[absorption.KlineBar], rule: str) -> dict[str, Any]:
    entries, exits = [], []
    with localcontext() as context:
        context.prec = 50
        def buy(weight: Decimal, gain: Decimal, minute: int) -> None:
            budget = weight; price = trigger * (D("1") + gain / D("100")); gross = budget / (D("1") + SPREAD + SLIPPAGE)
            entries.append({"quantity": gross / price, "gross": gross, "budget": budget, "fee": budget * TAKER, "minute": minute, "price": price})
        def sell(quantity: Decimal, price: Decimal, minute: int, reason: str) -> None:
            gross = quantity * price; executed = gross * (D("1") - SPREAD - SLIPPAGE)
            exits.append({"quantity": quantity, "gross": gross, "executed": executed, "fee": executed * TAKER, "minute": minute, "reason": reason})
        buy(*ENTRY[0], 0); next_rung = 1; position = acquired = entries[0]["quantity"]
        running = trigger; trail_active = False; targets: set[int] = set(); terminal = ""
        for minute, bar in enumerate(window[:-1], 1):
            average = sum((item["gross"] for item in entries), D("0")) / acquired; hard = average * D("0.98"); dynamic = None; dynamic_reason = ""
            if rule == "R2" and trail_active: dynamic, dynamic_reason = running * D("0.75"), "trailing_stop"
            elif rule == "R3" and len(entries) == len(ENTRY) and running > average: dynamic, dynamic_reason = average + (running - average) / D("2"), "giveback_exit"
            stop = hard if dynamic is None or hard >= dynamic else dynamic; reason = "hard_stop" if stop == hard else dynamic_reason
            if bar.open <= stop or bar.low <= stop:
                sell(position, bar.open if bar.open <= stop else stop, minute, reason); terminal = reason; position = D("0"); break
            entered = False
            while minute <= 120 and next_rung < len(ENTRY) and bar.high >= trigger * (D("1") + ENTRY[next_rung][1] / D("100")):
                buy(*ENTRY[next_rung], minute); position += entries[-1]["quantity"]; acquired += entries[-1]["quantity"]; next_rung += 1; entered = True
            average = sum((item["gross"] for item in entries), D("0")) / acquired
            if not entered and rule in {"R1", "R3"}:
                for index, gain in enumerate((D("10"), D("25"))):
                    if index not in targets and bar.high >= average * (D("1") + gain / D("100")):
                        quantity = min(position, acquired * D("0.50")); sell(quantity, average * (D("1") + gain / D("100")), minute, "target")
                        position -= quantity; targets.add(index)
                        if position == 0: terminal = "targets_complete"; break
                if position == 0: break
            running = max(running, bar.high)
            if rule == "R2" and running >= average * D("1.10"): trail_active = True
        if position > 0: sell(position, window[-1].open, 1_440, "time_stop"); terminal = "time_stop"
        entry_budget = sum((item["budget"] for item in entries), D("0")); gross_entry = sum((item["gross"] for item in entries), D("0")); gross_exit = sum((item["gross"] for item in exits), D("0"))
        entry_fee = sum((item["fee"] for item in entries), D("0")); exit_fee = sum((item["fee"] for item in exits), D("0")); exit_cash = sum((item["executed"] - item["fee"] for item in exits), D("0"))
        net = exit_cash - entry_budget - entry_fee; market_cost = entry_budget - gross_entry + sum((item["gross"] - item["executed"] for item in exits), D("0"))
        if sum((item["quantity"] for item in exits), D("0")) != acquired or abs(net - (gross_exit - gross_entry - market_cost - entry_fee - exit_fee)) > D("1e-40"): raise StudyError("simulated cash flow differs")
        return {"rule": rule, "net_return_pct": net / entry_budget * D("100"), "entry_rungs_filled": len(entries),
                "never_filled_rung_2": len(entries) == 1, "terminal_reason": terminal,
                "stopped_out": terminal in {"hard_stop", "trailing_stop", "giveback_exit"}}

def _group(roots: Sequence[Path], rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], Counter[str], int, int]:
    bars, archives, parsed = _bars(roots, rows); output, coverage = [], Counter()
    for row in rows:
        trigger_bar = bars.get(row["entry_ms"] - MINUTE_MS); window = [bars.get(row["entry_ms"] + minute * MINUTE_MS) for minute in range(1_441)]
        complete = trigger_bar is not None and trigger_bar.close_ms == row["entry_ms"] - 1 and not any(bar is None for bar in window)
        coverage["MEASURED" if complete else "ABSENT"] += 1
        if row["fired_component_count"] < 3: continue
        base = {key: row[key] for key in ("alarm_id", "symbol", "venue", "group", "window_end_utc", "fired_component_count", "true_alarm_72h")}
        if not complete:
            output.append(base | {"evidence_status": "ABSENT", "absence_reason": "trigger or complete following 24h kline evidence is absent", "trigger_price": None, "rules": {rule: None for rule in RULES}}); continue
        proved = [bar for bar in window if bar is not None]; rules = {rule: _simulate(trigger_bar.close, proved, rule) for rule in RULES}
        if len({item["entry_rungs_filled"] for item in rules.values()}) != 1: raise StudyError(f"{row['alarm_id']} fixed entry differs")
        output.append(base | {"evidence_status": "MEASURED", "absence_reason": None, "trigger_price": trigger_bar.close, "rules": rules})
    return output, coverage, archives, parsed

def _quantile(values: Sequence[Decimal], fraction: Decimal) -> Decimal:
    ordered = sorted(values); position = D(len(ordered) - 1) * fraction; lower = int(position); upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

def _stats(rows: Sequence[Mapping[str, Any]], rule: str, measured_days: Decimal, full_days: Decimal) -> dict[str, Any]:
    measured = [row["rules"][rule] for row in rows if row["evidence_status"] == "MEASURED"]; values = [row["net_return_pct"] for row in measured]
    if not measured: return {"trades_taken": len(rows), "trades_measured": 0, "observed_symbol_days": full_days, "return_measured_symbol_days": measured_days,
                             "never_filled_rung_2_fraction": None, "median_net_return_pct": None, "mean_net_return_pct": None, "win_rate": None,
                             "p5_net_return_pct": None, "stopped_out_fraction": None, "net_return_sum_pct_points": None,
                             "net_pct_points_per_measured_symbol_day": None, "net_pct_points_per_full_symbol_day": None}
    count = D(len(measured)); total = sum(values, D("0")); full = total / full_days if measured_days == full_days else None
    return {"trades_taken": len(rows), "trades_measured": len(measured), "observed_symbol_days": full_days, "return_measured_symbol_days": measured_days,
            "never_filled_rung_2_fraction": D(sum(row["never_filled_rung_2"] for row in measured)) / count,
            "median_net_return_pct": _quantile(values, D("0.5")), "mean_net_return_pct": total / count,
            "win_rate": D(sum(value > 0 for value in values)) / count, "p5_net_return_pct": _quantile(values, D("0.05")),
            "stopped_out_fraction": D(sum(row["stopped_out"] for row in measured)) / count, "net_return_sum_pct_points": total,
            "net_pct_points_per_measured_symbol_day": None if measured_days == 0 else total / measured_days, "net_pct_points_per_full_symbol_day": full}

def _serial(value: Any) -> Any:
    if isinstance(value, Decimal):
        text = format(value, "f"); return text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, Mapping): return {key: _serial(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)): return [_serial(item) for item in value]
    return value

def _write(path: Path, payload: Any, *, lines: bool = False, plain: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        if plain: handle.write(payload)
        elif lines:
            for row in payload: handle.write(json.dumps(_serial(row), sort_keys=True, separators=(",", ":")) + "\n")
        else: json.dump(_serial(payload), handle, sort_keys=True, separators=(",", ":")); handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)

def _short(value: Optional[Decimal], *, rate: bool = False) -> str:
    if value is None: return "NA"
    return format(value * D("100") if rate else value, ".8g")

def run(windows: Path, population: Path, ab9_summary: Path, roots: Sequence[Path], output: Path, report: Path, workers: int) -> Mapping[str, Any]:
    if absorption.tape_pull.stream_verified_member is not tape_pull.stream_verified_member: raise StudyError("shared ZIP reader binding differs")
    eligible, scheduled = _inputs(windows, population, ab9_summary); grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in eligible: grouped[(row["venue"], row["symbol"])].append(row)
    alarm_rows, coverage, archives, parsed = [], Counter(), 0, 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_group, roots, rows) for rows in grouped.values()]
        for ordinal, future in enumerate(as_completed(futures), 1):
            found, proved, archive_count, parsed_count = future.result(); alarm_rows.extend(found); coverage.update(proved); archives += archive_count; parsed += parsed_count
            if ordinal % 10 == 0 or ordinal == len(futures): print(f"AB25 | symbols={ordinal}/{len(futures)} | alarms={len(alarm_rows)}", flush=True)
    alarm_rows.sort(key=lambda row: (row["symbol"], row["window_end_utc"])); eligible_by_group = Counter(row["group"] for row in eligible)
    measured_by_group = Counter(row["group"] for row in alarm_rows if row["evidence_status"] == "MEASURED")
    eligible_by_group["all"] = len(eligible); measured_by_group["all"] = coverage["MEASURED"]
    rules: dict[str, Any] = {}
    for tier, _display in TIERS:
        selected = [row for row in alarm_rows if _tier(row, tier)]; rules[tier] = {}
        for rule in RULES:
            rules[tier][rule] = {}
            for group in GROUPS:
                subset = selected if group == "all" else [row for row in selected if row["group"] == group]
                rules[tier][rule][group] = _stats(subset, rule, D(measured_by_group[group]) / D("6"), D(eligible_by_group[group]) / D("6"))
    result = {"schema": "unconditional-exit-study-v1", "basis": "MEASURED", "question": "what AB21 exits return when entered at every rise-eligible AB9 alarm",
              "source": {"windows": str(windows), "population": str(population), "ab9_summary": str(ab9_summary), "roots_present": [str(root) for root in roots if root.is_dir()],
                         "reader": "study.tape_pull.stream_verified_member via study.absorption._kline_rows", "archives_read": archives, "bars_parsed": parsed},
              "coverage": {"windows_scheduled": scheduled, "rise_eligible_tape_ready_windows": len(eligible), "forward_return_evidence": dict(coverage),
                           "eligible_by_group": dict(eligible_by_group), "measured_by_group": dict(measured_by_group)},
              "definitions": {"alarm": "AB9 EVALUATED_TAPE_READY window with fired count at least 3, excluding windows overlapping an episode rise",
                "tiers": ">=3, >=4, and =5 overlap by definition", "trigger": "close of the final 1m bar in the AB9 window; the following minute starts the exit clock",
                "entry": "quote-budget weights 40/30/30 percent at trigger/+2/+5 percent; later rungs expire after the first 120 following 1m bars",
                "costs": "each side: 10 bps taker fee, 20 bps spread, and 20 bps slippage; returns use filled entry budget",
                "bar_order": "protective thresholds proved before a bar are checked first; a rung-fill bar has no favorable exit; current highs affect dynamic exits from the next bar; gaps through protection use the bar open",
                "R1": "50 percent of acquired quantity at average-entry +10 percent and 50 percent at +25 percent; hard stop -2 percent; 24h time stop",
                "R2": "all quantity trails 25 percent below the prior running high after +10 percent; hard stop -2 percent; 24h time stop",
                "R3": "after all three entry rungs fill, R1 targets plus all remaining quantity exits at average entry plus half the prior running gain; hard stop -2 percent; 24h time stop",
                "aggregate": "sum of equal-trade net return percentage-points divided by AB9 rise-eligible, tape-ready symbol-days with complete forward evidence"},
              "tiers": rules, "limits": "absent forward windows remain unknown; independent overlapping alarms are not capital-netted; 1m OHLC cannot prove within-bar path, liquidity, queue, funding, or executable fills"}
    def compact(item: Mapping[str, Any]) -> str:
        aggregate = item["net_pct_points_per_full_symbol_day"] if item["net_pct_points_per_full_symbol_day"] is not None else item["net_pct_points_per_measured_symbol_day"]
        suffix = "" if item["net_pct_points_per_full_symbol_day"] is not None else "(covered)"
        return (f"taken/measured={item['trades_taken']}/{item['trades_measured']} r2-unfilled={_short(item['never_filled_rung_2_fraction'], rate=True)}% "
                f"net-med/mean={_short(item['median_net_return_pct'])}/{_short(item['mean_net_return_pct'])}% win={_short(item['win_rate'], rate=True)}% "
                f"p5={_short(item['p5_net_return_pct'])}% stopped={_short(item['stopped_out_fraction'], rate=True)}% net-pp/symbol-day{suffix}={_short(aggregate)}")
    counts = [sum(_tier(row, name) for row in alarm_rows) for name, _display in TIERS]
    lines = [f"scope | AB9 scheduled={scheduled}; rise-eligible tape-ready={len(eligible)}; alarm trades >=3/>=4/=5={counts[0]}/{counts[1]}/{counts[2]}; forward MEASURED/ABSENT={coverage['MEASURED']}/{coverage['ABSENT']}; observed/return-measured symbol-days={_short(D(len(eligible))/D('6'))}/{_short(D(coverage['MEASURED'])/D('6'))}; archives={archives}; bars={parsed}; shared verified ZIP reader | MEASURED",
             "definitions | trigger=AB9 window-final 1m close; entry=40/30/30 quote-budget percent at trigger/+2/+5 with later rungs limited to 2h; R1=50 percent at +10/+25; R2=25 percent trail after +10; R3=R1 plus half-running-gain giveback after all rungs fill; hard=-2 percent; horizon=24h; each side=10bps taker+20bps spread+20bps slippage; rise windows excluded; false alarms included | MEASURED",
             "coverage and limits | aggregate is sum of equal-trade net percentage-points per return-measured rise-eligible symbol-day; a full-period aggregate is NA when forward evidence is absent; overlapping alarms are independent; absent returns stay unknown; no within-bar path, liquidity, queue, funding, or executable fill is claimed | MEASURED"]
    for tier, display in TIERS:
        for rule in RULES:
            item = rules[tier][rule]
            lines.append(f"{rule} {display} | ALL {compact(item['all'])}; CENSUS {compact(item['census'])}; CONTROL {compact(item['control'])} | MEASURED")
    _write(output / "unconditional-exit-trades.jsonl", alarm_rows, lines=True); _write(output / "unconditional-exit-summary.json", result)
    _write(output / "unconditional-exit-report.txt", "\n".join(lines) + "\n", plain=True); _write(report, "\n".join(lines) + "\n", plain=True)
    return result

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag, default in (("--windows", WINDOWS), ("--population", POPULATION), ("--ab9-summary", AB9_SUMMARY), ("--history", HISTORY),
                          ("--recent-data", RECENT), ("--output-dir", OUTPUT), ("--report", REPORT)): parser.add_argument(flag, type=Path, default=default)
    parser.add_argument("--workers", type=int, default=4); args = parser.parse_args(argv)
    try:
        if not 1 <= args.workers <= 8 or not args.history.is_dir(): raise StudyError("workers or history directory differs")
        result = run(args.windows, args.population, args.ab9_summary, (args.history, args.recent_data), args.output_dir, args.report, args.workers)
    except (OSError, UnicodeError, ValueError, TypeError, StudyError, absorption.AbsorptionError, tape_pull.TapePullError) as error:
        print(f"unconditional exit study: {error}", file=sys.stderr); return 2
    print(f"unconditional exit | alarms={result['coverage']['rise_eligible_tape_ready_windows']} | output={args.output_dir}"); return 0

if __name__ == "__main__": raise SystemExit(main())
