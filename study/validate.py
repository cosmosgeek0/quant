#!/usr/bin/env python3
"""Leakage-proof out-of-sample validation for Volume Dataset v2.

The cohort is split before event rows are inspected.  Within each venue class,
tokens are ranked by a seeded SHA-256 digest; the first floor(70%) are fit
tokens and the remainder are score tokens.  The time window is thirty UTC
calendar days: days 1-20 are fit days and days 21-30 are score days.  Threshold
selection receives only rows in the fit-token x fit-day intersection.  Every
reported economic metric receives only rows in the disjoint score-token x
score-day intersection.  The other two token/time quadrants are excluded.

Stage A supplies labels, event-time excursions, and the named pre-move feature
used as ``runner_score``.  Policy simulation supplies latency, gross/net return,
and the complete fee/spread/slippage decomposition.  They are joined only by
``event_id`` with symbol, venue class, label, and UTC cross-checks.  Standalone
combined rows are also accepted by :func:`load_trades` for focused fixtures.
Decimal evidence must be finite text; binary JSON numbers are refused except
for Stage A's exact integer ``time_to_peak_min`` field.

The selected threshold maximizes fit-set runner F1.  Ties prefer precision,
then recall, fewer false positives, and finally the higher threshold.  Tail
returns are the best and worst five percent of detected score trades (at least
one trade per non-empty tail), restricted to positive winners and negative
losses.  Drawdown compounds net percentage returns in event-time order from a
unit starting equity and reports the largest peak-to-trough percentage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional
from typing import Sequence, Tuple


STUDY_DIR = Path(__file__).resolve().parent
DEFAULT_COHORT_PATH = STUDY_DIR / "out" / "v2" / "cohort_manifest.json"
DEFAULT_EVENTS_PATH = STUDY_DIR / "out" / "v2" / "events.jsonl"
DEFAULT_TRADES_PATH = STUDY_DIR / "out" / "v2" / "trades.jsonl"
DEFAULT_OUTPUT_PATH = STUDY_DIR / "out" / "v2" / "v2_result.json"
DEFAULT_SPLIT_SEED = "volume-dataset-v2"
DEFAULT_RUNNER_SCORE_PATH = "pre_state.volume_acceleration.1m"
DEFAULT_EXCURSION_HORIZON_MIN = 60
LABEL_OUTCOME_HORIZON_MIN = 60
REPRO_SCRIPT = "study/validate.py"
VENUE_CLASSES = ("spot_only", "futures_only", "both")
EVENT_LABELS = (
    "DeadRegime",
    "Ignition",
    "GenuineRunner",
    "FalseIgnition",
    "SlowRamp",
    "AlreadyPumped",
)
FIT_DAY_COUNT = 20
SCORE_DAY_COUNT = 10
TOTAL_DAY_COUNT = FIT_DAY_COUNT + SCORE_DAY_COUNT
TOKEN_FIT_NUMERATOR = 7
TOKEN_SPLIT_DENOMINATOR = 10
TAIL_PERCENT = Decimal("5")
HUNDRED = Decimal("100")


class ValidationError(Exception):
    """The requested result cannot be proved from the supplied evidence."""


def decimal_text(value: Decimal) -> str:
    """Render one finite Decimal without exponent notation."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValidationError("cannot render a non-finite Decimal")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def parse_decimal(
    raw: Any,
    source: str,
    *,
    non_negative: bool = False,
) -> Decimal:
    """Parse finite Decimal text and explicitly refuse binary floats."""
    if isinstance(raw, Decimal):
        value = raw
    elif type(raw) is str and raw.strip():
        try:
            value = Decimal(raw.strip())
        except InvalidOperation:
            raise ValidationError(f"{source} is not Decimal text") from None
    else:
        raise ValidationError(f"{source} is not Decimal text")
    if not value.is_finite():
        raise ValidationError(f"{source} is not finite Decimal text")
    if non_negative and value < 0:
        raise ValidationError(f"{source} is negative")
    return value


def _decimal_field(
    row: Mapping[str, Any],
    names: Sequence[str],
    source: str,
    *,
    required: bool = True,
    non_negative: bool = False,
) -> Optional[Decimal]:
    found = [name for name in names if name in row]
    if not found:
        if required:
            raise ValidationError(f"{source} is missing {names[0]}")
        return None
    values = [
        parse_decimal(
            row[name],
            f"{source} {name}",
            non_negative=non_negative,
        )
        for name in found
    ]
    if any(value != values[0] for value in values[1:]):
        raise ValidationError(f"{source} has conflicting {names[0]} aliases")
    return values[0]


def _text_field(
    row: Mapping[str, Any],
    names: Sequence[str],
    source: str,
    *,
    required: bool = True,
) -> Optional[str]:
    found = [name for name in names if name in row]
    if not found:
        if required:
            raise ValidationError(f"{source} is missing {names[0]}")
        return None
    values = [row[name] for name in found]
    if any(type(value) is not str or not value.strip() for value in values):
        raise ValidationError(f"{source} {names[0]} is not non-empty text")
    normalized = [value.strip() for value in values]
    if any(value != normalized[0] for value in normalized[1:]):
        raise ValidationError(f"{source} has conflicting {names[0]} aliases")
    return normalized[0]


def _utc_datetime(raw: Any, source: str) -> datetime:
    if type(raw) is not str or not raw.endswith("Z"):
        raise ValidationError(f"{source} is not UTC text ending in Z")
    try:
        value = datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError:
        raise ValidationError(f"{source} is not UTC text") from None
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValidationError(f"{source} is not UTC text")
    return value.astimezone(timezone.utc)


def _canonical_label(raw: str, source: str) -> str:
    compact = re.sub(r"[^a-z]", "", raw.lower())
    matches = {
        re.sub(r"[^a-z]", "", label.lower()): label
        for label in EVENT_LABELS
    }
    if compact not in matches:
        raise ValidationError(f"{source} has unknown label {raw!r}")
    return matches[compact]


def _valid_symbol(raw: Any) -> bool:
    """Accept full-universe Unicode symbols without treating them as paths."""
    return bool(
        type(raw) is str
        and 2 <= len(raw) <= 64
        and raw == raw.strip()
        and raw.endswith("USDT")
        and all(character.isalnum() or character == "_" for character in raw)
    )


@dataclass(frozen=True)
class CohortMember:
    symbol: str
    venue_class: str

    def __post_init__(self) -> None:
        if not _valid_symbol(self.symbol):
            raise ValidationError("cohort member has invalid symbol")
        if self.venue_class not in VENUE_CLASSES:
            raise ValidationError(
                f"{self.symbol} has invalid venue_class {self.venue_class!r}"
            )


@dataclass(frozen=True)
class ValidationRecord:
    event_id: str
    symbol: str
    venue_class: str
    event_utc: datetime
    label: str
    runner_score: Decimal
    entry_latency_min: Decimal
    mfe_pct: Decimal
    mae_pct: Decimal
    time_to_peak_min: Decimal
    gross_return_pct: Decimal
    all_costs_pct: Decimal
    net_return_pct: Decimal
    source_order: int = 0
    policy: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        if type(self.event_id) is not str or not self.event_id:
            raise ValidationError("event_id is empty")
        if not _valid_symbol(self.symbol):
            raise ValidationError(f"{self.event_id} has invalid symbol")
        if self.venue_class not in VENUE_CLASSES:
            raise ValidationError(
                f"{self.event_id} has invalid venue_class"
            )
        if (
            not isinstance(self.event_utc, datetime)
            or self.event_utc.tzinfo is None
            or self.event_utc.utcoffset() != timedelta(0)
        ):
            raise ValidationError(f"{self.event_id} has non-UTC event time")
        if self.label not in EVENT_LABELS:
            raise ValidationError(f"{self.event_id} has invalid label")
        decimal_fields = (
            ("runner_score", self.runner_score, False),
            ("entry_latency_min", self.entry_latency_min, True),
            ("mfe_pct", self.mfe_pct, False),
            ("mae_pct", self.mae_pct, False),
            ("time_to_peak_min", self.time_to_peak_min, True),
            ("gross_return_pct", self.gross_return_pct, False),
            ("all_costs_pct", self.all_costs_pct, True),
            ("net_return_pct", self.net_return_pct, False),
        )
        for name, value, non_negative in decimal_fields:
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValidationError(
                    f"{self.event_id} {name} is not a finite Decimal"
                )
            if non_negative and value < 0:
                raise ValidationError(f"{self.event_id} {name} is negative")
        if self.net_return_pct != self.gross_return_pct - self.all_costs_pct:
            raise ValidationError(
                f"{self.event_id} net return does not reconcile with costs"
            )
        if self.net_return_pct < Decimal("-100"):
            raise ValidationError(
                f"{self.event_id} net return is below minus 100 percent"
            )
        if isinstance(self.source_order, bool) or not isinstance(
            self.source_order, int
        ):
            raise ValidationError(f"{self.event_id} source order is invalid")
        if self.policy is not None and not isinstance(self.policy, Mapping):
            raise ValidationError(f"{self.event_id} policy is invalid")

    @property
    def event_day(self) -> date:
        return self.event_utc.date()

    @property
    def genuine_runner(self) -> bool:
        return self.label == "GenuineRunner"


@dataclass(frozen=True)
class EventEvidence:
    event_id: str
    symbol: str
    venue_class: str
    event_utc: str
    label: str
    runner_score: Decimal
    mfe_pct: Decimal
    mae_pct: Decimal
    time_to_peak_min: Decimal


@dataclass(frozen=True)
class TokenSplit:
    fit_tokens: frozenset
    score_tokens: frozenset
    fit_by_venue: Mapping[str, Tuple[str, ...]]
    score_by_venue: Mapping[str, Tuple[str, ...]]


@dataclass(frozen=True)
class TimeSplit:
    fit_days: Tuple[date, ...]
    score_days: Tuple[date, ...]


@dataclass(frozen=True)
class RecordPartitions:
    fit_records: Tuple[ValidationRecord, ...]
    score_records: Tuple[ValidationRecord, ...]
    excluded_records: Tuple[ValidationRecord, ...]


def _read_json_lines(path: Path) -> Iterable[Tuple[int, Dict[str, Any]]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    raise ValidationError(
                        f"{path.name}:{line_number} is not JSON"
                    ) from None
                if not isinstance(row, dict):
                    raise ValidationError(
                        f"{path.name}:{line_number} is not an object"
                    )
                yield line_number, row
    except (OSError, UnicodeDecodeError) as error:
        raise ValidationError(f"cannot read {path}: {error}") from None


def _score_path_parts(raw: str) -> Tuple[str, ...]:
    if type(raw) is not str or not raw:
        raise ValidationError("runner score path is empty")
    parts = tuple(raw.split("."))
    if any(not part or not part.replace("_", "").isalnum() for part in parts):
        raise ValidationError("runner score path has an invalid component")
    if len(parts) < 2 or parts[0] != "pre_state":
        raise ValidationError("runner score must come from pre_state")
    forbidden = {
        "forward_return_pct",
        "mfe_pct",
        "mae_pct",
        "time_to_peak_min",
        "genuine_runner",
        "false_ignition",
    }
    if any(part in forbidden for part in parts[1:]):
        raise ValidationError("runner score path points to outcome evidence")
    return parts


def _nested_value(
    row: Mapping[str, Any], parts: Sequence[str], source: str
) -> Any:
    value: Any = row
    for part in parts:
        if not isinstance(value, Mapping) or part not in value:
            raise ValidationError(
                f"{source} is missing nested field {'.'.join(parts)}"
            )
        value = value[part]
    return value


def _exact_minutes(raw: Any, source: str) -> Decimal:
    if isinstance(raw, bool):
        raise ValidationError(f"{source} is not exact minutes")
    if isinstance(raw, int):
        if raw < 0:
            raise ValidationError(f"{source} is negative")
        return Decimal(raw)
    return parse_decimal(raw, source, non_negative=True)


def load_event_evidence(
    path: Path,
    *,
    runner_score_path: str = DEFAULT_RUNNER_SCORE_PATH,
    excursion_horizon_min: int = DEFAULT_EXCURSION_HORIZON_MIN,
) -> Dict[str, EventEvidence]:
    """Load Stage A labels, one pre-move score, and event-path outcomes."""
    if (
        isinstance(excursion_horizon_min, bool)
        or not isinstance(excursion_horizon_min, int)
        or excursion_horizon_min <= 0
    ):
        raise ValidationError("excursion horizon must be positive minutes")
    score_parts = _score_path_parts(runner_score_path)
    horizon = f"{excursion_horizon_min}m"
    evidence: Dict[str, EventEvidence] = {}
    path = Path(path)
    for line_number, row in _read_json_lines(path):
        if row.get("kind") == "summary":
            continue
        source = f"{path.name}:{line_number}"
        event_id = _text_field(row, ("event_id",), source)
        symbol = _text_field(row, ("symbol",), source)
        venue_class = _text_field(row, ("venue_class",), source)
        event_utc = _text_field(
            row, ("event_utc", "utc", "event_time"), source
        )
        label = _text_field(row, ("label", "event_label"), source)
        assert event_id is not None
        assert symbol is not None
        assert venue_class is not None
        assert event_utc is not None
        assert label is not None
        _utc_datetime(event_utc, f"{source} event UTC")
        CohortMember(symbol, venue_class)
        canonical_label = _canonical_label(label, source)
        runner_score = parse_decimal(
            _nested_value(row, score_parts, source),
            f"{source} {runner_score_path}",
        )
        pre_state = row.get("pre_state")
        if not isinstance(pre_state, Mapping):
            raise ValidationError(f"{source} pre_state is not an object")
        mfe_map = pre_state.get("mfe_pct")
        mae_map = pre_state.get("mae_pct")
        peak_map = pre_state.get("time_to_peak_min")
        if not isinstance(mfe_map, Mapping) or horizon not in mfe_map:
            raise ValidationError(f"{source} is missing MFE at {horizon}")
        if not isinstance(mae_map, Mapping) or horizon not in mae_map:
            raise ValidationError(f"{source} is missing MAE at {horizon}")
        if not isinstance(peak_map, Mapping) or horizon not in peak_map:
            raise ValidationError(
                f"{source} is missing time-to-peak at {horizon}"
            )
        item = EventEvidence(
            event_id=event_id,
            symbol=symbol,
            venue_class=venue_class,
            event_utc=event_utc,
            label=canonical_label,
            runner_score=runner_score,
            mfe_pct=parse_decimal(
                mfe_map[horizon], f"{source} mfe_pct.{horizon}"
            ),
            mae_pct=parse_decimal(
                mae_map[horizon], f"{source} mae_pct.{horizon}"
            ),
            time_to_peak_min=_exact_minutes(
                peak_map[horizon], f"{source} time_to_peak_min.{horizon}"
            ),
        )
        if event_id in evidence:
            raise ValidationError(f"{source} duplicates event_id {event_id}")
        evidence[event_id] = item
    if not evidence:
        raise ValidationError("events input has no Stage A evidence")
    return evidence


def load_cohort(path: Path) -> Tuple[CohortMember, ...]:
    """Load the frozen full-universe cohort manifest."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError(f"cannot read cohort {path}: {error}") from None
    if not isinstance(raw, list):
        raise ValidationError("cohort manifest is not a JSON array")
    members: List[CohortMember] = []
    seen: Dict[str, str] = {}
    for index, item in enumerate(raw):
        source = f"{path.name}:{index + 1}"
        if not isinstance(item, dict):
            raise ValidationError(f"{source} is not an object")
        symbol = _text_field(item, ("symbol",), source)
        venue_class = _text_field(item, ("venue_class",), source)
        assert symbol is not None and venue_class is not None
        if symbol in seen:
            raise ValidationError(f"{source} duplicates symbol {symbol}")
        member = CohortMember(symbol, venue_class)
        seen[symbol] = venue_class
        members.append(member)
    if not members:
        raise ValidationError("cohort manifest is empty")
    return tuple(members)


def _merge_event_evidence(
    row: Mapping[str, Any], evidence: EventEvidence, source: str
) -> Dict[str, Any]:
    event_id = _text_field(row, ("event_id",), source)
    symbol = _text_field(row, ("symbol",), source)
    venue_class = _text_field(row, ("venue_class",), source)
    utc_text = _text_field(
        row, ("event_utc", "utc", "event_time"), source
    )
    label = _text_field(row, ("label", "event_label"), source)
    assert event_id is not None
    assert symbol is not None
    assert venue_class is not None
    assert utc_text is not None
    assert label is not None
    if event_id != evidence.event_id:
        raise ValidationError(f"{source} event_id conflicts with Stage A")
    if symbol != evidence.symbol:
        raise ValidationError(f"{source} symbol conflicts with Stage A")
    if venue_class != evidence.venue_class:
        raise ValidationError(f"{source} venue_class conflicts with Stage A")
    if _utc_datetime(utc_text, source) != _utc_datetime(
        evidence.event_utc, source
    ):
        raise ValidationError(f"{source} event UTC conflicts with Stage A")
    if _canonical_label(label, source) != evidence.label:
        raise ValidationError(f"{source} label conflicts with Stage A")
    if "status" in row and row.get("status") != "CLOSED":
        raise ValidationError(f"{source} policy result is not CLOSED")

    merged = dict(row)
    additions = {
        "runner_score": evidence.runner_score,
        "mfe_pct": evidence.mfe_pct,
        "mae_pct": evidence.mae_pct,
        "time_to_peak_min": evidence.time_to_peak_min,
    }
    aliases = {
        "runner_score": (
            "runner_score",
            "signal_score",
            "detector_score",
            "score",
        ),
        "mfe_pct": (
            "mfe_pct",
            "maximum_favourable_excursion_pct",
            "maximum_favorable_excursion_pct",
        ),
        "mae_pct": ("mae_pct", "maximum_adverse_excursion_pct"),
        "time_to_peak_min": (
            "time_to_peak_min",
            "time_to_peak_minutes",
        ),
    }
    for name, expected in additions.items():
        existing = _decimal_field(
            row, aliases[name], source, required=False
        )
        if existing is not None and existing != expected:
            raise ValidationError(
                f"{source} {name} conflicts with Stage A evidence"
            )
        merged[name] = decimal_text(expected)
    return merged


def _policy_integer(raw: Any, source: str, *, non_negative: bool) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValidationError(f"{source} is not an integer")
    if non_negative and raw < 0:
        raise ValidationError(f"{source} is negative")
    return raw


def _normalized_policy(
    raw: Any, source: str, *, required: bool
) -> Optional[Dict[str, Any]]:
    if raw is None and not required:
        return None
    if not isinstance(raw, Mapping):
        raise ValidationError(f"{source} policy is not an object")
    decimal_names = (
        "max_alloc_usdt",
        "stop_pct",
        "spot_taker_fee_rate_per_side",
        "perp_taker_fee_rate_per_side",
        "spread_cost_rate_per_side",
        "slippage_conservative_rate_per_side",
    )
    decimals = {
        name: parse_decimal(
            raw.get(name),
            f"{source} policy.{name}",
            non_negative=(name != "stop_pct"),
        )
        for name in decimal_names
    }
    if decimals["max_alloc_usdt"] <= 0:
        raise ValidationError(f"{source} policy allocation is not positive")
    if not Decimal("0") < decimals["stop_pct"] < HUNDRED:
        raise ValidationError(f"{source} policy stop is outside (0,100)")
    for name in (
        "spot_taker_fee_rate_per_side",
        "perp_taker_fee_rate_per_side",
        "spread_cost_rate_per_side",
        "slippage_conservative_rate_per_side",
    ):
        if decimals[name] >= Decimal("1"):
            raise ValidationError(f"{source} policy {name} is not below one")
    if (
        decimals["spread_cost_rate_per_side"]
        + decimals["slippage_conservative_rate_per_side"]
        >= Decimal("1")
    ):
        raise ValidationError(f"{source} policy spread plus slippage is unsafe")

    entry_delay = _policy_integer(
        raw.get("entry_delay_min"),
        f"{source} policy.entry_delay_min",
        non_negative=True,
    )
    final_exit = _policy_integer(
        raw.get("final_exit_min_after_event"),
        f"{source} policy.final_exit_min_after_event",
        non_negative=True,
    )
    if final_exit <= 0:
        raise ValidationError(f"{source} policy final exit is not positive")

    stages_raw = raw.get("stages")
    if not isinstance(stages_raw, list) or not stages_raw:
        raise ValidationError(f"{source} policy stages are empty")
    stages: List[Dict[str, Any]] = []
    stage_total = Decimal("0")
    previous_trigger: Optional[int] = None
    for index, item in enumerate(stages_raw):
        if not isinstance(item, Mapping):
            raise ValidationError(f"{source} policy stage is not an object")
        fraction = parse_decimal(
            item.get("fraction"),
            f"{source} policy.stages[{index}].fraction",
        )
        trigger = _policy_integer(
            item.get("trigger_min_after_delay"),
            f"{source} policy.stages[{index}].trigger",
            non_negative=True,
        )
        if not Decimal("0") < fraction <= Decimal("1"):
            raise ValidationError(f"{source} policy stage fraction is invalid")
        if previous_trigger is not None and trigger <= previous_trigger:
            raise ValidationError(
                f"{source} policy stage triggers are not increasing"
            )
        previous_trigger = trigger
        stage_total += fraction
        stages.append(
            {"fraction": fraction, "trigger_min_after_delay": trigger}
        )
    if stages[0]["trigger_min_after_delay"] != 0 or stage_total > 1:
        raise ValidationError(f"{source} policy stages violate allocation")
    if final_exit <= entry_delay + stages[-1]["trigger_min_after_delay"]:
        raise ValidationError(f"{source} policy exits before its last entry")

    partials_raw = raw.get("partials")
    if not isinstance(partials_raw, list):
        raise ValidationError(f"{source} policy partials are not a list")
    partials: List[Dict[str, Decimal]] = []
    partial_total = Decimal("0")
    previous_gain: Optional[Decimal] = None
    for index, item in enumerate(partials_raw):
        if not isinstance(item, Mapping):
            raise ValidationError(f"{source} policy partial is not an object")
        fraction = parse_decimal(
            item.get("fraction"),
            f"{source} policy.partials[{index}].fraction",
        )
        gain = parse_decimal(
            item.get("gain_pct"),
            f"{source} policy.partials[{index}].gain_pct",
        )
        if not Decimal("0") < fraction <= Decimal("1") or gain <= 0:
            raise ValidationError(f"{source} policy partial is invalid")
        if previous_gain is not None and gain <= previous_gain:
            raise ValidationError(
                f"{source} policy partial gains are not increasing"
            )
        previous_gain = gain
        partial_total += fraction
        partials.append({"fraction": fraction, "gain_pct": gain})
    if partial_total > 1:
        raise ValidationError(f"{source} policy partial fractions exceed one")

    return {
        "entry_delay_min": entry_delay,
        "stages": stages,
        "max_alloc_usdt": decimals["max_alloc_usdt"],
        "stop_pct": decimals["stop_pct"],
        "partials": partials,
        "final_exit_min_after_event": final_exit,
        "spot_taker_fee_rate_per_side": decimals[
            "spot_taker_fee_rate_per_side"
        ],
        "perp_taker_fee_rate_per_side": decimals[
            "perp_taker_fee_rate_per_side"
        ],
        "spread_cost_rate_per_side": decimals[
            "spread_cost_rate_per_side"
        ],
        "slippage_conservative_rate_per_side": decimals[
            "slippage_conservative_rate_per_side"
        ],
    }


def _record_from_row(
    row: Mapping[str, Any], source: str, source_order: int
) -> ValidationRecord:
    policy = _normalized_policy(
        row.get("policy"), source, required="policy" in row
    )
    symbol = _text_field(row, ("symbol",), source)
    venue_class = _text_field(row, ("venue_class",), source)
    utc_text = _text_field(
        row,
        ("event_utc", "utc", "event_time"),
        source,
    )
    label_text = _text_field(row, ("label", "event_label"), source)
    assert symbol is not None
    assert venue_class is not None
    assert utc_text is not None
    assert label_text is not None
    event_utc = _utc_datetime(utc_text, f"{source} event_utc")
    event_id = _text_field(row, ("event_id",), source, required=False)
    if event_id is None:
        event_id = f"{symbol}@{utc_text}"

    runner_score = _decimal_field(
        row,
        ("runner_score", "signal_score", "detector_score", "score"),
        source,
    )
    entry_latency = _decimal_field(
        row,
        ("entry_latency_min", "entry_latency_minutes", "entry_delay_min"),
        source,
        non_negative=True,
    )
    mfe = _decimal_field(
        row,
        (
            "mfe_pct",
            "maximum_favourable_excursion_pct",
            "maximum_favorable_excursion_pct",
        ),
        source,
    )
    mae = _decimal_field(
        row,
        ("mae_pct", "maximum_adverse_excursion_pct"),
        source,
    )
    time_to_peak = _decimal_field(
        row,
        ("time_to_peak_min", "time_to_peak_minutes"),
        source,
        non_negative=True,
    )
    gross_return = _decimal_field(
        row,
        ("gross_return_pct",),
        source,
    )
    supplied_net = _decimal_field(
        row,
        ("net_return_pct", "return_after_costs_pct"),
        source,
        required=False,
    )

    total_cost = _decimal_field(
        row,
        ("all_costs_pct", "total_cost_pct", "costs_pct"),
        source,
        required=False,
        non_negative=True,
    )
    fee = _decimal_field(
        row,
        ("fee_cost_pct", "fees_pct"),
        source,
        required=False,
        non_negative=True,
    )
    spread = _decimal_field(
        row,
        ("spread_cost_pct", "spread_pct"),
        source,
        required=False,
        non_negative=True,
    )
    slippage = _decimal_field(
        row,
        ("slippage_cost_pct", "slippage_pct"),
        source,
        required=False,
        non_negative=True,
    )
    components = (fee, spread, slippage)
    if total_cost is None and all(value is not None for value in components):
        total_cost = sum(
            (value for value in components if value is not None), Decimal("0")
        )
    elif any(value is not None for value in components):
        if any(value is None for value in components):
            raise ValidationError(
                f"{source} does not prove fee, spread, and slippage costs"
            )
        component_total = sum(
            (value for value in components if value is not None),
            Decimal("0"),
        )
        if component_total != total_cost:
            raise ValidationError(
                f"{source} cost components do not equal total costs"
            )

    usd_names = (
        "fee_cost_usdt",
        "spread_cost_usdt",
        "slippage_cost_usdt",
        "total_cost_usdt",
        "allocation_used_usdt",
    )
    usd_cost_evidence = any(name in row for name in usd_names)
    if total_cost is None and usd_cost_evidence:
        fee_usdt = _decimal_field(
            row, ("fee_cost_usdt",), source, non_negative=True
        )
        spread_usdt = _decimal_field(
            row, ("spread_cost_usdt",), source, non_negative=True
        )
        slippage_usdt = _decimal_field(
            row, ("slippage_cost_usdt",), source, non_negative=True
        )
        total_usdt = _decimal_field(
            row, ("total_cost_usdt",), source, non_negative=True
        )
        allocation_usdt = _decimal_field(
            row, ("allocation_used_usdt",), source, non_negative=True
        )
        assert fee_usdt is not None
        assert spread_usdt is not None
        assert slippage_usdt is not None
        assert total_usdt is not None
        assert allocation_usdt is not None
        if allocation_usdt <= 0:
            raise ValidationError(f"{source} allocation used is not positive")
        if fee_usdt + spread_usdt + slippage_usdt != total_usdt:
            raise ValidationError(
                f"{source} USDT cost components do not equal total costs"
            )
        gross_pnl = _decimal_field(
            row, ("gross_pnl_usdt",), source, required=False
        )
        net_pnl = _decimal_field(
            row, ("net_pnl_usdt",), source, required=False
        )
        if (gross_pnl is None) != (net_pnl is None):
            raise ValidationError(
                f"{source} has incomplete USDT PnL reconciliation"
            )
        if (
            gross_pnl is not None
            and net_pnl is not None
            and gross_pnl - total_usdt != net_pnl
        ):
            raise ValidationError(
                f"{source} USDT net PnL does not reconcile with costs"
            )
        if supplied_net is None:
            with localcontext() as context:
                context.prec = 50
                total_cost = total_usdt / allocation_usdt * HUNDRED
        else:
            total_cost = gross_return - supplied_net
            if total_cost < 0:
                raise ValidationError(
                    f"{source} net return exceeds gross return"
                )

    if total_cost is None:
        raise ValidationError(
            f"{source} does not prove fee, spread, and slippage costs"
        )

    assert runner_score is not None
    assert entry_latency is not None
    assert mfe is not None
    assert mae is not None
    assert time_to_peak is not None
    assert gross_return is not None
    assert total_cost is not None
    derived_net = gross_return - total_cost
    if supplied_net is not None and supplied_net != derived_net:
        raise ValidationError(
            f"{source} net return does not reconcile with all costs"
        )
    net_return = derived_net if supplied_net is None else supplied_net

    return ValidationRecord(
        event_id=event_id,
        symbol=symbol,
        venue_class=venue_class,
        event_utc=event_utc,
        label=_canonical_label(label_text, source),
        runner_score=runner_score,
        entry_latency_min=entry_latency,
        mfe_pct=mfe,
        mae_pct=mae,
        time_to_peak_min=time_to_peak,
        gross_return_pct=gross_return,
        all_costs_pct=total_cost,
        net_return_pct=net_return,
        source_order=source_order,
        policy=policy,
    )


def load_trades(
    path: Path,
    *,
    event_evidence: Optional[Mapping[str, EventEvidence]] = None,
) -> Tuple[ValidationRecord, ...]:
    """Load and reconcile policy-simulation rows."""
    path = Path(path)
    records: List[ValidationRecord] = []
    seen = set()
    for line_number, row in _read_json_lines(path):
        source = f"{path.name}:{line_number}"
        if row.get("kind") == "summary":
            continue
        parsed_row: Mapping[str, Any] = row
        if event_evidence is not None:
            raw_id = row.get("event_id")
            if type(raw_id) is not str or not raw_id:
                raise ValidationError(f"{source} has no event_id for Stage A")
            event = event_evidence.get(raw_id)
            if event is None:
                raise ValidationError(
                    f"{source} event_id is absent from Stage A evidence"
                )
            parsed_row = _merge_event_evidence(row, event, source)
        elif "status" in row and row.get("status") != "CLOSED":
            raise ValidationError(f"{source} policy result is not CLOSED")
        record = _record_from_row(parsed_row, source, line_number)
        if record.event_id in seen:
            raise ValidationError(
                f"{source} duplicates event_id {record.event_id}"
            )
        seen.add(record.event_id)
        records.append(record)
    if not records:
        raise ValidationError("trades input is empty")
    policies = [record.policy for record in records]
    if event_evidence is not None and any(policy is None for policy in policies):
        raise ValidationError("policy_sim rows do not contain a complete policy")
    present_policies = [policy for policy in policies if policy is not None]
    if present_policies and any(
        policy != present_policies[0] for policy in present_policies[1:]
    ):
        raise ValidationError("trades input mixes different policies")
    return tuple(records)


def split_cohort_tokens(
    cohort: Sequence[CohortMember],
    *,
    seed: str = DEFAULT_SPLIT_SEED,
) -> TokenSplit:
    """Create deterministic, venue-stratified 70/30 token partitions."""
    if type(seed) is not str or not seed:
        raise ValidationError("split seed is empty")
    by_venue: Dict[str, List[str]] = {
        venue_class: [] for venue_class in VENUE_CLASSES
    }
    seen = set()
    for member in cohort:
        if not isinstance(member, CohortMember):
            raise ValidationError("cohort contains a non-CohortMember")
        if member.symbol in seen:
            raise ValidationError(f"cohort duplicates {member.symbol}")
        seen.add(member.symbol)
        by_venue[member.venue_class].append(member.symbol)

    fit_by_venue: Dict[str, Tuple[str, ...]] = {}
    score_by_venue: Dict[str, Tuple[str, ...]] = {}
    for venue_class in VENUE_CLASSES:
        symbols = by_venue[venue_class]
        if len(symbols) < 2:
            raise ValidationError(
                f"{venue_class} needs at least two cohort tokens"
            )

        def digest_key(symbol: str) -> Tuple[str, str]:
            payload = f"{seed}\0{venue_class}\0{symbol}".encode("utf-8")
            return hashlib.sha256(payload).hexdigest(), symbol

        ranked = sorted(symbols, key=digest_key)
        fit_count = (
            len(ranked) * TOKEN_FIT_NUMERATOR // TOKEN_SPLIT_DENOMINATOR
        )
        if fit_count <= 0 or fit_count >= len(ranked):
            raise ValidationError(
                f"{venue_class} cannot form non-empty 70/30 partitions"
            )
        fit_by_venue[venue_class] = tuple(sorted(ranked[:fit_count]))
        score_by_venue[venue_class] = tuple(sorted(ranked[fit_count:]))

    fit_tokens = frozenset(
        symbol
        for venue_class in VENUE_CLASSES
        for symbol in fit_by_venue[venue_class]
    )
    score_tokens = frozenset(
        symbol
        for venue_class in VENUE_CLASSES
        for symbol in score_by_venue[venue_class]
    )
    if fit_tokens & score_tokens:
        raise ValidationError("fit and score token partitions overlap")
    if fit_tokens | score_tokens != seen:
        raise ValidationError("token partitions do not cover the cohort")
    return TokenSplit(
        fit_tokens,
        score_tokens,
        fit_by_venue,
        score_by_venue,
    )


def make_time_split(window_start: date) -> TimeSplit:
    """Create consecutive 20-day fit and 10-day score UTC windows."""
    if isinstance(window_start, datetime) or not isinstance(window_start, date):
        raise ValidationError("window_start is not a date")
    days = tuple(
        window_start + timedelta(days=offset)
        for offset in range(TOTAL_DAY_COUNT)
    )
    fit_days = days[:FIT_DAY_COUNT]
    score_days = days[FIT_DAY_COUNT:]
    if set(fit_days) & set(score_days):
        raise ValidationError("fit and score time partitions overlap")
    return TimeSplit(fit_days, score_days)


def partition_records(
    records: Sequence[ValidationRecord],
    cohort: Sequence[CohortMember],
    token_split: TokenSplit,
    time_split: TimeSplit,
    *,
    outcome_horizon_min: int = LABEL_OUTCOME_HORIZON_MIN,
) -> RecordPartitions:
    """Keep only the double-fit and double-held-out intersections."""
    if (
        isinstance(outcome_horizon_min, bool)
        or not isinstance(outcome_horizon_min, int)
        or outcome_horizon_min <= 0
    ):
        raise ValidationError("outcome horizon must be positive minutes")
    cohort_classes = {member.symbol: member.venue_class for member in cohort}
    if len(cohort_classes) != len(cohort):
        raise ValidationError("cohort contains duplicate symbols")
    fit_days = frozenset(time_split.fit_days)
    score_days = frozenset(time_split.score_days)
    if fit_days & score_days:
        raise ValidationError("fit and score days overlap")
    if token_split.fit_tokens & token_split.score_tokens:
        raise ValidationError("fit and score tokens overlap")

    fit: List[ValidationRecord] = []
    score: List[ValidationRecord] = []
    excluded: List[ValidationRecord] = []
    event_ids = set()
    fit_end = datetime.combine(
        time_split.score_days[0], datetime.min.time(), timezone.utc
    )
    score_end = datetime.combine(
        time_split.score_days[-1] + timedelta(days=1),
        datetime.min.time(),
        timezone.utc,
    )
    outcome_delta = timedelta(minutes=outcome_horizon_min)
    for record in records:
        if not isinstance(record, ValidationRecord):
            raise ValidationError("records contain a non-ValidationRecord")
        if record.event_id in event_ids:
            raise ValidationError(f"records duplicate {record.event_id}")
        event_ids.add(record.event_id)
        expected_class = cohort_classes.get(record.symbol)
        if expected_class is None:
            raise ValidationError(
                f"{record.event_id} symbol is absent from the full cohort"
            )
        if record.venue_class != expected_class:
            raise ValidationError(
                f"{record.event_id} venue_class conflicts with the cohort"
            )
        if (
            record.symbol in token_split.fit_tokens
            and record.event_day in fit_days
            and record.event_utc + outcome_delta < fit_end
        ):
            fit.append(record)
        elif (
            record.symbol in token_split.score_tokens
            and record.event_day in score_days
            and record.event_utc + outcome_delta < score_end
        ):
            score.append(record)
        else:
            excluded.append(record)

    sort_key = lambda item: (
        item.event_utc,
        item.event_id,
        item.source_order,
    )
    fit_records = tuple(sorted(fit, key=sort_key))
    score_records = tuple(sorted(score, key=sort_key))
    excluded_records = tuple(sorted(excluded, key=sort_key))
    if any(
        record.symbol not in token_split.fit_tokens
        or record.event_day not in fit_days
        or record.event_utc + outcome_delta >= fit_end
        for record in fit_records
    ):
        raise ValidationError("fit partition invariant failed")
    if any(
        record.symbol not in token_split.score_tokens
        or record.event_day not in score_days
        or record.event_utc + outcome_delta >= score_end
        for record in score_records
    ):
        raise ValidationError("score partition invariant failed")
    if {record.event_id for record in fit_records} & {
        record.event_id for record in score_records
    }:
        raise ValidationError("fit and score records overlap")
    return RecordPartitions(fit_records, score_records, excluded_records)


def _percentage(numerator: int, denominator: int) -> Optional[Decimal]:
    if denominator == 0:
        return None
    with localcontext() as context:
        context.prec = 50
        return Decimal(numerator) * HUNDRED / Decimal(denominator)


def select_runner_score_threshold(
    fit_records: Sequence[ValidationRecord],
) -> Optional[Decimal]:
    """Select one runner-score threshold using fit labels only."""
    if not fit_records:
        return None
    scores: List[Decimal] = []
    for record in fit_records:
        if not isinstance(record, ValidationRecord):
            raise ValidationError("threshold fit contains an invalid record")
        scores.append(record.runner_score)
    candidates = sorted(set(scores), reverse=True)
    candidates.append(max(scores) + Decimal("1"))

    best_threshold: Optional[Decimal] = None
    best_quality: Optional[Tuple[Decimal, Decimal, Decimal, Decimal, Decimal]] = (
        None
    )
    for threshold in candidates:
        true_positive = sum(
            record.runner_score >= threshold and record.genuine_runner
            for record in fit_records
        )
        false_positive = sum(
            record.runner_score >= threshold and not record.genuine_runner
            for record in fit_records
        )
        false_negative = sum(
            record.runner_score < threshold and record.genuine_runner
            for record in fit_records
        )
        f1_denominator = (
            Decimal(2 * true_positive + false_positive + false_negative)
        )
        with localcontext() as context:
            context.prec = 50
            f1 = (
                Decimal("0")
                if f1_denominator == 0
                else Decimal(2 * true_positive) / f1_denominator
            )
            precision = (
                Decimal("0")
                if true_positive + false_positive == 0
                else Decimal(true_positive)
                / Decimal(true_positive + false_positive)
            )
            recall = (
                Decimal("0")
                if true_positive + false_negative == 0
                else Decimal(true_positive)
                / Decimal(true_positive + false_negative)
            )
        quality = (
            f1,
            precision,
            recall,
            -Decimal(false_positive),
            threshold,
        )
        if best_quality is None or quality > best_quality:
            best_quality = quality
            best_threshold = threshold
    return best_threshold


def fit_thresholds(
    partitions: RecordPartitions,
) -> Dict[str, Optional[Decimal]]:
    """Fit venue thresholds without accepting any score-partition argument."""
    return {
        venue_class: select_runner_score_threshold(
            tuple(
                record
                for record in partitions.fit_records
                if record.venue_class == venue_class
            )
        )
        for venue_class in VENUE_CLASSES
    }


def _mean(values: Sequence[Decimal]) -> Optional[Decimal]:
    if not values:
        return None
    with localcontext() as context:
        context.prec = 50
        return sum(values, Decimal("0")) / Decimal(len(values))


def _median(values: Sequence[Decimal]) -> Optional[Decimal]:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    with localcontext() as context:
        context.prec = 50
        return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _distribution(values: Sequence[Decimal]) -> Dict[str, Any]:
    return {
        "median": _median(values),
        "mean": _mean(values),
        "minimum": min(values) if values else None,
        "maximum": max(values) if values else None,
    }


def _compounded_return_pct(values: Sequence[Decimal]) -> Optional[Decimal]:
    if not values:
        return None
    with localcontext() as context:
        context.prec = 50
        equity = Decimal("1")
        for value in values:
            equity *= Decimal("1") + value / HUNDRED
        return (equity - Decimal("1")) * HUNDRED


def maximum_drawdown_pct(values: Sequence[Decimal]) -> Optional[Decimal]:
    """Return positive peak-to-trough drawdown on compounded net returns."""
    if not values:
        return None
    with localcontext() as context:
        context.prec = 50
        equity = Decimal("1")
        peak = Decimal("1")
        maximum = Decimal("0")
        for value in values:
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValidationError("drawdown input is not finite Decimal")
            if value < Decimal("-100"):
                raise ValidationError("drawdown input is below minus 100")
            equity *= Decimal("1") + value / HUNDRED
            if equity > peak:
                peak = equity
            drawdown = (peak - equity) * HUNDRED / peak
            if drawdown > maximum:
                maximum = drawdown
        return maximum


def _tail_summary(
    records: Sequence[ValidationRecord], *, winner: bool
) -> Dict[str, Any]:
    if not records:
        selected: List[ValidationRecord] = []
    else:
        tail_size = max(1, (len(records) + 19) // 20)
        ordered = sorted(
            records,
            key=lambda record: (
                record.net_return_pct,
                record.event_utc,
                record.event_id,
            ),
            reverse=winner,
        )
        selected = ordered[:tail_size]
        if winner:
            selected = [
                record for record in selected if record.net_return_pct > 0
            ]
        else:
            selected = [
                record for record in selected if record.net_return_pct < 0
            ]
    values = [record.net_return_pct for record in selected]
    cutoff: Optional[Decimal]
    extreme: Optional[Decimal]
    if not values:
        cutoff = None
        extreme = None
    elif winner:
        cutoff = min(values)
        extreme = max(values)
    else:
        cutoff = max(values)
        extreme = min(values)
    return {
        "count": len(selected),
        "event_ids": [record.event_id for record in selected],
        "cutoff_net_return_pct": cutoff,
        "extreme_net_return_pct": extreme,
        "mean_net_return_pct": _mean(values),
        "sum_net_return_pct": (
            sum(values, Decimal("0")) if values else None
        ),
    }


def _calculate_metrics(
    records: Sequence[ValidationRecord],
    detected: Callable[[ValidationRecord], bool],
    *,
    score_day_count: int,
) -> Dict[str, Any]:
    if (
        isinstance(score_day_count, bool)
        or not isinstance(score_day_count, int)
        or score_day_count <= 0
    ):
        raise ValidationError("score_day_count must be a positive integer")
    ordered = tuple(
        sorted(
            records,
            key=lambda record: (
                record.event_utc,
                record.event_id,
                record.source_order,
            ),
        )
    )
    detected_records = tuple(record for record in ordered if detected(record))
    genuine_runner_count = sum(record.genuine_runner for record in ordered)
    true_positives = sum(
        record.genuine_runner for record in detected_records
    )
    false_positives = len(detected_records) - true_positives
    false_negatives = genuine_runner_count - true_positives
    with localcontext() as context:
        context.prec = 50
        events_per_day = Decimal(len(detected_records)) / Decimal(
            score_day_count
        )

    latency = [record.entry_latency_min for record in detected_records]
    mfe = [record.mfe_pct for record in detected_records]
    mae = [record.mae_pct for record in detected_records]
    time_to_peak = [record.time_to_peak_min for record in detected_records]
    gross = [record.gross_return_pct for record in detected_records]
    costs = [record.all_costs_pct for record in detected_records]
    net = [record.net_return_pct for record in detected_records]
    return {
        "score_events": len(ordered),
        "events_detected": len(detected_records),
        "events_detected_per_day": events_per_day,
        "genuine_runner_events": genuine_runner_count,
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "runner_precision_pct": _percentage(
            true_positives, len(detected_records)
        ),
        "runner_recall_pct": _percentage(
            true_positives, genuine_runner_count
        ),
        "entry_latency_min": _distribution(latency),
        "maximum_favourable_excursion_pct": _distribution(mfe),
        "maximum_adverse_excursion_pct": _distribution(mae),
        "time_to_peak_min": _distribution(time_to_peak),
        "median_gross_return_pct": _median(gross),
        "mean_gross_return_pct": _mean(gross),
        "tail_winners": _tail_summary(detected_records, winner=True),
        "tail_losses": _tail_summary(detected_records, winner=False),
        "all_costs_pct": {
            **_distribution(costs),
            "sum": sum(costs, Decimal("0")) if costs else None,
        },
        "net_return_after_all_costs_pct": {
            "median": _median(net),
            "mean": _mean(net),
            "sum": sum(net, Decimal("0")) if net else None,
            "compounded": _compounded_return_pct(net),
        },
        "maximum_drawdown_pct": maximum_drawdown_pct(net),
    }


def calculate_metrics(
    records: Sequence[ValidationRecord],
    threshold: Optional[Decimal],
    *,
    score_day_count: int = SCORE_DAY_COUNT,
) -> Dict[str, Any]:
    """Calculate commander metrics at one already-fitted threshold."""
    if threshold is not None and (
        not isinstance(threshold, Decimal) or not threshold.is_finite()
    ):
        raise ValidationError("threshold is not a finite Decimal")
    return _calculate_metrics(
        records,
        lambda record: (
            threshold is not None and record.runner_score >= threshold
        ),
        score_day_count=score_day_count,
    )


def _machine_value(value: Any) -> Any:
    """Convert every numeric output to Decimal text for JSON emission."""
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return decimal_text(Decimal(value))
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _machine_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_machine_value(item) for item in value]
    raise ValidationError(
        f"result contains unsupported value type {type(value).__name__}"
    )


def build_validation_result(
    records: Sequence[ValidationRecord],
    cohort: Sequence[CohortMember],
    window_start: date,
    repro_command: str,
    *,
    split_seed: str = DEFAULT_SPLIT_SEED,
    runner_score_source: str = "record.runner_score",
    excursion_horizon_min: int = DEFAULT_EXCURSION_HORIZON_MIN,
) -> Dict[str, Any]:
    """Fit on one intersection and score exclusively on its double holdout."""
    if type(repro_command) is not str or not repro_command:
        raise ValidationError("repro command is empty")
    if type(runner_score_source) is not str or not runner_score_source:
        raise ValidationError("runner score source is empty")
    if (
        isinstance(excursion_horizon_min, bool)
        or not isinstance(excursion_horizon_min, int)
        or excursion_horizon_min <= 0
    ):
        raise ValidationError("excursion horizon must be positive minutes")
    policies = [record.policy for record in records]
    present_policies = [policy for policy in policies if policy is not None]
    if present_policies and len(present_policies) != len(policies):
        raise ValidationError(
            "validation records mix proved and absent policies"
        )
    if present_policies and any(
        policy != present_policies[0] for policy in present_policies[1:]
    ):
        raise ValidationError("validation records mix different policies")
    selected_policy = present_policies[0] if present_policies else None
    token_split = split_cohort_tokens(cohort, seed=split_seed)
    time_split = make_time_split(window_start)
    partitions = partition_records(
        records,
        cohort,
        token_split,
        time_split,
        outcome_horizon_min=LABEL_OUTCOME_HORIZON_MIN,
    )
    thresholds = fit_thresholds(partitions)
    results_by_venue: Dict[str, Dict[str, Any]] = {}
    for venue_class in VENUE_CLASSES:
        score_records = tuple(
            record
            for record in partitions.score_records
            if record.venue_class == venue_class
        )
        metrics = calculate_metrics(
            score_records,
            thresholds[venue_class],
            score_day_count=len(time_split.score_days),
        )
        metrics["selected_runner_score_threshold"] = thresholds[venue_class]
        results_by_venue[venue_class] = metrics

    overall = _calculate_metrics(
        partitions.score_records,
        lambda record: (
            thresholds[record.venue_class] is not None
            and record.runner_score >= thresholds[record.venue_class]
        ),
        score_day_count=len(time_split.score_days),
    )
    mean_net = overall["net_return_after_all_costs_pct"]["mean"]
    positive_expectancy = None if mean_net is None else mean_net > 0
    raw_result = {
        "schema": "volume_dataset_v2_result",
        "schema_version": Decimal("2"),
        "repro_command": repro_command,
        "policy": selected_policy,
        "validation": {
            "metric_source": (
                "score_tokens_x_score_days_intersection_only"
            ),
            "time_split": {
                "window_start_utc_day": time_split.fit_days[0],
                "window_end_utc_day": time_split.score_days[-1],
                "fit_day_count": len(time_split.fit_days),
                "score_day_count": len(time_split.score_days),
                "fit_days": time_split.fit_days,
                "score_days": time_split.score_days,
                "label_outcome_horizon_min": LABEL_OUTCOME_HORIZON_MIN,
                "boundary_rule": (
                    "event_plus_outcome_horizon_strictly_before_partition_end"
                ),
            },
            "token_split": {
                "method": "sha256_seeded_rank_within_venue_class",
                "seed": split_seed,
                "fit_ratio": Decimal("0.70"),
                "score_ratio": Decimal("0.30"),
                "fit_tokens_by_venue_class": token_split.fit_by_venue,
                "score_tokens_by_venue_class": token_split.score_by_venue,
            },
            "fit_record_count": len(partitions.fit_records),
            "score_record_count": len(partitions.score_records),
            "excluded_cross_quadrant_boundary_or_outside_record_count": len(
                partitions.excluded_records
            ),
            "fit_score_token_intersection": sorted(
                token_split.fit_tokens & token_split.score_tokens
            ),
            "fit_score_day_intersection": sorted(
                set(time_split.fit_days) & set(time_split.score_days)
            ),
            "zero_leakage": True,
        },
        "threshold_selection": {
            "scope": "per_venue_class",
            "fit_records_only": True,
            "runner_score_source": runner_score_source,
            "runner_score_timing": "pre_move_only",
            "objective": (
                "max_f1_then_precision_then_recall_then_fewer_false_"
                "positives_then_higher_threshold"
            ),
            "no_fit_events_behavior": "detect_none",
        },
        "tail_definition": (
            "best_and_worst_5pct_of_detected_net_returns_minimum_one_"
            "restricted_to_positive_winners_or_negative_losses"
        ),
        "excursion_horizon_min": excursion_horizon_min,
        "drawdown_definition": (
            "maximum_peak_to_trough_pct_on_compounded_net_returns_in_"
            "event_time_order"
        ),
        "results_by_venue_class": results_by_venue,
        "all_venues": overall,
        "verdict": {
            "positive_out_of_sample_expectancy_after_costs": (
                positive_expectancy
            ),
            "basis": (
                "mean_net_return_after_all_costs_pct_on_doubly_held_out_"
                "detected_events"
            ),
        },
    }
    return _machine_value(raw_result)


def write_result(path: Path, result: Mapping[str, Any]) -> None:
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(result, sort_keys=True, indent=2, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError) as error:
        raise ValidationError(f"cannot write result {path}: {error}") from None


def _window_start(raw: str) -> date:
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "window start must be YYYY-MM-DD"
        ) from None
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fit Volume Dataset v2 thresholds on 20d x 70% tokens and "
            "score only the held-out 10d x disjoint 30% tokens."
        )
    )
    parser.add_argument(
        "--cohort-manifest",
        type=Path,
        default=DEFAULT_COHORT_PATH,
        help="full-universe cohort_manifest.json",
    )
    parser.add_argument(
        "--trades",
        type=Path,
        default=DEFAULT_TRADES_PATH,
        help="policy_sim trades.jsonl with latency and complete economics",
    )
    parser.add_argument(
        "--events",
        type=Path,
        default=DEFAULT_EVENTS_PATH,
        help="Stage A events.jsonl with labels and event-path evidence",
    )
    parser.add_argument(
        "--runner-score-path",
        default=DEFAULT_RUNNER_SCORE_PATH,
        help="dot path to one Decimal pre_state feature fitted on fit rows",
    )
    parser.add_argument(
        "--excursion-horizon-min",
        type=int,
        choices=(5, 15, 60),
        default=DEFAULT_EXCURSION_HORIZON_MIN,
        help="Stage A horizon for MFE, MAE, and time-to-peak",
    )
    parser.add_argument(
        "--window-start",
        type=_window_start,
        required=True,
        help="first UTC day of the exact 30-calendar-day window",
    )
    parser.add_argument(
        "--split-seed",
        default=DEFAULT_SPLIT_SEED,
        help="public deterministic token-split seed",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="machine-readable v2_result.json path",
    )
    return parser


def exact_repro_command(args: argparse.Namespace) -> str:
    """Render a complete repo-root command with every resolved input."""
    command = (
        "python3",
        REPRO_SCRIPT,
        "--cohort-manifest",
        str(args.cohort_manifest),
        "--trades",
        str(args.trades),
        "--events",
        str(args.events),
        "--runner-score-path",
        args.runner_score_path,
        "--excursion-horizon-min",
        str(args.excursion_horizon_min),
        "--window-start",
        args.window_start.isoformat(),
        "--split-seed",
        args.split_seed,
        "--output",
        str(args.output),
    )
    return shlex.join(command)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = exact_repro_command(args)
    try:
        cohort = load_cohort(args.cohort_manifest)
        event_evidence = load_event_evidence(
            args.events,
            runner_score_path=args.runner_score_path,
            excursion_horizon_min=args.excursion_horizon_min,
        )
        records = load_trades(
            args.trades,
            event_evidence=event_evidence,
        )
        result = build_validation_result(
            records,
            cohort,
            args.window_start,
            command,
            split_seed=args.split_seed,
            runner_score_source=args.runner_score_path,
            excursion_horizon_min=args.excursion_horizon_min,
        )
        write_result(args.output, result)
    except ValidationError as error:
        print(f"VALIDATE | REFUSED | {error}", file=sys.stderr)
        return 2
    print(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
