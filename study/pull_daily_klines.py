#!/usr/bin/env python3
"""Pull verified Binance Vision 1d archives and measure 365-day lows."""
from __future__ import annotations

import argparse, csv, io, json, os, re, sys, tempfile, time, zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed; from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext; from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError; from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]; BASE = "https://data.binance.vision"
MANIFEST = ROOT / "study/out/v2/cohort_manifest.json"; SOURCE_DATA = Path("/Users/cosmosgeek/Desktop/quant/study/data")
DEST = Path(__file__).resolve().parent / "data-daily"; OUTPUT = ROOT / "lane-logs/AB5/long-low-positions.json"
MONTHS = tuple("2025-%02d" % n for n in range(8, 13)) + tuple("2026-%02d" % n for n in range(1, 8))
DAYS = tuple("2026-08-%02d" % n for n in range(1, 15)); START, END = date(2025, 8, 15), date(2026, 8, 14)
MAX_BYTES, WORKERS, CHUNK = 8_000_000, 4, 128 * 1024

class PullError(Exception): pass
class Missing(PullError): pass
class Refused(PullError):
    def __init__(self, measured: int) -> None:
        super().__init__("archive exceeds byte bound"); self.measured = measured

class Job(NamedTuple):
    symbol: str; venue: str; cadence: str; period: str
    def filename(self) -> str: return f"{self.symbol}-1d-{self.period}.zip"
    def member(self) -> str: return self.filename()[:-4] + ".csv"
    def prefix(self) -> str: return "spot" if self.venue == "spot" else "futures/um"
    def url(self) -> str:
        symbol = quote(self.symbol, safe="")
        return f"{BASE}/data/{self.prefix()}/{self.cadence}/klines/{symbol}/1d/{symbol}-1d-{self.period}.zip"
    def path(self, destination: Path) -> Path: return destination / self.prefix() / self.cadence / "klines" / self.symbol / "1d" / self.filename()

class Result(NamedTuple):
    status: str; job: Job; path: Path; size: int = 0; reason: str = ""

def symbol(raw: Any) -> str:
    if (not isinstance(raw, str) or not 5 <= len(raw) <= 100 or
            not raw.endswith("USDT") or not raw.isalnum() or raw.upper() != raw):
        raise PullError("symbol is not a safe uppercase USDT symbol")
    return raw

def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "data.binance.vision" or
            parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.port):
        raise PullError("public URL is outside the GET allowlist")
    decoded = unquote(parsed.path)
    if quote(decoded, safe="/-.") != parsed.path: raise PullError("public URL path is not canonical")
    match = re.fullmatch(
        r"/data/(spot|futures/um)/(monthly|daily)/klines/([^/]+)/1d/([^/]+)", decoded)
    if match is None: raise PullError("public URL path is outside the archive allowlist")
    _, cadence, name, filename = match.groups(); symbol(name)
    period = r"\d{4}-\d{2}" if cadence == "monthly" else r"\d{4}-\d{2}-\d{2}"
    if not re.fullmatch(re.escape(name) + r"-1d-" + period + r"\.zip", filename):
        raise PullError("public URL filename is outside the archive allowlist")

class Redirects(HTTPRedirectHandler):
    def redirect_request(self, request: Request, fp: Any, code: int, message: str,
                         headers: Any, new_url: str) -> Optional[Request]:
        validate_url(new_url); return super().redirect_request(request, fp, code, message, headers, new_url)

OPENER = build_opener(Redirects())

def open_get(job: Job) -> Any:
    url = job.url(); validate_url(url)
    request = Request(url, headers={"User-Agent": "quant-daily-kline-pull/1"}, method="GET")
    for attempt in range(5):
        try:
            response = OPENER.open(request, timeout=30); validate_url(response.geturl()); return response
        except HTTPError as error:
            try:
                if error.code == 404: raise Missing("archive is absent") from error
                if error.code != 429 or attempt == 4: raise PullError(f"public GET returned HTTP {error.code}") from error
                retry = error.headers.get("Retry-After") if error.headers else None
                time.sleep(min(int(retry) if retry and retry.isdigit() else 2 ** attempt, 60))
            finally: error.close()
        except (TimeoutError, URLError, OSError) as error: raise PullError("public GET transport failed") from error
    raise PullError("public GET retry limit reached")

def verify(job: Job, path: Path) -> int:
    size = path.stat().st_size
    if size > MAX_BYTES: raise Refused(size)
    if size <= 0: raise PullError("archive is empty")
    try:
        with zipfile.ZipFile(path) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) != 1 or members[0].filename != job.member(): raise PullError("archive member differs from expected CSV")
            if members[0].flag_bits & 1: raise PullError("encrypted archive is not accepted")
            with archive.open(members[0]) as handle:
                while handle.read(CHUNK): pass
    except PullError: raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error: raise PullError("file is not an intact ZIP") from error
    return size

def download(job: Job, destination: Path) -> Result:
    target = job.path(destination)
    try:
        if target.exists(): return Result("SKIPPED", job, target, verify(job, target))
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".part", dir=target.parent)
        temporary = Path(name)
        try:
            response = open_get(job); declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_BYTES: raise Refused(int(declared))
            written = 0
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = -1
                    while True:
                        chunk = response.read(CHUNK)
                        if not chunk: break
                        written += len(chunk)
                        if written > MAX_BYTES: raise Refused(written)
                        handle.write(chunk)
                    handle.flush(); os.fsync(handle.fileno())
            finally: response.close()
            verify(job, temporary); os.replace(temporary, target)
            return Result("DOWNLOADED", job, target, written)
        finally:
            if descriptor >= 0: os.close(descriptor)
            try: temporary.unlink()
            except FileNotFoundError: pass
    except Missing: return Result("GAP", job, target, reason="archive_absent")
    except Refused as error: return Result("REFUSED", job, target, error.measured, "archive_over_8000000_bytes")
    except (PullError, OSError) as error: return Result("ERROR", job, target, reason=str(error))

def cohort(manifest: Path, source_data: Path) -> Tuple[Dict[str, str], str]:
    if manifest.exists():
        try: payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error: raise PullError("cohort manifest is unreadable") from error
        if not isinstance(payload, list) or not payload: raise PullError("cohort manifest is not a nonempty list")
        answer: Dict[str, str] = {}
        for row in payload:
            if not isinstance(row, Mapping): raise PullError("cohort row is not an object")
            name, venue_class = symbol(row.get("symbol")), row.get("venue_class")
            if venue_class not in {"spot_only", "futures_only", "both"} or name in answer: raise PullError("cohort row is invalid or repeated")
            answer[name] = "futures" if venue_class == "futures_only" else "spot"
        return answer, "cohort_manifest"
    roots = {"spot": source_data / "spot/daily/klines", "futures": source_data / "futures/um/daily/klines"}
    found = {venue: set() for venue in roots}
    for venue, root in roots.items():
        if not root.is_dir(): continue
        for directory in root.iterdir():
            name = symbol(directory.name)
            if directory.is_dir() and any((directory / "1m").glob(f"{name}-1m-*.zip")):
                found[venue].add(name)
    names = found["spot"] | found["futures"]
    if not names: raise PullError("filename inventory yielded no cohort symbols")
    return ({name: "spot" if name in found["spot"] else "futures" for name in sorted(names)}, "filename_inventory")

def jobs_for(items: Mapping[str, str]) -> List[Job]:
    return [Job(name, venue, cadence, period) for name, venue in sorted(items.items())
            for cadence, periods in (("monthly", MONTHS), ("daily", DAYS)) for period in periods]

def archive_rows(job: Job, path: Path) -> Iterable[Tuple[date, Decimal, Decimal, Decimal]]:
    with zipfile.ZipFile(path) as archive, archive.open(job.member()) as raw:
        with io.TextIOWrapper(raw, encoding="utf-8", newline="") as handle:
            for row in csv.reader(handle):
                if row and row[0].lower().startswith("open"): continue
                if len(row) < 5: raise PullError("kline row has fewer than five columns")
                try:
                    stamp = int(row[0])
                    while stamp > 10_000_000_000_000: stamp //= 1_000
                    day = date(1970, 1, 1) + timedelta(days=stamp // 86_400_000)
                    high, low, close = Decimal(row[2]), Decimal(row[3]), Decimal(row[4])
                except (ValueError, InvalidOperation, OverflowError) as error: raise PullError("kline row has invalid price data") from error
                if not all(value.is_finite() and value > 0 for value in (high, low, close)): raise PullError("kline price is not finite and positive")
                if low > high or not low <= close <= high: raise PullError("kline price range is inconsistent")
                if START <= day <= END: yield day, high, low, close

def render(value: Decimal) -> str:
    text = format(value, "f"); return text.rstrip("0").rstrip(".") if "." in text else text

def quantile(values: Sequence[Decimal], tenth: int) -> Decimal:
    ordered = sorted(values)
    with localcontext() as context:
        context.prec = 50; position = Decimal(len(ordered) - 1) * Decimal(tenth) / 10
        lower = int(position); upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

def measure(items: Mapping[str, str], results: Sequence[Result]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    rows: Dict[str, Dict[date, Tuple[Decimal, Decimal, Decimal]]] = {name: {} for name in items}
    for result in results:
        if result.status not in {"DOWNLOADED", "SKIPPED"}: continue
        for day, high, low, close in archive_rows(result.job, result.path):
            previous, current = rows[result.job.symbol].get(day), (high, low, close)
            if previous is not None and previous != current: raise PullError("archives disagree for one symbol-day")
            rows[result.job.symbol][day] = current
    symbols: Dict[str, Any] = {}; positions: List[Decimal] = []
    for name, venue in sorted(items.items()):
        history = rows[name]; count = len(history)
        high = max((row[0] for row in history.values()), default=None)
        low = min((row[1] for row in history.values()), default=None)
        latest = max(history, default=None); close = history[latest][2] if latest else None
        position = None
        if high is not None and low is not None and close is not None and high != low:
            with localcontext() as context:
                context.prec = 50; position = (close - low) / (high - low)
        ready = count >= 90 and position is not None
        if ready: positions.append(position)
        symbols[name] = {
            "venue": venue, "status": "READY" if ready else "NOT-READY",
            "days_of_history": count, "high_365": None if high is None else render(high),
            "low_365": None if low is None else render(low),
            "latest_close": None if close is None else render(close),
            "latest_day": None if latest is None else latest.isoformat(),
            "low_position_365": None if position is None else render(position),
            "not_ready_reason": None if ready else (
                "under_90_days" if count < 90 else "zero_or_unknown_price_range")}
    deciles = {f"p{n * 10}": render(quantile(positions, n)) for n in range(11)} if positions else {}
    return symbols, {"n": len(positions), "deciles": deciles}

def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1; handle.write(encoded); handle.flush(); os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if descriptor >= 0: os.close(descriptor)
        try: Path(name).unlink()
        except FileNotFoundError: pass

def progress(result: Result) -> str:
    base = (f"{result.status} | {result.job.symbol} | {result.job.period} | "
            f"venue={result.job.venue} | cadence={result.job.cadence}")
    if result.status in {"DOWNLOADED", "SKIPPED"}: return f"{base} | bytes={result.size}"
    if result.status == "REFUSED":
        return f"{base} | measured_bytes={result.size} | bound={MAX_BYTES} | reason={result.reason}"
    return f"{base} | reason={result.reason}"

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST); parser.add_argument("--source-data", type=Path, default=SOURCE_DATA)
    parser.add_argument("--dest", type=Path, default=DEST); parser.add_argument("--output", type=Path, default=OUTPUT)
    arguments = parser.parse_args(argv)
    try:
        items, source = cohort(arguments.manifest, arguments.source_data); jobs = jobs_for(items)
        print(f"MODE | daily_klines | source={source} | symbols={len(items)} | requests={len(jobs)} | workers={WORKERS}", flush=True)
        results: List[Result] = []
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = [executor.submit(download, job, arguments.dest) for job in jobs]
            for future in as_completed(futures):
                result = future.result(); results.append(result); print(progress(result), flush=True)
        results.sort(key=lambda item: item.job)
        statuses = ("DOWNLOADED", "SKIPPED", "GAP", "REFUSED", "ERROR")
        counts = {status: sum(row.status == status for row in results) for status in statuses}
        symbols, distribution = measure(items, results)
        payload = {
            "schema": "long-low-positions-v1", "as_of": END.isoformat(),
            "window_start": START.isoformat(), "cohort_source": source,
            "symbol_count": len(items), "request_count": len(jobs),
            "download_tallies": {key.lower(): value for key, value in counts.items()},
            "distribution": distribution,
            "not_ready_count": sum(row["status"] == "NOT-READY" for row in symbols.values()),
            "gaps": [{"symbol": row.job.symbol, "venue": row.job.venue,
                      "cadence": row.job.cadence, "period": row.job.period}
                     for row in results if row.status == "GAP"], "symbols": symbols}
        write_json(arguments.output, payload)
        print(f"DONE | symbols={len(items)} | requests={len(jobs)} | "
              f"downloaded={counts['DOWNLOADED']} | skipped={counts['SKIPPED']} | "
              f"gaps={counts['GAP']} | refused={counts['REFUSED']} | errors={counts['ERROR']}",
              flush=True)
        return 0 if counts["REFUSED"] == counts["ERROR"] == 0 else 2
    except (PullError, OSError, zipfile.BadZipFile) as error:
        print(f"FATAL | {type(error).__name__} | reason={error}", file=sys.stderr); return 1

if __name__ == "__main__": raise SystemExit(main())
