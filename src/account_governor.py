#!/usr/bin/env python3
"""Durable account-wide entry authority and UTC-day loss budgets.

The two live strategies share one receipt root, so one ``O_EXCL`` lock in
that root is the account-wide entry slot.  Successful acquisitions and
realized results are append-only UTC-day ledger events.  No stale lock is
ever removed here; only its owner token may release it.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

import runtime_paths

try:
    from limits import (
        DAILY_LOSS_PLUS_FEES_USDT as DEFAULT_DAILY_LOSS_PLUS_FEES_USDT,
        MAX_ENTRIES_PER_UTC_DAY as DEFAULT_MAX_ENTRIES_PER_UTC_DAY,
    )
except ImportError:
    DEFAULT_MAX_ENTRIES_PER_UTC_DAY = 10
    DEFAULT_DAILY_LOSS_PLUS_FEES_USDT = Decimal("2.00")


ENTRY_LOCK_FILE = "account-governor.lock"
DAILY_LEDGER_PREFIX = "account-governor-"
DAILY_LEDGER_SUFFIX = ".jsonl"
BASIS_POSITION_FILE = "basis-position.json"
LOCK_STALE_AFTER = timedelta(hours=2)

_BASIS_STANDING_STATES = frozenset(
    {"OPENING", "OPEN", "ONE_LEG_EXPOSED", "CLOSING", "UNKNOWN"}
)
_MOMENTUM_STANDING_STATES = frozenset(
    {"OPENING", "OPEN", "CLOSING", "UNKNOWN"}
)


class GovernorRefusal(Exception):
    """A factual local condition forbids an account entry."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(raw: Any) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise ValueError("UTC is missing")
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise ValueError("UTC has no timezone")
    return value.astimezone(timezone.utc)


def _runtime(receipt_dir: Any) -> runtime_paths.RuntimePaths:
    try:
        raw = os.fspath(receipt_dir)
    except TypeError:
        raise GovernorRefusal("receipt_dir must be one filesystem path") from None
    if not isinstance(raw, str):
        raise GovernorRefusal("receipt_dir must be one filesystem path")
    try:
        return runtime_paths.resolve_runtime_paths({"RECEIPT_DIR": raw})
    except ValueError as error:
        raise GovernorRefusal(str(error)) from None


def _lock_path(paths: runtime_paths.RuntimePaths) -> Path:
    return paths.receipt_dir / ENTRY_LOCK_FILE


def _ledger_path(paths: runtime_paths.RuntimePaths, utc_day: date) -> Path:
    return paths.receipt_dir / (
        DAILY_LEDGER_PREFIX + utc_day.isoformat() + DAILY_LEDGER_SUFFIX
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, encoded: bytes) -> None:
    offset = 0
    while offset < len(encoded):
        written = os.write(descriptor, encoded[offset:])
        if written <= 0:
            raise OSError("governor write made no progress")
        offset += written


def _identity(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value \
            or any(character in value for character in "\x00\r\n"):
        raise GovernorRefusal(f"{name} must be one non-empty text value")
    return value


def _money(value: Any, name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise GovernorRefusal(f"{name} must be a finite Decimal")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise GovernorRefusal(f"{name} must be a finite Decimal") from None
    if not amount.is_finite():
        raise GovernorRefusal(f"{name} must be a finite Decimal")
    return amount


def _read_lock(path: Path) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise GovernorRefusal(
            "account entry lock exists but its owner is unreadable"
        ) from None
    if not isinstance(payload, dict) \
            or type(payload.get("version")) is not int \
            or payload["version"] != 1:
        raise GovernorRefusal("account entry lock owner is invalid")
    for name in ("token", "op", "symbol", "utc"):
        if not isinstance(payload.get(name), str) or not payload[name]:
            raise GovernorRefusal("account entry lock owner is invalid")
    try:
        _parse_utc(payload["utc"])
    except (TypeError, ValueError):
        raise GovernorRefusal("account entry lock owner UTC is invalid") from None
    return payload


def _create_lock(path: Path, payload: Dict[str, Any]) -> None:
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(str(path), flags, 0o600)
    except FileExistsError:
        try:
            owner = _read_lock(path)
            reason = (
                "account entry slot is held by "
                f"{owner['op']} {owner['symbol']} since {owner['utc']}"
            )
        except FileNotFoundError:
            reason = "account entry slot was concurrently held"
        except GovernorRefusal as error:
            reason = error.reason
        raise GovernorRefusal(reason) from None
    try:
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        raise
    else:
        os.close(descriptor)
    try:
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        raise


def _release_owned_lock(token: str, paths: runtime_paths.RuntimePaths) -> None:
    path = _lock_path(paths)
    try:
        owner = _read_lock(path)
    except FileNotFoundError:
        return
    if owner["token"] != token:
        raise GovernorRefusal("account entry lock belongs to another token")
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def _append_event(
    paths: runtime_paths.RuntimePaths, now: datetime, event: Dict[str, Any]
) -> None:
    path = _ledger_path(paths, now.date())
    encoded = (
        json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(str(path), flags, 0o600)
    try:
        written = os.write(descriptor, encoded)
        if written != len(encoded):
            raise OSError("short account-governor ledger append")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _ledger_rows(path: Path) -> Iterable[Dict[str, Any]]:
    try:
        with open(path, "rb") as handle:
            for encoded in handle:
                if not encoded.endswith(b"\n"):
                    raise GovernorRefusal(
                        "daily account-governor ledger has an incomplete row"
                    )
                try:
                    row = json.loads(encoded)
                except (UnicodeError, ValueError, json.JSONDecodeError):
                    raise GovernorRefusal(
                        "daily account-governor ledger is unreadable"
                    ) from None
                if not isinstance(row, dict) \
                        or type(row.get("version")) is not int \
                        or row["version"] != 1:
                    raise GovernorRefusal(
                        "daily account-governor ledger row is invalid"
                    )
                yield row
    except FileNotFoundError:
        return
    except OSError:
        raise GovernorRefusal(
            "daily account-governor ledger is unreadable"
        ) from None


def _standing_position_reference(
    paths: runtime_paths.RuntimePaths,
) -> Tuple[bool, str]:
    references = (
        (paths.receipt_dir / BASIS_POSITION_FILE, _BASIS_STANDING_STATES),
        (paths.momentum_position, _MOMENTUM_STANDING_STATES),
    )
    for path, standing_states in references:
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return False, f"{path.name} exists but is unreadable"
        if not isinstance(payload, dict) or not isinstance(
            payload.get("state"), str
        ):
            return False, f"{path.name} exists but its state is invalid"
        if payload["state"] in standing_states:
            return True, path.name
    return False, ""


def _stale_lock_reason(
    paths: runtime_paths.RuntimePaths, now: datetime
) -> str:
    path = _lock_path(paths)
    try:
        owner = _read_lock(path)
    except FileNotFoundError:
        return ""
    except GovernorRefusal as error:
        return error.reason
    locked_at = _parse_utc(owner["utc"])
    age = now - locked_at
    if age < timedelta(0):
        return "account entry lock owner UTC is in the future"
    if age <= LOCK_STALE_AFTER:
        return ""
    standing, detail = _standing_position_reference(paths)
    if standing:
        return ""
    if detail:
        return detail
    return (
        "stale account entry lock has no standing position reference; "
        "human or reconcile must clear it"
    )


def _budget_status(
    paths: runtime_paths.RuntimePaths, now: datetime
) -> Tuple[bool, str]:
    stale_reason = _stale_lock_reason(paths, now)
    if stale_reason:
        return False, stale_reason

    utc_day = now.date().isoformat()
    entries = 0
    cumulative_net = Decimal("0")
    cumulative_fees = Decimal("0")
    loss_limit_breached = False
    try:
        rows = _ledger_rows(_ledger_path(paths, now.date()))
        for row in rows:
            if row.get("utc_day") != utc_day:
                raise GovernorRefusal(
                    "daily account-governor ledger row has the wrong UTC day"
                )
            try:
                row_utc = _parse_utc(row.get("utc"))
            except (TypeError, ValueError):
                raise GovernorRefusal(
                    "daily account-governor ledger row UTC is invalid"
                ) from None
            if row_utc.date() != now.date():
                raise GovernorRefusal(
                    "daily account-governor ledger row UTC is outside its day"
                )
            if row.get("kind") == "entry":
                if not all(
                    isinstance(row.get(name), str) and row[name]
                    for name in ("op", "symbol", "token")
                ):
                    raise GovernorRefusal(
                        "daily account-governor entry row is invalid"
                    )
                entries += 1
            elif row.get("kind") == "realized":
                net = _money(row.get("net_usdt"), "ledger net_usdt")
                fees = _money(row.get("fees_usdt"), "ledger fees_usdt")
                if fees < 0:
                    raise GovernorRefusal(
                        "daily account-governor ledger fees are negative"
                    )
                cumulative_net += net
                cumulative_fees += fees
                used = max(-cumulative_net, Decimal("0")) + cumulative_fees
                if used >= DEFAULT_DAILY_LOSS_PLUS_FEES_USDT:
                    loss_limit_breached = True
            else:
                raise GovernorRefusal(
                    "daily account-governor ledger event kind is invalid"
                )
    except GovernorRefusal as error:
        return False, error.reason

    if entries >= DEFAULT_MAX_ENTRIES_PER_UTC_DAY:
        return False, (
            f"daily entry limit reached: {entries} of "
            f"{DEFAULT_MAX_ENTRIES_PER_UTC_DAY} entries used for {utc_day} UTC"
        )
    if loss_limit_breached:
        return False, (
            "daily realized-loss-plus-fees limit reached: "
            f"{format(DEFAULT_DAILY_LOSS_PLUS_FEES_USDT, 'f')} USDT"
        )
    return True, "daily budgets allow entry"


def acquire_entry_slot(op: str, symbol: str, receipt_dir: Any) -> str:
    """Acquire the sole account entry slot and consume one UTC-day entry."""
    op = _identity(op, "op")
    symbol = _identity(symbol, "symbol")
    paths = _runtime(receipt_dir)
    paths.receipt_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    now = _utc_now()
    token = secrets.token_hex(16)
    owner = {
        "version": 1,
        "token": token,
        "op": op,
        "symbol": symbol,
        "utc": _utc_text(now),
    }
    _create_lock(_lock_path(paths), owner)
    try:
        allowed, reason = _budget_status(paths, now)
        if not allowed:
            raise GovernorRefusal(reason)
        _append_event(
            paths,
            now,
            {
                "version": 1,
                "kind": "entry",
                "utc": _utc_text(now),
                "utc_day": now.date().isoformat(),
                "op": op,
                "symbol": symbol,
                "token": token,
            },
        )
    except BaseException:
        _release_owned_lock(token, paths)
        raise
    return token


def release_entry_slot(token: str, receipt_dir: Any) -> None:
    """Release only the caller's slot; repeating a completed release is safe."""
    token = _identity(token, "token")
    _release_owned_lock(token, _runtime(receipt_dir))


def record_realized(net_usdt: Any, fees_usdt: Any, receipt_dir: Any) -> None:
    """Append one realized result to today's durable account ledger."""
    net = _money(net_usdt, "net_usdt")
    fees = _money(fees_usdt, "fees_usdt")
    if fees < 0:
        raise GovernorRefusal("fees_usdt must be non-negative")
    paths = _runtime(receipt_dir)
    paths.receipt_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    now = _utc_now()
    _append_event(
        paths,
        now,
        {
            "version": 1,
            "kind": "realized",
            "utc": _utc_text(now),
            "utc_day": now.date().isoformat(),
            "net_usdt": format(net, "f"),
            "fees_usdt": format(fees, "f"),
        },
    )


def budgets_allow(receipt_dir: Any) -> Tuple[bool, str]:
    """Return today's budget verdict, including any stale-lock blocker."""
    paths = _runtime(receipt_dir)
    return _budget_status(paths, _utc_now())
