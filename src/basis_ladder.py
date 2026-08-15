"""Deterministic laddered-basis engine core.

This module owns strategy state and intent generation only.  It has no clock,
transport, signing, account, or order surface.  A caller supplies executable
directional gaps and persists the returned state after arranging execution.

Gap convention
--------------
Gaps are Decimal percentages in the selected direction.  For ``normal`` the
entry pair is spot long + perp short and a positive gap means perp is above
spot.  For ``reverse`` the pair is spot short + perp long and a positive gap
means spot is above perp.  The caller prices the executable leg pair before
calling this engine; this module never reconstructs a gap from marks.

Ladder and unwind rule
----------------------
The schedule is ordered from narrow to wide as ``(gap_threshold, rung_size)``.
Only the next unheld schedule row may be added, so a jump across several rows
still builds a ladder rather than one oversized position.  Wider rows must
have strictly larger sizes.

Unwind thresholds are derived without another static table: rung zero unwinds
at ``convergence_gap`` and every later rung unwinds when the executable exit
gap closes through the preceding entry threshold.  This gives one schedule
band of hysteresis.  Eligible rungs unwind LIFO.  An adverse funding sign flip
raises each rung's unwind threshold by one band, to that rung's own entry
threshold, so risk comes off one band earlier.  Positive funding is paid by
perp longs: negative is adverse to normal; positive is adverse to reverse.

``max_unwind_notional_per_tick`` may cap a non-convergence unwind.  If that
cap cuts through the top rung, the remaining rung and emitted amount retain
the exact Decimal values.  Full convergence overrides this pacing cap and
returns a flat state immediately.

State serialization is a versioned plain dict.  Every Decimal is text and a
restore followed by serialization is identical.  No execution result is
invented here: emitted rows are intents, and execution/reconciliation belongs
to a separate owner-controlled layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional, Tuple

__all__ = [
    "NORMAL",
    "REVERSE",
    "ADD_RUNG",
    "UNWIND_RUNG",
    "HOLD",
    "REFUSE",
    "LadderError",
    "LadderLevel",
    "LadderConfig",
    "FundingContext",
    "Rung",
    "LadderState",
    "Intent",
    "TickResult",
    "advance",
    "step",
    "tick",
    "serialize_state",
    "restore_state",
]

NORMAL = "normal"
REVERSE = "reverse"
DIRECTIONS = (NORMAL, REVERSE)

ADD_RUNG = "ADD_RUNG"
UNWIND_RUNG = "UNWIND_RUNG"
HOLD = "HOLD"
REFUSE = "REFUSE"
INTENT_KINDS = (ADD_RUNG, UNWIND_RUNG, HOLD, REFUSE)

STATE_VERSION = 1
ZERO = Decimal("0")


class LadderError(ValueError):
    """Malformed configuration or state, never a market outcome."""


def _is_decimal(value: Any) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _require_decimal(name: str, value: Any) -> Decimal:
    if not _is_decimal(value):
        raise LadderError("%s must be a finite Decimal" % name)
    return value


def _decimal_text(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value)


@dataclass(frozen=True)
class LadderLevel:
    """One widening-gap entry threshold and its requested quote notional."""

    gap_threshold: Decimal
    rung_size: Decimal

    def __post_init__(self) -> None:
        _require_decimal("gap_threshold", self.gap_threshold)
        _require_decimal("rung_size", self.rung_size)
        if self.rung_size <= ZERO:
            raise LadderError("rung_size must be > 0")


def _level(value: Any) -> LadderLevel:
    if isinstance(value, LadderLevel):
        return value
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return LadderLevel(value[0], value[1])
    raise LadderError("schedule rows must be LadderLevel or Decimal pairs")


@dataclass(frozen=True)
class LadderConfig:
    """Frozen schedule and hard notional/rung bounds for one engine."""

    schedule: Tuple[LadderLevel, ...]
    max_rungs: int
    max_total_notional: Decimal
    min_notional: Decimal
    convergence_gap: Decimal = ZERO
    max_unwind_notional_per_tick: Optional[Decimal] = None

    def __post_init__(self) -> None:
        try:
            levels = tuple(_level(row) for row in self.schedule)
        except TypeError as error:
            raise LadderError("schedule must be a finite sequence") from error
        if not levels:
            raise LadderError("schedule must not be empty")
        object.__setattr__(self, "schedule", levels)

        if isinstance(self.max_rungs, bool) or not isinstance(self.max_rungs, int):
            raise LadderError("max_rungs must be an int")
        if self.max_rungs < 0:
            raise LadderError("max_rungs must be >= 0")
        _require_decimal("max_total_notional", self.max_total_notional)
        _require_decimal("min_notional", self.min_notional)
        _require_decimal("convergence_gap", self.convergence_gap)
        if self.max_total_notional < ZERO:
            raise LadderError("max_total_notional must be >= 0")
        if self.min_notional <= ZERO:
            raise LadderError("min_notional must be > 0")
        if self.max_unwind_notional_per_tick is not None:
            _require_decimal(
                "max_unwind_notional_per_tick",
                self.max_unwind_notional_per_tick,
            )
            if self.max_unwind_notional_per_tick <= ZERO:
                raise LadderError("max_unwind_notional_per_tick must be > 0")

        previous_gap = self.convergence_gap
        previous_size: Optional[Decimal] = None
        for level in levels:
            if level.gap_threshold <= previous_gap:
                raise LadderError(
                    "schedule gap thresholds must increase above convergence_gap"
                )
            if previous_size is not None and level.rung_size <= previous_size:
                raise LadderError("wider schedule rows must have larger rung sizes")
            previous_gap = level.gap_threshold
            previous_size = level.rung_size


@dataclass(frozen=True)
class FundingContext:
    """Two consecutive funding rates, expressed as fraction per period."""

    previous_rate: Decimal
    current_rate: Decimal

    def valid(self) -> bool:
        return _is_decimal(self.previous_rate) and _is_decimal(self.current_rate)

    def adverse_flip(self, direction: str) -> bool:
        """Whether funding crossed from non-adverse to adverse this tick."""
        if not self.valid() or direction not in DIRECTIONS:
            return False
        if direction == NORMAL:
            return self.previous_rate >= ZERO and self.current_rate < ZERO
        return self.previous_rate <= ZERO and self.current_rate > ZERO


@dataclass(frozen=True)
class Rung:
    """Held quote notional entered at an observed executable gap."""

    gap_at_entry: Decimal
    size: Decimal

    def __post_init__(self) -> None:
        _require_decimal("gap_at_entry", self.gap_at_entry)
        _require_decimal("size", self.size)
        if self.size <= ZERO:
            raise LadderError("held rung size must be > 0")

    def to_dict(self) -> Dict[str, str]:
        return {"gap_at_entry": str(self.gap_at_entry), "size": str(self.size)}


def _rung(value: Any) -> Rung:
    if isinstance(value, Rung):
        return value
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return Rung(value[0], value[1])
    raise LadderError("rungs must be Rung or Decimal pairs")


@dataclass(frozen=True)
class LadderState:
    """Crash-safe dynamic state: direction plus its LIFO held rungs."""

    direction: str
    rungs: Tuple[Rung, ...] = ()

    def __post_init__(self) -> None:
        if self.direction not in DIRECTIONS:
            raise LadderError("direction must be normal or reverse")
        try:
            normalized = tuple(_rung(row) for row in self.rungs)
        except TypeError as error:
            raise LadderError("rungs must be a finite sequence") from error
        object.__setattr__(self, "rungs", normalized)

    @classmethod
    def flat(cls, direction: str = NORMAL) -> "LadderState":
        return cls(direction=direction, rungs=())

    @property
    def total_notional(self) -> Decimal:
        return sum((rung.size for rung in self.rungs), ZERO)

    @property
    def is_flat(self) -> bool:
        return not self.rungs

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "direction": self.direction,
            "rungs": [rung.to_dict() for rung in self.rungs],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LadderState":
        if not isinstance(payload, Mapping):
            raise LadderError("state payload must be a mapping")
        if set(payload) != {"version", "direction", "rungs"}:
            raise LadderError("state payload keys are invalid")
        if payload["version"] != STATE_VERSION:
            raise LadderError("state version is unsupported")
        raw_rungs = payload["rungs"]
        if not isinstance(raw_rungs, list):
            raise LadderError("state rungs must be a list")
        restored = []
        for row in raw_rungs:
            if not isinstance(row, Mapping) or set(row) != {"gap_at_entry", "size"}:
                raise LadderError("serialized rung keys are invalid")
            gap_text, size_text = row["gap_at_entry"], row["size"]
            if not isinstance(gap_text, str) or not isinstance(size_text, str):
                raise LadderError("serialized Decimals must be text")
            try:
                restored.append(Rung(Decimal(gap_text), Decimal(size_text)))
            except Exception as error:
                if isinstance(error, LadderError):
                    raise
                raise LadderError("serialized Decimal text is invalid") from error
        return cls(direction=payload["direction"], rungs=tuple(restored))


@dataclass(frozen=True)
class Intent:
    """One typed, execution-free engine output row."""

    kind: str
    reason: str
    direction: str
    size: Optional[Decimal]
    observed_gap: Optional[Decimal]
    threshold: Optional[Decimal]
    gap_at_entry: Optional[Decimal]
    scheduled_size: Optional[Decimal]
    total_notional_after: Decimal
    rung_count_after: int

    def __post_init__(self) -> None:
        if self.kind not in INTENT_KINDS:
            raise LadderError("unknown intent kind")
        if self.direction not in DIRECTIONS:
            raise LadderError("unknown intent direction")
        if not isinstance(self.reason, str) or not self.reason:
            raise LadderError("intent reason must be non-empty text")
        for name in (
            "size",
            "observed_gap",
            "threshold",
            "gap_at_entry",
            "scheduled_size",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_decimal(name, value)
        _require_decimal("total_notional_after", self.total_notional_after)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "direction": self.direction,
            "size": _decimal_text(self.size),
            "observed_gap": _decimal_text(self.observed_gap),
            "threshold": _decimal_text(self.threshold),
            "gap_at_entry": _decimal_text(self.gap_at_entry),
            "scheduled_size": _decimal_text(self.scheduled_size),
            "total_notional_after": str(self.total_notional_after),
            "rung_count_after": self.rung_count_after,
        }


@dataclass(frozen=True)
class TickResult:
    """New deterministic state and one or more ordered intent rows."""

    state: LadderState
    intents: Tuple[Intent, ...]

    def __post_init__(self) -> None:
        if not self.intents:
            raise LadderError("every tick must emit at least one intent")

    @property
    def intent(self) -> Intent:
        """First row convenience for the common one-transition tick."""
        return self.intents[0]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.to_dict(),
            "intents": [intent.to_dict() for intent in self.intents],
        }


def _status(
    state: LadderState,
    kind: str,
    reason: str,
    observed_gap: Optional[Decimal],
    threshold: Optional[Decimal] = None,
) -> TickResult:
    return TickResult(
        state=state,
        intents=(Intent(
            kind=kind,
            reason=reason,
            direction=state.direction,
            size=None,
            observed_gap=observed_gap,
            threshold=threshold,
            gap_at_entry=None,
            scheduled_size=None,
            total_notional_after=state.total_notional,
            rung_count_after=len(state.rungs),
        ),),
    )


def _unwind(
    state: LadderState,
    config: LadderConfig,
    exit_gap: Decimal,
    funding_flip: bool,
    full_convergence: bool,
) -> Optional[TickResult]:
    """Return an unwind result when one or more LIFO rungs are eligible."""
    if not state.rungs:
        return None
    if len(state.rungs) > len(config.schedule) and not full_convergence:
        return _status(state, REFUSE, "STATE_SCHEDULE_MISMATCH", exit_gap)

    working = list(state.rungs)
    intents = []
    budget = None if full_convergence else config.max_unwind_notional_per_tick

    while working:
        index = len(working) - 1
        if full_convergence:
            eligible = True
            threshold = config.convergence_gap
            reason = "FULL_CONVERGENCE"
        else:
            base_threshold = (
                config.convergence_gap
                if index == 0
                else config.schedule[index - 1].gap_threshold
            )
            own_threshold = config.schedule[index].gap_threshold
            if exit_gap <= base_threshold:
                eligible = True
                threshold = base_threshold
                reason = "GAP_CLOSED_THROUGH_THRESHOLD"
            elif funding_flip and exit_gap <= own_threshold:
                eligible = True
                threshold = own_threshold
                reason = "ADVERSE_FUNDING_FLIP_ACCELERATION"
            else:
                eligible = False
                threshold = base_threshold
                reason = ""
        if not eligible:
            break
        if budget is not None and budget <= ZERO:
            break

        top = working[-1]
        unwind_size = top.size if budget is None else min(top.size, budget)
        if unwind_size == top.size:
            working.pop()
        else:
            working[-1] = Rung(top.gap_at_entry, top.size - unwind_size)
        if budget is not None:
            budget -= unwind_size

        next_state = LadderState(state.direction, tuple(working))
        intents.append(Intent(
            kind=UNWIND_RUNG,
            reason=reason,
            direction=state.direction,
            size=unwind_size,
            observed_gap=exit_gap,
            threshold=threshold,
            gap_at_entry=top.gap_at_entry,
            scheduled_size=None,
            total_notional_after=next_state.total_notional,
            rung_count_after=len(next_state.rungs),
        ))
        if unwind_size < top.size:
            break

    if not intents:
        return None
    return TickResult(LadderState(state.direction, tuple(working)), tuple(intents))


def advance(
    state: LadderState,
    executable_entry_gap: Optional[Decimal],
    executable_exit_gap: Optional[Decimal],
    funding: Optional[FundingContext],
    config: LadderConfig,
    borrow_power: bool = False,
) -> TickResult:
    """Advance one tick and return only state plus typed intent rows.

    Risk reduction has priority over additions.  Missing/non-Decimal market
    facts refuse a prospective add, but a known exit-gap unwind remains
    available even when entry or funding facts are unknown.
    """
    if not isinstance(state, LadderState):
        raise LadderError("state must be LadderState")
    if not isinstance(config, LadderConfig):
        raise LadderError("config must be LadderConfig")

    exit_gap = executable_exit_gap if _is_decimal(executable_exit_gap) else None
    entry_gap = executable_entry_gap if _is_decimal(executable_entry_gap) else None
    funding_valid = isinstance(funding, FundingContext) and funding.valid()
    funding_flip = funding.adverse_flip(state.direction) if funding_valid else False

    # A known full convergence always removes risk, including restored state
    # that no longer matches the active schedule or configured pacing cap.
    if state.rungs and exit_gap is not None \
            and exit_gap <= config.convergence_gap:
        result = _unwind(state, config, exit_gap, funding_flip, True)
        assert result is not None
        return result

    if exit_gap is None:
        return _status(state, REFUSE, "EXIT_GAP_UNKNOWN", None)

    if state.rungs:
        unwind = _unwind(state, config, exit_gap, funding_flip, False)
        if unwind is not None:
            return unwind

    if entry_gap is None:
        return _status(state, REFUSE, "ENTRY_GAP_UNKNOWN", None)

    next_index = len(state.rungs)
    if next_index >= len(config.schedule):
        return _status(state, HOLD, "SCHEDULE_COMPLETE", entry_gap)
    level = config.schedule[next_index]
    if entry_gap < level.gap_threshold:
        return _status(
            state,
            HOLD,
            "BELOW_NEXT_THRESHOLD",
            entry_gap,
            level.gap_threshold,
        )

    # From this point the tick is asking to add exposure, so every unknown or
    # hard bound becomes an explicit REFUSE row rather than an inferred HOLD.
    if not funding_valid:
        return _status(
            state, REFUSE, "FUNDING_UNKNOWN", entry_gap, level.gap_threshold
        )
    if funding_flip:
        return _status(
            state,
            REFUSE,
            "ADVERSE_FUNDING_FLIP",
            entry_gap,
            level.gap_threshold,
        )
    if state.direction == REVERSE:
        if not isinstance(borrow_power, bool):
            return _status(
                state,
                REFUSE,
                "BORROW_POWER_UNKNOWN",
                entry_gap,
                level.gap_threshold,
            )
        if not borrow_power:
            return _status(
                state,
                REFUSE,
                "BORROW_POWER_REQUIRED",
                entry_gap,
                level.gap_threshold,
            )
    if len(state.rungs) >= config.max_rungs:
        return _status(
            state, REFUSE, "MAX_RUNGS", entry_gap, level.gap_threshold
        )
    if state.total_notional > config.max_total_notional:
        return _status(
            state,
            REFUSE,
            "STATE_MAX_TOTAL_EXCEEDED",
            entry_gap,
            level.gap_threshold,
        )

    remaining = config.max_total_notional - state.total_notional
    if remaining <= ZERO:
        return _status(
            state,
            REFUSE,
            "MAX_TOTAL_NOTIONAL",
            entry_gap,
            level.gap_threshold,
        )
    add_size = min(level.rung_size, remaining)
    if add_size < config.min_notional:
        return _status(
            state,
            REFUSE,
            "MIN_NOTIONAL_INFEASIBLE",
            entry_gap,
            level.gap_threshold,
        )

    added = Rung(gap_at_entry=entry_gap, size=add_size)
    next_state = LadderState(state.direction, state.rungs + (added,))
    reason = (
        "SCHEDULE_RUNG_CAP_CLAMPED"
        if add_size < level.rung_size
        else "SCHEDULE_THRESHOLD_REACHED"
    )
    intent = Intent(
        kind=ADD_RUNG,
        reason=reason,
        direction=state.direction,
        size=add_size,
        observed_gap=entry_gap,
        threshold=level.gap_threshold,
        gap_at_entry=entry_gap,
        scheduled_size=level.rung_size,
        total_notional_after=next_state.total_notional,
        rung_count_after=len(next_state.rungs),
    )
    return TickResult(next_state, (intent,))


# Familiar reducer names for callers; all three are the same pure operation.
step = advance
tick = advance


def serialize_state(state: LadderState) -> Dict[str, Any]:
    if not isinstance(state, LadderState):
        raise LadderError("state must be LadderState")
    return state.to_dict()


def restore_state(payload: Mapping[str, Any]) -> LadderState:
    return LadderState.from_dict(payload)
