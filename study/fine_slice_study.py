#!/usr/bin/env python3
"""Measure 30-minute tape-shape slices before labelled census episodes."""

from __future__ import annotations

import argparse, csv, io, json, os, sys, zipfile
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple

try:
    from study import absorption, tape_pull, tape_shape_study as shape
except ModuleNotFoundError as error:
    if error.name != "study": raise
    import absorption, tape_pull, tape_shape_study as shape  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CENSUS = ROOT / "lane-logs/AB13/pump-census-6mo.json"
DEFAULT_TAPE = ROOT / "study/data-episode-tape"
SLICE_US, WINDOW_US, SLICE_COUNT = 30 * shape.MINUTE_US, 12 * shape.HOUR_US, 24
Trade = Tuple[int, int, Decimal, Decimal, bool]


class FineSliceError(Exception): pass


@dataclass
class Episode:
    episode_id: str; symbol: str; venue: str; label: str; base_text: str
    base: int; start: int; end: int; month: str; days: Tuple[int, ...]
    missing: Tuple[str, ...] = ()
    invalid: Dict[str, str] = field(default_factory=dict)
    month_p95: Optional[Decimal] = None
    threshold: Optional[Decimal] = None
    threshold_source: Optional[str] = None
    fallback_used: bool = False
    month_valid_days: int = 0
    month_invalid_days: int = 0
    accumulators: Optional[list[Any]] = None


class _ChunkStream(io.RawIOBase):
    def __init__(self, chunks: Iterable[bytes]) -> None:
        super().__init__()
        self._chunks, self._pending = iter(chunks), memoryview(b"")

    def readable(self) -> bool: return True

    def readinto(self, target: Any) -> int:
        view = memoryview(target)
        while not self._pending:
            try:
                self._pending = memoryview(next(self._chunks))
            except StopIteration:
                return 0
        size = min(len(view), len(self._pending))
        view[:size], self._pending = self._pending[:size], self._pending[size:]
        return size

    def close(self) -> None:
        close = getattr(self._chunks, "close", None)
        if callable(close): close()
        super().close()


def _positive_limit(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("limit must be a positive integer") from None
    if value <= 0 or str(value) != raw.strip():
        raise argparse.ArgumentTypeError("limit must be a positive integer")
    return value


def _load_episodes(path: Path, limit: Optional[int]) -> list[Episode]:
    payload = shape._read(path)
    if not isinstance(payload, Mapping) or payload.get("header", {}).get("schema") != "pump-census-6mo-v1":
        raise FineSliceError("census schema differs")
    raw_rows, summary = payload.get("episodes"), payload.get("summary")
    if not isinstance(raw_rows, list) or not raw_rows: raise FineSliceError("census has no episodes")
    if not isinstance(summary, Mapping) or summary.get("episodes") != len(raw_rows): raise FineSliceError("census episode total differs")
    episodes, seen = [], set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping): raise FineSliceError("census episode row differs")
        episode_id, label, base_text = raw.get("episode_id"), raw.get("class"), raw.get("base_utc")
        if not isinstance(episode_id, str) or not episode_id or episode_id in seen:
            raise FineSliceError("episode_id is absent or repeated")
        if label not in {"HELD", "COLLAPSED"} or not isinstance(base_text, str):
            raise FineSliceError(f"{episode_id} label or base differs")
        try:
            symbol = tape_pull._validate_symbol(raw.get("symbol"))
            venue = tape_pull._validate_venue(raw.get("venue"))
            base = shape._utc_us(base_text)
        except (tape_pull.TapePullError, shape.StudyError) as error:
            raise FineSliceError(f"{episode_id} identity differs: {error}") from None
        if base % shape.MINUTE_US: raise FineSliceError(f"{episode_id} base is not minute-aligned")
        start = base - WINDOW_US
        days = tuple(sorted(shape._required_days(start, base)))
        episodes.append(Episode(episode_id, symbol, venue, label, base_text, base, start, base, base_text[:7], days))
        seen.add(episode_id)
    return episodes[:limit] if limit is not None else episodes


def _trade_rows(path: Path, day: int) -> Iterable[Trade]:
    expected, previous, found = path.stem + ".csv", None, False
    try:
        with zipfile.ZipFile(path, "r") as archive:
            members = [member for member in archive.infolist() if not member.is_dir()]
            if len(members) != 1 or members[0].filename != expected or members[0].flag_bits & 0x1:
                raise FineSliceError(f"{path.name} member differs")
            chunks = tape_pull.stream_verified_member(archive, members[0], tape_pull.MAX_MEMBER_BYTES)
            raw = io.BufferedReader(_ChunkStream(chunks))
            with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as handle:
                for row in csv.reader(handle):
                    if not row or not any(cell.strip() for cell in row):
                        continue
                    if not found and row[0].strip().lower().replace(" ", "_") in {"agg_trade_id", "aggregate_trade_id", "aggtradeid"}:
                        continue
                    if len(row) < 7:
                        raise FineSliceError(f"{path.name} row width differs")
                    aggregate_id, first_id, last_id = int(row[0]), int(row[3]), int(row[4])
                    stamp = int(row[5])
                    stamp_us = stamp if stamp >= 100_000_000_000_000 else stamp * 1_000
                    maker = row[6].strip().lower()
                    key = stamp_us, aggregate_id
                    if min(aggregate_id, first_id, last_id) < 0 or last_id < first_id or not day <= stamp_us < day + shape.DAY_US:
                        raise FineSliceError(f"{path.name} trade identity differs")
                    if previous is not None and key <= previous:
                        raise FineSliceError(f"{path.name} ordering differs")
                    if maker not in {"true", "false"}:
                        raise FineSliceError(f"{path.name} maker field differs")
                    previous, found = key, True
                    yield aggregate_id, stamp_us, shape._decimal(row[1]), shape._decimal(row[2]), maker == "true"
        if not found:
            raise FineSliceError(f"{path.name} has no trades")
    except FineSliceError:
        raise
    except tape_pull.ArchiveError as error:
        details = f" measured={error.measured_value}" if error.measured_value is not None else ""
        details += f" bound={error.bound}" if error.bound is not None else ""
        raise FineSliceError(f"{path.name} shared verification differs: {error}{details}") from None
    except shape.StudyError as error:
        raise FineSliceError(f"{path.name} trade value differs: {error}") from None
    except (OSError, UnicodeError, csv.Error, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise FineSliceError(f"{path.name} cannot be parsed: {type(error).__name__}") from None


def _scan_archive(path: Path, day: int, targets: Sequence[Episode], visit: Callable[[Episode, Trade], None], every: Optional[Callable[[Trade], None]] = None) -> None:
    ordered, active, cursor = sorted(targets, key=lambda item: (item.start, item.end, item.episode_id)), [], 0
    for trade in _trade_rows(path, day):
        stamp = trade[1]
        if every is not None:
            every(trade)
        while cursor < len(ordered) and ordered[cursor].start <= stamp:
            active.append(ordered[cursor]); cursor += 1
        active = [episode for episode in active if stamp < episode.end]
        for episode in active:
            visit(episode, trade)


def _bank_paths(tape_dir: Path, symbol: str, venue: str, required: set[int]) -> Dict[int, Path]:
    paths: Dict[int, Path] = {}
    for day in required:
        path = shape._archive(tape_dir, symbol, venue, day, "aggTrades")
        if path.is_file():
            paths[day] = path
    return paths


def _threshold(month_p95: Optional[Decimal], window_p95: Optional[Decimal]) -> Tuple[Optional[Decimal], Optional[str], bool]:
    if month_p95 is not None:
        return month_p95, "OWN_MONTH_P95", False
    return window_p95, "OWN_WINDOW_SET_P95" if window_p95 is not None else None, True


def _measure_group(tape_dir: Path, episodes: Sequence[Episode]) -> None:
    symbol, venue = episodes[0].symbol, episodes[0].venue
    for episode in episodes:
        episode.missing = tuple(shape._day_text(day) for day in episode.days if not shape._archive(tape_dir, symbol, venue, day, "aggTrades").is_file())
    present = [episode for episode in episodes if not episode.missing]
    required: Dict[int, list[Episode]] = {}
    for episode in present:
        for day in episode.days:
            required.setdefault(day, []).append(episode)
    months = {episode.month for episode in present}
    paths = _bank_paths(tape_dir, symbol, venue, set(required))
    reservoirs = {month: shape.PriorityReservoir(shape.MONTH_RESERVOIR) for month in months}
    evidence = {month: {"valid": 0, "invalid": 0} for month in months}
    for day, path in sorted(paths.items()):
        month, targets = shape._day_text(day)[:7], required.get(day, [])
        daily = shape.PriorityReservoir(shape.MONTH_RESERVOIR) if month in months else None
        salt = shape._salt(f"fine:{symbol}:{venue}:{month}")
        def every(trade: Trade) -> None:
            if daily is not None:
                aggregate_id, stamp, price, quantity, _maker = trade
                daily.offer(shape._priority(stamp, aggregate_id, salt), stamp, aggregate_id, price * quantity)
        try:
            _scan_archive(path, day, targets, lambda _episode, _trade: None, every)
        except FineSliceError as error:
            if month in evidence:
                evidence[month]["invalid"] += 1
            for episode in targets:
                episode.invalid[shape._day_text(day)] = str(error)
            continue
        if month in evidence and daily is not None:
            evidence[month]["valid"] += 1
            reservoirs[month].merge(daily)
    month_p95 = {month: absorption.quantile(sample.values(), 95, 100) for month, sample in reservoirs.items()}
    fallback = []
    for episode in present:
        episode.month_p95 = month_p95.get(episode.month)
        episode.month_valid_days = evidence.get(episode.month, {}).get("valid", 0)
        episode.month_invalid_days = evidence.get(episode.month, {}).get("invalid", 0)
        if not episode.invalid and episode.month_p95 is None:
            fallback.append(episode)
    if fallback:
        samples = {episode.episode_id: shape.PriorityReservoir(shape.MONTH_RESERVOIR) for episode in fallback}
        fallback_days: Dict[int, list[Episode]] = {}
        for episode in fallback:
            for day in episode.days:
                fallback_days.setdefault(day, []).append(episode)
        for day, targets in sorted(fallback_days.items()):
            path = shape._archive(tape_dir, symbol, venue, day, "aggTrades")
            def collect(episode: Episode, trade: Trade) -> None:
                aggregate_id, stamp, price, quantity, _maker = trade
                samples[episode.episode_id].offer(shape._priority(stamp, aggregate_id, shape._salt(episode.episode_id)), stamp, aggregate_id, price * quantity)
            try:
                _scan_archive(path, day, targets, collect)
            except FineSliceError as error:
                for episode in targets:
                    episode.invalid[shape._day_text(day)] = str(error)
        for episode in fallback:
            window_p95 = absorption.quantile(samples[episode.episode_id].values(), 95, 100) if not episode.invalid else None
            episode.threshold, episode.threshold_source, episode.fallback_used = _threshold(None, window_p95)
    ready = [episode for episode in present if not episode.invalid]
    for episode in ready:
        if episode not in fallback:
            episode.threshold, episode.threshold_source, episode.fallback_used = _threshold(episode.month_p95, None)
        episode.accumulators = [shape.SliceAccumulator(shape._salt(f"fine:{episode.episode_id}:{index}")) for index in range(SLICE_COUNT)]
    feature_days: Dict[int, list[Episode]] = {}
    for episode in ready:
        for day in episode.days:
            feature_days.setdefault(day, []).append(episode)
    for day, targets in sorted(feature_days.items()):
        path = shape._archive(tape_dir, symbol, venue, day, "aggTrades")
        def measure(episode: Episode, trade: Trade) -> None:
            _aggregate_id, stamp, price, quantity, maker = trade
            index = (stamp - episode.start) // SLICE_US
            episode.accumulators[index].add(stamp, price, price * quantity, maker, episode.threshold)
        try:
            _scan_archive(path, day, targets, measure)
        except FineSliceError as error:
            for episode in targets:
                episode.invalid[shape._day_text(day)] = str(error)


def _rows(episode: Episode) -> Iterable[Dict[str, Any]]:
    episode_status = "TAPE_ABSENT" if episode.missing else "TAPE_INVALID" if episode.invalid else "FULLY_BANKED"
    for index in range(SLICE_COUNT):
        accumulator = None if episode_status != "FULLY_BANKED" else episode.accumulators[index]
        status = episode_status if accumulator is None else "MEASURED" if accumulator.trades else "NO_TRADES"
        start, end = episode.start + index * SLICE_US, episode.start + (index + 1) * SLICE_US
        yield {"schema": "fine-slice-study-v1", "episode_id": episode.episode_id, "symbol": episode.symbol,
               "venue": episode.venue, "class": episode.label, "base_utc": episode.base_text,
               "episode_status": episode_status, "slice_index": index, "slice_start_utc": shape._utc_text(start),
               "slice_end_utc_exclusive": shape._utc_text(end), "status": status,
               "required_archive_days": [shape._day_text(day) for day in episode.days],
               "absent_archive_days": list(episode.missing), "invalid_archive_reasons": episode.invalid,
               "trade_count": 0 if accumulator is None else accumulator.trades,
               "size_reservoir_seen": 0 if accumulator is None else accumulator.sizes.seen,
               "size_reservoir_size": 0 if accumulator is None else len(accumulator.sizes.values),
               "large_trade_count": 0 if accumulator is None else accumulator.large_trades,
               "own_month_p95_trade_size_quote": episode.month_p95,
               "large_print_p95_trade_size_quote": episode.threshold,
               "large_print_p95_source": episode.threshold_source, "large_print_p95_fallback": episode.fallback_used,
               "own_month_valid_archive_days": episode.month_valid_days,
               "own_month_invalid_archive_days": episode.month_invalid_days,
               "features": {name: None for name in shape.FEATURES} if accumulator is None else accumulator.features()}


def run_fine_slice(census: Path, tape_dir: Path, output: Path, limit: Optional[int] = None) -> Dict[str, int]:
    getcontext().prec = 50
    episodes = _load_episodes(Path(census), limit)
    grouped: Dict[Tuple[str, str], list[Episode]] = {}
    for episode in episodes:
        grouped.setdefault((episode.symbol, episode.venue), []).append(episode)
    output = Path(output)
    if output.resolve() == Path(census).resolve(): raise FineSliceError("output must differ from census")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    counts = {"episodes": len(episodes), "fully_banked": 0, "tape_absent": 0, "tape_invalid": 0,
              "slices": len(episodes) * SLICE_COUNT, "rows": 0, "measured_slices": 0, "no_trade_slices": 0}
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            for ordinal, key in enumerate(sorted(grouped), 1):
                _measure_group(Path(tape_dir), grouped[key])
                for episode in grouped[key]:
                    episode_status = "TAPE_ABSENT" if episode.missing else "TAPE_INVALID" if episode.invalid else "FULLY_BANKED"
                    counts[episode_status.lower()] += 1
                    for row in _rows(episode):
                        handle.write(json.dumps(shape._serial(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
                        counts["rows"] += 1
                        if row["status"] == "MEASURED":
                            counts["measured_slices"] += 1
                        elif row["status"] == "NO_TRADES":
                            counts["no_trade_slices"] += 1
                if ordinal % 25 == 0 or ordinal == len(grouped):
                    print(f"fine slice progress | groups={ordinal}/{len(grouped)} | rows={counts['rows']}", flush=True)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, output)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return counts


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--census", type=Path, default=DEFAULT_CENSUS)
    parser.add_argument("--tape-dir", type=Path, default=DEFAULT_TAPE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=_positive_limit)
    args = parser.parse_args(argv)
    try:
        counts = run_fine_slice(args.census, args.tape_dir, args.output, args.limit)
    except (FineSliceError, shape.StudyError, OSError, TypeError, ValueError) as error:
        print(f"fine slice study: {error}", file=sys.stderr)
        return 2
    print("fine slice | " + " | ".join(f"{name}={value}" for name, value in counts.items()) + f" | output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
