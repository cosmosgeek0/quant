#!/usr/bin/env python3
"""Measure spot versus perpetual basis during AB13 pump episodes."""

from __future__ import annotations

import argparse, bisect, json, os, sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

try:
    from study import absorption, stage_a
except ModuleNotFoundError as error:
    if error.name != "study": raise
    import absorption, stage_a  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CENSUS = ROOT / "lane-logs/AB13/pump-census-6mo.json"
DEFAULT_HISTORY = ROOT / "study/data-1m-history"
DEFAULT_RECENT = Path("/Users/cosmosgeek/Desktop/quant/study/data")
DEFAULT_OUTPUT = ROOT / "lane-logs/AB18"
DEFAULT_REPORT = ROOT / "lane-logs/AB18-report.txt"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MINUTE_MS, HOUR_MS, DAY_MS = 60_000, 3_600_000, 86_400_000
THRESHOLDS = (Decimal("2"), Decimal("5"), Decimal("10"))

class BasisStudyError(Exception): pass

def _utc_ms(raw: Any, source: str) -> int:
    if not isinstance(raw, str): raise BasisStudyError(f"{source} is not canonical UTC text")
    try: value = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError: raise BasisStudyError(f"{source} is not canonical UTC text") from None
    delta = value - EPOCH
    if delta.microseconds % 1_000: raise BasisStudyError(f"{source} is not millisecond-aligned")
    result = (delta.days * 86_400 + delta.seconds) * 1_000 + delta.microseconds // 1_000
    if result % MINUTE_MS or absorption.utc_text(result) != raw:
        raise BasisStudyError(f"{source} is not a canonical minute")
    return result

def _load_census(path: Path) -> list[dict[str, Any]]:
    try: payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BasisStudyError(f"cannot read census: {error}") from None
    header = payload.get("header") if isinstance(payload, Mapping) else None
    raw_rows = payload.get("episodes") if isinstance(payload, Mapping) else None
    if not isinstance(header, Mapping) or header.get("schema") != "pump-census-6mo-v1":
        raise BasisStudyError("census schema differs")
    if not isinstance(raw_rows, list) or not raw_rows: raise BasisStudyError("census has no episodes")
    rows, identifiers = [], set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping): raise BasisStudyError("census episode is not an object")
        row, identifier = dict(raw), raw.get("episode_id")
        if not isinstance(identifier, str) or identifier in identifiers:
            raise BasisStudyError("census episode identifier differs")
        symbol, venue, label = raw.get("symbol"), raw.get("venue"), raw.get("class")
        try: canonical = absorption.validate_symbol(symbol, f"{identifier} symbol")
        except absorption._SymbolValidationError as error: raise BasisStudyError(str(error)) from None
        if canonical != symbol or venue not in {"spot", "futures"} or label not in {"HELD", "COLLAPSED"}:
            raise BasisStudyError(f"{identifier} identity differs")
        base, peak = _utc_ms(raw.get("base_utc"), f"{identifier} base"), _utc_ms(raw.get("peak_utc"), f"{identifier} peak")
        if not base <= peak <= base + DAY_MS: raise BasisStudyError(f"{identifier} peak time differs")
        row["_base_ms"], row["_peak_ms"] = base, peak
        identifiers.add(identifier); rows.append(row)
    return rows

def _both_symbols(recent: Path) -> set[str]:
    return {row.symbol for row in stage_a.discover_market_tapes(recent) if row.venue_class == "both"}

def _merge_intervals(rows: Sequence[Mapping[str, Any]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted((row["_base_ms"] - 2 * HOUR_MS, row["_peak_ms"] + 2 * HOUR_MS) for row in rows):
        if merged and start <= merged[-1][1] + MINUTE_MS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else: merged.append((start, end))
    return merged

def _days(intervals: Sequence[tuple[int, int]]) -> list[int]:
    found = set()
    for start, end in intervals:
        current = start - start % DAY_MS
        while current <= end: found.add(current); current += DAY_MS
    return sorted(found)

def _archive(history: Path, recent: Path, venue: str, symbol: str, day_ms: int) -> Optional[Path]:
    prefix = Path("spot") if venue == "spot" else Path("futures/um")
    day, month = absorption.utc_text(day_ms)[:10], absorption.utc_text(day_ms)[:7]
    daily = f"{symbol}-1m-{day}.zip"; monthly = f"{symbol}-1m-{month}.zip"
    candidates = (
        recent / prefix / "daily/klines" / symbol / "1m" / daily,
        history / prefix / "daily/klines" / symbol / "1m" / daily,
        history / prefix / "monthly/klines" / symbol / "1m" / monthly,
        recent / prefix / "monthly/klines" / symbol / "1m" / monthly,
    )
    return next((path for path in candidates if path.is_file()), None)

def _load_side(paths: set[Path], symbol: str, intervals: Sequence[tuple[int, int]]) -> tuple[dict[int, absorption.KlineBar], int]:
    indexed: dict[int, absorption.KlineBar] = {}; starts = [item[0] for item in intervals]; parsed = 0
    for path in sorted(paths):
        for bar in absorption._kline_rows(path, symbol):
            parsed += 1; point = bisect.bisect_right(starts, bar.open_ms) - 1
            if point < 0 or bar.open_ms > intervals[point][1]: continue
            prior = indexed.get(bar.open_ms)
            if prior is not None and prior != bar: raise BasisStudyError(f"{symbol} has conflicting {path.name} evidence")
            indexed[bar.open_ms] = bar
    return indexed, parsed

def _load_pair(history: Path, recent: Path, rows: Sequence[Mapping[str, Any]]) -> tuple[dict[int, absorption.KlineBar], dict[int, absorption.KlineBar], int, int]:
    symbol, intervals = rows[0]["symbol"], _merge_intervals(rows)
    paths = {"spot": set(), "futures": set()}
    for day in _days(intervals):
        spot = _archive(history, recent, "spot", symbol, day)
        futures = _archive(history, recent, "futures", symbol, day)
        if spot is not None and futures is not None:
            paths["spot"].add(spot); paths["futures"].add(futures)
    spot, spot_parsed = _load_side(paths["spot"], symbol, intervals)
    futures, futures_parsed = _load_side(paths["futures"], symbol, intervals)
    return spot, futures, len(paths["spot"]) + len(paths["futures"]), spot_parsed + futures_parsed

def _gap(spot: Decimal, futures: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 50; return (futures - spot) / spot * Decimal("100")

def _profile(row: Mapping[str, Any], spot: Mapping[int, absorption.KlineBar], futures: Mapping[int, absorption.KlineBar]) -> dict[str, Any]:
    base, peak = row["_base_ms"], row["_peak_ms"]
    start, end = base - 2 * HOUR_MS, peak + 2 * HOUR_MS
    window = range(start, end + MINUTE_MS, MINUTE_MS)
    pre = range(start, base, MINUTE_MS); rise = range(base, peak + MINUTE_MS, MINUTE_MS)
    paired = {stamp: _gap(spot[stamp].close, futures[stamp].close) for stamp in window if stamp in spot and stamp in futures}
    pre_complete, rise_complete = all(stamp in paired for stamp in pre), all(stamp in paired for stamp in rise)
    result = {key: row[key] for key in ("episode_id", "symbol", "venue", "class", "base_utc", "peak_utc")}
    result.update({"pre_base_median_gap_pct": None, "max_abs_gap_during_rise_pct": None,
                   "gap_at_max_abs_during_rise_pct": None, "minutes_base_to_max_abs_gap": None,
                   "gap_at_peak_plus_1h_pct": None, "minutes_abs_gap_above_2pct_during_rise": None})
    if pre_complete:
        result["pre_base_median_gap_pct"] = absorption.quantile([paired[stamp] for stamp in pre], 1, 2)
    if rise_complete:
        rise_values = [(stamp, paired[stamp]) for stamp in rise]
        max_stamp, max_gap = max(rise_values, key=lambda item: abs(item[1]))
        result["max_abs_gap_during_rise_pct"] = abs(max_gap)
        result["gap_at_max_abs_during_rise_pct"] = max_gap
        result["minutes_base_to_max_abs_gap"] = (max_stamp - base) // MINUTE_MS
        result["minutes_abs_gap_above_2pct_during_rise"] = sum(abs(value) > THRESHOLDS[0] for _, value in rise_values)
    hour = peak + HOUR_MS
    if hour in paired: result["gap_at_peak_plus_1h_pct"] = paired[hour]
    expected = len(window); pair_count = len(paired)
    result["evidence"] = {"status": "COMPLETE" if pair_count == expected else "PARTIAL" if pair_count else "ABSENT",
                          "expected_minutes": expected, "paired_minutes": pair_count,
                          "spot_missing_minutes": sum(stamp not in spot for stamp in window),
                          "futures_missing_minutes": sum(stamp not in futures for stamp in window),
                          "pre_base_complete": pre_complete, "rise_complete": rise_complete,
                          "peak_plus_1h_present": hour in paired}
    return result

def _group(history: Path, recent: Path, rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    spot, futures, archives, parsed = _load_pair(history, recent, rows)
    return [_profile(row, spot, futures) for row in rows], archives, parsed

def _stats(values: Sequence[Decimal], total: int) -> dict[str, Any]:
    return {"n": len(values), "absent": total - len(values), "min": min(values) if values else None,
            "median": absorption.quantile(values, 1, 2), "p90": absorption.quantile(values, 9, 10),
            "max": max(values) if values else None}

def _fraction(rows: Sequence[Mapping[str, Any]], threshold: Decimal) -> dict[str, Any]:
    values = [row["max_abs_gap_during_rise_pct"] for row in rows if row["max_abs_gap_during_rise_pct"] is not None]
    exceeded = sum(value > threshold for value in values)
    with localcontext() as context:
        context.prec = 50
        fraction = None if not values else Decimal(exceeded) / Decimal(len(values))
    return {"exceeded": exceeded, "measured": len(values), "fraction": fraction}

def _short(value: Any) -> str: return "NA" if value is None else format(value, ".8g")

def _plain_stats(stats: Mapping[str, Any]) -> str:
    values = "/".join(_short(stats[key]) for key in ("min", "median", "p90", "max"))
    return f"n={stats['n']} absent={stats['absent']} min/median/p90/max={values}"

def _summarize(rows: Sequence[Mapping[str, Any]], both_count: int, census_count: int) -> tuple[dict[str, Any], list[str]]:
    totals = Counter(row["class"] for row in rows); statuses = Counter(row["evidence"]["status"] for row in rows)
    distribution = {}; durations = {}; fractions = {}
    for label in ("HELD", "COLLAPSED"):
        selected = [row for row in rows if row["class"] == label]
        distribution[label] = _stats([row["max_abs_gap_during_rise_pct"] for row in selected if row["max_abs_gap_during_rise_pct"] is not None], totals[label])
        positive = [Decimal(row["minutes_abs_gap_above_2pct_during_rise"]) for row in selected if row["minutes_abs_gap_above_2pct_during_rise"] is not None and row["minutes_abs_gap_above_2pct_during_rise"] > 0]
        durations[label] = _stats(positive, len(positive))
    positive_all = [Decimal(row["minutes_abs_gap_above_2pct_during_rise"]) for row in rows if row["minutes_abs_gap_above_2pct_during_rise"] is not None and row["minutes_abs_gap_above_2pct_during_rise"] > 0]
    durations["ALL"] = _stats(positive_all, len(positive_all))
    for threshold in THRESHOLDS:
        key = absorption.decimal_text(threshold); fractions[key] = {"ALL": _fraction(rows, threshold)}
        for label in ("HELD", "COLLAPSED"): fractions[key][label] = _fraction([row for row in rows if row["class"] == label], threshold)
    lines = [f"MAX_ABS_GAP_DURING_RISE_PCT | HELD {_plain_stats(distribution['HELD'])} | COLLAPSED {_plain_stats(distribution['COLLAPSED'])} | MEASURED"]
    for threshold in THRESHOLDS:
        key = absorption.decimal_text(threshold); values = fractions[key]
        cells = [f"{name}={item['exceeded']}/{item['measured']}({_short(None if item['fraction'] is None else item['fraction'] * Decimal('100'))}%)" for name, item in values.items()]
        lines.append(f"MAX_ABS_GAP_EXCEEDED_{key}PCT | {' | '.join(cells)} | MEASURED")
    lines.append(f"MINUTES_ABS_GAP_ABOVE_2PCT_WHEN_EXCEEDED | ALL {_plain_stats(durations['ALL'])} | HELD {_plain_stats(durations['HELD'])} | COLLAPSED {_plain_stats(durations['COLLAPSED'])} | MEASURED")
    complete_rise = sum(row["evidence"]["rise_complete"] for row in rows)
    lines.append(f"COVERAGE | census={census_count} | both_symbols={both_count} | eligible={len(rows)} | complete_window={statuses['COMPLETE']} | partial={statuses['PARTIAL']} | absent={statuses['ABSENT']} | complete_rise={complete_rise} | MEASURED")
    summary = {"schema": "basis-during-pumps-summary-v1", "census_episodes": census_count,
               "both_venue_symbols": both_count, "eligible_episodes": len(rows), "class_counts": dict(totals),
               "evidence_status": dict(statuses), "complete_rise": complete_rise,
               "max_abs_gap_during_rise_pct": distribution, "threshold_fractions": fractions,
               "minutes_abs_gap_above_2pct_during_rise_when_exceeded": durations,
               "definitions": {"gap_pct": "(perpetual close - spot close) / spot close * 100",
                               "pre_base": "120 exact paired minutes before base; signed median",
                               "rise": "base through peak inclusive; every paired minute required",
                               "duration": "per-episode count of rise minutes strictly above 2 percent; summary conditions on a positive count",
                               "window_coverage": "both closes present each minute from base minus 2h through peak plus 2h"}}
    return summary, lines

def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal): return absorption.decimal_text(value)
    if isinstance(value, Mapping): return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)): return [_jsonable(item) for item in value]
    return value

def _write(path: Path, payload: Any, *, lines: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        if lines:
            for row in payload:
                text = row if isinstance(row, str) else json.dumps(_jsonable(row), sort_keys=True, separators=(",", ":"))
                handle.write(text + "\n")
        else: json.dump(_jsonable(payload), handle, sort_keys=True, separators=(",", ":")); handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--census", type=Path, default=DEFAULT_CENSUS); parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--recent-data-dir", type=Path, default=DEFAULT_RECENT); parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT); parser.add_argument("--workers", type=int, default=4); args = parser.parse_args(argv)
    try:
        required = (args.history_dir / "spot/monthly/klines", args.history_dir / "futures/um/monthly/klines",
                    args.recent_data_dir / "spot/daily/klines", args.recent_data_dir / "futures/um/daily/klines")
        if not all(path.is_dir() for path in required): raise BasisStudyError("spot and futures kline layouts must be readable")
        if not 1 <= args.workers <= 8: raise BasisStudyError("workers must be from one through eight")
        census = _load_census(args.census); both = _both_symbols(args.recent_data_dir)
        eligible = [row for row in census if row["symbol"] in both]
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in eligible: grouped[row["symbol"]].append(row)
        profiles: list[dict[str, Any]] = []; archives = parsed = 0
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(_group, args.history_dir, args.recent_data_dir, rows) for rows in grouped.values()]
            for ordinal, future in enumerate(as_completed(futures), 1):
                found, archive_count, parsed_count = future.result(); profiles.extend(found)
                archives += archive_count; parsed += parsed_count
                if ordinal % 50 == 0 or ordinal == len(futures):
                    print(f"AB18 | symbols={ordinal}/{len(futures)} | episodes={len(profiles)}", flush=True)
        profiles.sort(key=lambda row: row["episode_id"]); summary, report = _summarize(profiles, len(both), len(census))
        summary["eligible_symbols"] = len(grouped)
        history_spot = {path.name for path in (args.history_dir / "spot/monthly/klines").iterdir() if path.is_dir()}
        history_futures = {path.name for path in (args.history_dir / "futures/um/monthly/klines").iterdir() if path.is_dir()}
        summary["source"] = {"reader": "study.absorption._kline_rows", "class_reader": "study.stage_a.discover_market_tapes",
                             "history": str(args.history_dir), "recent": str(args.recent_data_dir),
                             "archives_read": archives, "bars_parsed": parsed,
                             "history_layout": {"spot_symbols": len(history_spot), "futures_symbols": len(history_futures),
                                                "same_symbol_overlap": len(history_spot & history_futures)}}
        _write(args.output_dir / "basis-during-pumps.jsonl", profiles, lines=True)
        _write(args.output_dir / "basis-summary.json", summary)
        for path in (args.output_dir / "basis-comparison.txt", args.report):
            _write(path, report, lines=True)
        print(f"DONE | eligible={len(profiles)} | complete_window={summary['evidence_status'].get('COMPLETE', 0)} | complete_rise={summary['complete_rise']}", flush=True)
        return 0
    except (OSError, UnicodeError, ValueError, BasisStudyError, absorption.AbsorptionError, stage_a.StageAError) as error:
        print(f"basis during pumps: {error}", file=sys.stderr); return 1

if __name__ == "__main__": raise SystemExit(main())
