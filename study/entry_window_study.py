#!/usr/bin/env python3
"""Measure offline entry-window timing from AB15 episode profiles."""

from __future__ import annotations

import argparse, json, os, sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

try:
    from study import absorption, tape_pull
except ModuleNotFoundError as error:
    if error.name != "study": raise
    import absorption, tape_pull  # type: ignore


ROOT = Path(__file__).resolve().parents[1]; DEFAULT_CENSUS = ROOT / "lane-logs/AB13/pump-census-6mo.json"
DEFAULT_PROFILES = ROOT / "lane-logs/AB15/post-base-profiles.jsonl"; DEFAULT_HISTORY = ROOT / "study/data-1m-history"
DEFAULT_RECENT = ROOT / "study/data"; DEFAULT_OUTPUT = ROOT / "lane-logs/AB17"
CLASSES, CUTOFFS = ("HELD", "COLLAPSED"), (15, 30, 60, 120)
MINUTE_MS, DAY_MS = 60_000, 86_400_000
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class StudyError(Exception): pass


def _decimal(raw: Any, label: str, *, positive: bool = False) -> Decimal:
    if raw is None or isinstance(raw, (bool, float)): raise StudyError(f"{label} is not Decimal text")
    try: value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError): raise StudyError(f"{label} is not Decimal text") from None
    if not value.is_finite() or value < 0 or (positive and value <= 0): raise StudyError(f"{label} is outside its Decimal range")
    return value


def _minutes(raw: Any, label: str) -> Optional[int]:
    if raw is None: return None
    value = _decimal(raw, label)
    if value != value.to_integral_value() or value > 1_440: raise StudyError(f"{label} is not a whole minute")
    return int(value)


def _utc_ms(raw: Any, label: str) -> int:
    if not isinstance(raw, str): raise StudyError(f"{label} is not canonical UTC text")
    try: moment = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError: raise StudyError(f"{label} is not canonical UTC text") from None
    delta = moment - EPOCH
    value = (delta.days * 86_400 + delta.seconds) * 1_000 + delta.microseconds // 1_000
    if (EPOCH + timedelta(milliseconds=value)).isoformat(timespec="milliseconds").replace("+00:00", "Z") != raw: raise StudyError(f"{label} is not canonical UTC text")
    return value


def _read_json(path: Path) -> Any:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error: raise StudyError(f"cannot read {path}: {error}") from None


def _load_census(path: Path) -> dict[str, dict[str, Any]]:
    payload = _read_json(path)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("header"), Mapping) or payload["header"].get("schema") != "pump-census-6mo-v1": raise StudyError("census schema differs")
    raw_rows = payload.get("episodes")
    summary = payload.get("summary")
    if not isinstance(raw_rows, list) or not raw_rows or not isinstance(summary, Mapping) or summary.get("episodes") != len(raw_rows): raise StudyError("census population differs")
    rows: dict[str, dict[str, Any]] = {}
    for raw in raw_rows:
        if not isinstance(raw, Mapping): raise StudyError("census episode row differs")
        identity, label = raw.get("episode_id"), raw.get("class")
        if not isinstance(identity, str) or not identity or identity in rows or label not in CLASSES: raise StudyError("census episode identity differs")
        try:
            symbol = tape_pull._validate_symbol(raw.get("symbol")); venue = tape_pull._validate_venue(raw.get("venue"))
        except tape_pull.TapePullError as error: raise StudyError(f"{identity} identity differs: {error}") from None
        base, peak = _utc_ms(raw.get("base_utc"), f"{identity} base"), _utc_ms(raw.get("peak_utc"), f"{identity} peak")
        if peak < base or peak > base + DAY_MS or (peak - base) % MINUTE_MS: raise StudyError(f"{identity} time span differs")
        rows[identity] = {"episode_id": identity, "class": label, "symbol": symbol, "venue": venue,
                          "base_utc": raw["base_utc"], "peak_utc": raw["peak_utc"], "base_ms": base, "peak_ms": peak,
                          "base_price": _decimal(raw.get("base_price"), f"{identity} base price", positive=True),
                          "peak_price": _decimal(raw.get("peak_price"), f"{identity} peak price", positive=True)}
    return rows


def _load_profiles(path: Path, census: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows, seen = [], set()
    try:
        with open(path, encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip(): continue
                try: raw = json.loads(line)
                except json.JSONDecodeError: raise StudyError(f"profile line {line_number} is not JSON") from None
                if not isinstance(raw, Mapping): raise StudyError(f"profile line {line_number} is not an object")
                identity = raw.get("episode_id"); source = census.get(identity) if isinstance(identity, str) else None
                if source is None or identity in seen: raise StudyError("profile identity differs")
                for field in ("class", "symbol", "venue", "base_utc", "peak_utc"):
                    if raw.get(field) != source[field]: raise StudyError(f"{identity} {field} differs")
                if _decimal(raw.get("base_price"), f"{identity} base price", positive=True) != source["base_price"]: raise StudyError(f"{identity} base price differs")
                t5 = _minutes(raw.get("time_to_5pct_minutes"), f"{identity} time to 5 percent")
                t10 = _minutes(raw.get("time_to_10pct_minutes"), f"{identity} time to 10 percent")
                peak_minutes = (source["peak_ms"] - source["base_ms"]) // MINUTE_MS
                if (t5 is not None and t5 > peak_minutes) or (t10 is not None and t10 > peak_minutes): raise StudyError(f"{identity} checkpoint is after peak")
                rows.append(dict(source) | {"time_to_5pct_minutes": t5, "time_to_10pct_minutes": t10}); seen.add(identity)
    except (OSError, UnicodeError) as error: raise StudyError(f"cannot read profiles: {error}") from None
    if seen != set(census): raise StudyError("profile population differs from census")
    return rows


def _kline_rows(path: Path, symbol: str) -> Any:
    if absorption.tape_pull.stream_verified_member is not tape_pull.stream_verified_member: raise StudyError("shared ZIP reader binding differs")
    for bar in absorption._kline_rows(path, symbol): yield bar.open_ms, bar.high, bar.low


def _archive_paths(row: Mapping[str, Any], roots: Sequence[Path]) -> set[Path]:
    start, end = EPOCH + timedelta(milliseconds=row["base_ms"]), EPOCH + timedelta(milliseconds=row["peak_ms"])
    months, days, cursor = {start.strftime("%Y-%m"), end.strftime("%Y-%m")}, set(), start.date()
    while cursor <= end.date(): days.add(cursor.isoformat()); cursor += timedelta(days=1)
    prefix = Path("spot") if row["venue"] == "spot" else Path("futures/um")
    paths = set()
    for root in roots:
        base = root / prefix
        paths.update(base / "monthly/klines" / row["symbol"] / "1m" / f"{row['symbol']}-1m-{month}.zip" for month in months)
        paths.update(base / "daily/klines" / row["symbol"] / "1m" / f"{row['symbol']}-1m-{day}.zip" for day in days)
    return paths


def _recompute(rows: Sequence[dict[str, Any]], history: Path, recent: Path) -> Mapping[str, Any]:
    missing = [row for row in rows if row["time_to_5pct_minutes"] is None or row["time_to_10pct_minutes"] is None]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in missing: groups[(row["venue"], row["symbol"])].append(row)
    archives = parsed = 0; filled = Counter()
    for episodes in groups.values():
        paths = set().union(*(_archive_paths(row, (history, recent)) for row in episodes)); bars = {}
        for path in sorted(path for path in paths if path.is_file()):
            archives += 1
            for stamp, high, low in _kline_rows(path, episodes[0]["symbol"]):
                parsed += 1; prior = bars.get(stamp)
                if prior is not None and prior != (high, low): raise StudyError(f"{path.name} conflicts with another archive")
                bars[stamp] = high, low
        for row in episodes:
            base_bar = bars.get(row["base_ms"])
            if base_bar is None or base_bar[1] != row["base_price"]: raise StudyError(f"{row['episode_id']} base kline is not proved")
            needed = [percent for percent in (5, 10) if row[f"time_to_{percent}pct_minutes"] is None]
            for stamp in range(row["base_ms"], row["peak_ms"] + MINUTE_MS, MINUTE_MS):
                bar = bars.get(stamp)
                if bar is None: raise StudyError(f"{row['episode_id']} checkpoint continuity is absent")
                for percent in tuple(needed):
                    if bar[0] >= row["base_price"] * (Decimal("1") + Decimal(percent) / Decimal("100")):
                        field = f"time_to_{percent}pct_minutes"; row[field] = (stamp - row["base_ms"]) // MINUTE_MS
                        filled[field] += 1; needed.remove(percent)
                if not needed: break
            if needed: raise StudyError(f"{row['episode_id']} missing checkpoint was not reached")
    return {"profile_rows_reused": len(rows) - len(missing), "episodes_recomputed": len(missing), "fields_recomputed": dict(filled),
            "archives_read": archives, "bars_parsed": parsed, "history_present": history.is_dir(), "recent_data_present": recent.is_dir()}


def _quantile(values: Sequence[Decimal], numerator: int, denominator: int = 10) -> Optional[Decimal]:
    if not values: return None
    ordered = sorted(values)
    with localcontext() as context:
        context.prec = 50; position = Decimal(len(ordered) - 1) * numerator / denominator
        lower = int(position); upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _distribution(values: Sequence[int]) -> Mapping[str, Any]:
    decimals = [Decimal(value) for value in values]
    return {"n": len(decimals), "min": min(decimals), "p10": _quantile(decimals, 1), "median": _quantile(decimals, 5),
            "p90": _quantile(decimals, 9), "max": max(decimals)}


def _rate_rows(rows: Sequence[Mapping[str, Any]], predicate: Any) -> Mapping[str, Any]:
    answer = {}
    for label in ("ALL",) + CLASSES:
        selected = rows if label == "ALL" else [row for row in rows if row["class"] == label]
        matched = sum(bool(predicate(row)) for row in selected)
        with localcontext() as context: context.prec = 50; fraction = Decimal(matched) / Decimal(len(selected))
        answer[label] = {"matched": matched, "episodes": len(selected), "fraction": fraction}
    return answer


def _serial(value: Any) -> Any:
    if isinstance(value, Decimal):
        rendered = format(value, "f")
        if "." in rendered: rendered = rendered.rstrip("0").rstrip(".")
        return "0" if rendered in {"", "-0"} else rendered
    if isinstance(value, Mapping): return {key: _serial(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)): return [_serial(item) for item in value]
    return value


def _text(value: Decimal) -> str: return format(value, ".8g")


def _write(path: Path, payload: Any, *, text_mode: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(payload if text_mode else json.dumps(_serial(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def run(census_path: Path, profiles_path: Path, history: Path, recent: Path, output: Path) -> Mapping[str, Any]:
    census = _load_census(census_path); rows = _load_profiles(profiles_path, census); source = _recompute(rows, history, recent)
    for row in rows:
        t5, t10 = row["time_to_5pct_minutes"], row["time_to_10pct_minutes"]
        if t5 is None or t10 is None or t5 > t10: raise StudyError(f"{row['episode_id']} checkpoint ordering differs")
    groups = {"ALL": rows} | {label: [row for row in rows if row["class"] == label] for label in CLASSES}
    distributions = {field: {label: _distribution([row[field] for row in group]) for label, group in groups.items()}
                     for field in ("time_to_5pct_minutes", "time_to_10pct_minutes")}
    within = {str(cutoff): _rate_rows(rows, lambda row, limit=cutoff: row["time_to_5pct_minutes"] <= limit) for cutoff in CUTOFFS}
    proved = _rate_rows(rows, lambda row: row["time_to_10pct_minutes"] - row["time_to_5pct_minutes"] >= 2)
    unresolved = _rate_rows(rows, lambda row: row["time_to_10pct_minutes"] - row["time_to_5pct_minutes"] < 2)
    earlier_bucket = _rate_rows(rows, lambda row: row["time_to_5pct_minutes"] < row["time_to_10pct_minutes"])
    result = {"schema": "entry-window-study-v1", "question": "minutes from pump base to first observed 5 and 10 percent rise",
              "population": {"episodes": len(rows), "class_counts": dict(Counter(row["class"] for row in rows))},
              "source": dict(source) | {"census": str(census_path), "profiles": str(profiles_path), "zip_reader": "study.tape_pull.stream_verified_member"},
              "definitions": {"checkpoint": "first continuous 1m high at or above the percentage rise from base",
                              "quantile": "exact linearly interpolated quantile",
                              "within": "checkpoint minute is less than or equal to the named minute",
                              "ladder_proof": "rungs become eligible at 0, 2, and 5 percent; later fills wait at least 20 seconds; proved before 10 percent only when the first 10 percent bucket is at least two minutes after the first 5 percent bucket"},
              "distributions": distributions, "within_5pct_minutes": within,
              "ladder_0_2_5_with_20s_gaps": {"proved_all_before_10pct": proved, "one_minute_resolution_unresolved": unresolved,
                                                   "first_5pct_in_earlier_bucket_than_10pct": earlier_bucket},
              "limits": "one-minute klines do not order crossings inside a candle; the ladder proved fraction is a fail-closed lower bound; no strategy threshold is proposed"}
    def stat(field: str, label: str) -> str:
        item = distributions[field][label]
        return f"{label} n={item['n']} min/p10/median/p90/max=" + "/".join(_text(item[key]) for key in ("min", "p10", "median", "p90", "max"))
    lines = [f"scope | episodes={len(rows)} HELD={len(groups['HELD'])} COLLAPSED={len(groups['COLLAPSED'])}; profile rows reused without kline recompute={source['profile_rows_reused']}; recomputed={source['episodes_recomputed']}; archives={source['archives_read']} | MEASURED"]
    for field, label in (("time_to_5pct_minutes", "base to +5 percent minutes"), ("time_to_10pct_minutes", "base to +10 percent minutes")):
        lines.extend(f"{label} | {stat(field, group)} | MEASURED" for group in CLASSES)
    lines.append("+5 percent within base minutes | " + "; ".join(f"{cutoff}={within[str(cutoff)]['ALL']['matched']}/{within[str(cutoff)]['ALL']['episodes']} {_text(within[str(cutoff)]['ALL']['fraction'] * 100)}%" for cutoff in CUTOFFS) + " | MEASURED")
    lines.append(f"ladder +0/+2/+5 with 20-second gaps before +10 | proved={proved['ALL']['matched']}/{proved['ALL']['episodes']} {_text(proved['ALL']['fraction'] * 100)}%; HELD={proved['HELD']['matched']}/{proved['HELD']['episodes']} {_text(proved['HELD']['fraction'] * 100)}%; COLLAPSED={proved['COLLAPSED']['matched']}/{proved['COLLAPSED']['episodes']} {_text(proved['COLLAPSED']['fraction'] * 100)}%; unresolved at 1m={unresolved['ALL']['matched']} | MEASURED")
    lines.append(f"resolution | first +5 bucket earlier than +10={earlier_bucket['ALL']['matched']}/{earlier_bucket['ALL']['episodes']} {_text(earlier_bucket['ALL']['fraction'] * 100)}%, but same/next-bucket timing cannot prove two 20-second gaps; proved rate is the lower bound | MEASURED")
    lines.append("limits | offline AB13/AB15 population only; 1m candles do not prove within-candle order or executable fills; no threshold proposed | MEASURED")
    _write(output / "entry-window-summary.json", result)
    _write(output / "entry-window-distributions.txt", "\n".join(lines) + "\n", text_mode=True)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--census", type=Path, default=DEFAULT_CENSUS); parser.add_argument("--profiles", type=Path, default=DEFAULT_PROFILES)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY); parser.add_argument("--recent-data", type=Path, default=DEFAULT_RECENT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT); args = parser.parse_args(argv)
    try: result = run(args.census, args.profiles, args.history, args.recent_data, args.output_dir)
    except (StudyError, absorption.AbsorptionError, tape_pull.TapePullError, OSError, UnicodeError, TypeError, ValueError) as error:
        print(f"entry window study: {error}", file=sys.stderr); return 2
    print(f"entry window | episodes={result['population']['episodes']} | output={args.output_dir}"); return 0


if __name__ == "__main__": raise SystemExit(main())
