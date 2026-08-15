#!/usr/bin/env python3
"""Pull verified Binance Vision 1m tapes for explicit V2 manifests.

Only unsigned public GETs are possible here.  The allowed metadata reads are
the spot and USD-M exchangeInfo documents, USD-M funding history, and Binance
Vision daily spot or USD-M kline and aggTrades paths.  This module never
imports an execution or money-mutation module.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


SPOT_EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"
FUTURES_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
FUNDING_RATE_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
VISION_BASE_URL = "https://data.binance.vision"
DEFAULT_DEST = Path(__file__).resolve().parent / "data"
DEFAULT_COHORT = 100
DEFAULT_DAYS = 3
MAX_COHORT = 1_000
MAX_DAYS = 366
MAX_WORKERS = 4
HTTP_TIMEOUT_SECONDS = 30.0
MAX_429_RETRIES = 4
MAX_METADATA_BYTES = 24_000_000
MAX_ARCHIVE_BYTES = 128_000_000
# ZIP members are verified by byte-accounted streaming, so high-volume daily
# aggTrades CSVs may safely reach 1 GiB without being held in memory.
MAX_MEMBER_BYTES = 1_073_741_824
MAX_MANIFEST_BYTES = 32_000_000
MAX_MANIFEST_WINDOWS = 1_000_000
MAX_WINDOW_DURATION_MS = 24 * 60 * 60 * 1_000
FUNDING_PAGE_LIMIT = 1_000
MAX_FUNDING_PAGES = 16
READ_CHUNK_BYTES = 128 * 1024
USER_AGENT = "quant-study-tape-pull/1"
COMPLETED_WITH_FAILURES_EXIT_CODE = 2
VENUE_SPOT = "spot"
VENUE_FUTURES = "futures"
VENUE_CLASS_VENUES = {
    "spot_only": (VENUE_SPOT,),
    "futures_only": (VENUE_FUTURES,),
    "both": (VENUE_SPOT, VENUE_FUTURES),
}


class TapePullError(Exception):
    """Expected, factual pull failure."""


class InputError(TapePullError):
    """Invalid local argument or fixture data."""


class PublicReadError(TapePullError):
    """Unsigned public GET failed."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class ArchiveMissing(PublicReadError):
    """The requested daily public archive does not exist."""


class ArchiveError(TapePullError):
    """A file is not a usable, intact expected archive."""

    def __init__(
        self,
        message: str,
        *,
        measured_value: Optional[Any] = None,
        bound: Optional[Any] = None,
    ) -> None:
        super().__init__(message)
        self.measured_value = measured_value
        self.bound = bound


@dataclass(frozen=True)
class DownloadResult:
    status: str
    symbol: str
    day: date
    path: Path
    size_bytes: int
    venue: str = VENUE_SPOT


@dataclass(frozen=True, order=True)
class ArchiveRequest:
    symbol: str
    venue: str
    day: date


@dataclass(frozen=True)
class ArchiveFailure:
    request: ArchiveRequest
    error_type: str
    reason: str
    measured_value: str
    bound: str


@dataclass(frozen=True)
class ManifestSymbol:
    symbol: str
    venue_class: str


@dataclass(frozen=True)
class WindowSpec:
    symbol: str
    venue_class: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class ManifestPullSummary:
    mode: str
    symbols: Tuple[str, ...]
    requests: int
    downloaded: int
    skipped: int
    gaps: Tuple[ArchiveRequest, ...]
    failures: Tuple[ArchiveFailure, ...] = ()


@dataclass(frozen=True)
class FundingResult:
    status: str
    symbol: str
    start_day: date
    end_day: date
    path: Path
    size_bytes: int
    records: int


@dataclass(frozen=True)
class FundingPullSummary:
    symbols: Tuple[str, ...]
    downloaded: int
    skipped: int
    records: int


@dataclass(frozen=True)
class RankedArchive:
    symbol: str
    quote_volume: Decimal
    path: Path
    already_at_destination: bool


@dataclass(frozen=True)
class PullSummary:
    candidates: int
    ranked: int
    selected: Tuple[str, ...]
    downloaded: int
    skipped: int
    gaps: Tuple[Tuple[str, date], ...]
    days: Tuple[date, ...]
    failures: Tuple[ArchiveFailure, ...] = ()


def _archive_path_allowed(path: str) -> bool:
    decoded = unquote(path)
    if quote(decoded, safe="/-") != path:
        return False
    parts = decoded.split("/")
    if parts[:3] == ["", "data", "spot"]:
        tail = parts[3:]
    elif parts[:4] == ["", "data", "futures", "um"]:
        tail = parts[4:]
    else:
        return False
    if len(tail) == 5 and tail[:2] == ["daily", "klines"] and tail[3] == "1m":
        symbol = tail[2]
        filename = tail[4]
        marker = "%s-1m-" % symbol
    elif len(tail) == 4 and tail[:2] == ["daily", "aggTrades"]:
        symbol = tail[2]
        filename = tail[3]
        marker = "%s-aggTrades-" % symbol
    else:
        return False
    try:
        _validate_symbol(symbol)
    except InputError:
        return False
    if not filename.startswith(marker) or not filename.endswith(".zip"):
        return False
    day_text = filename[len(marker) : -4]
    try:
        return _coerce_day(day_text).isoformat() == day_text
    except InputError:
        return False


def _validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.fragment
    ):
        raise InputError("public URL is outside the GET allowlist")
    if (
        parsed.netloc == "api.binance.com"
        and parsed.path == "/api/v3/exchangeInfo"
        and not parsed.query
    ):
        return
    if (
        parsed.netloc == "fapi.binance.com"
        and parsed.path == "/fapi/v1/exchangeInfo"
        and not parsed.query
    ):
        return
    if (
        parsed.netloc == "data.binance.vision"
        and not parsed.query
        and _archive_path_allowed(parsed.path)
    ):
        return
    if (
        parsed.netloc == "fapi.binance.com"
        and parsed.path == "/fapi/v1/fundingRate"
    ):
        try:
            pairs = parse_qsl(
                parsed.query, keep_blank_values=True, strict_parsing=True
            )
        except ValueError as error:
            raise InputError("public URL is outside the GET allowlist") from error
        if len(pairs) != 4 or len({key for key, _ in pairs}) != 4:
            raise InputError("public URL is outside the GET allowlist")
        values = dict(pairs)
        if set(values) != {"symbol", "startTime", "endTime", "limit"}:
            raise InputError("public URL is outside the GET allowlist")
        _validate_symbol(values["symbol"])
        if not values["startTime"].isdigit() or not values["endTime"].isdigit():
            raise InputError("public URL is outside the GET allowlist")
        start_time = int(values["startTime"])
        end_time = int(values["endTime"])
        try:
            limit = int(values["limit"])
        except ValueError as error:
            raise InputError("public URL is outside the GET allowlist") from error
        if (
            start_time < 0
            or end_time < start_time
            or str(start_time) != values["startTime"]
            or str(end_time) != values["endTime"]
            or not 1 <= limit <= FUNDING_PAGE_LIMIT
            or str(limit) != values["limit"]
        ):
            raise InputError("public URL is outside the GET allowlist")
        return
    raise InputError("public URL is outside the GET allowlist")


class _AllowlistedRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> Optional[Request]:
        _validate_public_url(new_url)
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )


_PUBLIC_OPENER = build_opener(_AllowlistedRedirectHandler())


def _retry_after_seconds(headers: Any, attempt: int) -> int:
    value = headers.get("Retry-After") if headers is not None else None
    try:
        parsed = int(value) if value is not None else 0
    except (TypeError, ValueError):
        parsed = 0
    if parsed > 0:
        return min(parsed, 60)
    return min(2**attempt, 60)


def _open_get(
    url: str,
    *,
    opener: Optional[Callable[..., Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> Any:
    """Open one allowlisted GET, backing off and retrying only HTTP 429."""
    _validate_public_url(url)
    open_call = opener if opener is not None else _PUBLIC_OPENER.open
    request = Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    for attempt in range(MAX_429_RETRIES + 1):
        try:
            response = open_call(request, timeout=timeout)
            final_url = response.geturl() if hasattr(response, "geturl") else url
            try:
                _validate_public_url(final_url)
            except TapePullError:
                response.close()
                raise
            status = getattr(response, "status", None)
            if status is None and hasattr(response, "getcode"):
                status = response.getcode()
            if status == 429:
                headers = getattr(response, "headers", None)
                response.close()
                if attempt == MAX_429_RETRIES:
                    raise PublicReadError("HTTP 429 retry limit reached", status=429)
                sleep_fn(_retry_after_seconds(headers, attempt))
                continue
            if isinstance(status, int) and status >= 400:
                response.close()
                raise PublicReadError("public GET returned HTTP %d" % status, status)
            return response
        except HTTPError as error:
            try:
                if error.code != 429:
                    raise PublicReadError(
                        "public GET returned HTTP %d" % error.code, error.code
                    ) from error
                if attempt == MAX_429_RETRIES:
                    raise PublicReadError(
                        "HTTP 429 retry limit reached", status=429
                    ) from error
                sleep_fn(_retry_after_seconds(error.headers, attempt))
            finally:
                if getattr(error, "fp", None) is not None:
                    error.close()
        except (TimeoutError, URLError, OSError) as error:
            raise PublicReadError("public GET transport failed") from error
    raise PublicReadError("HTTP 429 retry limit reached", status=429)


def fetch_json(
    url: str,
    *,
    opener: Optional[Callable[..., Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Any:
    response = _open_get(url, opener=opener, sleep_fn=sleep_fn)
    try:
        raw = response.read(MAX_METADATA_BYTES + 1)
    finally:
        response.close()
    if len(raw) > MAX_METADATA_BYTES:
        raise PublicReadError("exchangeInfo response exceeded byte limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicReadError("exchangeInfo response is not valid JSON") from error


def _coerce_day(value: Any) -> date:
    if type(value) is date:
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise InputError("day must be YYYY-MM-DD") from error
        if parsed.isoformat() != value:
            raise InputError("day must be YYYY-MM-DD")
        return parsed
    raise InputError("day must be a date or YYYY-MM-DD string")


def _validate_symbol(symbol: Any) -> str:
    if (
        not isinstance(symbol, str)
        or not 5 <= len(symbol) <= 100
        or not symbol.endswith("USDT")
        or not symbol.isalnum()
        or symbol.upper() != symbol
    ):
        raise InputError("symbol must be a safe uppercase USDT symbol")
    return symbol


def _validate_venue(venue: Any) -> str:
    if venue in {VENUE_FUTURES, "perp", "um"}:
        return VENUE_FUTURES
    if venue == VENUE_SPOT:
        return VENUE_SPOT
    raise InputError("venue must be spot or futures")


def _venue_archive_prefix(venue: Any) -> str:
    return "spot" if _validate_venue(venue) == VENUE_SPOT else "futures/um"


def _venues_for_class(venue_class: Any) -> Tuple[str, ...]:
    if not isinstance(venue_class, str) or venue_class not in VENUE_CLASS_VENUES:
        raise InputError(
            "venue_class must be spot_only, futures_only, or both"
        )
    return VENUE_CLASS_VENUES[venue_class]


def kline_url(symbol: str, day: Any, venue: str = VENUE_SPOT) -> str:
    symbol = _validate_symbol(symbol)
    day = _coerce_day(day)
    prefix = _venue_archive_prefix(venue)
    encoded_symbol = quote(symbol, safe="")
    filename = "%s-1m-%s.zip" % (encoded_symbol, day.isoformat())
    return "%s/data/%s/daily/klines/%s/1m/%s" % (
        VISION_BASE_URL,
        prefix,
        encoded_symbol,
        filename,
    )


def kline_path(
    destination: Path,
    symbol: str,
    day: Any,
    venue: str = VENUE_SPOT,
) -> Path:
    symbol = _validate_symbol(symbol)
    day = _coerce_day(day)
    prefix = _venue_archive_prefix(venue)
    filename = "%s-1m-%s.zip" % (symbol, day.isoformat())
    return (
        Path(destination)
        / prefix
        / "daily"
        / "klines"
        / symbol
        / "1m"
        / filename
    )


def agg_trades_url(
    symbol: str, day: Any, venue: str = VENUE_SPOT
) -> str:
    symbol = _validate_symbol(symbol)
    day = _coerce_day(day)
    prefix = _venue_archive_prefix(venue)
    encoded_symbol = quote(symbol, safe="")
    filename = "%s-aggTrades-%s.zip" % (encoded_symbol, day.isoformat())
    return "%s/data/%s/daily/aggTrades/%s/%s" % (
        VISION_BASE_URL,
        prefix,
        encoded_symbol,
        filename,
    )


def agg_trades_path(
    destination: Path,
    symbol: str,
    day: Any,
    venue: str = VENUE_SPOT,
) -> Path:
    symbol = _validate_symbol(symbol)
    day = _coerce_day(day)
    prefix = _venue_archive_prefix(venue)
    filename = "%s-aggTrades-%s.zip" % (symbol, day.isoformat())
    return Path(destination) / prefix / "daily" / "aggTrades" / symbol / filename


def funding_rate_url(
    symbol: str,
    start_time_ms: int,
    end_time_ms: int,
    limit: int = FUNDING_PAGE_LIMIT,
) -> str:
    symbol = _validate_symbol(symbol)
    if (
        isinstance(start_time_ms, bool)
        or not isinstance(start_time_ms, int)
        or isinstance(end_time_ms, bool)
        or not isinstance(end_time_ms, int)
        or start_time_ms < 0
        or end_time_ms < start_time_ms
    ):
        raise InputError("funding timestamps must be ordered nonnegative integers")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= FUNDING_PAGE_LIMIT
    ):
        raise InputError("funding page limit is outside bounds")
    query = urlencode(
        (
            ("symbol", symbol),
            ("startTime", str(start_time_ms)),
            ("endTime", str(end_time_ms)),
            ("limit", str(limit)),
        )
    )
    url = "%s?%s" % (FUNDING_RATE_URL, query)
    _validate_public_url(url)
    return url


def _day_start_ms(day: Any) -> int:
    parsed = _coerce_day(day)
    return (parsed - date(1970, 1, 1)).days * 86_400_000


def funding_path(
    destination: Path, symbol: str, start_day: Any, end_day: Any
) -> Path:
    symbol = _validate_symbol(symbol)
    first = _coerce_day(start_day)
    last = _coerce_day(end_day)
    if last < first:
        raise InputError("funding day range is reversed")
    filename = "%s-fundingRate-%s-%s.json" % (
        symbol,
        first.isoformat(),
        last.isoformat(),
    )
    return Path(destination) / "funding" / filename


def _expected_member(symbol: str, day: date) -> str:
    return "%s-1m-%s.csv" % (symbol, day.isoformat())


def _expected_agg_trades_member(symbol: str, day: date) -> str:
    return "%s-aggTrades-%s.csv" % (symbol, day.isoformat())


def _validate_member_byte_bound(max_member_bytes: int) -> int:
    if (
        isinstance(max_member_bytes, bool)
        or not isinstance(max_member_bytes, int)
        or max_member_bytes <= 0
    ):
        raise InputError("member byte bound must be a positive integer")
    return max_member_bytes


def _stream_verified_member(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    max_member_bytes: int,
) -> None:
    """Read one member in bounded chunks while accounting for every byte."""
    declared_bytes = member.file_size
    if declared_bytes <= 0 or declared_bytes > max_member_bytes:
        raise ArchiveError(
            "archive member size is outside bounds",
            measured_value=declared_bytes,
            bound=max_member_bytes,
        )
    streamed_bytes = 0
    try:
        with archive.open(member, "r") as handle:
            while True:
                remaining_with_guard = max_member_bytes - streamed_bytes + 1
                chunk = handle.read(min(READ_CHUNK_BYTES, remaining_with_guard))
                if not chunk:
                    break
                streamed_bytes += len(chunk)
                if streamed_bytes > max_member_bytes:
                    raise ArchiveError(
                        "streamed archive member exceeded byte bound",
                        measured_value=streamed_bytes,
                        bound=max_member_bytes,
                    )
    except zipfile.BadZipFile as error:
        raise ArchiveError(
            "archive CRC failed",
            measured_value=streamed_bytes,
            bound=declared_bytes,
        ) from error
    if streamed_bytes != declared_bytes:
        raise ArchiveError(
            "streamed archive member size differs from ZIP metadata",
            measured_value=streamed_bytes,
            bound=declared_bytes,
        )


def _verify_single_member_zip(
    path: Path,
    expected: str,
    label: str,
    max_member_bytes: int,
) -> None:
    max_member_bytes = _validate_member_byte_bound(max_member_bytes)
    try:
        with zipfile.ZipFile(path, "r") as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) != 1 or members[0].filename != expected:
                raise ArchiveError(
                    "archive member does not match expected %s CSV" % label
                )
            member = members[0]
            if member.flag_bits & 0x1:
                raise ArchiveError("encrypted archive member is not accepted")
            _stream_verified_member(archive, member, max_member_bytes)
    except ArchiveError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise ArchiveError("file is not an intact %s ZIP" % label) from error


def verify_kline_zip(
    path: Path,
    symbol: str,
    day: Any,
    *,
    max_member_bytes: int = MAX_MEMBER_BYTES,
) -> None:
    """Stream and verify the expected kline member without full extraction."""
    path = Path(path)
    symbol = _validate_symbol(symbol)
    day = _coerce_day(day)
    _verify_single_member_zip(
        path, _expected_member(symbol, day), "kline", max_member_bytes
    )


def verify_agg_trades_zip(
    path: Path,
    symbol: str,
    day: Any,
    *,
    max_member_bytes: int = MAX_MEMBER_BYTES,
) -> None:
    """Stream and verify the expected aggTrades member and stored CRC."""
    path = Path(path)
    symbol = _validate_symbol(symbol)
    day = _coerce_day(day)
    _verify_single_member_zip(
        path,
        _expected_agg_trades_member(symbol, day),
        "aggTrades",
        max_member_bytes,
    )


def quote_volume_from_zip(
    path: Path,
    symbol: str,
    day: Any,
    *,
    max_member_bytes: int = MAX_MEMBER_BYTES,
) -> Decimal:
    """Sum column 8 (quote-asset volume) with Decimal-only arithmetic."""
    symbol = _validate_symbol(symbol)
    day = _coerce_day(day)
    verify_kline_zip(
        path, symbol, day, max_member_bytes=max_member_bytes
    )
    member = _expected_member(symbol, day)
    total = Decimal(0)
    rows = 0
    try:
        with zipfile.ZipFile(path, "r") as archive:
            with archive.open(member, "r") as raw_handle:
                with io.TextIOWrapper(raw_handle, encoding="utf-8", newline="") as handle:
                    with localcontext() as context:
                        context.prec = 50
                        for row in csv.reader(handle):
                            rows += 1
                            if len(row) < 8:
                                raise ArchiveError("kline CSV row has fewer than 8 columns")
                            try:
                                value = Decimal(row[7])
                            except (InvalidOperation, ValueError) as error:
                                raise ArchiveError("kline quote volume is not decimal") from error
                            if not value.is_finite() or value < 0:
                                raise ArchiveError("kline quote volume is invalid")
                            total += value
    except ArchiveError:
        raise
    except (OSError, UnicodeError, csv.Error, zipfile.BadZipFile) as error:
        raise ArchiveError("kline CSV could not be read") from error
    if rows == 0:
        raise ArchiveError("kline CSV is empty")
    return total


def download_kline(
    symbol: str,
    day: Any,
    destination: Path,
    *,
    venue: str = VENUE_SPOT,
    opener: Optional[Callable[..., Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    max_member_bytes: int = MAX_MEMBER_BYTES,
) -> DownloadResult:
    """Download one archive atomically, or verify and skip an existing one."""
    symbol = _validate_symbol(symbol)
    day = _coerce_day(day)
    venue = _validate_venue(venue)
    max_member_bytes = _validate_member_byte_bound(max_member_bytes)
    target = kline_path(Path(destination), symbol, day, venue)
    if target.exists():
        verify_kline_zip(
            target, symbol, day, max_member_bytes=max_member_bytes
        )
        return DownloadResult(
            "SKIPPED", symbol, day, target, target.stat().st_size, venue
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % target.name, suffix=".part", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        response = _open_get(
            kline_url(symbol, day, venue), opener=opener, sleep_fn=sleep_fn
        )
        written = 0
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                while True:
                    chunk = response.read(READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_ARCHIVE_BYTES:
                        raise ArchiveError(
                            "archive response exceeded byte limit",
                            measured_value=written,
                            bound=MAX_ARCHIVE_BYTES,
                        )
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            response.close()
        if written == 0:
            raise ArchiveError(
                "archive response was empty", measured_value=written, bound=1
            )
        verify_kline_zip(
            temporary, symbol, day, max_member_bytes=max_member_bytes
        )
        if target.exists():
            verify_kline_zip(
                target, symbol, day, max_member_bytes=max_member_bytes
            )
            return DownloadResult(
                "SKIPPED", symbol, day, target, target.stat().st_size, venue
            )
        os.replace(temporary, target)
        return DownloadResult("DOWNLOADED", symbol, day, target, written, venue)
    except PublicReadError as error:
        if error.status == 404:
            raise ArchiveMissing("daily kline archive is absent", status=404) from error
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def download_agg_trades(
    symbol: str,
    day: Any,
    destination: Path,
    *,
    venue: str = VENUE_SPOT,
    opener: Optional[Callable[..., Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    max_member_bytes: int = MAX_MEMBER_BYTES,
) -> DownloadResult:
    """Download one daily aggTrades archive atomically and idempotently."""
    symbol = _validate_symbol(symbol)
    day = _coerce_day(day)
    venue = _validate_venue(venue)
    max_member_bytes = _validate_member_byte_bound(max_member_bytes)
    target = agg_trades_path(Path(destination), symbol, day, venue)
    if target.exists():
        verify_agg_trades_zip(
            target, symbol, day, max_member_bytes=max_member_bytes
        )
        return DownloadResult(
            "SKIPPED", symbol, day, target, target.stat().st_size, venue
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % target.name, suffix=".part", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        response = _open_get(
            agg_trades_url(symbol, day, venue), opener=opener, sleep_fn=sleep_fn
        )
        written = 0
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                while True:
                    chunk = response.read(READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_ARCHIVE_BYTES:
                        raise ArchiveError(
                            "archive response exceeded byte limit",
                            measured_value=written,
                            bound=MAX_ARCHIVE_BYTES,
                        )
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            response.close()
        if written == 0:
            raise ArchiveError(
                "archive response was empty", measured_value=written, bound=1
            )
        verify_agg_trades_zip(
            temporary, symbol, day, max_member_bytes=max_member_bytes
        )
        if target.exists():
            verify_agg_trades_zip(
                target, symbol, day, max_member_bytes=max_member_bytes
            )
            return DownloadResult(
                "SKIPPED", symbol, day, target, target.stat().st_size, venue
            )
        os.replace(temporary, target)
        return DownloadResult("DOWNLOADED", symbol, day, target, written, venue)
    except PublicReadError as error:
        if error.status == 404:
            raise ArchiveMissing(
                "daily aggTrades archive is absent", status=404
            ) from error
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parse_exchange_symbols(payload: Any, venue: str) -> Dict[str, Mapping[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise InputError("%s exchangeInfo has no symbols list" % venue)
    result: Dict[str, Mapping[str, Any]] = {}
    for entry in payload["symbols"]:
        if not isinstance(entry, dict):
            raise InputError("%s exchangeInfo symbol entry is not an object" % venue)
        symbol = entry.get("symbol")
        status = entry.get("status")
        quote_asset = entry.get("quoteAsset")
        if not isinstance(symbol, str):
            raise InputError("%s exchangeInfo symbol is not text" % venue)
        if not isinstance(status, str) or not isinstance(quote_asset, str):
            raise InputError("%s exchangeInfo fields are malformed for %s" % (venue, symbol))
        if status != "TRADING" or quote_asset != "USDT":
            continue
        if venue == "spot" and entry.get("isSpotTradingAllowed") is False:
            continue
        if venue == "futures" and entry.get("contractType") != "PERPETUAL":
            continue
        _validate_symbol(symbol)
        if symbol in result:
            raise InputError("%s exchangeInfo repeats %s" % (venue, symbol))
        result[symbol] = entry
    return result


def common_spot_perpetuals(spot_payload: Any, futures_payload: Any) -> List[str]:
    """Return sorted TRADING USDT spot symbols with an exact USD-M perpetual."""
    spot = _parse_exchange_symbols(spot_payload, "spot")
    futures = _parse_exchange_symbols(futures_payload, "futures")
    common = sorted(set(spot) & set(futures))
    if not common:
        raise InputError("exchangeInfo yielded no common spot/perpetual symbols")
    return common


def rank_cohort(
    candidates: Iterable[str], quote_volumes: Mapping[str, Decimal], cohort: int
) -> List[str]:
    """Choose the top exact quote-volume rows, with symbol as stable tie-break."""
    if (
        isinstance(cohort, bool)
        or not isinstance(cohort, int)
        or not 1 <= cohort <= MAX_COHORT
    ):
        raise InputError("cohort must be between 1 and %d" % MAX_COHORT)
    symbols = list(candidates)
    if not symbols:
        raise InputError("no daily archives supplied a known quote volume")
    if len(symbols) != len(set(symbols)):
        raise InputError("cohort candidates contain duplicates")
    for symbol in symbols:
        _validate_symbol(symbol)
        value = quote_volumes.get(symbol)
        if (
            not isinstance(value, Decimal)
            or not value.is_finite()
            or value < 0
        ):
            raise InputError("quote volume is unknown or invalid for %s" % symbol)
    symbols.sort(key=lambda symbol: (-quote_volumes[symbol], symbol))
    return symbols[:cohort]


def parse_cohort(
    spot_payload: Any,
    futures_payload: Any,
    quote_volumes: Mapping[str, Decimal],
    cohort: int,
) -> List[str]:
    """Parse both exchangeInfo fixtures and rank their common universe."""
    return rank_cohort(
        common_spot_perpetuals(spot_payload, futures_payload), quote_volumes, cohort
    )


def completed_utc_days(days: int, now: Optional[datetime] = None) -> List[date]:
    if (
        isinstance(days, bool)
        or not isinstance(days, int)
        or not 1 <= days <= MAX_DAYS
    ):
        raise InputError("days must be between 1 and %d" % MAX_DAYS)
    current = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise InputError("now must be a timezone-aware datetime")
    today = current.astimezone(timezone.utc).date()
    return [today - timedelta(days=offset) for offset in range(days, 0, -1)]


def _read_json_file(path: Path, label: str) -> Any:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise InputError("%s could not be read" % label) from error
    if len(raw) > MAX_MANIFEST_BYTES:
        raise InputError("%s exceeded byte limit" % label)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InputError("%s is not valid UTF-8 JSON" % label) from error


def _atomic_write_json(path: Path, payload: Any) -> int:
    path = Path(path)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise InputError("JSON output exceeded byte limit")
    try:
        if path.exists() and path.read_bytes() == encoded:
            return len(encoded)
    except OSError as error:
        raise InputError("existing JSON output could not be read") from error
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".%s." % path.name, suffix=".part", dir=path.parent
        )
    except OSError as error:
        raise InputError("JSON output could not be created") from error
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise InputError("JSON output could not be published") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return len(encoded)


def parse_cohort_manifest(payload: Any) -> Tuple[ManifestSymbol, ...]:
    """Validate the frozen V2 cohort list without discovering extra symbols."""
    if not isinstance(payload, list) or not payload:
        raise InputError("cohort manifest must be a nonempty JSON list")
    if len(payload) > MAX_COHORT:
        raise InputError("cohort manifest exceeds symbol limit")
    entries: List[ManifestSymbol] = []
    seen = set()
    for row in payload:
        if not isinstance(row, dict):
            raise InputError("cohort manifest row is not an object")
        symbol = _validate_symbol(row.get("symbol"))
        venue_class = row.get("venue_class")
        _venues_for_class(venue_class)
        if symbol in seen:
            raise InputError("cohort manifest repeats %s" % symbol)
        seen.add(symbol)
        gap_days = row.get("gap_days", [])
        if not isinstance(gap_days, list):
            raise InputError("cohort gap_days is not a list for %s" % symbol)
        parsed_gaps = [_coerce_day(gap_day) for gap_day in gap_days]
        if len(parsed_gaps) != len(set(parsed_gaps)):
            raise InputError("cohort gap_days repeats a day for %s" % symbol)
        days_available = row.get("days_available")
        if days_available is not None and (
            isinstance(days_available, bool)
            or not isinstance(days_available, int)
            or days_available < 0
        ):
            raise InputError("cohort days_available is invalid for %s" % symbol)
        if "listed_since" in row:
            _coerce_day(row["listed_since"])
        entries.append(ManifestSymbol(symbol, venue_class))
    return tuple(entries)


def load_cohort_manifest(path: Path) -> Tuple[ManifestSymbol, ...]:
    return parse_cohort_manifest(_read_json_file(path, "cohort manifest"))


def cohort_archive_requests(
    entries: Iterable[ManifestSymbol], days: Iterable[Any]
) -> Tuple[ArchiveRequest, ...]:
    parsed_days = tuple(_coerce_day(day) for day in days)
    if not parsed_days or len(parsed_days) != len(set(parsed_days)):
        raise InputError("cohort days must be nonempty and unique")
    requests: List[ArchiveRequest] = []
    seen_symbols = set()
    for entry in entries:
        if not isinstance(entry, ManifestSymbol):
            raise InputError("cohort entry is invalid")
        if entry.symbol in seen_symbols:
            raise InputError("cohort entries repeat %s" % entry.symbol)
        seen_symbols.add(entry.symbol)
        for venue in _venues_for_class(entry.venue_class):
            for day in parsed_days:
                requests.append(ArchiveRequest(entry.symbol, venue, day))
    if not requests:
        raise InputError("cohort manifest yielded no archive requests")
    return tuple(requests)


def _coerce_window_ms(value: Any) -> int:
    if isinstance(value, bool):
        raise InputError("window timestamp is invalid")
    if isinstance(value, int):
        parsed_ms = value
    elif isinstance(value, str) and value.isdigit():
        parsed_ms = int(value)
    elif isinstance(value, str):
        text_value = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(text_value)
        except ValueError as error:
            raise InputError("window timestamp is not ISO-8601 or epoch-ms") from error
        if parsed.tzinfo is None:
            raise InputError("window timestamp must include a timezone")
        utc_value = parsed.astimezone(timezone.utc)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        delta = utc_value - epoch
        parsed_ms = (
            delta.days * 86_400_000
            + delta.seconds * 1_000
            + delta.microseconds // 1_000
        )
    else:
        raise InputError("window timestamp is not ISO-8601 or epoch-ms")
    if parsed_ms < 0:
        raise InputError("window timestamp must be nonnegative")
    return parsed_ms


def _window_rows(payload: Any) -> List[Mapping[str, Any]]:
    rows: Any
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("windows")
    else:
        rows = None
    if not isinstance(rows, list) or not rows:
        raise InputError("windows manifest must contain a nonempty windows list")
    if len(rows) > MAX_MANIFEST_WINDOWS:
        raise InputError("windows manifest exceeds row limit")
    if any(not isinstance(row, dict) for row in rows):
        raise InputError("windows manifest row is not an object")
    return rows


def _window_bounds(row: Mapping[str, Any]) -> Tuple[int, int]:
    sources: List[Mapping[str, Any]] = [row]
    nested = row.get("window")
    if isinstance(nested, dict):
        sources.append(nested)
    aliases = (
        ("window_start_ms", "window_end_ms"),
        ("start_ms", "end_ms"),
        ("window_start_utc", "window_end_utc"),
        ("start_utc", "end_utc"),
        ("window_start", "window_end"),
        ("start", "end"),
    )
    found: List[Tuple[int, int]] = []
    for source in sources:
        for start_key, end_key in aliases:
            has_start = start_key in source
            has_end = end_key in source
            if has_start != has_end:
                raise InputError("window manifest has an incomplete time bound")
            if has_start:
                found.append(
                    (
                        _coerce_window_ms(source[start_key]),
                        _coerce_window_ms(source[end_key]),
                    )
                )
    if not found:
        raise InputError("window manifest row has no explicit time bounds")
    if any(bounds != found[0] for bounds in found[1:]):
        raise InputError("window manifest time bounds disagree")
    start_ms, end_ms = found[0]
    if end_ms <= start_ms:
        raise InputError("window manifest time range is empty or reversed")
    if end_ms - start_ms > MAX_WINDOW_DURATION_MS:
        raise InputError("window manifest range exceeds the 24-hour safety bound")
    return start_ms, end_ms


def parse_windows_manifest(payload: Any) -> Tuple[WindowSpec, ...]:
    specs: List[WindowSpec] = []
    for row in _window_rows(payload):
        symbol = _validate_symbol(row.get("symbol"))
        venue_class = row.get("venue_class")
        _venues_for_class(venue_class)
        start_ms, end_ms = _window_bounds(row)
        specs.append(WindowSpec(symbol, venue_class, start_ms, end_ms))
    return tuple(specs)


def _utc_day_from_ms(value: int) -> date:
    try:
        return date(1970, 1, 1) + timedelta(days=value // 86_400_000)
    except OverflowError as error:
        raise InputError("window timestamp is outside supported range") from error


def window_archive_requests(payload: Any) -> Tuple[ArchiveRequest, ...]:
    """Return stable, deduplicated daily aggTrades requests for explicit windows."""
    requests: List[ArchiveRequest] = []
    seen = set()
    for spec in parse_windows_manifest(payload):
        first_day = _utc_day_from_ms(spec.start_ms)
        last_day = _utc_day_from_ms(spec.end_ms - 1)
        day = first_day
        while day <= last_day:
            for venue in _venues_for_class(spec.venue_class):
                request = ArchiveRequest(spec.symbol, venue, day)
                if request not in seen:
                    seen.add(request)
                    requests.append(request)
            day += timedelta(days=1)
    return tuple(requests)


def load_window_archive_requests(path: Path) -> Tuple[ArchiveRequest, ...]:
    return window_archive_requests(_read_json_file(path, "windows manifest"))


def _update_gap_days(
    path: Path,
    label: str,
    row_requests: Sequence[Tuple[Mapping[str, Any], Tuple[ArchiveRequest, ...]]],
    attempted: Iterable[ArchiveRequest],
    gaps: Iterable[ArchiveRequest],
    payload: Any,
) -> None:
    attempted_set = set(attempted)
    gap_set = set(gaps)
    for row, requests in row_requests:
        relevant = {request for request in requests if request in attempted_set}
        by_day = {request.day for request in relevant}
        missing_days = {
            request.day for request in relevant if request in gap_set
        }
        existing_raw = row.get("gap_days", [])
        if not isinstance(existing_raw, list):
            raise InputError("%s gap_days is not a list" % label)
        existing = {_coerce_day(value) for value in existing_raw}
        old_gap_count = len(existing)
        existing.difference_update(by_day)
        existing.update(missing_days)
        row["gap_days"] = [day.isoformat() for day in sorted(existing)]
        if "days_available" in row:
            days_available = row["days_available"]
            if (
                isinstance(days_available, bool)
                or not isinstance(days_available, int)
                or days_available < 0
            ):
                raise InputError("%s days_available is invalid" % label)
            adjusted = days_available + old_gap_count - len(existing)
            if adjusted < 0:
                raise InputError("%s days_available would become negative" % label)
            row["days_available"] = adjusted
    _atomic_write_json(path, payload)


def update_cohort_gap_days(
    path: Path,
    attempted: Iterable[ArchiveRequest],
    gaps: Iterable[ArchiveRequest],
) -> None:
    payload = _read_json_file(path, "cohort manifest")
    entries = parse_cohort_manifest(payload)
    attempted_tuple = tuple(attempted)
    rows = []
    for row, entry in zip(payload, entries):
        requests = tuple(
            request
            for request in attempted_tuple
            if request.symbol == entry.symbol
        )
        rows.append((row, requests))
    _update_gap_days(
        Path(path), "cohort manifest", rows, attempted_tuple, gaps, payload
    )


def update_window_gap_days(
    path: Path,
    attempted: Iterable[ArchiveRequest],
    gaps: Iterable[ArchiveRequest],
) -> None:
    payload = _read_json_file(path, "windows manifest")
    raw_rows = _window_rows(payload)
    attempted_tuple = tuple(attempted)
    rows = []
    for row in raw_rows:
        requests = window_archive_requests([row])
        rows.append((row, requests))
    _update_gap_days(
        Path(path), "windows manifest", rows, attempted_tuple, gaps, payload
    )


def format_progress(result: DownloadResult) -> str:
    return "%s | %s | %s | venue=%s | bytes=%d | zip=ok" % (
        result.status,
        result.symbol,
        result.day.isoformat(),
        result.venue,
        result.size_bytes,
    )


def _row_value(value: Any) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").replace("|", "/")
    compact = " ".join(text.split())
    return compact if compact else "unknown"


def _identified_archive_failure(
    request: ArchiveRequest, error: TapePullError
) -> ArchiveFailure:
    measured_value = getattr(error, "measured_value", None)
    bound = getattr(error, "bound", None)
    if measured_value is None and isinstance(error, PublicReadError):
        measured_value = error.status
        if measured_value is not None:
            bound = 399
    return ArchiveFailure(
        request=request,
        error_type=type(error).__name__,
        reason=_row_value(error),
        measured_value=(
            "unknown" if measured_value is None else _row_value(measured_value)
        ),
        bound="not_applicable" if bound is None else _row_value(bound),
    )


def format_archive_failure(failure: ArchiveFailure) -> str:
    request = failure.request
    return (
        "ERROR | %s | symbol=%s | date=%s | venue=%s | measured=%s | "
        "bound=%s | reason=%s"
        % (
            failure.error_type,
            request.symbol,
            request.day.isoformat(),
            request.venue,
            failure.measured_value,
            failure.bound,
            failure.reason,
        )
    )


def _pull_archive_requests(
    requests: Sequence[ArchiveRequest],
    destination: Path,
    downloader: Callable[..., DownloadResult],
    *,
    opener: Optional[Callable[..., Any]],
    sleep_fn: Callable[[float], None],
    emit: Callable[[str], None],
    max_member_bytes: int = MAX_MEMBER_BYTES,
) -> Tuple[List[DownloadResult], List[ArchiveRequest], List[ArchiveFailure]]:
    max_member_bytes = _validate_member_byte_bound(max_member_bytes)
    results: List[DownloadResult] = []
    gaps: List[ArchiveRequest] = []
    failures: List[ArchiveFailure] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                downloader,
                request.symbol,
                request.day,
                destination,
                venue=request.venue,
                opener=opener,
                sleep_fn=sleep_fn,
                max_member_bytes=max_member_bytes,
            ): request
            for request in requests
        }
        for future in as_completed(futures):
            request = futures[future]
            try:
                result = future.result()
            except ArchiveMissing:
                gaps.append(request)
                emit(
                    "GAP | %s | %s | venue=%s | reason=archive_absent"
                    % (request.symbol, request.day.isoformat(), request.venue)
                )
                continue
            except TapePullError as error:
                failure = _identified_archive_failure(request, error)
                failures.append(failure)
                emit(format_archive_failure(failure))
                continue
            results.append(result)
            emit(format_progress(result))
    gaps.sort(key=lambda item: (item.day, item.symbol, item.venue))
    failures.sort(
        key=lambda item: (
            item.request.day,
            item.request.symbol,
            item.request.venue,
        )
    )
    return results, gaps, failures


def pull_cohort_file(
    cohort_file: Path,
    days: int,
    destination: Path,
    *,
    now: Optional[datetime] = None,
    opener: Optional[Callable[..., Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
    max_member_bytes: int = MAX_MEMBER_BYTES,
) -> ManifestPullSummary:
    """Pull only manifest-declared spot/perpetual kline archives."""
    entries = load_cohort_manifest(cohort_file)
    requested_days = completed_utc_days(days, now)
    requests = cohort_archive_requests(entries, requested_days)
    emit(
        "MODE | cohort_file | symbols=%d | requests=%d | workers=%d"
        % (len(entries), len(requests), MAX_WORKERS)
    )
    results, gaps, failures = _pull_archive_requests(
        requests,
        Path(destination),
        download_kline,
        opener=opener,
        sleep_fn=sleep_fn,
        emit=emit,
        max_member_bytes=max_member_bytes,
    )
    unavailable = tuple(gaps) + tuple(
        failure.request for failure in failures
    )
    update_cohort_gap_days(cohort_file, requests, unavailable)
    downloaded = sum(result.status == "DOWNLOADED" for result in results)
    skipped = sum(result.status == "SKIPPED" for result in results)
    emit(
        "DONE | mode=cohort_file | symbols=%d | requests=%d | "
        "downloaded=%d | skipped=%d | gaps=%d | failures=%d"
        % (
            len(entries),
            len(requests),
            downloaded,
            skipped,
            len(gaps),
            len(failures),
        )
    )
    return ManifestPullSummary(
        "cohort_file",
        tuple(entry.symbol for entry in entries),
        len(requests),
        downloaded,
        skipped,
        tuple(gaps),
        tuple(failures),
    )


def pull_windows_file(
    windows_file: Path,
    destination: Path,
    *,
    opener: Optional[Callable[..., Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
    max_member_bytes: int = MAX_MEMBER_BYTES,
) -> ManifestPullSummary:
    """Pull daily aggTrades archives only for days touched by explicit windows."""
    payload = _read_json_file(windows_file, "windows manifest")
    requests = window_archive_requests(payload)
    symbols = tuple(dict.fromkeys(request.symbol for request in requests))
    emit(
        "MODE | windows_file | symbols=%d | requests=%d | workers=%d"
        % (len(symbols), len(requests), MAX_WORKERS)
    )
    results, gaps, failures = _pull_archive_requests(
        requests,
        Path(destination),
        download_agg_trades,
        opener=opener,
        sleep_fn=sleep_fn,
        emit=emit,
        max_member_bytes=max_member_bytes,
    )
    unavailable = tuple(gaps) + tuple(
        failure.request for failure in failures
    )
    update_window_gap_days(windows_file, requests, unavailable)
    downloaded = sum(result.status == "DOWNLOADED" for result in results)
    skipped = sum(result.status == "SKIPPED" for result in results)
    emit(
        "DONE | mode=windows_file | symbols=%d | requests=%d | "
        "downloaded=%d | skipped=%d | gaps=%d | failures=%d"
        % (
            len(symbols),
            len(requests),
            downloaded,
            skipped,
            len(gaps),
            len(failures),
        )
    )
    return ManifestPullSummary(
        "windows_file",
        symbols,
        len(requests),
        downloaded,
        skipped,
        tuple(gaps),
        tuple(failures),
    )


def _normalize_funding_records(
    payload: Any, symbol: str, start_time_ms: int, end_time_ms: int
) -> List[Dict[str, Any]]:
    if not isinstance(payload, list):
        raise ArchiveError("funding history response is not a list")
    records: Dict[int, Dict[str, Any]] = {}
    for row in payload:
        if not isinstance(row, dict):
            raise ArchiveError("funding history row is not an object")
        if row.get("symbol") != symbol:
            raise ArchiveError("funding history row has the wrong symbol")
        funding_time = row.get("fundingTime")
        if isinstance(funding_time, bool) or not isinstance(funding_time, int):
            raise ArchiveError("funding history timestamp is invalid")
        if not start_time_ms <= funding_time <= end_time_ms:
            raise ArchiveError("funding history timestamp is outside request")
        raw_rate = row.get("fundingRate")
        if not isinstance(raw_rate, str):
            raise ArchiveError("funding rate is not decimal text")
        try:
            rate = Decimal(raw_rate)
        except (InvalidOperation, ValueError) as error:
            raise ArchiveError("funding rate is not decimal text") from error
        if not rate.is_finite() or abs(rate) > Decimal("0.1"):
            raise ArchiveError("funding rate is outside bounds")
        normalized: Dict[str, Any] = {
            "symbol": symbol,
            "fundingTime": funding_time,
            "fundingRate": raw_rate,
        }
        if "markPrice" in row:
            raw_mark = row["markPrice"]
            if not isinstance(raw_mark, str):
                raise ArchiveError("funding mark price is not decimal text")
            try:
                mark = Decimal(raw_mark)
            except (InvalidOperation, ValueError) as error:
                raise ArchiveError("funding mark price is not decimal text") from error
            if not mark.is_finite() or mark <= 0:
                raise ArchiveError("funding mark price is outside bounds")
            normalized["markPrice"] = raw_mark
        existing = records.get(funding_time)
        if existing is not None and existing != normalized:
            raise ArchiveError("funding history timestamp has conflicting rows")
        records[funding_time] = normalized
    return [records[key] for key in sorted(records)]


def fetch_funding_history(
    symbol: str,
    start_day: Any,
    end_day: Any,
    *,
    opener: Optional[Callable[..., Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> List[Dict[str, Any]]:
    symbol = _validate_symbol(symbol)
    first = _coerce_day(start_day)
    last = _coerce_day(end_day)
    if last < first:
        raise InputError("funding day range is reversed")
    start_time_ms = _day_start_ms(first)
    end_time_ms = _day_start_ms(last + timedelta(days=1)) - 1
    cursor = start_time_ms
    records: List[Dict[str, Any]] = []
    for page_number in range(MAX_FUNDING_PAGES):
        payload = fetch_json(
            funding_rate_url(symbol, cursor, end_time_ms),
            opener=opener,
            sleep_fn=sleep_fn,
        )
        page = _normalize_funding_records(
            payload, symbol, cursor, end_time_ms
        )
        records.extend(page)
        if not page or len(payload) < FUNDING_PAGE_LIMIT:
            break
        last_time = page[-1]["fundingTime"]
        if last_time >= end_time_ms:
            break
        cursor = last_time + 1
    else:
        raise PublicReadError("funding history exceeded pagination bound")
    return _normalize_funding_records(
        records, symbol, start_time_ms, end_time_ms
    )


def verify_funding_file(
    path: Path, symbol: str, start_day: Any, end_day: Any
) -> int:
    symbol = _validate_symbol(symbol)
    first = _coerce_day(start_day)
    last = _coerce_day(end_day)
    payload = _read_json_file(path, "funding history")
    records = _normalize_funding_records(
        payload,
        symbol,
        _day_start_ms(first),
        _day_start_ms(last + timedelta(days=1)) - 1,
    )
    if records != payload:
        raise ArchiveError("funding history file is not canonical")
    return len(records)


def download_funding_history(
    symbol: str,
    start_day: Any,
    end_day: Any,
    destination: Path,
    *,
    opener: Optional[Callable[..., Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> FundingResult:
    symbol = _validate_symbol(symbol)
    first = _coerce_day(start_day)
    last = _coerce_day(end_day)
    target = funding_path(destination, symbol, first, last)
    if target.exists():
        record_count = verify_funding_file(target, symbol, first, last)
        return FundingResult(
            "SKIPPED",
            symbol,
            first,
            last,
            target,
            target.stat().st_size,
            record_count,
        )
    records = fetch_funding_history(
        symbol, first, last, opener=opener, sleep_fn=sleep_fn
    )
    if target.exists():
        record_count = verify_funding_file(target, symbol, first, last)
        return FundingResult(
            "SKIPPED",
            symbol,
            first,
            last,
            target,
            target.stat().st_size,
            record_count,
        )
    size_bytes = _atomic_write_json(target, records)
    verify_funding_file(target, symbol, first, last)
    return FundingResult(
        "DOWNLOADED",
        symbol,
        first,
        last,
        target,
        size_bytes,
        len(records),
    )


def pull_funding(
    cohort_file: Path,
    days: int,
    destination: Path,
    *,
    now: Optional[datetime] = None,
    opener: Optional[Callable[..., Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
) -> FundingPullSummary:
    entries = load_cohort_manifest(cohort_file)
    requested_days = completed_utc_days(days, now)
    symbols = tuple(
        entry.symbol
        for entry in entries
        if VENUE_FUTURES in _venues_for_class(entry.venue_class)
    )
    emit(
        "MODE | funding | perp_symbols=%d | days=%d | workers=%d"
        % (len(symbols), len(requested_days), MAX_WORKERS)
    )
    results: List[FundingResult] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                download_funding_history,
                symbol,
                requested_days[0],
                requested_days[-1],
                Path(destination),
                opener=opener,
                sleep_fn=sleep_fn,
            ): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            emit(
                "%s | %s | %s..%s | records=%d | bytes=%d | json=ok"
                % (
                    result.status,
                    result.symbol,
                    result.start_day.isoformat(),
                    result.end_day.isoformat(),
                    result.records,
                    result.size_bytes,
                )
            )
    downloaded = sum(result.status == "DOWNLOADED" for result in results)
    skipped = sum(result.status == "SKIPPED" for result in results)
    record_count = sum(result.records for result in results)
    emit(
        "DONE | mode=funding | symbols=%d | downloaded=%d | skipped=%d | records=%d"
        % (len(symbols), downloaded, skipped, record_count)
    )
    return FundingPullSummary(symbols, downloaded, skipped, record_count)


def _ranking_archive(
    symbol: str,
    day: date,
    destination: Path,
    scratch: Path,
    opener: Optional[Callable[..., Any]],
    sleep_fn: Callable[[float], None],
    max_member_bytes: int,
) -> RankedArchive:
    existing = kline_path(destination, symbol, day)
    if existing.exists():
        volume = quote_volume_from_zip(
            existing, symbol, day, max_member_bytes=max_member_bytes
        )
        return RankedArchive(symbol, volume, existing, True)
    result = download_kline(
        symbol,
        day,
        scratch,
        opener=opener,
        sleep_fn=sleep_fn,
        max_member_bytes=max_member_bytes,
    )
    volume = quote_volume_from_zip(
        result.path, symbol, day, max_member_bytes=max_member_bytes
    )
    return RankedArchive(symbol, volume, result.path, False)


def pull_tapes(
    cohort: int,
    days: int,
    destination: Path,
    *,
    agg_trades: bool = False,
    now: Optional[datetime] = None,
    opener: Optional[Callable[..., Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
    max_member_bytes: int = MAX_MEMBER_BYTES,
) -> PullSummary:
    """Discover, rank, and materialize one historical tape cohort."""
    if agg_trades:
        raise InputError(
            "indiscriminate aggTrades pulls are disabled; use --windows-file"
        )
    max_member_bytes = _validate_member_byte_bound(max_member_bytes)
    requested_days = completed_utc_days(days, now)
    if (
        isinstance(cohort, bool)
        or not isinstance(cohort, int)
        or not 1 <= cohort <= MAX_COHORT
    ):
        raise InputError("cohort must be between 1 and %d" % MAX_COHORT)
    destination = Path(destination)
    spot_payload = fetch_json(
        SPOT_EXCHANGE_INFO_URL, opener=opener, sleep_fn=sleep_fn
    )
    futures_payload = fetch_json(
        FUTURES_EXCHANGE_INFO_URL, opener=opener, sleep_fn=sleep_fn
    )
    candidates = common_spot_perpetuals(spot_payload, futures_payload)
    ranking_day = requested_days[-1]
    emit(
        "COHORT | candidates=%d | ranking_day=%s | workers=%d"
        % (len(candidates), ranking_day.isoformat(), MAX_WORKERS)
    )

    ranked_archives: Dict[str, RankedArchive] = {}
    failures: List[ArchiveFailure] = []
    missing = 0
    completed = 0
    with tempfile.TemporaryDirectory(prefix="tape-cohort-") as scratch_name:
        scratch = Path(scratch_name)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    _ranking_archive,
                    symbol,
                    ranking_day,
                    destination,
                    scratch,
                    opener,
                    sleep_fn,
                    max_member_bytes,
                ): symbol
                for symbol in candidates
            }
            for future in as_completed(futures):
                symbol = futures[future]
                completed += 1
                try:
                    ranked_archives[symbol] = future.result()
                except ArchiveMissing:
                    missing += 1
                except TapePullError as error:
                    failure = _identified_archive_failure(
                        ArchiveRequest(symbol, VENUE_SPOT, ranking_day), error
                    )
                    failures.append(failure)
                    emit(format_archive_failure(failure))
                if completed % 25 == 0 or completed == len(candidates):
                    emit(
                        "RANK | checked=%d/%d | available=%d | missing=%d"
                        % (completed, len(candidates), len(ranked_archives), missing)
                    )

        selected = rank_cohort(
            ranked_archives,
            {symbol: row.quote_volume for symbol, row in ranked_archives.items()},
            cohort,
        )
        emit(
            "COHORT | ranked=%d | selected=%d | quote_volume_day=%s"
            % (len(ranked_archives), len(selected), ranking_day.isoformat())
        )

        results: List[DownloadResult] = []
        gaps: List[Tuple[str, date]] = []
        for symbol in selected:
            ranked = ranked_archives[symbol]
            target = kline_path(destination, symbol, ranking_day)
            if ranked.already_at_destination:
                result = DownloadResult(
                    "SKIPPED", symbol, ranking_day, target, target.stat().st_size
                )
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    verify_kline_zip(
                        target,
                        symbol,
                        ranking_day,
                        max_member_bytes=max_member_bytes,
                    )
                    result = DownloadResult(
                        "SKIPPED", symbol, ranking_day, target, target.stat().st_size
                    )
                else:
                    os.replace(ranked.path, target)
                    result = DownloadResult(
                        "DOWNLOADED", symbol, ranking_day, target, target.stat().st_size
                    )
            results.append(result)
            emit(format_progress(result))

        remaining = [
            (symbol, day)
            for symbol in selected
            for day in requested_days
            if day != ranking_day
        ]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    download_kline,
                    symbol,
                    day,
                    destination,
                    opener=opener,
                    sleep_fn=sleep_fn,
                    max_member_bytes=max_member_bytes,
                ): (symbol, day)
                for symbol, day in remaining
            }
            for future in as_completed(futures):
                symbol, day = futures[future]
                try:
                    result = future.result()
                except ArchiveMissing:
                    gaps.append((symbol, day))
                    emit(
                        "GAP | %s | %s | reason=archive_absent"
                        % (symbol, day.isoformat())
                    )
                    continue
                except TapePullError as error:
                    failure = _identified_archive_failure(
                        ArchiveRequest(symbol, VENUE_SPOT, day), error
                    )
                    failures.append(failure)
                    emit(format_archive_failure(failure))
                    continue
                results.append(result)
                emit(format_progress(result))

    downloaded = sum(result.status == "DOWNLOADED" for result in results)
    skipped = sum(result.status == "SKIPPED" for result in results)
    gaps.sort(key=lambda item: (item[1], item[0]))
    failures.sort(
        key=lambda item: (
            item.request.day,
            item.request.symbol,
            item.request.venue,
        )
    )
    emit(
        "DONE | symbols=%d | days=%d | downloaded=%d | skipped=%d | "
        "gaps=%d | failures=%d"
        % (
            len(selected),
            len(requested_days),
            downloaded,
            skipped,
            len(gaps),
            len(failures),
        )
    )
    return PullSummary(
        candidates=len(candidates),
        ranked=len(ranked_archives),
        selected=tuple(selected),
        downloaded=downloaded,
        skipped=skipped,
        gaps=tuple(gaps),
        days=tuple(requested_days),
        failures=tuple(failures),
    )


def _bounded_positive(value: str, name: str, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("%s must be an integer" % name) from error
    if not 1 <= parsed <= maximum:
        raise argparse.ArgumentTypeError(
            "%s must be between 1 and %d" % (name, maximum)
        )
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pull verified Binance Vision V2 historical tapes."
    )
    manifest_group = parser.add_mutually_exclusive_group()
    manifest_group.add_argument(
        "--cohort-file",
        type=Path,
        help="V2 cohort manifest; pull exactly its venue-aware symbols",
    )
    manifest_group.add_argument(
        "--windows-file",
        type=Path,
        help="V2 windows manifest; pull aggTrades only for its UTC days",
    )
    parser.add_argument(
        "--cohort",
        type=lambda value: _bounded_positive(value, "cohort", MAX_COHORT),
        default=DEFAULT_COHORT,
        help="top quote-volume spot/perpetual symbols (default: 100)",
    )
    parser.add_argument(
        "--days",
        type=lambda value: _bounded_positive(value, "days", MAX_DAYS),
        default=DEFAULT_DAYS,
        help="completed UTC days to pull (default: 3)",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_DEST,
        help="archive root (default: study/data/)",
    )
    parser.add_argument(
        "--agg-trades",
        action="store_true",
        help="deprecated; explicit --windows-file is required for aggTrades",
    )
    parser.add_argument(
        "--funding",
        action="store_true",
        help="with --cohort-file, pull public history for each perp symbol",
    )
    return parser


def selected_mode(arguments: argparse.Namespace) -> str:
    if arguments.funding:
        if arguments.cohort_file is None or arguments.windows_file is not None:
            raise InputError("--funding requires --cohort-file")
        if arguments.agg_trades:
            raise InputError("--funding cannot be combined with --agg-trades")
        return "funding"
    if arguments.windows_file is not None:
        return "windows_file"
    if arguments.cohort_file is not None:
        if arguments.agg_trades:
            raise InputError("use --windows-file for manifest-scoped aggTrades")
        return "cohort_file"
    return "ranked"


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = build_parser().parse_args(argv)
    summary: Any = None
    try:
        mode = selected_mode(arguments)
        if mode == "cohort_file":
            summary = pull_cohort_file(
                arguments.cohort_file, arguments.days, arguments.dest
            )
        elif mode == "windows_file":
            summary = pull_windows_file(arguments.windows_file, arguments.dest)
        elif mode == "funding":
            summary = pull_funding(
                arguments.cohort_file, arguments.days, arguments.dest
            )
        else:
            summary = pull_tapes(
                arguments.cohort,
                arguments.days,
                arguments.dest,
                agg_trades=arguments.agg_trades,
            )
    except TapePullError as error:
        print(
            "FATAL | %s | reason=%s" % (type(error).__name__, _row_value(error)),
            file=sys.stderr,
        )
        return 1
    if isinstance(summary, (ManifestPullSummary, PullSummary)) and summary.failures:
        return COMPLETED_WITH_FAILURES_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
