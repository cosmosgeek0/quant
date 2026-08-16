#!/usr/bin/env python3
"""Measure the 30-day base rate of three-or-more absorption components."""

from __future__ import annotations

import argparse, json, os, sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

try:
    from study import absorption, baselines
    from study import validate_absorption_runners as runners
except ImportError:
    import absorption, baselines  # type: ignore
    import validate_absorption_runners as runners  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = Path("/Users/cosmosgeek/Desktop/quant/study/data")
DEFAULT_CENSUS = ROOT / "lane-logs" / "AB8" / "pump-census.json"
DEFAULT_PRIORS = ROOT / "lane-logs" / "AB8" / "pump-priors.jsonl"
DEFAULT_LOWS = ROOT / "lane-logs" / "AB5" / "long-low-positions.json"
DEFAULT_OUTPUT = ROOT / "lane-logs" / "AB9"
AB8_BASELINES = ROOT / "lane-logs" / "AB8"
DAY_COUNT, STEP_HOURS, MATCH_HOURS = 30, 4, 72
HOUR_MS, DAY_MS = 3_600_000, 86_400_000
TIERS = (("at_least_3", 3), ("at_least_4", 4), ("exactly_5", 5))


class StudyError(Exception): pass


def _read_json(path: Path) -> Any:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StudyError(f"cannot read {path.name}: {error}") from None


def _write(path: Path, payload: Any, *, lines: bool = False) -> None:
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


def _venue_root(data_dir: Path, venue: str) -> Path:
    return data_dir / "spot" if venue == "spot" else data_dir / "futures" / "um"


def _days(as_of: str) -> Tuple[datetime, ...]:
    try: end = datetime.fromisoformat(as_of + "T00:00:00+00:00") + timedelta(days=1)
    except ValueError: raise StudyError("census as_of is not a date") from None
    return tuple(end - timedelta(days=DAY_COUNT - index) for index in range(DAY_COUNT))


def _census_profiles(symbols: Sequence[str], lows: Mapping[str, Any]) -> Dict[str, absorption.HistoricalKlineBaseline]:
    loaded: Dict[str, absorption.HistoricalKlineBaseline] = {}
    for venue in ("spot", "futures"):
        path = AB8_BASELINES / f"runner-baselines-{venue}.json"
        loaded.update(absorption.load_historical_baselines(path, expected_window_minutes=runners.WINDOW_MINUTES))
    missing = [symbol for symbol in symbols if symbol not in loaded or loaded[symbol].tape is None]
    wrong = [symbol for symbol in symbols if lows.get(symbol, {}).get("venue") not in {"spot", "futures"}]
    if missing or wrong: raise StudyError("AB8 tape-ready census profile set differs")
    return {symbol: loaded[symbol] for symbol in symbols}


def _candidate_has_archives(data_dir: Path, symbol: str, venue: str) -> bool:
    source = _venue_root(data_dir, venue)
    tapes = source / "daily" / "aggTrades" / symbol
    klines = source / "daily" / "klines" / symbol / "1m"
    return len(tuple(tapes.glob("*.zip"))) >= runners.MIN_TAPE_DAYS and len(tuple(klines.glob("*.zip"))) >= runners.MIN_KLINE_DAYS


def _control_profiles(data_dir: Path, output_dir: Path, lows: Mapping[str, Any], census_symbols: set[str]) -> Tuple[Tuple[str, ...], Dict[str, absorption.HistoricalKlineBaseline], list[Dict[str, Any]]]:
    selected, profiles, evidence = [], {}, []
    temporary = output_dir / ".control-candidate-baseline.json"
    try:
        for symbol, record in sorted(lows.items()):
            venue = record.get("venue") if isinstance(record, Mapping) else None
            if symbol in census_symbols or record.get("status") != "READY" or venue not in {"spot", "futures"}: continue
            if not _candidate_has_archives(data_dir, symbol, venue): continue
            try:
                payload = baselines.run_absorption_baselines(
                    _venue_root(data_dir, venue), temporary, symbols=(symbol,),
                    window_minutes=runners.WINDOW_MINUTES, minimum_days=runners.MIN_KLINE_DAYS,
                    minimum_tape_days=runners.MIN_TAPE_DAYS)
                loaded = absorption.load_historical_baselines(temporary, expected_window_minutes=runners.WINDOW_MINUTES)
            except (OSError, ValueError, baselines.BaselineError, absorption.AbsorptionError):
                evidence.append({"symbol": symbol, "venue": venue, "status": "NOT_READY", "reason": "baseline_evidence_invalid"}); continue
            profile = loaded.get(symbol); tape = None if profile is None else profile.tape
            raw = payload.get("symbols", {}).get(symbol, {}).get("components", {}).get("tape", {})
            if tape is None:
                evidence.append({"symbol": symbol, "venue": venue, "status": "NOT_READY", "reason": raw.get("reason", "tape_profile_absent")}); continue
            selected.append(symbol); profiles[symbol] = profile
            evidence.append({"symbol": symbol, "venue": venue, "status": "READY", "baseline_tape_days": tape.day_count})
            if len(selected) == 30: break
    finally:
        try: temporary.unlink()
        except FileNotFoundError: pass
    if len(selected) != 30: raise StudyError(f"only {len(selected)} sorted controls proved tape-ready")
    return tuple(selected), profiles, evidence


def _day_evidence(source: Path, symbol: str, day: datetime) -> Tuple[Dict[int, absorption.KlineBar], Dict[int, Tuple[absorption.AggTrade, ...]], str, str]:
    raw_day = day.date().isoformat(); day_ms = runners._utc_ms(raw_day + "T00:00:00Z")
    kline_path = source / "daily" / "klines" / symbol / "1m" / f"{symbol}-1m-{raw_day}.zip"
    trade_path = source / "daily" / "aggTrades" / symbol / f"{symbol}-aggTrades-{raw_day}.zip"
    bars: Dict[int, absorption.KlineBar] = {}
    kline_status = "ARCHIVE_ABSENT"
    if kline_path.is_file():
        try:
            for bar in absorption._kline_rows(kline_path, symbol):
                prior = bars.get(bar.open_ms)
                if prior is not None and prior != bar: raise absorption.AbsorptionError("conflicting kline row")
                bars[bar.open_ms] = bar
            kline_status = "ARCHIVE_PRESENT"
        except absorption.AbsorptionError:
            bars.clear(); kline_status = "ARCHIVE_INVALID"
    buckets: Dict[int, list[absorption.AggTrade]] = {hour: [] for hour in range(0, 24, STEP_HOURS)}
    tape_status = "ARCHIVE_ABSENT"
    if trade_path.is_file():
        try:
            previous = None
            for trade in absorption._agg_trade_rows(trade_path, symbol):
                key = (trade.timestamp_ms, trade.aggregate_id)
                if not day_ms <= trade.timestamp_ms < day_ms + DAY_MS or (previous is not None and key <= previous):
                    raise absorption.AbsorptionError("trade ordering differs")
                previous = key; hour = (trade.timestamp_ms - day_ms) // HOUR_MS
                if hour in buckets: buckets[hour].append(trade)
            tape_status = "ARCHIVE_PRESENT"
        except absorption.AbsorptionError:
            buckets = {hour: [] for hour in buckets}; tape_status = "ARCHIVE_INVALID"
    return bars, {hour: tuple(values) for hour, values in buckets.items()}, kline_status, tape_status


def _episode_index(episodes: Sequence[Mapping[str, Any]]) -> Dict[str, Tuple[Dict[str, Any], ...]]:
    indexed: Dict[str, list[Dict[str, Any]]] = {}
    for row in episodes:
        try:
            item = {"episode_id": row["episode_id"], "class": row["class"],
                    "base_ms": runners._utc_ms(row["base_utc"]), "peak_ms": runners._utc_ms(row["peak_utc"])}
        except (KeyError, TypeError, ValueError, runners.ValidationError): raise StudyError("census episode row differs") from None
        indexed.setdefault(str(row.get("symbol")), []).append(item)
    return {symbol: tuple(sorted(rows, key=lambda row: (row["base_ms"], row["episode_id"]))) for symbol, rows in indexed.items()}


def _evaluate_symbol(data_dir: Path, symbol: str, group: str, venue: str, profile: absorption.HistoricalKlineBaseline,
                     days: Sequence[datetime], episodes: Sequence[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    rows = []; source = _venue_root(data_dir, venue)
    for day in days:
        bars, trades, kline_status, tape_status = _day_evidence(source, symbol, day)
        day_ms = runners._utc_ms(day.date().isoformat() + "T00:00:00Z")
        for hour in range(0, 24, STEP_HOURS):
            start = day_ms + hour * HOUR_MS; end = start + HOUR_MS - 1
            window = tuple(bars[value] for value in (start + minute * runners.MINUTE_MS for minute in range(60)) if value in bars)
            inside = any(start <= item["peak_ms"] and end >= item["base_ms"] for item in episodes)
            following = [item for item in episodes if end < item["base_ms"] <= end + MATCH_HOURS * HOUR_MS]
            base = {"schema": "signal-base-rate-window-v1", "symbol": symbol, "group": group, "venue": venue,
                    "window_start_utc": absorption.utc_text(start), "window_end_utc": absorption.utc_text(end),
                    "inside_episode_rise": inside, "following_72h_episode_ids": [item["episode_id"] for item in following],
                    "following_72h_classes": sorted({item["class"] for item in following})}
            if len(window) != 60:
                rows.append(base | {"status": "MISSING_KLINE_EVIDENCE", "kline_evidence": kline_status,
                                    "tape_evidence": tape_status, "fired_component_count": None, "fired_components": None}); continue
            scored = absorption.score_absorption_window(symbol, (), window, (), trades[hour], historical_baseline=profile)
            components = runners._components(scored, window[-1].close, profile)
            values = [runners._decimal(components[name]["factor"]) for name in runners.COMPONENT_NAMES]
            ready = tape_status == "ARCHIVE_PRESENT" and not scored["unknown_features"]
            fired = None if not ready else [name for name, value in zip(runners.COMPONENT_NAMES, values) if value is not None and value > 0]
            evidence = tape_status if trades[hour] else ("NO_WINDOW_TRADES" if tape_status == "ARCHIVE_PRESENT" else tape_status)
            rows.append(base | {"status": "EVALUATED_TAPE_READY" if ready else "EVALUATED_TAPE_MISSING",
                                "kline_evidence": kline_status, "tape_evidence": evidence,
                                "fired_component_count": None if fired is None else len(fired), "fired_components": fired})
    return rows


def _fraction(numerator: int, denominator: int) -> Optional[str]:
    if denominator == 0: return None
    with localcontext() as context:
        context.prec = 50; return absorption.decimal_text(Decimal(numerator) / Decimal(denominator))


def _group_summary(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    ready = [row for row in rows if row["status"] == "EVALUATED_TAPE_READY"]
    eligible = [row for row in ready if not row["inside_episode_rise"]]
    tiers: Dict[str, Any] = {}
    for name, count in TIERS:
        alarms = [row for row in eligible if row["fired_component_count"] >= count]
        true = sum(bool(row["following_72h_episode_ids"]) for row in alarms); false = len(alarms) - true
        tiers[name] = {"alarms": len(alarms), "true_alarms": true, "false_alarms": false,
                       "precision_fraction": f"{true}/{len(alarms)}", "precision": _fraction(true, len(alarms)),
                       "alarm_frequency_per_observed_symbol_day": _fraction(len(alarms) * 6, len(eligible)),
                       "excluded_rise_firings": sum(row["inside_episode_rise"] and row["fired_component_count"] >= count for row in ready)}
    return {"windows_scheduled": len(rows), "windows_evaluated": sum(row["status"] != "MISSING_KLINE_EVIDENCE" for row in rows),
            "windows_tape_ready": len(ready), "windows_lacking_tape_evidence": sum(row["status"] == "EVALUATED_TAPE_MISSING" for row in rows),
            "windows_lacking_kline_evidence": sum(row["status"] == "MISSING_KLINE_EVIDENCE" for row in rows),
            "rise_windows_excluded": sum(row["inside_episode_rise"] for row in ready), "tiers": tiers}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag, default in (("--data-dir", DEFAULT_DATA), ("--census", DEFAULT_CENSUS), ("--priors", DEFAULT_PRIORS),
                          ("--long-lows", DEFAULT_LOWS), ("--output-dir", DEFAULT_OUTPUT)):
        parser.add_argument(flag, type=Path, default=default)
    args = parser.parse_args(argv)
    try:
        census, lows_payload = _read_json(args.census), _read_json(args.long_lows)
        priors = [json.loads(line) for line in args.priors.read_text(encoding="utf-8").splitlines() if line]
        if census.get("header", {}).get("schema") != "pump-census-v1" or lows_payload.get("schema") != "long-low-positions-v1":
            raise StudyError("input schema differs")
        episodes, lows = census.get("episodes"), lows_payload.get("symbols")
        if not isinstance(episodes, list) or not isinstance(lows, Mapping): raise StudyError("input population differs")
        ready_priors = [row for row in priors if row.get("windows_tape_ready", 0) > 0]
        if len(ready_priors) != 33: raise StudyError(f"AB8 tape-ready episode count is {len(ready_priors)}, expected 33")
        census_cohort = tuple(sorted({row["symbol"] for row in ready_priors})); all_census = {row["symbol"] for row in episodes}
        census_profiles = _census_profiles(census_cohort, lows)
        controls, control_profiles, control_evidence = _control_profiles(args.data_dir, args.output_dir, lows, all_census)
        profiles = census_profiles | control_profiles; episode_map = _episode_index(episodes); days = _days(census["header"]["as_of"])
        rows = []
        for ordinal, symbol in enumerate(census_cohort + controls, 1):
            group = "census" if symbol in census_cohort else "control"; venue = lows[symbol]["venue"]
            rows.extend(_evaluate_symbol(args.data_dir, symbol, group, venue, profiles[symbol], days, episode_map.get(symbol, ())))
            print(f"AB9 evaluate | symbols={ordinal}/{len(census_cohort) + len(controls)} | symbol={symbol}", flush=True)
        expected = len(profiles) * DAY_COUNT * (24 // STEP_HOURS)
        if len(rows) != expected or any(row["group"] == "control" and row["following_72h_episode_ids"] for row in rows):
            raise StudyError("population window invariant differs")
        groups = {"all": _group_summary(rows), "census": _group_summary([row for row in rows if row["group"] == "census"]),
                  "control": _group_summary([row for row in rows if row["group"] == "control"])}
        population = {"schema": "signal-base-rate-population-v1", "selection": {
            "census_source": "33 AB8 tape-ready episode rows, deduplicated by symbol",
            "control_source": "first 30 sorted AB5 READY symbols with no census episode and a proved tape baseline",
            "randomness_used": False}, "requested_census_episode_rows": len(ready_priors),
            "unique_census_symbol_count": len(census_cohort), "control_symbol_count": len(controls),
            "census_symbols": list(census_cohort), "control_symbols": list(controls), "control_baseline_evidence": control_evidence}
        summary = {"schema": "signal-base-rate-summary-v1", "question": "when at least three components fire, how often does a census pump arrive within 72 hours",
                   "parameters": {"as_of": census["header"]["as_of"], "days": DAY_COUNT, "window_minutes": 60,
                                  "step_hours": STEP_HOURS, "match_hours": MATCH_HOURS,
                                  "rise_exclusion": "any window overlapping base through peak"},
                   "population": {key: population[key] for key in ("requested_census_episode_rows", "unique_census_symbol_count", "control_symbol_count")},
                   "groups": groups}
        _write(args.output_dir / "signal-windows.jsonl", rows, lines=True)
        _write(args.output_dir / "population.json", population)
        _write(args.output_dir / "signal-base-rate.json", summary)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, StudyError, absorption.AbsorptionError, baselines.BaselineError) as error:
        print(f"signal base rate: {error}", file=sys.stderr); return 2
    print(f"AB9 | windows={len(rows)} | census_symbols={len(census_cohort)} | controls={len(controls)} | output={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
