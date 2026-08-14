#!/usr/bin/env python3
"""Canonical filesystem paths shared by the deployed runtime.

All mutable runtime artifacts live below ``RECEIPT_DIR``.  Import ``PATHS``
or call :func:`resolve_runtime_paths` with an explicit environment mapping.
The small ``--shell`` interface lets host scripts consume the same values
without duplicating path literals.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence


DEFAULT_RECEIPT_DIR = "~/quant_receipts"


@dataclass(frozen=True)
class RuntimePaths:
    receipt_dir: Path
    jobs_dir: Path
    job_results: Path
    proposals_dir: Path
    watch_dir: Path
    stream_state: Path
    stream_log: Path
    stream_windows: Path
    basis_watch_state: Path
    decisions: Path
    health: Path
    momentum_state: Path
    momentum_position: Path
    momentum_receipts: Path
    stop: Path
    stop_jobs: Path

    def as_env(self) -> dict[str, str]:
        """Return the stable variable names consumed by host shell scripts."""
        return {
            "RECEIPT_DIR": str(self.receipt_dir),
            "JOBS_DIR": str(self.jobs_dir),
            "JOB_RESULTS": str(self.job_results),
            "PROPOSALS_DIR": str(self.proposals_dir),
            "WATCH_DIR": str(self.watch_dir),
            "STREAM_STATE": str(self.stream_state),
            "STREAM_LOG": str(self.stream_log),
            "STREAM_WINDOWS": str(self.stream_windows),
            "BASIS_WATCH_STATE": str(self.basis_watch_state),
            "DECISIONS": str(self.decisions),
            "HEALTH": str(self.health),
            "MOMENTUM_STATE": str(self.momentum_state),
            "MOMENTUM_POSITION": str(self.momentum_position),
            "MOMENTUM_RECEIPTS": str(self.momentum_receipts),
            "STOP_FILE": str(self.stop),
            "STOP_JOBS_FILE": str(self.stop_jobs),
        }


def _expanded_receipt_dir(environ: Mapping[str, str]) -> Path:
    raw = environ.get("RECEIPT_DIR", DEFAULT_RECEIPT_DIR)
    if not isinstance(raw, str) or not raw \
            or any(char in raw for char in "\x00\r\n"):
        raise ValueError("RECEIPT_DIR must be one non-empty filesystem path")
    if raw == "~" or raw.startswith("~/"):
        home = environ.get("HOME") or os.path.expanduser("~")
        if not home or any(char in home for char in "\x00\r\n"):
            raise ValueError("HOME must resolve ~ to one filesystem path")
        return Path(home) if raw == "~" else Path(home) / raw[2:]
    return Path(os.path.expanduser(raw))


def resolve_runtime_paths(
    environ: Optional[Mapping[str, str]] = None,
) -> RuntimePaths:
    """Resolve every runtime artifact from the one receipt-directory root."""
    source = os.environ if environ is None else environ
    receipt_dir = _expanded_receipt_dir(source)
    jobs_dir = receipt_dir / "jobs"
    watch_dir = receipt_dir / "watch"
    return RuntimePaths(
        receipt_dir=receipt_dir,
        jobs_dir=jobs_dir,
        job_results=jobs_dir / "results.jsonl",
        proposals_dir=receipt_dir / "proposals",
        watch_dir=watch_dir,
        stream_state=receipt_dir / "stream_state.json",
        stream_log=receipt_dir / "stream_log.jsonl",
        stream_windows=receipt_dir / "stream_windows.json",
        basis_watch_state=watch_dir / "basis_watch_state.json",
        decisions=receipt_dir / "decisions.jsonl",
        health=receipt_dir / "health.jsonl",
        momentum_state=receipt_dir / "momentum-paper-state.json",
        momentum_position=receipt_dir / "momentum-position.json",
        momentum_receipts=receipt_dir / "momentum-paper-receipts.jsonl",
        stop=receipt_dir / "STOP",
        stop_jobs=receipt_dir / "STOP-JOBS",
    )


def render_shell(paths: RuntimePaths) -> str:
    """Render quoted assignments safe to evaluate as shell-owned data."""
    return "\n".join(
        f"{name}={shlex.quote(value)}"
        for name, value in paths.as_env().items()
    )


PATHS = resolve_runtime_paths()
RECEIPT_DIR = PATHS.receipt_dir
JOBS_DIR = PATHS.jobs_dir
JOB_RESULTS = PATHS.job_results
PROPOSALS_DIR = PATHS.proposals_dir
WATCH_DIR = PATHS.watch_dir
STREAM_STATE = PATHS.stream_state
STREAM_LOG = PATHS.stream_log
STREAM_WINDOWS = PATHS.stream_windows
BASIS_WATCH_STATE = PATHS.basis_watch_state
DECISIONS = PATHS.decisions
HEALTH = PATHS.health
MOMENTUM_STATE = PATHS.momentum_state
MOMENTUM_POSITION = PATHS.momentum_position
MOMENTUM_RECEIPTS = PATHS.momentum_receipts
STOP_FILE = PATHS.stop
STOP_JOBS_FILE = PATHS.stop_jobs


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--shell", action="store_true", help="print quoted shell assignments"
    )
    args = parser.parse_args(argv)
    if args.shell:
        print(render_shell(PATHS))
    else:
        print(
            json.dumps(
                {name: str(value) for name, value in asdict(PATHS).items()},
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
