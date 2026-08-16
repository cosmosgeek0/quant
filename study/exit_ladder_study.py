#!/usr/bin/env python3
"""Measure three fixed exit definitions on the six-month pump census."""

from __future__ import annotations

import argparse, bisect, json, os, sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

try:
    from study import absorption, tape_pull
except ModuleNotFoundError as error:
    if error.name != "study": raise
    import absorption, tape_pull  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CENSUS = ROOT / "lane-logs/AB13/pump-census-6mo.json"
DEFAULT_PROFILES = ROOT / "lane-logs/AB15/post-base-profiles.jsonl"
DEFAULT_ENTRY = ROOT / "lane-logs/AB17/entry-window-summary.json"
DEFAULT_HISTORY = ROOT / "study/data-1m-history"; DEFAULT_RECENT = ROOT / "study/data"
DEFAULT_OUTPUT = ROOT / "lane-logs/AB21"; DEFAULT_REPORT = ROOT / "lane-logs/AB21-report.txt"
D = Decimal; MINUTE_MS = 60_000; DAY_MS = 86_400_000
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc); CLASSES = ("HELD", "COLLAPSED")
ENTRY = ((D("0.40"), D("0")), (D("0.30"), D("2")), (D("0.30"), D("5")))
TAKER, SPREAD, SLIPPAGE = D("0.001"), D("0.002"), D("0.002")
RULES = ("R1", "R2", "R3")

class StudyError(Exception): pass
class EvidenceAbsent(StudyError): pass

def _decimal(raw: Any, label: str, *, positive: bool = False) -> Decimal:
    if raw is None or isinstance(raw, (bool, float)): raise StudyError(f"{label} is not Decimal text")
    try: value = D(str(raw).strip())
    except (InvalidOperation, ValueError): raise StudyError(f"{label} is not Decimal text") from None
    if not value.is_finite() or (positive and value <= 0): raise StudyError(f"{label} is outside its Decimal range")
    return value

def _utc_ms(raw: Any, label: str) -> int:
    if not isinstance(raw, str): raise StudyError(f"{label} is not canonical UTC text")
    try: moment = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError: raise StudyError(f"{label} is not canonical UTC text") from None
    delta = moment - EPOCH; value = (delta.days * 86_400 + delta.seconds) * 1_000 + delta.microseconds // 1_000
    if absorption.utc_text(value) != raw: raise StudyError(f"{label} is not canonical UTC text")
    return value

def _json(path: Path) -> Any:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error: raise StudyError(f"cannot read {path}: {error}") from None

def _inputs(census_path: Path, profiles_path: Path, entry_path: Path) -> tuple[list[dict[str, Any]], list[Decimal]]:
    payload = _json(census_path); header = payload.get("header") if isinstance(payload, Mapping) else None
    raw_rows = payload.get("episodes") if isinstance(payload, Mapping) else None; summary = payload.get("summary") if isinstance(payload, Mapping) else None
    if not isinstance(header, Mapping) or header.get("schema") != "pump-census-6mo-v1": raise StudyError("census schema differs")
    if not isinstance(raw_rows, list) or not isinstance(summary, Mapping) or summary.get("episodes") != len(raw_rows): raise StudyError("census population differs")
    rows: dict[str, dict[str, Any]] = {}
    for raw in raw_rows:
        if not isinstance(raw, Mapping): raise StudyError("census identity differs")
        identity, label = raw.get("episode_id"), raw.get("class")
        if not isinstance(identity, str) or identity in rows or label not in CLASSES: raise StudyError("census identity differs")
        try: symbol = tape_pull._validate_symbol(raw.get("symbol")); venue = tape_pull._validate_venue(raw.get("venue"))
        except tape_pull.TapePullError as error: raise StudyError(f"{identity} identity differs: {error}") from None
        base, peak = _utc_ms(raw.get("base_utc"), f"{identity} base"), _utc_ms(raw.get("peak_utc"), f"{identity} peak")
        if not base <= peak <= base + DAY_MS: raise StudyError(f"{identity} peak time differs")
        rows[identity] = {"episode_id": identity, "class": label, "symbol": symbol, "venue": venue, "base_ms": base, "peak_ms": peak,
                          "base_price": _decimal(raw.get("base_price"), f"{identity} base", positive=True),
                          "peak_price": _decimal(raw.get("peak_price"), f"{identity} peak", positive=True)}
    seen, escape = set(), []
    try:
        with open(profiles_path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip(): continue
                try: raw = json.loads(line)
                except json.JSONDecodeError: raise StudyError(f"profile line {number} is not JSON") from None
                identity = raw.get("episode_id") if isinstance(raw, Mapping) else None; source = rows.get(identity)
                if source is None or identity in seen or raw.get("evidence_status") != "COMPLETE": raise StudyError("profile coverage differs")
                for field in ("class", "symbol", "venue"):
                    if raw.get(field) != source[field]: raise StudyError(f"{identity} profile identity differs")
                if _decimal(raw.get("base_price"), f"{identity} profile base", positive=True) != source["base_price"]: raise StudyError(f"{identity} profile base differs")
                if raw.get("time_to_5pct_minutes") is None: raise StudyError(f"{identity} entry evidence is absent")
                if source["class"] == "COLLAPSED": escape.append(_decimal(raw.get("half_giveback_minutes"), f"{identity} half giveback"))
                seen.add(identity)
    except (OSError, UnicodeError) as error: raise StudyError(f"cannot read profiles: {error}") from None
    entry = _json(entry_path); population = entry.get("population") if isinstance(entry, Mapping) else None
    if seen != set(rows) or not escape or entry.get("schema") != "entry-window-study-v1" or not isinstance(population, Mapping) or population.get("episodes") != len(rows): raise StudyError("AB15 or AB17 population differs")
    return list(rows.values()), escape

def _merge(rows: Sequence[Mapping[str, Any]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted((row["base_ms"], row["base_ms"] + DAY_MS) for row in rows):
        if merged and start <= merged[-1][1] + MINUTE_MS: merged[-1] = merged[-1][0], max(end, merged[-1][1])
        else: merged.append((start, end))
    return merged

def _bars(roots: Sequence[Path], rows: Sequence[Mapping[str, Any]]) -> tuple[dict[int, absorption.KlineBar], int, int]:
    symbol, venue = rows[0]["symbol"], rows[0]["venue"]; intervals = _merge(rows); starts = [item[0] for item in intervals]
    months, days = set(), set()
    for start, end in intervals:
        cursor = start - start % DAY_MS
        while cursor <= end:
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
            parsed += 1; point = bisect.bisect_right(starts, bar.open_ms) - 1
            if point < 0 or bar.open_ms > intervals[point][1]: continue
            prior = indexed.get(bar.open_ms)
            if prior is not None and prior != bar: raise StudyError(f"{symbol} kline evidence conflicts")
            indexed[bar.open_ms] = bar
    return indexed, archives, parsed

def _simulate(source: Mapping[str, Any], bars: Mapping[int, absorption.KlineBar], rule: str) -> dict[str, Any]:
    base_ms, base = source["base_ms"], source["base_price"]
    window = [bars.get(base_ms + minute * MINUTE_MS) for minute in range(1_441)]
    if any(bar is None for bar in window): raise EvidenceAbsent(f"{source['episode_id']} 24h continuity is absent")
    if window[0].low != base or bars.get(source["peak_ms"]) is None or bars[source["peak_ms"]].high != source["peak_price"]: raise StudyError(f"{source['episode_id']} census prices differ")
    entries, exits = [], []
    with localcontext() as context:
        context.prec = 50
        def buy(weight: Decimal, gain: Decimal, minute: int) -> None:
            budget = weight; price = base * (D("1") + gain / D("100")); gross = budget / (D("1") + SPREAD + SLIPPAGE)
            entries.append({"quantity": gross / price, "gross": gross, "budget": budget, "fee": budget * TAKER, "minute": minute, "price": price})
        def sell(quantity: Decimal, price: Decimal, minute: int, reason: str) -> None:
            gross = quantity * price; executed = gross * (D("1") - SPREAD - SLIPPAGE)
            exits.append({"quantity": quantity, "gross": gross, "executed": executed, "fee": executed * TAKER, "minute": minute, "price": price, "reason": reason})
        buy(*ENTRY[0], 0); next_rung = 1; position = entries[0]["quantity"]; acquired = position
        running = base; trail_active = False; targets: set[int] = set(); terminal = ""
        for minute, bar in enumerate(window[1:-1], 1):
            basis = sum((item["gross"] for item in entries), D("0")); average = basis / acquired
            hard = average * D("0.98"); dynamic = None; dynamic_reason = ""
            if rule == "R2" and trail_active: dynamic, dynamic_reason = running * D("0.75"), "trailing_stop"
            elif rule == "R3" and next_rung == len(ENTRY) and running > average: dynamic, dynamic_reason = average + (running - average) / D("2"), "giveback_exit"
            trigger = hard if dynamic is None or hard >= dynamic else dynamic; reason = "hard_stop" if trigger == hard else dynamic_reason
            if bar.open <= trigger or bar.low <= trigger:
                sell(position, bar.open if bar.open <= trigger else trigger, minute, reason); position = D("0"); terminal = reason; break
            entered = False
            while next_rung < len(ENTRY) and bar.high >= base * (D("1") + ENTRY[next_rung][1] / D("100")):
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
        if position > 0:
            sell(position, window[-1].open, 1_440, "time_stop"); terminal = "time_stop"; position = D("0")
        entry_budget = sum((item["budget"] for item in entries), D("0")); gross_entry = sum((item["gross"] for item in entries), D("0"))
        gross_exit = sum((item["gross"] for item in exits), D("0")); entry_fee = sum((item["fee"] for item in entries), D("0"))
        exit_fee = sum((item["fee"] for item in exits), D("0")); exit_cash = sum((item["executed"] - item["fee"] for item in exits), D("0"))
        net_pnl = exit_cash - entry_budget - entry_fee; market_cost = entry_budget - gross_entry + sum((item["gross"] - item["executed"] for item in exits), D("0"))
        if sum((item["quantity"] for item in exits), D("0")) != acquired: raise StudyError(f"{source['episode_id']} quantity differs")
        if abs(net_pnl - (gross_exit - gross_entry - market_cost - entry_fee - exit_fee)) > D("1e-40"): raise StudyError(f"{source['episode_id']} cash flow differs")
        stopped = terminal in {"hard_stop", "trailing_stop", "giveback_exit"}; weighted = sum((item["quantity"] * D(item["minute"]) for item in exits), D("0")) / acquired
        return {"rule": rule, "net_return_pct": net_pnl / entry_budget * D("100"), "gross_return_pct": (gross_exit - gross_entry) / entry_budget * D("100"),
                "cost_pct": (market_cost + entry_fee + exit_fee) / entry_budget * D("100"), "average_entry": gross_entry / acquired,
                "entry_rungs_filled": len(entries), "entry_budget_fraction": entry_budget, "final_hold_minutes": max(item["minute"] for item in exits),
                "quantity_weighted_hold_minutes": weighted, "terminal_reason": terminal, "stopped_out": stopped,
                "exit_fills": [{"minute": item["minute"], "price": item["price"], "reason": item["reason"], "quantity_fraction": item["quantity"] / acquired} for item in exits]}

def _group(roots: Sequence[Path], rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    bars, archives, parsed = _bars(roots, rows); output = []
    for row in rows:
        base = {"episode_id": row["episode_id"], "symbol": row["symbol"], "venue": row["venue"], "class": row["class"]}
        try:
            rules = {rule: _simulate(row, bars, rule) for rule in RULES}
            if len({item["entry_rungs_filled"] for item in rules.values()}) != 1: raise StudyError(f"{row['episode_id']} fixed entry differs")
            output.append(base | {"evidence_status": "MEASURED", "absence_reason": None, "rules": rules})
        except EvidenceAbsent as error: output.append(base | {"evidence_status": "ABSENT", "absence_reason": str(error), "rules": {rule: None for rule in RULES}})
    return output, archives, parsed

def _quantile(values: Sequence[Decimal], fraction: Decimal) -> Decimal:
    ordered = sorted(values); position = D(len(ordered) - 1) * fraction; lower = int(position); upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

def _stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = [row["net_return_pct"] for row in rows]; holds = [D(row["final_hold_minutes"]) for row in rows]
    weighted = [row["quantity_weighted_hold_minutes"] for row in rows]; count = D(len(rows))
    return {"trades": len(rows), "median_net_return_pct": _quantile(values, D("0.5")), "mean_net_return_pct": sum(values, D("0")) / count,
            "win_rate": D(sum(value > 0 for value in values)) / count, "p5_net_return_pct": _quantile(values, D("0.05")),
            "stopped_out_fraction": D(sum(row["stopped_out"] for row in rows)) / count, "terminal_reasons": dict(Counter(row["terminal_reason"] for row in rows)),
            "final_hold_median_minutes": _quantile(holds, D("0.5")), "final_hold_mean_minutes": sum(holds, D("0")) / count,
            "weighted_hold_median_minutes": _quantile(weighted, D("0.5")), "weighted_hold_mean_minutes": sum(weighted, D("0")) / count}

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

def _short(value: Decimal) -> str: return format(value, ".8g")

def run(census: Path, profiles: Path, entry: Path, roots: Sequence[Path], output: Path, report: Path, workers: int) -> Mapping[str, Any]:
    if absorption.tape_pull.stream_verified_member is not tape_pull.stream_verified_member: raise StudyError("shared ZIP reader binding differs")
    rows, escape = _inputs(census, profiles, entry); grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows: grouped[(row["venue"], row["symbol"])].append(row)
    episode_rows, archives, parsed = [], 0, 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_group, roots, group) for group in grouped.values()]
        for ordinal, future in enumerate(as_completed(futures), 1):
            found, archive_count, parsed_count = future.result(); episode_rows.extend(found); archives += archive_count; parsed += parsed_count
            if ordinal % 50 == 0 or ordinal == len(futures): print(f"AB21 | symbols={ordinal}/{len(futures)} | episodes={len(episode_rows)}", flush=True)
    episode_rows.sort(key=lambda row: row["episode_id"]); measured_rows = [row for row in episode_rows if row["evidence_status"] == "MEASURED"]
    if not measured_rows: raise StudyError("no episode has complete local evidence")
    coverage = dict(Counter(row["evidence_status"] for row in episode_rows)); rule_summary = {}
    for rule in RULES:
        results = [row["rules"][rule] for row in measured_rows]; groups = {"ALL": results}
        groups.update({label: [row["rules"][rule] for row in measured_rows if row["class"] == label] for label in CLASSES})
        top_count = (len(results) + 19) // 20; top = sorted(results, key=lambda row: row["net_return_pct"], reverse=True)[:top_count]
        top_sum = sum((row["net_return_pct"] for row in top), D("0")); total_sum = sum((row["net_return_pct"] for row in results), D("0"))
        rule_summary[rule] = {label: _stats(group) for label, group in groups.items()}
        rule_summary[rule]["top_5pct_contribution"] = {"trades": top_count, "top_net_return_sum_pct_points": top_sum,
            "all_net_return_sum_pct_points": total_sum, "signed_share_pct": None if total_sum == 0 else top_sum / total_sum * D("100")}
    escape_stats = {"n": len(escape), "min_minutes": min(escape), "median_minutes": _quantile(escape, D("0.5")), "p90_minutes": _quantile(escape, D("0.9")), "max_minutes": max(escape)}
    result = {"schema": "exit-ladder-study-v1", "basis": "MEASURED", "question": "holding duration and net return of three fixed exit definitions on the six-month census",
              "population": {"episodes": len(episode_rows), "class_counts": dict(Counter(row["class"] for row in episode_rows)), "evidence_status": coverage,
                             "measured_class_counts": dict(Counter(row["class"] for row in measured_rows))},
              "source": {"census": str(census), "profiles": str(profiles), "entry_summary": str(entry), "roots_present": [str(root) for root in roots if root.is_dir()],
                         "reader": "study.tape_pull.stream_verified_member via study.absorption._kline_rows", "archives_read": archives, "bars_parsed": parsed},
              "definitions": {"entry": "quote-budget weights 40/30/30 percent at base/+2/+5 percent; first base rung creates the trade; later rungs fill only on a proved 1m high",
                "costs": "each side: 10 bps taker fee, 20 bps spread, and 20 bps slippage; entry budgets include spread and slippage; returns use filled entry budget",
                "bar_order": "protective thresholds proved before a 1m bar are checked first; a rung-fill bar has no favorable exit; current highs affect dynamic exits from the next bar; gaps through protective exits use the bar open",
                "time_stop": "remaining quantity exits at the open exactly 1440 minutes after the base", "R1": "50 percent of acquired quantity at average-entry +10 percent and 50 percent at +25 percent; hard stop -2 percent; 24h time stop",
                "R2": "all quantity trails 25 percent below the prior running high after +10 percent; hard stop -2 percent; 24h time stop",
                "R3": "after the final entry rung, R1 targets plus all remaining quantity exits at average entry plus half the prior running gain; hard stop -2 percent; 24h time stop",
                "top_5pct": "ceiling of five percent of equal-weighted trades ranked by net return; signed share is top sum divided by all-trade sum"},
              "collapsed_peak_to_half_giveback": escape_stats, "rules": rule_summary,
              "limits": "returns use only locally complete episodes; absent rows remain unknown; no within-bar path, queue, liquidity, confirmation timing, funding, or executable fill is claimed"}
    def split(rule: str, label: str) -> str:
        item = rule_summary[rule][label]; return f"{label} med/mean/win={_short(item['median_net_return_pct'])}/{_short(item['mean_net_return_pct'])}/{_short(item['win_rate'] * D('100'))}%"
    lines = [f"scope | census={len(episode_rows)} HELD={result['population']['class_counts']['HELD']} COLLAPSED={result['population']['class_counts']['COLLAPSED']}; rule trades={len(measured_rows)} HELD={result['population']['measured_class_counts']['HELD']} COLLAPSED={result['population']['measured_class_counts']['COLLAPSED']}; absent={coverage.get('ABSENT', 0)}; archives={archives}; bars parsed={parsed}; shared verified ZIP reader | MEASURED",
             f"COLLAPSED exit window | peak to half-giveback n={len(escape)} min/median/p90/max={_short(escape_stats['min_minutes'])}/{_short(escape_stats['median_minutes'])}/{_short(escape_stats['p90_minutes'])}/{_short(escape_stats['max_minutes'])} minutes | MEASURED",
             "definitions | entry=40/30/30 quote-budget percent at base/+2/+5; R1=50 percent at +10/+25; R2=25 percent trail after +10; R3=R1 plus half-running-gain giveback after final entry rung; stopped=hard/trailing/giveback terminal; per side costs=10bps taker+20bps spread+20bps slippage; risk-first prior-threshold 1m ordering; 24h horizon-open exit | MEASURED"]
    for rule in RULES:
        item = rule_summary[rule]["ALL"]; top = rule_summary[rule]["top_5pct_contribution"]
        lines.append(f"{rule} | trades={item['trades']}; final hold median/mean={_short(item['final_hold_median_minutes'] / D('60'))}/{_short(item['final_hold_mean_minutes'] / D('60'))}h; weighted hold median/mean={_short(item['weighted_hold_median_minutes'] / D('60'))}/{_short(item['weighted_hold_mean_minutes'] / D('60'))}h; net median/mean={_short(item['median_net_return_pct'])}/{_short(item['mean_net_return_pct'])}%; win={_short(item['win_rate'] * D('100'))}%; p5={_short(item['p5_net_return_pct'])}%; stopped={_short(item['stopped_out_fraction'] * D('100'))}%; {split(rule, 'HELD')}; {split(rule, 'COLLAPSED')} | MEASURED")
        share = "NA" if top["signed_share_pct"] is None else _short(top["signed_share_pct"]) + "%"
        lines.append(f"{rule} top 5% return contribution | trades={top['trades']}/{len(measured_rows)}; top/all equal-trade net sum={_short(top['top_net_return_sum_pct_points'])}/{_short(top['all_net_return_sum_pct_points'])} percentage-points; signed share={share} | MEASURED")
    lines.append(f"limits | {coverage.get('ABSENT', 0)} census rows lack a complete local 24h window and remain unknown; 1m OHLC cannot prove within-bar path, liquidity, queue, 20-second confirmation timing, funding, or executable fills | MEASURED")
    _write(output / "exit-ladder-trades.jsonl", episode_rows, lines=True); _write(output / "exit-ladder-summary.json", result)
    _write(output / "exit-ladder-report.txt", "\n".join(lines) + "\n", plain=True); _write(report, "\n".join(lines) + "\n", plain=True)
    return result

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--census", type=Path, default=DEFAULT_CENSUS)
    parser.add_argument("--profiles", type=Path, default=DEFAULT_PROFILES); parser.add_argument("--entry-summary", type=Path, default=DEFAULT_ENTRY)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY); parser.add_argument("--recent-data", type=Path, default=DEFAULT_RECENT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT); parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--workers", type=int, default=4); args = parser.parse_args(argv)
    try:
        if not 1 <= args.workers <= 8 or not args.history.is_dir(): raise StudyError("workers or history directory differs")
        result = run(args.census, args.profiles, args.entry_summary, (args.history, args.recent_data), args.output_dir, args.report, args.workers)
    except (OSError, UnicodeError, ValueError, TypeError, StudyError, absorption.AbsorptionError, tape_pull.TapePullError) as error:
        print(f"exit ladder study: {error}", file=sys.stderr); return 2
    print(f"exit ladder | episodes={result['population']['episodes']} | output={args.output_dir}"); return 0

if __name__ == "__main__": raise SystemExit(main())
