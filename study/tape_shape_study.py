#!/usr/bin/env python3
"""Measure deep tape shape before AB8 pumps against two quiet controls."""

from __future__ import annotations

import argparse, bisect, csv, heapq, io, json, os, sys, zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

try: from study import absorption, tape_pull
except ImportError:
    import absorption  # type: ignore
    import tape_pull  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = Path("/Users/cosmosgeek/Desktop/quant/study/data")
DEFAULT_CENSUS, DEFAULT_PRIORS = ROOT / "lane-logs/AB8/pump-census.json", ROOT / "lane-logs/AB8/pump-priors.jsonl"
DEFAULT_POPULATION, DEFAULT_OUTPUT = ROOT / "lane-logs/AB9/population.json", ROOT / "lane-logs/AB11"
DAY_US, HOUR_US, MINUTE_US = 86_400_000_000, 3_600_000_000, 60_000_000
SLICE_US, STRETCH_US, MONTH_DAYS = 4 * HOUR_US, 48 * HOUR_US, 30
MONTH_RESERVOIR, SLICE_RESERVOIR, PRICE_LEVEL_CAP = 50_000, 10_000, 20_000
MASK, EPOCH = (1 << 64) - 1, datetime(1970, 1, 1, tzinfo=timezone.utc)
FEATURES = ("trade_size_p50_quote", "trade_size_p90_quote", "trade_size_p99_quote", "large_print_share",
            "large_trade_net_aggression", "interarrival_cv", "price_level_churn_per_1000")


class StudyError(Exception): pass


def _read(path: Path) -> Any:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error: raise StudyError(f"cannot read {path.name}: {error}") from None


def _utc_us(raw: str) -> int:
    try: value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError): raise StudyError("UTC timestamp differs") from None
    if value.tzinfo is None: raise StudyError("UTC timestamp lacks a zone")
    delta = value.astimezone(timezone.utc) - EPOCH; return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _utc_text(value: Optional[int]) -> Optional[str]:
    return None if value is None else (EPOCH + timedelta(microseconds=value)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _day_text(value: int) -> str: return _utc_text(value)[:10]  # type: ignore[index]


def _venue_root(root: Path, venue: str) -> Path:
    if venue not in {"spot", "futures"}: raise StudyError("venue differs")
    return root / "spot" if venue == "spot" else root / "futures" / "um"


def _archive(root: Path, symbol: str, venue: str, day: int, kind: str) -> Path:
    source, raw_day = _venue_root(root, venue), _day_text(day)
    return (source / "daily" / kind / symbol / f"{symbol}-{kind}-{raw_day}.zip" if kind == "aggTrades" else source / "daily" / "klines" / symbol / "1m" / f"{symbol}-1m-{raw_day}.zip")


def _decimal(raw: str) -> Decimal:
    try: value = Decimal(raw.strip())
    except (InvalidOperation, ValueError): raise StudyError("trade Decimal differs") from None
    if not value.is_finite() or value <= 0: raise StudyError("trade Decimal is not positive")
    return value


class _ChunkStream(io.RawIOBase):
    """Expose shared verified chunks as a buffered-reader source."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        super().__init__()
        self._chunks = iter(chunks)
        self._pending = memoryview(b"")

    def readable(self) -> bool:
        return True

    def readinto(self, target: Any) -> int:
        view = memoryview(target)
        while len(self._pending) == 0:
            try:
                self._pending = memoryview(next(self._chunks))
            except StopIteration:
                return 0
        size = min(len(view), len(self._pending))
        view[:size] = self._pending[:size]
        self._pending = self._pending[size:]
        return size

    def close(self) -> None:
        close_chunks = getattr(self._chunks, "close", None)
        if callable(close_chunks):
            close_chunks()
        super().close()


def _trade_rows(path: Path) -> Iterable[Tuple[int, int, Decimal, Decimal, bool]]:
    expected = path.stem + ".csv"
    identity = f"{path.name}:{expected}"
    try:
        with zipfile.ZipFile(path) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) != 1 or members[0].filename != expected or members[0].flag_bits & 1:
                raise StudyError(f"{path.name} member differs")
            chunks = tape_pull.stream_verified_member(
                archive, members[0], tape_pull.MAX_MEMBER_BYTES
            )
            raw = io.BufferedReader(_ChunkStream(chunks))
            with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as handle:
                found = False
                for row in csv.reader(handle):
                    if not row or not any(cell.strip() for cell in row): continue
                    if row[0].strip().lower().replace(" ", "_") in {"agg_trade_id", "aggregate_trade_id", "aggtradeid"}: continue
                    if len(row) < 7: raise StudyError(f"{path.name} row width differs")
                    aggregate_id, first_id, last_id = int(row[0]), int(row[3]), int(row[4])
                    if min(aggregate_id, first_id, last_id) < 0 or last_id < first_id: raise StudyError(f"{path.name} trade ids differ")
                    stamp = int(row[5]); stamp_us = stamp if stamp >= 100_000_000_000_000 else stamp * 1_000
                    if not 100_000_000_000_000 <= stamp_us < 100_000_000_000_000_000: raise StudyError(f"{path.name} time differs")
                    maker = row[6].strip().lower()
                    if maker not in {"true", "false"}: raise StudyError(f"{path.name} maker field differs")
                    found = True; yield aggregate_id, stamp_us, _decimal(row[1]), _decimal(row[2]), maker == "true"
                if not found: raise StudyError(f"{path.name} has no trades")
    except StudyError:
        raise
    except tape_pull.ArchiveError as error:
        details = ""
        if error.measured_value is not None:
            details += f" | measured={error.measured_value}"
        if error.bound is not None:
            details += f" | bound={error.bound}"
        raise StudyError(f"{identity} {error}{details}") from None
    except (OSError, UnicodeError, csv.Error, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise StudyError(f"cannot parse {path.name}: {type(error).__name__}") from None


def _salt(raw: str) -> int:
    value = 14_695_981_039_346_656_037
    for byte in raw.encode("utf-8"): value = ((value ^ byte) * 1_099_511_628_211) & MASK
    return value


def _priority(stamp: int, aggregate_id: int, salt: int) -> int:
    value = (stamp ^ (aggregate_id * 0x9E3779B97F4A7C15) ^ salt) & MASK
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK; value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK
    return value ^ (value >> 31)


class PriorityReservoir:
    def __init__(self, cap: int) -> None: self.cap, self.heap = cap, []
    def offer(self, priority: int, stamp: int, aggregate_id: int, value: Decimal) -> None:
        item = (-priority, -stamp, -aggregate_id, value)
        if len(self.heap) < self.cap: heapq.heappush(self.heap, item)
        elif item > self.heap[0]: heapq.heapreplace(self.heap, item)
    def merge(self, other: "PriorityReservoir") -> None:
        for priority, stamp, aggregate_id, value in other.heap: self.offer(-priority, -stamp, -aggregate_id, value)
    def values(self) -> list[Decimal]: return [item[3] for item in self.heap]


class Reservoir:
    def __init__(self, cap: int, salt: int) -> None: self.cap, self.salt, self.state, self.seen, self.values = cap, salt, salt or 1, 0, []
    def add(self, value: Decimal) -> None:
        self.seen += 1; self.state = (6_364_136_223_846_793_005 * self.state + 1_442_695_040_888_963_407) & MASK
        if len(self.values) < self.cap: self.values.append(value)
        else:
            index = self.state % self.seen
            if index < self.cap: self.values[index] = value


class SliceAccumulator:
    def __init__(self, salt: int) -> None:
        self.sizes, self.trades, self.previous, self.gaps = Reservoir(SLICE_RESERVOIR, salt), 0, None, 0
        self.quote = self.large_quote = self.large_net = self.gap_mean = self.gap_m2 = Decimal("0"); self.large_trades = 0; self.prices, self.price_overflow = set(), False
    def add(self, stamp: int, price: Decimal, quote: Decimal, maker: bool, p95: Optional[Decimal]) -> None:
        self.trades += 1; self.sizes.add(quote); self.quote += quote
        if p95 is not None and quote > p95:
            self.large_trades += 1; self.large_quote += quote; self.large_net += -quote if maker else quote
        if self.previous is not None:
            gap = Decimal(stamp - self.previous); self.gaps += 1; delta = gap - self.gap_mean
            self.gap_mean += delta / Decimal(self.gaps); self.gap_m2 += delta * (gap - self.gap_mean)
        self.previous = stamp
        if not self.price_overflow:
            if price in self.prices or len(self.prices) < PRICE_LEVEL_CAP: self.prices.add(price)
            else: self.prices.clear(); self.price_overflow = True
    def features(self) -> Dict[str, Optional[Decimal]]:
        sizes = self.sizes.values; share = self.large_quote / self.quote if self.quote > 0 else None
        aggression = self.large_net / self.large_quote if self.large_quote > 0 else None; burst = None
        if self.gaps and self.gap_mean > 0: burst = max(Decimal("0"), self.gap_m2 / Decimal(self.gaps)).sqrt() / self.gap_mean
        churn = None if self.price_overflow or not self.trades else Decimal(len(self.prices)) * Decimal("1000") / Decimal(self.trades)
        return {"trade_size_p50_quote": absorption.quantile(sizes, 1, 2), "trade_size_p90_quote": absorption.quantile(sizes, 9, 10),
                "trade_size_p99_quote": absorption.quantile(sizes, 99, 100), "large_print_share": share,
                "large_trade_net_aggression": aggression, "interarrival_cv": burst, "price_level_churn_per_1000": churn}


def _required_days(start: int, end: int) -> set[int]:
    return {value * DAY_US for value in range(start // DAY_US, (end - 1) // DAY_US + 1)}


def _month_baseline(root: Path, symbol: str, venue: str, start: int) -> Dict[str, Any]:
    sample, valid, invalid, trades, salt = PriorityReservoir(MONTH_RESERVOIR), set(), set(), 0, _salt(symbol)
    for day in (start + index * DAY_US for index in range(MONTH_DAYS)):
        path = _archive(root, symbol, venue, day, "aggTrades")
        if not path.is_file(): continue
        daily, previous, count = PriorityReservoir(MONTH_RESERVOIR), None, 0
        try:
            for aggregate_id, stamp, price, quantity, _maker in _trade_rows(path):
                key = (stamp, aggregate_id)
                if not day <= stamp < day + DAY_US or (previous is not None and key <= previous): raise StudyError(f"{path.name} ordering differs")
                previous, count = key, count + 1; daily.offer(_priority(stamp, aggregate_id, salt), stamp, aggregate_id, price * quantity)
        except StudyError: invalid.add(day); continue
        valid.add(day); trades += count; sample.merge(daily)
    values = sample.values(); return {"valid": valid, "invalid": invalid, "trade_count": trades, "sample": values, "p95": absorption.quantile(values, 95, 100)}


def _quiet_start(root: Path, symbol: str, venue: str, month: int, valid_tape: set[int]) -> Tuple[Optional[int], Dict[str, int]]:
    buckets: Dict[int, Tuple[int, Decimal]] = {}; valid_kline, invalid_kline = set(), set()
    for day in (month + index * DAY_US for index in range(MONTH_DAYS)):
        path = _archive(root, symbol, venue, day, "klines")
        if not path.is_file(): continue
        daily: Dict[int, Tuple[int, Decimal]] = {}; previous = None
        try:
            for bar in absorption._kline_rows(path, symbol):
                stamp = bar.open_ms * 1_000
                if not day <= stamp < day + DAY_US or stamp % MINUTE_US or (previous is not None and stamp <= previous): raise StudyError(f"{path.name} ordering differs")
                previous = stamp; bucket = (stamp - month) // SLICE_US; count, volume = daily.get(bucket, (0, Decimal("0")))
                daily[bucket] = (count + 1, volume + bar.quote_volume)
        except (StudyError, absorption.AbsorptionError): invalid_kline.add(day); continue
        valid_kline.add(day); buckets.update(daily)
    candidates = []
    for index in range(MONTH_DAYS * 6 - 11):
        start = month + index * SLICE_US; parts = [buckets.get(index + offset) for offset in range(12)]
        if all(part is not None and part[0] == 240 for part in parts) and _required_days(start, start + STRETCH_US) <= valid_tape:
            candidates.append((sum((part[1] for part in parts if part is not None), Decimal("0")), start))
    return (min(candidates)[1] if candidates else None), {"valid_kline_days": len(valid_kline), "invalid_kline_days": len(invalid_kline), "candidates": len(candidates)}


def _stretch(group: str, symbol: str, venue: str, key: str, start: Optional[int], valid: set[int], invalid: set[int], metadata: Mapping[str, Any]) -> Dict[str, Any]:
    slices = []
    for index in range(12):
        left = None if start is None else start + index * SLICE_US; right = None if left is None else left + SLICE_US
        needed = set() if left is None else _required_days(left, right)  # type: ignore[arg-type]
        status = "NO_QUIET_WINDOW" if left is None else ("TAPE_PRESENT" if needed <= valid else ("TAPE_INVALID" if needed & invalid else "TAPE_ABSENT"))
        slices.append({"index": index, "start": left, "end": right, "status": status,
                       "accumulator": SliceAccumulator(_salt(f"{symbol}:{key}:{index}")) if status == "TAPE_PRESENT" else None})
    return {"group": group, "symbol": symbol, "venue": venue, "stretch_id": key, "start": start, "end": None if start is None else start + STRETCH_US, "metadata": dict(metadata), "slices": slices}


def _measure(root: Path, symbol: str, venue: str, baseline: Mapping[str, Any], stretches: Sequence[Dict[str, Any]]) -> None:
    days = set()
    for stretch in stretches:
        for item in stretch["slices"]:
            if item["status"] == "TAPE_PRESENT": days.update(_required_days(item["start"], item["end"]))
    for day in sorted(days):
        for aggregate_id, stamp, price, quantity, maker in _trade_rows(_archive(root, symbol, venue, day, "aggTrades")):
            quote = price * quantity
            for stretch in stretches:
                if stretch["start"] is None or not stretch["start"] <= stamp < stretch["end"]: continue
                item = stretch["slices"][(stamp - stretch["start"]) // SLICE_US]
                if item["accumulator"] is not None: item["accumulator"].add(stamp, price, quote, maker, baseline["p95"])


def _slice_rows(stretches: Sequence[Dict[str, Any]], p95: Optional[Decimal]) -> list[Dict[str, Any]]:
    rows = []
    for stretch in stretches:
        for item in stretch["slices"]:
            acc = item["accumulator"]; status = item["status"] if acc is None else ("MEASURED" if acc.trades else "NO_TRADES")
            rows.append({"schema": "tape-shape-slice-v1", "group": stretch["group"], "symbol": stretch["symbol"], "venue": stretch["venue"],
                         "stretch_id": stretch["stretch_id"], "slice_index": item["index"], "slice_start_utc": _utc_text(item["start"]),
                         "slice_end_utc_exclusive": _utc_text(item["end"]), "status": status, "trade_count": 0 if acc is None else acc.trades,
                         "size_reservoir_seen": 0 if acc is None else acc.sizes.seen, "size_reservoir_size": 0 if acc is None else len(acc.sizes.values),
                         "large_trade_count": 0 if acc is None else acc.large_trades, "month_p95_trade_size_quote": p95,
                         "features": {name: None for name in FEATURES} if acc is None else acc.features(), "metadata": stretch["metadata"]})
    return rows


def _stats(values: Sequence[Decimal]) -> Dict[str, Any]:
    return {"count": len(values), "min": min(values) if values else None, "p10": absorption.quantile(values, 1, 10), "median": absorption.quantile(values, 1, 2), "p90": absorption.quantile(values, 9, 10), "max": max(values) if values else None}


def _rank_map(rows: Sequence[Mapping[str, Any]], feature: str) -> Dict[str, Decimal]:
    values = sorted(row["features"][feature] for row in rows if row["features"][feature] is not None); found = {}
    for row in rows:
        value = row["features"][feature]
        if value is None: continue
        left, right = bisect.bisect_left(values, value), bisect.bisect_right(values, value)
        found[row["slice_id"]] = Decimal(left + right - 1) / Decimal(2 * max(1, len(values) - 1))
    return found


def _auc(pump: Sequence[Decimal], control: Sequence[Decimal]) -> Optional[Decimal]:
    if not pump or not control: return None
    ordered = sorted([(value, 1) for value in pump] + [(value, 0) for value in control]); rank_sum = Decimal("0"); index = 0
    while index < len(ordered):
        right = index + 1
        while right < len(ordered) and ordered[right][0] == ordered[index][0]: right += 1
        rank = Decimal(index + 1 + right) / Decimal("2"); rank_sum += rank * sum(label for _value, label in ordered[index:right]); index = right
    return (rank_sum - Decimal(len(pump) * (len(pump) + 1)) / Decimal("2")) / Decimal(len(pump) * len(control))


def _best_pair(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    eligible = [row for row in rows if row["group"] in {"pump_prior", "control", "quiet_own"}]
    for index, row in enumerate(eligible): row["slice_id"] = f"R{index}"
    ranks = {name: _rank_map(eligible, name) for name in FEATURES}; best = None
    for left_index, left in enumerate(FEATURES):
        for right in FEATURES[left_index + 1:]:
            common_pump = [row for row in eligible if row["group"] == "pump_prior" and row["slice_id"] in ranks[left] and row["slice_id"] in ranks[right]]
            common_control = [row for row in eligible if row["group"] != "pump_prior" and row["slice_id"] in ranks[left] and row["slice_id"] in ranks[right]]
            if not common_pump or not common_control: continue
            orient_left = Decimal("1") if absorption.quantile([r["features"][left] for r in common_pump], 1, 2) >= absorption.quantile([r["features"][left] for r in common_control], 1, 2) else Decimal("-1")
            orient_right = Decimal("1") if absorption.quantile([r["features"][right] for r in common_pump], 1, 2) >= absorption.quantile([r["features"][right] for r in common_control], 1, 2) else Decimal("-1")
            score = lambda row: orient_left * ranks[left][row["slice_id"]] + orient_right * ranks[right][row["slice_id"]]
            auc = _auc([score(row) for row in common_pump], [score(row) for row in common_control])
            candidate = None if auc is None else (auc, left, right, len(common_pump), len(common_control), orient_left, orient_right)
            if candidate is not None and (best is None or candidate[:3] > best[:3]): best = candidate
    if best is None: return {"features": None, "auc": None, "pump_slices": 0, "pooled_control_slices": 0}
    return {"features": [best[1], best[2]], "auc": best[0], "pump_slices": best[3], "pooled_control_slices": best[4],
            "orientations": {best[1]: "high" if best[5] > 0 else "low", best[2]: "high" if best[6] > 0 else "low"}}


def _plain(value: Optional[Decimal]) -> str: return "NA" if value is None else format(value, ".7g")


def _serial(value: Any) -> Any:
    if isinstance(value, Decimal): return absorption.decimal_text(value)
    if isinstance(value, set): return sorted(value)
    if isinstance(value, Mapping): return {key: _serial(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)): return [_serial(item) for item in value]
    return value


def _write(path: Path, payload: Any, lines: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        if lines:
            for row in payload: handle.write(json.dumps(_serial(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        else: json.dump(_serial(payload), handle, ensure_ascii=False, sort_keys=True, separators=(",", ":")); handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA); parser.add_argument("--census", type=Path, default=DEFAULT_CENSUS)
    parser.add_argument("--priors", type=Path, default=DEFAULT_PRIORS); parser.add_argument("--population", type=Path, default=DEFAULT_POPULATION)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT); parser.add_argument("--control-stretches", type=int, choices=(2, 4), default=4)
    args = parser.parse_args(argv); getcontext().prec = 50
    try:
        census, population = _read(args.census), _read(args.population)
        priors = [json.loads(line) for line in args.priors.read_text(encoding="utf-8").splitlines() if line]
        episodes = {row["episode_id"]: row for row in census.get("episodes", [])}; ready = [row for row in priors if row.get("windows_tape_ready", 0) > 0]
        if census.get("header", {}).get("schema") != "pump-census-v1" or population.get("schema") != "signal-base-rate-population-v1": raise StudyError("input schema differs")
        if len(ready) != 33 or {row["symbol"] for row in ready} != set(population.get("census_symbols", [])): raise StudyError("AB8 tape-ready population differs")
        selected = [episodes[row["episode_id"]] for row in ready]; control_symbols = population.get("control_symbols", [])
        evidence = {row["symbol"]: row for row in population.get("control_baseline_evidence", []) if row.get("symbol") in control_symbols}
        if len(control_symbols) != 30 or len(evidence) != 30: raise StudyError("AB9 control population differs")
        month_end = _utc_us(census["header"]["as_of"] + "T00:00:00Z") + DAY_US; month = month_end - MONTH_DAYS * DAY_US
        episode_map: Dict[str, list[Mapping[str, Any]]] = {}
        for row in selected: episode_map.setdefault(row["symbol"], []).append(row)
        order = sorted(episode_map) + list(control_symbols); baselines, quiet_evidence, all_rows, stretches_total = {}, {}, [], 0
        for ordinal, symbol in enumerate(order, 1):
            venue = episode_map[symbol][0]["venue"] if symbol in episode_map else evidence[symbol]["venue"]
            baseline = _month_baseline(args.data_dir, symbol, venue, month); baselines[symbol] = baseline
            stretches = []
            if symbol in episode_map:
                for row in sorted(episode_map[symbol], key=lambda item: item["episode_id"]):
                    start = _utc_us(row["base_utc"]) - STRETCH_US
                    stretches.append(_stretch("pump_prior", symbol, venue, row["episode_id"], start, baseline["valid"], baseline["invalid"],
                                              {"episode_id": row["episode_id"], "class": row["class"], "base_utc": row["base_utc"]}))
                quiet, quiet_meta = _quiet_start(args.data_dir, symbol, venue, month, baseline["valid"]); quiet_evidence[symbol] = quiet_meta | {"start_utc": _utc_text(quiet)}
                stretches.append(_stretch("quiet_own", symbol, venue, f"Q-{symbol}", quiet, baseline["valid"], baseline["invalid"], quiet_meta))
            else:
                position = control_symbols.index(symbol); phase = position % 7
                for index in range(args.control_stretches):
                    start = month + (phase + index * (14 if args.control_stretches == 2 else 7)) * DAY_US
                    stretches.append(_stretch("control", symbol, venue, f"C{position + 1:02d}-{index + 1}", start, baseline["valid"], baseline["invalid"],
                                              {"population_position": position + 1, "phase_days": phase, "stretch_number": index + 1}))
            _measure(args.data_dir, symbol, venue, baseline, stretches); all_rows.extend(_slice_rows(stretches, baseline["p95"])); stretches_total += len(stretches)
            print(f"AB11 measure | symbols={ordinal}/{len(order)} | symbol={symbol} | month_trades={baseline['trade_count']}", flush=True)
        distributions = {name: {group: _stats([row["features"][name] for row in all_rows if row["group"] == group and row["features"][name] is not None])
                                for group in ("pump_prior", "control", "quiet_own")} for name in FEATURES}
        overlap = {}
        for name in FEATURES:
            pump, control = distributions[name]["pump_prior"], distributions[name]["control"]
            overlap[name] = "unavailable" if pump["median"] is None or control["p90"] is None else (
                "pump median above control p90" if pump["median"] > control["p90"] else
                "pump median below control p10" if pump["median"] < control["p10"] else "pump median inside control p10-p90")
        pooled = {name: [row["features"][name] for row in all_rows if row["group"] != "pump_prior" and row["features"][name] is not None] for name in FEATURES}
        clean = [name for name in FEATURES if distributions[name]["pump_prior"]["count"] and pooled[name] and
                 (distributions[name]["pump_prior"]["min"] > max(pooled[name]) or distributions[name]["pump_prior"]["max"] < min(pooled[name]))]
        pair = _best_pair(all_rows)
        coverage = {group: {"scheduled": sum(row["group"] == group for row in all_rows),
                            "measured": sum(row["group"] == group and row["status"] == "MEASURED" for row in all_rows),
                            "tape_absent_or_invalid": sum(row["group"] == group and row["status"] in {"TAPE_ABSENT", "TAPE_INVALID"} for row in all_rows),
                            "no_trades": sum(row["group"] == group and row["status"] == "NO_TRADES" for row in all_rows),
                            "no_quiet_window": sum(row["group"] == group and row["status"] == "NO_QUIET_WINDOW" for row in all_rows)}
                    for group in ("pump_prior", "control", "quiet_own")}
        pump_missing = len({row["stretch_id"] for row in all_rows if row["group"] == "pump_prior" and row["status"] in {"TAPE_ABSENT", "TAPE_INVALID"}})
        reservoir_sizes = [len(value["sample"]) for value in baselines.values()]
        summary = {"schema": "tape-shape-summary-v1", "question": "do deep tape features separate prior windows of real pumps from quiet stretches",
                   "parameters": {"as_of": census["header"]["as_of"], "month_days": MONTH_DAYS, "slice_hours": 4, "stretch_hours": 48,
                     "control_stretches_per_symbol": args.control_stretches, "control_start_rule": "position_mod_7_then_7_day_spacing",
                     "month_reservoir_cap": MONTH_RESERVOIR, "month_sampling": "deterministic_bottom_priority", "slice_reservoir_cap": SLICE_RESERVOIR,
                     "price_level_exact_cap": PRICE_LEVEL_CAP, "large_print_rule": "strictly_above_symbol_month_p95",
                     "quiet_rule": "lowest_quote_volume_complete_48h_4h_aligned_with_complete_tape_days",
                     "pair_rule": "sum_of_oriented_empirical_feature_ranks_against_pooled_controls_then_Mann_Whitney_AUC"},
                   "population": {"pump_episodes": len(ready), "census_symbols": len(episode_map), "control_symbols": len(control_symbols), "stretches": stretches_total},
                   "coverage": coverage, "pump_episodes_with_any_missing_tape_slice": pump_missing, "distributions": distributions,
                   "overlap": overlap, "best_pair": pair, "clean_separation_against_pooled_controls": clean, "quiet_evidence": quiet_evidence,
                   "reservoirs": {symbol: {"month_trade_count": value["trade_count"], "month_sample_size": len(value["sample"]),
                     "month_p95_quote": value["p95"], "valid_tape_days": len(value["valid"]), "invalid_tape_days": len(value["invalid"])}
                     for symbol, value in baselines.items()}}
        report = [f"scope | pump episodes={len(ready)}; census symbols={len(episode_map)}; controls={len(control_symbols)}; slices are 4h | MEASURED"]
        for name in FEATURES:
            parts = []
            for group in ("pump_prior", "control", "quiet_own"):
                stat = distributions[name][group]; parts.append(f"{group} n={stat['count']} min/med/p90/max=" + "/".join(_plain(stat[key]) for key in ("min", "median", "p90", "max")))
            report.append(f"{name} | " + " | ".join(parts) + f" | {overlap[name]} | MEASURED")
        report.append(f"best pair | features={pair['features']}; AUC={_plain(pair['auc'])}; pump n={pair['pump_slices']}; pooled control n={pair['pooled_control_slices']} | MEASURED")
        report.append(f"honest | missing-tape pump episodes={pump_missing}/{len(ready)}; slice coverage={coverage}; month reservoir min/median/max={min(reservoir_sizes)}/{int(absorption.quantile([Decimal(v) for v in reservoir_sizes], 1, 2))}/{max(reservoir_sizes)} cap={MONTH_RESERVOIR}; any clean feature={'YES' if clean else 'NO'} | MEASURED")
        _write(args.output_dir / "slice-features.jsonl", all_rows, lines=True); _write(args.output_dir / "tape-shape-summary.json", summary)
        text_path = args.output_dir / "separation-report.txt"; text_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = text_path.with_name(text_path.name + ".tmp"); temporary.write_text("\n".join(report) + "\n", encoding="utf-8"); os.replace(temporary, text_path)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, StudyError, absorption.AbsorptionError) as error:
        print(f"tape shape study: {error}", file=sys.stderr); return 2
    print(f"AB11 | slices={len(all_rows)} | output={args.output_dir}"); return 0


if __name__ == "__main__": raise SystemExit(main())
