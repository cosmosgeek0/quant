#!/usr/bin/env python3
"""Bank verified USD-M aggTrades archives for AB23 basis lock-ins."""
from __future__ import annotations

import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path
from typing import Dict, Tuple

try:
    from study import executable_basis_study as basis
    from study import pull_episode_tape as pull
    from study import tape_pull as house
except ImportError:
    import executable_basis_study as basis  # type: ignore
    import pull_episode_tape as pull  # type: ignore
    import tape_pull as house  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "lane-logs/AB23/basis-funding-cost.jsonl"
WORKERS = 4


def _load() -> Tuple[Tuple[house.ArchiveRequest, ...], Dict[str, Tuple[house.ArchiveRequest, ...]]]:
    rows, _ = basis._load(INPUT)
    if len(rows) != 549:
        raise basis.ExecutableBasisError("AB23 does not contain exactly 549 measured lock-ins")
    requested, by_episode = set(), {}
    for row in rows:
        days = basis._days(row["_base_ms"], row["_peak_ms"] + basis.DAY_MS)
        jobs = tuple(
            house.ArchiveRequest(
                row["symbol"], house.VENUE_FUTURES,
                (basis.EPOCH + timedelta(milliseconds=stamp)).date(),
            )
            for stamp in days
        )
        by_episode[row["episode_id"]] = jobs
        requested.update(jobs)
    return tuple(sorted(requested, key=lambda item: (item.day, item.symbol))), by_episode


def _progress(results: list[pull.Result], total: int, landed: int) -> None:
    counts = Counter(row.status for row in results)
    print(
        f"PROGRESS | completed={len(results)}/{total} | downloaded={counts['DOWNLOADED']} "
        f"| skipped={counts['SKIPPED']} | gaps={counts['GAP']} | refused={counts['REFUSED']} "
        f"| errors={counts['ERROR']} | boundary={counts['BOUNDARY']} | bytes_landed={landed}",
        flush=True,
    )


def main() -> int:
    try:
        requests, by_episode = _load()
    except (basis.ExecutableBasisError, OSError, UnicodeError, ValueError) as error:
        print(f"FATAL | {type(error).__name__} | reason={pull._clean(error)}", file=sys.stderr)
        return 1
    pull.DEST.mkdir(parents=True, exist_ok=True)
    results, pending = [], []
    budget = pull.Budget(0)
    print(
        f"MODE | basis_pair_futures | lock_ins={len(by_episode)} | requests={len(requests)} "
        f"| workers={WORKERS} | archive_bound={pull.MAX_BYTES} | disk_bound={pull.DISK_BOUND}",
        flush=True,
    )
    for request in requests:
        result = pull._present(request, pull.DEST, "SKIPPED")
        if result is None:
            pending.append(request)
            continue
        results.append(result)
        if result.status in {"REFUSED", "ERROR"}: print(pull._row(result), flush=True)
        if len(results) % 50 == 0: _progress(results, len(requests), budget.new)
    boundary_reported = False
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(pull._download, request, budget) for request in pending]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if result.status in {"GAP", "REFUSED", "ERROR"} or (
                result.status == "BOUNDARY" and not boundary_reported
            ):
                print(pull._row(result), flush=True)
                boundary_reported = boundary_reported or result.status == "BOUNDARY"
            if len(results) % 50 == 0 or len(results) == len(requests):
                _progress(results, len(requests), budget.new)
    counts = Counter(row.status for row in results)
    available = {row.request for row in results if row.status in {"DOWNLOADED", "SKIPPED"}}
    coverage = Counter(
        "full" if set(jobs) <= available else "partial" if set(jobs) & available else "uncovered"
        for jobs in by_episode.values()
    )
    exit_code = 2 if counts["REFUSED"] or counts["ERROR"] or counts["BOUNDARY"] else 0
    print(
        f"DONE | requests={len(requests)} | downloaded={counts['DOWNLOADED']} "
        f"| skipped={counts['SKIPPED']} | gaps={counts['GAP']} | refused={counts['REFUSED']} "
        f"| errors={counts['ERROR']} | boundary={counts['BOUNDARY']} | bytes_landed={budget.new} "
        f"| full={coverage['full']} | partial={coverage['partial']} "
        f"| uncovered={coverage['uncovered']} | exit={exit_code}",
        flush=True,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
