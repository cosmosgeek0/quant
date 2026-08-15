#!/usr/bin/env python3
"""Offline replay of the current momentum detector over Binance 1m tapes.

The input is one or more ``*-1m-*.zip`` archives in ``study/data`` using the
CSV layout published by data.binance.vision.  This module imports only the
read-only market math and lifecycle decision engine.  It never imports a live
runner, creates a job, or performs network I/O.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from study.windows import WindowError as _SymbolValidationError
    from study.windows import validate_symbol
except ImportError:
    from windows import WindowError as _SymbolValidationError
    from windows import validate_symbol


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import market_stream  # noqa: E402  (read-only detector math)
import momentum_lifecycle  # noqa: E402  (decision-only lifecycle)


STUDY_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = STUDY_DIR / "data"
DEFAULT_OUTPUT_DIR = STUDY_DIR / "out"
FORWARD_MINUTES = (5, 15, 60)
COMPARISON_RATIOS = (Decimal("3"), Decimal("10"))
SYMBOL_FROM_NAME = re.compile(
    r"^(?P<symbol>.+?)-1m(?:-|\.)", re.IGNORECASE
)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ReplayError(Exception):
    """A historical tape cannot be proved safe to replay."""


@dataclass(frozen=True)
class TapeBar:
    symbol: str
    open_ms: int
    close_ms: int
    close: Decimal
    quote_volume: Decimal


@dataclass(frozen=True)
class Ignition:
    symbol: str
    utc: str
    close_ms: int
    price: Decimal
    surge_ratio: Decimal


def _decimal(raw: Any, source: str, *, positive: bool) -> Decimal:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise ReplayError(f"{source} is not a Decimal") from None
    if not value.is_finite() or value < 0 or (positive and value <= 0):
        qualifier = "positive" if positive else "non-negative"
        raise ReplayError(f"{source} is not a finite {qualifier} Decimal")
    return value


def _timestamp_ms(raw: Any, source: str) -> int:
    """Normalize Binance millisecond or microsecond epoch text to millis."""
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ReplayError(f"{source} is not an integer timestamp") from None
    if value >= 100_000_000_000_000:
        value //= 1_000
    if value < 100_000_000_000 or value >= 100_000_000_000_000:
        raise ReplayError(f"{source} is outside the supported epoch range")
    return value


def _utc_text(epoch_ms: int) -> str:
    value = EPOCH + timedelta(milliseconds=epoch_ms)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _symbol_from_name(name: str) -> Optional[str]:
    match = SYMBOL_FROM_NAME.match(name)
    if match is None:
        return None
    try:
        return validate_symbol(
            match.group("symbol").upper(), f"{name} symbol"
        )
    except _SymbolValidationError:
        return None


def _rows_from_member(
    archive: Path,
    handle: Iterable[str],
    member_name: str,
    symbol: str,
) -> Iterable[TapeBar]:
    source = f"{archive.name}:{member_name}"
    try:
        rows = csv.reader(handle)
        found = False
        for line_number, row in enumerate(rows, start=1):
            if not row or not any(cell.strip() for cell in row):
                continue
            first = row[0].strip().lower().replace(" ", "_")
            if line_number == 1 and first in {"open_time", "opentime"}:
                continue
            if len(row) < 8:
                raise ReplayError(
                    f"{source}:{line_number} has fewer than eight kline columns"
                )
            open_ms = _timestamp_ms(
                row[0], f"{source}:{line_number} open time"
            )
            close_ms = _timestamp_ms(
                row[6], f"{source}:{line_number} close time"
            )
            if close_ms - open_ms not in {59_999, 60_000}:
                raise ReplayError(
                    f"{source}:{line_number} is not a proved one-minute kline"
                )
            close = _decimal(
                row[4], f"{source}:{line_number} close", positive=True
            )
            quote_volume = _decimal(
                row[7],
                f"{source}:{line_number} quote volume",
                positive=False,
            )
            found = True
            yield TapeBar(symbol, open_ms, close_ms, close, quote_volume)
        if not found:
            raise ReplayError(f"{source} has no kline rows")
    except csv.Error as error:
        raise ReplayError(f"{source} has invalid CSV: {error}") from None


def _read_archive(path: Path) -> Iterable[TapeBar]:
    archive_symbol = _symbol_from_name(path.name)
    try:
        with zipfile.ZipFile(path) as zipped:
            members = sorted(
                (
                    member
                    for member in zipped.infolist()
                    if not member.is_dir()
                    and member.filename.lower().endswith(".csv")
                ),
                key=lambda member: member.filename,
            )
            if not members:
                raise ReplayError(f"{path.name} has no CSV member")
            for member in members:
                if member.flag_bits & 0x1:
                    raise ReplayError(
                        f"{path.name}:{member.filename} is encrypted"
                    )
                member_symbol = _symbol_from_name(member.filename)
                symbol = member_symbol or archive_symbol
                if symbol is None:
                    raise ReplayError(
                        f"cannot prove symbol from {path.name}:{member.filename}"
                    )
                if archive_symbol is not None and member_symbol is not None \
                        and archive_symbol != member_symbol:
                    raise ReplayError(
                        f"symbol mismatch in {path.name}:{member.filename}"
                    )
                with zipped.open(member, "r") as raw:
                    text = (line.decode("utf-8-sig") for line in raw)
                    yield from _rows_from_member(
                        path, text, member.filename, symbol
                    )
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as error:
        raise ReplayError(f"cannot read {path.name}: {error}") from None


def load_tape(data_dir: Path = DEFAULT_DATA_DIR) -> Dict[str, Tuple[TapeBar, ...]]:
    """Load, validate, de-duplicate, and order all Binance 1m zip bars."""
    root = Path(data_dir)
    if not root.is_dir():
        raise ReplayError(f"data directory does not exist: {root}")
    archives = sorted(path for path in root.rglob("*.zip") if path.is_file())
    if not archives:
        raise ReplayError(f"data directory has no 1m kline zips: {root}")

    indexed: Dict[str, Dict[int, TapeBar]] = {}
    for archive in archives:
        for bar in _read_archive(archive):
            existing = indexed.setdefault(bar.symbol, {}).get(bar.close_ms)
            if existing is not None and existing != bar:
                raise ReplayError(
                    f"conflicting duplicate {bar.symbol} kline at "
                    f"{_utc_text(bar.close_ms)}"
                )
            indexed[bar.symbol][bar.close_ms] = bar
    if not indexed:
        raise ReplayError("tape has no valid symbols")
    return {
        symbol: tuple(by_close[close_ms] for close_ms in sorted(by_close))
        for symbol, by_close in sorted(indexed.items())
    }


def _stream_record(
    bar: TapeBar,
    volume: Optional[str],
    median: Optional[str],
    surge: Optional[str],
) -> momentum_lifecycle.StreamRecord:
    price = momentum_lifecycle.fmt(bar.close)
    payload = {
        "utc": _utc_text(bar.close_ms),
        "venues": "spot",
        "spot_bid": price,
        "spot_ask": price,
        "perp_bid": None,
        "perp_ask": None,
        "mark": None,
        "vol_1m_quote": volume,
        "vol_med_1m": median,
        "surge_ratio": surge,
        "oi": None,
        "oi_change_pct": None,
        "funding_rate": None,
        "next_funding_ms": None,
        "basis_gap_pct": None,
        "freshness_ms": "0",
    }
    return momentum_lifecycle.StreamRecord.from_payload(bar.symbol, payload)


def detect_ignitions(
    tape: Mapping[str, Sequence[TapeBar]],
    config: momentum_lifecycle.LifecycleConfig,
    window: int = market_stream.DEFAULT_WINDOW,
) -> Tuple[Ignition, ...]:
    """Return each lifecycle ignition that reaches ENTER_SMALL decision-only."""
    if isinstance(window, bool) or not isinstance(window, int) \
            or window < 2 or window > market_stream.MAX_WINDOW:
        raise ReplayError(
            f"window must be an integer in [2, {market_stream.MAX_WINDOW}]"
        )

    found: List[Ignition] = []
    with tempfile.TemporaryDirectory(prefix="replay-detector-") as temporary:
        receipt_path = Path(temporary) / "decision-receipts.jsonl"
        for symbol in sorted(tape):
            bars = tape[symbol]
            state = momentum_lifecycle.LifecycleState()
            rolling: List[List[str]] = []
            observations: Dict[str, Tuple[TapeBar, Decimal]] = {}
            for bar in bars:
                rolling.append(
                    [
                        str(bar.close_ms),
                        momentum_lifecycle.fmt(bar.quote_volume),
                    ]
                )
                if len(rolling) > window:
                    del rolling[:-window]
                volume, median, surge_text = market_stream.volume_metrics(
                    rolling,
                    window,
                    market_stream.DEFAULT_WINDOW_MIN_COVERAGE_PCT,
                )
                record = _stream_record(bar, volume, median, surge_text)
                if record.surge_ratio is not None:
                    observations[record.utc] = (bar, record.surge_ratio)
                decision = momentum_lifecycle.process_record(
                    state,
                    record,
                    config,
                    receipt_path,
                    now_fn=lambda utc=record.utc: utc,
                )
                if decision.get("action") \
                        != momentum_lifecycle.ACTION_ENTER_SMALL:
                    token = state.tokens[symbol]
                    active_utc = (
                        token.get("ignition_utc")
                        if token.get("candidate") is True
                        else None
                    )
                    if isinstance(active_utc, str) \
                            and active_utc in observations:
                        observations = {
                            active_utc: observations[active_utc]
                        }
                    else:
                        observations.clear()
                    continue
                token = state.tokens[symbol]
                ignition_utc = token.get("ignition_utc")
                observation = observations.get(ignition_utc)
                if not isinstance(ignition_utc, str) or observation is None:
                    raise ReplayError(
                        f"lifecycle fired {symbol} without a proved ignition bar"
                    )
                ignition_bar, ignition_surge = observation
                found.append(
                    Ignition(
                        symbol=symbol,
                        utc=ignition_utc,
                        close_ms=ignition_bar.close_ms,
                        price=ignition_bar.close,
                        surge_ratio=ignition_surge,
                    )
                )
                # A replay decision is not a fill.  Begin a fresh flat shadow
                # after recording it so later threshold crossings remain
                # measurable without ever binding or publishing a job.
                state = momentum_lifecycle.LifecycleState()
                observations.clear()
                try:
                    receipt_path.unlink()
                except FileNotFoundError:
                    pass
    return tuple(sorted(found, key=lambda item: (item.utc, item.symbol)))


def _forward_move(
    ignition: Ignition,
    by_close: Mapping[int, TapeBar],
    minutes: int,
) -> Optional[str]:
    future = by_close.get(ignition.close_ms + (minutes * 60_000))
    if future is None:
        return None
    with localcontext() as context:
        context.prec = 50
        move_pct = (
            (future.close / ignition.price) - Decimal("1")
        ) * Decimal("100")
    return momentum_lifecycle.fmt(move_pct)


def _ignition_row(
    ignition: Ignition,
    by_close: Mapping[int, TapeBar],
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "kind": "ignition",
        "symbol": ignition.symbol,
        "utc": ignition.utc,
        "surge_ratio": momentum_lifecycle.fmt(ignition.surge_ratio),
    }
    for minutes in FORWARD_MINUTES:
        row[f"price_move_next_{minutes}m_pct"] = _forward_move(
            ignition, by_close, minutes
        )
    return row


def _tape_day_count(tape: Mapping[str, Sequence[TapeBar]]) -> int:
    days = {
        (EPOCH + timedelta(milliseconds=bar.close_ms)).date()
        for bars in tape.values()
        for bar in bars
    }
    if not days:
        raise ReplayError("tape has no dated bars")
    return len(days)


def replay_rows(
    tape: Mapping[str, Sequence[TapeBar]],
    config: Optional[momentum_lifecycle.LifecycleConfig] = None,
    window: int = market_stream.DEFAULT_WINDOW,
) -> Tuple[Dict[str, Any], ...]:
    """Build ignition rows plus one 3x-versus-10x summary row."""
    current = config or momentum_lifecycle.LifecycleConfig()
    ignitions = detect_ignitions(tape, current, window)
    rows: List[Dict[str, Any]] = []
    by_symbol: Dict[str, List[Ignition]] = {}
    for ignition in ignitions:
        by_symbol.setdefault(ignition.symbol, []).append(ignition)
    for symbol in sorted(by_symbol):
        by_close = {bar.close_ms: bar for bar in tape[symbol]}
        rows.extend(
            _ignition_row(ignition, by_close)
            for ignition in by_symbol[symbol]
        )
    rows.sort(key=lambda row: (str(row["utc"]), str(row["symbol"])))
    days = _tape_day_count(tape)
    comparisons: Dict[str, Tuple[int, str]] = {}
    for ratio in COMPARISON_RATIOS:
        comparison = replace(current, ignition_surge_ratio=ratio)
        count = len(detect_ignitions(tape, comparison, window))
        with localcontext() as context:
            context.prec = 50
            per_day = Decimal(str(count)) / Decimal(str(days))
        comparisons[momentum_lifecycle.fmt(ratio)] = (
            count,
            momentum_lifecycle.fmt(per_day),
        )
    rows.append(
        {
            "kind": "summary",
            "comparison_basis": "ignition_surge_ratio",
            "volume_window_bars": window,
            "tape_days": days,
            "current_ignition_surge_ratio": momentum_lifecycle.fmt(
                current.ignition_surge_ratio
            ),
            "current_ignitions": len(ignitions),
            "ignitions_bar_3": comparisons["3"][0],
            "ignitions_per_day_bar_3": comparisons["3"][1],
            "ignitions_bar_10": comparisons["10"][0],
            "ignitions_per_day_bar_10": comparisons["10"][1],
        }
    )
    return tuple(rows)


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(
                    json.dumps(
                        dict(row),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as error:
        raise ReplayError(f"cannot write replay output {target}: {error}") from None


def default_output_path(run_date: Optional[date] = None) -> Path:
    resolved = run_date or datetime.now(timezone.utc).date()
    return DEFAULT_OUTPUT_DIR / f"replay-{resolved.isoformat()}.jsonl"


def run_replay(
    data_dir: Path = DEFAULT_DATA_DIR,
    output_path: Optional[Path] = None,
    config: Optional[momentum_lifecycle.LifecycleConfig] = None,
    window: int = market_stream.DEFAULT_WINDOW,
) -> Tuple[Dict[str, Any], ...]:
    tape = load_tape(Path(data_dir))
    rows = replay_rows(tape, config, window)
    _write_jsonl(output_path or default_output_path(), rows)
    return rows


def _run_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from None


def _window(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("window must be an integer") from None
    if value < 2 or value > market_stream.MAX_WINDOW:
        raise argparse.ArgumentTypeError(
            f"window must be in [2, {market_stream.MAX_WINDOW}]"
        )
    return value


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay the current momentum detector over Binance 1m zips."
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--date", type=_run_date)
    parser.add_argument(
        "--window", type=_window, default=market_stream.DEFAULT_WINDOW
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = argument_parser().parse_args(argv)
    run_date = args.date or datetime.now(timezone.utc).date()
    output_path = args.output_dir / f"replay-{run_date.isoformat()}.jsonl"
    try:
        rows = run_replay(args.data_dir, output_path, window=args.window)
    except ReplayError as error:
        print(f"replay_detector: {error}", file=sys.stderr)
        return 2
    print(
        "REPLAY | "
        f"ignitions={sum(row.get('kind') == 'ignition' for row in rows)} | "
        f"output={output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
