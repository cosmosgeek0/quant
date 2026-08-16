#!/usr/bin/env python3
"""Bank verified daily aggTrades archives for AB13 episode windows."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, NamedTuple, Sequence, Tuple

try:
    from study import tape_pull as house
except ImportError:
    import tape_pull as house  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "lane-logs/AB13/pump-census-6mo.json"
DEST = ROOT / "study/data-episode-tape"
CROSS = ROOT / "study/data"
# Archive responses are streamed to a temporary file; accept up to 2 GiB.
MAX_BYTES = 2_147_483_648
DISK_BOUND = 20_000_000_000
WORKERS = 4


class PullError(Exception): pass
class Refused(PullError): pass
class Boundary(PullError): pass


class Result(NamedTuple):
    status: str
    request: house.ArchiveRequest
    path: Path
    size: int = 0
    reason: str = ""


class Budget:
    def __init__(self, used: int) -> None:
        self.used, self.reserved, self.new = used, 0, 0
        self.lock, self.stopped = threading.Lock(), threading.Event()
        if used >= DISK_BOUND: self.stopped.set()

    def reserve(self, size: int) -> bool:
        with self.lock:
            if self.stopped.is_set() or self.used + self.reserved + size > DISK_BOUND:
                self.stopped.set(); return False
            self.reserved += size; return True

    def grow(self, size: int) -> bool:
        return self.reserve(size)

    def release(self, size: int) -> None:
        with self.lock: self.reserved -= size

    def commit(self, reserved: int, actual: int) -> None:
        with self.lock:
            self.reserved -= reserved; self.used += actual; self.new += actual


def _clean(value: Any) -> str:
    return " ".join(str(value).replace("|", "/").split()) or "unknown"


def _base_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PullError("episode base_utc is not UTC text")
    try: parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError: raise PullError("episode base_utc is invalid") from None
    if parsed.utcoffset() != timedelta(0): raise PullError("episode base_utc is not UTC")
    return parsed.astimezone(timezone.utc)


def _load(path: Path) -> Tuple[Sequence[Mapping[str, Any]], Tuple[house.ArchiveRequest, ...], Dict[str, Tuple[house.ArchiveRequest, ...]]]:
    try: payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error: raise PullError(f"census cannot be read: {_clean(error)}") from None
    rows = payload.get("episodes") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list) or len(rows) != 3_429: raise PullError("census does not contain exactly 3429 episodes")
    requested, by_episode, seen_ids = set(), {}, set()
    for row in rows:
        if not isinstance(row, Mapping): raise PullError("census episode row differs")
        episode_id = row.get("episode_id")
        if not isinstance(episode_id, str) or episode_id in seen_ids: raise PullError("episode_id is absent or repeated")
        try: symbol, venue = house._validate_symbol(row.get("symbol")), house._validate_venue(row.get("venue"))
        except house.TapePullError as error: raise PullError(_clean(error)) from None
        base = _base_time(row.get("base_utc")); first = (base - timedelta(hours=12)).date(); last = base.date()
        jobs = tuple(house.ArchiveRequest(symbol, venue, day) for day in (first,) if first == last)
        if first != last: jobs = (house.ArchiveRequest(symbol, venue, first), house.ArchiveRequest(symbol, venue, last))
        seen_ids.add(episode_id); by_episode[episode_id] = jobs; requested.update(jobs)
    return rows, tuple(sorted(requested, key=lambda row: (row.day, row.symbol, row.venue))), by_episode


def _present(request: house.ArchiveRequest, root: Path, status: str) -> Result | None:
    path = house.agg_trades_path(root, request.symbol, request.day, request.venue)
    if not path.is_file(): return None
    size = path.stat().st_size
    if size > MAX_BYTES: return Result("REFUSED", request, path, size, "archive_over_2147483648_bytes")
    try: house.verify_agg_trades_zip(path, request.symbol, request.day, max_member_bytes=MAX_BYTES)
    except (house.TapePullError, OSError) as error: return Result("ERROR", request, path, size, f"existing_archive_invalid:{_clean(error)}")
    return Result(status, request, path, size)


def _download(request: house.ArchiveRequest, budget: Budget) -> Result:
    target = house.agg_trades_path(DEST, request.symbol, request.day, request.venue)
    if budget.stopped.is_set(): return Result("BOUNDARY", request, target, reason="disk_boundary")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary, response, reserved, committed = -1, None, None, 0, False
    try:
        response = house._open_get(house.agg_trades_url(request.symbol, request.day, request.venue))
        raw = response.headers.get("Content-Length") if response.headers is not None else None
        declared = int(raw) if raw and raw.isdigit() else MAX_BYTES
        if declared > MAX_BYTES: raise Refused(declared)
        reserved = declared
        if not budget.reserve(reserved): raise Boundary("disk_boundary")
        descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".part", dir=target.parent); temporary = Path(name)
        written = 0
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            while True:
                chunk = response.read(house.READ_CHUNK_BYTES)
                if not chunk: break
                written += len(chunk)
                if written > MAX_BYTES: raise Refused(written)
                if written > reserved:
                    extra = written - reserved
                    if not budget.grow(extra): raise Boundary("disk_boundary")
                    reserved += extra
                handle.write(chunk)
            handle.flush(); os.fsync(handle.fileno())
        if written <= 0: raise PullError("archive_response_empty")
        house.verify_agg_trades_zip(temporary, request.symbol, request.day, max_member_bytes=MAX_BYTES)
        os.replace(temporary, target); budget.commit(reserved, written); committed = True
        return Result("DOWNLOADED", request, target, written)
    except house.PublicReadError as error:
        return Result("GAP" if error.status == 404 else "ERROR", request, target, reason="archive_absent" if error.status == 404 else _clean(error))
    except Refused as error:
        return Result("REFUSED", request, target, int(error.args[0]), "archive_over_2147483648_bytes")
    except Boundary:
        return Result("BOUNDARY", request, target, reason="disk_boundary")
    except house.ArchiveError as error:
        measured, bound = getattr(error, "measured_value", None), getattr(error, "bound", None)
        if bound == MAX_BYTES and isinstance(measured, int) and measured > MAX_BYTES:
            return Result("REFUSED", request, target, measured, "archive_over_2147483648_bytes")
        return Result("ERROR", request, target, reason=_clean(error))
    except (house.TapePullError, PullError, OSError, ValueError) as error:
        return Result("ERROR", request, target, reason=_clean(error))
    finally:
        if response is not None: response.close()
        if descriptor >= 0: os.close(descriptor)
        if temporary is not None:
            try: temporary.unlink()
            except FileNotFoundError: pass
        if reserved and not committed: budget.release(reserved)


def _row(result: Result) -> str:
    request = result.request; base = f"{result.status} | {request.symbol} | {request.day.isoformat()} | venue={request.venue}"
    if result.status in {"SKIPPED", "CROSS-SKIP", "DOWNLOADED"}: return f"{base} | bytes={result.size} | zip=ok"
    if result.status == "REFUSED": return f"{base} | measured_bytes={result.size} | bound={MAX_BYTES} | reason={result.reason}"
    return f"{base} | reason={result.reason}"


def main() -> int:
    try: episodes, requests, by_episode = _load(INPUT)
    except PullError as error: print(f"FATAL | PullError | reason={_clean(error)}", file=sys.stderr); return 1
    DEST.mkdir(parents=True, exist_ok=True); results, pending = [], []
    print(f"MODE | episode_tape | episodes={len(episodes)} | requests={len(requests)} | workers={WORKERS} | bound={DISK_BOUND}", flush=True)
    for request in requests:
        result = _present(request, DEST, "SKIPPED") or _present(request, CROSS, "CROSS-SKIP")
        (results if result else pending).append(result or request)
        if result and result.status in {"CROSS-SKIP", "REFUSED", "ERROR"}: print(_row(result), flush=True)
    budget = Budget(0); completed, boundary_reported = len(results), False
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(_download, request, budget) for request in pending]
        for future in as_completed(futures):
            result = future.result(); results.append(result); completed += 1
            if result.status in {"GAP", "REFUSED", "ERROR"} or (result.status == "BOUNDARY" and not boundary_reported):
                print(_row(result), flush=True); boundary_reported = boundary_reported or result.status == "BOUNDARY"
            if completed % 100 == 0 or completed == len(requests):
                counts = Counter(row.status for row in results)
                print(f"PROGRESS | completed={completed}/{len(requests)} | downloaded={counts['DOWNLOADED']} | skipped={counts['SKIPPED']} | cross_skipped={counts['CROSS-SKIP']} | gaps={counts['GAP']} | refused={counts['REFUSED']} | errors={counts['ERROR']} | boundary={counts['BOUNDARY']} | bytes_landed={budget.new}", flush=True)
    counts = Counter(row.status for row in results); available = {row.request for row in results if row.status in {"DOWNLOADED", "SKIPPED", "CROSS-SKIP"}}
    coverage = Counter("full" if set(jobs) <= available else "partial" if set(jobs) & available else "uncovered" for jobs in by_episode.values())
    exit_code = 2 if counts["REFUSED"] or counts["ERROR"] or counts["BOUNDARY"] else 0
    print(f"DONE | requests={len(requests)} | downloaded={counts['DOWNLOADED']} | skipped={counts['SKIPPED']} | cross_skipped={counts['CROSS-SKIP']} | gaps={counts['GAP']} | refused={counts['REFUSED']} | errors={counts['ERROR']} | boundary={counts['BOUNDARY']} | bytes_landed={budget.new} | full={coverage['full']} | partial={coverage['partial']} | uncovered={coverage['uncovered']} | exit={exit_code}", flush=True)
    return exit_code


if __name__ == "__main__": raise SystemExit(main())
