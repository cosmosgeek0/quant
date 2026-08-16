#!/usr/bin/env python3
"""Compare fresh-listing and veteran pump episodes from saved evidence."""

from __future__ import annotations

import argparse, json, math, os, sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

try:
    from study import absorption
except ModuleNotFoundError as error:
    if error.name != "study": raise
    import absorption  # type: ignore


ROOT = Path(__file__).resolve().parents[1]; DEFAULT_CENSUS = ROOT / "lane-logs/AB13/pump-census-6mo.json"
DEFAULT_LOWS = ROOT / "lane-logs/AB5/long-low-positions.json"; DEFAULT_HISTORY = ROOT / "study/data-1m-history"
DEFAULT_RECENT = ROOT / "study/data"; DEFAULT_OUTPUT = ROOT / "lane-logs/AB16"
CLASSES, MINUTE_MS, DAY_MS = ("fresh", "veteran"), 60_000, 86_400_000


class StudyError(Exception): pass


def _read(path: Path) -> Any:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StudyError(f"cannot read {path}: {error}") from None


def _decimal(raw: Any, label: str) -> Decimal:
    if raw is None or isinstance(raw, bool): raise StudyError(f"{label} is not proved")
    try: value = Decimal(str(raw))
    except (InvalidOperation, ValueError): raise StudyError(f"{label} is not numeric") from None
    if not value.is_finite(): raise StudyError(f"{label} is not finite")
    return value


def _text(value: Optional[Decimal]) -> str:
    if value is None: return "NA"
    rendered = format(value, ".8g")
    return "0" if rendered in {"-0", ""} else rendered


def _quantile(values: Sequence[Decimal], numerator: int, denominator: int = 10) -> Optional[Decimal]:
    if not values: return None
    ordered = sorted(values)
    with localcontext() as context:
        context.prec = 50; position = Decimal(len(ordered) - 1) * numerator / denominator
        lower = int(position); upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _dist(values: Sequence[Decimal]) -> Mapping[str, Any]:
    return {"n": len(values), "min": min(values) if values else None, "p10": _quantile(values, 1),
            "median": _quantile(values, 5), "p90": _quantile(values, 9), "max": max(values) if values else None}


def _rate(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0: raise StudyError("rate denominator is not positive")
    with localcontext() as context: context.prec = 50; return Decimal(numerator) / denominator


def _score_p(left: int, left_n: int, right: int, right_n: int) -> Decimal:
    pooled = _rate(left + right, left_n + right_n)
    variance = pooled * (Decimal("1") - pooled) * (Decimal("1") / left_n + Decimal("1") / right_n)
    if variance == 0: return Decimal("1") if left * right_n == right * left_n else Decimal("0")
    z = float((_rate(left, left_n) - _rate(right, right_n)) / variance.sqrt())
    return Decimal(str(math.erfc(abs(z) / math.sqrt(2))))


def _auc(left: Sequence[Decimal], right: Sequence[Decimal]) -> Optional[Decimal]:
    if not left or not right: return None
    ordered = sorted([(value, 1) for value in left] + [(value, 0) for value in right])
    rank_sum, index = Decimal("0"), 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][0] == ordered[index][0]: end += 1
        rank = Decimal(index + 1 + end) / 2
        rank_sum += rank * sum(label for _value, label in ordered[index:end]); index = end
    return (rank_sum - Decimal(len(left) * (len(left) + 1)) / 2) / Decimal(len(left) * len(right))


def _utc_ms(raw: Any, label: str) -> int:
    if not isinstance(raw, str): raise StudyError(f"{label} is not text")
    try: moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError: raise StudyError(f"{label} is not a timestamp") from None
    if moment.utcoffset() != timedelta(0): raise StudyError(f"{label} is not UTC")
    return int(moment.timestamp() * 1000)


def _validate(census: Any, lows: Any) -> list[dict[str, Any]]:
    if not isinstance(census, Mapping) or census.get("header", {}).get("schema") != "pump-census-6mo-v1": raise StudyError("census schema differs")
    if not isinstance(lows, Mapping) or lows.get("schema") != "long-low-positions-v1": raise StudyError("long-low schema differs")
    rows, symbols = census.get("episodes"), lows.get("symbols")
    if not isinstance(rows, list) or not rows or not isinstance(symbols, Mapping): raise StudyError("input population differs")
    if census.get("summary", {}).get("episodes") != len(rows) or lows.get("symbol_count") != len(symbols): raise StudyError("declared input count differs")
    as_of = date.fromisoformat(lows["as_of"]); seen = set()
    for raw in rows:
        if not isinstance(raw, dict): raise StudyError("episode row differs")
        identity, symbol, group, outcome = raw.get("episode_id"), raw.get("symbol"), raw.get("listing_age_class"), raw.get("class")
        if not isinstance(identity, str) or identity in seen or symbol not in symbols: raise StudyError("episode identity differs")
        record, days = symbols[symbol], raw.get("listing_age_days")
        if not isinstance(record, Mapping) or record.get("latest_day") != lows["as_of"]: raise StudyError(f"{identity} long-low date differs")
        if group not in CLASSES or outcome not in {"HELD", "COLLAPSED"}: raise StudyError(f"{identity} class differs")
        if isinstance(days, bool) or not isinstance(days, int) or days != record.get("days_of_history"): raise StudyError(f"{identity} history age differs")
        expected = "veteran" if days >= absorption.VETERAN_HISTORY_DAYS else "fresh"
        if group != expected: raise StudyError(f"{identity} listing class differs")
        for field in ("base_price", "peak_price", "rise_pct", "hours_to_peak", "volume_multiple", "following_24h_low"):
            if _decimal(raw.get(field), f"{identity} {field}") <= 0: raise StudyError(f"{identity} {field} is not positive")
        episode_day = date.fromisoformat(raw["date_utc"]); age = days - (as_of - episode_day).days
        if age < 1: raise StudyError(f"{identity} episode age is not proved")
        raw["_episode_age_days"], raw["_age_censored"] = Decimal(age), days >= 365
        raw["_peak_ms"] = _utc_ms(raw.get("peak_utc"), f"{identity} peak")
        seen.add(identity)
    for group in CLASSES:
        if census["summary"].get(group) != sum(row["listing_age_class"] == group for row in rows): raise StudyError(f"{group} count differs")
    return rows


def _venue_root(root: Path, venue: str) -> Path:
    if venue not in {"spot", "futures"}: raise StudyError("episode venue differs")
    return root / ("spot" if venue == "spot" else "futures/um")


def _collapse(rows: Sequence[dict[str, Any]], history: Path, recent: Path) -> Mapping[str, Any]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows: groups[(row["venue"], row["symbol"])].append(row)
    coverage = {group: {"complete_followups": 0, "missing_followups": 0, "collapsed_measured": 0} for group in CLASSES}
    archives = parsed = 0
    for ordinal, ((venue, symbol), episodes) in enumerate(sorted(groups.items()), 1):
        needed, months, days = set(), set(), set()
        for row in episodes:
            peak = row["_peak_ms"]; needed.update(range(peak + MINUTE_MS, peak + DAY_MS + MINUTE_MS, MINUTE_MS))
            start = datetime.fromtimestamp(peak / 1000, timezone.utc); end = start + timedelta(days=1)
            months.update((start.strftime("%Y-%m"), end.strftime("%Y-%m"))); days.update((start.date().isoformat(), end.date().isoformat()))
        paths = set()
        for root in (history, recent):
            base = _venue_root(root, venue)
            paths.update(base / "monthly/klines" / symbol / "1m" / f"{symbol}-1m-{month}.zip" for month in months)
            paths.update(base / "daily/klines" / symbol / "1m" / f"{symbol}-1m-{day}.zip" for day in days)
        bars = {}
        for path in sorted(path for path in paths if path.is_file()):
            archives += 1
            for bar in absorption._kline_rows(path, symbol):
                parsed += 1
                if bar.open_ms not in needed: continue
                prior = bars.get(bar.open_ms)
                if prior is not None and prior != bar: raise StudyError(f"{symbol} kline evidence conflicts")
                bars[bar.open_ms] = bar
        for row in episodes:
            group, peak = row["listing_age_class"], row["_peak_ms"]
            stamps = range(peak + MINUTE_MS, peak + DAY_MS + MINUTE_MS, MINUTE_MS)
            follow = [bars.get(stamp) for stamp in stamps]
            if any(bar is None for bar in follow): coverage[group]["missing_followups"] += 1; row["_collapse_hours"] = None; continue
            proved = [bar for bar in follow if bar is not None]; coverage[group]["complete_followups"] += 1
            if min(bar.low for bar in proved) != _decimal(row["following_24h_low"], f"{row['episode_id']} follow low"): raise StudyError(f"{row['episode_id']} follow low differs")
            midpoint = (_decimal(row["base_price"], "base") + _decimal(row["peak_price"], "peak")) / 2
            crossing = next((bar.open_ms for bar in proved if bar.low <= midpoint), None)
            observed = "HELD" if crossing is None else "COLLAPSED"
            if observed != row["class"]: raise StudyError(f"{row['episode_id']} outcome differs")
            row["_collapse_hours"] = None if crossing is None else Decimal(crossing - peak) / Decimal("3600000")
            if crossing is not None: coverage[group]["collapsed_measured"] += 1
        if ordinal % 50 == 0 or ordinal == len(groups): print(f"fresh listing progress | symbols={ordinal}/{len(groups)}", flush=True)
    return {"coverage": coverage, "archives_read": archives, "bars_parsed": parsed,
            "history_present": history.is_dir(), "recent_data_present": recent.is_dir()}


def _serial(value: Any) -> Any:
    if isinstance(value, Decimal): return absorption.decimal_text(value)
    if isinstance(value, Mapping): return {key: _serial(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)): return [_serial(item) for item in value]
    return value


def _write(path: Path, payload: Any, text_mode: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(payload if text_mode else json.dumps(_serial(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def run(census_path: Path, lows_path: Path, history: Path, recent: Path, output: Path) -> Mapping[str, Any]:
    census, lows = _read(census_path), _read(lows_path); rows = _validate(census, lows); scan = _collapse(rows, history, recent)
    grouped = {group: [row for row in rows if row["listing_age_class"] == group] for group in CLASSES}
    fields = {"rise_pct": "rise_pct", "time_to_peak_hours": "hours_to_peak", "volume_multiple": "volume_multiple",
              "days_since_listing_at_episode": "_episode_age_days", "collapse_speed_hours": "_collapse_hours"}
    metrics = {}
    for name, field in fields.items():
        values = {group: [_decimal(row[field], f"{name} {group}") for row in grouped[group] if row.get(field) is not None] for group in CLASSES}
        metrics[name] = {group: _dist(values[group]) for group in CLASSES} | {"auc_fresh_gt_veteran": _auc(values["fresh"], values["veteran"])}
    held = {group: {"held": sum(row["class"] == "HELD" for row in grouped[group]), "episodes": len(grouped[group])} for group in CLASSES}
    for group in CLASSES: held[group]["rate"] = _rate(held[group]["held"], held[group]["episodes"])
    held_diff = (held["fresh"]["rate"] - held["veteran"]["rate"]) * 100
    held.update({"difference_percentage_points": held_diff, "two_sided_score_p": _score_p(held["fresh"]["held"], held["fresh"]["episodes"], held["veteran"]["held"], held["veteran"]["episodes"])})
    counts = Counter(row["symbol"] for row in rows); repeat = {}
    for group in CLASSES:
        symbols = [name for name, record in lows["symbols"].items() if ("veteran" if record["days_of_history"] >= absorption.VETERAN_HISTORY_DAYS else "fresh") == group]
        values = [Decimal(counts[name]) for name in symbols]; repeats = sum(value >= 2 for value in values); any_episode = sum(value > 0 for value in values)
        repeat[group] = {"cohort_symbols": len(symbols), "symbols_with_episode": any_episode,
                         "repeat_symbols": repeats, "repeat_rate": _rate(repeats, len(symbols)), "repeat_rate_among_episode_symbols": _rate(repeats, any_episode), "episodes_per_symbol": _dist(values)}
    repeat_diff = (repeat["fresh"]["repeat_rate"] - repeat["veteran"]["repeat_rate"]) * 100
    repeat.update({"difference_percentage_points": repeat_diff, "two_sided_score_p": _score_p(repeat["fresh"]["repeat_symbols"], repeat["fresh"]["cohort_symbols"], repeat["veteran"]["repeat_symbols"], repeat["veteran"]["cohort_symbols"])})
    material = [name for name in ("rise_pct", "time_to_peak_hours", "volume_multiple", "collapse_speed_hours") if metrics[name]["auc_fresh_gt_veteran"] is not None and abs(metrics[name]["auc_fresh_gt_veteran"] - Decimal("0.5")) >= Decimal("0.10")]
    answer = abs(held_diff) >= 10 and held["two_sided_score_p"] < Decimal("0.05") and bool(material)
    result = {"schema": "fresh-listing-study-v1", "question": "are fresh listings a measurably different pump class",
              "definitions": {"fresh": "AB5 history under 90 observed days as of 2026-08-14", "collapse_speed": "hours from peak to first low at or below the base-to-peak midpoint within a complete following 24h", "repeat_rate": "cohort symbols with at least two census episodes divided by all AB5 symbols in class", "age": "AB5 observed-history count minus calendar days from episode date to as-of; values from 365-day histories are right-censored lower bounds", "auc": "probability a fresh value exceeds a veteran value, with ties split"},
              "population": {group: {"episodes": len(grouped[group]), "episode_symbols": len({row["symbol"] for row in grouped[group]}), "episode_age_under_90": sum(row["_episode_age_days"] < 90 for row in grouped[group]), "age_right_censored_episodes": sum(row["_age_censored"] for row in grouped[group])} for group in CLASSES},
              "held_rate": held, "metrics": metrics, "repeat_pump": repeat, "kline_scan": scan,
              "detector_answer": {"one_class_blind_detector_measurably_wrong": answer, "material_shape_metrics": material,
                 "rule": "YES when HELD rates differ by at least 10 points with score p below 0.05 and any shape AUC is at most 0.40 or at least 0.60"},
              "limits": "observational class comparison; AB13 classes use as-of history; non-pump controls and actual detector errors were not measured"}
    def stat(name: str, group: str) -> str:
        row = metrics[name][group]; return f"{group} n={row['n']} min/p10/med/p90/max=" + "/".join(_text(row[key]) for key in ("min", "p10", "median", "p90", "max"))
    lines = [f"scope | fresh episodes={len(grouped['fresh'])} symbols={len({r['symbol'] for r in grouped['fresh']})}; veteran episodes={len(grouped['veteran'])} symbols={len({r['symbol'] for r in grouped['veteran']})}; archives={scan['archives_read']} bars={scan['bars_parsed']} | MEASURED",
             f"HELD rate | fresh={held['fresh']['held']}/{held['fresh']['episodes']} {_text(held['fresh']['rate'] * 100)}%; veteran={held['veteran']['held']}/{held['veteran']['episodes']} {_text(held['veteran']['rate'] * 100)}%; difference={_text(held_diff)} points; score p={_text(held['two_sided_score_p'])} | MEASURED"]
    for name in ("rise_pct", "time_to_peak_hours", "collapse_speed_hours", "volume_multiple", "days_since_listing_at_episode"):
        extra = f"; coverage={scan['coverage']}" if name == "collapse_speed_hours" else (f"; under90-at-episode fresh/veteran={result['population']['fresh']['episode_age_under_90']}/{result['population']['veteran']['episode_age_under_90']}; right-censored={result['population']['fresh']['age_right_censored_episodes']}/{result['population']['veteran']['age_right_censored_episodes']}" if name.startswith("days_") else "")
        lines.append(f"{name} | {stat(name, 'fresh')} | {stat(name, 'veteran')} | AUC fresh>veteran={_text(metrics[name]['auc_fresh_gt_veteran'])}{extra} | MEASURED")
    lines.append(f"repeat pumps | fresh cohort repeat={repeat['fresh']['repeat_symbols']}/{repeat['fresh']['cohort_symbols']} {_text(repeat['fresh']['repeat_rate'] * 100)}%, among-pumped={_text(repeat['fresh']['repeat_rate_among_episode_symbols'] * 100)}%, episodes/symbol min/p10/med/p90/max=" + "/".join(_text(repeat['fresh']['episodes_per_symbol'][key]) for key in ("min", "p10", "median", "p90", "max")) + f"; veteran repeat={repeat['veteran']['repeat_symbols']}/{repeat['veteran']['cohort_symbols']} {_text(repeat['veteran']['repeat_rate'] * 100)}%, among-pumped={_text(repeat['veteran']['repeat_rate_among_episode_symbols'] * 100)}%, episodes/symbol min/p10/med/p90/max=" + "/".join(_text(repeat['veteran']['episodes_per_symbol'][key]) for key in ("min", "p10", "median", "p90", "max")) + f"; difference={_text(repeat_diff)} points; score p={_text(repeat['two_sided_score_p'])} | MEASURED")
    lines.append(f"detector answer | {'YES' if answer else 'NO'} under stated class-blind rule; HELD difference={_text(held_diff)} points p={_text(held['two_sided_score_p'])}; material shape metrics={','.join(material) if material else 'none'} | MEASURED")
    lines.append(f"limits | AB13 classes use as-of history; recent study/data present={'yes' if scan['recent_data_present'] else 'no'}; missing followups fresh/veteran={scan['coverage']['fresh']['missing_followups']}/{scan['coverage']['veteran']['missing_followups']}; non-pump controls and actual detector errors not measured | MEASURED")
    _write(output / "fresh-listing-summary.json", result); _write(output / "fresh-listing-distributions.txt", "\n".join(lines) + "\n", True)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--census", type=Path, default=DEFAULT_CENSUS); parser.add_argument("--long-lows", type=Path, default=DEFAULT_LOWS)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY); parser.add_argument("--recent-data", type=Path, default=DEFAULT_RECENT); parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT); args = parser.parse_args(argv)
    try: result = run(args.census, args.long_lows, args.history, args.recent_data, args.output_dir)
    except (StudyError, absorption.AbsorptionError, OSError, UnicodeError, TypeError, ValueError) as error:
        print(f"fresh listing study: {error}", file=sys.stderr); return 2
    print(f"fresh listing | fresh={result['population']['fresh']['episodes']} | veteran={result['population']['veteran']['episodes']} | output={args.output_dir}"); return 0


if __name__ == "__main__": raise SystemExit(main())
