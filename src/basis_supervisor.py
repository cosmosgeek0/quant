#!/usr/bin/env python3
"""Stateless tick driver for the persistent basis-position machine.

The supervisor owns ordering, not trading mechanics.  Every lifecycle action
is delegated to ``basis_position`` and every tick reloads its durable state.
The only durable artifact created here is an exclusive proposal claim.

Exit codes: 0 acted, 1 held, 2 frozen, 3 idle.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Callable, Dict, List, Optional

import basis_position
from basis_position import (
    EMERGENCY_FILE,
    EXIT_CODE as POSITION_EXIT_CODE,
    FEE_BUFFER,
    FREEZE_NEW_ACTIONS,
    LOCK_FILE,
    MARGIN_QUANTUM,
    PROPOSAL_DIR,
    PROPOSAL_ID_RE,
    REQUEST_EMERGENCY_UNWIND,
    REQUEST_EXIT,
    STATE_FILE,
    STOP_FILE,
    ST_CLOSED,
    ST_CLOSING,
    ST_NONE,
    ST_ONE_LEG,
    ST_OPEN,
    ST_OPENING,
    ST_UNKNOWN,
    SYMBOL_RE,
    PositionConfig,
    PositionError,
    cmd_check,
    cmd_close,
    cmd_open,
    cmd_resume,
    config_from_env,
    default_now_ms,
    live_request_factory,
    load_entry_proposal,
    load_position,
    supervise,
)
from job_runner import (
    FIRST_AUTONOMOUS_CLOSE_MARKER,
    JOB_ID_RE,
    Job,
    Outcome as RunnerOutcome,
    _acquire_governor,
    _consume_accept_one,
    _entry_outcome_succeeded,
    _entry_outcome_terminal,
    _entry_ticket_refusal,
    _load_operator,
    _paths,
    _release_governor,
    _release_recovered_governor,
    _touch_stop,
)

try:
    from limits import FIRST_TRADE_CAP_USDT
except ImportError:
    # TODO: remove this fallback after the sibling limits.py lane merges.
    FIRST_TRADE_CAP_USDT = Decimal("8")


EXIT_ACTED = 0
EXIT_HELD = 1
EXIT_FROZEN = 2
EXIT_IDLE = 3

FREEZE_STATES = frozenset((ST_UNKNOWN, ST_ONE_LEG, ST_OPENING, ST_CLOSING))
CLOSE_DECISIONS = frozenset((REQUEST_EXIT, REQUEST_EMERGENCY_UNWIND))
PROPOSAL_NAME_RE = re.compile(r"proposal-(.+)\.json")
HEALTH_FILE_NAME = "health.jsonl"
WATCH_STATE_PATH = os.path.join("watch", "basis_watch_state.json")
ACCEPT_ONE_FILE = "ACCEPT-ONE"
RUNNER_STOP_FILE = "STOP-JOBS"
POSITION_STATES = (ST_NONE, ST_OPENING, ST_ONE_LEG, ST_OPEN, ST_CLOSING,
                   ST_CLOSED, ST_UNKNOWN)
_ENTRY_OPEN_LOCK = threading.Lock()

# Public identity for callers which need the complete pure ladder.  Runtime
# market checks stay in cmd_check: the supervisor must not reproduce its
# executable-price/funding reads or its decision rules.
supervision_ladder = supervise


@dataclass(frozen=True)
class ProposalCandidate:
    proposal_id: str
    symbol: str
    created_ms: int
    expires_ms: int
    net_edge_pct: Decimal
    requested_notional: Decimal
    digest: str


def _text(value: Any, limit: int = 300) -> str:
    """One safe factual row, without multiline transport/error text."""
    return " ".join(str(value).split())[:limit]


def _row(action: str, outcome: str, note: Any) -> None:
    print("SUPERVISOR | %s | %s | %s"
          % (_text(action, 40), _text(outcome, 40), _text(note)))


def _utc_from_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _count_reject(histogram: Dict[str, int], reason: str) -> None:
    histogram[reason] = histogram.get(reason, 0) + 1


def _append_health_jsonl(receipt_dir: str,
                         payload: Dict[str, Any]) -> bool:
    """Append one compact UI telemetry record without gating supervision."""
    path = os.path.join(receipt_dir, HEALTH_FILE_NAME)
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":"))
               + "\n").encode("utf-8")
    descriptor: Optional[int] = None
    try:
        os.makedirs(receipt_dir, mode=0o700, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags, 0o600)
        if os.write(descriptor, encoded) != len(encoded):
            raise OSError("short health append")
        os.fsync(descriptor)
        return True
    except OSError:
        return False
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _watch_last_pass_ms(receipt_dir: str) -> Optional[int]:
    path = os.path.join(receipt_dir, WATCH_STATE_PATH)
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        value = payload["watermark"]["last_pass_ms"]
    except (OSError, KeyError, TypeError, ValueError, UnicodeError):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _append_tick_health(cfg: PositionConfig, now_ms: int,
                        code: Optional[int], error: Optional[str],
                        rejects: Dict[str, int]) -> bool:
    try:
        position = load_position(cfg.receipt_dir)
    except PositionError:
        position = {"state": ST_UNKNOWN}
    state = position.get("state") if isinstance(position, dict) else ST_NONE
    if state not in POSITION_STATES:
        state = ST_UNKNOWN
    state_counts = {name: 0 for name in POSITION_STATES}
    state_counts[state] = 1
    last_pass_ms = _watch_last_pass_ms(cfg.receipt_dir)
    age = None
    if last_pass_ms is not None and now_ms >= last_pass_ms:
        age = now_ms - last_pass_ms
    if error is not None or code is None or code == EXIT_FROZEN:
        status = "ERROR"
    elif rejects:
        status = "DEGRADED"
    elif code == EXIT_IDLE:
        status = "IDLE"
    else:
        status = "OK"
    payload: Dict[str, Any] = {
        "component": "basis_supervisor",
        "cycle": "tick",
        "exit_code": code,
        "last_pass_age_ms": age,
        "last_pass_ms": last_pass_ms,
        "reject_histogram": dict(sorted(rejects.items())),
        "state_counts": state_counts,
        "status": status,
        "ts": _utc_from_ms(now_ms),
    }
    if error is not None:
        payload["error"] = error
    return _append_health_jsonl(cfg.receipt_dir, payload)


def _proposal_ms(payload: dict, field: str) -> int:
    text = payload[field]
    parsed = datetime.fromisoformat(
        text[:-1] + "+00:00" if text.endswith("Z") else text)
    return int(parsed.timestamp() * 1000)


def _positive_decimal(value: Any, label: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise PositionError("proposal %s is not a positive decimal" % label)
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise PositionError(
            "proposal %s is not a positive decimal" % label) from None
    if not parsed.is_finite() or parsed <= 0:
        raise PositionError("proposal %s is not a positive decimal" % label)
    return parsed


def _accepted_notionals(accepted: Any) -> tuple[Decimal, Decimal]:
    """Return the exact proposal-plan notionals without guessing missing data."""
    payload = accepted.payload
    quantity = _positive_decimal(accepted.quantity, "quantity")
    spot_raw = payload.get("spot_quote_cap")
    if spot_raw is None:
        spot_ask = _positive_decimal(payload.get("spot_ask"), "spot_ask")
        spot_notional = (
            quantity * spot_ask * (Decimal(1) + FEE_BUFFER)
        ).quantize(MARGIN_QUANTUM)
    else:
        spot_notional = _positive_decimal(spot_raw, "spot_quote_cap")
    short_raw = payload.get("short_notional")
    if short_raw is None:
        perp_bid = _positive_decimal(payload.get("perp_bid"), "perp_bid")
        short_notional = (quantity * perp_bid).quantize(MARGIN_QUANTUM)
    else:
        short_notional = _positive_decimal(short_raw, "short_notional")
    if spot_notional <= 0 or short_notional <= 0:
        raise PositionError("proposal execution notionals must be positive")
    return spot_notional, short_notional


def _clamp_accepted_proposal(accepted: Any, cap: Decimal) -> Any:
    """Derive a capped execution view while preserving source bytes/digest."""
    if not isinstance(cap, Decimal) or not cap.is_finite() or cap <= 0:
        raise PositionError("first-trade notional cap is invalid")
    spot_notional, short_notional = _accepted_notionals(accepted)
    largest = max(spot_notional, short_notional)
    if largest <= cap:
        return accepted
    scale = cap / largest
    capped_spot = (spot_notional * scale).quantize(
        MARGIN_QUANTUM, rounding=ROUND_DOWN)
    capped_short = (short_notional * scale).quantize(
        MARGIN_QUANTUM, rounding=ROUND_DOWN)
    if capped_spot <= 0 or capped_short <= 0:
        raise PositionError("first-trade notional cap is below executable size")
    payload = dict(accepted.payload)
    payload["spot_quote_cap"] = format(capped_spot, "f")
    payload["short_notional"] = format(capped_short, "f")
    return dataclasses.replace(accepted, payload=payload)


def _entry_notional_cap(receipt_dir: str) -> Optional[Decimal]:
    marker = os.path.join(receipt_dir, FIRST_AUTONOMOUS_CLOSE_MARKER)
    return None if os.path.isfile(marker) else FIRST_TRADE_CAP_USDT


def _call_capped_open(cfg: PositionConfig, request, sleep, now_ms,
                      proposal_id: str,
                      cap: Optional[Decimal]) -> int:
    """Call cmd_open with a per-call accepted-proposal view capped in memory."""
    if cap is None:
        return cmd_open(
            cfg, request, sleep=sleep, now_ms=now_ms,
            proposal_id=proposal_id)
    # cmd_open has no notional argument.  Serialize the narrow dependency swap
    # so its loader sees a capped copy; the immutable proposal and digest stay
    # unchanged, and the original loader is restored on every outcome.
    with _ENTRY_OPEN_LOCK:
        original_loader = basis_position.load_entry_proposal

        def capped_loader(*args, **kwargs):
            accepted = original_loader(*args, **kwargs)
            return _clamp_accepted_proposal(accepted, cap)

        basis_position.load_entry_proposal = capped_loader
        try:
            return cmd_open(
                cfg, request, sleep=sleep, now_ms=now_ms,
                proposal_id=proposal_id)
        finally:
            basis_position.load_entry_proposal = original_loader


def _basis_entry_job(candidate: ProposalCandidate, requested_by: str,
                     cap: Optional[Decimal]) -> Job:
    requested = candidate.requested_notional
    effective = min(requested, cap) if cap is not None else requested
    job_id = _basis_entry_job_id(
        candidate.proposal_id, candidate.digest)
    return Job(
        job_id=job_id,
        op="BASIS_ROUNDTRIP",
        symbol=candidate.symbol,
        requested_max_notional=requested,
        effective_notional=effective,
        created_utc=datetime.fromtimestamp(
            candidate.created_ms / 1000, timezone.utc),
        expires_utc=datetime.fromtimestamp(
            candidate.expires_ms / 1000, timezone.utc),
        requested_by=requested_by,
    )


def _basis_entry_job_id(proposal_id: str, digest: str) -> str:
    if JOB_ID_RE.fullmatch(proposal_id) is not None:
        return proposal_id
    return "basis-" + digest[:58]


def _runner_entry_outcome(open_code: int, opened: Optional[dict],
                          cfg: PositionConfig) -> Optional[RunnerOutcome]:
    """Translate position truth to the runner's shared terminal vocabulary."""
    if opened is not None:
        state = opened.get("state")
        if state == ST_OPEN:
            verdict = "OPEN"
        elif state == ST_ONE_LEG:
            verdict = "OPEN_EXPOSURE"
        elif state in (ST_OPENING, ST_CLOSING, ST_UNKNOWN):
            verdict = "UNKNOWN"
        elif state in (ST_NONE, ST_CLOSED):
            verdict = "REFUSED"
        else:
            return None
        return RunnerOutcome(verdict, open_code, "")
    if _position_file_exists(cfg):
        return None
    if open_code in {
            POSITION_EXIT_CODE["REFUSED"],
            POSITION_EXIT_CODE["BLOCKED"],
            POSITION_EXIT_CODE["PREFLIGHT_REFUSED"]}:
        return RunnerOutcome("REFUSED", open_code, "")
    return None


def _load_candidate(cfg: PositionConfig, proposal_id: str,
                    tick_ms: int) -> ProposalCandidate:
    """Validate one proposal using the symbol declared by that proposal."""
    path = os.path.join(cfg.receipt_dir, PROPOSAL_DIR,
                        "proposal-%s.json" % proposal_id)
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, ValueError):
        raise PositionError("proposal %s is unreadable" % proposal_id) \
            from None
    if not isinstance(payload, dict):
        raise PositionError(
            "proposal %s is unreadable: JSON object required" % proposal_id)
    symbol = payload.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        raise PositionError("proposal required field symbol is missing")
    if SYMBOL_RE.fullmatch(symbol) is None:
        raise PositionError("proposal symbol is invalid")
    accepted = load_entry_proposal(
        cfg.receipt_dir, proposal_id, symbol, cfg.sha, tick_ms)
    spot_notional, short_notional = _accepted_notionals(accepted)
    return ProposalCandidate(
        proposal_id=proposal_id,
        symbol=accepted.payload["symbol"],
        created_ms=_proposal_ms(accepted.payload, "created_utc"),
        expires_ms=_proposal_ms(accepted.payload, "expires_utc"),
        net_edge_pct=accepted.net_edge_pct,
        requested_notional=max(spot_notional, short_notional),
        digest=accepted.digest)


def _scan_newest_valid(cfg: PositionConfig, tick_ms: int,
                       rejects: Optional[Dict[str, int]] = None
                       ) -> Optional[ProposalCandidate]:
    """Validate all proposals; select best edge, then newest, in whitelist."""
    directory = os.path.join(cfg.receipt_dir, PROPOSAL_DIR)
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return None
    except OSError as error:
        _row("SCAN", "FROZEN", "proposal directory unreadable: %s"
             % type(error).__name__)
        raise

    valid: List[ProposalCandidate] = []
    for name in names:
        match = PROPOSAL_NAME_RE.fullmatch(name)
        if match is None:
            continue
        proposal_id = match.group(1)
        if PROPOSAL_ID_RE.fullmatch(proposal_id) is None:
            if rejects is not None:
                _count_reject(rejects, "proposal_id_invalid")
            _row("PROPOSAL", "SKIPPED", "%s has an invalid id" % name)
            continue
        try:
            candidate = _load_candidate(cfg, proposal_id, tick_ms)
            if cfg.symbol and candidate.symbol != cfg.symbol:
                if rejects is not None:
                    _count_reject(rejects, "proposal_symbol_filtered")
                _row("PROPOSAL", "FILTERED", "%s symbol %s"
                     % (proposal_id, candidate.symbol))
                continue
            valid.append(candidate)
        except (KeyError, TypeError, ValueError, PositionError) as error:
            if rejects is not None:
                reason = "proposal_expired" if "expired" in str(error) \
                    else "proposal_invalid"
                _count_reject(rejects, reason)
            _row("PROPOSAL", "SKIPPED", "%s: %s"
                 % (proposal_id, _text(error, 220)))
    if not valid:
        return None
    return max(valid, key=lambda item: (
        item.net_edge_pct, item.created_ms, item.proposal_id))


def _claim(candidate: ProposalCandidate, cfg: PositionConfig, claimant: str,
           claimed_ms: int) -> bool:
    """Create the proposal claim exactly once; an existing inode owns it."""
    path = os.path.join(cfg.receipt_dir, PROPOSAL_DIR,
                        "proposal-%s.claim" % candidate.proposal_id)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    payload = (json.dumps({
        "claimant": _text(claimant, 160),
        "utc": _utc_from_ms(claimed_ms),
    }, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        # The claim is durable before cmd_open starts.  A process can die in
        # that exact gap, leaving no position, lane lock, or position-command
        # receipt to resume.  The same supervisor identity may adopt only
        # that provably unstarted claim; every attempted/foreign claim remains
        # immutable contention and is never replayed.
        try:
            with open(path, encoding="utf-8") as handle:
                prior_claim = json.load(handle)
        except (OSError, ValueError, UnicodeError):
            prior_claim = None
        attempted = False
        try:
            names = os.listdir(cfg.receipt_dir)
        except OSError:
            names = []
            attempted = True
        for name in names:
            if not (name.startswith("basis-position-")
                    and name.endswith(".json")):
                continue
            try:
                with open(os.path.join(cfg.receipt_dir, name),
                          encoding="utf-8") as handle:
                    receipt = json.load(handle)
            except (OSError, ValueError, UnicodeError):
                attempted = True
                break
            if isinstance(receipt, dict) and receipt.get("proposal_id") \
                    == candidate.proposal_id:
                attempted = True
                break
        try:
            position = load_position(cfg.receipt_dir)
        except PositionError:
            position = None
            attempted = True
        if isinstance(position, dict) and position.get("proposal_id") \
                == candidate.proposal_id:
            attempted = True
        same_unstarted_owner = isinstance(prior_claim, dict) \
            and prior_claim.get("claimant") == _text(claimant, 160) \
            and isinstance(prior_claim.get("utc"), str) \
            and not attempted \
            and not os.path.exists(os.path.join(cfg.receipt_dir, LOCK_FILE))
        if same_unstarted_owner:
            _row("CLAIM", "RECOVERED", "proposal %s claimant %s"
                 % (candidate.proposal_id, _text(claimant, 80)))
            return True
        _row("CLAIM", "SKIPPED", "proposal %s already owned"
             % candidate.proposal_id)
        return False
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("claim write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_descriptor = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    _row("CLAIM", "CREATED", "proposal %s claimant %s"
         % (candidate.proposal_id, _text(claimant, 80)))
    return True


def _claim_governor_token(cfg: PositionConfig,
                          proposal_id: str) -> str:
    if not isinstance(proposal_id, str) \
            or PROPOSAL_ID_RE.fullmatch(proposal_id) is None:
        return ""
    path = os.path.join(cfg.receipt_dir, PROPOSAL_DIR,
                        "proposal-%s.claim" % proposal_id)
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, TypeError, ValueError, UnicodeError):
        return ""
    token = payload.get("governor_token") \
        if isinstance(payload, dict) else None
    return token if isinstance(token, str) else ""


def _write_claim_governor_token(cfg: PositionConfig, proposal_id: str,
                                claimant: Optional[str], token: str) -> bool:
    """Persist or clear the recovery token inside the owned proposal claim."""
    if not isinstance(proposal_id, str) \
            or PROPOSAL_ID_RE.fullmatch(proposal_id) is None:
        return False
    path = os.path.join(cfg.receipt_dir, PROPOSAL_DIR,
                        "proposal-%s.claim" % proposal_id)
    temporary = ""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict) or (
                claimant is not None
                and payload.get("claimant") != _text(claimant, 160)):
            return False
        if token:
            payload["governor_token"] = token
        else:
            payload.pop("governor_token", None)
        encoded = (json.dumps(
            payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        temporary = "%s.tmp-governor-%s-%s" % (
            path, os.getpid(), threading.get_ident())
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise OSError("claim write made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True
    except (OSError, TypeError, ValueError, UnicodeError):
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        return False


def _load_after(action: str, cfg: PositionConfig) -> Optional[dict]:
    try:
        return load_position(cfg.receipt_dir)
    except PositionError as error:
        _row(action, "FROZEN", error)
        return None


def _position_file_exists(cfg: PositionConfig) -> bool:
    return os.path.exists(os.path.join(cfg.receipt_dir, STATE_FILE))


def _cfg_for_position(cfg: PositionConfig,
                      position: Optional[dict]) -> PositionConfig:
    """Bind commands to durable position identity, never the entry filter."""
    if position is None:
        return dataclasses.replace(cfg, symbol="")
    symbol = position.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        raise PositionError("basis-position.json symbol is missing or invalid")
    return dataclasses.replace(cfg, symbol=symbol)


def _reconcile(cfg: PositionConfig, request, sleep, now_ms,
               label: str = "RECONCILE") -> tuple[int, Optional[dict]]:
    code = cmd_resume(cfg, request, sleep=sleep, now_ms=now_ms)
    position = _load_after(label, cfg)
    state = position.get("state") if position else ST_NONE
    _row(label, "DONE" if code == 0 else "HELD",
         "position command exit %s state %s" % (code, state))
    return code, position


def _fresh_check_decision(before: dict, after: dict,
                          check_code: int) -> Optional[str]:
    if check_code != POSITION_EXIT_CODE["DECIDED"]:
        return None
    old_revision = before.get("revision")
    new_revision = after.get("revision")
    changed = isinstance(old_revision, int) and isinstance(new_revision, int) \
        and new_revision > old_revision
    if not changed and after.get("last_check") == before.get("last_check"):
        return None
    last_check = after.get("last_check")
    if not isinstance(last_check, dict):
        return None
    decision = last_check.get("decision")
    return decision if isinstance(decision, str) else None


def _default_claimant() -> str:
    return "pid-%s" % os.getpid()


def tick(cfg: PositionConfig, request, *, sleep: Callable[[float], None] = time.sleep,
         now_ms: Callable[[], int] = default_now_ms,
         claimant: Optional[str] = None) -> int:
    """Run one cycle and append one non-authoritative health snapshot."""
    observed_times: List[int] = []
    rejects: Dict[str, int] = {}
    code: Optional[int] = None
    error_name: Optional[str] = None

    def tracked_now_ms() -> int:
        value = now_ms()
        observed_times.append(value)
        return value

    try:
        code = _tick(cfg, request, sleep=sleep, now_ms=tracked_now_ms,
                     claimant=claimant, rejects=rejects)
        return code
    except Exception as error:
        error_name = type(error).__name__
        _count_reject(rejects, "tick_error")
        raise
    finally:
        if code == EXIT_FROZEN and not rejects:
            _count_reject(rejects, "tick_frozen")
        if observed_times:
            health_now = observed_times[-1]
        else:
            try:
                health_now = now_ms()
            except Exception:
                health_now = default_now_ms()
        try:
            _append_tick_health(cfg, health_now, code, error_name, rejects)
        except Exception:
            pass


def _tick(cfg: PositionConfig, request, *,
          sleep: Callable[[float], None] = time.sleep,
          now_ms: Callable[[], int] = default_now_ms,
          claimant: Optional[str] = None,
          rejects: Optional[Dict[str, int]] = None) -> int:
    """Run exactly one restart-safe supervisor cycle."""
    rejects = rejects if rejects is not None else {}
    operator = _load_operator(_paths(cfg.receipt_dir)["operator"])
    claimant = claimant or _default_claimant()

    # Reconcile before every decision whenever position state (or its shared
    # ownership lock) exists.  A corrupt state also goes through cmd_resume so
    # the position machinery, not this driver, owns the refusal.
    position: Optional[dict]
    try:
        position = load_position(cfg.receipt_dir)
    except PositionError as error:
        _row("TICK", "FROZEN", error)
        return EXIT_FROZEN
    has_recovery_artifact = _position_file_exists(cfg) or os.path.exists(
        os.path.join(cfg.receipt_dir, LOCK_FILE))
    if has_recovery_artifact:
        try:
            position_cfg = _cfg_for_position(cfg, position)
        except PositionError as error:
            _row("TICK", "FROZEN", error)
            return EXIT_FROZEN
        resume_code, position = _reconcile(
            position_cfg, request, sleep, now_ms)
        state = position.get("state") if position else None
        lock_stands = os.path.exists(os.path.join(cfg.receipt_dir, LOCK_FILE))
        ownership_inconsistent = (state == ST_OPEN and not lock_stands) \
            or (state in (ST_NONE, ST_CLOSED) and lock_stands)
        proposal_id = position.get("proposal_id") if position else None
        recovered_token = _claim_governor_token(cfg, proposal_id) \
            if isinstance(proposal_id, str) else ""
        acceptance_claimed = True
        if recovered_token:
            entry = position.get("entry") if position else None
            consumes_ticket = state == ST_ONE_LEG or (
                state in (ST_OPEN, ST_CLOSED) and isinstance(entry, dict))
            if consumes_ticket:
                digest = position.get("proposal_digest")
                if not isinstance(digest, str):
                    acceptance_claimed = False
                else:
                    job_id = _basis_entry_job_id(proposal_id, digest)
                    accept_path = os.path.join(
                        cfg.receipt_dir, ACCEPT_ONE_FILE)
                    claimed_path = accept_path + ".claimed-" + job_id
                    already_claimed = os.path.isfile(claimed_path) \
                        and not os.path.islink(claimed_path)
                    acceptance_claimed = already_claimed \
                        or _consume_accept_one(accept_path, job_id)
                if not acceptance_claimed:
                    _touch_stop(os.path.join(
                        cfg.receipt_dir, RUNNER_STOP_FILE))
                    _count_reject(rejects, "accept_one_claim_failed")
                    _row("ENTRY", "FROZEN", "ACCEPT_ONE_CLAIM_FAILED")
        if resume_code != 0 or state in FREEZE_STATES or position is None:
            _row("TICK", "FROZEN",
                 "reconciliation is the only action; state %s"
                 % (state or "UNREADABLE"))
            return EXIT_FROZEN
        if ownership_inconsistent:
            _row("TICK", "FROZEN",
                 "reconciliation is the only action; state/lock ownership "
                 "is inconsistent")
            return EXIT_FROZEN
        if recovered_token:
            if acceptance_claimed and _release_recovered_governor(
                    recovered_token, cfg.receipt_dir):
                if not _write_claim_governor_token(
                        cfg, proposal_id, None, ""):
                    _touch_stop(os.path.join(
                        cfg.receipt_dir, RUNNER_STOP_FILE))
                    _count_reject(rejects, "governor_token_clear_failed")
                    _row("GOVERNOR", "HELD",
                         "recovered release cleared account lock but not "
                         "claim token; STOP-JOBS written")
            elif acceptance_claimed:
                _touch_stop(os.path.join(cfg.receipt_dir, RUNNER_STOP_FILE))
                _count_reject(rejects, "governor_release_failed")
                _row("GOVERNOR", "HELD",
                     "recovered release failed; STOP-JOBS written")

    if position is not None and position.get("state") == ST_OPEN:
        try:
            position_cfg = _cfg_for_position(cfg, position)
        except PositionError as error:
            _row("TICK", "FROZEN", error)
            return EXIT_FROZEN
        before = position
        prior_check = before.get("last_check")
        prior_decision = prior_check.get("decision") \
            if isinstance(prior_check, dict) else None
        emergency_before = os.path.exists(os.path.join(
            cfg.receipt_dir, EMERGENCY_FILE))
        check_code = cmd_check(
            position_cfg, request, sleep=sleep, now_ms=now_ms)
        after = _load_after("CHECK", position_cfg)
        if after is None:
            return EXIT_FROZEN
        decision = _fresh_check_decision(before, after, check_code)
        emergency = emergency_before or os.path.exists(os.path.join(
            cfg.receipt_dir, EMERGENCY_FILE))
        _row("CHECK", "DONE" if check_code == 0 else "FROZEN",
             "position command exit %s decision %s"
             % (check_code, decision or
                (REQUEST_EMERGENCY_UNWIND if emergency else "UNPROVEN")))
        close_reason = REQUEST_EMERGENCY_UNWIND if emergency else (
            decision if decision in CLOSE_DECISIONS else
            prior_decision if prior_decision in CLOSE_DECISIONS else None)
        if check_code != POSITION_EXIT_CODE["DECIDED"] \
                and close_reason is None:
            return EXIT_FROZEN
        if after.get("state") != ST_OPEN:
            return EXIT_FROZEN
        if close_reason is not None:
            close_code = cmd_close(
                position_cfg, request, sleep=sleep, now_ms=now_ms)
            _row("CLOSE", "AUTOMATIC",
                 "%s; position command exit %s"
                 % (close_reason, close_code))
            resume_code, final_position = _reconcile(
                position_cfg, request, sleep, now_ms,
                label="CLOSE-RECONCILE")
            final_state = final_position.get("state") \
                if final_position else None
            if resume_code != 0 or final_state in FREEZE_STATES \
                    or final_position is None:
                return EXIT_FROZEN
            return EXIT_ACTED
        if decision == FREEZE_NEW_ACTIONS:
            return EXIT_FROZEN
        if decision is None:
            _row("TICK", "FROZEN", "check decision was not persisted")
            return EXIT_FROZEN
        _row("TICK", "HELD", "OPEN position remains within policy")
        return EXIT_HELD

    # CLOSED/NONE state does not stand.  STOP gates only entry; reconciliation,
    # checks, and exits above never consult it.
    for seal_name in (STOP_FILE, RUNNER_STOP_FILE):
        if os.path.exists(os.path.join(cfg.receipt_dir, seal_name)):
            _row("ENTRY", "SKIPPED", "%s file present" % seal_name)
            return EXIT_IDLE

    tick_ms = now_ms()
    try:
        candidate = _scan_newest_valid(cfg, tick_ms, rejects)
    except OSError:
        _count_reject(rejects, "proposal_scan_error")
        return EXIT_FROZEN
    if candidate is None:
        _row("ENTRY", "IDLE", "no valid proposal")
        return EXIT_IDLE
    if operator is None or not operator.strip() or len(operator) > 128:
        _count_reject(rejects, "operator_mismatch")
        _row("ENTRY", "HELD", "OPERATOR_MISMATCH")
        return EXIT_HELD
    try:
        claimed = _claim(candidate, cfg, claimant, tick_ms)
    except OSError as error:
        _count_reject(rejects, "claim_error")
        _row("CLAIM", "FROZEN", "%s: %s"
             % (candidate.proposal_id, type(error).__name__))
        return EXIT_FROZEN
    if not claimed:
        _count_reject(rejects, "claim_owned")
        return EXIT_IDLE

    # The claim owns the proposal id.  Re-read the immutable proposal after
    # that atomic boundary and bind the position machine to its symbol.
    try:
        claimed_candidate = _load_candidate(
            cfg, candidate.proposal_id, tick_ms)
    except (KeyError, TypeError, ValueError, PositionError) as error:
        _count_reject(rejects, "claimed_proposal_invalid")
        _row("CLAIM", "FROZEN", "%s: %s"
             % (candidate.proposal_id, _text(error, 220)))
        return EXIT_FROZEN
    if claimed_candidate.digest != candidate.digest:
        _count_reject(rejects, "claimed_proposal_changed")
        _row("CLAIM", "FROZEN", "proposal %s changed during claim"
             % candidate.proposal_id)
        return EXIT_FROZEN
    if cfg.symbol and claimed_candidate.symbol != cfg.symbol:
        _count_reject(rejects, "proposal_symbol_filtered")
        _row("CLAIM", "FROZEN", "proposal %s escaped symbol whitelist"
             % candidate.proposal_id)
        return EXIT_FROZEN

    recovered_token = _claim_governor_token(cfg, candidate.proposal_id)
    if recovered_token:
        released = _release_recovered_governor(
            recovered_token, cfg.receipt_dir)
        cleared = released and _write_claim_governor_token(
            cfg, candidate.proposal_id, claimant, "")
        if not released or not cleared:
            _touch_stop(os.path.join(cfg.receipt_dir, RUNNER_STOP_FILE))
            reason = "governor_release_failed" if not released \
                else "governor_token_clear_failed"
            _count_reject(rejects, reason)
            _row("GOVERNOR", "HELD",
                 "unstarted claim recovery failed; STOP-JOBS written")
            return EXIT_FROZEN

    notional_cap = _entry_notional_cap(cfg.receipt_dir)
    entry_job = _basis_entry_job(claimed_candidate, operator, notional_cap)
    ticket_refusal = _entry_ticket_refusal(
        entry_job, _paths(cfg.receipt_dir),
        datetime.fromtimestamp(tick_ms / 1000, timezone.utc))
    if ticket_refusal:
        reason_key = ticket_refusal.lower()
        _count_reject(rejects, reason_key)
        _row("ENTRY", "HELD", ticket_refusal)
        return EXIT_HELD

    governor_token, release_governor, governor_refusal = \
        _acquire_governor(entry_job, cfg.receipt_dir)
    if governor_refusal:
        _count_reject(rejects, "governor_refused")
        _row("ENTRY", "HELD", governor_refusal)
        return EXIT_HELD
    if not _write_claim_governor_token(
            cfg, candidate.proposal_id, claimant, governor_token):
        released = _release_governor(
            release_governor, governor_token, cfg.receipt_dir)
        if not released:
            _touch_stop(os.path.join(cfg.receipt_dir, RUNNER_STOP_FILE))
        _count_reject(rejects, "governor_token_persist_failed")
        _row("ENTRY", "FROZEN", "GOVERNOR_TOKEN_PERSIST_FAILED")
        return EXIT_FROZEN

    proposal_cfg = dataclasses.replace(
        cfg, symbol=claimed_candidate.symbol)
    open_code = _call_capped_open(
        proposal_cfg, request, sleep, now_ms, candidate.proposal_id,
        notional_cap)
    opened = _load_after("OPEN", proposal_cfg)
    opened_state = opened.get("state") if opened else ST_NONE
    runner_outcome = _runner_entry_outcome(
        open_code, opened, proposal_cfg)
    accept_claim_failed = False
    if runner_outcome is not None and _entry_outcome_succeeded(runner_outcome):
        accept_path = os.path.join(cfg.receipt_dir, ACCEPT_ONE_FILE)
        accept_claim_failed = not _consume_accept_one(
            accept_path, entry_job.job_id)
        if accept_claim_failed:
            _touch_stop(os.path.join(cfg.receipt_dir, RUNNER_STOP_FILE))
            _count_reject(rejects, "accept_one_claim_failed")
            _row("ENTRY", "FROZEN", "ACCEPT_ONE_CLAIM_FAILED")
    _row("OPEN", "DONE" if open_code == 0 else "HELD",
         "proposal %s position command exit %s state %s"
         % (candidate.proposal_id, open_code, opened_state))
    if open_code != 0:
        _count_reject(rejects, "open_refused")
    if not accept_claim_failed and governor_token \
            and runner_outcome is not None \
            and _entry_outcome_terminal(runner_outcome):
        if not _release_governor(
                release_governor, governor_token, cfg.receipt_dir):
            _touch_stop(os.path.join(cfg.receipt_dir, RUNNER_STOP_FILE))
            _count_reject(rejects, "governor_release_failed")
            _row("GOVERNOR", "HELD",
                 "release failed; STOP-JOBS entry seal written")
        elif not _write_claim_governor_token(
                cfg, candidate.proposal_id, claimant, ""):
            _touch_stop(os.path.join(cfg.receipt_dir, RUNNER_STOP_FILE))
            _count_reject(rejects, "governor_token_clear_failed")
            _row("GOVERNOR", "HELD",
                 "released but claim token clear failed; STOP-JOBS written")
    if accept_claim_failed:
        return EXIT_FROZEN
    if opened is not None and opened_state in FREEZE_STATES:
        return EXIT_FROZEN
    return EXIT_ACTED


def run(cfg: PositionConfig, request, seconds: float, halt_file: str, *,
        sleep: Callable[[float], None] = time.sleep,
        now_ms: Callable[[], int] = default_now_ms,
        claimant: Optional[str] = None) -> int:
    """Run ticks until the independent halt file appears."""
    if seconds <= 0:
        raise ValueError("SECONDS must be positive")
    last_code = EXIT_IDLE
    while not os.path.exists(halt_file):
        last_code = tick(cfg, request, sleep=sleep, now_ms=now_ms,
                         claimant=claimant)
        if os.path.exists(halt_file):
            break
        sleep(seconds)
    _row("LOOP", "HALTED", halt_file)
    return last_code


def _positive_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("SECONDS must be numeric") from None
    if not parsed > 0:
        raise argparse.ArgumentTypeError("SECONDS must be positive")
    return parsed


def _config_from_env(env, symbol_arg: str) -> PositionConfig:
    """Load shared policy while keeping SYMBOL an optional whitelist."""
    requested = symbol_arg or env.get("SYMBOL", "")
    cfg = config_from_env(env, requested or "ALLUSDT")
    return dataclasses.replace(cfg, symbol=cfg.symbol if requested else "")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="restart-safe one-position basis supervisor")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--tick", action="store_true",
                      help="run one supervisor cycle")
    mode.add_argument("--run", metavar="SECONDS", type=_positive_seconds,
                      help="run ticks at this interval until the halt file")
    parser.add_argument("--symbol", default="",
                        help="optional proposal-symbol whitelist (default: "
                             "SYMBOL environment; unset means all)")
    parser.add_argument("--halt-file",
                        help="loop halt path (default: RECEIPT_DIR/HALT)")
    args = parser.parse_args(argv)

    try:
        cfg = _config_from_env(os.environ, args.symbol)
    except PositionError as error:
        _row("CONFIG", "FROZEN", error)
        return EXIT_FROZEN
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        _row("CONFIG", "FROZEN",
             "missing BINANCE_API_KEY / BINANCE_API_SECRET")
        return EXIT_FROZEN
    request = live_request_factory(api_key, api_secret)
    if args.tick:
        return tick(cfg, request)
    halt_file = os.path.expanduser(
        args.halt_file or os.path.join(cfg.receipt_dir, "HALT"))
    try:
        return run(cfg, request, args.run, halt_file)
    except KeyboardInterrupt:
        _row("LOOP", "HALTED", "keyboard interrupt")
        return EXIT_IDLE


if __name__ == "__main__":
    raise SystemExit(main())
