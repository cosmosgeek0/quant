#!/usr/bin/env python3
"""Measure final-hour per-trade order-flow sequences against matched controls."""

from __future__ import annotations

import argparse, bisect, json, os, sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

try:
    from study import fine_slice_study as fine, tape_pull, tape_shape_study as shape
except ModuleNotFoundError as error:
    if error.name != "study": raise
    import fine_slice_study as fine  # type: ignore
    import tape_pull  # type: ignore
    import tape_shape_study as shape  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CENSUS = ROOT / "lane-logs/AB13/pump-census-6mo.json"
DEFAULT_POPULATION = ROOT / "lane-logs/AB9/population.json"
DEFAULT_EPISODE_TAPE = ROOT / "study/data-episode-tape"
DEFAULT_CONTROL_TAPE = ROOT / "study/data-canonical"
DEFAULT_OUTPUT, DEFAULT_REPORT = ROOT / "lane-logs/OF1", ROOT / "lane-logs/OF1-report.txt"
HOUR_US, MINUTE_US, BUFFER_US = shape.HOUR_US, shape.MINUTE_US, 72 * shape.HOUR_US
BASE_FEATURES = ("same_side_run_max", "same_side_run_mean", "longest_buy_run_quote_share",
                 "buy_run_escalating_fraction", "iceberg_like_repeat_count")
DECILES = tuple(f"size_decile_{index}_quote_share" for index in range(1, 11))
FEATURES = BASE_FEATURES + DECILES + ("within_100ms_fraction",)


class ProbeError(Exception): pass


class SequenceAccumulator:
    def __init__(self) -> None:
        self.trades = self.clustered = self.iceberg_runs = 0
        self.total_quote = Decimal("0"); self.sizes: list[Decimal] = []; self.previous_stamp: Optional[int] = None
        self.run_side: Optional[bool] = None; self.run_length = 0; self.run_quote = Decimal("0")
        self.run_previous_quote: Optional[Decimal] = None; self.run_increasing = True
        self.run_lengths: list[int] = []; self.buy_runs = self.escalating_buy_runs = 0
        self.longest_buy_length = 0; self.longest_buy_quote = Decimal("0")
        self.repeat_key: Optional[Tuple[bool, Decimal]] = None; self.repeat_stamp: Optional[int] = None; self.repeat_length = 0

    def _finish_run(self) -> None:
        if not self.run_length: return
        self.run_lengths.append(self.run_length)
        if self.run_side:
            if self.run_length >= 2:
                self.buy_runs += 1; self.escalating_buy_runs += int(self.run_increasing)
            if (self.run_length, self.run_quote) > (self.longest_buy_length, self.longest_buy_quote):
                self.longest_buy_length, self.longest_buy_quote = self.run_length, self.run_quote

    def _finish_repeat(self) -> None:
        if self.repeat_length >= 3: self.iceberg_runs += 1

    def add(self, stamp: int, price: Decimal, quantity: Decimal, maker: bool) -> None:
        side, quote = not maker, price * quantity
        if self.previous_stamp is not None and stamp - self.previous_stamp <= 100_000: self.clustered += 1
        self.previous_stamp = stamp; self.trades += 1; self.total_quote += quote; self.sizes.append(quote)
        if side != self.run_side:
            self._finish_run(); self.run_side, self.run_length, self.run_quote = side, 1, quote
            self.run_previous_quote, self.run_increasing = quote, True
        else:
            self.run_length += 1; self.run_quote += quote
            self.run_increasing = self.run_increasing and quote > self.run_previous_quote  # type: ignore[operator]
            self.run_previous_quote = quote
        key = side, quantity
        if key == self.repeat_key and self.repeat_stamp is not None and stamp - self.repeat_stamp <= 2_000_000:
            self.repeat_length += 1
        else:
            self._finish_repeat(); self.repeat_key, self.repeat_length = key, 1
        self.repeat_stamp = stamp

    def features(self) -> Dict[str, Optional[Decimal]]:
        self._finish_run(); self._finish_repeat(); found: Dict[str, Optional[Decimal]] = {name: None for name in FEATURES}
        if not self.trades: return found
        found["same_side_run_max"] = Decimal(max(self.run_lengths))
        found["same_side_run_mean"] = Decimal(sum(self.run_lengths)) / Decimal(len(self.run_lengths))
        found["longest_buy_run_quote_share"] = None if not self.longest_buy_length else self.longest_buy_quote / self.total_quote
        found["buy_run_escalating_fraction"] = None if not self.buy_runs else Decimal(self.escalating_buy_runs) / Decimal(self.buy_runs)
        found["iceberg_like_repeat_count"] = Decimal(self.iceberg_runs)
        bins = [Decimal("0") for _ in DECILES]; ordered = sorted(self.sizes); count = len(ordered)
        for index, quote in enumerate(ordered): bins[min(9, (10 * (index + 1) - 1) // count)] += quote
        for name, quote in zip(DECILES, bins): found[name] = quote / self.total_quote
        found["within_100ms_fraction"] = None if self.trades < 2 else Decimal(self.clustered) / Decimal(self.trades - 1)
        self.sizes.clear(); return found


@dataclass
class WindowState:
    raw: Dict[str, Any]; required: Tuple[int, ...]
    acc: Optional[SequenceAccumulator] = field(default_factory=SequenceAccumulator)
    missing: set[str] = field(default_factory=set); invalid: set[str] = field(default_factory=set)


def _load_inputs(census_path: Path, population_path: Path) -> Tuple[list[Dict[str, Any]], list[Tuple[str, str]], Dict[Tuple[str, str], list[int]], int]:
    census, population = shape._read(census_path), shape._read(population_path)
    raw = census.get("episodes") if isinstance(census, Mapping) else None
    if census.get("header", {}).get("schema") != "pump-census-6mo-v1" or not isinstance(raw, list) or census.get("summary", {}).get("episodes") != len(raw):
        raise ProbeError("census schema or episode total differs")
    if population.get("schema") != "signal-base-rate-population-v1" or population.get("control_symbol_count") != 30:
        raise ProbeError("control population schema or total differs")
    controls = set(population.get("control_symbols", ())); evidence = population.get("control_baseline_evidence", ())
    venues = {row.get("symbol"): row.get("venue") for row in evidence if isinstance(row, Mapping) and row.get("symbol") in controls}
    if len(controls) != 30 or set(venues) != controls: raise ProbeError("control symbol evidence differs")
    seen_ids, class_seen, candidates, bases = set(), {"HELD": 0, "COLLAPSED": 0}, [], {}
    for position, item in enumerate(raw):
        if not isinstance(item, Mapping) or item.get("class") not in class_seen or item.get("episode_id") in seen_ids: raise ProbeError("census episode identity differs")
        symbol, venue = tape_pull._validate_symbol(item.get("symbol")), tape_pull._validate_venue(item.get("venue")); base = shape._utc_us(item.get("base_utc"))
        if base % MINUTE_US: raise ProbeError(f"{item.get('episode_id')} base is not minute aligned")
        row = dict(item); row["_base"], row["_position"] = base, position; seen_ids.add(row["episode_id"])
        bases.setdefault((symbol, venue), []).append(base)
        class_index = class_seen[row["class"]]; class_seen[row["class"]] += 1
        if class_index % 8 == 0: candidates.append(row)
    selected = candidates[:400]
    if len(selected) != 400: raise ProbeError("class-local stride did not provide 400 episodes")
    return selected, [(symbol, venues[symbol]) for symbol in population["control_symbols"]], {key: sorted(value) for key, value in bases.items()}, len(raw)


def _path(root: Path, symbol: str, venue: str, day: int) -> Path: return shape._archive(root, symbol, venue, day, "aggTrades")


def _archive_days(root: Path, symbol: str, venue: str) -> set[int]:
    folder = shape._venue_root(root, venue) / "daily/aggTrades" / symbol; found = set()
    for path in folder.glob(f"{symbol}-aggTrades-????-??-??.zip"):
        try: found.add(shape._utc_us(path.name[-14:-4] + "T00:00:00Z"))
        except shape.StudyError: raise ProbeError(f"{path.name} day differs") from None
    return found


def _has_nearby_base(values: Sequence[int], start: int, end: int) -> bool:
    index = bisect.bisect_left(values, start - BUFFER_US)
    return index < len(values) and values[index] <= end + BUFFER_US


def _overlaps(intervals: Sequence[Tuple[int, int]], start: int, end: int) -> bool:
    index = bisect.bisect_left(intervals, (start, end))
    return (index and intervals[index - 1][1] > start) or (index < len(intervals) and intervals[index][0] < end)


def _windows(selected: Sequence[Dict[str, Any]], controls: Sequence[Tuple[str, str]], bases: Mapping[Tuple[str, str], Sequence[int]],
             episode_root: Path, control_root: Path) -> Tuple[list[Dict[str, Any]], Dict[str, Any]]:
    if not episode_root.is_dir(): raise ProbeError("episode tape directory is absent")
    if not control_root.is_dir(): raise ProbeError("control tape directory is absent in this worktree")
    control_days = {(symbol, venue): _archive_days(control_root, symbol, venue) for symbol, venue in controls}
    by_venue = {venue: [(symbol, venue) for symbol, item_venue in controls if item_venue == venue] for venue in ("spot", "futures")}
    chosen: Dict[Tuple[str, str], list[Tuple[int, int]]] = {key: [] for key in control_days}; venue_count = {"spot": 0, "futures": 0}; rows = []
    for item in selected:
        base, start = item["_base"], item["_base"] - HOUR_US
        rows.append({"window_id": item["episode_id"], "group": item["class"], "symbol": item["symbol"], "venue": item["venue"],
                     "start": start, "end": base, "matched_episode_id": None, "_root": str(episode_root), "_order": len(rows)})
    for match_index, item in enumerate(selected, 1):
        venue, base = item["venue"], item["_base"]; candidates = by_venue[venue]
        if not candidates: raise ProbeError(f"no {venue} control symbols")
        rotation = venue_count[venue] % len(candidates); venue_count[venue] += 1; match = None
        minute, base_hour = (base // MINUTE_US) % 60, (base // HOUR_US) % 24
        offsets = sorted(range(-12, 12), key=lambda value: (abs(value), value < 0, value))
        for offset in offsets:
            if match is not None: break
            hour = (base_hour + offset) % 24
            for shift in range(len(candidates)):
                key = candidates[(rotation + shift) % len(candidates)]; days = control_days[key]
                for day in sorted(days, key=lambda value: (abs(value - base), value)):
                    end = day + hour * HOUR_US + minute * MINUTE_US; start = end - HOUR_US
                    required = shape._required_days(start, end)
                    if not required.issubset(days) or _has_nearby_base(bases.get(key, ()), start, end) or _overlaps(chosen[key], start, end): continue
                    match = key, start, end; break
                if match is not None: break
        if match is None: raise ProbeError(f"no eligible matched {venue} control window for {item['episode_id']}")
        key, start, end = match; bisect.insort(chosen[key], (start, end))
        rows.append({"window_id": f"C{match_index:04d}", "group": "CONTROL", "symbol": key[0], "venue": key[1], "start": start, "end": end,
                     "matched_episode_id": item["episode_id"], "_root": str(control_root), "_order": len(rows)})
    return rows, {"episode_venue_counts": {venue: sum(row["venue"] == venue for row in rows[:400]) for venue in ("spot", "futures")},
                  "control_symbols_with_archives": sum(bool(days) for days in control_days.values()),
                  "control_symbols_used": len({(row["symbol"], row["venue"]) for row in rows[400:]})}


def _finish(state: WindowState) -> Dict[str, Any]:
    raw, acc = state.raw, state.acc; status = "MEASURED"
    if state.missing: status = "ARCHIVE_MISSING"
    elif state.invalid: status = "ARCHIVE_INVALID"
    elif acc is None or not acc.trades: status = "NO_TRADES"
    features = acc.features() if status == "MEASURED" and acc is not None else {name: None for name in FEATURES}
    return {key: raw[key] for key in ("window_id", "group", "symbol", "venue", "matched_episode_id", "_order")} | {
        "schema": "orderflow-window-v1", "window_start_utc": shape._utc_text(raw["start"]), "window_end_utc_exclusive": shape._utc_text(raw["end"]),
        "status": status, "trade_count": acc.trades if acc is not None else None, "required_archive_days": [shape._day_text(day) for day in state.required],
        "missing_archive_days": sorted(state.missing), "invalid_archive_days": sorted(state.invalid), "features": features}


def _scan_task(task: Tuple[str, str, str, list[Dict[str, Any]]]) -> Dict[str, Any]:
    root_text, symbol, venue, windows = task; root = Path(root_text); getcontext().prec = 50
    states = [WindowState(raw, tuple(sorted(shape._required_days(raw["start"], raw["end"])))) for raw in windows]
    by_day: Dict[int, list[WindowState]] = {}
    for state in states:
        for day in state.required: by_day.setdefault(day, []).append(state)
    output, archives, trades, errors = [], 0, 0, []
    for day, targets in sorted(by_day.items()):
        path = _path(root, symbol, venue, day)
        if not path.is_file():
            for state in targets: state.missing.add(shape._day_text(day)); state.acc = None
        else:
            ordered, active, cursor = sorted(targets, key=lambda state: (state.raw["start"], state.raw["end"])), [], 0
            try:
                for aggregate_id, stamp, price, quantity, maker in fine._trade_rows(path, day):
                    trades += 1
                    while cursor < len(ordered) and ordered[cursor].raw["start"] <= stamp: active.append(ordered[cursor]); cursor += 1
                    active = [state for state in active if stamp < state.raw["end"]]
                    for state in active:
                        if state.acc is not None: state.acc.add(stamp, price, quantity, maker)
                archives += 1
            except (fine.FineSliceError, OSError) as error:
                errors.append({"archive": path.name, "kind": type(error).__name__})
                for state in targets: state.invalid.add(shape._day_text(day)); state.acc = None
        for state in targets:
            if state.required[-1] == day: output.append(_finish(state)); state.acc = None
    return {"rows": output, "archives": archives, "trades": trades, "errors": errors}


def _rank_map(rows: Sequence[Mapping[str, Any]], feature: str) -> Dict[str, Decimal]:
    values = sorted(row["features"][feature] for row in rows if row["features"][feature] is not None); found = {}
    for row in rows:
        value = row["features"][feature]
        if value is None: continue
        left, right = bisect.bisect_left(values, value), bisect.bisect_right(values, value)
        found[row["window_id"]] = Decimal(left + right - 1) / Decimal(2 * max(1, len(values) - 1))
    return found


def _comparison(rows: Sequence[Mapping[str, Any]], positive: Any, negative: Any, positive_name: str) -> Dict[str, Any]:
    eligible = [row for row in rows if row["status"] == "MEASURED" and (positive(row) or negative(row))]; singles, ranks = {}, {}
    for feature in FEATURES:
        pos = [row["features"][feature] for row in eligible if positive(row) and row["features"][feature] is not None]
        neg = [row["features"][feature] for row in eligible if negative(row) and row["features"][feature] is not None]; raw = shape._auc(pos, neg)
        if raw is None: continue
        direction, auc = ("high", raw) if raw >= Decimal("0.5") else ("low", Decimal("1") - raw)
        singles[feature] = {"feature": feature, f"direction_for_{positive_name}": direction, "rank_auc": auc, "positive_n": len(pos), "negative_n": len(neg)}
        ranks[feature] = _rank_map(eligible, feature)
    best_single = min(singles.values(), key=lambda row: (-row["rank_auc"], row["feature"])) if singles else None; best_pair = None
    for left_index, left in enumerate(FEATURES):
        for right in FEATURES[left_index + 1:]:
            if left not in singles or right not in singles: continue
            common = [row for row in eligible if row["window_id"] in ranks[left] and row["window_id"] in ranks[right]]
            signs = [Decimal("1") if singles[name][f"direction_for_{positive_name}"] == "high" else Decimal("-1") for name in (left, right)]
            score = lambda row: signs[0] * ranks[left][row["window_id"]] + signs[1] * ranks[right][row["window_id"]]
            pos = [score(row) for row in common if positive(row)]; neg = [score(row) for row in common if negative(row)]; raw = shape._auc(pos, neg)
            if raw is None: continue
            direction, auc = ("high", raw) if raw >= Decimal("0.5") else ("low", Decimal("1") - raw)
            candidate = {"features": [left, right], "member_orientations": {name: singles[name][f"direction_for_{positive_name}"] for name in (left, right)},
                         f"score_direction_for_{positive_name}": direction, "rank_auc": auc, "positive_n": len(pos), "negative_n": len(neg)}
            if best_pair is None or (-auc, left, right) < (-best_pair["rank_auc"], *best_pair["features"]): best_pair = candidate
    clean = []
    for feature in FEATURES:
        pos = [row["features"][feature] for row in eligible if positive(row) and row["features"][feature] is not None]
        neg = [row["features"][feature] for row in eligible if negative(row) and row["features"][feature] is not None]
        if pos and neg and (max(pos) < min(neg) or max(neg) < min(pos)): clean.append(feature)
    return {"best_single": best_single, "best_pair": best_pair, "clean_range_separation": clean}


def _overlap(first: Mapping[str, Any], second: Mapping[str, Any], first_name: str, second_name: str) -> str:
    if first["median"] is None or second["p10"] is None: return "unavailable"
    if first["max"] < second["min"] or second["max"] < first["min"]: return "ranges separate"
    position = "below" if first["median"] < second["p10"] else "above" if first["median"] > second["p90"] else "inside"
    return f"ranges overlap; {first_name} median {position} {second_name} p10-p90"


def _short(value: Optional[Decimal]) -> str: return "NA" if value is None else format(value, ".6g")


def _dist_text(item: Mapping[str, Any]) -> str:
    parts = []
    for label, short in (("HELD", "H"), ("COLLAPSED", "C"), ("CONTROL", "K")):
        row = item[label]; parts.append(f"{short} n={row['count']} min/p10/med/p90/max=" + "/".join(_short(row[key]) for key in ("min", "p10", "median", "p90", "max")))
    return "; ".join(parts) + f"; BK={item['base_control_overlap']}; HC={item['held_collapsed_overlap']}"


def _auc_text(item: Optional[Mapping[str, Any]]) -> str:
    if item is None: return "unavailable"
    names = item.get("features") or [item["feature"]]
    return f"{'+'.join(names)} AUC={_short(item['rank_auc'])} n={item['positive_n']}/{item['negative_n']}"


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle: handle.write(text); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--census", type=Path, default=DEFAULT_CENSUS)
    parser.add_argument("--population", type=Path, default=DEFAULT_POPULATION); parser.add_argument("--episode-tape-dir", type=Path, default=DEFAULT_EPISODE_TAPE)
    parser.add_argument("--control-tape-dir", type=Path, default=DEFAULT_CONTROL_TAPE); parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT); parser.add_argument("--workers", type=int, choices=range(1, 9), default=8); args = parser.parse_args(argv)
    getcontext().prec = 50
    try:
        if fine.tape_pull.stream_verified_member is not tape_pull.stream_verified_member: raise ProbeError("shared ZIP reader binding differs")
        selected, controls, bases, census_total = _load_inputs(args.census, args.population)
        windows, match_meta = _windows(selected, controls, bases, args.episode_tape_dir, args.control_tape_dir)
        tasks: Dict[Tuple[str, str, str], list[Dict[str, Any]]] = {}
        for row in windows: tasks.setdefault((row["_root"], row["symbol"], row["venue"]), []).append(row)
        scan_tasks = [(*key, value) for key, value in tasks.items()]
        if args.workers == 1: results = list(map(_scan_task, scan_tasks))
        else:
            with ThreadPoolExecutor(max_workers=args.workers) as executor: results = list(executor.map(_scan_task, scan_tasks))
        rows = sorted((row for result in results for row in result["rows"]), key=lambda row: row["_order"])
        for row in rows: row.pop("_order")
        distributions = {}
        for feature in FEATURES:
            grouped = {group: shape._stats([row["features"][feature] for row in rows if row["status"] == "MEASURED" and row["group"] == group and row["features"][feature] is not None]) for group in ("HELD", "COLLAPSED", "CONTROL")}
            base = shape._stats([row["features"][feature] for row in rows if row["status"] == "MEASURED" and row["group"] != "CONTROL" and row["features"][feature] is not None])
            grouped.update({"BASE": base, "base_control_overlap": _overlap(base, grouped["CONTROL"], "base", "control"),
                            "held_collapsed_overlap": _overlap(grouped["HELD"], grouped["COLLAPSED"], "HELD", "COLLAPSED")}); distributions[feature] = grouped
        base_control = _comparison(rows, lambda row: row["group"] != "CONTROL", lambda row: row["group"] == "CONTROL", "base")
        held_collapsed = _comparison(rows, lambda row: row["group"] == "HELD", lambda row: row["group"] == "COLLAPSED", "HELD")
        class_counts = {label: sum(row["class"] == label for row in selected) for label in ("HELD", "COLLAPSED")}
        coverage = {group: {status: sum(row["group"] == group and row["status"] == status for row in rows) for status in ("MEASURED", "NO_TRADES", "ARCHIVE_MISSING", "ARCHIVE_INVALID")} for group in ("HELD", "COLLAPSED", "CONTROL")}
        source = {"archives_streamed_valid": sum(result["archives"] for result in results), "archive_errors": [item for result in results for item in result["errors"]],
                  "aggregate_trade_rows_parsed": sum(result["trades"] for result in results), "zip_reader": "study.fine_slice_study._trade_rows using study.tape_pull.stream_verified_member"}
        definitions = {"same_side_runs": "consecutive taker-buy or taker-sell aggregate trades; mean and maximum use trade counts",
                       "longest_buy_run_quote_share": "quote volume of the buy run with largest trade count, then largest quote-volume tie-break, divided by window quote volume",
                       "buy_run_escalating_fraction": "strictly increasing quote size on every transition, among buy runs of at least two trades",
                       "iceberg_like_repeat_count": "maximal consecutive same-side exact-base-quantity sequences of at least three trades with each gap at most two seconds",
                       "size_deciles": "trade quote sizes sorted into equal-rank deciles; each value is that decile's quote-volume share",
                       "within_100ms_fraction": "after-first trades no more than 100 milliseconds after the prior aggregate trade"}
        summary = {"schema": "orderflow-probe-summary-v1", "question": "does final-hour per-trade sequencing separate bases from controls or HELD from COLLAPSED",
                   "parameters": {"window_minutes": 60, "episode_selection": "class-local zero-based index modulo 8 equals zero; first 400 candidates in census order",
                                  "control_match": "one per episode; same venue; round-robin symbol; same minute and nearest available UTC hour/date; no reuse or overlap; no same-symbol census base in the window expanded by 72h",
                                  "features": definitions, "pair_rule": "sum of comparison-oriented empirical ranks on complete common rows; in-sample descriptive"},
                   "population": {"census_episodes": census_total, "selected_episodes": len(selected), "selected_class_counts": class_counts, "matched_controls": 400} | match_meta,
                   "coverage": coverage, "source": source, "distributions": distributions,
                   "base_vs_control": base_control, "held_vs_collapsed": held_collapsed,
                   "answer": {"base_vs_control_clean_range_separation": bool(base_control["clean_range_separation"]), "held_vs_collapsed_clean_range_separation": bool(held_collapsed["clean_range_separation"])},
                   "limits": "in-sample descriptive ranks; aggregate trades identify aggressor side but not participant identity; no threshold proposed"}
        lines = [f"COVERAGE_METHOD | census={census_total}; class-local stride=8 then first 400 selected HELD/COLLAPSED={class_counts['HELD']}/{class_counts['COLLAPSED']}; controls=400; episode venue spot/futures={match_meta['episode_venue_counts']['spot']}/{match_meta['episode_venue_counts']['futures']}; measured H/C/K={coverage['HELD']['MEASURED']}/{coverage['COLLAPSED']['MEASURED']}/{coverage['CONTROL']['MEASURED']}; archives/errors={source['archives_streamed_valid']}/{len(source['archive_errors'])}; rows={source['aggregate_trade_rows_parsed']}; shared verified reader | MEASURED",
                 "SAME_SIDE_RUN_LENGTHS | max " + _dist_text(distributions["same_side_run_max"]) + " || mean " + _dist_text(distributions["same_side_run_mean"]) + " | MEASURED",
                 "LONGEST_BUY_RUN_QUOTE_SHARE | " + _dist_text(distributions["longest_buy_run_quote_share"]) + " | MEASURED",
                 "BUY_RUN_ESCALATING_FRACTION | " + _dist_text(distributions["buy_run_escalating_fraction"]) + " | MEASURED",
                 "ICEBERG_LIKE_REPEAT_COUNT | " + _dist_text(distributions["iceberg_like_repeat_count"]) + " | MEASURED",
                 "SIZE_DECILE_QUOTE_SHARES | " + " || ".join(f"d{index} {_dist_text(distributions[name])}" for index, name in enumerate(DECILES, 1)) + " | MEASURED",
                 "WITHIN_100MS_FRACTION | " + _dist_text(distributions["within_100ms_fraction"]) + " | MEASURED",
                 f"RANK_AUC_BASE_CONTROL | best single={_auc_text(base_control['best_single'])}; best pair={_auc_text(base_control['best_pair'])}; no threshold proposed | MEASURED",
                 f"RANK_AUC_HELD_COLLAPSED | best single={_auc_text(held_collapsed['best_single'])}; best pair={_auc_text(held_collapsed['best_pair'])}; no threshold proposed | MEASURED",
                 f"FLAT_ANSWER | clean observed-range separation base/control={'YES ' + ','.join(base_control['clean_range_separation']) if base_control['clean_range_separation'] else 'NO'}; held/collapsed={'YES ' + ','.join(held_collapsed['clean_range_separation']) if held_collapsed['clean_range_separation'] else 'NO'}; participant identity is unavailable in aggTrades; full suite, network, Git, production, host, deployment, and live actions not checked or used | MEASURED"]
        shape._write(args.output_dir / "window-features.jsonl", rows, lines=True); shape._write(args.output_dir / "orderflow-summary.json", summary)
        text = "\n".join(lines) + "\n"; _write_text(args.output_dir / "orderflow-distributions.txt", text); _write_text(args.report, text)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, tape_pull.TapePullError, shape.StudyError, ProbeError) as error:
        print(f"order-flow probe: {error}", file=sys.stderr); return 2
    print(f"OF1 | episodes={len(selected)} | controls=400 | output={args.output_dir}"); return 0


if __name__ == "__main__": raise SystemExit(main())
