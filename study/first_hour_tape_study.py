#!/usr/bin/env python3
"""Compare the first post-base hour on banked raw tape by episode class."""

from __future__ import annotations

import argparse, bisect, json, os, sys, tempfile
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

try:
    from study import absorption, tape_pull, tape_shape_study as shape
except ModuleNotFoundError as error:
    if error.name != "study": raise
    import absorption, tape_pull, tape_shape_study as shape  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CENSUS = ROOT / "lane-logs/AB13/pump-census-6mo.json"
DEFAULT_TAPE, DEFAULT_OUTPUT = ROOT / "study/data-episode-tape", ROOT / "lane-logs/AB20"
DEFAULT_REPORT = ROOT / "lane-logs/AB20-report.txt"
SLICE_US, HOUR_US, PRE_US, SLICE_COUNT = 10 * shape.MINUTE_US, shape.HOUR_US, 12 * shape.HOUR_US, 6
FEATURES = ("taker_buy_quote_share", "large_print_share", "large_trade_net_aggression",
            "trade_count_acceleration", "price_level_churn_per_1000", "top3_minute_volume_share")


class FirstHourError(Exception): pass


@dataclass
class TapeSlice:
    core: Any
    taker_buy_quote: Decimal = Decimal("0")
    minute_quote: Dict[int, Decimal] = field(default_factory=dict)

    def add(self, stamp: int, price: Decimal, quote: Decimal, maker: bool, p95: Optional[Decimal]) -> None:
        self.core.add(stamp, price, quote, maker, p95)
        if not maker: self.taker_buy_quote += quote
        minute = stamp // shape.MINUTE_US
        self.minute_quote[minute] = self.minute_quote.get(minute, Decimal("0")) + quote

    def values(self, p95_valid: bool, previous_count: Optional[int]) -> Dict[str, Optional[Decimal]]:
        base = self.core.features(); quote = self.core.quote
        acceleration = None if previous_count is None or previous_count <= 0 else Decimal(self.core.trades - previous_count) / Decimal(previous_count)
        top = sum(sorted(self.minute_quote.values(), reverse=True)[:3], Decimal("0"))
        return {"taker_buy_quote_share": None if quote <= 0 else self.taker_buy_quote / quote,
                "large_print_share": base["large_print_share"] if p95_valid else None,
                "large_trade_net_aggression": base["large_trade_net_aggression"] if p95_valid else None,
                "trade_count_acceleration": acceleration,
                "price_level_churn_per_1000": base["price_level_churn_per_1000"],
                "top3_minute_volume_share": None if quote <= 0 else top / quote}


@dataclass
class EpisodeState:
    raw: Mapping[str, Any]; base: int; pre_days: Tuple[int, ...]; post_days: Tuple[int, ...]
    pre_complete: bool; missing_pre: Tuple[str, ...]
    baseline: Any = field(init=False); slices: list[TapeSlice] = field(init=False)
    baseline_salt: int = field(init=False)
    p95: Optional[Decimal] = None; p95_ready: bool = False
    running_high: Optional[Decimal] = None; max_drawdown: Optional[Decimal] = None; pre_trades: int = 0; post_trades: int = 0
    invalid_pre: set[str] = field(default_factory=set); invalid_post: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.baseline = shape.PriorityReservoir(shape.MONTH_RESERVOIR)
        self.baseline_salt = shape._salt(f"AB20-pre:{self.raw['episode_id']}")
        self.slices = [TapeSlice(shape.SliceAccumulator(shape._salt(f"AB20:{self.raw['episode_id']}:{index}"))) for index in range(SLICE_COUNT)]

    @property
    def start(self) -> int: return self.base - PRE_US

    @property
    def end(self) -> int: return self.base + HOUR_US

    def prepare_p95(self) -> None:
        if self.p95_ready: return
        sample = self.baseline.values()
        self.p95 = absorption.quantile(sample, 95, 100) if self.pre_complete and sample else None
        self.p95_ready = True

    def add(self, aggregate_id: int, stamp: int, price: Decimal, quantity: Decimal, maker: bool) -> None:
        quote = price * quantity
        if stamp < self.base:
            self.baseline.offer(shape._priority(stamp, aggregate_id, self.baseline_salt), stamp, aggregate_id, quote); self.pre_trades += 1; return
        self.prepare_p95(); index = (stamp - self.base) // SLICE_US
        self.slices[index].add(stamp, price, quote, maker, self.p95); self.post_trades += 1
        if self.running_high is None or price > self.running_high: self.running_high = price
        drawdown = (self.running_high - price) / self.running_high
        if self.max_drawdown is None or drawdown > self.max_drawdown: self.max_drawdown = drawdown

    def output(self) -> Dict[str, Any]:
        self.prepare_p95(); post_valid = not self.invalid_post
        p95_valid = self.pre_complete and not self.invalid_pre and self.p95 is not None
        status = ("POST_TAPE_INVALID" if not post_valid else "MEASURED" if p95_valid else "MEASURED_PRE_BASELINE_UNAVAILABLE")
        rows, previous = [], None
        for index, item in enumerate(self.slices):
            features = item.values(p95_valid, previous) if post_valid else {name: None for name in FEATURES}
            rows.append({"slice_index": index, "slice_start_utc": shape._utc_text(self.base + index * SLICE_US),
                         "slice_end_utc_exclusive": shape._utc_text(self.base + (index + 1) * SLICE_US),
                         "trade_count": item.core.trades if post_valid else None, "features": features})
            previous = item.core.trades if post_valid else None
        return _episode_row(self.raw, self.pre_days, self.post_days, self.missing_pre, (), status,
                            rows, self.p95 if p95_valid else None, self.pre_trades, len(self.baseline.values()),
                            self.max_drawdown if post_valid else None, sorted(self.invalid_pre), sorted(self.invalid_post))


def _days(row: Mapping[str, Any]) -> Tuple[int, Tuple[int, ...], Tuple[int, ...]]:
    base = shape._utc_us(row["base_utc"])
    return base, tuple(sorted(shape._required_days(base - PRE_US, base))), tuple(sorted(shape._required_days(base, base + HOUR_US)))


def _path(tape: Path, row: Mapping[str, Any], day: int) -> Path:
    return shape._archive(tape, row["symbol"], row["venue"], day, "aggTrades")


def _episode_row(raw: Mapping[str, Any], pre_days: Sequence[int], post_days: Sequence[int], missing_pre: Sequence[str], missing_post: Sequence[str],
                 status: str, slices: Sequence[Mapping[str, Any]], p95: Optional[Decimal] = None, seen: int = 0, sample: int = 0,
                 drawdown: Optional[Decimal] = None, invalid_pre: Sequence[str] = (), invalid_post: Sequence[str] = ()) -> Dict[str, Any]:
    return {"schema": "first-hour-tape-episode-v1", "episode_id": raw["episode_id"], "symbol": raw["symbol"], "venue": raw["venue"],
            "class": raw["class"], "base_utc": raw["base_utc"], "sampling_selected": status != "NOT_SELECTED_STRIDE",
            "status": status, "required_pre_archive_days": [shape._day_text(day) for day in pre_days],
            "required_post_archive_days": [shape._day_text(day) for day in post_days], "missing_pre_archive_days": list(missing_pre),
            "missing_post_archive_days": list(missing_post), "invalid_pre_archive_days": list(invalid_pre),
            "invalid_post_archive_days": list(invalid_post), "pre_base_p95_quote": p95, "pre_base_trade_count": seen,
            "pre_base_p95_sample_size": sample, "pre_base_p95_sample_cap": shape.MONTH_RESERVOIR,
            "slices": list(slices), "max_drawdown_from_running_high": drawdown}


def _scan_group(task: Tuple[str, str, str, list[Mapping[str, Any]]]) -> Dict[str, Any]:
    tape_text, symbol, venue, raw_rows = task; getcontext().prec = 50; tape = Path(tape_text)
    states, by_pre, by_post, paths = [], {}, {}, {}
    for raw in raw_rows:
        base, pre_days, post_days = _days(raw); missing = tuple(shape._day_text(day) for day in pre_days if not _path(tape, raw, day).is_file())
        state = EpisodeState(raw, base, pre_days, post_days, not missing, missing); states.append(state)
        for day in pre_days: by_pre.setdefault(day, []).append(state)
        for day in post_days: by_post.setdefault(day, []).append(state)
        for day in set(pre_days) | set(post_days):
            path = _path(tape, raw, day)
            if path.is_file(): paths[day] = path
    ordered = sorted(states, key=lambda item: (item.start, item.end, item.raw["episode_id"])); active = []; cursor = parsed = archives = 0; errors = []
    for day, path in sorted(paths.items()):
        previous = None
        try:
            for aggregate_id, stamp, price, quantity, maker in shape._trade_rows(path):
                key = stamp, aggregate_id
                if not day <= stamp < day + shape.DAY_US or (previous is not None and key <= previous): raise FirstHourError(f"{path.name} ordering differs")
                previous = key; parsed += 1
                while cursor < len(ordered) and ordered[cursor].start <= stamp: active.append(ordered[cursor]); cursor += 1
                active = [state for state in active if stamp < state.end]
                for state in active: state.add(aggregate_id, stamp, price, quantity, maker)
            archives += 1
        except (shape.StudyError, FirstHourError, OSError) as error:
            for state in by_pre.get(day, []): state.invalid_pre.add(shape._day_text(day))
            for state in by_post.get(day, []): state.invalid_post.add(shape._day_text(day))
            errors.append({"archive": path.name, "error": type(error).__name__})
    return {"symbol": symbol, "venue": venue, "rows": [state.output() for state in states],
            "archives": archives, "trades": parsed, "errors": errors}


def _load(path: Path) -> list[Mapping[str, Any]]:
    payload = shape._read(path); rows = payload.get("episodes") if isinstance(payload, Mapping) else None
    if payload.get("header", {}).get("schema") != "pump-census-6mo-v1" or not isinstance(rows, list) or payload.get("summary", {}).get("episodes") != len(rows):
        raise FirstHourError("census schema or episode total differs")
    seen = set()
    for row in rows:
        if not isinstance(row, Mapping) or row.get("class") not in {"HELD", "COLLAPSED"} or row.get("episode_id") in seen: raise FirstHourError("census episode identity differs")
        tape_pull._validate_symbol(row.get("symbol")); tape_pull._validate_venue(row.get("venue")); base, _, _ = _days(row)
        if base % shape.MINUTE_US: raise FirstHourError(f"{row.get('episode_id')} base is not minute aligned")
        seen.add(row["episode_id"])
    return rows


def _stats(rows: Sequence[Mapping[str, Any]], feature: str, index: Optional[int]) -> Dict[str, Any]:
    found = {}
    for label in ("HELD", "COLLAPSED"):
        values = []
        for row in rows:
            if row["class"] != label or not row["status"].startswith("MEASURED"): continue
            value = row["max_drawdown_from_running_high"] if index is None else row["slices"][index]["features"][feature]
            if value is not None: values.append(value)
        found[label] = shape._stats(values)
    held, collapsed = found["HELD"], found["COLLAPSED"]
    found["overlap"] = ("unavailable" if held["median"] is None or collapsed["p10"] is None else
                        "HELD median below COLLAPSED p10-p90" if held["median"] < collapsed["p10"] else
                        "HELD median above COLLAPSED p10-p90" if held["median"] > collapsed["p90"] else
                        "HELD median inside COLLAPSED p10-p90")
    return found


def _matrix(rows: Sequence[Mapping[str, Any]]) -> Tuple[Dict[str, Dict[str, Decimal]], Dict[str, Tuple[str, int]]]:
    matrix, variables = {}, {}
    for row in rows:
        if not row["status"].startswith("MEASURED"): continue
        values = {}
        for index, item in enumerate(row["slices"]):
            for feature in FEATURES:
                value = item["features"][feature]
                if value is not None:
                    key = f"{feature}:s{index + 1}"; values[key] = value; variables[key] = (feature, index)
        matrix[row["episode_id"]] = values
    return matrix, variables


def _best(rows: Sequence[Mapping[str, Any]]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    matrix, variables = _matrix(rows); labels = {row["episode_id"]: row["class"] for row in rows}; singles, ranks = {}, {}
    for key in sorted(variables):
        held = [values[key] for episode, values in matrix.items() if key in values and labels[episode] == "HELD"]
        collapsed = [values[key] for episode, values in matrix.items() if key in values and labels[episode] == "COLLAPSED"]
        raw = shape._auc(held, collapsed)
        if raw is None: continue
        direction, auc = ("high", raw) if raw >= Decimal("0.5") else ("low", Decimal("1") - raw)
        singles[key] = {"feature": variables[key][0], "slice_index": variables[key][1], "direction_for_HELD": direction,
                        "rank_auc": auc, "held_n": len(held), "collapsed_n": len(collapsed)}
        ordered = sorted(held + collapsed); ranks[key] = {}
        for episode, values in matrix.items():
            if key not in values: continue
            left, right = bisect.bisect_left(ordered, values[key]), bisect.bisect_right(ordered, values[key])
            ranks[key][episode] = Decimal(left + right - 1) / Decimal(2 * max(1, len(ordered) - 1))
    best_single = min(singles.values(), key=lambda item: (-item["rank_auc"], item["feature"], item["slice_index"])) if singles else None
    best_pair = None; keys = sorted(singles)
    for left_index, left in enumerate(keys):
        for right in keys[left_index + 1:]:
            if variables[left][0] == variables[right][0]: continue
            common = sorted(set(ranks[left]) & set(ranks[right])); sign_left = Decimal("1") if singles[left]["direction_for_HELD"] == "high" else Decimal("-1")
            sign_right = Decimal("1") if singles[right]["direction_for_HELD"] == "high" else Decimal("-1")
            held = [sign_left * ranks[left][key] + sign_right * ranks[right][key] for key in common if labels[key] == "HELD"]
            collapsed = [sign_left * ranks[left][key] + sign_right * ranks[right][key] for key in common if labels[key] == "COLLAPSED"]
            raw_auc = shape._auc(held, collapsed)
            if raw_auc is None: continue
            score_direction, auc = ("high", raw_auc) if raw_auc >= Decimal("0.5") else ("low", Decimal("1") - raw_auc)
            candidate = {"members": [{"feature": variables[key][0], "slice_index": variables[key][1], "direction_for_HELD": singles[key]["direction_for_HELD"]} for key in (left, right)],
                         "score_direction_for_HELD": score_direction, "rank_auc": auc, "held_n": len(held), "collapsed_n": len(collapsed)}
            signature = tuple((item["feature"], item["slice_index"]) for item in candidate["members"])
            if best_pair is None or auc > best_pair["rank_auc"] or (auc == best_pair["rank_auc"] and signature < best_pair["signature"]): candidate["signature"] = signature; best_pair = candidate
    if best_pair is not None: best_pair.pop("signature")
    return best_single, best_pair


def _short(value: Optional[Decimal]) -> str: return "NA" if value is None else format(value, ".7g")


def _dist_text(stats: Mapping[str, Any]) -> str:
    parts = []
    for label, short in (("HELD", "H"), ("COLLAPSED", "C")):
        row = stats[label]; parts.append(f"{short} n={row['count']} min/med/p90/max=" + "/".join(_short(row[key]) for key in ("min", "median", "p90", "max")))
    return "; ".join(parts) + f"; {stats['overlap']}"


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle: handle.write(text); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def _task_weight(task: Tuple[str, str, str, list[Mapping[str, Any]]]) -> int:
    tape, paths = Path(task[0]), set()
    for row in task[3]:
        _, pre, post = _days(row)
        paths.update(_path(tape, row, day) for day in set(pre) | set(post) if _path(tape, row, day).is_file())
    return sum(path.stat().st_size for path in paths)


def _restore(result: Dict[str, Any]) -> Dict[str, Any]:
    for row in result["rows"]:
        for key in ("pre_base_p95_quote", "max_drawdown_from_running_high"):
            if row[key] is not None: row[key] = Decimal(row[key])
        for item in row["slices"]:
            for key, value in item["features"].items():
                if value is not None: item["features"][key] = Decimal(value)
    return result


def _parallel_scan(tasks: Sequence[Tuple[str, str, str, list[Mapping[str, Any]]]], workers: int) -> list[Dict[str, Any]]:
    if workers == 1: return [_scan_group(task) for task in tasks]
    count = min(workers, len(tasks)); ordered = sorted(tasks, key=_task_weight, reverse=True); shards = [ordered[index::count] for index in range(count)]
    children = []
    for index, shard in enumerate(shards):
        descriptor, name = tempfile.mkstemp(prefix=f"ab20-worker-{index}-", suffix=".json"); os.close(descriptor); path = Path(name); pid = os.fork()
        if pid == 0:
            try: shape._write(path, [_scan_group(task) for task in shard]); code = 0
            except BaseException as error: shape._write(path, {"worker_error": f"{type(error).__name__}: {error}"}); code = 2
            os._exit(code)
        children.append((pid, path))
    results = []
    for pid, path in children:
        _, status = os.waitpid(pid, 0); payload = shape._read(path)
        try: path.unlink()
        except FileNotFoundError: pass
        if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0 or not isinstance(payload, list):
            detail = payload.get("worker_error", "unknown") if isinstance(payload, Mapping) else "unknown"
            raise FirstHourError(f"worker {pid} did not complete: {detail}")
        results.extend(_restore(result) for result in payload)
    return results


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--census", type=Path, default=DEFAULT_CENSUS)
    parser.add_argument("--tape-dir", type=Path, default=DEFAULT_TAPE); parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT); parser.add_argument("--episode-stride", type=int, choices=range(1, 1001), default=2)
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=4); args = parser.parse_args(argv); getcontext().prec = 50
    try:
        raw_rows = _load(args.census); selected = [row for index, row in enumerate(raw_rows) if index % args.episode_stride == 0]
        groups, coverage = {}, {"population": {"post_full": 0, "post_partial": 0}, "selected": {"post_full": 0, "post_partial": 0}}
        info = {}
        for index, row in enumerate(raw_rows):
            base, pre_days, post_days = _days(row); missing_pre = tuple(shape._day_text(day) for day in pre_days if not _path(args.tape_dir, row, day).is_file())
            missing_post = tuple(shape._day_text(day) for day in post_days if not _path(args.tape_dir, row, day).is_file()); chosen = index % args.episode_stride == 0
            info[row["episode_id"]] = (pre_days, post_days, missing_pre, missing_post, chosen)
            coverage["population"]["post_partial" if missing_post else "post_full"] += 1
            if chosen: coverage["selected"]["post_partial" if missing_post else "post_full"] += 1
            if chosen and not missing_post: groups.setdefault((row["symbol"], row["venue"]), []).append(row)
        measured, archives, trades, errors = {}, 0, 0, []
        tasks = [(str(args.tape_dir), symbol, venue, rows) for (symbol, venue), rows in sorted(groups.items())]
        for count, result in enumerate(_parallel_scan(tasks, args.workers), 1):
            archives += result["archives"]; trades += result["trades"]; errors.extend(result["errors"]); measured.update({row["episode_id"]: row for row in result["rows"]})
            if count % 20 == 0 or count == len(tasks): print(f"AB20 progress | groups={count}/{len(tasks)} | episodes={len(measured)}", flush=True)
        episode_rows = []
        for row in raw_rows:
            pre_days, post_days, missing_pre, missing_post, chosen = info[row["episode_id"]]
            if row["episode_id"] in measured: episode_rows.append(measured[row["episode_id"]]); continue
            status = "POST_TAPE_PARTIAL" if chosen else "NOT_SELECTED_STRIDE"
            empty = [{"slice_index": index, "slice_start_utc": shape._utc_text(_days(row)[0] + index * SLICE_US),
                      "slice_end_utc_exclusive": shape._utc_text(_days(row)[0] + (index + 1) * SLICE_US),
                      "trade_count": None, "features": {name: None for name in FEATURES}} for index in range(SLICE_COUNT)]
            episode_rows.append(_episode_row(row, pre_days, post_days, missing_pre, missing_post, status, empty))
        distributions = {feature: {f"slice_{index + 1}": _stats(episode_rows, feature, index) for index in range(SLICE_COUNT)} for feature in FEATURES}
        drawdown = _stats(episode_rows, "max_drawdown_from_running_high", None); best_single, best_pair = _best(episode_rows)
        clean = []
        for feature in FEATURES:
            for index in range(SLICE_COUNT):
                stat = distributions[feature][f"slice_{index + 1}"]; held, collapsed = stat["HELD"], stat["COLLAPSED"]
                if held["count"] and collapsed["count"] and (held["min"] > collapsed["max"] or held["max"] < collapsed["min"]): clean.append(f"{feature}:s{index + 1}")
        if drawdown["HELD"]["count"] and drawdown["COLLAPSED"]["count"] and (drawdown["HELD"]["min"] > drawdown["COLLAPSED"]["max"] or drawdown["HELD"]["max"] < drawdown["COLLAPSED"]["min"]): clean.append("max_drawdown:hour")
        measured_valid = [row for row in episode_rows if row["status"].startswith("MEASURED")]
        coverage["selected"].update({"measured_post_hours": len(measured_valid), "post_invalid": sum(row["status"] == "POST_TAPE_INVALID" for row in episode_rows),
                                     "pre_baseline_unavailable": sum(row["status"] == "MEASURED_PRE_BASELINE_UNAVAILABLE" for row in episode_rows)})
        summary = {"schema": "first-hour-tape-study-v1", "question": "does first-hour raw tape separate HELD from COLLAPSED episodes",
                   "parameters": {"episode_stride": args.episode_stride, "selection": "zero-based census index modulo stride equals zero", "slice_minutes": 10,
                                  "slices": SLICE_COUNT, "pre_base_hours": 12, "large_print_rule": "quote size strictly above deterministic pre-base p95",
                                  "pre_base_p95_sample_cap": shape.MONTH_RESERVOIR, "trade_count_acceleration": "(current slice count - prior slice count) / prior slice count; unknown for slice 1 or zero prior count",
                                  "price_level_rule": f"exact distinct prices per 1000 trades up to {shape.PRICE_LEVEL_CAP} levels, otherwise unknown",
                                  "burst_rule": "top three one-minute quote-volume buckets divided by slice quote volume",
                                  "drawdown_rule": "largest (running raw-trade high - later raw-trade price) / running high in the post-base hour",
                                  "pair_rule": "sum of class-oriented empirical ranks on common episodes; members must be different features"},
                   "coverage": coverage, "population": {"episodes": len(raw_rows), "selected": len(selected), "class_counts": {label: sum(row["class"] == label for row in raw_rows) for label in ("HELD", "COLLAPSED")},
                                  "selected_class_counts": {label: sum(row["class"] == label for row in selected) for label in ("HELD", "COLLAPSED")}},
                   "source": {"archives_streamed_valid": archives, "archive_errors": errors, "aggregate_trade_rows_parsed": trades,
                              "zip_reader": "study.tape_shape_study._trade_rows using study.tape_pull.stream_verified_member"},
                   "distributions": distributions, "hour_price_path": {"max_drawdown_from_running_high": drawdown},
                   "best_single_feature_slice": best_single, "best_two_feature_pair": best_pair, "clean_separation": clean,
                   "answer": "YES" if clean else "NO", "limits": "deterministic every-other census subset; in-sample descriptive ranks; no threshold proposed"}
        lines = [f"COVERAGE_METHOD | population={len(raw_rows)} HELD={summary['population']['class_counts']['HELD']} COLLAPSED={summary['population']['class_counts']['COLLAPSED']}; stride={args.episode_stride} selected={len(selected)}; population post-hour full/partial={coverage['population']['post_full']}/{coverage['population']['post_partial']}; selected full/partial={coverage['selected']['post_full']}/{coverage['selected']['post_partial']}; measured={coverage['selected']['measured_post_hours']}; pre-baseline unavailable={coverage['selected']['pre_baseline_unavailable']}; archives valid/errors={archives}/{len(errors)}; rows parsed={trades}; p95 sample cap={shape.MONTH_RESERVOIR}; shared verified reader | MEASURED"]
        for feature in FEATURES: lines.append(feature.upper() + " | " + " || ".join(f"s{index + 1} {_dist_text(distributions[feature][f'slice_{index + 1}'])}" for index in range(SLICE_COUNT)) + " | MEASURED")
        lines.append("MAX_DRAWDOWN_FROM_RUNNING_HIGH | hour " + _dist_text(drawdown) + " | MEASURED")
        single_text = "unavailable" if best_single is None else f"{best_single['feature']}:s{best_single['slice_index'] + 1} direction={best_single['direction_for_HELD']} AUC={_short(best_single['rank_auc'])} n={best_single['held_n']}/{best_single['collapsed_n']}"
        pair_text = "unavailable" if best_pair is None else "+".join(f"{item['feature']}:s{item['slice_index'] + 1}:{item['direction_for_HELD']}" for item in best_pair["members"]) + f" score_direction={best_pair['score_direction_for_HELD']} AUC={_short(best_pair['rank_auc'])} n={best_pair['held_n']}/{best_pair['collapsed_n']}"
        lines.append(f"RANK_AUC | best single={single_text}; best two={pair_text}; in-sample descriptive; no threshold proposed | MEASURED")
        lines.append(f"CLEAN_SEPARATION_AND_CHECKS | any feature cleanly separates classes={'YES ' + ','.join(clean) if clean else 'NO'}; py_compile and real-run checks pending; full suite, network, Git, production, host, deployment, and live actions not checked or used | MEASURED")
        shape._write(args.output_dir / "episode-features.jsonl", episode_rows, lines=True); shape._write(args.output_dir / "first-hour-summary.json", summary)
        report_text = "\n".join(lines) + "\n"; _write_text(args.output_dir / "first-hour-distributions.txt", report_text); _write_text(args.report, report_text)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, tape_pull.TapePullError, shape.StudyError, FirstHourError) as error:
        print(f"first hour tape study: {error}", file=sys.stderr); return 2
    print(f"AB20 | selected={len(selected)} | measured={coverage['selected']['measured_post_hours']} | output={args.output_dir}"); return 0


if __name__ == "__main__": raise SystemExit(main())
