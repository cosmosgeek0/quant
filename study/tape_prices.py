#!/usr/bin/env python3
"""Stream consumer-shaped policy price rows from local aggTrades archives."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


try:
    from study import policy_sim, stage_b, tape_pull
except ModuleNotFoundError as error:
    if error.name != "study":
        raise
    import policy_sim  # type: ignore
    import stage_b  # type: ignore
    import tape_pull  # type: ignore


STUDY_DIR = Path(__file__).resolve().parent
DEFAULT_EVENTS_PATH = STUDY_DIR / "out" / "v2" / "events.jsonl"
DEFAULT_TAPE_DIR = STUDY_DIR / "data"
DEFAULT_OUTPUT_PATH = STUDY_DIR / "out" / "v2" / "tape_prices.jsonl"
MINUTE_MS = 60_000
PRE_MINUTES = 60
POST_MINUTES = 120
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
Window = Tuple[int, int]
Key = Tuple[str, str]


class TapePricesError(Exception):
    """An event, archive, or output cannot be proved valid."""


def _positive_limit(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("limit must be a positive integer") \
            from None
    if value <= 0 or str(value) != raw.strip():
        raise argparse.ArgumentTypeError("limit must be a positive integer")
    return value


def _event_windows(
    path: Path, limit: Optional[int]
) -> Tuple[Dict[Key, List[Window]], int]:
    grouped: Dict[Key, List[Window]] = {}
    seen = set()
    count = 0
    try:
        rows = policy_sim._read_jsonl(Path(path), Path(path).name)
        for line_number, row in rows:
            if row.get("kind") == "summary":
                continue
            event = policy_sim.event_from_mapping(
                row, f"{Path(path).name}:{line_number}"
            )
            if event.event_id in seen:
                raise TapePricesError(f"duplicate event_id: {event.event_id}")
            seen.add(event.event_id)
            start_ms = event.event_time_ms - PRE_MINUTES * MINUTE_MS
            end_ms = event.event_time_ms + POST_MINUTES * MINUTE_MS
            grouped.setdefault((event.symbol, event.venue), []).append(
                (start_ms, end_ms)
            )
            count += 1
            if limit is not None and count >= limit:
                break
    except policy_sim.PolicySimError as error:
        raise TapePricesError(str(error)) from None
    if count == 0:
        raise TapePricesError("events JSONL has no labelled events")
    return grouped, count


def _days(start_ms: int, end_ms: int) -> Iterable[date]:
    first = (EPOCH + timedelta(milliseconds=start_ms)).date()
    last = (EPOCH + timedelta(milliseconds=end_ms)).date()
    current = first
    while current <= last:
        yield current
        current += timedelta(days=1)


def _archive_path(tape_dir: Path, key: Key, day: date) -> Path:
    symbol, venue = key
    archive_venue = "spot" if venue == "spot" else "futures"
    relative = stage_b._canonical_relative("agg", archive_venue, symbol, day)
    return Path(tape_dir) / relative


def _available_windows(
    grouped: Mapping[Key, Sequence[Window]], tape_dir: Path
) -> Tuple[Dict[Key, List[Window]], int]:
    available: Dict[Key, List[Window]] = {}
    absent = 0
    for key, windows in grouped.items():
        for start_ms, end_ms in windows:
            paths = (
                _archive_path(tape_dir, key, day)
                for day in _days(start_ms, end_ms)
            )
            if not all(path.is_file() for path in paths):
                absent += 1
                continue
            available.setdefault(key, []).append((start_ms, end_ms))
    return available, absent


def _merged(windows: Sequence[Window]) -> Tuple[Window, ...]:
    result: List[List[int]] = []
    for start_ms, end_ms in sorted(windows):
        if not result or start_ms > result[-1][1]:
            result.append([start_ms, end_ms])
        elif end_ms > result[-1][1]:
            result[-1][1] = end_ms
    return tuple((start_ms, end_ms) for start_ms, end_ms in result)


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


def _archive_csv_rows(
    path: Path, expected: str
) -> Iterable[Tuple[int, List[str]]]:
    identity = f"{path.name}:{expected}"
    try:
        with zipfile.ZipFile(path, "r") as archive:
            members = [
                member for member in archive.infolist() if not member.is_dir()
            ]
            if len(members) != 1 or members[0].filename != expected:
                raise TapePricesError(
                    f"{path.name} does not contain only {expected}"
                )
            member = members[0]
            if member.flag_bits & 0x1:
                raise TapePricesError(f"{identity} is encrypted")
            chunks = tape_pull.stream_verified_member(
                archive, member, tape_pull.MAX_MEMBER_BYTES
            )
            raw = io.BufferedReader(_ChunkStream(chunks))
            with io.TextIOWrapper(
                raw, encoding="utf-8-sig", newline=""
            ) as handle:
                yield from enumerate(csv.reader(handle), start=1)
    except TapePricesError:
        raise
    except tape_pull.ArchiveError as error:
        details = ""
        if error.measured_value is not None:
            details += f" | measured={error.measured_value}"
        if error.bound is not None:
            details += f" | bound={error.bound}"
        raise TapePricesError(f"{identity} {error}{details}") from None
    except (
        OSError,
        UnicodeError,
        csv.Error,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as error:
        raise TapePricesError(f"cannot parse {path.name}: {error}") from None


def _agg_trade_rows(
    path: Path,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> Iterable[Any]:
    parsed = stage_b._archive_name_match(path, stage_b.AGG_ARCHIVE)
    if parsed is None or parsed[1] != symbol:
        raise stage_b.StageBError(
            f"aggTrades archive does not match {symbol}: {path.name}"
        )
    archive_day = stage_b._archive_day(path, stage_b.AGG_ARCHIVE)
    expected = path.stem + ".csv"
    found = False
    for line_number, row in _archive_csv_rows(path, expected):
        if not row or not any(cell.strip() for cell in row):
            continue
        first = row[0].strip().lower().replace(" ", "_")
        if not found and first in {
            "agg_trade_id",
            "aggregate_trade_id",
            "aggtradeid",
        }:
            continue
        source = f"{path.name}:{expected}:{line_number}"
        if len(row) < 7:
            raise stage_b.StageBError(
                f"{source} has fewer than seven aggTrades columns"
            )
        aggregate_id = stage_b._integer(
            row[0], f"{source} aggregate id", non_negative=True
        )
        price = stage_b._decimal(row[1], f"{source} price", positive=True)
        quantity = stage_b._decimal(
            row[2], f"{source} quantity", positive=True
        )
        first_trade_id = stage_b._integer(
            row[3], f"{source} first trade id", non_negative=True
        )
        last_trade_id = stage_b._integer(
            row[4], f"{source} last trade id", non_negative=True
        )
        if last_trade_id < first_trade_id:
            raise stage_b.StageBError(f"{source} has reversed trade ids")
        timestamp_ms = stage_b._timestamp_ms(
            row[5], f"{source} transact time"
        )
        actual_day = (EPOCH + timedelta(milliseconds=timestamp_ms)).date()
        if actual_day != archive_day:
            raise stage_b.StageBError(f"{source} is outside archive day")
        buyer_is_maker = stage_b._boolean(
            row[6], f"{source} buyer-is-maker"
        )
        found = True
        if start_ms <= timestamp_ms < end_ms:
            yield stage_b.AggTrade(
                symbol,
                aggregate_id,
                timestamp_ms,
                price,
                quantity,
                buyer_is_maker,
            )
    if not found:
        raise stage_b.StageBError(
            f"{path.name}:{expected} has no aggTrades rows"
        )


def _price_rows(
    grouped: Mapping[Key, Sequence[Window]], tape_dir: Path
) -> Iterable[Dict[str, Any]]:
    for key in sorted(grouped):
        symbol, venue = key
        previous: Optional[Tuple[int, int]] = None
        for start_ms, end_ms in _merged(grouped[key]):
            for day in _days(start_ms, end_ms):
                path = _archive_path(tape_dir, key, day)
                try:
                    trades = _agg_trade_rows(
                        path, symbol, start_ms, end_ms + 1
                    )
                    for trade in trades:
                        marker = (trade.timestamp_ms, trade.aggregate_id)
                        if previous is not None and marker <= previous:
                            raise TapePricesError(
                                f"{symbol} {venue} aggTrades are not ordered"
                            )
                        previous = marker
                        yield {
                            "symbol": symbol,
                            "venue": venue,
                            "timestamp_ms": trade.timestamp_ms,
                            "price": stage_b.decimal_text(trade.price),
                        }
                except stage_b.StageBError as error:
                    raise TapePricesError(str(error)) from None


def _write_output(
    output_path: Path,
    grouped: Mapping[Key, Sequence[Window]],
    tape_dir: Path,
) -> int:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    count = 0
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            for row in _price_rows(grouped, tape_dir):
                handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
                handle.write("\n")
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except (OSError, TypeError, ValueError, TapePricesError) as error:
        try:
            temporary.unlink()
        except OSError:
            pass
        if isinstance(error, TapePricesError):
            raise
        raise TapePricesError(f"cannot write tape prices: {error}") from None
    return count


def run_tape_prices(
    events_path: Path = DEFAULT_EVENTS_PATH,
    tape_dir: Path = DEFAULT_TAPE_DIR,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    *,
    limit: Optional[int] = None,
) -> Dict[str, int]:
    """Write exact policy tape rows and return factual production counts."""
    if limit is not None and (isinstance(limit, bool) or limit <= 0):
        raise TapePricesError("limit must be a positive integer")
    grouped, event_count = _event_windows(Path(events_path), limit)
    available, absent = _available_windows(grouped, Path(tape_dir))
    row_count = _write_output(Path(output_path), available, Path(tape_dir))
    return {"events": event_count, "absent": absent, "rows": row_count}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="produce policy-simulator prices from local aggTrades"
    )
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS_PATH)
    parser.add_argument("--tape-dir", type=Path, default=DEFAULT_TAPE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--limit", type=_positive_limit)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run_tape_prices(
            args.events, args.tape_dir, args.output, limit=args.limit
        )
    except TapePricesError as error:
        print(f"TAPE-PRICES | REFUSED | {error}", file=sys.stderr)
        return 2
    print(
        f"TAPE-PRICES | events={summary['events']} | rows={summary['rows']} | "
        f"absent={summary['absent']} | output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
