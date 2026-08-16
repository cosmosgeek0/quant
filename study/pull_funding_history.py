#!/usr/bin/env python3
"""Pull verified monthly USD-M perpetual funding-rate archives."""
from __future__ import annotations

import csv, io, json, os, re, sys, tempfile, time, zipfile
from collections import Counter; from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone; from decimal import Decimal, InvalidOperation; from pathlib import Path
from typing import Any, NamedTuple, Optional; from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit; from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from study import pull_daily_klines as house, tape_pull
except ImportError:
    import pull_daily_klines as house, tape_pull  # type: ignore

ROOT = Path(__file__).resolve().parents[1]; BASE = "https://data.binance.vision"; KLINES = ROOT / "study/data-1m-history"
DEST = ROOT / "study/data-funding"; OUTPUT = ROOT / "lane-logs/AB23/funding-pull.json"
MONTHS = tuple(f"2026-{month:02d}" for month in range(2, 9)); MAX_ZIP, MAX_MEMBER, CHUNK, WORKERS = 1_000_000, 2_000_000, 128 * 1024, 4; HEADER = ["calc_time", "funding_interval_hours", "last_funding_rate"]

class PullError(Exception): pass
class Missing(PullError): pass
class Refused(PullError): pass

class Job(NamedTuple):
    symbol: str; month: str
    def filename(self) -> str: return f"{self.symbol}-fundingRate-{self.month}.zip"
    def member(self) -> str: return self.filename()[:-4] + ".csv"
    def url(self) -> str: name = quote(self.symbol, safe=""); return f"{BASE}/data/futures/um/monthly/fundingRate/{name}/{name}-fundingRate-{self.month}.zip"
    def path(self, root: Path) -> Path: return root / "futures/um/monthly/fundingRate" / self.symbol / self.filename()

class Result(NamedTuple):
    status: str; job: Job; path: Path; size: int = 0; rows: int = 0; intervals: tuple[int, ...] = (); reason: str = ""

def _validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "data.binance.vision" or parsed.query or parsed.fragment or
            parsed.username or parsed.password or parsed.port): raise PullError("public URL is outside the GET allowlist")
    decoded = unquote(parsed.path); match = re.fullmatch(r"/data/futures/um/monthly/fundingRate/([^/]+)/([^/]+)", decoded)
    if quote(decoded, safe="/-.") != parsed.path or match is None: raise PullError("public URL path differs")
    name, filename = match.groups(); house.symbol(name)
    if not re.fullmatch(re.escape(name) + r"-fundingRate-2026-(02|03|04|05|06|07|08)\.zip", filename):
        raise PullError("public URL filename differs")

class Redirects(HTTPRedirectHandler):
    def redirect_request(self, request: Request, fp: Any, code: int, message: str, headers: Any, new_url: str) -> Optional[Request]:
        _validate_url(new_url); return super().redirect_request(request, fp, code, message, headers, new_url)

OPENER = build_opener(Redirects())

def _open(job: Job) -> Any:
    request = Request(job.url(), headers={"User-Agent": "quant-funding-history-pull/1"}, method="GET"); _validate_url(request.full_url)
    for attempt in range(5):
        try:
            response = OPENER.open(request, timeout=45); _validate_url(response.geturl()); return response
        except HTTPError as error:
            code = error.code; retry = error.headers.get("Retry-After") if error.headers else None; error.close()
            if code == 404: raise Missing("archive is absent") from error
            if code not in {429, 500, 502, 503, 504} or attempt == 4: raise PullError(f"public GET returned HTTP {code}") from error
            time.sleep(min(int(retry) if retry and retry.isdigit() else 2 ** attempt, 60))
        except (TimeoutError, URLError, OSError) as error:
            if attempt == 4: raise PullError("public GET transport failed") from error
            time.sleep(2 ** attempt)
    raise PullError("public GET retry limit reached")

def _verify(job: Job, path: Path) -> tuple[int, int, tuple[int, ...]]:
    size = path.stat().st_size
    if size <= 0: raise PullError("archive is empty")
    if size > MAX_ZIP: raise Refused(f"archive exceeds {MAX_ZIP} bytes")
    try:
        with zipfile.ZipFile(path, "r") as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) != 1 or members[0].filename != job.member(): raise PullError("archive member differs")
            if members[0].flag_bits & 1: raise PullError("encrypted archive is not accepted")
            if members[0].file_size > MAX_MEMBER: raise Refused(f"member exceeds {MAX_MEMBER} bytes")
            raw = b"".join(tape_pull.stream_verified_member(archive, members[0], MAX_MEMBER))
        reader = csv.reader(io.StringIO(raw.decode("utf-8")))
        if next(reader, None) != HEADER: raise PullError("funding CSV header differs")
        count, prior, intervals = 0, None, set()
        for row in reader:
            if len(row) != 3: raise PullError("funding CSV row width differs")
            stamp, interval, rate = int(row[0]), int(row[1]), Decimal(row[2])
            while stamp > 10_000_000_000_000: stamp //= 1_000
            if (prior is not None and stamp <= prior) or not 0 < interval <= 24 or not rate.is_finite():
                raise PullError("funding CSV value differs")
            if datetime.fromtimestamp(stamp / 1000, timezone.utc).strftime("%Y-%m") != job.month:
                raise PullError("funding timestamp is outside its archive month")
            prior, count = stamp, count + 1; intervals.add(interval)
        if not count: raise PullError("funding CSV has no settlement rows")
        return size, count, tuple(sorted(intervals))
    except (PullError, Refused): raise
    except tape_pull.ArchiveError as error: raise PullError(str(error)) from None
    except (OSError, UnicodeError, ValueError, InvalidOperation, csv.Error, zipfile.BadZipFile) as error: raise PullError("funding archive is not readable") from error

def _download(job: Job, root: Path) -> Result:
    target = job.path(root); descriptor = -1; temporary: Optional[Path] = None
    try:
        if target.exists():
            size, rows, intervals = _verify(job, target); return Result("SKIPPED", job, target, size, rows, intervals)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".part", dir=target.parent); temporary = Path(name)
        response = _open(job); written = 0
        try:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_ZIP: raise Refused("declared archive is above bound")
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                while chunk := response.read(CHUNK):
                    written += len(chunk)
                    if written > MAX_ZIP: raise Refused("archive is above bound")
                    handle.write(chunk)
                handle.flush(); os.fsync(handle.fileno())
        finally: response.close()
        size, rows, intervals = _verify(job, temporary); os.replace(temporary, target); temporary = None
        return Result("DOWNLOADED", job, target, size, rows, intervals)
    except Missing: return Result("GAP", job, target, reason="archive_absent")
    except Refused as error: return Result("REFUSED", job, target, reason=str(error))
    except (PullError, OSError) as error: return Result("ERROR", job, target, reason=str(error))
    finally:
        if descriptor >= 0: os.close(descriptor)
        if temporary is not None:
            try: temporary.unlink()
            except FileNotFoundError: pass

def _symbols(root: Path) -> list[str]:
    sides = []
    for prefix in (Path("spot/monthly/klines"), Path("futures/um/monthly/klines")):
        directory = root / prefix
        if not directory.is_dir(): raise PullError("paired kline symbol layout is absent")
        sides.append({path.name for path in directory.iterdir() if path.is_dir()})
    names = sorted(sides[0] & sides[1])
    for name in names: house.symbol(name)
    if not names: raise PullError("paired kline symbol overlap is empty")
    return names

def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":")); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)

def main() -> int:
    try:
        names = _symbols(KLINES); jobs = [Job(name, month) for month in MONTHS for name in names]; results = []
        print(f"MODE | fundingRate_monthly | symbols={len(names)} | requests={len(jobs)} | workers={WORKERS}", flush=True)
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = [executor.submit(_download, job, DEST) for job in jobs]
            for ordinal, future in enumerate(as_completed(futures), 1):
                result = future.result(); results.append(result)
                if result.status in {"GAP", "REFUSED", "ERROR"}:
                    print(f"{result.status} | {result.job.symbol} | {result.job.month} | reason={result.reason}", flush=True)
                if ordinal % 100 == 0 or ordinal == len(jobs):
                    counts = Counter(row.status for row in results)
                    print(f"PROGRESS | requests={ordinal}/{len(jobs)} | downloaded={counts['DOWNLOADED']} | skipped={counts['SKIPPED']} | gaps={counts['GAP']} | refused={counts['REFUSED']} | errors={counts['ERROR']}", flush=True)
        counts = Counter(row.status for row in results); intervals = Counter(value for row in results for value in row.intervals)
        payload = {"schema": "funding-rate-pull-v1", "symbol_source": "paired local 1m-history overlap", "symbols": len(names),
                   "months": list(MONTHS), "requests": len(jobs), "tallies": {key.lower(): counts[key] for key in ("DOWNLOADED", "SKIPPED", "GAP", "REFUSED", "ERROR")},
                   "available_rows": sum(row.rows for row in results), "available_bytes": sum(row.size for row in results if row.status in {"DOWNLOADED", "SKIPPED"}),
                   "archive_interval_hours": {str(key): value for key, value in sorted(intervals.items())},
                   "gaps": [{"symbol": row.job.symbol, "month": row.job.month} for row in results if row.status == "GAP"]}
        _write(OUTPUT, payload)
        print(f"DONE | symbols={len(names)} | requests={len(jobs)} | available={counts['DOWNLOADED'] + counts['SKIPPED']} | gaps={counts['GAP']} | rows={payload['available_rows']} | exit={0 if not counts['REFUSED'] and not counts['ERROR'] else 2}", flush=True)
        return 0 if not counts["REFUSED"] and not counts["ERROR"] else 2
    except (OSError, UnicodeError, ValueError, PullError, house.PullError) as error:
        print(f"funding history pull: {error}", file=sys.stderr); return 1

if __name__ == "__main__": raise SystemExit(main())
