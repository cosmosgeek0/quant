#!/usr/bin/env python3
"""Pull six monthly 1m kline sets and run the pump census Stage 1."""
from __future__ import annotations

import argparse, os, re, sys, tempfile, time, zipfile
from collections import Counter; from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone; from decimal import Decimal; from pathlib import Path
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError; from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from study import pull_daily_klines as house; from study import pump_census as census; from study import tape_pull
except ImportError:
    import pull_daily_klines as house; import pump_census as census; import tape_pull  # type: ignore

ROOT = Path(__file__).resolve().parents[1]; BASE = "https://data.binance.vision"
DEST = ROOT / "study/data-1m-history"; RECENT = Path("/Users/cosmosgeek/Desktop/quant/study/data")
LOWS = ROOT / "lane-logs/AB5/long-low-positions.json"; OUTPUT = ROOT / "lane-logs/AB13/pump-census-6mo.json"
MONTHS = tuple(f"2026-{month:02d}" for month in range(2, 8))
START_MS = int(datetime(2026, 2, 15, tzinfo=timezone.utc).timestamp() * 1000); CONTEXT_MS = int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp() * 1000)
END_MS = int(datetime(2026, 8, 15, tzinfo=timezone.utc).timestamp() * 1000)
# Six monthly 1m archives can be substantial, but 256 MiB per archive and
# member is the acquisition's deliberate local boundary.
MAX_BYTES, DISK_LIMIT, WORKERS = 268_435_456, 60_000_000_000, 4


class Missing(house.PullError): pass
class Refused(house.PullError): pass


class Job(NamedTuple):
    symbol: str; venue: str; month: str
    def prefix(self) -> str: return "spot" if self.venue == "spot" else "futures/um"
    def filename(self) -> str: return f"{self.symbol}-1m-{self.month}.zip"
    def member(self) -> str: return self.filename()[:-4] + ".csv"
    def url(self) -> str: return f"{BASE}/data/{self.prefix()}/monthly/klines/{quote(self.symbol, safe='')}/1m/{quote(self.symbol, safe='')}-1m-{self.month}.zip"
    def path(self, root: Path) -> Path: return root / self.prefix() / "monthly/klines" / self.symbol / "1m" / self.filename()


class Result(NamedTuple): status: str; job: Job; path: Path; size: int = 0; reason: str = ""


def _validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "data.binance.vision" or parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.port):
        raise house.PullError("public URL is outside the GET allowlist")
    decoded = unquote(parsed.path); match = re.fullmatch(r"/data/(spot|futures/um)/monthly/klines/([^/]+)/1m/([^/]+)", decoded)
    if quote(decoded, safe="/-.") != parsed.path: raise house.PullError("public URL path is not canonical")
    if match is None: raise house.PullError("public URL path is outside the archive allowlist")
    _, name, filename = match.groups(); house.symbol(name)
    if not re.fullmatch(re.escape(name) + r"-1m-2026-(02|03|04|05|06|07)\.zip", filename):
        raise house.PullError("public URL filename is outside the archive allowlist")


class Redirects(HTTPRedirectHandler):
    def redirect_request(self, request: Request, fp: Any, code: int, message: str, headers: Any, new_url: str) -> Optional[Request]:
        _validate_url(new_url); return super().redirect_request(request, fp, code, message, headers, new_url)


OPENER = build_opener(Redirects())


def _open(job: Job) -> Any:
    request = Request(job.url(), headers={"User-Agent": "quant-census-kline-pull/1"}, method="GET"); _validate_url(request.full_url)
    for attempt in range(5):
        try:
            response = OPENER.open(request, timeout=45); _validate_url(response.geturl()); return response
        except HTTPError as error:
            code = error.code; retry = error.headers.get("Retry-After") if error.headers else None; error.close()
            if code == 404: raise Missing("archive is absent") from error
            if code not in {429, 500, 502, 503, 504} or attempt == 4: raise house.PullError(f"public GET returned HTTP {code}") from error
            time.sleep(min(int(retry) if retry and retry.isdigit() else 2 ** attempt, 60))
        except (TimeoutError, URLError, OSError) as error:
            if attempt == 4: raise house.PullError("public GET transport failed") from error
            time.sleep(2 ** attempt)
    raise house.PullError("public GET retry limit reached")


def _verify(job: Job, path: Path) -> int:
    size = path.stat().st_size
    if size > MAX_BYTES: raise Refused(size)
    if size <= 0: raise house.PullError("archive is empty")
    try:
        with zipfile.ZipFile(path, "r") as archive:
            members = [row for row in archive.infolist() if not row.is_dir()]
            if len(members) != 1 or members[0].filename != job.member(): raise house.PullError("archive member differs from expected CSV")
            if members[0].flag_bits & 1: raise house.PullError("encrypted archive is not accepted")
            for _chunk in tape_pull.stream_verified_member(archive, members[0], MAX_BYTES): pass
    except house.PullError: raise
    except tape_pull.ArchiveError as error:
        measured, bound = error.measured_value, error.bound
        if bound == MAX_BYTES and isinstance(measured, int) and measured > MAX_BYTES: raise Refused(measured) from None
        details = ""
        if measured is not None: details += f" | measured={measured}"
        if bound is not None: details += f" | bound={bound}"
        raise house.PullError(f"{error}{details}") from None
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error: raise house.PullError("file is not an intact ZIP") from error
    return size


def _download(job: Job, root: Path) -> Result:
    target = job.path(root)
    try:
        if target.exists(): return Result("SKIPPED", job, target, _verify(job, target))
        target.parent.mkdir(parents=True, exist_ok=True); descriptor, raw_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".part", dir=target.parent)
        temporary = Path(raw_name)
        try:
            response = _open(job); declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_BYTES: raise Refused(int(declared))
            written = 0
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = -1
                    while chunk := response.read(house.CHUNK):
                        written += len(chunk)
                        if written > MAX_BYTES: raise Refused(written)
                        handle.write(chunk)
                    handle.flush(); os.fsync(handle.fileno())
            finally: response.close()
            _verify(job, temporary); os.replace(temporary, target); return Result("DOWNLOADED", job, target, written)
        finally:
            if descriptor >= 0: os.close(descriptor)
            try: temporary.unlink()
            except FileNotFoundError: pass
    except Missing: return Result("GAP", job, target, reason="archive_absent")
    except Refused as error: return Result("REFUSED", job, target, int(error.args[0]), "archive_over_268435456_bytes")
    except (house.PullError, OSError) as error: return Result("ERROR", job, target, reason=str(error))


def _progress(result: Result) -> str:
    row = f"{result.status} | {result.job.symbol} | {result.job.month} | venue={result.job.venue}"
    if result.status in {"DOWNLOADED", "SKIPPED"}: return f"{row} | bytes={result.size}"
    if result.status == "REFUSED": return f"{row} | measured_bytes={result.size} | bound={MAX_BYTES} | reason={result.reason}"
    return f"{row} | reason={result.reason}"


def _tree_bytes(root: Path) -> int: return sum(path.stat().st_size for path in root.rglob("*.zip")) if root.exists() else 0


def _pull(records: Mapping[str, Mapping[str, Any]], root: Path) -> Tuple[List[Result], List[str], bool]:
    results: List[Result] = []; completed: List[str] = []; stopped = False
    for month in MONTHS:
        jobs = [Job(name, row["venue"], month) for name, row in records.items()]; print(f"MONTH | {month} | requests={len(jobs)} | workers={WORKERS}", flush=True)
        monthly: List[Result] = []
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = [executor.submit(_download, job, root) for job in jobs]
            for future in as_completed(futures):
                result = future.result(); monthly.append(result); print(_progress(result), flush=True)
        results.extend(monthly); completed.append(month)
        total, month_bytes = _tree_bytes(root), sum(row.size for row in monthly if row.status in {"DOWNLOADED", "SKIPPED"})
        counts = Counter(row.status for row in monthly)
        print(f"MONTH_DONE | {month} | downloaded={counts['DOWNLOADED']} | skipped={counts['SKIPPED']} | "
              f"gaps={counts['GAP']} | refused={counts['REFUSED']} | errors={counts['ERROR']} | disk_bytes={total}", flush=True)
        if month != MONTHS[-1] and total + month_bytes > DISK_LIMIT:
            stopped = True; print(f"DISK_BOUNDARY | completed={month} | disk_bytes={total} | bound={DISK_LIMIT}", flush=True); break
    return results, completed, stopped


def _scan_one(item: Tuple[str, Mapping[str, Any], Path, Path, Tuple[str, ...]]) -> Tuple[List[Dict[str, Any]], int, int, int, int]:
    symbol, record, root, recent, months = item; prefix = "spot" if record["venue"] == "spot" else "futures/um"
    paths = [root / prefix / "monthly/klines" / symbol / "1m" / f"{symbol}-1m-{month}.zip" for month in months] + sorted((recent / prefix / "daily/klines" / symbol / "1m").glob(f"{symbol}-1m-*.zip"))
    indexed: Dict[int, Any] = {}
    for path in paths:
        if not path.is_file(): continue
        for bar in census.absorption._kline_rows(path, symbol):
            if not CONTEXT_MS <= bar.open_ms < END_MS: continue
            prior = indexed.get(bar.open_ms)
            if prior is not None and prior != bar: raise census.CensusError(f"{symbol} has conflicting kline evidence")
            indexed[bar.open_ms] = bar
    bars = tuple(indexed[key] for key in sorted(indexed))
    if not bars: return [], 1, 0, 0, 0
    median, complete_days = census._median_day(bars)
    if median is None: return [], 0, 1, complete_days, 0
    episodes, trailing = census._symbol_episodes(symbol, record["venue"], bars, median, record)
    return [row for row in episodes if START_MS <= row["_base_ms"] < END_MS], 0, 0, complete_days, trailing


def _scan(records: Mapping[str, Mapping[str, Any]], root: Path, recent: Path, months: Sequence[str]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    episodes: List[Dict[str, Any]] = []; missing = no_median = complete_days = trailing = 0
    items = [(name, row, root, recent, tuple(months)) for name, row in records.items()]
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(_scan_one, item) for item in items]
        for ordinal, future in enumerate(as_completed(futures), 1):
            rows, absent, no_day, days, incomplete = future.result(); episodes.extend(rows)
            missing += absent; no_median += no_day; complete_days += days; trailing += incomplete
            if ordinal % 25 == 0 or ordinal == len(items):
                print(f"CENSUS | symbols={ordinal}/{len(items)} | episodes={len(episodes)}", flush=True)
    episodes.sort(key=lambda row: (row["_base_ms"], row["symbol"], -row["_rise"]));
    for number, row in enumerate(episodes, 1): row["episode_id"] = f"P{number:05d}"
    return episodes, {"symbols_scanned": len(records), "symbols_without_bars": missing,
                      "symbols_without_complete_day_median": no_median, "complete_symbol_days": complete_days,
                      "trailing_candidates_without_24h_followup": trailing}


def _payload(metadata: Mapping[str, Any], results: Sequence[Result], months: Sequence[str], stopped: bool, episodes: Sequence[Mapping[str, Any]], scan: Mapping[str, int], root: Path) -> Dict[str, Any]:
    counts = Counter(row.status for row in results); monthly = {}
    for month in tuple(MONTHS) + ("2026-08",):
        rows = [row for row in episodes if row["date_utc"].startswith(month)]
        monthly[month] = {"episodes": len(rows), "HELD": sum(row["class"] == "HELD" for row in rows),
                          "COLLAPSED": sum(row["class"] == "COLLAPSED" for row in rows)}
    low = sum(Decimal(row["low_position_365"]) < Decimal("0.10") for row in episodes)
    summary = {"episodes": len(episodes), "HELD": sum(row["class"] == "HELD" for row in episodes),
               "COLLAPSED": sum(row["class"] == "COLLAPSED" for row in episodes),
               "veteran": sum(row["listing_age_class"] == "veteran" for row in episodes),
               "fresh": sum(row["listing_age_class"] == "fresh" for row in episodes),
               "yearly_low_launchpad": {"episodes": low, "fraction": f"{low}/{len(episodes)}"}, "per_month": monthly}
    acquisition = {"months_completed": list(months), "stopped_at_disk_boundary": stopped,
                   "disk_bytes": _tree_bytes(root), "disk_bound_bytes": DISK_LIMIT,
                   "tallies": {name.lower(): counts[name] for name in ("DOWNLOADED", "SKIPPED", "GAP", "REFUSED", "ERROR")},
                   "gaps": [{"symbol": row.job.symbol, "venue": row.job.venue, "month": row.job.month}
                            for row in results if row.status == "GAP"],
                   "refused": [{"symbol": row.job.symbol, "venue": row.job.venue, "month": row.job.month,
                                "measured_bytes": row.size} for row in results if row.status == "REFUSED"]}
    header = {"schema": "pump-census-6mo-v1", "as_of": metadata.get("as_of"), "window_start": "2026-02-15",
              "cohort_source": "AB5 long-low positions", "symbol_count": len(metadata["symbols"]), "download": acquisition,
              "definitions": {"base": "lowest proved 1m low in the preceding continuous 24h",
                              "rise": "at least 25 percent from base to a peak no more than 24h later",
                              "volume": "base-to-peak quote volume at least 3x the symbol median complete UTC day",
                              "HELD": "following continuous 24h low retraced less than half the rise",
                              "COLLAPSED": "following continuous 24h low gave back half or more"}, "scan": dict(scan)}
    return {"header": header, "summary": summary,
            "episodes": [{key: value for key, value in row.items() if not key.startswith("_")} for row in episodes]}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--dest", type=Path, default=DEST); parser.add_argument("--recent-data", type=Path, default=RECENT)
    parser.add_argument("--long-lows", type=Path, default=LOWS); parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    try:
        metadata, records = census._metadata(args.long_lows)
        if len(records) != 659: raise census.CensusError(f"AB5 universe has {len(records)} symbols, expected 659")
        for name in records: house.symbol(name)
        results, months, stopped = _pull(records, args.dest)
        episodes, scan = _scan(records, args.dest, args.recent_data, months)
        if not episodes: raise census.CensusError("census found no proved pump episodes")
        house.write_json(args.output, _payload(metadata, results, months, stopped, episodes, scan, args.dest))
        counts = Counter(row.status for row in results)
        print(f"DONE | symbols={len(records)} | episodes={len(episodes)} | downloaded={counts['DOWNLOADED']} | skipped={counts['SKIPPED']} | gaps={counts['GAP']} | refused={counts['REFUSED']} | errors={counts['ERROR']}", flush=True)
        return 0 if not stopped and counts["REFUSED"] == counts["ERROR"] == 0 else 2
    except (OSError, UnicodeError, ValueError, house.PullError, census.CensusError, census.absorption.AbsorptionError) as error:
        print(f"census pull: {error}", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
