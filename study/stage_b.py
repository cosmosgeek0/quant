#!/usr/bin/env python3
"""Extract Volume Dataset v2 Stage-B features from local Binance tapes.

The windows manifest is the sole selector: only daily archives intersecting a
manifest window are opened.  Required aggTrades archives that are absent or
unusable produce a factual skip row for that window.  Klines, funding, and
open-interest files are optional context and remain null when they cannot be
proved.  This module has no network, credential, account, or execution path.

Windows are half-open intervals.  For the frozen ``[T-60m, T+120m]``
interface that means ``window_start <= trade < window_end``; the pre-print
baseline ends at ``T`` and the scoring tape starts at ``T``.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


try:
    from study import tape_pull
    from study.windows import validate_symbol
    from study.windows import WindowError as SymbolValidationError
except ModuleNotFoundError as error:
    if error.name != "study":
        raise
    import tape_pull  # type: ignore
    from windows import validate_symbol
    from windows import WindowError as SymbolValidationError


STUDY_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = STUDY_DIR / "data"
DEFAULT_MANIFEST = STUDY_DIR / "out" / "v2" / "windows_manifest.json"
DEFAULT_OUTPUT = STUDY_DIR / "out" / "v2" / "features.jsonl"
SCHEMA = "volume-dataset-v2-stage-b"
MINUTE_MS = 60_000
FROZEN_PRE_MINUTES = 60
FROZEN_POST_MINUTES = 120
PROGRESS_WINDOW_INTERVAL = 250
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
AGG_ARCHIVE = re.compile(
    r"^(?P<symbol>.+)-aggTrades-"
    r"(?P<day>\d{4}-\d{2}-\d{2})\.zip$"
)
KLINE_ARCHIVE = re.compile(
    r"^(?P<symbol>.+)-1m-"
    r"(?P<day>\d{4}-\d{2}-\d{2})\.zip$"
)
QUANTILES = (
    ("q10", 1, 10),
    ("q25", 1, 4),
    ("q50", 1, 2),
    ("q75", 3, 4),
    ("q90", 9, 10),
    ("q95", 19, 20),
    ("q99", 99, 100),
)


class StageBError(Exception):
    """A Stage-B input or result cannot be proved from local data."""


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


@dataclass(frozen=True)
class AggTrade:
    symbol: str
    aggregate_id: int
    timestamp_ms: int
    price: Decimal
    quantity: Decimal
    buyer_is_maker: bool

    @property
    def quote_size(self) -> Decimal:
        with localcontext() as context:
            context.prec = 50
            return self.price * self.quantity

    @property
    def taker_side(self) -> str:
        return "sell" if self.buyer_is_maker else "buy"


@dataclass(frozen=True)
class KlineBar:
    symbol: str
    open_ms: int
    close_ms: int
    high: Decimal
    low: Decimal


@dataclass(frozen=True)
class ContextPoint:
    timestamp_ms: int
    value: Decimal


@dataclass(frozen=True)
class WindowSpec:
    manifest_index: int
    window_id: str
    event_id: Optional[str]
    symbol: str
    venue_class: str
    venue: str
    label: Optional[str]
    window_start_ms: int
    event_ms: int
    window_end_ms: int
    raw: Mapping[str, Any]


def decimal_text(value: Decimal) -> str:
    """Render one finite Decimal as plain, exponent-free JSON text."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise StageBError("cannot format a non-finite Decimal")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def _optional_text(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else decimal_text(value)


def _decimal(
    raw: Any,
    source: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    if isinstance(raw, bool) or isinstance(raw, float):
        raise StageBError(f"{source} is not Decimal text")
    try:
        value = raw if isinstance(raw, Decimal) else Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise StageBError(f"{source} is not a Decimal") from None
    if not value.is_finite():
        raise StageBError(f"{source} is not a finite Decimal")
    if positive and value <= 0:
        raise StageBError(f"{source} is not a positive Decimal")
    if non_negative and value < 0:
        raise StageBError(f"{source} is not a non-negative Decimal")
    return value


def _integer(raw: Any, source: str, *, non_negative: bool = False) -> int:
    if isinstance(raw, bool) or isinstance(raw, float):
        raise StageBError(f"{source} is not an integer")
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise StageBError(f"{source} is not an integer") from None
    if str(value) != str(raw).strip() and not isinstance(raw, int):
        raise StageBError(f"{source} is not canonical integer text")
    if non_negative and value < 0:
        raise StageBError(f"{source} is negative")
    return value


def _timestamp_ms(raw: Any, source: str) -> int:
    """Parse UTC ISO text or Binance millisecond/microsecond epoch text."""
    if isinstance(raw, bool) or isinstance(raw, float):
        raise StageBError(f"{source} is not a timestamp")
    numeric: Optional[int] = None
    if isinstance(raw, (int, Decimal)):
        if isinstance(raw, Decimal) and raw != raw.to_integral_value():
            raise StageBError(f"{source} is not an integer timestamp")
        numeric = int(raw)
    elif isinstance(raw, str):
        stripped = raw.strip()
        if re.fullmatch(r"-?\d+", stripped):
            numeric = int(stripped)
        else:
            normalized = stripped[:-1] + "+00:00" \
                if stripped.endswith("Z") else stripped
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                raise StageBError(f"{source} is not an ISO UTC timestamp") \
                    from None
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise StageBError(f"{source} has no timezone")
            utc = parsed.astimezone(timezone.utc)
            delta = utc - EPOCH
            numeric = (
                delta.days * 86_400_000
                + delta.seconds * 1_000
                + delta.microseconds // 1_000
            )
    if numeric is None:
        raise StageBError(f"{source} is not a timestamp")
    if numeric >= 100_000_000_000_000:
        numeric //= 1_000
    if numeric < 100_000_000_000 or numeric >= 100_000_000_000_000:
        raise StageBError(f"{source} is outside the supported epoch range")
    return numeric


def utc_text(epoch_ms: int) -> str:
    return (EPOCH + timedelta(milliseconds=epoch_ms)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _boolean(raw: Any, source: str) -> bool:
    if isinstance(raw, bool):
        return raw
    normalized = str(raw).strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise StageBError(f"{source} is not true or false")


def _json_loads(raw: str, source: str) -> Any:
    try:
        return json.loads(raw, parse_float=Decimal)
    except json.JSONDecodeError as error:
        raise StageBError(f"{source} is not valid JSON: {error.msg}") from None


def _lookup(row: Mapping[str, Any], names: Sequence[str]) -> Any:
    containers = [row]
    for key in ("window", "bounds", "times"):
        nested = row.get(key)
        if isinstance(nested, Mapping):
            containers.append(nested)
    for container in containers:
        for name in names:
            if name in container and container[name] is not None:
                return container[name]
    return None


def _timestamp_lookup(
    row: Mapping[str, Any], names: Sequence[str], source: str
) -> Optional[int]:
    containers = [row]
    for key in ("window", "bounds", "times"):
        nested = row.get(key)
        if isinstance(nested, Mapping):
            containers.append(nested)
    raw_values = [
        container[name]
        for container in containers
        for name in names
        if name in container and container[name] is not None
    ]
    if not raw_values:
        return None
    parsed = [_timestamp_ms(value, source) for value in raw_values]
    if any(value != parsed[0] for value in parsed[1:]):
        raise StageBError(f"{source} aliases conflict")
    return parsed[0]


def _normalize_venue(raw: Any, source: str) -> str:
    normalized = str(raw).strip().lower().replace("-", "_")
    if normalized in {"spot"}:
        return "spot"
    if normalized in {
        "futures",
        "future",
        "perp",
        "perpetual",
        "usdm",
        "usd_m",
    }:
        return "futures"
    raise StageBError(f"{source} is not spot or futures")


def _window_spec(raw: Mapping[str, Any], index: int) -> WindowSpec:
    symbol_raw = _lookup(raw, ("symbol",))
    if not isinstance(symbol_raw, str):
        raise StageBError(f"window {index} has no symbol")
    try:
        symbol = validate_symbol(symbol_raw, f"window {index} symbol")
    except SymbolValidationError:
        raise StageBError(f"window {index} has an invalid symbol") from None

    venue_class_raw = _lookup(raw, ("venue_class", "venueClass"))
    venue_raw = _lookup(raw, ("tape_venue", "venue", "market_type"))
    if venue_class_raw is None:
        if venue_raw is None:
            raise StageBError(f"window {index} has no venue class")
        venue = _normalize_venue(venue_raw, f"window {index} venue")
        venue_class = "spot_only" if venue == "spot" else "futures_only"
    else:
        venue_class = str(venue_class_raw).strip().lower().replace("-", "_")
        if venue_class not in {"spot_only", "futures_only", "both"}:
            raise StageBError(f"window {index} has an invalid venue class")
        if venue_raw is None:
            venue = "futures" if venue_class == "futures_only" else "spot"
        else:
            venue = _normalize_venue(venue_raw, f"window {index} venue")
    if venue_class == "spot_only" and venue != "spot":
        raise StageBError(f"window {index} venue contradicts venue class")
    if venue_class == "futures_only" and venue != "futures":
        raise StageBError(f"window {index} venue contradicts venue class")

    start_names = (
        "window_start_ms",
        "start_ms",
        "window_start_utc",
        "window_start",
        "start_utc",
        "start",
    )
    event_names = (
        "event_ms",
        "event_time_ms",
        "event_utc",
        "event_time",
        "event_at",
        "anchor_ms",
        "anchor_utc",
        "t0_ms",
        "t0",
    )
    end_names = (
        "window_end_ms",
        "end_ms",
        "window_end_utc",
        "window_end",
        "end_utc",
        "end",
    )
    start_ms = _timestamp_lookup(
        raw,
        start_names,
        f"window {index} start",
    )
    event_ms = _timestamp_lookup(
        raw,
        event_names,
        f"window {index} event",
    )
    end_ms = _timestamp_lookup(
        raw,
        end_names,
        f"window {index} end",
    )
    if event_ms is None and (start_ms is None or end_ms is None):
        raise StageBError(f"window {index} has insufficient time bounds")
    start_ms = (
        event_ms - FROZEN_PRE_MINUTES * MINUTE_MS
        if start_ms is None and event_ms is not None
        else start_ms
    )
    end_ms = (
        event_ms + FROZEN_POST_MINUTES * MINUTE_MS
        if end_ms is None and event_ms is not None
        else end_ms
    )
    if start_ms is None or end_ms is None:
        raise StageBError(f"window {index} has insufficient time bounds")
    if event_ms is None:
        event_ms = start_ms + FROZEN_PRE_MINUTES * MINUTE_MS
    if not start_ms < event_ms < end_ms:
        raise StageBError(f"window {index} does not bracket its event")

    window_id_raw = _lookup(raw, ("window_id", "id"))
    event_id_raw = _lookup(raw, ("event_id", "matched_event_id"))
    event_id = None if event_id_raw is None else str(event_id_raw)
    label_raw = _lookup(raw, ("label", "event_label", "window_type", "kind"))
    label = None if label_raw is None else str(label_raw)
    window_id = str(window_id_raw) if window_id_raw is not None else (
        f"{symbol}:{venue}:{event_ms}:{index}"
    )
    if not window_id:
        raise StageBError(f"window {index} has an empty id")
    return WindowSpec(
        index,
        window_id,
        event_id,
        symbol,
        venue_class,
        venue,
        label,
        start_ms,
        event_ms,
        end_ms,
        raw,
    )


def load_manifest(path: Path = DEFAULT_MANIFEST) -> Tuple[WindowSpec, ...]:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as error:
        raise StageBError(f"cannot read windows manifest {source}: {error}") \
            from None
    payload = _json_loads(text, source.name)
    if isinstance(payload, Mapping):
        windows = payload.get("windows")
        if windows is None:
            windows = payload.get("rows")
    else:
        windows = payload
    if not isinstance(windows, list):
        raise StageBError("windows manifest must be a list or contain windows")
    parsed: List[WindowSpec] = []
    for index, raw in enumerate(windows):
        if not isinstance(raw, Mapping):
            raise StageBError(f"window {index} is not an object")
        parsed.append(_window_spec(raw, index))
    parsed.sort(
        key=lambda item: (
            item.window_start_ms,
            item.symbol,
            item.venue,
            item.window_id,
            item.manifest_index,
        )
    )
    return tuple(parsed)


def _required_days(start_ms: int, end_ms: int) -> Tuple[date, ...]:
    if end_ms <= start_ms:
        raise StageBError("archive interval is empty")
    first = (EPOCH + timedelta(milliseconds=start_ms)).date()
    last = (EPOCH + timedelta(milliseconds=end_ms - 1)).date()
    days: List[date] = []
    current = first
    while current <= last:
        days.append(current)
        current += timedelta(days=1)
    return tuple(days)


def _path_values(value: Any, venue: str) -> Tuple[str, ...]:
    if isinstance(value, Path):
        return (str(value),)
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        found: List[str] = []
        for item in value:
            found.extend(_path_values(item, venue))
        return tuple(found)
    if isinstance(value, Mapping):
        normalized = {
            str(key).lower().replace("-", "_"): item
            for key, item in value.items()
        }
        venue_aliases = (venue,) if venue == "spot" else (
            "futures",
            "future",
            "perp",
            "usdm",
            "usd_m",
        )
        for key in venue_aliases:
            if key in normalized:
                return _path_values(normalized[key], venue)
        if any(key in normalized for key in ("spot", "futures", "perp", "usdm")):
            return ()
        found = []
        for key in sorted(normalized):
            found.extend(_path_values(normalized[key], venue))
        return tuple(found)
    return ()


SOURCE_KEYS = {
    "agg": (
        "agg_trades_zips",
        "agg_trades_files",
        "aggtrades_zips",
        "aggtrades_files",
        "agg_trades",
        "aggtrades",
    ),
    "kline": ("kline_zips", "kline_files", "klines", "kline"),
    "funding": ("funding_files", "funding_file", "funding"),
    "open_interest": (
        "open_interest_files",
        "open_interest_file",
        "open_interest",
        "oi_files",
        "oi_file",
    ),
}


def _explicit_sources(spec: WindowSpec, kind: str) -> Tuple[str, ...]:
    aliases = SOURCE_KEYS[kind]
    containers = [spec.raw]
    for key in ("files", "sources", "data_files"):
        nested = spec.raw.get(key)
        if isinstance(nested, Mapping):
            containers.append(nested)
    found: List[str] = []
    for container in containers:
        normalized = {
            str(key).lower().replace("-", "_"): value
            for key, value in container.items()
        }
        for alias in aliases:
            if alias in normalized:
                found.extend(_path_values(normalized[alias], spec.venue))
    return tuple(dict.fromkeys(found))


def _safe_source_path(data_dir: Path, raw: str) -> Path:
    root = Path(data_dir).resolve()
    candidate = Path(raw)
    resolved = candidate.resolve() if candidate.is_absolute() \
        else (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise StageBError("manifest source path escapes data directory") \
            from None
    return resolved


def _path_venue(path: Path) -> Optional[str]:
    parts = {part.lower() for part in path.parts}
    if "spot" in parts:
        return "spot"
    if "futures" in parts or "um" in parts or "usdm" in parts:
        return "futures"
    return None


def _canonical_relative(kind: str, venue: str, symbol: str, day: date) -> Path:
    if kind == "agg":
        filename = f"{symbol}-aggTrades-{day.isoformat()}.zip"
        if venue == "spot":
            return Path("spot") / "daily" / "aggTrades" / symbol / filename
        return (
            Path("futures") / "um" / "daily" / "aggTrades" / symbol / filename
        )
    filename = f"{symbol}-1m-{day.isoformat()}.zip"
    if venue == "spot":
        return Path("spot") / "daily" / "klines" / symbol / "1m" / filename
    return (
        Path("futures") / "um" / "daily" / "klines" / symbol / "1m" / filename
    )


def _discover_daily(
    data_dir: Path,
    kind: str,
    venue: str,
    symbol: str,
    day: date,
) -> Tuple[Optional[Path], str]:
    root = Path(data_dir)
    relative = _canonical_relative(kind, venue, symbol, day)
    canonical = root / relative
    if canonical.is_file():
        return canonical, relative.as_posix()
    filename = relative.name
    matches = []
    if root.is_dir():
        for candidate in root.rglob(filename):
            if not candidate.is_file():
                continue
            candidate_venue = _path_venue(candidate.relative_to(root))
            if candidate_venue is None or candidate_venue == venue:
                matches.append(candidate)
    matches = sorted(set(matches))
    if len(matches) == 1:
        return matches[0], relative.as_posix()
    if len(matches) > 1:
        raise StageBError(f"ambiguous local archive {filename}")
    return None, relative.as_posix()


def _daily_sources(
    spec: WindowSpec,
    data_dir: Path,
    kind: str,
) -> Tuple[Tuple[Path, ...], Tuple[str, ...]]:
    days = _required_days(spec.window_start_ms, spec.window_end_ms)
    explicit = _explicit_sources(spec, kind)
    paths: List[Path] = []
    missing: List[str] = []
    if explicit:
        resolved = tuple(_safe_source_path(data_dir, item) for item in explicit)
        by_name = {path.name: path for path in resolved}
        for day in days:
            expected = _canonical_relative(
                kind, spec.venue, spec.symbol, day
            ).name
            path = by_name.get(expected)
            if path is None or not path.is_file():
                missing.append(expected)
            else:
                paths.append(path)
        return tuple(paths), tuple(missing)
    for day in days:
        path, expected = _discover_daily(
            data_dir, kind, spec.venue, spec.symbol, day
        )
        if path is None:
            missing.append(expected)
        else:
            paths.append(path)
    return tuple(paths), tuple(missing)


def _single_zip_member(
    path: Path, expected: str
) -> Tuple[zipfile.ZipFile, zipfile.ZipInfo]:
    try:
        archive = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise StageBError(f"cannot read {path.name}: {error}") from None
    members = [member for member in archive.infolist() if not member.is_dir()]
    if len(members) != 1 or members[0].filename != expected:
        archive.close()
        raise StageBError(f"{path.name} does not contain only {expected}")
    member = members[0]
    if member.flag_bits & 0x1:
        archive.close()
        raise StageBError(f"{path.name}:{expected} is encrypted")
    return archive, member


def _verified_member_chunks(
    path: Path,
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
) -> Iterable[bytes]:
    try:
        yield from tape_pull.stream_verified_member(
            archive, member, tape_pull.MAX_MEMBER_BYTES
        )
    except tape_pull.ArchiveError as error:
        details = ""
        if error.measured_value is not None:
            details += f" | measured={error.measured_value}"
        if error.bound is not None:
            details += f" | bound={error.bound}"
        raise StageBError(
            f"{path.name}:{member.filename} {error}{details}"
        ) from None


def _archive_name_match(
    path: Path, pattern: re.Pattern
) -> Optional[Tuple[re.Match, str]]:
    match = pattern.fullmatch(path.name)
    if match is None:
        return None
    try:
        symbol = validate_symbol(match.group("symbol"), f"{path.name} symbol")
    except SymbolValidationError:
        return None
    return match, symbol


def _archive_day(path: Path, pattern: re.Pattern) -> date:
    parsed = _archive_name_match(path, pattern)
    if parsed is None:
        raise StageBError(f"archive name is not recognized: {path.name}")
    match, _ = parsed
    try:
        parsed = date.fromisoformat(match.group("day"))
    except ValueError:
        raise StageBError(f"archive date is invalid: {path.name}") from None
    return parsed


def _agg_trade_rows(
    path: Path,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> Iterable[AggTrade]:
    parsed = _archive_name_match(path, AGG_ARCHIVE)
    if parsed is None or parsed[1] != symbol:
        raise StageBError(f"aggTrades archive does not match {symbol}: {path.name}")
    archive_day = _archive_day(path, AGG_ARCHIVE)
    expected = path.stem + ".csv"
    archive, member = _single_zip_member(path, expected)
    found = False
    try:
        with archive:
            chunks = _verified_member_chunks(path, archive, member)
            raw = io.BufferedReader(_ChunkStream(chunks))
            with raw:
                with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as handle:
                    for line_number, row in enumerate(csv.reader(handle), start=1):
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
                            raise StageBError(
                                f"{source} has fewer than seven aggTrades columns"
                            )
                        aggregate_id = _integer(
                            row[0], f"{source} aggregate id", non_negative=True
                        )
                        price = _decimal(row[1], f"{source} price", positive=True)
                        quantity = _decimal(
                            row[2], f"{source} quantity", positive=True
                        )
                        first_trade_id = _integer(
                            row[3], f"{source} first trade id", non_negative=True
                        )
                        last_trade_id = _integer(
                            row[4], f"{source} last trade id", non_negative=True
                        )
                        if last_trade_id < first_trade_id:
                            raise StageBError(f"{source} has reversed trade ids")
                        timestamp_ms = _timestamp_ms(
                            row[5], f"{source} transact time"
                        )
                        actual_day = (
                            EPOCH + timedelta(milliseconds=timestamp_ms)
                        ).date()
                        if actual_day != archive_day:
                            raise StageBError(f"{source} is outside archive day")
                        buyer_is_maker = _boolean(
                            row[6], f"{source} buyer-is-maker"
                        )
                        found = True
                        if start_ms <= timestamp_ms < end_ms:
                            yield AggTrade(
                                symbol,
                                aggregate_id,
                                timestamp_ms,
                                price,
                                quantity,
                                buyer_is_maker,
                            )
    except (OSError, UnicodeError, csv.Error, zipfile.BadZipFile) as error:
        raise StageBError(f"cannot parse {path.name}: {error}") from None
    if not found:
        raise StageBError(f"{path.name}:{expected} has no aggTrades rows")


def load_agg_trades(
    paths: Sequence[Path],
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> Tuple[AggTrade, ...]:
    indexed: Dict[int, AggTrade] = {}
    for path in paths:
        for trade in _agg_trade_rows(path, symbol, start_ms, end_ms):
            existing = indexed.get(trade.aggregate_id)
            if existing is not None and existing != trade:
                raise StageBError(
                    f"conflicting duplicate {symbol} aggTrade {trade.aggregate_id}"
                )
            indexed[trade.aggregate_id] = trade
    return tuple(
        sorted(
            indexed.values(),
            key=lambda trade: (trade.timestamp_ms, trade.aggregate_id),
        )
    )


def quantile(
    values: Sequence[Decimal], numerator: int, denominator: int
) -> Optional[Decimal]:
    """Return an exact, linearly interpolated quantile."""
    if not values:
        return None
    if denominator <= 0 or numerator < 0 or numerator > denominator:
        raise StageBError("invalid quantile")
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
        raise StageBError("quantile contains a non-finite Decimal")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    with localcontext() as context:
        context.prec = 50
        position = (
            Decimal(len(ordered) - 1)
            * Decimal(numerator)
            / Decimal(denominator)
        )
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        fraction = position - Decimal(lower)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _sum_decimal(values: Iterable[Decimal]) -> Decimal:
    total = Decimal("0")
    with localcontext() as context:
        context.prec = 50
        for value in values:
            total += value
    return total


def taker_quote_totals(
    trades: Sequence[AggTrade],
) -> Tuple[Decimal, Decimal, int, int]:
    """Return taker buy/sell quote and aggTrade counts from maker-side truth."""
    buy_quote = Decimal("0")
    sell_quote = Decimal("0")
    buy_count = 0
    sell_count = 0
    with localcontext() as context:
        context.prec = 50
        for trade in trades:
            if not isinstance(trade, AggTrade):
                raise StageBError("taker activity contains a non-trade")
            if trade.buyer_is_maker:
                sell_quote += trade.quote_size
                sell_count += 1
            else:
                buy_quote += trade.quote_size
                buy_count += 1
    return buy_quote, sell_quote, buy_count, sell_count


def taker_imbalance(trades: Sequence[AggTrade]) -> Optional[Decimal]:
    """Return ``(taker buy quote - taker sell quote) / total quote``."""
    buy_quote, sell_quote, _, _ = taker_quote_totals(trades)
    with localcontext() as context:
        context.prec = 50
        total = buy_quote + sell_quote
        if total <= 0:
            return None
        return (buy_quote - sell_quote) / total


def _ratio(
    numerator: Optional[Decimal], denominator: Optional[Decimal]
) -> Optional[Decimal]:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    with localcontext() as context:
        context.prec = 50
        return numerator / denominator


def size_quantile_shift(
    baseline: Sequence[AggTrade], scoring: Sequence[AggTrade]
) -> Dict[str, Dict[str, Optional[Decimal]]]:
    """Measure quote-size quantiles and exact scoring/baseline shifts."""
    before_sizes = [trade.quote_size for trade in baseline]
    after_sizes = [trade.quote_size for trade in scoring]
    result: Dict[str, Dict[str, Optional[Decimal]]] = {
        "before": {},
        "after": {},
        "delta": {},
        "ratio": {},
    }
    for name, numerator, denominator in QUANTILES:
        before = quantile(before_sizes, numerator, denominator)
        after = quantile(after_sizes, numerator, denominator)
        result["before"][name] = before
        result["after"][name] = after
        with localcontext() as context:
            context.prec = 50
            result["delta"][name] = (
                None if before is None or after is None else after - before
            )
        result["ratio"][name] = _ratio(after, before)
    return result


def repeated_large_taker_metrics(
    baseline: Sequence[AggTrade], scoring: Sequence[AggTrade]
) -> Dict[str, Any]:
    """Score repeated same-side trades at/above the baseline quote-size p90.

    The score is the percentage of transitions between large trades that keep
    the same taker side.  A single large print has no repetition and scores 0.
    """
    threshold = quantile(
        [trade.quote_size for trade in baseline], 9, 10
    )
    if threshold is None:
        return {
            "threshold": None,
            "large_count": 0,
            "large_buy_count": 0,
            "large_sell_count": 0,
            "longest_same_side_run": 0,
            "score": None,
        }
    large = [trade for trade in scoring if trade.quote_size >= threshold]
    buy_count = sum(not trade.buyer_is_maker for trade in large)
    sell_count = sum(trade.buyer_is_maker for trade in large)
    longest = 0
    current = 0
    previous: Optional[str] = None
    repeated = 0
    for trade in large:
        side = trade.taker_side
        if side == previous:
            repeated += 1
            current += 1
        else:
            current = 1
        longest = max(longest, current)
        previous = side
    if len(large) < 2:
        score = Decimal("0")
    else:
        with localcontext() as context:
            context.prec = 50
            score = Decimal(repeated) * Decimal("100") / Decimal(len(large) - 1)
    return {
        "threshold": threshold,
        "large_count": len(large),
        "large_buy_count": buy_count,
        "large_sell_count": sell_count,
        "longest_same_side_run": longest,
        "score": score,
    }


def persistence_metrics(
    trades: Sequence[AggTrade], start_ms: int, end_ms: int
) -> Dict[str, Any]:
    """Measure active and buy-dominant minutes after the first print minute.

    Minute buckets are anchored at the event ``start_ms``.  The first bucket
    containing a print identifies the first volume print; persistence is then
    measured over every later bucket through ``end_ms``.  Thus the initiating
    print is not allowed to prove its own persistence.
    """
    if end_ms <= start_ms:
        raise StageBError("persistence interval is empty")
    if any(trade.timestamp_ms < start_ms or trade.timestamp_ms >= end_ms for trade in trades):
        raise StageBError("persistence tape falls outside its interval")
    bucket_count = (end_ms - start_ms + MINUTE_MS - 1) // MINUTE_MS
    buckets: List[List[AggTrade]] = [[] for _ in range(bucket_count)]
    for trade in trades:
        index = (trade.timestamp_ms - start_ms) // MINUTE_MS
        buckets[index].append(trade)
    first_index = next(
        (index for index, bucket in enumerate(buckets) if bucket), None
    )
    if first_index is None:
        return {
            "first_print_ms": None,
            "measured_minutes": 0,
            "active_minutes": 0,
            "buy_dominant_minutes": 0,
            "active_ratio": None,
            "buy_dominant_ratio": None,
            "score": None,
        }
    following = buckets[first_index + 1 :]
    measured = len(following)
    active = sum(bool(bucket) for bucket in following)
    buy_dominant = 0
    for bucket in following:
        imbalance = taker_imbalance(bucket)
        if imbalance is not None and imbalance > 0:
            buy_dominant += 1
    if measured == 0:
        active_ratio = None
        buy_ratio = None
        score = None
    else:
        with localcontext() as context:
            context.prec = 50
            active_ratio = Decimal(active) / Decimal(measured)
            buy_ratio = Decimal(buy_dominant) / Decimal(measured)
            score = active_ratio * Decimal("100")
    return {
        "first_print_ms": trades[0].timestamp_ms,
        "measured_minutes": measured,
        "active_minutes": active,
        "buy_dominant_minutes": buy_dominant,
        "active_ratio": active_ratio,
        "buy_dominant_ratio": buy_ratio,
        "score": score,
    }


def price_move_pct(trades: Sequence[AggTrade]) -> Optional[Decimal]:
    if not trades:
        return None
    first = trades[0].price
    last = trades[-1].price
    with localcontext() as context:
        context.prec = 50
        return ((last / first) - Decimal("1")) * Decimal("100")


def price_range_pct(trades: Sequence[AggTrade]) -> Optional[Decimal]:
    if not trades:
        return None
    low = min(trade.price for trade in trades)
    high = max(trade.price for trade in trades)
    with localcontext() as context:
        context.prec = 50
        return ((high / low) - Decimal("1")) * Decimal("100")


def absorption_score(trades: Sequence[AggTrade]) -> Optional[Decimal]:
    """Return 0..100 buy pressure discounted by realised upward movement.

    ``100 * taker_buy_share / (1 + max(net_move_pct, 0))`` is high only when
    quote-volume buying dominates while the tape fails to move upward.  A
    falling price is not treated as successful price impact by buyers.
    """
    buy_quote, sell_quote, _, _ = taker_quote_totals(trades)
    move = price_move_pct(trades)
    with localcontext() as context:
        context.prec = 50
        total = buy_quote + sell_quote
        if total <= 0 or move is None:
            return None
        upward_move = max(Decimal("0"), move)
        buy_share = buy_quote / total
        return Decimal("100") * buy_share / (Decimal("1") + upward_move)


def _frequency(count: int, duration_ms: int) -> Decimal:
    if duration_ms <= 0:
        raise StageBError("trade-frequency duration is empty")
    with localcontext() as context:
        context.prec = 50
        return Decimal(count) * Decimal(MINUTE_MS) / Decimal(duration_ms)


def _activity_fields(prefix: str, trades: Sequence[AggTrade]) -> Dict[str, Any]:
    buy, sell, buy_count, sell_count = taker_quote_totals(trades)
    with localcontext() as context:
        context.prec = 50
        total = buy + sell
        if total > 0:
            buy_share = buy / total
            sell_share = sell / total
        else:
            buy_share = None
            sell_share = None
    return {
        f"{prefix}taker_buy_quote": decimal_text(buy),
        f"{prefix}taker_sell_quote": decimal_text(sell),
        f"{prefix}taker_buy_count": buy_count,
        f"{prefix}taker_sell_count": sell_count,
        f"{prefix}taker_buy_quote_share": _optional_text(buy_share),
        f"{prefix}taker_sell_quote_share": _optional_text(sell_share),
        f"{prefix}taker_imbalance": _optional_text(taker_imbalance(trades)),
    }


def _quantiles_text(
    values: Mapping[str, Mapping[str, Optional[Decimal]]]
) -> Dict[str, Dict[str, Optional[str]]]:
    return {
        group: {name: _optional_text(value) for name, value in entries.items()}
        for group, entries in values.items()
    }


def _kline_rows(
    path: Path,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> Iterable[KlineBar]:
    parsed = _archive_name_match(path, KLINE_ARCHIVE)
    if parsed is None or parsed[1] != symbol:
        raise StageBError(f"kline archive does not match {symbol}: {path.name}")
    archive_day = _archive_day(path, KLINE_ARCHIVE)
    expected = path.stem + ".csv"
    archive, member = _single_zip_member(path, expected)
    found = False
    try:
        with archive:
            chunks = _verified_member_chunks(path, archive, member)
            raw = io.BufferedReader(_ChunkStream(chunks))
            with raw:
                with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as handle:
                    for line_number, row in enumerate(csv.reader(handle), start=1):
                        if not row or not any(cell.strip() for cell in row):
                            continue
                        first = row[0].strip().lower().replace(" ", "_")
                        if not found and first in {"open_time", "opentime"}:
                            continue
                        source = f"{path.name}:{expected}:{line_number}"
                        if len(row) < 7:
                            raise StageBError(
                                f"{source} has fewer than seven kline columns"
                            )
                        open_ms = _timestamp_ms(row[0], f"{source} open time")
                        high = _decimal(row[2], f"{source} high", positive=True)
                        low = _decimal(row[3], f"{source} low", positive=True)
                        close_ms = _timestamp_ms(row[6], f"{source} close time")
                        if close_ms - open_ms not in {59_999, 60_000}:
                            raise StageBError(f"{source} is not a one-minute kline")
                        if high < low:
                            raise StageBError(f"{source} has high below low")
                        actual_day = (
                            EPOCH + timedelta(milliseconds=open_ms)
                        ).date()
                        if actual_day != archive_day:
                            raise StageBError(f"{source} is outside archive day")
                        found = True
                        if start_ms <= open_ms < end_ms:
                            yield KlineBar(symbol, open_ms, close_ms, high, low)
    except (OSError, UnicodeError, csv.Error, zipfile.BadZipFile) as error:
        raise StageBError(f"cannot parse {path.name}: {error}") from None
    if not found:
        raise StageBError(f"{path.name}:{expected} has no kline rows")


def load_klines(
    paths: Sequence[Path], symbol: str, start_ms: int, end_ms: int
) -> Tuple[KlineBar, ...]:
    indexed: Dict[int, KlineBar] = {}
    for path in paths:
        for bar in _kline_rows(path, symbol, start_ms, end_ms):
            existing = indexed.get(bar.open_ms)
            if existing is not None and existing != bar:
                raise StageBError(
                    f"conflicting duplicate {symbol} kline at {utc_text(bar.open_ms)}"
                )
            indexed[bar.open_ms] = bar
    return tuple(indexed[key] for key in sorted(indexed))


def _spread_proxy(bars: Sequence[KlineBar]) -> Optional[Decimal]:
    ranges: List[Decimal] = []
    with localcontext() as context:
        context.prec = 50
        for bar in bars:
            ranges.append(((bar.high / bar.low) - Decimal("1")) * Decimal("100"))
    return quantile(ranges, 1, 2)


def _complete_minute_bars(
    bars: Sequence[KlineBar], start_ms: int, end_ms: int
) -> bool:
    """Return whether every minute-open intersecting the interval is present."""
    first_open = ((start_ms + MINUTE_MS - 1) // MINUTE_MS) * MINUTE_MS
    expected = tuple(range(first_open, end_ms, MINUTE_MS))
    actual = tuple(bar.open_ms for bar in bars)
    return bool(expected) and actual == expected


def _normalize_key(raw: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(raw).lower())


CONTEXT_ALIASES = {
    "timestamp": (
        "timestamp",
        "timestampms",
        "ts",
        "tsms",
        "time",
        "fundingtime",
        "calctime",
        "opentime",
    ),
    "funding": ("fundingrate", "lastfundingrate", "rate"),
    "open_interest": (
        "openinterest",
        "sumopeninterest",
        "oi",
    ),
}


def _records_from_json(value: Any, source: str) -> List[Mapping[str, Any]]:
    if isinstance(value, list):
        rows = value
    elif isinstance(value, Mapping):
        rows = None
        for key in ("data", "rows", "result", "points"):
            if isinstance(value.get(key), list):
                rows = value[key]
                break
        if rows is None:
            rows = [value]
    else:
        raise StageBError(f"{source} context JSON is not row-shaped")
    if any(not isinstance(row, Mapping) for row in rows):
        raise StageBError(f"{source} context contains a non-object row")
    return list(rows)


def _context_records(name: str, text: str) -> List[Mapping[str, Any]]:
    lowered = name.lower()
    if lowered.endswith(".jsonl"):
        rows: List[Mapping[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            value = _json_loads(line, f"{name}:{line_number}")
            rows.extend(_records_from_json(value, f"{name}:{line_number}"))
        return rows
    if lowered.endswith(".json"):
        return _records_from_json(_json_loads(text, name), name)
    try:
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            raise StageBError(f"{name} context CSV has no header")
        return [dict(row) for row in reader]
    except csv.Error as error:
        raise StageBError(f"{name} context CSV is invalid: {error}") from None


def _read_context_file(path: Path) -> List[Mapping[str, Any]]:
    if path.suffix.lower() != ".zip":
        try:
            return _context_records(path.name, path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError) as error:
            raise StageBError(f"cannot read {path.name}: {error}") from None
    try:
        with zipfile.ZipFile(path, "r") as archive:
            members = sorted(
                member
                for member in archive.infolist()
                if not member.is_dir()
                and member.filename.lower().endswith((".csv", ".json", ".jsonl"))
            )
            if not members:
                raise StageBError(f"{path.name} has no context data member")
            rows: List[Mapping[str, Any]] = []
            for member in members:
                if member.flag_bits & 0x1:
                    raise StageBError(f"{path.name}:{member.filename} is encrypted")
                text = b"".join(
                    _verified_member_chunks(path, archive, member)
                ).decode("utf-8-sig")
                rows.extend(_context_records(member.filename, text))
            return rows
    except (OSError, UnicodeError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise StageBError(f"cannot read {path.name}: {error}") from None


def load_context_points(
    paths: Sequence[Path], kind: str, symbol: str
) -> Tuple[ContextPoint, ...]:
    if kind not in {"funding", "open_interest"}:
        raise StageBError("unknown context kind")
    indexed: Dict[int, ContextPoint] = {}
    for path in paths:
        for row_number, row in enumerate(_read_context_file(path), start=1):
            normalized = {_normalize_key(key): value for key, value in row.items()}
            row_symbol = normalized.get("symbol")
            if row_symbol is not None and str(row_symbol).strip().upper() != symbol:
                continue
            timestamp_raw = next(
                (
                    normalized[key]
                    for key in CONTEXT_ALIASES["timestamp"]
                    if key in normalized and normalized[key] not in {None, ""}
                ),
                None,
            )
            value_raw = next(
                (
                    normalized[key]
                    for key in CONTEXT_ALIASES[kind]
                    if key in normalized and normalized[key] not in {None, ""}
                ),
                None,
            )
            if timestamp_raw is None or value_raw is None:
                continue
            source = f"{path.name}:{row_number}"
            point = ContextPoint(
                _timestamp_ms(timestamp_raw, f"{source} timestamp"),
                _decimal(
                    value_raw,
                    f"{source} {kind}",
                    non_negative=(kind == "open_interest"),
                ),
            )
            existing = indexed.get(point.timestamp_ms)
            if existing is not None and existing != point:
                raise StageBError(
                    f"conflicting duplicate {kind} point at "
                    f"{utc_text(point.timestamp_ms)}"
                )
            indexed[point.timestamp_ms] = point
    if not indexed:
        raise StageBError(f"context files have no usable {kind} rows")
    return tuple(indexed[key] for key in sorted(indexed))


def _discover_context(data_dir: Path, symbol: str, kind: str) -> Tuple[Path, ...]:
    root = Path(data_dir)
    if not root.is_dir():
        return ()
    found = []
    symbol_lower = symbol.lower()
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {
            ".csv",
            ".json",
            ".jsonl",
            ".zip",
        }:
            continue
        compact = _normalize_key(path.name)
        if symbol_lower not in path.name.lower():
            continue
        if kind == "funding" and "funding" in compact:
            found.append(path)
        if kind == "open_interest" and (
            "openinterest" in compact or re.search(r"(?:^|[-_])oi(?:[-_.]|$)", path.name.lower())
        ):
            found.append(path)
    return tuple(sorted(set(found)))


def _context_sources(
    spec: WindowSpec, data_dir: Path, kind: str
) -> Tuple[Path, ...]:
    explicit = _explicit_sources(spec, kind)
    if explicit:
        paths = tuple(_safe_source_path(data_dir, item) for item in explicit)
        return tuple(path for path in paths if path.is_file())
    return _discover_context(data_dir, spec.symbol, kind)


def _context_values(
    points: Sequence[ContextPoint], event_ms: int, end_ms: int
) -> Tuple[Optional[ContextPoint], Optional[ContextPoint]]:
    before = next(
        (point for point in reversed(points) if point.timestamp_ms < event_ms),
        None,
    )
    after = next(
        (
            point
            for point in reversed(points)
            if event_ms <= point.timestamp_ms < end_ms
        ),
        None,
    )
    return before, after


def _relative(path: Path, data_dir: Path) -> str:
    try:
        return path.resolve().relative_to(Path(data_dir).resolve()).as_posix()
    except ValueError:
        return path.name


def _base_row(spec: WindowSpec) -> Dict[str, Any]:
    return {
        "schema": SCHEMA,
        "kind": "stage_b_features",
        "manifest_index": spec.manifest_index,
        "window_id": spec.window_id,
        "event_id": spec.event_id,
        "symbol": spec.symbol,
        "venue_class": spec.venue_class,
        "venue": spec.venue,
        "label": spec.label,
        "window_start_utc": utc_text(spec.window_start_ms),
        "event_utc": utc_text(spec.event_ms),
        "window_end_utc": utc_text(spec.window_end_ms),
    }


def _skip_row(
    spec: WindowSpec,
    reason: str,
    *,
    missing_files: Sequence[str] = (),
    detail: Optional[str] = None,
) -> Dict[str, Any]:
    row = _base_row(spec)
    row.update(
        {
            "status": "skipped",
            "skip_reason": reason,
            "skip_detail": detail,
            "missing_files": list(missing_files),
            "source_files": [],
            "unavailable": [],
            "features": None,
        }
    )
    return row


def extract_features(
    spec: WindowSpec,
    trades: Sequence[AggTrade],
    *,
    klines: Sequence[KlineBar] = (),
    funding_points: Sequence[ContextPoint] = (),
    open_interest_points: Sequence[ContextPoint] = (),
    unavailable: Sequence[str] = (),
    source_files: Sequence[str] = (),
) -> Dict[str, Any]:
    """Build one complete Stage-B feature row from a proved manifest window."""
    ordered = tuple(sorted(trades, key=lambda item: (item.timestamp_ms, item.aggregate_id)))
    if any(trade.symbol != spec.symbol for trade in ordered):
        raise StageBError("feature tape contains another symbol")
    if any(
        trade.timestamp_ms < spec.window_start_ms
        or trade.timestamp_ms >= spec.window_end_ms
        for trade in ordered
    ):
        raise StageBError("feature tape falls outside manifest window")
    baseline = tuple(
        trade for trade in ordered if trade.timestamp_ms < spec.event_ms
    )
    scoring = tuple(
        trade for trade in ordered if trade.timestamp_ms >= spec.event_ms
    )
    before_frequency = _frequency(
        len(baseline), spec.event_ms - spec.window_start_ms
    )
    after_frequency = _frequency(
        len(scoring), spec.window_end_ms - spec.event_ms
    )
    with localcontext() as context:
        context.prec = 50
        frequency_delta = after_frequency - before_frequency
    frequency_ratio = _ratio(after_frequency, before_frequency)
    before_imbalance = taker_imbalance(baseline)
    after_imbalance = taker_imbalance(scoring)
    with localcontext() as context:
        context.prec = 50
        imbalance_shift = (
            None
            if before_imbalance is None or after_imbalance is None
            else after_imbalance - before_imbalance
        )
    sizes = size_quantile_shift(baseline, scoring)
    repeated = repeated_large_taker_metrics(baseline, scoring)
    persistence = persistence_metrics(
        scoring, spec.event_ms, spec.window_end_ms
    )

    baseline_bars = tuple(bar for bar in klines if bar.open_ms < spec.event_ms)
    scoring_bars = tuple(bar for bar in klines if bar.open_ms >= spec.event_ms)
    spread_before = _spread_proxy(baseline_bars)
    spread_after = _spread_proxy(scoring_bars)
    with localcontext() as context:
        context.prec = 50
        spread_delta = (
            None
            if spread_before is None or spread_after is None
            else spread_after - spread_before
        )

    funding_before, funding_after = _context_values(
        funding_points, spec.event_ms, spec.window_end_ms
    )
    oi_before, oi_after = _context_values(
        open_interest_points, spec.event_ms, spec.window_end_ms
    )
    with localcontext() as context:
        context.prec = 50
        funding_change = (
            None
            if funding_before is None or funding_after is None
            else funding_after.value - funding_before.value
        )
        oi_change = (
            None
            if oi_before is None or oi_after is None
            else oi_after.value - oi_before.value
        )
        oi_change_pct = _ratio(
            oi_change, None if oi_before is None else oi_before.value
        )
        if oi_change_pct is not None:
            oi_change_pct *= Decimal("100")

        baseline_minutes = (
            Decimal(spec.event_ms - spec.window_start_ms) / Decimal(MINUTE_MS)
        )
        post_minutes = (
            Decimal(spec.window_end_ms - spec.event_ms) / Decimal(MINUTE_MS)
        )

    features: Dict[str, Any] = {
        "baseline_minutes": decimal_text(baseline_minutes),
        "post_minutes": decimal_text(post_minutes),
        "baseline_trade_count": len(baseline),
        "trade_count": len(scoring),
        "trade_frequency_before_per_min": decimal_text(before_frequency),
        "trade_frequency_after_per_min": decimal_text(after_frequency),
        "trade_frequency_delta_per_min": decimal_text(frequency_delta),
        "trade_frequency_ratio": _optional_text(frequency_ratio),
        "taker_imbalance_shift": _optional_text(imbalance_shift),
        "trade_size_quantiles_quote": _quantiles_text(sizes),
        "repeated_large_taker_threshold_quote": _optional_text(repeated["threshold"]),
        "repeated_large_taker_count": repeated["large_count"],
        "repeated_large_taker_buy_count": repeated["large_buy_count"],
        "repeated_large_taker_sell_count": repeated["large_sell_count"],
        "repeated_large_taker_longest_same_side_run": repeated[
            "longest_same_side_run"
        ],
        "repeated_large_taker_score": _optional_text(repeated["score"]),
        "first_volume_print_utc": (
            None
            if persistence["first_print_ms"] is None
            else utc_text(persistence["first_print_ms"])
        ),
        "persistence_measured_minutes": persistence["measured_minutes"],
        "persistence_active_minutes": persistence["active_minutes"],
        "persistence_buy_dominant_minutes": persistence["buy_dominant_minutes"],
        "persistence_active_minute_ratio": _optional_text(
            persistence["active_ratio"]
        ),
        "persistence_buy_dominant_minute_ratio": _optional_text(
            persistence["buy_dominant_ratio"]
        ),
        "persistence_score": _optional_text(persistence["score"]),
        "price_move_pct": _optional_text(price_move_pct(scoring)),
        "price_range_pct": _optional_text(price_range_pct(scoring)),
        "absorption_score": _optional_text(absorption_score(scoring)),
        "spread_proxy_before_pct": _optional_text(spread_before),
        "spread_proxy_pct": _optional_text(spread_after),
        "spread_proxy_delta_pct": _optional_text(spread_delta),
        "spread_proxy_kline_count_before": len(baseline_bars),
        "spread_proxy_kline_count": len(scoring_bars),
        "funding_rate_before": _optional_text(
            None if funding_before is None else funding_before.value
        ),
        "funding_rate_after": _optional_text(
            None if funding_after is None else funding_after.value
        ),
        "funding_rate_change": _optional_text(funding_change),
        "funding_before_utc": (
            None if funding_before is None else utc_text(funding_before.timestamp_ms)
        ),
        "funding_after_utc": (
            None if funding_after is None else utc_text(funding_after.timestamp_ms)
        ),
        "open_interest_before": _optional_text(
            None if oi_before is None else oi_before.value
        ),
        "open_interest_after": _optional_text(
            None if oi_after is None else oi_after.value
        ),
        "open_interest_change": _optional_text(oi_change),
        "open_interest_change_pct": _optional_text(oi_change_pct),
        "open_interest_before_utc": (
            None if oi_before is None else utc_text(oi_before.timestamp_ms)
        ),
        "open_interest_after_utc": (
            None if oi_after is None else utc_text(oi_after.timestamp_ms)
        ),
    }
    features.update(_activity_fields("baseline_", baseline))
    features.update(_activity_fields("", scoring))
    row = _base_row(spec)
    row.update(
        {
            "status": "ok",
            "skip_reason": None,
            "skip_detail": None,
            "missing_files": [],
            "source_files": sorted(set(source_files)),
            "unavailable": sorted(set(unavailable)),
            "features": features,
        }
    )
    return row


def process_window(spec: WindowSpec, data_dir: Path) -> Dict[str, Any]:
    try:
        agg_paths, missing = _daily_sources(spec, data_dir, "agg")
    except StageBError as error:
        return _skip_row(spec, "invalid_agg_trades_source", detail=str(error))
    if missing:
        return _skip_row(
            spec,
            "missing_agg_trades_zip",
            missing_files=missing,
        )
    try:
        trades = load_agg_trades(
            agg_paths,
            spec.symbol,
            spec.window_start_ms,
            spec.window_end_ms,
        )
    except StageBError as error:
        return _skip_row(
            spec,
            "invalid_agg_trades_zip",
            detail=str(error),
        )

    unavailable: List[str] = []
    source_paths: List[Path] = list(agg_paths)
    klines: Tuple[KlineBar, ...] = ()
    try:
        kline_paths, kline_missing = _daily_sources(spec, data_dir, "kline")
        if kline_missing:
            unavailable.append("spread_proxy_missing")
        else:
            klines = load_klines(
                kline_paths,
                spec.symbol,
                spec.window_start_ms,
                spec.window_end_ms,
            )
            source_paths.extend(kline_paths)
            baseline_bars = tuple(
                bar for bar in klines if bar.open_ms < spec.event_ms
            )
            scoring_bars = tuple(
                bar for bar in klines if bar.open_ms >= spec.event_ms
            )
            if not _complete_minute_bars(
                baseline_bars, spec.window_start_ms, spec.event_ms
            ) or not _complete_minute_bars(
                scoring_bars, spec.event_ms, spec.window_end_ms
            ):
                klines = ()
                unavailable.append("spread_proxy_partial")
    except StageBError:
        unavailable.append("spread_proxy_invalid")

    funding_points: Tuple[ContextPoint, ...] = ()
    oi_points: Tuple[ContextPoint, ...] = ()
    if spec.venue_class in {"futures_only", "both"}:
        for kind in ("funding", "open_interest"):
            try:
                paths = _context_sources(spec, data_dir, kind)
                if not paths:
                    unavailable.append(f"{kind}_missing")
                    continue
                points = load_context_points(paths, kind, spec.symbol)
                source_paths.extend(paths)
                before, after = _context_values(
                    points, spec.event_ms, spec.window_end_ms
                )
                if before is None or after is None:
                    unavailable.append(f"{kind}_incomplete")
                if kind == "funding":
                    funding_points = points
                else:
                    oi_points = points
            except StageBError:
                unavailable.append(f"{kind}_invalid")
    else:
        unavailable.extend(("funding_not_applicable", "open_interest_not_applicable"))

    return extract_features(
        spec,
        trades,
        klines=klines,
        funding_points=funding_points,
        open_interest_points=oi_points,
        unavailable=unavailable,
        source_files=[_relative(path, data_dir) for path in source_paths],
    )


def _assert_no_float(value: Any, source: str = "output") -> None:
    if isinstance(value, float):
        raise StageBError(f"{source} contains a float")
    if isinstance(value, Decimal):
        raise StageBError(f"{source} contains an unrendered Decimal")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_no_float(item, f"{source}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_float(item, f"{source}[{index}]")


def _jsonl_line(row: Mapping[str, Any]) -> str:
    _assert_no_float(row)
    return json.dumps(
        dict(row), sort_keys=True, separators=(",", ":")
    ) + "\n"


def write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(_jsonl_line(row))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except (OSError, TypeError, ValueError) as error:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise StageBError(f"cannot write Stage-B output {target}: {error}") \
            from None


def _checkpoint_path(output_path: Path) -> Path:
    target = Path(output_path)
    return target.with_name(target.name + ".checkpoint")


def _checkpoint_rows(
    path: Path, specs: Sequence[WindowSpec]
) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise StageBError(f"cannot read Stage-B checkpoint {path}: {error}") \
            from None
    complete_bytes = raw.rfind(b"\n") + 1
    complete = raw[:complete_bytes]
    if complete_bytes != len(raw):
        try:
            with open(path, "r+b") as handle:
                handle.truncate(complete_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise StageBError(
                f"cannot repair Stage-B checkpoint {path}: {error}"
            ) from None
    try:
        lines = complete.decode("utf-8").splitlines()
    except UnicodeError as error:
        raise StageBError(f"Stage-B checkpoint {path} is not UTF-8: {error}") \
            from None
    if len(lines) > len(specs):
        raise StageBError("Stage-B checkpoint has more rows than the manifest")
    rows: List[Dict[str, Any]] = []
    for index, line in enumerate(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise StageBError(
                f"Stage-B checkpoint row {index} is not valid JSON: {error.msg}"
            ) from None
        if not isinstance(row, Mapping):
            raise StageBError(f"Stage-B checkpoint row {index} is not an object")
        expected = _base_row(specs[index])
        if any(row.get(key) != value for key, value in expected.items()):
            raise StageBError(
                f"Stage-B checkpoint row {index} does not match the manifest"
            )
        expected_keys = set(_skip_row(specs[index], "checkpoint"))
        if (
            set(row) != expected_keys
            or row.get("status") not in ("ok", "skipped")
        ):
            raise StageBError(
                f"Stage-B checkpoint row {index} has an invalid schema"
            )
        _assert_no_float(row, f"checkpoint[{index}]")
        rows.append(dict(row))
    return rows


def run_stage_b(
    manifest_path: Path = DEFAULT_MANIFEST,
    data_dir: Path = DEFAULT_DATA_DIR,
    output_path: Path = DEFAULT_OUTPUT,
) -> Tuple[Dict[str, Any], ...]:
    specs = load_manifest(Path(manifest_path))
    root = Path(data_dir)
    target = Path(output_path)
    checkpoint = _checkpoint_path(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise StageBError(f"cannot prepare Stage-B output {target}: {error}") \
            from None
    rows = _checkpoint_rows(checkpoint, specs)
    started_ns = time.monotonic_ns()
    try:
        with open(checkpoint, "a", encoding="utf-8") as handle:
            for index in range(len(rows), len(specs)):
                spec = specs[index]
                row = process_window(spec, root)
                handle.write(_jsonl_line(row))
                handle.flush()
                os.fsync(handle.fileno())
                rows.append(row)
                done = index + 1
                if done % PROGRESS_WINDOW_INTERVAL == 0:
                    elapsed_seconds = (
                        time.monotonic_ns() - started_ns
                    ) // 1_000_000_000
                    print(
                        "STAGE-B-PROGRESS | "
                        f"done={done}/{len(specs)} | "
                        f"elapsed_seconds={elapsed_seconds} | "
                        f"symbol={spec.symbol}",
                        file=sys.stderr,
                        flush=True,
                    )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(checkpoint, target)
    except (OSError, TypeError, ValueError) as error:
        raise StageBError(f"cannot write Stage-B output {target}: {error}") \
            from None
    return tuple(rows)


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract deterministic Stage-B features from local manifest-selected "
            "aggTrades archives."
        )
    )
    parser.add_argument(
        "--manifest",
        "--windows-manifest",
        dest="manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="windows_manifest.json path",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="root containing local Binance archives",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="features.jsonl path",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = argument_parser().parse_args(argv)
    try:
        rows = run_stage_b(args.manifest, args.data_dir, args.output)
    except StageBError as error:
        print(f"stage_b: {error}", file=sys.stderr)
        return 2
    skipped = sum(row["status"] == "skipped" for row in rows)
    print(
        "STAGE-B | "
        f"windows={len(rows)} | features={len(rows) - skipped} | "
        f"skipped={skipped} | output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
