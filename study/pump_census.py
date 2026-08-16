#!/usr/bin/env python3
"""Census recent pump episodes and measure their preceding absorption rows."""

from __future__ import annotations

import argparse, json, os, sys
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

try:
    from study import absorption, baselines
    from study import validate_absorption_runners as runners
except ImportError:
    import absorption, baselines  # type: ignore
    import validate_absorption_runners as runners  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = Path("/Users/cosmosgeek/Desktop/quant/study/data")
DEFAULT_LOWS = ROOT / "lane-logs" / "AB5" / "long-low-positions.json"
DEFAULT_OUTPUT = ROOT / "lane-logs" / "AB8"
MINUTE_MS, DAY_MINUTES = 60_000, 1_440
RISE_PCT, VOLUME_MULTIPLE = Decimal("25"), Decimal("3")
LOW_EDGE, HIGH_EDGE = Decimal("0.10"), Decimal("1")
EVALUATION_CAP = 40
COMPONENTS = runners.COMPONENT_NAMES


class CensusError(Exception): pass


def _decimal(raw: Any, label: str) -> Decimal:
    try: value = Decimal(str(raw))
    except (InvalidOperation, ValueError): raise CensusError(f"{label} is not a Decimal") from None
    if not value.is_finite(): raise CensusError(f"{label} is not finite")
    return value


def _text(value: Decimal) -> str:
    return absorption.decimal_text(value)


def _write(path: Path, payload: Any, *, lines: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        if lines:
            for row in payload: handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        else: json.dump(payload, handle, sort_keys=True, separators=(",", ":")); handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def _metadata(path: Path) -> Tuple[Mapping[str, Any], Dict[str, Dict[str, Any]]]:
    try: payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CensusError(f"cannot read long-low input: {error}") from None
    if not isinstance(payload, Mapping) or payload.get("schema") != "long-low-positions-v1":
        raise CensusError("long-low input schema differs")
    raw_symbols = payload.get("symbols")
    if not isinstance(raw_symbols, Mapping) or not raw_symbols: raise CensusError("long-low input has no symbols")
    records: Dict[str, Dict[str, Any]] = {}
    for symbol, raw in sorted(raw_symbols.items()):
        if not isinstance(symbol, str) or not isinstance(raw, Mapping): raise CensusError("long-low symbol row differs")
        venue, days = raw.get("venue"), raw.get("days_of_history")
        if venue not in {"spot", "futures"} or not isinstance(days, int) or days < 1:
            raise CensusError(f"{symbol} venue or history differs")
        low, high = _decimal(raw.get("low_365"), f"{symbol} low"), _decimal(raw.get("high_365"), f"{symbol} high")
        if low <= 0 or high < low: raise CensusError(f"{symbol} yearly bounds differ")
        records[symbol] = {"venue": venue, "days": days, "low": low, "high": high,
                           "reported_position": _decimal(raw.get("low_position_365"), f"{symbol} position")}
    if payload.get("symbol_count") != len(records): raise CensusError("long-low symbol count differs")
    return payload, records


def _venue_root(data_dir: Path, venue: str) -> Path:
    return data_dir / "spot" if venue == "spot" else data_dir / "futures" / "um"


def _bars(data_dir: Path, symbol: str, venue: str) -> Tuple[absorption.KlineBar, ...]:
    paths = sorted((_venue_root(data_dir, venue) / "daily" / "klines" / symbol / "1m").glob("*.zip"))
    if not paths: return ()
    indexed: Dict[int, absorption.KlineBar] = {}
    for path in paths:
        for bar in absorption._kline_rows(path, symbol):
            prior = indexed.get(bar.open_ms)
            if prior is not None and prior != bar: raise CensusError(f"{symbol} has conflicting kline evidence")
            indexed[bar.open_ms] = bar
    return tuple(indexed[key] for key in sorted(indexed))


def _median_day(bars: Sequence[absorption.KlineBar]) -> Tuple[Optional[Decimal], int]:
    days: Dict[int, Dict[int, absorption.KlineBar]] = defaultdict(dict)
    for bar in bars: days[bar.open_ms - bar.open_ms % (DAY_MINUTES * MINUTE_MS)][bar.open_ms] = bar
    volumes = []
    for start, by_open in sorted(days.items()):
        expected = tuple(start + minute * MINUTE_MS for minute in range(DAY_MINUTES))
        if tuple(sorted(by_open)) == expected:
            volumes.append(sum((by_open[value].quote_volume for value in expected), Decimal("0")))
    positive = [value for value in volumes if value > 0]
    return (absorption.quantile(positive, 1, 2) if positive else None), len(volumes)


def _segments(bars: Sequence[absorption.KlineBar]) -> Iterable[Tuple[int, int]]:
    start = 0
    for index in range(1, len(bars)):
        if bars[index].open_ms - bars[index - 1].open_ms != MINUTE_MS:
            yield start, index - 1; start = index
    if bars: yield start, len(bars) - 1


def _ratio(numerator: Decimal, denominator: Decimal, scale: Decimal = Decimal("1")) -> Decimal:
    with localcontext() as context:
        context.prec = 50; return numerator / denominator * scale


def _year_position(base: Decimal, record: Mapping[str, Any]) -> Decimal:
    low, high = record["low"], record["high"]
    if high == low: return Decimal("0") if base <= low else Decimal("1")
    return min(Decimal("1"), max(Decimal("0"), _ratio(base - low, high - low)))


def _episode_row(symbol: str, venue: str, bars: Sequence[absorption.KlineBar], prefix: Sequence[Decimal],
                 base_index: int, peak_index: int, median_day: Decimal, record: Mapping[str, Any]) -> Dict[str, Any]:
    base, peak = bars[base_index].low, bars[peak_index].high
    rise = _ratio(peak - base, base, Decimal("100"))
    volume = _ratio(prefix[peak_index + 1] - prefix[base_index], median_day)
    follow_low = min(bar.low for bar in bars[peak_index + 1:peak_index + DAY_MINUTES + 1])
    midpoint = base + (peak - base) / Decimal("2")
    position = _year_position(base, record)
    return {"symbol": symbol, "venue": venue, "date_utc": absorption.utc_text(bars[base_index].open_ms)[:10],
            "base_utc": absorption.utc_text(bars[base_index].open_ms), "peak_utc": absorption.utc_text(bars[peak_index].open_ms),
            "base_price": _text(base), "peak_price": _text(peak), "rise_pct": _text(rise),
            "hours_to_peak": _text(_ratio(Decimal(bars[peak_index].open_ms - bars[base_index].open_ms), Decimal("3600000"))),
            "volume_multiple": _text(volume), "class": "HELD" if follow_low > midpoint else "COLLAPSED",
            "following_24h_low": _text(follow_low), "low_position_365": _text(position),
            "ab5_reported_latest_low_position_365": _text(record["reported_position"]),
            "listing_age_days": record["days"], "listing_age_class": "veteran" if record["days"] >= absorption.VETERAN_HISTORY_DAYS else "fresh",
            "_base_ms": bars[base_index].open_ms, "_rise": rise}


def _symbol_episodes(symbol: str, venue: str, bars: Sequence[absorption.KlineBar],
                     median_day: Decimal, record: Mapping[str, Any]) -> Tuple[list[Dict[str, Any]], int]:
    prefix = [Decimal("0")]
    for bar in bars: prefix.append(prefix[-1] + bar.quote_volume)
    found, trailing = [], 0
    for start, end in _segments(bars):
        bases = [-1] * len(bars); queue: deque[int] = deque()
        for index in range(start, end + 1):
            while queue and queue[0] < index - DAY_MINUTES: queue.popleft()
            if index - start >= DAY_MINUTES: bases[index] = queue[0]
            while queue and bars[index].low < bars[queue[-1]].low: queue.pop()
            queue.append(index)
        index, last_peak = start + DAY_MINUTES, start - 1
        while index <= end:
            base_index = bases[index]
            if base_index <= last_peak: index += 1; continue
            base = bars[base_index].low
            rise = _ratio(bars[index].high - base, base, Decimal("100"))
            volume = _ratio(prefix[index + 1] - prefix[base_index], median_day)
            if rise < RISE_PCT or volume < VOLUME_MULTIPLE: index += 1; continue
            final = min(end, base_index + DAY_MINUTES)
            eligible = [point for point in range(index, final + 1)
                        if _ratio(bars[point].high - base, base, Decimal("100")) >= RISE_PCT
                        and _ratio(prefix[point + 1] - prefix[base_index], median_day) >= VOLUME_MULTIPLE]
            peak_index = max(eligible, key=lambda point: (bars[point].high, -point))
            if peak_index + DAY_MINUTES <= end:
                found.append(_episode_row(symbol, venue, bars, prefix, base_index, peak_index, median_day, record))
            else: trailing += 1
            last_peak, index = peak_index, peak_index + 1
    return found, trailing


def _scan(data_dir: Path, records: Mapping[str, Mapping[str, Any]]) -> Tuple[list[Dict[str, Any]], Dict[str, Any]]:
    episodes, without_bars, without_median, trailing, complete_days = [], 0, 0, 0, 0
    for ordinal, (symbol, record) in enumerate(records.items(), 1):
        bars = _bars(data_dir, symbol, record["venue"])
        if not bars: without_bars += 1; continue
        median, days = _median_day(bars); complete_days += days
        if median is None: without_median += 1; continue
        rows, incomplete = _symbol_episodes(symbol, record["venue"], bars, median, record)
        episodes.extend(rows); trailing += incomplete
        if ordinal % 100 == 0: print(f"AB8 scan | symbols={ordinal}/{len(records)} | episodes={len(episodes)}", flush=True)
    episodes.sort(key=lambda row: (row["_base_ms"], row["symbol"], -row["_rise"]))
    for number, row in enumerate(episodes, 1): row["episode_id"] = f"P{number:04d}"
    return episodes, {"symbols_scanned": len(records), "symbols_without_bars": without_bars,
                      "symbols_without_complete_day_median": without_median,
                      "complete_symbol_days": complete_days, "trailing_candidates_without_24h_followup": trailing}


def _lead_hours(base_ms: int, window_end: str) -> Decimal:
    return _ratio(Decimal(base_ms - runners._utc_ms(window_end)), Decimal("3600000"))


def _prior_record(episode: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], reason: Optional[Any]) -> Dict[str, Any]:
    ready = [row for row in rows if row["window_tape_ready"]]
    elevated = None if not ready else {name: any((runners._decimal(row["components"][name]["factor"]) or Decimal("0")) > 0
                                                  for row in ready) for name in COMPONENTS}
    best = max(ready, key=runners._rank) if ready else None
    caught = [row for row in ready if sum((runners._decimal(row["components"][name]["factor"]) or Decimal("0")) > 0
                                          for name in COMPONENTS) >= 3]
    earliest = min(caught, key=lambda row: row["window_end_utc"]) if caught else None
    return {"episode_id": episode["episode_id"], "symbol": episode["symbol"], "class": episode["class"],
            "windows_scheduled": runners.HORIZON_HOURS, "windows_evaluated": len(rows), "windows_tape_ready": len(ready),
            "component_elevated": elevated, "best_pre_window": best, "catchable": None if not ready else bool(caught),
            "earliest_catchable_window": earliest,
            "earliest_catchable_lead_hours": None if earliest is None else _text(_lead_hours(episode["_base_ms"], earliest["window_end_utc"])),
            "gap_reason": (reason or "no_tape_ready_prior_window") if not ready else None}


def _baseline(source: Path, path: Path, symbols: Sequence[str], venue: str) -> Mapping[str, Any]:
    if path.is_file():
        try: cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError): cached = None
        if isinstance(cached, Mapping):
            covered = set(cached.get("symbols", {})) | set(cached.get("not_ready", {}))
            if (cached.get("schema") == baselines.ABSORPTION_BASELINE_SCHEMA
                    and cached.get("window_minutes") == runners.WINDOW_MINUTES
                    and cached.get("minimum_days") == runners.MIN_KLINE_DAYS
                    and cached.get("minimum_tape_days") == runners.MIN_TAPE_DAYS
                    and cached.get("source", {}).get("venue") == venue and covered == set(symbols)):
                if cached.get("symbols"): absorption.load_historical_baselines(path, expected_window_minutes=runners.WINDOW_MINUTES)
                print(f"AB8 baseline | venue={venue} | symbols={len(symbols)} | reused=true", flush=True)
                return cached
    payload = baselines.run_absorption_baselines(source, path, symbols=symbols,
                window_minutes=runners.WINDOW_MINUTES, minimum_days=runners.MIN_KLINE_DAYS,
                minimum_tape_days=runners.MIN_TAPE_DAYS)
    payload["source"]["venue"] = venue
    if venue == "futures": payload["source"].update({"tape": "futures_um_1m_klines", "agg_trades": "futures_um_daily_aggTrades"})
    _write(path, payload); return payload


def _prior_trades(source: Path, symbol: str, episodes: Sequence[Mapping[str, Any]]) -> Tuple[absorption.AggTrade, ...]:
    day_ms = DAY_MINUTES * MINUTE_MS; days = set()
    for episode in episodes:
        first, last = episode["_base_ms"] - runners.HORIZON_HOURS * 60 * MINUTE_MS, episode["_base_ms"] - 1
        days.update(range(first - first % day_ms, last - last % day_ms + 1, day_ms))
    indexed: Dict[int, absorption.AggTrade] = {}
    for day in sorted(days):
        raw_day = absorption.utc_text(day)[:10]
        path = source / "daily" / "aggTrades" / symbol / f"{symbol}-aggTrades-{raw_day}.zip"
        if not path.is_file(): continue
        for trade in absorption._agg_trade_rows(path, symbol):
            prior = indexed.get(trade.aggregate_id)
            if prior is not None and prior != trade: raise CensusError(f"{symbol} has conflicting aggTrade evidence")
            indexed[trade.aggregate_id] = trade
    return tuple(sorted(indexed.values(), key=lambda trade: (trade.timestamp_ms, trade.aggregate_id)))


def _evaluate(data_dir: Path, output_dir: Path, episodes: Sequence[Mapping[str, Any]]) -> Tuple[list[Dict[str, Any]], int]:
    chosen = sorted(episodes, key=lambda row: (-row["_rise"], row["symbol"], row["_base_ms"]))[:EVALUATION_CAP]
    results: Dict[str, Dict[str, Any]] = {}
    for venue in ("spot", "futures"):
        selected = [row for row in chosen if row["venue"] == venue]
        if not selected: continue
        symbols = tuple(sorted({row["symbol"] for row in selected})); source = _venue_root(data_dir, venue)
        baseline_path = output_dir / f"runner-baselines-{venue}.json"
        payload = _baseline(source, baseline_path, symbols, venue)
        profiles = absorption.load_historical_baselines(baseline_path, expected_window_minutes=runners.WINDOW_MINUTES) if payload["symbols"] else {}
        archive_days = {symbol: runners._archive_days(source, symbol) for symbol in symbols}
        for ordinal, symbol in enumerate(symbols, 1):
            symbol_episodes = [row for row in selected if row["symbol"] == symbol]
            reason = payload.get("not_ready", {}).get(symbol)
            profile = profiles.get(symbol); bars = _bars(data_dir, symbol, venue) if profile is not None else ()
            trades = _prior_trades(source, symbol, symbol_episodes) if profile is not None and profile.tape is not None else ()
            for episode in symbol_episodes:
                rows = () if profile is None or not bars else runners._score_rows(
                    symbol, episode["base_utc"], "pump_census_base", bars, trades, profile, archive_days[symbol])
                tape_reason = reason
                if profile is not None and profile.tape is None:
                    tape_reason = payload["symbols"][symbol].get("components", {}).get("tape", {})
                results[episode["episode_id"]] = _prior_record(episode, rows, tape_reason)
            print(f"AB8 prior | venue={venue} | symbols={ordinal}/{len(symbols)} | episodes={len(symbol_episodes)}", flush=True)
    ordered = [results[row["episode_id"]] for row in chosen]
    return ordered, max(0, len(episodes) - len(chosen))


def _pattern(episodes: Sequence[Mapping[str, Any]], priors: Sequence[Mapping[str, Any]], cut: int) -> Dict[str, Any]:
    ready = [row for row in priors if row["windows_tape_ready"] > 0]
    scoreboard: Dict[str, Any] = {}
    for name in COMPONENTS:
        scoreboard[name] = {}
        for outcome in ("HELD", "COLLAPSED"):
            group = [row for row in ready if row["class"] == outcome]
            hits = sum(bool(row["component_elevated"][name]) for row in group)
            scoreboard[name][outcome] = {"fired": hits, "population": len(group), "fraction": f"{hits}/{len(group)}"}
    positions = {"yearly_lows_under_0.10": 0, "mid_range_0.10_to_below_1.00": 0, "at_yearly_high_1.00": 0}
    for row in episodes:
        value = _decimal(row["low_position_365"], "episode low position")
        key = "yearly_lows_under_0.10" if value < LOW_EDGE else "at_yearly_high_1.00" if value >= HIGH_EDGE else "mid_range_0.10_to_below_1.00"
        positions[key] += 1
    caught = [row for row in priors if row["catchable"]]
    leads = [_decimal(row["earliest_catchable_lead_hours"], "catchable lead") for row in caught]
    return {"census": {"episodes": len(episodes), "HELD": sum(row["class"] == "HELD" for row in episodes),
                        "COLLAPSED": sum(row["class"] == "COLLAPSED" for row in episodes),
                        "veteran": sum(row["listing_age_class"] == "veteran" for row in episodes),
                        "fresh": sum(row["listing_age_class"] == "fresh" for row in episodes), "yearly_position_bins": positions},
            "prior_population": {"evaluated_largest_by_rise": len(priors), "cut_by_cap": cut, "with_tape_ready_prior": len(ready)},
            "component_scoreboard": scoreboard,
            "catchability": {"episodes": len(caught), "evaluated_population": len(priors), "tape_ready_population": len(ready),
                              "median_earliest_lead_hours": None if not leads else _text(absorption.quantile(leads, 1, 2))},
            "honest_gaps": {"episodes_without_tape_ready_prior": len(priors) - len(ready)}}


def _public_episode(row: Mapping[str, Any], prior_ids: set[str]) -> Dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_")} | {
        "stage2_status": "EVALUATED" if row["episode_id"] in prior_ids else "CUT_BY_RISE_CAP"}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--long-lows", type=Path, default=DEFAULT_LOWS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        metadata, records = _metadata(args.long_lows)
        episodes, scan = _scan(args.data_dir, records)
        if not episodes: raise CensusError("census found no proved pump episodes")
        priors, cut = _evaluate(args.data_dir, args.output_dir, episodes)
        pattern = _pattern(episodes, priors, cut); prior_ids = {row["episode_id"] for row in priors}
        header = {"schema": "pump-census-v1", "as_of": metadata.get("as_of"),
                  "census_definitions_not_trading_thresholds": {
                      "base": "lowest proved 1m low in the preceding continuous 24h",
                      "rise": "at least 25 percent from base to a peak no more than 24h later",
                      "volume": "base-to-peak quote volume at least 3x the symbol median complete UTC day",
                      "HELD": "following continuous 24h low retraced less than half the rise",
                      "COLLAPSED": "following continuous 24h low gave back half or more"},
                  "episode_consolidation": "after the first qualifying minute, retain the highest qualifying peak within that base's 24h horizon; later episodes require a base after that peak",
                  "listing_age_classes": "fresh under 90 history days; veteran at least 90 days, reusing study.absorption",
                  "yearly_position": "episode base in the AB5 low_365 to high_365 range, clamped to zero through one",
                  "yearly_position_bins_descriptive_only": "low under 0.10; mid 0.10 to below 1.00; high only at the 1.00 range endpoint",
                  "stage2": "largest 40 episodes by rise when capped; 48 hourly windows immediately before each base; existing runner evaluator and rank",
                  "scan": scan}
        _write(args.output_dir / "pump-census.json", {"header": header, "episodes": [_public_episode(row, prior_ids) for row in episodes]})
        _write(args.output_dir / "pump-priors.jsonl", priors, lines=True)
        _write(args.output_dir / "pump-pattern.json", pattern)
    except (OSError, UnicodeError, ValueError, CensusError, baselines.BaselineError, absorption.AbsorptionError,
            runners.ValidationError) as error:
        print(f"pump census: {error}", file=sys.stderr); return 2
    print(f"AB8 | symbols={scan['symbols_scanned']} | episodes={len(episodes)} | evaluated={len(priors)} | cut={cut} | output={args.output_dir}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
