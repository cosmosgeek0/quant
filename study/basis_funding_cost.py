#!/usr/bin/env python3
"""Measure funding and fee drag on AB18/AB19 basis-gap episodes."""
from __future__ import annotations

import argparse, bisect, csv, json, zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

try:
    from study import absorption, basis_during_pumps as basis, pull_funding_history as funding
except ImportError:
    import absorption, basis_during_pumps as basis, pull_funding_history as funding  # type: ignore

ROOT = Path(__file__).resolve().parents[1]; OUTPUT = ROOT / "lane-logs/AB23"
SNAPSHOT = OUTPUT / "basis-profile-source.jsonl"; PROFILES = SNAPSHOT if SNAPSHOT.is_file() else ROOT / "lane-logs/AB18/basis-during-pumps.jsonl"
HISTORY = ROOT / "study/data-1m-history"; FUNDING = ROOT / "study/data-funding"
MINUTE_MS, DAY_MS = 60_000, 86_400_000; ENTRY, EXIT, FEE_PCT = Decimal("2"), Decimal("1"), Decimal("0.30")

class CostStudyError(Exception): pass

def _decimal(raw: Any, source: str) -> Decimal:
    try: value = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError): raise CostStudyError(f"{source} is not decimal text") from None
    if not value.is_finite(): raise CostStudyError(f"{source} is not finite")
    return value

def _profiles(path: Path) -> list[dict[str, Any]]:
    try: lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error: raise CostStudyError("basis profiles are unreadable") from error
    selected, identifiers = [], set()
    for ordinal, line in enumerate(lines, 1):
        try: raw = json.loads(line)
        except json.JSONDecodeError as error: raise CostStudyError(f"basis profile row {ordinal} is invalid") from error
        if not isinstance(raw, Mapping): raise CostStudyError(f"basis profile row {ordinal} is not an object")
        row, identifier = dict(raw), raw.get("episode_id")
        if not isinstance(identifier, str) or identifier in identifiers: raise CostStudyError("basis profile identifier differs")
        identifiers.add(identifier); maximum = raw.get("max_abs_gap_during_rise_pct")
        if maximum is None or _decimal(maximum, f"{identifier} maximum gap") <= ENTRY: continue
        if raw.get("class") not in {"HELD", "COLLAPSED"} or not raw.get("evidence", {}).get("rise_complete"):
            raise CostStudyError(f"{identifier} selected evidence differs")
        row["_base_ms"] = basis._utc_ms(raw.get("base_utc"), f"{identifier} base")
        row["_peak_ms"] = basis._utc_ms(raw.get("peak_utc"), f"{identifier} peak")
        selected.append(row)
    if not selected: raise CostStudyError("no basis profile exceeded 2 percent")
    return selected

def _months(start: int, end: int) -> set[str]:
    found, current = set(), start - start % DAY_MS
    while current <= end: found.add(absorption.utc_text(current)[:7]); current += DAY_MS
    return found

def _merge(rows: Sequence[Mapping[str, Any]]) -> list[tuple[int, int]]:
    answer = []
    for start, end in sorted((row["_base_ms"], row["_peak_ms"] + DAY_MS) for row in rows):
        if answer and start <= answer[-1][1] + MINUTE_MS: answer[-1] = (answer[-1][0], max(answer[-1][1], end))
        else: answer.append((start, end))
    return answer

def _bars(history: Path, symbol: str, venue: str, intervals: Sequence[tuple[int, int]]) -> tuple[dict[int, absorption.KlineBar], int, int]:
    starts = [row[0] for row in intervals]; indexed = {}; archives = parsed = 0
    prefix = Path("spot") if venue == "spot" else Path("futures/um")
    months = set().union(*(_months(start, end) for start, end in intervals))
    for month in sorted(months):
        path = history / prefix / "monthly/klines" / symbol / "1m" / f"{symbol}-1m-{month}.zip"
        if not path.is_file(): continue
        archives += 1
        for bar in absorption._kline_rows(path, symbol):
            parsed += 1; point = bisect.bisect_right(starts, bar.open_ms) - 1
            if point < 0 or bar.open_ms > intervals[point][1]: continue
            prior = indexed.get(bar.open_ms)
            if prior is not None and prior != bar: raise CostStudyError(f"{symbol} has conflicting {venue} bars")
            indexed[bar.open_ms] = bar
    return indexed, archives, parsed

def _funding_rows(root: Path, symbol: str, months: set[str]) -> tuple[list[tuple[int, int, Decimal]], set[str], int, int]:
    rows, available, archives = [], set(), 0
    for month in sorted(months):
        job = funding.Job(symbol, month); path = job.path(root)
        if not path.is_file(): continue
        funding._verify(job, path); archives += 1; available.add(month)
        with zipfile.ZipFile(path, "r") as archive, archive.open(job.member(), "r") as handle:
            reader = csv.reader(line.decode("utf-8") for line in handle)
            if next(reader, None) != funding.HEADER: raise CostStudyError(f"{symbol} funding header differs")
            for row in reader:
                stamp = int(row[0]); interval = int(row[1]); rate = _decimal(row[2], f"{symbol} funding rate")
                while stamp > 10_000_000_000_000: stamp //= 1_000
                rows.append((stamp, interval, rate))
    rows.sort(); return rows, available, archives, len(rows)

def _empty(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"episode_id": row["episode_id"], "symbol": row["symbol"], "class": row["class"],
            "base_utc": row["base_utc"], "peak_utc": row["peak_utc"], "profile_max_abs_gap_pct": row["max_abs_gap_during_rise_pct"],
            "status": None, "entry_utc": None, "entry_gap_pct": None, "hedge": None, "exit_utc": None,
            "exit_gap_pct": None, "close_reason": None, "hold_minutes": None, "gap_captured_pct": None,
            "funding_cost_pct": None, "funding_settlements": None, "funding_interval_hours": [], "fee_pct": None,
            "net_pct": None, "missing_funding_months": []}

def _measure(row: Mapping[str, Any], spot: Mapping[int, absorption.KlineBar], perp: Mapping[int, absorption.KlineBar],
             rates: Sequence[tuple[int, int, Decimal]], available: set[str]) -> dict[str, Any]:
    result = _empty(row); base, peak = row["_base_ms"], row["_peak_ms"]; entry = entry_gap = None
    def price_incomplete(start: int, end: int) -> dict[str, Any]:
        missing = sorted(_months(start, end) - available); result["missing_funding_months"] = missing
        result["status"] = "PRICE_AND_FUNDING_INCOMPLETE" if missing else "PRICE_INCOMPLETE"; return result
    for stamp in range(base, peak + MINUTE_MS, MINUTE_MS):
        if stamp not in spot or stamp not in perp:
            return price_incomplete(base, peak)
        gap = basis._gap(spot[stamp].close, perp[stamp].close)
        if abs(gap) > ENTRY: entry, entry_gap = stamp, gap; break
    if entry is None or entry_gap is None: result["status"] = "ENTRY_NOT_REPRODUCED"; return result
    exit_stamp, exit_gap, reason = entry + DAY_MS, None, "CAPPED_24H"
    for stamp in range(entry + MINUTE_MS, exit_stamp + MINUTE_MS, MINUTE_MS):
        if stamp not in spot or stamp not in perp:
            return price_incomplete(entry, stamp)
        gap = basis._gap(spot[stamp].close, perp[stamp].close)
        if abs(gap) < EXIT: exit_stamp, exit_gap, reason = stamp, gap, "BELOW_1PCT"; break
        if stamp == exit_stamp: exit_gap = gap
    if exit_gap is None: raise CostStudyError(f"{row['episode_id']} exit evidence differs")
    direction = Decimal("1") if entry_gap > 0 else Decimal("-1")
    result.update({"entry_utc": absorption.utc_text(entry), "entry_gap_pct": entry_gap,
                   "hedge": "LONG_SPOT_SHORT_PERP" if direction > 0 else "SHORT_SPOT_LONG_PERP",
                   "exit_utc": absorption.utc_text(exit_stamp), "exit_gap_pct": exit_gap, "close_reason": reason,
                   "hold_minutes": (exit_stamp - entry) // MINUTE_MS,
                   "gap_captured_pct": direction * (entry_gap - exit_gap), "fee_pct": FEE_PCT})
    required = _months(entry, exit_stamp); missing = sorted(required - available); result["missing_funding_months"] = missing
    if missing: result["status"] = "FUNDING_INCOMPLETE"; return result
    entry_close, exit_close = entry + MINUTE_MS - 1, exit_stamp + MINUTE_MS - 1
    settlements = [(stamp, interval, rate) for stamp, interval, rate in rates if entry_close < stamp <= exit_close]
    funding_cost = -direction * sum((rate for _, _, rate in settlements), Decimal("0")) * Decimal("100")
    result.update({"status": "MEASURED", "funding_cost_pct": funding_cost, "funding_settlements": len(settlements),
                   "funding_interval_hours": sorted({interval for _, interval, _ in settlements}),
                   "net_pct": result["gap_captured_pct"] - funding_cost - FEE_PCT})
    return result

def _group(history: Path, funding_root: Path, rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int, int, int, int]:
    symbol, intervals = rows[0]["symbol"], _merge(rows)
    spot, spot_archives, spot_rows = _bars(history, symbol, "spot", intervals)
    perp, perp_archives, perp_rows = _bars(history, symbol, "futures", intervals)
    months = set().union(*(_months(start, end) for start, end in intervals))
    rates, available, funding_archives, funding_rows = _funding_rows(funding_root, symbol, months)
    return ([_measure(row, spot, perp, rates, available) for row in rows], spot_archives + perp_archives,
            spot_rows + perp_rows, funding_archives, funding_rows)

def _stats(values: Sequence[Decimal]) -> dict[str, Any]:
    return {"n": len(values), "min": min(values) if values else None, "median": absorption.quantile(values, 1, 2),
            "p90": absorption.quantile(values, 9, 10), "max": max(values) if values else None}

def _summary(rows: Sequence[Mapping[str, Any]], source_count: int, evidence: Mapping[str, int]) -> tuple[dict[str, Any], list[str]]:
    statuses = Counter(row["status"] for row in rows); groups = {}
    for label in ("ALL", "HELD", "COLLAPSED"):
        chosen = [row for row in rows if row["status"] == "MEASURED" and (label == "ALL" or row["class"] == label)]
        positives = sum(row["net_pct"] > 0 for row in chosen)
        groups[label] = {"n": len(chosen), "gap_captured_pct": _stats([row["gap_captured_pct"] for row in chosen]),
                         "funding_cost_pct": _stats([row["funding_cost_pct"] for row in chosen]),
                         "net_pct": _stats([row["net_pct"] for row in chosen]),
                         "hold_minutes": _stats([Decimal(row["hold_minutes"]) for row in chosen]),
                         "net_positive": {"count": positives, "fraction": None if not chosen else Decimal(positives) / Decimal(len(chosen))}}
    measured = [row for row in rows if row["status"] == "MEASURED"]; bearing = [row for row in measured if row["funding_settlements"]]
    outcomes = {"no_settlement": len(measured) - len(bearing), "paid": sum(row["funding_cost_pct"] > 0 for row in bearing),
                "received": sum(row["funding_cost_pct"] < 0 for row in bearing), "settlement_episode_cost_pct": _stats([row["funding_cost_pct"] for row in bearing])}
    def short(value: Any) -> str: return "NA" if value is None else format(value, ".8g")
    def plain(item: Mapping[str, Any]) -> str: return "/".join(short(item[key]) for key in ("min", "median", "p90", "max"))
    lines = [f"GAP_CAPTURED_PCT | n={groups['ALL']['n']} min/median/p90/max={plain(groups['ALL']['gap_captured_pct'])} | MEASURED",
             f"FUNDING_COST_PCT | n={groups['ALL']['n']} min/median/p90/max={plain(groups['ALL']['funding_cost_pct'])} | negative=received | MEASURED",
             f"NET_PCT | n={groups['ALL']['n']} min/median/p90/max={plain(groups['ALL']['net_pct'])} | MEASURED",
             f"NET_POSITIVE | {groups['ALL']['net_positive']['count']}/{groups['ALL']['n']}({short(None if not groups['ALL']['n'] else groups['ALL']['net_positive']['fraction'] * Decimal('100'))}%) | MEASURED"]
    lines.insert(2, f"FUNDING_SETTLEMENT_EPISODES | n={len(bearing)} cost_min/median/p90/max={plain(outcomes['settlement_episode_cost_pct'])} | paid={outcomes['paid']} received={outcomes['received']} no_settlement={outcomes['no_settlement']} | MEASURED")
    for label in ("HELD", "COLLAPSED"):
        item = groups[label]; lines.append(f"{label} | n={item['n']} captured={plain(item['gap_captured_pct'])} funding={plain(item['funding_cost_pct'])} net={plain(item['net_pct'])} hold_minutes={plain(item['hold_minutes'])} net_positive={item['net_positive']['count']}/{item['n']} | MEASURED")
    hold = groups["ALL"]["hold_minutes"]; lines.append(f"HOLD_MINUTES | n={hold['n']} min/median/p90/max={plain(hold)} | below_1pct={sum(r['close_reason'] == 'BELOW_1PCT' for r in rows if r['status'] == 'MEASURED')} capped_24h={sum(r['close_reason'] == 'CAPPED_24H' for r in rows if r['status'] == 'MEASURED')} | MEASURED")
    lines.append(f"COVERAGE | source_profiles={source_count} selected_over_2pct={len(rows)} measured={statuses['MEASURED']} price_incomplete={statuses['PRICE_INCOMPLETE']} funding_incomplete={statuses['FUNDING_INCOMPLETE']} price_and_funding_incomplete={statuses['PRICE_AND_FUNDING_INCOMPLETE']} entry_not_reproduced={statuses['ENTRY_NOT_REPRODUCED']} | MEASURED")
    payload = {"schema": "basis-funding-cost-summary-v1", "source_profiles": source_count, "selected_episodes": len(rows),
               "coverage": dict(statuses), "groups": groups, "funding_outcomes": outcomes, "evidence": dict(evidence),
               "definitions": {"selection": "AB18/AB19 max absolute rise gap strictly above 2 percent",
                               "entry": "first base-to-peak minute close with absolute gap strictly above 2 percent",
                               "exit": "first later minute close with absolute gap below 1 percent, capped at 24 hours",
                               "gap_pct": "(perpetual close - spot close) / spot close * 100",
                               "funding_cost": "perpetual side signed sum of published settlements; positive is paid and negative is received",
                               "settlement_inclusion": "entry minute close strictly before settlement timestamp through exit minute close inclusive",
                               "fees": "0.30 percent total: spot 0.10 percent each side and perpetual 0.05 percent each side",
                               "net": "captured gap percent minus funding cost percent minus 0.30 percent fees"}}
    return payload, lines

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--profiles", type=Path, default=PROFILES)
    parser.add_argument("--history-dir", type=Path, default=HISTORY); parser.add_argument("--funding-dir", type=Path, default=FUNDING)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT); parser.add_argument("--workers", type=int, default=4); args = parser.parse_args(argv)
    try:
        selected = _profiles(args.profiles); source_count = len(args.profiles.read_text(encoding="utf-8").splitlines())
        if not 1 <= args.workers <= 8: raise CostStudyError("workers must be from one through eight")
        grouped = defaultdict(list)
        for row in selected: grouped[row["symbol"]].append(row)
        results = []; evidence = Counter()
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(_group, args.history_dir, args.funding_dir, rows) for rows in grouped.values()]
            for ordinal, future in enumerate(as_completed(futures), 1):
                rows, kline_archives, kline_rows, funding_archives, funding_rows = future.result(); results.extend(rows)
                evidence.update(kline_archives=kline_archives, kline_rows=kline_rows, funding_archives=funding_archives, funding_rows=funding_rows)
                if ordinal % 25 == 0 or ordinal == len(futures): print(f"AB23 | symbols={ordinal}/{len(futures)} | episodes={len(results)}", flush=True)
        results.sort(key=lambda row: row["episode_id"])
        evidence["settlements_used"] = sum(row["funding_settlements"] or 0 for row in results)
        for row in results:
            for interval in row["funding_interval_hours"]: evidence[f"settlement_interval_{interval}h_episodes"] += 1
        summary, lines = _summary(results, source_count, evidence)
        source_lines = args.profiles.read_text(encoding="utf-8").splitlines()
        basis._write(args.output_dir / "basis-profile-source.jsonl", source_lines, lines=True)
        basis._write(args.output_dir / "basis-funding-cost.jsonl", results, lines=True)
        basis._write(args.output_dir / "basis-funding-summary.json", summary)
        basis._write(args.output_dir / "basis-funding-comparison.txt", lines, lines=True)
        print(f"DONE | selected={len(results)} | measured={summary['coverage'].get('MEASURED', 0)} | exit=0", flush=True); return 0
    except (OSError, UnicodeError, ValueError, CostStudyError, basis.BasisStudyError, absorption.AbsorptionError, funding.PullError) as error:
        print(f"basis funding cost: {error}", file=sys.stderr); return 1

if __name__ == "__main__": raise SystemExit(main())
