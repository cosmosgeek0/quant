#!/usr/bin/env python3
"""Pull verified Binance Vision spot 1m tapes for a spot/perpetual cohort.

Only unsigned public GETs are possible here.  The allowed metadata reads are
the spot and USD-M exchangeInfo documents; archive reads are restricted to
Binance Vision daily spot kline or aggTrades paths.  This module never imports
an execution or money-mutation module.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
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
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


SPOT_EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"
FUTURES_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
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
MAX_MEMBER_BYTES = 256_000_000
READ_CHUNK_BYTES = 128 * 1024
SYMBOL_PATTERN = re.compile(r"[A-Z0-9]{5,30}")
ARCHIVE_PATH_PATTERN = re.compile(
    r"/data/spot/daily/(?:"
    r"klines/(?P<kline_symbol>[A-Z0-9]{5,30})/1m/"
    r"(?P=kline_symbol)-1m-\d{4}-\d{2}-\d{2}\.zip|"
    r"aggTrades/(?P<trade_symbol>[A-Z0-9]{5,30})/"
    r"(?P=trade_symbol)-aggTrades-\d{4}-\d{2}-\d{2}\.zip)"
)
USER_AGENT = "quant-study-tape-pull/1"


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


@dataclass(frozen=True)
class DownloadResult:
    status: str
    symbol: str
    day: date
    path: Path
    size_bytes: int


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


def _validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise InputError("public URL is outside the GET allowlist")
    if parsed.netloc == "api.binance.com" and parsed.path == "/api/v3/exchangeInfo":
        return
    if (
        parsed.netloc == "fapi.binance.com"
        and parsed.path == "/fapi/v1/exchangeInfo"
    ):
        return
    if (
        parsed.netloc == "data.binance.vision"
        and ARCHIVE_PATH_PATTERN.fullmatch(parsed.path) is not None
    ):
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
    if not isinstance(symbol, str) or SYMBOL_PATTERN.fullmatch(symbol) is None:
        raise InputError("symbol must contain 5-30 uppercase letters or digits")
    return symbol


def kline_url(symbol: str, day: Any) -> str:
    symbol = _validate_symbol(symbol)
    day = _coerce_day(day)
    filename = "%s-1m-%s.zip" % (symbol, day.isoformat())
    return "%s/data/spot/daily/klines/%s/1m/%s" % (
        VISION_BASE_URL,
        symbol,
        filename,
    )


def kline_path(destination: Path, symbol: str, day: Any) -> Path:
    symbol = _validate_symbol(symbol)
    day = _coerce_day(day)
    filename = "%s-1m-%s.zip" % (symbol, day.isoformat())
    return Path(destination) / "spot" / "daily" / "klines" / symbol / "1m" / filename


def agg_trades_url(symbol: str, day: Any) -> str:
    symbol = _validate_symbol(symbol)
    day = _coerce_day(day)
    filename = "%s-aggTrades-%s.zip" % (symbol, day.isoformat())
    return "%s/data/spot/daily/aggTrades/%s/%s" % (
        VISION_BASE_URL,
        symbol,
        filename,
    )


def agg_trades_path(destination: Path, symbol: str, day: Any) -> Path:
    symbol = _validate_symbol(symbol)
    day = _coerce_day(day)
    filename = "%s-aggTrades-%s.zip" % (symbol, day.isoformat())
    return Path(destination) / "spot" / "daily" / "aggTrades" / symbol / filename


def _expected_member(symbol: str, day: date) -> str:
    return "%s-1m-%s.csv" % (symbol, day.isoformat())


def _expected_agg_trades_member(symbol: str, day: date) -> str:
    return "%s-aggTrades-%s.csv" % (symbol, day.isoformat())


def verify_kline_zip(path: Path, symbol: str, day: Any) -> None:
    """Verify ZIP structure, expected member name, size, and every stored CRC."""
    path = Path(path)
    symbol = _validate_symbol(symbol)
    day = _coerce_day(day)
    expected = _expected_member(symbol, day)
    try:
        with zipfile.ZipFile(path, "r") as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) != 1 or members[0].filename != expected:
                raise ArchiveError("archive member does not match expected kline CSV")
            member = members[0]
            if member.flag_bits & 0x1:
                raise ArchiveError("encrypted archive member is not accepted")
            if member.file_size <= 0 or member.file_size > MAX_MEMBER_BYTES:
                raise ArchiveError("archive member size is outside bounds")
            bad_member = archive.testzip()
            if bad_member is not None:
                raise ArchiveError("archive CRC failed")
    except ArchiveError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise ArchiveError("file is not an intact kline ZIP") from error


def verify_agg_trades_zip(path: Path, symbol: str, day: Any) -> None:
    """Verify aggTrades member identity, size, encryption state, and CRC."""
    path = Path(path)
    symbol = _validate_symbol(symbol)
    day = _coerce_day(day)
    expected = _expected_agg_trades_member(symbol, day)
    try:
        with zipfile.ZipFile(path, "r") as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) != 1 or members[0].filename != expected:
                raise ArchiveError(
                    "archive member does not match expected aggTrades CSV"
                )
            member = members[0]
            if member.flag_bits & 0x1:
                raise ArchiveError("encrypted archive member is not accepted")
            if member.file_size <= 0 or member.file_size > MAX_MEMBER_BYTES:
                raise ArchiveError("archive member size is outside bounds")
            bad_member = archive.testzip()
            if bad_member is not None:
                raise ArchiveError("archive CRC failed")
    except ArchiveError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise ArchiveError("file is not an intact aggTrades ZIP") from error


def quote_volume_from_zip(path: Path, symbol: str, day: Any) -> Decimal:
    """Sum column 8 (quote-asset volume) with Decimal-only arithmetic."""
    symbol = _validate_symbol(symbol)
    day = _coerce_day(day)
    verify_kline_zip(path, symbol, day)
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
    opener: Optional[Callable[..., Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> DownloadResult:
    """Download one archive atomically, or verify and skip an existing one."""
    symbol = _validate_symbol(symbol)
    day = _coerce_day(day)
    target = kline_path(Path(destination), symbol, day)
    if target.exists():
        verify_kline_zip(target, symbol, day)
        return DownloadResult("SKIPPED", symbol, day, target, target.stat().st_size)

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % target.name, suffix=".part", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        response = _open_get(kline_url(symbol, day), opener=opener, sleep_fn=sleep_fn)
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
                        raise ArchiveError("archive response exceeded byte limit")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            response.close()
        if written == 0:
            raise ArchiveError("archive response was empty")
        verify_kline_zip(temporary, symbol, day)
        if target.exists():
            verify_kline_zip(target, symbol, day)
            return DownloadResult("SKIPPED", symbol, day, target, target.stat().st_size)
        os.replace(temporary, target)
        return DownloadResult("DOWNLOADED", symbol, day, target, written)
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
    opener: Optional[Callable[..., Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> DownloadResult:
    """Download one daily aggTrades archive atomically and idempotently."""
    symbol = _validate_symbol(symbol)
    day = _coerce_day(day)
    target = agg_trades_path(Path(destination), symbol, day)
    if target.exists():
        verify_agg_trades_zip(target, symbol, day)
        return DownloadResult("SKIPPED", symbol, day, target, target.stat().st_size)

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % target.name, suffix=".part", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        response = _open_get(
            agg_trades_url(symbol, day), opener=opener, sleep_fn=sleep_fn
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
                        raise ArchiveError("archive response exceeded byte limit")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            response.close()
        if written == 0:
            raise ArchiveError("archive response was empty")
        verify_agg_trades_zip(temporary, symbol, day)
        if target.exists():
            verify_agg_trades_zip(target, symbol, day)
            return DownloadResult(
                "SKIPPED", symbol, day, target, target.stat().st_size
            )
        os.replace(temporary, target)
        return DownloadResult("DOWNLOADED", symbol, day, target, written)
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
        if SYMBOL_PATTERN.fullmatch(symbol) is None:
            continue
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


def format_progress(result: DownloadResult) -> str:
    return "%s | %s | %s | bytes=%d | zip=ok" % (
        result.status,
        result.symbol,
        result.day.isoformat(),
        result.size_bytes,
    )


def _ranking_archive(
    symbol: str,
    day: date,
    destination: Path,
    scratch: Path,
    opener: Optional[Callable[..., Any]],
    sleep_fn: Callable[[float], None],
) -> RankedArchive:
    existing = kline_path(destination, symbol, day)
    if existing.exists():
        volume = quote_volume_from_zip(existing, symbol, day)
        return RankedArchive(symbol, volume, existing, True)
    result = download_kline(
        symbol, day, scratch, opener=opener, sleep_fn=sleep_fn
    )
    volume = quote_volume_from_zip(result.path, symbol, day)
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
) -> PullSummary:
    """Discover, rank, and materialize one historical tape cohort."""
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
                    verify_kline_zip(target, symbol, ranking_day)
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
                results.append(result)
                emit(format_progress(result))

        if agg_trades:
            trade_archives = [
                (symbol, day)
                for symbol in selected
                for day in requested_days
            ]
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {
                    executor.submit(
                        download_agg_trades,
                        symbol,
                        day,
                        destination,
                        opener=opener,
                        sleep_fn=sleep_fn,
                    ): (symbol, day)
                    for symbol, day in trade_archives
            }
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                emit(format_progress(result))

    downloaded = sum(result.status == "DOWNLOADED" for result in results)
    skipped = sum(result.status == "SKIPPED" for result in results)
    gaps.sort(key=lambda item: (item[1], item[0]))
    emit(
        "DONE | symbols=%d | days=%d | downloaded=%d | skipped=%d | gaps=%d"
        % (len(selected), len(requested_days), downloaded, skipped, len(gaps))
    )
    return PullSummary(
        candidates=len(candidates),
        ranked=len(ranked_archives),
        selected=tuple(selected),
        downloaded=downloaded,
        skipped=skipped,
        gaps=tuple(gaps),
        days=tuple(requested_days),
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
        description="Pull verified Binance Vision spot 1m daily tapes."
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
        help="also pull verified daily spot aggTrades archives",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        pull_tapes(
            arguments.cohort,
            arguments.days,
            arguments.dest,
            agg_trades=arguments.agg_trades,
        )
    except TapePullError as error:
        print("ERROR | %s | %s" % (type(error).__name__, error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
