#!/usr/bin/env python3
"""Archive one local basis observation per perpetual symbol and stream pass.

The recorder reads only ``stream_state.json``.  It has no HTTP, credential,
order, or account path.  A complete observation preserves funding, the next
funding epoch, the stream's coherence-checked executable basis gap, open
interest, and mark as Decimal text.  Missing or invalid inputs produce an
explicit SKIPPED row; they are never converted to zero.

``basis_history.jsonl`` uses the decisions ledger's two-segment 4 MiB
rotation: the old current segment becomes ``.1`` and the complete incoming
pass is installed from a same-directory, fsynced temporary file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence


HISTORY_FILENAME = "basis_history.jsonl"
HISTORY_ROTATE_BYTES = 4 * 1024 * 1024
PERP_VENUES = frozenset(("perp", "both"))
VENUES = frozenset(("spot", "perp", "both"))
SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,40}$")
STATUS_RECORDED = "RECORDED"
STATUS_SKIPPED = "SKIPPED"
METRIC_FIELDS = (
    "funding_rate",
    "next_funding_ms",
    "basis_gap_pct",
    "oi",
    "mark",
)


class BasisRecorderError(Exception):
    """A local input-validation or durable-write failure."""


def _decimal_text(
    raw: Any, *, positive: bool = False, nonnegative: bool = False
) -> Optional[str]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    if positive and value <= 0:
        return None
    if nonnegative and value < 0:
        return None
    return format(value, "f")


def _positive_integer_text(raw: Any) -> Optional[str]:
    if not isinstance(raw, str) or not raw.isdigit():
        return None
    value = int(raw)
    if value <= 0:
        return None
    return str(value)


def build_row(symbol: str, record: Mapping[str, Any]) -> Dict[str, Any]:
    """Build one complete observation or an explicit fail-closed skip row."""
    if not isinstance(symbol, str) or SYMBOL_RE.fullmatch(symbol) is None:
        raise BasisRecorderError("stream state has an invalid symbol")
    if not isinstance(record, Mapping):
        raise BasisRecorderError(f"stream state record is invalid for {symbol}")
    venue = record.get("venues")
    if venue not in PERP_VENUES:
        raise BasisRecorderError(f"stream state record has no perpetual for {symbol}")

    raw_utc = record.get("utc")
    utc = raw_utc if isinstance(raw_utc, str) and raw_utc else None
    values: Dict[str, Optional[str]] = {
        "funding_rate": _decimal_text(record.get("funding_rate")),
        "next_funding_ms": _positive_integer_text(
            record.get("next_funding_ms")
        ),
        "basis_gap_pct": (
            _decimal_text(record.get("basis_gap_pct"))
            if venue == "both"
            else None
        ),
        "oi": _decimal_text(record.get("oi"), nonnegative=True),
        "mark": _decimal_text(record.get("mark"), positive=True),
    }
    unavailable = ([] if utc is not None else ["utc"]) + [
        field for field in METRIC_FIELDS if values[field] is None
    ]
    return {
        "utc": utc,
        "symbol": symbol,
        "status": STATUS_RECORDED if not unavailable else STATUS_SKIPPED,
        **values,
        "unavailable": unavailable,
    }


def build_rows(stream_state: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return symbol-sorted rows for every current perpetual universe member."""
    if not isinstance(stream_state, Mapping):
        raise BasisRecorderError("stream state must be a symbol mapping")
    symbols = list(stream_state)
    if any(
        not isinstance(symbol, str) or SYMBOL_RE.fullmatch(symbol) is None
        for symbol in symbols
    ):
        raise BasisRecorderError("stream state has an invalid symbol")
    rows: List[Dict[str, Any]] = []
    for raw_symbol in sorted(symbols):
        record = stream_state[raw_symbol]
        if not isinstance(record, Mapping):
            raise BasisRecorderError(
                f"stream state record is invalid for {raw_symbol}"
            )
        venue = record.get("venues")
        if venue not in VENUES:
            raise BasisRecorderError(
                f"stream state venue is invalid for {raw_symbol}"
            )
        if venue in PERP_VENUES:
            rows.append(build_row(raw_symbol, record))
    return rows


def load_stream_state(path: Path) -> Mapping[str, Any]:
    path = Path(path)
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(
                handle, parse_float=Decimal, parse_int=Decimal
            )
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise BasisRecorderError(f"stream state is unreadable: {path}") from None
    if not isinstance(payload, dict):
        raise BasisRecorderError("stream state must be a symbol mapping")
    return payload


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes, description: str) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError(f"{description} write made no progress")
        offset += written


def _stage_payload(path: Path, payload: bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload, "basis history")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        return temporary
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".1")


def _rotate_history(path: Path, incoming: bytes, rotate_bytes: int) -> bool:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    except OSError:
        raise BasisRecorderError(
            f"basis history ledger is unreadable: {path}"
        ) from None
    if size == 0 or size + len(incoming) <= rotate_bytes:
        return False
    try:
        current_payload = path.read_bytes()
    except OSError:
        raise BasisRecorderError(
            f"basis history ledger is unreadable: {path}"
        ) from None
    if not current_payload or len(current_payload) + len(incoming) <= rotate_bytes:
        return False

    current_temporary: Optional[Path] = None
    backup_temporary: Optional[Path] = None
    try:
        current_temporary = _stage_payload(path, incoming)
        backup = _backup_path(path)
        backup_temporary = _stage_payload(backup, current_payload)
        os.replace(backup_temporary, backup)
        backup_temporary = None
        _fsync_directory(path.parent)
        os.replace(current_temporary, path)
        current_temporary = None
        _fsync_directory(path.parent)
    except OSError:
        raise BasisRecorderError(
            f"basis history ledger rotation failed: {path}"
        ) from None
    finally:
        for temporary in (current_temporary, backup_temporary):
            if temporary is None:
                continue
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return True


def _encode_rows(rows: Sequence[Mapping[str, Any]]) -> bytes:
    encoded: List[bytes] = []
    try:
        for row in rows:
            if not isinstance(row, Mapping):
                raise BasisRecorderError("basis history row is not a mapping")
            encoded.append(
                (
                    json.dumps(
                        dict(row), sort_keys=True, separators=(",", ":")
                    )
                    + "\n"
                ).encode("utf-8")
            )
    except (TypeError, ValueError):
        raise BasisRecorderError("basis history row is not JSON-safe") from None
    return b"".join(encoded)


def append_history(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
    *,
    rotate_bytes: Optional[int] = None,
) -> int:
    """Durably append one complete pass, rotating before it when necessary."""
    threshold = HISTORY_ROTATE_BYTES if rotate_bytes is None else rotate_bytes
    if isinstance(threshold, bool) or not isinstance(threshold, int) \
            or threshold <= 0:
        raise BasisRecorderError("basis history rotation threshold must be positive")
    payload = _encode_rows(rows)
    if not payload:
        return 0
    if len(payload) > threshold:
        raise BasisRecorderError(
            "basis history pass exceeds the rotation threshold"
        )

    path = Path(path)
    try:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    except OSError:
        raise BasisRecorderError(
            f"basis history directory is unavailable: {path.parent}"
        ) from None
    if _rotate_history(path, payload, threshold):
        return len(rows)

    descriptor = -1
    try:
        descriptor = os.open(
            str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        _write_all(descriptor, payload, "basis history append")
        os.fsync(descriptor)
    except OSError:
        raise BasisRecorderError(
            f"basis history ledger append failed: {path}"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return len(rows)


def record_pass(
    stream_state_path: Path,
    receipt_dir: Path,
    *,
    rotate_bytes: Optional[int] = None,
) -> List[Dict[str, Any]]:
    state = load_stream_state(Path(stream_state_path))
    rows = build_rows(state)
    append_history(
        Path(receipt_dir) / HISTORY_FILENAME,
        rows,
        rotate_bytes=rotate_bytes,
    )
    return rows


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default_receipt_dir = Path(
        os.path.expanduser(os.environ.get("RECEIPT_DIR", "~/quant_receipts"))
    )
    parser.add_argument("--receipt-dir", type=Path, default=default_receipt_dir)
    parser.add_argument(
        "--stream-state",
        type=Path,
        help="local stream_state.json; defaults below --receipt-dir",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = argument_parser().parse_args(argv)
    stream_state = (
        args.stream_state
        if args.stream_state is not None
        else args.receipt_dir / "stream_state.json"
    )
    try:
        rows = record_pass(stream_state, args.receipt_dir)
    except BasisRecorderError as exc:
        print(f"BASIS-RECORDER | ERROR | {exc}", file=sys.stderr)
        return 2
    recorded = sum(row["status"] == STATUS_RECORDED for row in rows)
    print(
        "BASIS-RECORDER | rows=%d recorded=%d skipped=%d"
        % (len(rows), recorded, len(rows) - recorded)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
