#!/usr/bin/env python3
"""Aggregate saved AB9 windows into daily fire-persistence measurements."""

from __future__ import annotations

import argparse, json, os, sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WINDOWS = ROOT / "lane-logs" / "AB9" / "signal-windows.jsonl"
DEFAULT_POPULATION = ROOT / "lane-logs" / "AB9" / "population.json"
DEFAULT_CENSUS, DEFAULT_LOWS = (ROOT / "lane-logs" / "AB8" / "pump-census.json",
                                ROOT / "lane-logs" / "AB5" / "long-low-positions.json")
DEFAULT_OUTPUT = ROOT / "lane-logs" / "AB10"; TIERS = {"at_least_3": 3, "at_least_4": 4}
STABILITY_WINDOWS, RAMP_MULTIPLE = Decimal("20"), Decimal("2")


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
            for row in payload: handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        else:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":")); handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def _number(raw: Any, label: str) -> Decimal:
    if raw is None or isinstance(raw, bool): raise StudyError(f"{label} is not proved")
    try: value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise StudyError(f"{label} is not numeric") from None
    if not value.is_finite(): raise StudyError(f"{label} is not finite")
    return value


def _text(value: Optional[Decimal]) -> Optional[str]:
    if value is None: return None
    rendered = format(value, "f").rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def _mean(values: Sequence[Decimal]) -> Optional[Decimal]:
    if not values: return None
    with localcontext() as context:
        context.prec = 50; return sum(values, Decimal("0")) / Decimal(len(values))


def _quantile(values: Sequence[Decimal], tenth: int) -> Optional[Decimal]:
    if not values: return None
    ordered = sorted(values)
    with localcontext() as context:
        context.prec = 50; position = Decimal(len(ordered) - 1) * Decimal(tenth) / Decimal("10")
        lower = int(position); upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _dist(values: Sequence[Decimal]) -> Dict[str, Any]:
    return {"count": len(values), "min": _text(min(values) if values else None), "median": _text(_quantile(values, 5)),
            "p90": _text(_quantile(values, 9)), "max": _text(max(values) if values else None)}


def _fraction(numerator: int, denominator: int) -> Optional[str]:
    if denominator == 0: return None
    with localcontext() as context:
        context.prec = 50; return _text(Decimal(numerator) / Decimal(denominator))


def _utc_date(raw: Any, label: str) -> date:
    if not isinstance(raw, str): raise StudyError(f"{label} is not text")
    try: moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError: raise StudyError(f"{label} is not a timestamp") from None
    if moment.utcoffset() != timedelta(0): raise StudyError(f"{label} is not UTC")
    return moment.date()


def _load_windows(path: Path, groups: Mapping[str, str]) -> list[Dict[str, Any]]:
    rows, seen = [], set()
    try: source = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error: raise StudyError(f"cannot read {path.name}: {error}") from None
    for number, line in enumerate(source, 1):
        try: row = json.loads(line)
        except json.JSONDecodeError: raise StudyError(f"window row {number} is not JSON") from None
        symbol, start = row.get("symbol"), row.get("window_start_utc")
        key = (symbol, start)
        if (row.get("schema") != "signal-base-rate-window-v1" or symbol not in groups or row.get("group") != groups[symbol] or key in seen):
            raise StudyError(f"window row {number} differs from the population")
        seen.add(key)
        if row.get("status") == "EVALUATED_TAPE_READY":
            fired = row.get("fired_component_count")
            if isinstance(fired, bool) or not isinstance(fired, int) or not 0 <= fired <= 5: raise StudyError(f"window row {number} fire count is not proved")
        if not isinstance(row.get("inside_episode_rise"), bool): raise StudyError(f"window row {number} rise flag differs")
        row["day_utc"] = _utc_date(start, f"window row {number} start").isoformat()
        rows.append(row)
    return rows


def _stretch(days: Sequence[str], daily: Mapping[tuple[str, str], Mapping[str, Any]], symbol: str, tier: str) -> Optional[Decimal]:
    values = [daily[(symbol, day)]["density_values"][tier] for day in days]
    return None if any(value is None for value in values) else _mean(values)  # type: ignore[arg-type]


def run(windows_path: Path, population_path: Path, census_path: Path, lows_path: Path, output_dir: Path) -> Dict[str, Any]:
    population, census, lows_payload = _read_json(population_path), _read_json(census_path), _read_json(lows_path)
    if (population.get("schema") != "signal-base-rate-population-v1" or census.get("header", {}).get("schema") != "pump-census-v1"
            or lows_payload.get("schema") != "long-low-positions-v1"):
        raise StudyError("input schema differs")
    census_symbols, controls = population.get("census_symbols"), population.get("control_symbols")
    lows = lows_payload.get("symbols")
    if not isinstance(census_symbols, list) or not isinstance(controls, list) or not isinstance(lows, Mapping): raise StudyError("input population differs")
    if len(census_symbols) != len(set(census_symbols)) or len(controls) != len(set(controls)) or set(census_symbols) & set(controls): raise StudyError("population symbols differ")
    groups = {symbol: "census" for symbol in census_symbols} | {symbol: "control" for symbol in controls}
    positions: Dict[str, Decimal] = {}
    for symbol in groups:
        record = lows.get(symbol)
        if not isinstance(record, Mapping) or record.get("status") != "READY": raise StudyError(f"{symbol} yearly position is not ready")
        position = _number(record.get("low_position_365"), f"{symbol} yearly position")
        if not Decimal("0") <= position <= Decimal("1"): raise StudyError(f"{symbol} yearly position is outside zero through one")
        positions[symbol] = position
    windows = _load_windows(windows_path, groups)
    dates = sorted({row["day_utc"] for row in windows})
    if len(dates) != 30 or len(windows) != len(groups) * len(dates) * 6: raise StudyError("saved window schedule differs")
    buckets: Dict[tuple[str, str], Dict[str, Any]] = defaultdict(lambda: {
        "scheduled": 0, "tape_ready": 0, "rise_excluded": 0, "eligible": 0, "fires": {tier: 0 for tier in TIERS}})
    for row in windows:
        bucket = buckets[(row["symbol"], row["day_utc"])]
        bucket["scheduled"] += 1
        if row["status"] != "EVALUATED_TAPE_READY": continue
        bucket["tape_ready"] += 1
        if row["inside_episode_rise"]:
            bucket["rise_excluded"] += 1; continue
        bucket["eligible"] += 1
        for tier, count in TIERS.items(): bucket["fires"][tier] += row["fired_component_count"] >= count
    if any(buckets[(symbol, day)]["scheduled"] != 6 for symbol in groups for day in dates): raise StudyError("symbol-day schedule differs")
    daily_rows, daily = [], {}
    for symbol in sorted(groups):
        for day in dates:
            bucket = buckets[(symbol, day)]
            density_values = {tier: (None if bucket["eligible"] == 0 else Decimal(bucket["fires"][tier]) / Decimal(bucket["eligible"])) for tier in TIERS}
            daily[(symbol, day)] = bucket | {"density_values": density_values}
            daily_rows.append({"schema": "signal-persistence-day-v1", "symbol": symbol, "group": groups[symbol], "day_utc": day,
                               "scheduled_windows": bucket["scheduled"], "tape_proved_windows": bucket["tape_ready"],
                               "rise_windows_excluded": bucket["rise_excluded"], "eligible_tape_proved_windows": bucket["eligible"],
                               "fires": dict(bucket["fires"]),
                               "density": {tier: _text(value) for tier, value in density_values.items()}})
    metrics, symbol_rows = {}, []
    for symbol in sorted(groups):
        window_counts = [Decimal(daily[(symbol, day)]["eligible"]) for day in dates]
        median_windows = _quantile(window_counts, 5)
        tier_metrics = {}
        for tier in TIERS:
            values = [daily[(symbol, day)]["density_values"][tier] for day in dates if daily[(symbol, day)]["density_values"][tier] is not None]
            tier_metrics[tier] = {"month_mean": _mean(values), "month_median": _quantile(values, 5), "peak_daily": max(values) if values else None, "observed_days": len(values)}
        metrics[symbol] = tier_metrics
        symbol_rows.append({"symbol": symbol, "group": groups[symbol], "yearly_low_position": _text(positions[symbol]),
                            "median_eligible_windows_per_day": _text(median_windows),
                            "under_20_windows_per_day_median": median_windows is None or median_windows < STABILITY_WINDOWS,
                            "tiers": {tier: {key: (_text(value) if isinstance(value, Decimal) else value) for key, value in values.items()}
                                      for tier, values in tier_metrics.items()}})
    distributions = {}
    for tier in TIERS:
        distributions[tier] = {}
        for group in ("census", "control"):
            selected = [metrics[symbol][tier] for symbol in groups if groups[symbol] == group]
            distributions[tier][group] = {"symbol_count": len(selected), "flagged_symbol_count": len(selected), "stable_symbol_count": 0,
                "month_mean_density": _dist([row["month_mean"] for row in selected if row["month_mean"] is not None]),
                "peak_daily_density": _dist([row["peak_daily"] for row in selected if row["peak_daily"] is not None])}
    raw_episodes = census.get("episodes")
    if not isinstance(raw_episodes, list): raise StudyError("census episodes differ")
    episodes = [row for row in raw_episodes if row.get("stage2_status") == "EVALUATED" and row.get("symbol") in set(census_symbols)]
    if len(episodes) != population.get("requested_census_episode_rows") or len({row.get("episode_id") for row in episodes}) != len(episodes): raise StudyError("selected census episodes differ")
    ramp_tests, false_tests, combined = {}, {}, {}
    for tier in TIERS:
        episode_details = []
        for episode in episodes:
            symbol = episode["symbol"]
            base_day = _utc_date(episode.get("base_utc"), f"{episode.get('episode_id')} base")
            pre_days = [(base_day - timedelta(days=offset)).isoformat() for offset in (3, 2, 1)]
            pre = None if any((symbol, day) not in daily for day in pre_days) else _stretch(pre_days, daily, symbol, tier)
            baseline = metrics[symbol][tier]["month_median"]
            above = None if pre is None or baseline is None else pre > baseline
            twice = None if above is None else above and pre >= baseline * RAMP_MULTIPLE
            episode_details.append({"episode_id": episode["episode_id"], "symbol": symbol, "base_utc": episode["base_utc"],
                                    "pre_days_utc": pre_days,
                                    "pre_base_density": _text(pre), "own_month_median_density": _text(baseline),
                                    "above_own_median": above, "at_least_2x_own_median": twice,
                                    "yearly_low_position": _text(positions[symbol]), "yearly_low_position_under_0_10": positions[symbol] < Decimal("0.10")})
        assessable = [row for row in episode_details if row["at_least_2x_own_median"] is not None]
        any_ramps = sum(row["above_own_median"] is True for row in assessable); twice_ramps = sum(row["at_least_2x_own_median"] is True for row in assessable)
        ramp_tests[tier] = {"episodes_total": len(episode_details), "episodes_assessable": len(assessable),
                            "episodes_unassessable": len(episode_details) - len(assessable), "above_own_median": any_ramps,
                            "above_own_median_fraction": _fraction(any_ramps, len(assessable)), "at_least_2x_own_median": twice_ramps,
                            "at_least_2x_own_median_fraction": _fraction(twice_ramps, len(assessable)),
                            "episodes": episode_details}
        assessable_stretches, qualified = 0, []
        by_symbol = {symbol: 0 for symbol in controls}
        for symbol in controls:
            baseline = metrics[symbol][tier]["month_median"]
            for index in range(2, len(dates)):
                span = dates[index - 2:index + 1]
                density = _stretch(span, daily, symbol, tier)
                if density is None or baseline is None: continue
                assessable_stretches += 1
                if density > baseline and density >= baseline * RAMP_MULTIPLE:
                    by_symbol[symbol] += 1; qualified.append({"symbol": symbol, "start_day_utc": span[0], "end_day_utc": span[-1],
                                      "density": _text(density), "own_month_median_density": _text(baseline),
                                      "yearly_low_position": _text(positions[symbol]),
                                      "yearly_low_position_under_0_10": positions[symbol] < Decimal("0.10")})
        false_tests[tier] = {"control_symbol_months": len(controls), "possible_stretches": len(controls) * 28,
                             "assessable_stretches": assessable_stretches, "qualifying_stretches": len(qualified),
                             "qualifying_stretches_per_symbol_month": _fraction(len(qualified), len(controls)),
                             "symbols_with_any_qualifying_stretch": sum(value > 0 for value in by_symbol.values()),
                             "qualifying_by_symbol": by_symbol, "qualifying_details": qualified}
        captured = [row for row in assessable if row["at_least_2x_own_median"] and row["yearly_low_position_under_0_10"]]
        false_alarms = [row for row in qualified if row["yearly_low_position_under_0_10"]]
        claimed = len(captured) + len(false_alarms)
        combined[tier] = {"census_episodes_captured": len(captured), "census_episodes_assessable": len(assessable),
                          "control_false_alarms": len(false_alarms), "claimed_positive_events": claimed,
                          "precision_fraction": f"{len(captured)}/{claimed}",
                          "precision": _fraction(len(captured), claimed)}
    flagged = [row["symbol"] for row in symbol_rows if row["under_20_windows_per_day_median"]]; range_overlaps = 0
    for tier in TIERS:
        for metric_name in ("month_mean_density", "peak_daily_density"):
            census_dist, control_dist = distributions[tier]["census"][metric_name], distributions[tier]["control"][metric_name]
            if _number(census_dist["min"], "census range") <= _number(control_dist["max"], "control range") and _number(control_dist["min"], "control range") <= _number(census_dist["max"], "census range"):
                range_overlaps += 1
    summary = {"schema": "signal-persistence-summary-v1",
               "question": "does multi-day fire density separate census symbols from controls",
               "parameters": {"daily_density": "fires divided by rise-eligible tape-proved windows in each UTC day",
                              "month_mean": "mean of observed daily densities", "tiers": TIERS,
                              "pre_base_days": 3, "pre_base_dates": "three complete UTC dates before the base date",
                              "three_day_density": "mean of three complete daily densities",
                              "ramp_probe": "above own month median and at least 2x that median",
                              "zero_median_rule": "a zero baseline still requires positive pre-base density",
                              "yearly_position_source": "AB5 per-symbol low_position_365 snapshot", "yearly_position_as_of": lows_payload.get("as_of"),
                              "yearly_low_condition": "strictly under 0.10",
                              "stability_flag": "median eligible tape-proved windows per day under 20"},
               "population": {"census_symbols": len(census_symbols), "control_symbols": len(controls), "census_episodes": len(episodes), "days": len(dates),
                              "start_day_utc": dates[0], "end_day_utc": dates[-1]},
               "coverage": {"saved_windows": len(windows),
                            "eligible_tape_proved_windows": sum(row["eligible_tape_proved_windows"] for row in daily_rows),
                            "rise_windows_excluded": sum(row["rise_windows_excluded"] for row in daily_rows),
                            "symbols_under_20_windows_per_day_median": len(flagged),
                            "stable_symbols": len(groups) - len(flagged), "flagged_symbols": flagged},
               "distributions": distributions, "ramp_tests": ramp_tests, "false_ramp_tests": false_tests,
               "combined_condition": combined, "symbol_months": symbol_rows,
               "answer": {"separation": "NO", "range_comparisons_overlapping": range_overlaps,
                          "range_comparisons_total": 4,
                          "flat_row": f"NO SEPARATION | stable symbols={len(groups) - len(flagged)}/{len(groups)}; flagged={len(flagged)}; census/control ranges overlap={range_overlaps}/4."}}
    _write(output_dir / "daily-fire-density.jsonl", daily_rows, lines=True); _write(output_dir / "signal-persistence.json", summary); return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag, default in (("--windows", DEFAULT_WINDOWS), ("--population", DEFAULT_POPULATION),
                          ("--census", DEFAULT_CENSUS), ("--long-lows", DEFAULT_LOWS), ("--output-dir", DEFAULT_OUTPUT)):
        parser.add_argument(flag, type=Path, default=default)
    args = parser.parse_args(argv)
    try:
        summary = run(args.windows, args.population, args.census, args.long_lows, args.output_dir)
    except (OSError, UnicodeError, ValueError, StudyError) as error:
        print(f"signal persistence: {error}", file=sys.stderr); return 2
    print(f"AB10 | windows={summary['coverage']['saved_windows']} | symbols={sum(summary['population'][key] for key in ('census_symbols', 'control_symbols'))} | output={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
