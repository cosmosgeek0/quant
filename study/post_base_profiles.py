#!/usr/bin/env python3
"""Measure offline post-base pump paths from the AB13 census and 1m klines."""

from __future__ import annotations

import argparse, bisect, json, os, sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

try:
    from study import absorption
except ModuleNotFoundError as error:
    if error.name != "study": raise
    import absorption  # type: ignore

ROOT = Path(__file__).resolve().parents[1]; DEFAULT_CENSUS = ROOT / "lane-logs/AB13/pump-census-6mo.json"
DEFAULT_DATA = ROOT / "study/data-1m-history"; DEFAULT_OUTPUT = ROOT / "lane-logs/AB15"
DEFAULT_RECENT = Path("/Users/cosmosgeek/Desktop/quant/study/data")
DEFAULT_REPORT = ROOT / "lane-logs/AB15-report.txt"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MINUTE_MS, DAY_MS = 60_000, 86_400_000
CHECKPOINTS = (Decimal("5"), Decimal("10"), Decimal("25"))
HORIZON_MINUTES = {60: 1, 180: 3, 360: 6, 720: 12, 1_440: 24}
METRICS = ("time_to_peak_minutes", "time_to_5pct_minutes", "time_to_10pct_minutes", "time_to_25pct_minutes",
           "mfe_24h_pct", "mae_24h_pct", "giveback_peak_plus_1h_fraction", "giveback_peak_plus_3h_fraction",
           "giveback_peak_plus_6h_fraction", "giveback_peak_plus_12h_fraction", "giveback_peak_plus_24h_fraction")

class ProfileError(Exception): pass

def _decimal(raw: Any, source: str, *, positive: bool = False) -> Decimal:
    if isinstance(raw, (float, bool)): raise ProfileError(f"{source} is not Decimal text")
    try: value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError): raise ProfileError(f"{source} is not Decimal text") from None
    if not value.is_finite() or (positive and value <= 0):
        raise ProfileError(f"{source} is not a finite positive Decimal")
    return value

def _ratio(numerator: Decimal, denominator: Decimal, scale: Decimal = Decimal("1")) -> Decimal:
    with localcontext() as context:
        context.prec = 50; return numerator / denominator * scale

def _utc_ms(raw: Any, source: str) -> int:
    if not isinstance(raw, str): raise ProfileError(f"{source} is not canonical UTC text")
    try: parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError: raise ProfileError(f"{source} is not canonical UTC text") from None
    delta = parsed - EPOCH
    if delta.microseconds % 1_000: raise ProfileError(f"{source} is not millisecond-aligned")
    value = (delta.days * 86_400 + delta.seconds) * 1_000 + delta.microseconds // 1_000
    if absorption.utc_text(value) != raw: raise ProfileError(f"{source} is not canonical UTC text")
    return value

def _load_census(path: Path) -> list[dict[str, Any]]:
    try: payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProfileError(f"cannot read census: {error}") from None
    header = payload.get("header") if isinstance(payload, Mapping) else None
    raw_rows = payload.get("episodes") if isinstance(payload, Mapping) else None
    if not isinstance(header, Mapping) or header.get("schema") != "pump-census-6mo-v1":
        raise ProfileError("census schema differs")
    if not isinstance(raw_rows, list) or not raw_rows: raise ProfileError("census has no episode rows")
    rows, identifiers = [], set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping): raise ProfileError("census episode is not an object")
        row = dict(raw); identifier = row.get("episode_id")
        symbol, venue, label = row.get("symbol"), row.get("venue"), row.get("class")
        if not isinstance(identifier, str) or identifier in identifiers:
            raise ProfileError("census episode identifier differs")
        try: canonical = absorption.validate_symbol(symbol, f"{identifier} symbol")
        except absorption._SymbolValidationError as error: raise ProfileError(str(error)) from None
        if canonical != symbol or venue not in {"spot", "futures"} or label not in {"HELD", "COLLAPSED"}:
            raise ProfileError(f"{identifier} identity differs")
        base_ms = _utc_ms(row.get("base_utc"), f"{identifier} base")
        peak_ms = _utc_ms(row.get("peak_utc"), f"{identifier} peak")
        if peak_ms < base_ms or peak_ms > base_ms + DAY_MS or (peak_ms - base_ms) % MINUTE_MS:
            raise ProfileError(f"{identifier} peak time differs")
        row["_base_ms"], row["_peak_ms"] = base_ms, peak_ms
        row["_base_price"] = _decimal(row.get("base_price"), f"{identifier} base price", positive=True)
        row["_peak_price"] = _decimal(row.get("peak_price"), f"{identifier} peak price", positive=True)
        if row["_peak_price"] <= row["_base_price"]: raise ProfileError(f"{identifier} peak is not above base")
        identifiers.add(identifier); rows.append(row)
    return rows

def _merge_intervals(rows: Sequence[Mapping[str, Any]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted((row["_base_ms"], row["_peak_ms"] + DAY_MS) for row in rows):
        if merged and start <= merged[-1][1] + MINUTE_MS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else: merged.append((start, end))
    return merged

def _periods(intervals: Sequence[tuple[int, int]]) -> tuple[list[str], list[str]]:
    months, days = set(), set()
    for start, end in intervals:
        current = start - start % DAY_MS
        while current <= end:
            day = absorption.utc_text(current)[:10]; days.add(day); months.add(day[:7]); current += DAY_MS
    return sorted(months), sorted(days)

def _load_bars(history: Path, recent: Path, rows: Sequence[Mapping[str, Any]]) -> tuple[dict[int, absorption.KlineBar], int, int]:
    symbol, venue = rows[0]["symbol"], rows[0]["venue"]
    prefix = Path("spot") if venue == "spot" else Path("futures/um")
    intervals = _merge_intervals(rows); starts = [item[0] for item in intervals]
    months, days = _periods(intervals); paths = set()
    for root in (history, recent):
        base = root / prefix
        paths.update(base / "monthly/klines" / symbol / "1m" / f"{symbol}-1m-{month}.zip" for month in months)
        paths.update(base / "daily/klines" / symbol / "1m" / f"{symbol}-1m-{day}.zip" for day in days)
    indexed: dict[int, absorption.KlineBar] = {}; archives = parsed = 0
    for path in sorted(path for path in paths if path.is_file()):
        archives += 1
        for bar in absorption._kline_rows(path, symbol):
            parsed += 1
            point = bisect.bisect_right(starts, bar.open_ms) - 1
            if point < 0 or bar.open_ms > intervals[point][1]: continue
            prior = indexed.get(bar.open_ms)
            if prior is not None and prior != bar: raise ProfileError(f"{symbol} has conflicting kline evidence")
            indexed[bar.open_ms] = bar
    return indexed, archives, parsed

def _profile(row: Mapping[str, Any], bars: Mapping[int, absorption.KlineBar]) -> dict[str, Any]:
    identifier = row["episode_id"]; base_ms, peak_ms = row["_base_ms"], row["_peak_ms"]
    base_bar, peak_bar = bars.get(base_ms), bars.get(peak_ms)
    if base_bar is not None and base_bar.low != row["_base_price"]:
        raise ProfileError(f"{identifier} base price differs from kline")
    if peak_bar is not None and peak_bar.high != row["_peak_price"]:
        raise ProfileError(f"{identifier} peak price differs from kline")
    base = None if base_bar is None else base_bar.low
    peak = None if peak_bar is None else peak_bar.high
    result = {key: row[key] for key in ("episode_id", "symbol", "venue", "class", "base_utc", "peak_utc")}
    result.update({metric: None for metric in METRICS})
    result["base_price"], result["peak_price"] = base, peak
    if base is not None and peak is not None:
        result["time_to_peak_minutes"] = Decimal((peak_ms - base_ms) // MINUTE_MS)
        if _ratio(result["time_to_peak_minutes"], Decimal("60")) != _decimal(row.get("hours_to_peak"), f"{identifier} hours to peak"):
            raise ProfileError(f"{identifier} time to peak differs")
        rise = _ratio(peak - base, base, Decimal("100"))
        if rise != _decimal(row.get("rise_pct"), f"{identifier} rise"): raise ProfileError(f"{identifier} rise differs")
    base_times = range(base_ms, base_ms + DAY_MS + MINUTE_MS, MINUTE_MS)
    base_missing = sum(timestamp not in bars for timestamp in base_times)
    if base is not None:
        uninterrupted, remaining = True, set(CHECKPOINTS)
        for timestamp in range(base_ms, peak_ms + MINUTE_MS, MINUTE_MS):
            bar = bars.get(timestamp)
            if bar is None: uninterrupted = False
            elif uninterrupted:
                for checkpoint in tuple(remaining):
                    if bar.high >= base * (Decimal("1") + checkpoint / Decimal("100")):
                        name = absorption.decimal_text(checkpoint)
                        result[f"time_to_{name}pct_minutes"] = Decimal((timestamp - base_ms) // MINUTE_MS)
                        remaining.remove(checkpoint)
    if base is not None and base_missing == 0:
        window = [bars[timestamp] for timestamp in base_times]
        result["mfe_24h_pct"] = max(Decimal("0"), _ratio(max(bar.high for bar in window) - base, base, Decimal("100")))
        result["mae_24h_pct"] = min(Decimal("0"), _ratio(min(bar.low for bar in window) - base, base, Decimal("100")))
    post_missing = 0; lowest: Optional[Decimal] = None; half_time: Optional[Decimal] = None
    half_status = "EVIDENCE_ABSENT" if base is None or peak is None else "NOT_REACHED_24H"
    amplitude = None if base is None or peak is None else peak - base
    for minute in range(1, 1_441):
        bar = bars.get(peak_ms + minute * MINUTE_MS)
        if bar is None: post_missing += 1
        elif post_missing == 0:
            lowest = bar.low if lowest is None else min(lowest, bar.low)
            if amplitude is not None and half_time is None and lowest <= base + amplitude / Decimal("2"):
                half_time, half_status = Decimal(minute), "REACHED"
            if minute in HORIZON_MINUTES and amplitude is not None:
                hour = HORIZON_MINUTES[minute]
                result[f"giveback_peak_plus_{hour}h_fraction"] = max(Decimal("0"), _ratio(peak - lowest, amplitude))
    if post_missing and half_time is None: half_status = "EVIDENCE_ABSENT"
    if post_missing == 0 and amplitude is not None:
        observed = "COLLAPSED" if half_time is not None else "HELD"
        if observed != row["class"]: raise ProfileError(f"{identifier} class differs from kline")
        if lowest != _decimal(row.get("following_24h_low"), f"{identifier} following low"):
            raise ProfileError(f"{identifier} following low differs")
    result["half_giveback_minutes"], result["half_giveback_status"] = half_time, half_status
    result["evidence"] = {
        "base_to_peak_missing_minutes": sum(t not in bars for t in range(base_ms, peak_ms + MINUTE_MS, MINUTE_MS)),
        "base_24h_missing_minutes": base_missing, "post_peak_24h_missing_minutes": post_missing,
    }
    measured = sum(result[name] is not None for name in METRICS)
    result["evidence_status"] = "COMPLETE" if base_missing == post_missing == 0 else ("ABSENT" if measured == 0 else "PARTIAL")
    return result

def _group(history: Path, recent: Path, rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    bars, archives, parsed = _load_bars(history, recent, rows)
    return [_profile(row, bars) for row in rows], archives, parsed

def _stats(values: Sequence[Decimal], total: int) -> dict[str, Any]:
    return {"n": len(values), "absent": total - len(values), "min": min(values) if values else None,
            "median": absorption.quantile(values, 1, 2), "p90": absorption.quantile(values, 9, 10),
            "max": max(values) if values else None}

def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal): return absorption.decimal_text(value)
    if isinstance(value, Mapping): return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)): return [_jsonable(item) for item in value]
    return value

def _write(path: Path, payload: Any, *, lines: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        if lines:
            for row in payload: handle.write(json.dumps(_jsonable(row), sort_keys=True, separators=(",", ":")) + "\n")
        else: json.dump(_jsonable(payload), handle, sort_keys=True, separators=(",", ":")); handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)

def _plain(label: str, stats: Mapping[str, Any]) -> str:
    values = "/".join("NA" if stats[key] is None else format(stats[key], ".8g") for key in ("min", "median", "p90", "max"))
    return f"{label} n={stats['n']} absent={stats['absent']} min/median/p90/max={values}"

def _summarize(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    totals = Counter(row["class"] for row in rows); comparison = {}
    statuses = Counter(row["evidence_status"] for row in rows)
    lines = []
    for metric in METRICS:
        comparison[metric] = {}
        for label in ("HELD", "COLLAPSED"):
            values = [row[metric] for row in rows if row["class"] == label and row[metric] is not None]
            comparison[metric][label] = _stats(values, totals[label])
        lines.append(f"{metric} | {_plain('HELD', comparison[metric]['HELD'])} | {_plain('COLLAPSED', comparison[metric]['COLLAPSED'])} | MEASURED")
    escaped = [row["half_giveback_minutes"] for row in rows if row["class"] == "COLLAPSED" and row["half_giveback_minutes"] is not None]
    escape = _stats(escaped, totals["COLLAPSED"])
    lines.append(f"COLLAPSED_PEAK_TO_HALF_GIVEBACK_MINUTES | {_plain('COLLAPSED', escape)} | MEASURED")
    half_status = {label: dict(Counter(row["half_giveback_status"] for row in rows if row["class"] == label)) for label in ("HELD", "COLLAPSED")}
    summary = {"schema": "post-base-profile-summary-v1", "episodes": len(rows),
               "class_counts": dict(totals), "evidence_status": dict(statuses), "half_giveback_status": half_status,
               "metrics": comparison, "collapsed_peak_to_half_giveback_minutes": escape,
               "definitions": {"checkpoint": "first proved 1m high at or above the percentage rise from base",
                               "mfe_mae": "high and low percent excursions over base through the +24h endpoint",
                               "giveback": "max(0, (peak - cumulative post-peak low) / (peak - base)); values above one are retained",
                               "half_giveback": "first post-peak 1m low at or below the base/peak midpoint"}}
    return summary, lines

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--census", type=Path, default=DEFAULT_CENSUS); parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--recent-data-dir", type=Path, default=DEFAULT_RECENT); parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--workers", type=int, default=4); args = parser.parse_args(argv)
    try:
        if not 1 <= args.workers <= 8: raise ProfileError("workers must be from one through eight")
        if not args.data_dir.is_dir() or not args.recent_data_dir.is_dir(): raise ProfileError("both kline data directories must be readable")
        episodes = _load_census(args.census); grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for row in episodes: grouped[(row["venue"], row["symbol"])].append(row)
        profiles: list[dict[str, Any]] = []; archives = parsed = 0
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(_group, args.data_dir, args.recent_data_dir, rows) for rows in grouped.values()]
            for ordinal, future in enumerate(as_completed(futures), 1):
                group_profiles, group_archives, group_parsed = future.result()
                profiles.extend(group_profiles); archives += group_archives; parsed += group_parsed
                if ordinal % 50 == 0 or ordinal == len(futures):
                    print(f"AB15 | symbols={ordinal}/{len(futures)} | episodes={len(profiles)}", flush=True)
        profiles.sort(key=lambda row: row["episode_id"]); summary, lines = _summarize(profiles)
        summary["source"] = {"reader": "study.absorption._kline_rows", "archives_read": archives, "bars_parsed": parsed, "history": str(args.data_dir), "recent": str(args.recent_data_dir)}
        _write(args.output_dir / "post-base-profiles.jsonl", profiles, lines=True)
        _write(args.output_dir / "post-base-summary.json", summary)
        for text_path in (args.output_dir / "class-comparison.txt", args.report):
            temporary = text_path.with_name(text_path.name + ".tmp"); text_path.parent.mkdir(parents=True, exist_ok=True)
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n"); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, text_path)
        print(f"DONE | episodes={len(profiles)} | complete={summary['evidence_status'].get('COMPLETE', 0)} | partial={summary['evidence_status'].get('PARTIAL', 0)} | absent={summary['evidence_status'].get('ABSENT', 0)}", flush=True)
        return 0
    except (OSError, UnicodeError, ValueError, ProfileError, absorption.AbsorptionError) as error:
        print(f"post-base profiles: {error}", file=sys.stderr); return 1

if __name__ == "__main__": raise SystemExit(main())
