#!/usr/bin/env python3
"""Scale the OF1 order-flow pair and measure precision at alarm density."""

from __future__ import annotations

import argparse, bisect, json, os, sys, zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

try:
    from study import orderflow_probe as probe
except ModuleNotFoundError as error:
    if error.name != "study": raise
    import orderflow_probe as probe  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CENSUS = ROOT / "lane-logs/AB13/pump-census-6mo.json"
DEFAULT_POPULATION = ROOT / "lane-logs/AB9/population.json"
DEFAULT_AB9 = ROOT / "lane-logs/AB9/signal-base-rate.json"
DEFAULT_EPISODE_TAPE = ROOT / "study/data-episode-tape"
DEFAULT_CONTROL_TAPE = ROOT / "study/data-canonical"
DEFAULT_OUTPUT, DEFAULT_REPORT = ROOT / "lane-logs/OF2", ROOT / "lane-logs/OF2-report.txt"
FEATURES = ("longest_buy_run_quote_share", "within_100ms_fraction", "same_side_run_max",
            "same_side_run_mean", "iceberg_like_repeat_count")
PAIR = ("longest_buy_run_quote_share", "within_100ms_fraction")
PERCENTILES = (90, 95, 99)
DAY_US, HOUR_US, PREBASE_US = probe.shape.DAY_US, probe.HOUR_US, 72 * probe.HOUR_US
FULL_BANK_US, MEMBER_LIMIT = 12 * probe.HOUR_US, 2_147_483_648


class PrecisionError(Exception): pass


def _read(path: Path) -> Any:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PrecisionError(f"cannot read {path.name}: {error}") from None


def _load(census_path: Path, population_path: Path, episode_root: Path, stride: int) -> Dict[str, Any]:
    census, population = _read(census_path), _read(population_path)
    raw = census.get("episodes") if isinstance(census, Mapping) else None
    if census.get("header", {}).get("schema") != "pump-census-6mo-v1" or not isinstance(raw, list):
        raise PrecisionError("census schema differs")
    if census.get("summary", {}).get("episodes") != len(raw) or len(raw) != 3429:
        raise PrecisionError("census episode total differs")
    if population.get("schema") != "signal-base-rate-population-v1": raise PrecisionError("AB9 population schema differs")
    census_symbols, control_symbols = population.get("census_symbols"), population.get("control_symbols")
    if not isinstance(census_symbols, list) or not isinstance(control_symbols, list) or len(census_symbols) != 26 or len(control_symbols) != 30:
        raise PrecisionError("AB9 population totals differ")
    if len(set(census_symbols) | set(control_symbols)) != 56 or set(census_symbols) & set(control_symbols):
        raise PrecisionError("AB9 population identity differs")
    evidence = population.get("control_baseline_evidence")
    if not isinstance(evidence, list): raise PrecisionError("AB9 control evidence differs")
    control_venues = {row.get("symbol"): row.get("venue") for row in evidence if isinstance(row, Mapping) and row.get("status") == "READY"}
    if set(control_venues) != set(control_symbols): raise PrecisionError("AB9 control venue evidence differs")
    parsed, seen, venue_sets, bases = [], set(), defaultdict(set), defaultdict(list)
    for position, row in enumerate(raw):
        if not isinstance(row, Mapping) or row.get("class") not in {"HELD", "COLLAPSED"}: raise PrecisionError("census row differs")
        episode_id = row.get("episode_id")
        if not isinstance(episode_id, str) or episode_id in seen: raise PrecisionError("census episode identity differs")
        symbol, venue = probe.tape_pull._validate_symbol(row.get("symbol")), probe.tape_pull._validate_venue(row.get("venue"))
        base = probe.shape._utc_us(row.get("base_utc"))
        if base % probe.MINUTE_US: raise PrecisionError(f"{episode_id} base is not minute aligned")
        item = dict(row); item["_base"], item["_position"] = base, position; parsed.append(item); seen.add(episode_id)
        venue_sets[symbol].add(venue); bases[(symbol, venue)].append(base)
    census_venues = {}
    for symbol in census_symbols:
        if venue_sets[symbol] not in ({"spot"}, {"futures"}): raise PrecisionError(f"{symbol} census venue differs")
        census_venues[symbol] = next(iter(venue_sets[symbol]))
    banked, bank_excluded = [], []
    for row in parsed:
        missing = [probe.shape._day_text(day) for day in probe.shape._required_days(row["_base"] - FULL_BANK_US, row["_base"])
                   if not probe.shape._archive(episode_root, row["symbol"], row["venue"], day, "aggTrades").is_file()]
        if missing: bank_excluded.append({"episode_id": row["episode_id"], "symbol": row["symbol"], "missing_archive_days": missing})
        else: banked.append(row)
    if len(banked) != 3425 or len(bank_excluded) != 4: raise PrecisionError("fully banked census count differs")
    selected = banked[::stride]
    symbol_venues = {symbol: census_venues[symbol] for symbol in census_symbols} | {symbol: control_venues[symbol] for symbol in control_symbols}
    complete_days = census.get("header", {}).get("scan", {}).get("complete_symbol_days")
    if not isinstance(complete_days, int) or complete_days <= 0: raise PrecisionError("census complete symbol-days differ")
    return {"census": census, "parsed": parsed, "banked": banked, "selected": selected, "bank_excluded": bank_excluded,
            "symbols": census_symbols + control_symbols, "census_symbol_set": set(census_symbols), "symbol_venues": symbol_venues,
            "bases": {key: sorted(values) for key, values in bases.items()}, "complete_days": complete_days}


def _windows(inputs: Mapping[str, Any], episode_root: Path, control_root: Path) -> Tuple[list[Dict[str, Any]], Dict[str, Any]]:
    if not episode_root.is_dir() or not control_root.is_dir(): raise PrecisionError("tape directory is absent")
    rows = []
    for item in inputs["selected"]:
        rows.append({"window_id": item["episode_id"], "group": item["class"], "symbol": item["symbol"], "venue": item["venue"],
                     "start": item["_base"] - HOUR_US, "end": item["_base"], "matched_episode_id": None,
                     "_root": str(episode_root), "_order": len(rows)})
    as_of = inputs["census"].get("header", {}).get("as_of")
    try: last = datetime.fromisoformat(str(as_of) + "T00:00:00+00:00")
    except ValueError: raise PrecisionError("census as_of differs") from None
    first = last - timedelta(days=29); excluded, by_symbol, candidates = 0, defaultdict(int), 0
    for symbol in inputs["symbols"]:
        venue, values = inputs["symbol_venues"][symbol], inputs["bases"].get((symbol, inputs["symbol_venues"][symbol]), ())
        for day_index in range(30):
            day = probe.shape._utc_us((first + timedelta(days=day_index)).astimezone(timezone.utc).isoformat())
            for hour in range(0, 24, 4):
                start, end = day + hour * HOUR_US, day + hour * HOUR_US + HOUR_US; candidates += 1
                index = bisect.bisect_left(values, end)
                if index < len(values) and values[index] <= end + PREBASE_US:
                    excluded += 1; by_symbol[symbol] += 1; continue
                rows.append({"window_id": f"K:{symbol}:{probe.shape._day_text(day)}:{hour:02d}", "group": "CONTROL", "symbol": symbol,
                             "venue": venue, "start": start, "end": end, "matched_episode_id": None,
                             "_root": str(control_root), "_order": len(rows)})
    if candidates != 10080 or len(rows) != len(inputs["selected"]) + candidates - excluded:
        raise PrecisionError("control window schedule differs")
    return rows, {"candidate_windows": candidates, "positive_windows_excluded": excluded, "exclusions_by_symbol": dict(sorted(by_symbol.items()))}


def _scan_day(path: Path, day: int, targets: Sequence[probe.WindowState]) -> Tuple[int, int]:
    ordered = sorted(targets, key=lambda state: (state.raw["start"], state.raw["end"])); active = []; cursor = checked = featured = 0
    stop, pending, previous, found, past = max(state.raw["end"] for state in ordered), b"", None, False, False
    expected = path.stem + ".csv"
    with zipfile.ZipFile(path, "r") as archive:
        members = [member for member in archive.infolist() if not member.is_dir()]
        if len(members) != 1 or members[0].filename != expected or members[0].flag_bits & 1:
            raise probe.fine.FineSliceError(f"{path.name} member differs")
        for chunk in probe.tape_pull.stream_verified_member(archive, members[0], MEMBER_LIMIT):
            if past: continue
            pieces = (pending + chunk).split(b"\n"); pending = pieces.pop()
            for raw in pieces:
                raw = raw.strip()
                if not raw: continue
                cells = raw.split(b","); first = cells[0].lstrip(b"\xef\xbb\xbf").strip()
                if not found and first.lower().replace(b" ", b"_") in {b"agg_trade_id", b"aggregate_trade_id", b"aggtradeid"}: continue
                if len(cells) < 7: raise probe.fine.FineSliceError(f"{path.name} row width differs")
                aggregate_id, first_id, last_id, stamp = int(first), int(cells[3]), int(cells[4]), int(cells[5])
                stamp = stamp if stamp >= 100_000_000_000_000 else stamp * 1_000; key = stamp, aggregate_id
                if min(aggregate_id, first_id, last_id) < 0 or last_id < first_id or not day <= stamp < day + DAY_US:
                    raise probe.fine.FineSliceError(f"{path.name} trade identity differs")
                if previous is not None and key <= previous: raise probe.fine.FineSliceError(f"{path.name} ordering differs")
                previous, found, checked = key, True, checked + 1
                if stamp >= stop: past = True; pending = b""; break
                while cursor < len(ordered) and ordered[cursor].raw["start"] <= stamp: active.append(ordered[cursor]); cursor += 1
                active = [state for state in active if stamp < state.raw["end"]]
                if active:
                    price, quantity = probe.shape._decimal(cells[1].decode("ascii")), probe.shape._decimal(cells[2].decode("ascii"))
                    maker = cells[6].strip().lower()
                    if maker not in {b"true", b"false"}: raise probe.fine.FineSliceError(f"{path.name} maker field differs")
                    for state in active:
                        if state.acc is not None: state.acc.add(stamp, price, quantity, maker == b"true")
                    featured += 1
        if not past and pending.strip():
            raise probe.fine.FineSliceError(f"{path.name} final row framing differs")
    if not found: raise probe.fine.FineSliceError(f"{path.name} has no trades")
    return checked, featured


def _scan_task(task: Tuple[str, str, str, list[Dict[str, Any]]]) -> Dict[str, Any]:
    root_text, symbol, venue, windows = task; root = Path(root_text); getcontext().prec = 50
    states = [probe.WindowState(raw, tuple(sorted(probe.shape._required_days(raw["start"], raw["end"])))) for raw in windows]
    by_day = defaultdict(list)
    for state in states:
        for day in state.required: by_day[day].append(state)
    output, archives, checked, featured, errors = [], 0, 0, 0, []
    for day, targets in sorted(by_day.items()):
        path = probe._path(root, symbol, venue, day)
        if not path.is_file():
            for state in targets: state.missing.add(probe.shape._day_text(day)); state.acc = None
        else:
            try:
                day_checked, day_featured = _scan_day(path, day, targets); checked += day_checked; featured += day_featured; archives += 1
            except (probe.fine.FineSliceError, probe.tape_pull.ArchiveError, probe.shape.StudyError, OSError, UnicodeError,
                    ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
                errors.append({"archive": path.name, "kind": type(error).__name__})
                for state in targets: state.invalid.add(probe.shape._day_text(day)); state.acc = None
        for state in targets:
            if state.required[-1] == day: output.append(probe._finish(state)); state.acc = None
    return {"rows": output, "archives": archives, "rows_time_checked": checked, "rows_featured": featured, "errors": errors}


def _auc(rows: Sequence[Mapping[str, Any]], feature: str, positive: str, negative: str) -> Dict[str, Any]:
    pos = [row["features"][feature] for row in rows if row["group"] == positive and row["features"][feature] is not None]
    neg = [row["features"][feature] for row in rows if row["group"] == negative and row["features"][feature] is not None]
    raw = probe.shape._auc(pos, neg)
    if raw is None: return {"feature": feature, "rank_auc_positive_high": None, "oriented_rank_auc": None, "direction": None, "positive_n": len(pos), "negative_n": len(neg)}
    direction, oriented = ("high", raw) if raw >= Decimal("0.5") else ("low", Decimal("1") - raw)
    return {"feature": feature, "rank_auc_positive_high": raw, "oriented_rank_auc": oriented, "direction": direction,
            "positive_n": len(pos), "negative_n": len(neg)}


def _fraction(top: int, bottom: int) -> Optional[Decimal]: return None if not bottom else Decimal(top) / Decimal(bottom)
def _short(value: Optional[Decimal]) -> str: return "NA" if value is None else format(value, ".7g")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag, default in (("--census", DEFAULT_CENSUS), ("--population", DEFAULT_POPULATION), ("--ab9-summary", DEFAULT_AB9),
                          ("--episode-tape-dir", DEFAULT_EPISODE_TAPE), ("--control-tape-dir", DEFAULT_CONTROL_TAPE),
                          ("--output-dir", DEFAULT_OUTPUT), ("--report", DEFAULT_REPORT)):
        parser.add_argument(flag, type=Path, default=default)
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=8)
    parser.add_argument("--base-stride", type=int, choices=(1, 2), default=1)
    args = parser.parse_args(argv); getcontext().prec = 50
    try:
        if probe.fine.tape_pull.stream_verified_member is not probe.tape_pull.stream_verified_member:
            raise PrecisionError("shared ZIP reader binding differs")
        inputs = _load(args.census, args.population, args.episode_tape_dir, args.base_stride)
        windows, control_meta = _windows(inputs, args.episode_tape_dir, args.control_tape_dir)
        tasks = defaultdict(list)
        for row in windows: tasks[(row["_root"], row["symbol"], row["venue"])].append(row)
        scan_tasks = [(*key, value) for key, value in sorted(tasks.items())]; results = []
        if args.workers == 1: results = [_scan_task(task) for task in scan_tasks]
        else:
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = [executor.submit(_scan_task, task) for task in scan_tasks]
                for count, future in enumerate(as_completed(futures), 1):
                    results.append(future.result())
                    if count % 25 == 0 or count == len(futures): print(f"OF2 scan | groups={count}/{len(futures)}", flush=True)
        rows = sorted((row for result in results for row in result["rows"]), key=lambda row: row["_order"])
        if len(rows) != len(windows) or len({row["window_id"] for row in rows}) != len(rows): raise PrecisionError("scanned window identity differs")
        population_group = {symbol: "AB9_CENSUS" if symbol in inputs["census_symbol_set"] else "AB9_CONTROL" for symbol in inputs["symbols"]}
        measured = [row for row in rows if row["status"] == "MEASURED"]
        ranks = {feature: probe._rank_map(measured, feature) for feature in FEATURES}
        for row in rows:
            row["population_group"] = population_group.get(row["symbol"])
            row["feature_ranks"] = {feature: ranks[feature].get(row["window_id"]) for feature in FEATURES}
            left, right = row["feature_ranks"][PAIR[0]], row["feature_ranks"][PAIR[1]]
            row["of1_pair_score"] = None if left is None or right is None else -left + right
            row["features"] = {feature: row["features"][feature] for feature in FEATURES}; row.pop("_order")
        base_rows = [row for row in rows if row["group"] != "CONTROL"]
        control_rows = [row for row in rows if row["group"] == "CONTROL"]
        pair_pool = sorted(row["of1_pair_score"] for row in rows if row["of1_pair_score"] is not None)
        if not pair_pool: raise PrecisionError("OF1 pair score pool is empty")
        points = []
        base_rate = Decimal(len(inputs["banked"])) / Decimal(inputs["complete_days"])
        observed_controls = sum(row["status"] == "MEASURED" for row in control_rows)
        for percentile in PERCENTILES:
            cutoff = pair_pool[(percentile * len(pair_pool) + 99) // 100 - 1]
            base_fires = sum(row["of1_pair_score"] is not None and row["of1_pair_score"] >= cutoff for row in base_rows)
            control_fires = sum(row["of1_pair_score"] is not None and row["of1_pair_score"] >= cutoff for row in control_rows)
            recall, false_density = _fraction(base_fires, len(base_rows)), _fraction(control_fires * 6, observed_controls)
            true_density = None if recall is None else recall * base_rate
            precision = None if true_density is None or false_density is None or true_density + false_density == 0 else true_density / (true_density + false_density)
            points.append({"percentile": percentile, "cutoff": cutoff, "base_fires": base_fires, "base_total": len(base_rows), "recall": recall,
                           "control_fires": control_fires, "observed_control_windows": observed_controls,
                           "false_alarms_per_observed_symbol_day": false_density, "census_base_rate_per_complete_symbol_day": base_rate,
                           "true_fires_per_symbol_day_at_recall": true_density, "census_rate_standardized_precision": precision})
        base_control = {feature: _auc([row | {"group": "BASE" if row["group"] != "CONTROL" else "CONTROL"} for row in measured], feature, "BASE", "CONTROL") for feature in FEATURES}
        pair_base = probe.shape._auc([row["of1_pair_score"] for row in base_rows if row["of1_pair_score"] is not None],
                                     [row["of1_pair_score"] for row in control_rows if row["of1_pair_score"] is not None])
        held_collapsed = {feature: _auc(measured, feature, "HELD", "COLLAPSED") for feature in FEATURES}
        pair_hc = probe.shape._auc([row["of1_pair_score"] for row in rows if row["group"] == "HELD" and row["of1_pair_score"] is not None],
                                   [row["of1_pair_score"] for row in rows if row["group"] == "COLLAPSED" and row["of1_pair_score"] is not None])
        coverage = {group: {status: sum(row["group"] == group and row["status"] == status for row in rows)
                            for status in ("MEASURED", "NO_TRADES", "ARCHIVE_MISSING", "ARCHIVE_INVALID")}
                    for group in ("HELD", "COLLAPSED", "CONTROL")}
        distributions = {feature: {group: probe.shape._stats([row["features"][feature] for row in rows if row["status"] == "MEASURED" and
                                                              (row["group"] == group if group != "BASE" else row["group"] != "CONTROL") and row["features"][feature] is not None])
                                   for group in ("BASE", "CONTROL", "HELD", "COLLAPSED")} for feature in FEATURES}
        ab9 = _read(args.ab9_summary); ab9_point = ab9.get("groups", {}).get("all", {}).get("tiers", {}).get("at_least_3", {})
        if ab9.get("schema") != "signal-base-rate-summary-v1": raise PrecisionError("AB9 summary schema differs")
        source = {"archives_streamed_valid": sum(item["archives"] for item in results), "archive_errors": [error for item in results for error in item["errors"]],
                  "trade_rows_time_checked": sum(item["rows_time_checked"] for item in results), "trade_rows_used_by_at_least_one_window": sum(item["rows_featured"] for item in results),
                  "zip_reader": "study.tape_pull.stream_verified_member", "member_byte_limit": MEMBER_LIMIT}
        summary = {"schema": "orderflow-precision-summary-v1", "question": "does the fixed OF1 pair retain base separation at scale and at what alarm density",
                   "parameters": {"window_minutes": 60, "control_days": 30, "control_step_hours": 4, "prebase_control_exclusion_hours": 72,
                                  "base_selection": "all fully banked episodes" if args.base_stride == 1 else "zero-based even positions in fully banked census order",
                                  "base_stride": args.base_stride, "rank_method": "pooled tie-midrank from zero through one",
                                  "pair_score": "negative rank(longest_buy_run_quote_share) plus rank(within_100ms_fraction)",
                                  "percentile_method": "empirical nearest rank", "measurement_points_only": True,
                                  "precision_standardization": "sample recall times 3425/complete census symbol-days, divided by that true-fire density plus measured control false-fire density"},
                   "population": {"raw_census_episodes": len(inputs["parsed"]), "fully_banked_census_episodes": len(inputs["banked"]),
                                  "selected_census_episodes": len(inputs["selected"]), "selected_class_counts": {label: sum(row["class"] == label for row in inputs["selected"]) for label in ("HELD", "COLLAPSED")},
                                  "bank_exclusions": inputs["bank_excluded"], "census_complete_symbol_days": inputs["complete_days"],
                                  "ab9_population_symbols": len(inputs["symbols"])} | control_meta,
                   "coverage": coverage, "source": source, "distributions": distributions,
                   "base_vs_control": {"features": base_control, "fixed_of1_pair_rank_auc": pair_base, "of1_probe_pair_rank_auc": Decimal("0.73039141414141414141414141414141414141414141414141")},
                   "held_vs_collapsed": {"features": held_collapsed, "fixed_of1_pair_rank_auc_held_high": pair_hc}, "precision_at_density": points,
                   "ab9_yardstick": {"precision": ab9_point.get("precision"), "alarm_frequency_per_observed_symbol_day": ab9_point.get("alarm_frequency_per_observed_symbol_day")},
                   "limits": "deterministic in-sample measurement; aggregate trades do not identify participants; precision is census-rate standardized; percentile cutoffs are measurement points, not proposed thresholds"}
        strongest_hc = max((item["oriented_rank_auc"] for item in held_collapsed.values() if item["oriented_rank_auc"] is not None), default=None)
        feature_text = "; ".join(f"{name}={_short(base_control[name]['oriented_rank_auc'])}({base_control[name]['direction']})" for name in FEATURES)
        hc_text = "; ".join(f"{name}={_short(held_collapsed[name]['oriented_rank_auc'])}({held_collapsed[name]['direction']})" for name in FEATURES)
        lines = [f"METHOD | fully banked={len(inputs['banked'])}/3429; selected={len(inputs['selected'])} by " + ("all rows" if args.base_stride == 1 else "deterministic zero-based every-other banked row") + f"; selected HELD/COLLAPSED={summary['population']['selected_class_counts']['HELD']}/{summary['population']['selected_class_counts']['COLLAPSED']}; AB9 symbols=56 | MEASURED",
                 f"CENSUS_COVERAGE | H/C measured={coverage['HELD']['MEASURED']}/{coverage['COLLAPSED']['MEASURED']}; no-trades={coverage['HELD']['NO_TRADES'] + coverage['COLLAPSED']['NO_TRADES']}; missing={coverage['HELD']['ARCHIVE_MISSING'] + coverage['COLLAPSED']['ARCHIVE_MISSING']}; invalid={coverage['HELD']['ARCHIVE_INVALID'] + coverage['COLLAPSED']['ARCHIVE_INVALID']}; full-bank exclusions=4 | MEASURED",
                 f"CONTROL_COVERAGE | candidates={control_meta['candidate_windows']}; 72h positives excluded={control_meta['positive_windows_excluded']}; scheduled={len(control_rows)}; measured={coverage['CONTROL']['MEASURED']}; no-trades={coverage['CONTROL']['NO_TRADES']}; missing={coverage['CONTROL']['ARCHIVE_MISSING']}; invalid={coverage['CONTROL']['ARCHIVE_INVALID']} | MEASURED",
                 f"SOURCE | archives valid/errors={source['archives_streamed_valid']}/{len(source['archive_errors'])}; rows time-checked/featured={source['trade_rows_time_checked']}/{source['trade_rows_used_by_at_least_one_window']}; shared byte-accounted reader; 2GiB member bound | MEASURED",
                 f"BASE_CONTROL_SCALE | fixed OF1 pair AUC={_short(pair_base)} versus OF1={_short(summary['base_vs_control']['of1_probe_pair_rank_auc'])}; {feature_text} | MEASURED"]
        for point in points:
            lines.append(f"P{point['percentile']}_MEASUREMENT | cutoff={_short(point['cutoff'])}; control fires={point['control_fires']}/{point['observed_control_windows']}={_short(point['false_alarms_per_observed_symbol_day'])}/observed symbol-day; base recall={point['base_fires']}/{point['base_total']}={_short(point['recall'])}; census-rate standardized precision={_short(point['census_rate_standardized_precision'])}; no threshold proposed | MEASURED")
        lines += [f"HELD_COLLAPSED | fixed pair held-high AUC={_short(pair_hc)}; strongest oriented feature AUC={_short(strongest_hc)}; {hc_text}; observed distributions overlap | MEASURED",
                  f"YARDSTICK_LIMITS | AB9 aggregate family precision={ab9_point.get('precision')} at {ab9_point.get('alarm_frequency_per_observed_symbol_day')} fires/symbol-day; OF2 rows above state precision with density; aggregate trades lack participant identity; percentile cutoffs are measurement points; no thresholds proposed | MEASURED"]
        probe.shape._write(args.output_dir / "window-features.jsonl", rows, lines=True)
        probe.shape._write(args.output_dir / "orderflow-precision.json", summary)
        probe._write_text(args.output_dir / "orderflow-precision.txt", "\n".join(lines) + "\n")
        probe._write_text(args.report, "\n".join(lines) + "\n")
    except (OSError, UnicodeError, TypeError, ValueError, probe.tape_pull.TapePullError, probe.shape.StudyError, PrecisionError) as error:
        print(f"order-flow precision: {error}", file=sys.stderr); return 2
    print(f"OF2 | bases={len(inputs['selected'])}/{len(inputs['banked'])} | controls={len(control_rows)} | output={args.output_dir}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
