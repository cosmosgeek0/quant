#!/usr/bin/env python3
"""Build deterministic runner and matched-control tape windows offline.

The input is Stage A's ``events.jsonl``.  Each row must identify a symbol,
UTC event time, label, venue class, dead-regime state, and the token's own
30-day volume percentile.  GenuineRunner rows are treated observations;
non-runner rows are potential controls.  A row with ``is_event: true`` is an
exclusion event rather than a control candidate, while a row without that
optional field excludes controls only when it is a GenuineRunner.

No network or execution module is imported.  Percentiles and their matching
distance are Decimal values, and every Decimal written to JSON is text.
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)


STUDY_DIR = Path(__file__).resolve().parent
DEFAULT_EVENTS_PATH = STUDY_DIR / "out" / "v2" / "events.jsonl"
DEFAULT_OUTPUT_PATH = STUDY_DIR / "out" / "v2" / "windows_manifest.json"
RUNNER_LABEL = "GenuineRunner"
VENUE_CLASSES = ("spot_only", "futures_only", "both")
WINDOW_BEFORE_MINUTES = 60
WINDOW_AFTER_MINUTES = 120
CONTROL_EXCLUSION_HOURS = 24
VOLUME_PERCENTILE_BAND = Decimal("10")
MILLISECONDS_PER_MINUTE = 60_000
MILLISECONDS_PER_HOUR = 3_600_000
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MAX_SYMBOL_LENGTH = 64
NESTED_STATE_KEYS = ("pre_state", "features", "state")


class WindowError(Exception):
    """The event input or requested manifest cannot be proved valid."""


@dataclass(frozen=True)
class Event:
    """One validated Stage A event or explicit control-candidate row."""

    event_id: str
    symbol: str
    timestamp_ms: int
    labels: Tuple[str, ...]
    venue_class: str
    dead_regime: bool
    volume_percentile: Decimal
    blocks_controls: bool
    source_line: int

    @property
    def is_runner(self) -> bool:
        return RUNNER_LABEL in self.labels


def decimal_text(value: Decimal) -> str:
    """Return finite Decimal text without exponent notation."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise WindowError("cannot format a non-finite Decimal")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered in {"", "-0"}:
        return "0"
    return rendered


def utc_text(timestamp_ms: int) -> str:
    """Render epoch milliseconds as canonical UTC text."""
    value = EPOCH + timedelta(milliseconds=timestamp_ms)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _values(row: Mapping[str, Any], names: Sequence[str]) -> List[Any]:
    found: List[Any] = []
    for name in names:
        if name in row:
            found.append(row[name])
    for container_name in NESTED_STATE_KEYS:
        container = row.get(container_name)
        if isinstance(container, Mapping):
            for name in names:
                if name in container:
                    found.append(container[name])
    return found


def _single_value(
    row: Mapping[str, Any], names: Sequence[str], source: str
) -> Any:
    values = _values(row, names)
    if not values:
        raise WindowError(f"{source} is missing {names[0]}")
    first = values[0]
    if any(value != first for value in values[1:]):
        raise WindowError(f"{source} has conflicting {names[0]} values")
    return first


def _timestamp_value(raw: Any, source: str) -> int:
    if isinstance(raw, bool):
        raise WindowError(f"{source} is not a UTC timestamp")
    if isinstance(raw, int) or (
        isinstance(raw, str) and raw.strip().isdigit()
    ):
        value = int(raw)
        if value < 100_000_000_000 or value >= 100_000_000_000_000:
            raise WindowError(f"{source} is outside the epoch-ms range")
        return value
    if not isinstance(raw, str) or not raw.strip():
        raise WindowError(f"{source} is not a UTC timestamp")
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise WindowError(f"{source} is not an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WindowError(f"{source} has no UTC offset")
    parsed = parsed.astimezone(timezone.utc)
    if parsed.microsecond % 1_000:
        raise WindowError(f"{source} is more precise than milliseconds")
    delta = parsed - EPOCH
    return (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )


def _timestamp(row: Mapping[str, Any], source: str) -> int:
    raw_values = _values(
        row,
        (
            "event_time_ms",
            "timestamp_ms",
            "minute_ms",
            "open_ms",
            "event_time",
            "timestamp",
            "minute_utc",
            "utc",
        ),
    )
    if not raw_values:
        raise WindowError(f"{source} is missing event_time")
    parsed = [_timestamp_value(value, f"{source} event_time") for value in raw_values]
    if any(value != parsed[0] for value in parsed[1:]):
        raise WindowError(f"{source} has conflicting event_time values")
    return parsed[0]


def _labels(row: Mapping[str, Any], source: str) -> Tuple[str, ...]:
    labels: List[str] = []
    for key in ("label", "event_label"):
        if key in row:
            value = row[key]
            if not isinstance(value, str) or not value:
                raise WindowError(f"{source} {key} is not a non-empty string")
            labels.append(value)
    if "labels" in row:
        value = row["labels"]
        if isinstance(value, str):
            labels.append(value)
        elif isinstance(value, (list, tuple)):
            for label in value:
                if not isinstance(label, str) or not label:
                    raise WindowError(f"{source} labels contains an invalid label")
                labels.append(label)
        elif isinstance(value, Mapping):
            for label, enabled in value.items():
                if not isinstance(label, str) or not label:
                    raise WindowError(f"{source} labels contains an invalid label")
                if not isinstance(enabled, bool):
                    raise WindowError(f"{source} labels flags must be booleans")
                if enabled:
                    labels.append(label)
        else:
            raise WindowError(f"{source} labels has an invalid type")
    normalized = tuple(sorted(set(labels)))
    if not normalized:
        raise WindowError(f"{source} has no label")
    return normalized


def _decimal(raw: Any, source: str) -> Decimal:
    if isinstance(raw, bool) or isinstance(raw, float):
        raise WindowError(f"{source} is not Decimal text")
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise WindowError(f"{source} is not a Decimal") from None
    if not value.is_finite() or value < 0 or value > 100:
        raise WindowError(f"{source} is outside percentile range 0..100")
    return value


def _symbol(raw: Any, source: str) -> str:
    """Return a normalized symbol that is safe as a filename component."""
    if not isinstance(raw, str):
        raise WindowError(f"{source} is not a string")
    symbol = raw.upper()
    try:
        encoded = symbol.encode("utf-8")
    except UnicodeEncodeError:
        raise WindowError(f"{source} is invalid") from None
    if (
        not symbol
        or len(symbol) > MAX_SYMBOL_LENGTH
        or len(encoded) > MAX_SYMBOL_LENGTH
        or not symbol.isprintable()
        or any(character.isspace() for character in symbol)
        or symbol == "."
        or ".." in symbol
        or any(character in symbol for character in ("/", "\\", ":"))
    ):
        raise WindowError(f"{source} is invalid")
    return symbol


def parse_event(row: Mapping[str, Any], line_number: int) -> Event:
    """Validate and normalize one input mapping."""
    source = f"events.jsonl line {line_number}"
    if not isinstance(row, Mapping):
        raise WindowError(f"{source} is not a JSON object")
    raw_symbol = _single_value(row, ("symbol",), source)
    symbol = _symbol(raw_symbol, f"{source} symbol")

    raw_venue = _single_value(row, ("venue_class",), source)
    if not isinstance(raw_venue, str) or raw_venue not in VENUE_CLASSES:
        raise WindowError(f"{source} venue_class is invalid")

    raw_dead = _single_value(row, ("dead_regime",), source)
    if not isinstance(raw_dead, bool):
        raise WindowError(f"{source} dead_regime is not a boolean")

    raw_percentile = _single_value(
        row,
        (
            "volume_percentile_30d",
            "volume_percentile",
            "volume_percentile_pct",
        ),
        source,
    )
    percentile = _decimal(raw_percentile, f"{source} volume_percentile")
    labels = _labels(row, source)
    timestamp_ms = _timestamp(row, source)

    raw_id = row.get("event_id", row.get("id"))
    if raw_id is None:
        event_id = f"event-{line_number:08d}"
    elif not isinstance(raw_id, str) or not raw_id.strip():
        raise WindowError(f"{source} event_id is not a non-empty string")
    else:
        event_id = raw_id.strip()

    # stage_a stamps is_event=True on every labelled row (an "event record"
    # marker); only an explicit blocks_controls field may widen blocking
    # beyond GenuineRunner rows. One field, two meanings was seam bug #1.
    explicit_event = row.get("blocks_controls")
    if explicit_event is not None and not isinstance(explicit_event, bool):
        raise WindowError(f"{source} blocks_controls is not a boolean")
    is_runner = RUNNER_LABEL in labels
    blocks_controls = is_runner if explicit_event is None else explicit_event
    if is_runner and not blocks_controls:
        raise WindowError(f"{source} GenuineRunner cannot set is_event false")

    return Event(
        event_id=event_id,
        symbol=symbol,
        timestamp_ms=timestamp_ms,
        labels=labels,
        venue_class=raw_venue,
        dead_regime=raw_dead,
        volume_percentile=percentile,
        blocks_controls=blocks_controls,
        source_line=line_number,
    )


def load_events(path: Path = DEFAULT_EVENTS_PATH) -> Tuple[Event, ...]:
    """Read and validate a JSONL event file."""
    source_path = Path(path)
    if not source_path.is_file():
        raise WindowError(f"events file does not exist: {source_path}")
    events: List[Event] = []
    try:
        with open(source_path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    raise WindowError(
                        f"events.jsonl line {line_number} is invalid JSON"
                    ) from None
                events.append(parse_event(row, line_number))
    except (OSError, UnicodeDecodeError) as error:
        raise WindowError(f"cannot read events file {source_path}: {error}") from None
    if not events:
        raise WindowError("events file has no rows")
    identifiers: Set[str] = set()
    for event in events:
        if event.event_id in identifiers:
            raise WindowError(f"duplicate event_id: {event.event_id}")
        identifiers.add(event.event_id)
    return tuple(events)


def _event_order(event: Event) -> Tuple[int, str, str]:
    return event.timestamp_ms, event.symbol, event.event_id


def _clear_of_events(
    candidate: Event,
    blocking_times: Mapping[str, Sequence[int]],
) -> bool:
    limit = CONTROL_EXCLUSION_HOURS * MILLISECONDS_PER_HOUR
    times = blocking_times.get(candidate.symbol, ())
    insertion = bisect.bisect_left(times, candidate.timestamp_ms)
    neighbors = times[max(0, insertion - 1) : insertion + 1]
    return all(
        abs(candidate.timestamp_ms - timestamp_ms) > limit
        for timestamp_ms in neighbors
    )


def _eligible(runner: Event, candidate: Event) -> bool:
    return (
        runner.event_id != candidate.event_id
        and runner.source_line != candidate.source_line
        and not candidate.is_runner
        and not candidate.blocks_controls
        and runner.venue_class == candidate.venue_class
        and runner.dead_regime is candidate.dead_regime
        and abs(
            runner.volume_percentile - candidate.volume_percentile
        )
        <= VOLUME_PERCENTILE_BAND
    )


def match_controls(
    events: Sequence[Event],
) -> Tuple[
    Tuple[Event, ...],
    Tuple[Event, ...],
    Dict[int, int],
    Dict[int, int],
    int,
]:
    """Match the maximum unique controls within ordered caliper strata."""
    ordered = tuple(sorted(events, key=_event_order))
    runners = tuple(event for event in ordered if event.is_runner)
    candidates = tuple(
        event
        for event in ordered
        if not event.is_runner and not event.blocks_controls
    )
    blocking_times: Dict[str, List[int]] = {}
    for event in ordered:
        if event.blocks_controls:
            blocking_times.setdefault(event.symbol, []).append(event.timestamp_ms)
    for times in blocking_times.values():
        times.sort()
    clear_candidates = tuple(
        candidate
        for candidate in candidates
        if _clear_of_events(candidate, blocking_times)
    )
    excluded_count = len(candidates) - len(clear_candidates)

    runner_groups: Dict[Tuple[str, bool], List[int]] = {}
    candidate_groups: Dict[Tuple[str, bool], List[int]] = {}
    for runner_index, runner in enumerate(runners):
        runner_groups.setdefault(
            (runner.venue_class, runner.dead_regime), []
        ).append(runner_index)
    for candidate_index, candidate in enumerate(clear_candidates):
        candidate_groups.setdefault(
            (candidate.venue_class, candidate.dead_regime), []
        ).append(candidate_index)

    matching: Dict[int, int] = {}
    eligible_counts: Dict[int, int] = {
        runner_index: 0 for runner_index in range(len(runners))
    }
    strata = sorted(set(runner_groups) | set(candidate_groups))
    for stratum in strata:
        runner_indexes = sorted(
            runner_groups.get(stratum, ()),
            key=lambda index: (
                runners[index].volume_percentile,
                runners[index].timestamp_ms,
                runners[index].symbol,
                runners[index].event_id,
            ),
        )
        candidate_indexes = sorted(
            candidate_groups.get(stratum, ()),
            key=lambda index: (
                clear_candidates[index].volume_percentile,
                clear_candidates[index].timestamp_ms,
                clear_candidates[index].symbol,
                clear_candidates[index].event_id,
            ),
        )
        percentiles = [
            clear_candidates[index].volume_percentile
            for index in candidate_indexes
        ]
        for runner_index in runner_indexes:
            percentile = runners[runner_index].volume_percentile
            lower = bisect.bisect_left(
                percentiles, percentile - VOLUME_PERCENTILE_BAND
            )
            upper = bisect.bisect_right(
                percentiles, percentile + VOLUME_PERCENTILE_BAND
            )
            eligible_counts[runner_index] = upper - lower

        runner_offset = 0
        candidate_offset = 0
        while (
            runner_offset < len(runner_indexes)
            and candidate_offset < len(candidate_indexes)
        ):
            runner_index = runner_indexes[runner_offset]
            candidate_index = candidate_indexes[candidate_offset]
            runner = runners[runner_index]
            candidate = clear_candidates[candidate_index]
            if (
                candidate.volume_percentile
                < runner.volume_percentile - VOLUME_PERCENTILE_BAND
            ):
                candidate_offset += 1
                continue
            if (
                candidate.volume_percentile
                > runner.volume_percentile + VOLUME_PERCENTILE_BAND
            ):
                runner_offset += 1
                continue
            if not _eligible(runner, candidate):
                raise WindowError("internal control matching invariant failed")
            matching[runner_index] = candidate_index
            runner_offset += 1
            candidate_offset += 1

    return (
        runners,
        clear_candidates,
        matching,
        eligible_counts,
        excluded_count,
    )


def _venues(venue_class: str) -> List[str]:
    if venue_class == "spot_only":
        return ["spot"]
    if venue_class == "futures_only":
        return ["futures"]
    return ["spot", "futures"]


def _window(
    event: Event,
    kind: str,
    window_id: str,
    matched_event_id: Optional[str],
) -> Dict[str, Any]:
    start_ms = event.timestamp_ms - (
        WINDOW_BEFORE_MINUTES * MILLISECONDS_PER_MINUTE
    )
    end_ms = event.timestamp_ms + (
        WINDOW_AFTER_MINUTES * MILLISECONDS_PER_MINUTE
    )
    tape_venue = (
        "futures" if event.venue_class == "futures_only" else "spot"
    )
    return {
        "window_id": window_id,
        "kind": kind,
        "label": RUNNER_LABEL if kind == "runner" else "control",
        "event_id": event.event_id,
        "source_event_id": event.event_id,
        "matched_event_id": matched_event_id,
        "symbol": event.symbol,
        "venue_class": event.venue_class,
        "venue": tape_venue,
        "venues": _venues(event.venue_class),
        "dead_regime": event.dead_regime,
        "volume_percentile_30d": decimal_text(event.volume_percentile),
        "labels": list(event.labels),
        "event_ms": event.timestamp_ms,
        "event_utc": utc_text(event.timestamp_ms),
        "anchor_ms": event.timestamp_ms,
        "anchor_utc": utc_text(event.timestamp_ms),
        "window_start_ms": start_ms,
        "start_ms": start_ms,
        "start_utc": utc_text(start_ms),
        "window_end_ms": end_ms,
        "end_ms": end_ms,
        "end_utc": utc_text(end_ms),
    }


def build_windows_manifest(
    events: Sequence[Event], source_events: str = "events.jsonl"
) -> Dict[str, Any]:
    """Build the Stage B fetch manifest without writing it."""
    if not events:
        raise WindowError("cannot build a manifest from no events")
    if any(not isinstance(event, Event) for event in events):
        raise WindowError("manifest input contains an unvalidated event")
    identifiers = [event.event_id for event in events]
    if len(set(identifiers)) != len(identifiers):
        raise WindowError("manifest input has duplicate event_id values")

    (
        runners,
        candidates,
        matching,
        eligible_counts,
        excluded_count,
    ) = match_controls(events)
    windows: List[Dict[str, Any]] = []
    matches: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = []
    shortfall_by_venue: Dict[str, int] = {
        venue_class: 0 for venue_class in VENUE_CLASSES
    }
    shortfall_by_regime = {"true": 0, "false": 0}

    for runner_index, runner in enumerate(runners):
        pair_number = runner_index + 1
        runner_window_id = f"runner-{pair_number:08d}"
        candidate_index = matching.get(runner_index)
        control = None if candidate_index is None else candidates[candidate_index]
        windows.append(
            _window(
                runner,
                "runner",
                runner_window_id,
                None if control is None else control.event_id,
            )
        )
        if control is None:
            shortfall_by_venue[runner.venue_class] += 1
            shortfall_by_regime[str(runner.dead_regime).lower()] += 1
            unmatched.append(
                {
                    "runner_event_id": runner.event_id,
                    "eligible_control_count": eligible_counts[runner_index],
                    "reason": (
                        "no_eligible_control"
                        if not eligible_counts[runner_index]
                        else "no_unique_eligible_control"
                    ),
                }
            )
            continue
        control_window_id = f"control-{pair_number:08d}"
        windows.append(
            _window(
                control,
                "control",
                control_window_id,
                runner.event_id,
            )
        )
        matches.append(
            {
                "runner_event_id": runner.event_id,
                "control_event_id": control.event_id,
                "runner_window_id": runner_window_id,
                "control_window_id": control_window_id,
                "volume_percentile_delta": decimal_text(
                    abs(
                        runner.volume_percentile
                        - control.volume_percentile
                    )
                ),
            }
        )

    runner_count = len(runners)
    matched_count = len(matches)
    shortfall = runner_count - matched_count
    return {
        "schema": "volume-dataset-v2-windows-manifest-v1",
        "source": {
            "events_jsonl": source_events,
            "event_rows": len(events),
        },
        "parameters": {
            "runner_label": RUNNER_LABEL,
            "window_before_minutes": WINDOW_BEFORE_MINUTES,
            "window_after_minutes": WINDOW_AFTER_MINUTES,
            "volume_percentile_band": decimal_text(
                VOLUME_PERCENTILE_BAND
            ),
            "control_exclusion_hours": CONTROL_EXCLUSION_HOURS,
            "control_ratio_requested": "1:1",
            "control_reuse": False,
            "event_exclusion_scope": "same_symbol",
            "matching_order": "maximum_cardinality_percentile_order",
        },
        "counts": {
            "runner_events": runner_count,
            "runner_windows": runner_count,
            "control_candidates_clear": len(candidates),
            "control_candidates_excluded_near_event": excluded_count,
            "controls_requested": runner_count,
            "controls_matched": matched_count,
            "control_shortfall": shortfall,
            "windows_total": len(windows),
        },
        "shortfall_counts": {
            "total": shortfall,
            "by_venue_class": shortfall_by_venue,
            "by_dead_regime": shortfall_by_regime,
        },
        "matches": matches,
        "unmatched_runners": unmatched,
        "windows": windows,
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(
                dict(payload),
                handle,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as error:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise WindowError(f"cannot write window manifest {target}: {error}") \
            from None


def run_windows(
    events_path: Path = DEFAULT_EVENTS_PATH,
    output_path: Path = DEFAULT_OUTPUT_PATH,
) -> Dict[str, Any]:
    """Consume events.jsonl and atomically emit windows_manifest.json."""
    source = Path(events_path).resolve()
    target = Path(output_path)
    if source == target.resolve():
        raise WindowError("events input and manifest output are the same path")
    try:
        relative_source = os.path.relpath(source, target.parent.resolve())
    except ValueError as error:
        raise WindowError(f"cannot relate input and output paths: {error}") \
            from None
    events = load_events(source)
    payload = build_windows_manifest(events, relative_source)
    _write_json(target, payload)
    return payload


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build offline GenuineRunner and matched-control tape windows."
        )
    )
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = argument_parser().parse_args(argv)
    try:
        payload = run_windows(args.events, args.output)
    except WindowError as error:
        print(f"windows: {error}", file=sys.stderr)
        return 2
    counts = payload["counts"]
    print(
        "WINDOWS | "
        f"runners={counts['runner_windows']} | "
        f"controls={counts['controls_matched']}/"
        f"{counts['controls_requested']} | "
        f"shortfall={counts['control_shortfall']} | output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
