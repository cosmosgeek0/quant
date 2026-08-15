#!/usr/bin/env python3
"""Build the Volume Dataset V2 full-universe cohort manifest.

The only network reads in this module are unsigned GETs of the Binance Spot
and USD-M ``exchangeInfo`` documents.  Market-data availability is measured
from exact daily 1m kline paths already present below ``study/data``; this
module does not download archives or infer availability from listing status.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


SPOT_EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"
FUTURES_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
ALLOWED_PUBLIC_URLS = frozenset(
    {SPOT_EXCHANGE_INFO_URL, FUTURES_EXCHANGE_INFO_URL}
)
STUDY_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = STUDY_DIR / "data"
DEFAULT_OUTPUT = STUDY_DIR / "out" / "v2" / "cohort_manifest.json"
DEFAULT_DAYS = 30
MAX_DAYS = 3_660
HTTP_TIMEOUT_SECONDS = 30.0
MAX_METADATA_BYTES = 24_000_000
USER_AGENT = "quant-study-cohort-v2/1"
VENUE_CLASSES = ("spot_only", "futures_only", "both")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class CohortError(Exception):
    """Expected, factual cohort-manifest failure."""


class InputError(CohortError):
    """An argument or exchangeInfo document is not usable."""


class PublicReadError(CohortError):
    """An allowlisted unsigned public GET failed."""


@dataclass(frozen=True)
class VenueSymbol:
    symbol: str
    listed_since: Optional[date]


@dataclass(frozen=True)
class CohortMember:
    symbol: str
    venue_class: str
    listed_since: Optional[date]


def _validate_public_url(url: str) -> None:
    if not isinstance(url, str) or url not in ALLOWED_PUBLIC_URLS:
        raise InputError("public URL is outside the exchangeInfo GET allowlist")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise InputError("public URL is outside the exchangeInfo GET allowlist")


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


def fetch_json(
    url: str,
    *,
    opener: Optional[Callable[..., Any]] = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> Any:
    """Read one exact allowlisted endpoint with an unsigned GET."""
    _validate_public_url(url)
    open_call = opener if opener is not None else _PUBLIC_OPENER.open
    request = Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    try:
        response = open_call(request, timeout=timeout)
    except HTTPError as error:
        if getattr(error, "fp", None) is not None:
            error.close()
        raise PublicReadError(
            "exchangeInfo GET returned HTTP %d" % error.code
        ) from error
    except (TimeoutError, URLError, OSError) as error:
        raise PublicReadError("exchangeInfo GET transport failed") from error

    try:
        final_url = response.geturl() if hasattr(response, "geturl") else url
        _validate_public_url(final_url)
        status = getattr(response, "status", None)
        if status is None and hasattr(response, "getcode"):
            status = response.getcode()
        if isinstance(status, int) and status >= 400:
            raise PublicReadError(
                "exchangeInfo GET returned HTTP %d" % status
            )
        raw = response.read(MAX_METADATA_BYTES + 1)
    except CohortError:
        raise
    except (TimeoutError, URLError, OSError) as error:
        raise PublicReadError("exchangeInfo response read failed") from error
    finally:
        response.close()

    if len(raw) > MAX_METADATA_BYTES:
        raise PublicReadError("exchangeInfo response exceeded byte limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicReadError("exchangeInfo response is not valid JSON") from error


def _listed_since(entry: Mapping[str, Any], venue: str, symbol: str) -> Optional[date]:
    raw = entry.get("onboardDate")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise InputError(
            "%s exchangeInfo onboardDate is malformed for %s" % (venue, symbol)
        )
    try:
        seconds, milliseconds = divmod(raw, 1_000)
        return (EPOCH + timedelta(seconds=seconds, milliseconds=milliseconds)).date()
    except OverflowError as error:
        raise InputError(
            "%s exchangeInfo onboardDate is out of range for %s" % (venue, symbol)
        ) from error


def _validate_symbol(symbol: Any, source: str = "symbol") -> str:
    """Accept Binance's safe uppercase alphanumeric symbol alphabet.

    Spot currently includes non-ASCII asset names, so an ASCII-only regular
    expression would silently truncate the full universe.  ``isalnum`` keeps
    those names while excluding separators, punctuation, and control bytes.
    """
    if (
        not isinstance(symbol, str)
        or not 5 <= len(symbol) <= 100
        or not symbol.endswith("USDT")
        or not symbol.isalnum()
        or symbol.upper() != symbol
    ):
        raise InputError("%s is not a safe uppercase USDT symbol" % source)
    return symbol


def parse_venue_symbols(payload: Any, venue: str) -> Dict[str, VenueSymbol]:
    """Return every active USDT Spot or USD-M perpetual symbol in a fixture."""
    if venue not in {"spot", "futures"}:
        raise InputError("venue must be spot or futures")
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise InputError("%s exchangeInfo has no symbols list" % venue)

    parsed: Dict[str, VenueSymbol] = {}
    for entry in payload["symbols"]:
        if not isinstance(entry, dict):
            raise InputError("%s exchangeInfo symbol entry is not an object" % venue)
        symbol = entry.get("symbol")
        status = entry.get("status")
        quote_asset = entry.get("quoteAsset")
        if not isinstance(symbol, str):
            raise InputError("%s exchangeInfo symbol is not text" % venue)
        if not isinstance(status, str) or not isinstance(quote_asset, str):
            raise InputError(
                "%s exchangeInfo fields are malformed for %s" % (venue, symbol)
            )
        if status != "TRADING" or quote_asset != "USDT":
            continue
        if venue == "spot":
            allowed = entry.get("isSpotTradingAllowed")
            if allowed is not None and not isinstance(allowed, bool):
                raise InputError(
                    "spot exchangeInfo isSpotTradingAllowed is malformed for %s"
                    % symbol
                )
            if allowed is False:
                continue
        else:
            contract_type = entry.get("contractType")
            if not isinstance(contract_type, str):
                raise InputError(
                    "futures exchangeInfo contractType is malformed for %s" % symbol
                )
            if contract_type != "PERPETUAL":
                continue
        _validate_symbol(symbol, "%s exchangeInfo symbol" % venue)
        if symbol in parsed:
            raise InputError("%s exchangeInfo repeats %s" % (venue, symbol))
        parsed[symbol] = VenueSymbol(
            symbol=symbol,
            listed_since=_listed_since(entry, venue, symbol),
        )
    return parsed


def classify_universe(
    spot_payload: Any, futures_payload: Any
) -> Tuple[CohortMember, ...]:
    """Classify the complete active USDT union without volume selection."""
    spot = parse_venue_symbols(spot_payload, "spot")
    futures = parse_venue_symbols(futures_payload, "futures")
    symbols = sorted(set(spot) | set(futures))
    if not symbols:
        raise InputError("exchangeInfo yielded no active USDT symbols")

    members: List[CohortMember] = []
    for symbol in symbols:
        in_spot = symbol in spot
        in_futures = symbol in futures
        if in_spot and in_futures:
            venue_class = "both"
        elif in_spot:
            venue_class = "spot_only"
        else:
            venue_class = "futures_only"
        known_dates = [
            row.listed_since
            for row in (spot.get(symbol), futures.get(symbol))
            if row is not None and row.listed_since is not None
        ]
        members.append(
            CohortMember(
                symbol=symbol,
                venue_class=venue_class,
                listed_since=min(known_dates) if known_dates else None,
            )
        )
    return tuple(members)


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


def coverage_days(
    days: int = DEFAULT_DAYS,
    *,
    end_day: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> Tuple[date, ...]:
    """Return an inclusive ascending window ending on a completed UTC day."""
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
        raise InputError("days must be between 1 and %d" % MAX_DAYS)
    if end_day is None:
        current = now if now is not None else datetime.now(timezone.utc)
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise InputError("now must be a timezone-aware datetime")
        last = current.astimezone(timezone.utc).date() - timedelta(days=1)
    else:
        last = _coerce_day(end_day)
    first = last - timedelta(days=days - 1)
    return tuple(first + timedelta(days=offset) for offset in range(days))


def kline_path(data_dir: Path, venue: str, symbol: str, day: Any) -> Path:
    """Return the one exact daily archive path used for an availability probe."""
    if venue not in {"spot", "futures"}:
        raise InputError("venue must be spot or futures")
    symbol = _validate_symbol(symbol)
    parsed_day = _coerce_day(day)
    filename = "%s-1m-%s.zip" % (symbol, parsed_day.isoformat())
    if venue == "spot":
        relative = Path("spot") / "daily" / "klines" / symbol / "1m" / filename
    else:
        relative = (
            Path("futures")
            / "um"
            / "daily"
            / "klines"
            / symbol
            / "1m"
            / filename
        )
    return Path(data_dir) / relative


def _required_venues(venue_class: str) -> Tuple[str, ...]:
    if venue_class == "spot_only":
        return ("spot",)
    if venue_class == "futures_only":
        return ("futures",)
    if venue_class == "both":
        return ("spot", "futures")
    raise InputError("unknown venue class")


def build_manifest(
    spot_payload: Any,
    futures_payload: Any,
    data_dir: Path,
    requested_days: Iterable[Any],
) -> List[Dict[str, Any]]:
    """Build deterministic rows from metadata and local archive presence.

    A day is available only when every venue implied by the row's class has
    its exact regular-file archive.  Thus a ``both`` row with only one venue's
    archive records that day as a gap rather than overstating coverage.
    """
    days = tuple(sorted(_coerce_day(day) for day in requested_days))
    if not days:
        raise InputError("requested_days must not be empty")
    if len(days) != len(set(days)):
        raise InputError("requested_days contains duplicates")

    root = Path(data_dir)
    rows: List[Dict[str, Any]] = []
    for member in classify_universe(spot_payload, futures_payload):
        required = _required_venues(member.venue_class)
        gaps = [
            day.isoformat()
            for day in days
            if not all(
                kline_path(root, venue, member.symbol, day).is_file()
                for venue in required
            )
        ]
        row: Dict[str, Any] = {
            "symbol": member.symbol,
            "venue_class": member.venue_class,
        }
        if member.listed_since is not None:
            row["listed_since"] = member.listed_since.isoformat()
        row["days_available"] = len(days) - len(gaps)
        row["gap_days"] = gaps
        rows.append(row)
    return rows


def write_manifest(rows: Sequence[Mapping[str, Any]], output: Path) -> None:
    """Publish the JSON list atomically."""
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % target.name, suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(list(rows), handle, ensure_ascii=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def generate_manifest(
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    output: Path = DEFAULT_OUTPUT,
    days: int = DEFAULT_DAYS,
    end_day: Optional[Any] = None,
    fetcher: Callable[[str], Any] = fetch_json,
    emit: Callable[[str], None] = print,
) -> List[Dict[str, Any]]:
    """Fetch both metadata documents, probe local data, and emit one manifest."""
    spot_payload = fetcher(SPOT_EXCHANGE_INFO_URL)
    futures_payload = fetcher(FUTURES_EXCHANGE_INFO_URL)
    requested = coverage_days(days, end_day=end_day)
    rows = build_manifest(spot_payload, futures_payload, data_dir, requested)
    write_manifest(rows, output)

    counts = {venue_class: 0 for venue_class in VENUE_CLASSES}
    for row in rows:
        counts[row["venue_class"]] += 1
    emit(
        "COHORT | total=%d | spot_only=%d | futures_only=%d | both=%d"
        % (
            len(rows),
            counts["spot_only"],
            counts["futures_only"],
            counts["both"],
        )
    )
    emit(
        "COVERAGE | first=%s | last=%s | requested_days=%d | output=%s"
        % (
            requested[0].isoformat(),
            requested[-1].isoformat(),
            len(requested),
            Path(output),
        )
    )
    return rows


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build the full active-USDT Spot/USD-M cohort and local-data gap manifest."
        )
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="local study data root to probe (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="manifest JSON path (default: %(default)s)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help="number of calendar days in the coverage window (default: %(default)s)",
    )
    parser.add_argument(
        "--end-date",
        dest="end_day",
        help="last covered UTC day, YYYY-MM-DD (default: yesterday UTC)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        generate_manifest(
            data_dir=args.data_dir,
            output=args.output,
            days=args.days,
            end_day=args.end_day,
        )
    except (CohortError, OSError) as error:
        print(
            "ERROR | cohort | %s | %s" % (type(error).__name__, error),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
