#!/usr/bin/env python3
"""Score replay ignitions against adaptive tape-derived baselines.

Filter v2 is intentionally offline and fail-closed.  It keeps the v1 repeat,
post-print persistence, range sanity, and time-of-day magnitude features, then
adds sustained hot-tape confirmation plus delay-by-magnitude forward-return
measurement.  It has no network or execution path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import baselines  # noqa: E402


STUDY_DIR = Path(__file__).resolve().parent
DEFAULT_SCORE_PATH = STUDY_DIR / "out" / "replay-2026-08-15.jsonl"
DEFAULT_BASELINES_PATH = STUDY_DIR / "out" / "baselines.json"
FORWARD_MINUTES = (5, 15, 60)
ENTRY_DELAYS_MINUTES = tuple(range(6))
DEFAULT_SUSTAINED_REQUIRED = 3
DEFAULT_SUSTAINED_WINDOW = 4
MAGNITUDE_BUCKETS: Tuple[
    Tuple[str, Decimal, Optional[Decimal]], ...
] = (
    ("1x_to_lt_2x", Decimal("1"), Decimal("2")),
    ("2x_to_lt_5x", Decimal("2"), Decimal("5")),
    ("5x_to_lt_10x", Decimal("5"), Decimal("10")),
    ("10x_plus", Decimal("10"), None),
)
SCORE_DATE = re.compile(r"^replay-(\d{4}-\d{2}-\d{2})\.jsonl$")
SYMBOL = re.compile(r"[A-Z0-9]{5,30}")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class FilterError(Exception):
    """Replay, baseline, tape, or output data cannot be proved valid."""


def _decimal(
    raw: Any,
    source: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    if type(raw) is not str or not raw.strip():
        raise FilterError(f"{source} is not Decimal text")
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        raise FilterError(f"{source} is not Decimal text") from None
    if not value.is_finite():
        raise FilterError(f"{source} is not a finite Decimal")
    if positive and value <= 0:
        raise FilterError(f"{source} is not a positive Decimal")
    if non_negative and value < 0:
        raise FilterError(f"{source} is not a non-negative Decimal")
    return value


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= 0:
        raise FilterError("cannot divide by a non-positive baseline")
    with localcontext() as context:
        context.prec = 50
        return numerator / denominator


def _mean(values: Sequence[Decimal]) -> Optional[Decimal]:
    if not values:
        return None
    with localcontext() as context:
        context.prec = 50
        return sum(values, Decimal("0")) / Decimal(len(values))


def _clamp_one(value: Decimal) -> Decimal:
    if value < 0:
        return Decimal("0")
    return min(Decimal("1"), value)


def _validate_sustained_parameters(required: int, window: int) -> None:
    if isinstance(required, bool) or not isinstance(required, int):
        raise FilterError("sustained required minutes is not an integer")
    if isinstance(window, bool) or not isinstance(window, int):
        raise FilterError("sustained window minutes is not an integer")
    if required <= 0 or window <= 0 or required > window:
        raise FilterError(
            "sustained confirmation requires 1 <= required <= window"
        )


def _magnitude_bucket(value: Decimal) -> Optional[str]:
    for name, lower, upper in MAGNITUDE_BUCKETS:
        if value >= lower and (upper is None or value < upper):
            return name
    return None


def _utc_ms(raw: Any, source: str) -> int:
    if type(raw) is not str or not raw.endswith("Z"):
        raise FilterError(f"{source} is not UTC text")
    try:
        value = datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError:
        raise FilterError(f"{source} is not UTC text") from None
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise FilterError(f"{source} is not UTC text")
    delta = value - EPOCH
    if delta.days < 0 or delta.microseconds % 1_000 != 0:
        raise FilterError(f"{source} is not a supported millisecond UTC")
    return (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FilterError(f"cannot read baselines {path}: {error}") from None
    if not isinstance(payload, dict):
        raise FilterError("baseline document is not an object")
    return payload


def _read_replay(path: Path) -> Tuple[Dict[str, Any], ...]:
    rows: List[Dict[str, Any]] = []
    seen = set()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    raise FilterError(
                        f"{path.name}:{line_number} is not JSON"
                    ) from None
                if not isinstance(raw, dict):
                    raise FilterError(
                        f"{path.name}:{line_number} is not an object"
                    )
                if raw.get("kind") == "summary":
                    continue
                if raw.get("kind") != "ignition":
                    raise FilterError(
                        f"{path.name}:{line_number} has unknown kind"
                    )
                symbol = raw.get("symbol")
                utc = raw.get("utc")
                if (
                    type(symbol) is not str
                    or SYMBOL.fullmatch(symbol) is None
                ):
                    raise FilterError(
                        f"{path.name}:{line_number} has invalid symbol"
                    )
                _utc_ms(utc, f"{path.name}:{line_number} utc")
                surge = _decimal(
                    raw.get("surge_ratio"),
                    f"{path.name}:{line_number} surge_ratio",
                    positive=True,
                )
                key = (symbol, utc)
                if key in seen:
                    raise FilterError(
                        f"{path.name}:{line_number} duplicates {symbol} {utc}"
                    )
                seen.add(key)
                row: Dict[str, Any] = {
                    "kind": "ignition",
                    "symbol": symbol,
                    "utc": utc,
                    "surge_ratio": baselines.decimal_text(surge),
                }
                for minutes in FORWARD_MINUTES:
                    name = f"price_move_next_{minutes}m_pct"
                    value = raw.get(name)
                    if value is None:
                        row[name] = None
                    else:
                        parsed = _decimal(
                            value, f"{path.name}:{line_number} {name}"
                        )
                        row[name] = baselines.decimal_text(parsed)
                rows.append(row)
    except (OSError, UnicodeDecodeError) as error:
        raise FilterError(f"cannot read replay {path}: {error}") from None
    return tuple(rows)


def _validate_baseline_document(payload: Mapping[str, Any]) -> None:
    if payload.get("schema") != "adaptive-volume-baselines-v1":
        raise FilterError("unsupported baseline schema")
    symbols = payload.get("symbols")
    if not isinstance(symbols, dict):
        raise FilterError("baseline symbols are missing")
    days = payload.get("tape_days")
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise FilterError("baseline tape_days is not a positive integer")


def _source_data_dir(
    baseline_payload: Mapping[str, Any], baseline_path: Path
) -> Path:
    source = baseline_payload.get("source")
    if not isinstance(source, dict):
        raise FilterError("baseline source is missing")
    raw = source.get("data_dir")
    if type(raw) is not str or not raw or Path(raw).is_absolute():
        raise FilterError("baseline source data_dir is not a relative path")
    boundary = baseline_path.resolve().parent.parent
    candidate = (baseline_path.resolve().parent / raw).resolve()
    if candidate != boundary and boundary not in candidate.parents:
        raise FilterError("baseline source data_dir escapes the study root")
    return candidate


def _token_decimal(
    token: Mapping[str, Any], path: Sequence[str], source: str
) -> Decimal:
    current: Any = token
    for part in path:
        if not isinstance(current, dict):
            raise FilterError(f"{source} is missing")
        current = current.get(part)
    return _decimal(current, source, positive=True)


def _event_threshold(
    token: Mapping[str, Any], close_ms: int
) -> Decimal:
    hour = datetime.fromtimestamp(close_ms // 1_000, timezone.utc).strftime(
        "%H"
    )
    return _token_decimal(
        token,
        ("time_of_day_utc", hour, "ignition_quote_volume"),
        f"hour {hour} ignition_quote_volume",
    )


def _forward_move(
    entry: baselines.TapeBar,
    future: Optional[baselines.TapeBar],
) -> Optional[str]:
    if future is None:
        return None
    with localcontext() as context:
        context.prec = 50
        move_pct = (
            (future.close / entry.close) - Decimal("1")
        ) * Decimal("100")
    return baselines.decimal_text(move_pct)


def _entry_ladder(
    event_bar: baselines.TapeBar,
    bars: Sequence[baselines.TapeBar],
) -> List[Dict[str, Any]]:
    by_close = {bar.close_ms: bar for bar in bars}
    ladder: List[Dict[str, Any]] = []
    for delay in ENTRY_DELAYS_MINUTES:
        entry_ms = event_bar.close_ms + delay * 60_000
        entry = by_close.get(entry_ms)
        item: Dict[str, Any] = {
            "delay_minutes": delay,
            "entry_utc": (
                None if entry is None else baselines.utc_text(entry.close_ms)
            ),
            "entry_price": (
                None
                if entry is None
                else baselines.decimal_text(entry.close)
            ),
        }
        for minutes in FORWARD_MINUTES:
            name = f"price_move_next_{minutes}m_pct"
            future = by_close.get(entry_ms + minutes * 60_000)
            item[name] = (
                None if entry is None else _forward_move(entry, future)
            )
        ladder.append(item)
    return ladder


def _unknown_row(
    event: Mapping[str, Any],
    reason: str,
    repeat_count: int,
    repeat_hot: Decimal,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "kind": "filtered_ignition",
        "symbol": event["symbol"],
        "utc": event["utc"],
        "surge_ratio": event["surge_ratio"],
        "decision": "drop",
        "reason": reason,
        "score": None,
        "entry_ladder": None,
        "features": {
            "repeat_count": repeat_count,
            "repeat_hot_count": baselines.decimal_text(repeat_hot),
            "serial_score": None,
            "baseline_relative_magnitude": None,
            "token_ignition_multiple": None,
            "followthrough_magnitude": None,
            "range_load": None,
            "volume_load": None,
            "range_sanity_score": None,
            "sustained_hot_count": None,
            "sustained_hot_fraction": None,
            "magnitude_bucket": None,
        },
    }
    for minutes in FORWARD_MINUTES:
        name = f"price_move_next_{minutes}m_pct"
        row[name] = event[name]
    return row


def _score_event(
    event: Mapping[str, Any],
    token: Mapping[str, Any],
    bars: Sequence[baselines.TapeBar],
    repeat_count: int,
    repeat_hot: Decimal,
    sustained_required: int,
    sustained_window: int,
) -> Dict[str, Any]:
    if (
        token.get("bar_count") != len(bars)
        or not bars
        or token.get("first_utc") != baselines.utc_text(bars[0].close_ms)
        or token.get("last_utc") != baselines.utc_text(bars[-1].close_ms)
    ):
        return _unknown_row(
            event,
            "baseline_tape_mismatch",
            repeat_count,
            repeat_hot,
        )

    event_ms = _utc_ms(event["utc"], "ignition utc")
    by_close = {bar.close_ms: bar for bar in bars}
    event_bar = by_close.get(event_ms)
    if event_bar is None:
        return _unknown_row(
            event, "unknown_ignition_bar", repeat_count, repeat_hot
        )
    following: List[baselines.TapeBar] = []
    for offset in (1, 2):
        bar = by_close.get(event_ms + offset * 60_000)
        if bar is None:
            return _unknown_row(
                event,
                "unknown_followthrough_tape",
                repeat_count,
                repeat_hot,
            )
        following.append(bar)
    confirmation: List[baselines.TapeBar] = []
    for offset in range(-(sustained_window - 1), 1):
        bar = by_close.get(event_ms + offset * 60_000)
        if bar is None:
            return _unknown_row(
                event,
                "unknown_sustained_tape",
                repeat_count,
                repeat_hot,
            )
        confirmation.append(bar)

    try:
        threshold = _event_threshold(token, event_bar.close_ms)
        ignition_multiple = _token_decimal(
            token,
            ("ignition_threshold", "quiet_floor_multiple"),
            "token quiet_floor_multiple",
        )
        global_volume_hot = _token_decimal(
            token,
            ("rolling_volume_regimes", "1m", "hot"),
            "token hot 1m volume",
        )
        global_range_hot = _token_decimal(
            token,
            ("range_regime_pct", "hot"),
            "token hot kline range",
        )
        magnitude = _ratio(event_bar.quote_volume, threshold)
        following_magnitudes = [
            _ratio(bar.quote_volume, _event_threshold(token, bar.close_ms))
            for bar in following
        ]
        followthrough = baselines.quantile(
            following_magnitudes, 1, 2
        )
        if followthrough is None:
            raise FilterError("followthrough magnitude is unknown")
        volume_load = _ratio(event_bar.quote_volume, global_volume_hot)
        range_pct = baselines.bar_range_pct(event_bar)
        range_load = _ratio(range_pct, global_range_hot)
        sustained_hot_count = sum(
            bar.quote_volume > global_volume_hot for bar in confirmation
        )
        sustained_hot_fraction = _ratio(
            Decimal(sustained_hot_count), Decimal(sustained_window)
        )
        magnitude_bucket = _magnitude_bucket(magnitude)
    except (FilterError, baselines.BaselineError):
        return _unknown_row(
            event, "unknown_adaptive_feature", repeat_count, repeat_hot
        )

    serial_score = _clamp_one(
        _ratio(repeat_hot, Decimal(repeat_count))
    )
    magnitude_score = _clamp_one(magnitude)
    sustained_score = _clamp_one(followthrough)
    range_score = (
        Decimal("1")
        if range_load == 0
        else _clamp_one(_ratio(volume_load, range_load))
    )
    score_value = _mean(
        (serial_score, magnitude_score, sustained_score, range_score)
    )
    if score_value is None:
        return _unknown_row(
            event, "unknown_adaptive_feature", repeat_count, repeat_hot
        )

    if Decimal(repeat_count) > repeat_hot:
        decision, reason = "drop", "serial_igniter"
    elif magnitude <= Decimal("1"):
        decision, reason = "drop", "below_token_threshold"
    elif followthrough <= Decimal("1"):
        decision, reason = "drop", "one_print_spike"
    elif sustained_hot_count < sustained_required:
        decision, reason = "drop", "insufficient_sustained_tape"
    elif range_load > volume_load:
        decision, reason = "drop", "range_depth_sanity"
    else:
        decision, reason = "keep", "sustained_runner"

    row = {
        "kind": "filtered_ignition",
        "symbol": event["symbol"],
        "utc": event["utc"],
        "surge_ratio": event["surge_ratio"],
        "decision": decision,
        "reason": reason,
        "score": baselines.decimal_text(score_value),
        "entry_ladder": _entry_ladder(event_bar, bars),
        "features": {
            "repeat_count": repeat_count,
            "repeat_hot_count": baselines.decimal_text(repeat_hot),
            "serial_score": baselines.decimal_text(serial_score),
            "baseline_relative_magnitude": baselines.decimal_text(magnitude),
            "token_ignition_multiple": baselines.decimal_text(
                ignition_multiple
            ),
            "followthrough_magnitude": baselines.decimal_text(followthrough),
            "range_load": baselines.decimal_text(range_load),
            "volume_load": baselines.decimal_text(volume_load),
            "range_sanity_score": baselines.decimal_text(range_score),
            "sustained_hot_count": sustained_hot_count,
            "sustained_hot_fraction": baselines.decimal_text(
                sustained_hot_fraction
            ),
            "magnitude_bucket": magnitude_bucket,
        },
    }
    for minutes in FORWARD_MINUTES:
        name = f"price_move_next_{minutes}m_pct"
        row[name] = event[name]
    return row


def _return_stats(values: Sequence[Decimal]) -> Dict[str, Any]:
    median = baselines.quantile(values, 1, 2)
    mean = _mean(values)
    wins = sum(value > 0 for value in values)
    win_pct = None
    if values:
        with localcontext() as context:
            context.prec = 50
            win_pct = Decimal(wins) * Decimal("100") / Decimal(len(values))
    return {
        "n": len(values),
        "median_pct": None if median is None else baselines.decimal_text(median),
        "mean_pct": None if mean is None else baselines.decimal_text(mean),
        "win_pct": None if win_pct is None else baselines.decimal_text(win_pct),
    }


def _forward_stats(
    rows: Sequence[Mapping[str, Any]], decision: str, minutes: int
) -> Dict[str, Any]:
    name = f"price_move_next_{minutes}m_pct"
    values = [
        _decimal(row[name], name)
        for row in rows
        if row.get("decision") == decision and row.get(name) is not None
    ]
    return _return_stats(values)


def _grid_cells(
    rows: Sequence[Mapping[str, Any]], days: int
) -> List[Dict[str, Any]]:
    grid: List[Dict[str, Any]] = []
    for delay in ENTRY_DELAYS_MINUTES:
        for bucket, _lower, _upper in MAGNITUDE_BUCKETS:
            entries: List[Mapping[str, Any]] = []
            for row in rows:
                features = row.get("features")
                if (
                    row.get("decision") != "keep"
                    or not isinstance(features, dict)
                    or features.get("magnitude_bucket") != bucket
                ):
                    continue
                ladder = row.get("entry_ladder")
                if not isinstance(ladder, list):
                    continue
                entry = next(
                    (
                        item
                        for item in ladder
                        if isinstance(item, dict)
                        and item.get("delay_minutes") == delay
                    ),
                    None,
                )
                if entry is None or any(
                    entry.get(f"price_move_next_{minutes}m_pct") is None
                    for minutes in FORWARD_MINUTES
                ):
                    continue
                entries.append(entry)

            with localcontext() as context:
                context.prec = 50
                kept_per_day = Decimal(len(entries)) / Decimal(days)
            returns: Dict[str, Any] = {}
            for minutes in FORWARD_MINUTES:
                name = f"price_move_next_{minutes}m_pct"
                values = [_decimal(entry[name], name) for entry in entries]
                returns[f"{minutes}m"] = _return_stats(values)
            grid.append(
                {
                    "delay_minutes": delay,
                    "magnitude_bucket": bucket,
                    "n": len(entries),
                    "kept_per_day": baselines.decimal_text(kept_per_day),
                    "forward_returns": returns,
                }
            )
    return grid


def _best_grid_cell(
    grid: Sequence[Mapping[str, Any]], minutes: int
) -> Optional[Dict[str, Any]]:
    bucket_rank = {
        name: index
        for index, (name, _lower, _upper) in enumerate(MAGNITUDE_BUCKETS)
    }
    best: Optional[Mapping[str, Any]] = None
    best_key: Optional[Tuple[Decimal, int, int, int]] = None
    for cell in grid:
        count = cell.get("n")
        delay = cell.get("delay_minutes")
        bucket = cell.get("magnitude_bucket")
        returns = cell.get("forward_returns")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
            or isinstance(delay, bool)
            or not isinstance(delay, int)
            or type(bucket) is not str
            or bucket not in bucket_rank
            or not isinstance(returns, dict)
        ):
            continue
        stats = returns.get(f"{minutes}m")
        if not isinstance(stats, dict) or stats.get("median_pct") is None:
            continue
        median = _decimal(stats["median_pct"], "grid median")
        key = (median, count, -delay, -bucket_rank[bucket])
        if best_key is None or key > best_key:
            best = cell
            best_key = key
    if best is None:
        return None
    stats = best["forward_returns"][f"{minutes}m"]
    return {
        "delay_minutes": best["delay_minutes"],
        "magnitude_bucket": best["magnitude_bucket"],
        "n": best["n"],
        "kept_per_day": best["kept_per_day"],
        "median_pct": stats["median_pct"],
        "win_pct": stats["win_pct"],
    }


def score_ignitions(
    events: Sequence[Mapping[str, Any]],
    baseline_payload: Mapping[str, Any],
    tape: Mapping[str, Sequence[baselines.TapeBar]],
    *,
    sustained_required: int = DEFAULT_SUSTAINED_REQUIRED,
    sustained_window: int = DEFAULT_SUSTAINED_WINDOW,
) -> Tuple[Dict[str, Any], ...]:
    """Return v2 scored rows followed by the delay-by-magnitude summary."""
    _validate_baseline_document(baseline_payload)
    _validate_sustained_parameters(sustained_required, sustained_window)
    symbols = baseline_payload["symbols"]
    counts = Counter(str(row.get("symbol")) for row in events)
    repeat_values = [Decimal(value) for value in counts.values()]
    repeat_hot = baselines.quantile(repeat_values, 9, 10)
    if repeat_hot is None:
        repeat_hot = Decimal("1")

    scored: List[Dict[str, Any]] = []
    for event in events:
        symbol = event.get("symbol")
        if type(symbol) is not str or SYMBOL.fullmatch(symbol) is None:
            raise FilterError("event has invalid symbol")
        repeat_count = counts[symbol]
        token = symbols.get(symbol)
        bars = tape.get(symbol)
        if not isinstance(token, dict):
            scored.append(
                _unknown_row(
                    event, "unknown_token_baseline", repeat_count, repeat_hot
                )
            )
        elif bars is None:
            scored.append(
                _unknown_row(
                    event, "unknown_token_tape", repeat_count, repeat_hot
                )
            )
        else:
            scored.append(
                _score_event(
                    event,
                    token,
                    bars,
                    repeat_count,
                    repeat_hot,
                    sustained_required,
                    sustained_window,
                )
            )

    kept = sum(row["decision"] == "keep" for row in scored)
    dropped = len(scored) - kept
    days = baseline_payload["tape_days"]
    with localcontext() as context:
        context.prec = 50
        kept_per_day = Decimal(kept) / Decimal(days)
        dropped_per_day = Decimal(dropped) / Decimal(days)
    grid = _grid_cells(scored, days)
    summary = {
        "kind": "summary",
        "filter": "adaptive-noise-filter-v2",
        "ignitions": len(scored),
        "kept": kept,
        "dropped": dropped,
        "tape_days": days,
        "kept_per_day": baselines.decimal_text(kept_per_day),
        "dropped_per_day": baselines.decimal_text(dropped_per_day),
        "sustained_confirmation": {
            "required_hot_minutes": sustained_required,
            "window_minutes": sustained_window,
            "baseline": "token_rolling_volume_regimes_1m_hot",
            "comparison": "strictly_above",
            "alignment": "trailing_through_threshold_cross",
        },
        "entry_delays_minutes": list(ENTRY_DELAYS_MINUTES),
        "magnitude_ladder_basis": (
            "event_quote_volume_over_time_of_day_ignition_threshold"
        ),
        "magnitude_ladder": [
            {
                "bucket": name,
                "lower_inclusive": baselines.decimal_text(lower),
                "upper_exclusive": (
                    None
                    if upper is None
                    else baselines.decimal_text(upper)
                ),
            }
            for name, lower, upper in MAGNITUDE_BUCKETS
        ],
        "forward_returns": {
            "kept": {
                f"{minutes}m": _forward_stats(scored, "keep", minutes)
                for minutes in FORWARD_MINUTES
            },
            "dropped": {
                f"{minutes}m": _forward_stats(scored, "drop", minutes)
                for minutes in FORWARD_MINUTES
            },
        },
        "delay_magnitude_grid": grid,
        "best_grid_cell_basis": (
            "highest_median_then_n_then_earliest_delay_then_lower_bucket"
        ),
        "best_grid_cells": {
            f"{minutes}m": _best_grid_cell(grid, minutes)
            for minutes in FORWARD_MINUTES
        },
    }
    return tuple(scored + [summary])


def _default_output_path(score_path: Path) -> Path:
    match = SCORE_DATE.fullmatch(score_path.name)
    if match is None:
        raise FilterError("score filename must be replay-YYYY-MM-DD.jsonl")
    return score_path.parent / f"filtered-{match.group(1)}.jsonl"


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(
                    json.dumps(
                        dict(row), sort_keys=True, separators=(",", ":")
                    )
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
        raise FilterError(f"cannot write filter output {target}: {error}") \
            from None


def run_filter(
    score_path: Path = DEFAULT_SCORE_PATH,
    baselines_path: Path = DEFAULT_BASELINES_PATH,
    output_path: Optional[Path] = None,
    *,
    sustained_required: int = DEFAULT_SUSTAINED_REQUIRED,
    sustained_window: int = DEFAULT_SUSTAINED_WINDOW,
) -> Tuple[Tuple[Dict[str, Any], ...], Path]:
    baseline_payload = _read_json(Path(baselines_path))
    _validate_baseline_document(baseline_payload)
    source_dir = _source_data_dir(baseline_payload, Path(baselines_path))
    try:
        tape = baselines.load_tape(source_dir)
    except baselines.BaselineError as error:
        raise FilterError(f"cannot load baseline tape: {error}") from None
    events = _read_replay(Path(score_path))
    rows = score_ignitions(
        events,
        baseline_payload,
        tape,
        sustained_required=sustained_required,
        sustained_window=sustained_window,
    )
    target = Path(output_path) if output_path else _default_output_path(
        Path(score_path)
    )
    _write_jsonl(target, rows)
    return rows, target


def _grid_stat_text(cell: Mapping[str, Any], minutes: int) -> str:
    stats = cell["forward_returns"][f"{minutes}m"]
    return (
        f"{minutes}m med={stats['median_pct']} win={stats['win_pct']}"
    )


def _best_text(summary: Mapping[str, Any], minutes: int) -> str:
    cell = summary["best_grid_cells"][f"{minutes}m"]
    if cell is None:
        return f"{minutes}m=none"
    return (
        f"{minutes}m={cell['delay_minutes']}m/"
        f"{cell['magnitude_bucket']}/n={cell['n']}/"
        f"med={cell['median_pct']}"
    )


def summary_row(summary: Mapping[str, Any], output_path: Path) -> str:
    sustained = summary["sustained_confirmation"]
    best = ",".join(
        _best_text(summary, minutes) for minutes in FORWARD_MINUTES
    )
    lines = [
        (
            "FILTER v2 | "
            f"kept/day={summary['kept_per_day']} | "
            f"dropped/day={summary['dropped_per_day']} | "
            f"sustained={sustained['required_hot_minutes']}/"
            f"{sustained['window_minutes']}>hot | "
            f"best={best} | output={output_path}"
        )
    ]
    for cell in summary["delay_magnitude_grid"]:
        stats = " | ".join(
            _grid_stat_text(cell, minutes)
            for minutes in FORWARD_MINUTES
        )
        lines.append(
            "GRID | "
            f"delay={cell['delay_minutes']}m | "
            f"magnitude={cell['magnitude_bucket']} | "
            f"n={cell['n']} | kept/day={cell['kept_per_day']} | "
            f"{stats}"
        )
    return "\n".join(lines)


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Score replay ignitions with adaptive noise filter v2."
    )
    parser.add_argument("--score", type=Path, default=DEFAULT_SCORE_PATH)
    parser.add_argument(
        "--baselines", type=Path, default=DEFAULT_BASELINES_PATH
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--sustained-required",
        type=int,
        default=DEFAULT_SUSTAINED_REQUIRED,
    )
    parser.add_argument(
        "--sustained-window",
        type=int,
        default=DEFAULT_SUSTAINED_WINDOW,
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = argument_parser().parse_args(argv)
    try:
        rows, output_path = run_filter(
            args.score,
            args.baselines,
            args.output,
            sustained_required=args.sustained_required,
            sustained_window=args.sustained_window,
        )
    except FilterError as error:
        print(f"noise_filter: {error}", file=sys.stderr)
        return 2
    print(summary_row(rows[-1], output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
