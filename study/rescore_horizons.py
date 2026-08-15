#!/usr/bin/env python3
"""Re-score kept noise-filter ignitions on longer offline tape horizons.

The summary's median and win rate are gross exit-model returns, matching the
short-horizon study convention.  Expectancy is the arithmetic mean after the
selected round-trip fee is subtracted in percentage points.  The provided fee
figures are complete round-trip assumptions, not per-fill rates.

The ladder simulator fills resting targets at their target price.  Its trailing
stop uses only the peak proved before the current one-minute bar, so a bar's
unknown high/low ordering cannot manufacture a same-bar stop.  A gap through
the stop exits at the bar open.  Every tranche still open at the requested
horizon exits at that bar's close.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import baselines  # noqa: E402


STUDY_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = STUDY_DIR / "data"
DEFAULT_FILTERED_DIR = STUDY_DIR / "out"
DEFAULT_FILTERED_PATTERN = "filtered-*.jsonl"
DEFAULT_FILTER_NAME = "adaptive-noise-filter-v1"
DEFAULT_HORIZONS_MINUTES = (60, 360, 1_440)
EXIT_MODELS = ("simple", "ladder")
SYMBOL = re.compile(r"[A-Z0-9]{5,30}")
ARCHIVE_NAME = re.compile(
    r"(?P<symbol>[A-Z0-9]{5,30})-1m-"
    r"(?P<day>\d{4}-\d{2}-\d{2})\.zip"
)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class RescoreError(Exception):
    """The cohort, tape, parameters, or requested score is unproved."""


@dataclass(frozen=True)
class KeptTrade:
    symbol: str
    close_ms: int
    utc: str


@dataclass(frozen=True)
class LadderParameters:
    first_target_pct: Decimal
    second_target_pct: Decimal
    trail_pct: Decimal
    first_weight: Decimal
    second_weight: Decimal
    trail_weight: Decimal

    def __post_init__(self) -> None:
        values = (
            ("first target", self.first_target_pct),
            ("second target", self.second_target_pct),
            ("trail", self.trail_pct),
            ("first weight", self.first_weight),
            ("second weight", self.second_weight),
            ("trail weight", self.trail_weight),
        )
        for name, value in values:
            if not isinstance(value, Decimal) or not value.is_finite():
                raise RescoreError(f"{name} is not a finite Decimal")
            if value <= 0:
                raise RescoreError(f"{name} must be positive")
        if self.first_target_pct >= self.second_target_pct:
            raise RescoreError("first target must be below second target")
        if self.trail_pct >= Decimal("100"):
            raise RescoreError("trail must be below 100 percent")


@dataclass(frozen=True)
class FeeModel:
    name: str
    round_trip_pct: Decimal

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise RescoreError("fee model name is empty")
        if (
            not isinstance(self.round_trip_pct, Decimal)
            or not self.round_trip_pct.is_finite()
            or self.round_trip_pct < 0
        ):
            raise RescoreError("round-trip fee must be a non-negative Decimal")


def decimal_text(value: Decimal) -> str:
    """Render finite Decimal values without exponent notation."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise RescoreError("cannot render a non-finite Decimal")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def parse_decimal(
    raw: Any,
    source: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    """Parse Decimal text while explicitly refusing binary floating point."""
    if isinstance(raw, Decimal):
        value = raw
    elif type(raw) is str and raw.strip():
        try:
            value = Decimal(raw.strip())
        except InvalidOperation:
            raise RescoreError(f"{source} is not Decimal text") from None
    else:
        raise RescoreError(f"{source} is not Decimal text")
    if not value.is_finite():
        raise RescoreError(f"{source} is not a finite Decimal")
    if positive and value <= 0:
        raise RescoreError(f"{source} must be positive")
    if non_negative and value < 0:
        raise RescoreError(f"{source} must be non-negative")
    return value


def utc_ms(raw: Any, source: str) -> int:
    if type(raw) is not str or not raw.endswith("Z"):
        raise RescoreError(f"{source} is not UTC text")
    try:
        value = datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError:
        raise RescoreError(f"{source} is not UTC text") from None
    if (
        value.tzinfo is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise RescoreError(f"{source} is not UTC text")
    delta = value - EPOCH
    if delta.days < 0 or delta.microseconds % 1_000 != 0:
        raise RescoreError(f"{source} is not supported millisecond UTC")
    return (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )


def _read_json_lines(path: Path) -> Iterable[Tuple[int, Dict[str, Any]]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    raise RescoreError(
                        f"{path.name}:{line_number} is not JSON"
                    ) from None
                if not isinstance(row, dict):
                    raise RescoreError(
                        f"{path.name}:{line_number} is not an object"
                    )
                yield line_number, row
    except (OSError, UnicodeDecodeError) as error:
        raise RescoreError(f"cannot read cohort {path}: {error}") from None


def load_kept_trades(
    paths: Sequence[Path],
    *,
    required_filter: str = DEFAULT_FILTER_NAME,
) -> Tuple[KeptTrade, ...]:
    """Validate filtered JSONL files and return their unique v1 kept cohort."""
    if not paths:
        raise RescoreError("no filtered cohort files matched")
    kept: List[KeptTrade] = []
    seen = set()
    for raw_path in paths:
        path = Path(raw_path)
        summaries = 0
        file_kept = 0
        summary_kept: Optional[int] = None
        saw_summary = False
        for line_number, row in _read_json_lines(path):
            source = f"{path.name}:{line_number}"
            kind = row.get("kind")
            if kind == "summary":
                if saw_summary:
                    raise RescoreError(f"{source} duplicates the summary")
                saw_summary = True
                summaries += 1
                if row.get("filter") != required_filter:
                    raise RescoreError(
                        f"{source} is not {required_filter} output"
                    )
                raw_kept = row.get("kept")
                if (
                    isinstance(raw_kept, bool)
                    or not isinstance(raw_kept, int)
                    or raw_kept < 0
                ):
                    raise RescoreError(f"{source} has invalid kept count")
                summary_kept = raw_kept
                continue
            if saw_summary:
                raise RescoreError(f"{source} follows the summary")
            if kind != "filtered_ignition":
                raise RescoreError(f"{source} has unknown kind")
            decision = row.get("decision")
            if decision not in {"keep", "drop"}:
                raise RescoreError(f"{source} has invalid decision")
            if decision == "drop":
                continue
            symbol = row.get("symbol")
            utc = row.get("utc")
            if type(symbol) is not str or SYMBOL.fullmatch(symbol) is None:
                raise RescoreError(f"{source} has invalid symbol")
            close_ms = utc_ms(utc, f"{source} utc")
            key = (symbol, close_ms)
            if key in seen:
                raise RescoreError(
                    f"{source} duplicates kept trade {symbol} {utc}"
                )
            seen.add(key)
            kept.append(KeptTrade(symbol=symbol, close_ms=close_ms, utc=utc))
            file_kept += 1
        if summaries != 1:
            raise RescoreError(f"{path.name} does not have one summary")
        if summary_kept != file_kept:
            raise RescoreError(
                f"{path.name} kept count does not match its rows"
            )
    return tuple(kept)


def _utc_day(epoch_ms: int) -> str:
    return (EPOCH + timedelta(milliseconds=epoch_ms)).date().isoformat()


def _required_archive_days(
    trades: Sequence[KeptTrade], max_horizon_minutes: int
) -> Dict[str, set]:
    required: Dict[str, set] = {}
    for trade in trades:
        first_day = (EPOCH + timedelta(milliseconds=trade.close_ms)).date()
        last_day = (
            EPOCH
            + timedelta(
                milliseconds=(
                    trade.close_ms + max_horizon_minutes * 60_000
                )
            )
        ).date()
        day = first_day
        while day <= last_day:
            required.setdefault(trade.symbol, set()).add(day.isoformat())
            day += timedelta(days=1)
    return required


def load_relevant_tape(
    data_dir: Path,
    trades: Sequence[KeptTrade],
    max_horizon_minutes: int,
) -> Dict[str, Tuple[baselines.TapeBar, ...]]:
    """Load only daily archives that can contribute to requested trades."""
    root = Path(data_dir)
    if not root.is_dir():
        raise RescoreError(f"data directory does not exist: {root}")
    required = _required_archive_days(trades, max_horizon_minutes)
    selected: List[Tuple[Path, str, str]] = []
    for path in root.rglob("*.zip"):
        if not path.is_file():
            continue
        match = ARCHIVE_NAME.fullmatch(path.name)
        if match is None:
            continue
        symbol = match.group("symbol")
        day = match.group("day")
        if day in required.get(symbol, set()):
            selected.append((path, symbol, day))
    if trades and not selected:
        raise RescoreError("data directory has no relevant 1m kline zips")

    indexed: Dict[str, Dict[int, baselines.TapeBar]] = {}
    for path, archive_symbol, archive_day in sorted(selected):
        try:
            archive_rows = baselines._read_archive(path)
            for bar in archive_rows:
                if bar.symbol != archive_symbol:
                    raise RescoreError(
                        f"{path.name} has unexpected symbol {bar.symbol}"
                    )
                if _utc_day(bar.close_ms) != archive_day:
                    raise RescoreError(
                        f"{path.name} has a kline outside its UTC day"
                    )
                by_close = indexed.setdefault(bar.symbol, {})
                existing = by_close.get(bar.close_ms)
                if existing is not None and existing != bar:
                    raise RescoreError(
                        f"conflicting duplicate {bar.symbol} kline at "
                        f"{baselines.utc_text(bar.close_ms)}"
                    )
                by_close[bar.close_ms] = bar
        except baselines.BaselineError as error:
            raise RescoreError(f"cannot load tape: {error}") from None
    return {
        symbol: tuple(by_close[key] for key in sorted(by_close))
        for symbol, by_close in sorted(indexed.items())
    }


def _return_pct(entry_price: Decimal, exit_price: Decimal) -> Decimal:
    if entry_price <= 0 or exit_price <= 0:
        raise RescoreError("entry and exit prices must be positive")
    with localcontext() as context:
        context.prec = 50
        return ((exit_price / entry_price) - Decimal("1")) * Decimal("100")


def simple_horizon_return(
    entry: baselines.TapeBar,
    future_bars: Sequence[baselines.TapeBar],
) -> Decimal:
    if not future_bars:
        raise RescoreError("simple horizon has no future bars")
    return _return_pct(entry.close, future_bars[-1].close)


def laddered_horizon_return(
    entry: baselines.TapeBar,
    future_bars: Sequence[baselines.TapeBar],
    parameters: LadderParameters,
) -> Decimal:
    """Return the weighted gross percentage for the three exit tranches."""
    if not future_bars:
        raise RescoreError("ladder horizon has no future bars")
    with localcontext() as context:
        context.prec = 50
        hundred = Decimal("100")
        first_price = entry.close * (
            Decimal("1") + parameters.first_target_pct / hundred
        )
        second_price = entry.close * (
            Decimal("1") + parameters.second_target_pct / hundred
        )
        trail_fraction = parameters.trail_pct / hundred
        first_exit: Optional[Decimal] = None
        second_exit: Optional[Decimal] = None
        trail_exit: Optional[Decimal] = None
        proved_peak = entry.close

        for bar in future_bars:
            if first_exit is None and bar.high >= first_price:
                first_exit = first_price
            if second_exit is None and bar.high >= second_price:
                second_exit = second_price
            if trail_exit is None:
                stop_price = proved_peak * (Decimal("1") - trail_fraction)
                if bar.open <= stop_price:
                    trail_exit = bar.open
                elif bar.low <= stop_price:
                    trail_exit = stop_price
                else:
                    proved_peak = max(proved_peak, bar.high)

        horizon_close = future_bars[-1].close
        if first_exit is None:
            first_exit = horizon_close
        if second_exit is None:
            second_exit = horizon_close
        if trail_exit is None:
            trail_exit = horizon_close
        total_weight = (
            parameters.first_weight
            + parameters.second_weight
            + parameters.trail_weight
        )
        weighted_exit = (
            first_exit * parameters.first_weight
            + second_exit * parameters.second_weight
            + trail_exit * parameters.trail_weight
        ) / total_weight
    return _return_pct(entry.close, weighted_exit)


def _validated_horizons(values: Sequence[int]) -> Tuple[int, ...]:
    if not values:
        raise RescoreError("at least one horizon is required")
    parsed: List[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RescoreError("horizons must be positive integer minutes")
        if value in parsed:
            raise RescoreError("horizons must be unique")
        parsed.append(value)
    return tuple(parsed)


def gross_returns_by_cell(
    trades: Sequence[KeptTrade],
    tape: Mapping[str, Sequence[baselines.TapeBar]],
    horizons_minutes: Sequence[int],
    parameters: LadderParameters,
) -> Tuple[
    Dict[Tuple[int, str], Tuple[Decimal, ...]],
    Dict[int, int],
]:
    """Measure gross returns, omitting any trade with an unproved 1m window."""
    horizons = _validated_horizons(horizons_minutes)
    mutable: Dict[Tuple[int, str], List[Decimal]] = {
        (horizon, model): []
        for horizon in horizons
        for model in EXIT_MODELS
    }
    skipped = {horizon: 0 for horizon in horizons}
    indexes: Dict[str, Dict[int, baselines.TapeBar]] = {
        symbol: {bar.close_ms: bar for bar in bars}
        for symbol, bars in tape.items()
    }
    for trade in trades:
        bars_by_close = indexes.get(trade.symbol)
        entry = (
            None
            if bars_by_close is None
            else bars_by_close.get(trade.close_ms)
        )
        for horizon in horizons:
            if entry is None or bars_by_close is None:
                skipped[horizon] += 1
                continue
            future: List[baselines.TapeBar] = []
            for minute in range(1, horizon + 1):
                bar = bars_by_close.get(trade.close_ms + minute * 60_000)
                if bar is None:
                    future = []
                    break
                future.append(bar)
            if not future:
                skipped[horizon] += 1
                continue
            mutable[(horizon, "simple")].append(
                simple_horizon_return(entry, future)
            )
            mutable[(horizon, "ladder")].append(
                laddered_horizon_return(entry, future, parameters)
            )
    return (
        {key: tuple(values) for key, values in mutable.items()},
        skipped,
    )


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


def _mean(values: Sequence[Decimal]) -> Optional[Decimal]:
    if not values:
        return None
    with localcontext() as context:
        context.prec = 50
        return sum(values, Decimal("0")) / Decimal(len(values))


def build_summary_grid(
    gross_returns: Mapping[Tuple[int, str], Sequence[Decimal]],
    horizons_minutes: Sequence[int],
    fee_models: Sequence[FeeModel],
) -> Tuple[Dict[str, Any], ...]:
    """Build horizon x exit-model x fee-model summary cells."""
    horizons = _validated_horizons(horizons_minutes)
    if not fee_models:
        raise RescoreError("at least one fee model is required")
    if len({model.name for model in fee_models}) != len(fee_models):
        raise RescoreError("fee model names must be unique")
    grid: List[Dict[str, Any]] = []
    for horizon in horizons:
        for exit_model in EXIT_MODELS:
            raw_values = gross_returns.get((horizon, exit_model), ())
            values: List[Decimal] = []
            for value in raw_values:
                if not isinstance(value, Decimal) or not value.is_finite():
                    raise RescoreError("gross return is not a finite Decimal")
                values.append(value)
            median = _median(values)
            mean = _mean(values)
            wins = sum(value > 0 for value in values)
            win_pct: Optional[Decimal] = None
            if values:
                with localcontext() as context:
                    context.prec = 50
                    win_pct = (
                        Decimal(wins) * Decimal("100") / Decimal(len(values))
                    )
            for fee_model in fee_models:
                net_expectancy = (
                    None if mean is None else mean - fee_model.round_trip_pct
                )
                grid.append(
                    {
                        "horizon_minutes": horizon,
                        "exit_model": exit_model,
                        "fee_model": fee_model.name,
                        "round_trip_fee_pct": decimal_text(
                            fee_model.round_trip_pct
                        ),
                        "n": len(values),
                        "median_pct": (
                            None if median is None else decimal_text(median)
                        ),
                        "win_pct": (
                            None if win_pct is None else decimal_text(win_pct)
                        ),
                        "net_expectancy_pct": (
                            None
                            if net_expectancy is None
                            else decimal_text(net_expectancy)
                        ),
                    }
                )
    return tuple(grid)


def run_rescore(
    filtered_paths: Sequence[Path],
    data_dir: Path,
    horizons_minutes: Sequence[int],
    parameters: LadderParameters,
    fee_models: Sequence[FeeModel],
) -> Dict[str, Any]:
    trades = load_kept_trades(filtered_paths)
    horizons = _validated_horizons(horizons_minutes)
    tape = load_relevant_tape(Path(data_dir), trades, max(horizons))
    gross, skipped = gross_returns_by_cell(
        trades, tape, horizons, parameters
    )
    grid = build_summary_grid(gross, horizons, fee_models)
    return {
        "cohort": DEFAULT_FILTER_NAME,
        "kept": len(trades),
        "filtered_files": len(filtered_paths),
        "ladder_parameters": {
            "first_target_pct": decimal_text(parameters.first_target_pct),
            "second_target_pct": decimal_text(parameters.second_target_pct),
            "trail_pct": decimal_text(parameters.trail_pct),
            "first_weight": decimal_text(parameters.first_weight),
            "second_weight": decimal_text(parameters.second_weight),
            "trail_weight": decimal_text(parameters.trail_weight),
        },
        "skipped_by_horizon": dict(skipped),
        "grid": grid,
    }


def horizon_label(minutes: int) -> str:
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def summary_rows(result: Mapping[str, Any]) -> Tuple[str, ...]:
    skipped = result["skipped_by_horizon"]
    ladder = result["ladder_parameters"]
    skip_text = ",".join(
        f"{horizon_label(minutes)}:{skipped[minutes]}"
        for minutes in skipped
    )
    rows = [
        (
            "RESCORE | "
            f"cohort={result['cohort']} | kept={result['kept']} | "
            f"files={result['filtered_files']} | "
            f"ladder={ladder['first_weight']}@+"
            f"{ladder['first_target_pct']}%,"
            f"{ladder['second_weight']}@+"
            f"{ladder['second_target_pct']}%,"
            f"{ladder['trail_weight']}@trail-"
            f"{ladder['trail_pct']}% | skipped={skip_text}"
        )
    ]
    for cell in result["grid"]:
        rows.append(
            "GRID | "
            f"horizon={horizon_label(cell['horizon_minutes'])} | "
            f"exit={cell['exit_model']} | fee={cell['fee_model']}/"
            f"{cell['round_trip_fee_pct']}% | "
            f"n={cell['n']} | med={cell['median_pct']} | "
            f"win={cell['win_pct']} | "
            f"net_expectancy={cell['net_expectancy_pct']}"
        )
    return tuple(rows)


def best_cells(
    grid: Sequence[Mapping[str, Any]], count: int = 3
) -> Tuple[Mapping[str, Any], ...]:
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise RescoreError("best-cell count must be a non-negative integer")
    scored = [
        cell for cell in grid if cell.get("net_expectancy_pct") is not None
    ]
    scored.sort(
        key=lambda cell: (
            parse_decimal(cell["net_expectancy_pct"], "net expectancy"),
            cell["n"],
            -cell["horizon_minutes"],
            cell["exit_model"],
            cell["fee_model"],
        ),
        reverse=True,
    )
    return tuple(scored[:count])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Re-score v1-kept ignitions on offline long horizons."
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument(
        "--filtered",
        action="append",
        type=Path,
        help="filtered JSONL path; repeat for multiple files",
    )
    parser.add_argument(
        "--filtered-dir", type=Path, default=DEFAULT_FILTERED_DIR
    )
    parser.add_argument(
        "--filtered-pattern", default=DEFAULT_FILTERED_PATTERN
    )
    parser.add_argument(
        "--horizon-minutes",
        nargs="+",
        default=[str(value) for value in DEFAULT_HORIZONS_MINUTES],
        metavar="MINUTES",
    )
    parser.add_argument("--first-target-pct", default="1")
    parser.add_argument("--second-target-pct", default="3")
    parser.add_argument("--trail-pct", default="2")
    parser.add_argument("--first-weight", default="1")
    parser.add_argument("--second-weight", default="1")
    parser.add_argument("--trail-weight", default="1")
    parser.add_argument("--taker-round-trip-fee-pct", default="0.30")
    parser.add_argument("--maker-exit-round-trip-fee-pct", default="0.20")
    return parser


def _horizons_from_args(values: Sequence[str]) -> Tuple[int, ...]:
    parsed: List[int] = []
    for raw in values:
        if type(raw) is not str or not raw.isdigit():
            raise RescoreError("horizons must be positive integer minutes")
        value = int(raw)
        parsed.append(value)
    return _validated_horizons(parsed)


def _parameters_from_args(args: argparse.Namespace) -> LadderParameters:
    return LadderParameters(
        first_target_pct=parse_decimal(
            args.first_target_pct, "first target", positive=True
        ),
        second_target_pct=parse_decimal(
            args.second_target_pct, "second target", positive=True
        ),
        trail_pct=parse_decimal(args.trail_pct, "trail", positive=True),
        first_weight=parse_decimal(
            args.first_weight, "first weight", positive=True
        ),
        second_weight=parse_decimal(
            args.second_weight, "second weight", positive=True
        ),
        trail_weight=parse_decimal(
            args.trail_weight, "trail weight", positive=True
        ),
    )


def _fees_from_args(args: argparse.Namespace) -> Tuple[FeeModel, ...]:
    return (
        FeeModel(
            "taker_round_trip",
            parse_decimal(
                args.taker_round_trip_fee_pct,
                "taker round-trip fee",
                non_negative=True,
            ),
        ),
        FeeModel(
            "maker_exit",
            parse_decimal(
                args.maker_exit_round_trip_fee_pct,
                "maker-exit round-trip fee",
                non_negative=True,
            ),
        ),
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        horizons = _horizons_from_args(args.horizon_minutes)
        parameters = _parameters_from_args(args)
        fee_models = _fees_from_args(args)
        filtered_paths = (
            tuple(args.filtered)
            if args.filtered
            else tuple(sorted(args.filtered_dir.glob(args.filtered_pattern)))
        )
        result = run_rescore(
            filtered_paths,
            args.data_dir,
            horizons,
            parameters,
            fee_models,
        )
    except RescoreError as error:
        print(f"RESCORE | ERROR | {error}", file=sys.stderr)
        return 2
    for row in summary_rows(result):
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
