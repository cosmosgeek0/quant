#!/usr/bin/env python3
"""Write read-only reverse-intent shadow receipts from executable gaps.

For BASIS-LOCKIN-SPEC row 120 only.  A gap is not a ``DislocationEvent``:
``basis_events.py:18-21`` requires a fresh volume/price impulse and gap growth.
Rows measure reverse economics without authority; unknowns reach the fixed
refusal order in :mod:`reverse_rung_intent`.

Optional books use ``RECEIPT_DIR/reverse_depth.json`` keyed by symbol;
``RECEIPT_DIR/funding_info.json`` accepts keyed or raw fundingInfo rows.  Missing symbol rows retain stamped fallback sources.
"""
from __future__ import annotations

import argparse, json, os, sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import basis_kernel, basis_ladder, executable_gap, executable_gap_watch, funding_ruler, limits, margin_ops
import reverse_rung_intent as reverse
from runtime_paths import PATHS, RuntimePaths

GAP_FILENAME = "executable_gaps.jsonl"; OUTPUT_FILENAME = "reverse_shadow.jsonl"
METADATA_FILENAME = "stream_metadata.json"; DEPTH_FILENAME = "reverse_depth.json"
FUNDING_INFO_FILENAME = "funding_info.json"; TAIL_MAX_BYTES = 4 * 1024 * 1024
ZERO = Decimal("0")
DEFAULT_HOLD_MINUTES = Decimal(basis_kernel.DEFAULT_CONFIG.default_hold_ms) / Decimal("60000")
SHADOW_NOTIONAL = min(limits.FIRST_TRADE_CAP_USDT, margin_ops.PRINCIPAL_CEILING)
DEFAULT_LADDER_CONFIG = basis_ladder.LadderConfig(
    schedule=((Decimal("2"), SHADOW_NOTIONAL),), max_rungs=1,
    max_total_notional=SHADOW_NOTIONAL, min_notional=Decimal("0.00000001"), convergence_gap=ZERO)


class ReverseShadowError(Exception): pass


@dataclass(frozen=True)
class GapCandidate: utc: str; reading: executable_gap.ExecutableGapReading


@dataclass(frozen=True)
class ShadowBorrowInput:
    asset: Optional[str]; borrowable_amount: Optional[Decimal]; hourly_interest_rate: Optional[Decimal]; borrow_power: bool = False


@dataclass(frozen=True)
class ShadowSummary:
    symbols_evaluated: int; reverse_direction: int; would_intent: int; top_5: Tuple[Tuple[str, Decimal], ...]; gate_histogram: Tuple[Tuple[str, int], ...]


@dataclass(frozen=True)
class ShadowPass: rows: Tuple[Mapping[str, Any], ...]; summary: ShadowSummary


def _decimal(raw: Any, *, signed: bool = False, positive: bool = False) -> Optional[Decimal]:
    if type(raw) is not str or not raw: return None
    try: value = Decimal(raw)
    except (InvalidOperation, ValueError): return None
    if not value.is_finite() or (positive and value <= ZERO) or (not signed and not positive and value < ZERO): return None
    return value


def _integer(raw: Any) -> Optional[int]:
    if type(raw) is int and raw >= 0: return raw
    if type(raw) is str and raw.isdigit(): return int(raw)
    return None


def _text(value: Decimal) -> str: return "0" if value == ZERO else format(value.normalize(), "f")


def _tail_rows(path: Path):
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            start = max(0, size - TAIL_MAX_BYTES)
            if start:
                handle.seek(start - 1)
                if handle.read(1) != b"\n": handle.readline()
            for raw in handle:
                if not raw.strip(): continue
                try: row = json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    raise ReverseShadowError("executable gap ledger has an invalid row") from None
                if not isinstance(row, dict):
                    raise ReverseShadowError("executable gap ledger row must be an object")
                yield row
    except FileNotFoundError:
        raise ReverseShadowError(f"executable gap ledger is unavailable: {path}") from None
    except ReverseShadowError: raise
    except OSError:
        raise ReverseShadowError(f"executable gap ledger is unreadable: {path}") from None


def latest_ok_candidates(path: Path) -> Tuple[GapCandidate, ...]:
    latest: Dict[str, GapCandidate] = {}
    for row in _tail_rows(Path(path)):
        if row.get("result") != "OK": continue
        symbol = row.get("symbol")
        entry = _decimal(row.get("entry_gap_pct"), signed=True)
        exit_gap = _decimal(row.get("exit_gap_pct"), signed=True)
        now_ms = executable_gap_watch._utc_ms(row.get("utc"))
        ages = (_integer(row.get("spot_age_ms")), _integer(row.get("perp_age_ms")))
        skew = row.get("skew_checked")
        if type(symbol) is not str or executable_gap.SYMBOL_RE.fullmatch(symbol) is None \
                or entry is None or exit_gap is None or now_ms is None or None in ages \
                or type(skew) is not bool:
            raise ReverseShadowError("executable gap ledger has an invalid OK row")
        reading = executable_gap.ExecutableGapReading(
            symbol, now_ms, entry, exit_gap, ages[0], ages[1], skew_checked=skew)
        latest[symbol] = GapCandidate(row["utc"], reading)
    return tuple(latest[symbol] for symbol in sorted(latest))


def _read_payload(path: Path, label: str):
    try:
        with open(path, encoding="utf-8") as handle: payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ReverseShadowError(f"{label} is unreadable: {path}") from None
    return payload


def _read_object(path: Path, label: str) -> Mapping[str, Any]:
    payload = _read_payload(path, label)
    if not isinstance(payload, dict): raise ReverseShadowError(f"{label} must be an object")
    return payload


def _optional_rows(path: Path, label: str) -> Mapping[str, Any]:
    if not path.exists(): return {}
    payload = _read_payload(path, label)
    if isinstance(payload, list):
        return {row["symbol"]: row for row in payload if isinstance(row, dict) and type(row.get("symbol")) is str}
    if not isinstance(payload, dict): raise ReverseShadowError(f"{label} has an invalid shape")
    nested = payload.get("symbols")
    return nested if isinstance(nested, dict) else payload


def _depth_input(symbol: str, rows: Mapping[str, Any]):
    row = rows.get(symbol)
    if row is None: return None
    if not isinstance(row, dict): row = {}
    return reverse.ReverseDepthInput(row.get("spot_depth"), row.get("perp_depth"))


def _funding_input(symbol: str, rows: Mapping[str, Any]):
    row = rows.get(symbol)
    if row is None: return None
    if not isinstance(row, dict): row = {}
    cap = row.get("cap", row.get("adjustedFundingRateCap"))
    floor = row.get("floor", row.get("adjustedFundingRateFloor"))
    interval = row.get("interval_hours", row.get("fundingIntervalHours"))
    info = funding_ruler.FundingInfo(_decimal(cap, signed=True),
                                    _decimal(floor, signed=True), _integer(interval))
    return reverse.ReverseFundingInput(info, DEFAULT_HOLD_MINUTES)


def load_stream_inputs(paths: RuntimePaths) -> Tuple[Mapping[str, Any], Mapping[str, Any]]:
    state = _read_object(paths.stream_state, "stream state")
    metadata_path = paths.stream_state.with_name(METADATA_FILENAME)
    metadata = _read_object(metadata_path, "stream metadata") if metadata_path.exists() else {}
    return state, metadata


def _stream_record(symbol: str, state: Mapping[str, Any], metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    source = state.get(symbol)
    record = dict(source) if isinstance(source, dict) else {}
    for venue in ("spot", "perp"):
        rows = metadata.get(venue)
        cached = rows.get(symbol) if isinstance(rows, dict) else None
        if isinstance(cached, dict):
            record[f"{venue}_step"] = cached.get("step")
            record[f"{venue}_min_notional"] = cached.get("min_notional")
    rows = metadata.get("margin")
    margin = rows.get(symbol) if isinstance(rows, dict) else None
    if isinstance(margin, dict): record["hourly_interest_rate"] = margin.get("hourly_interest_rate")
    return record


def _asset(symbol: str) -> Optional[str]:
    return (symbol[:-4] if symbol.endswith("USDT") else "") or None


def _intent_inputs(candidate: GapCandidate, record: Mapping[str, Any],
                   assume: bool, config: basis_ladder.LadderConfig,
                   depth_input=None, funding_input=None):
    spot_bid = _decimal(record.get("spot_bid"), positive=True)
    borrowable = None
    if assume and spot_bid is not None:
        with localcontext() as context:
            context.prec = 50; borrowable = SHADOW_NOTIONAL / spot_bid
    shadow = ShadowBorrowInput(_asset(candidate.reading.symbol), borrowable,
                               _decimal(record.get("hourly_interest_rate")), assume)
    market = reverse.ReverseMarketMetadata(
        spot_bid, _decimal(record.get("spot_step"), positive=True),
        _decimal(record.get("spot_min_notional")), _decimal(record.get("perp_ask"), positive=True),
        _decimal(record.get("perp_step"), positive=True),
        _decimal(record.get("perp_min_notional")), _decimal(record.get("funding_rate"), signed=True),
        _integer(record.get("next_funding_ms")), _integer(record.get("funding_period_ms")))
    return (
        reverse.DirectionalGap(candidate.reading, basis_ladder.REVERSE),
        reverse.LadderSnapshot(basis_ladder.LadderState.flat(basis_ladder.REVERSE), config),
        reverse.BorrowFacts(shadow.asset, shadow.borrowable_amount, shadow.hourly_interest_rate,
                            shadow.borrow_power),
        market, reverse.ReverseRungCaps(
            limits.PER_LEG_CAP_USDT, limits.FIRST_TRADE_CAP_USDT, limits.MEASURED_ROUNDTRIP_FEE_PCT),
        depth_input, funding_input)


def evaluate_candidate(candidate: GapCandidate, record: Mapping[str, Any], assume_borrow_power: bool,
                       ladder_config: basis_ladder.LadderConfig = DEFAULT_LADDER_CONFIG, depth_input=None,
                       funding_input=None) -> Dict[str, Any]:
    result = reverse.build_reverse_rung_intent(*_intent_inputs(
        candidate, record, assume_borrow_power, ladder_config,
        depth_input, funding_input))
    row: Dict[str, Any] = {
        "utc": candidate.utc, "symbol": candidate.reading.symbol,
        "entry_gap_pct": _text(candidate.reading.entry_gap_pct),
        "would_intent": isinstance(result, reverse.ReverseRungIntent),
        "shadow_assumption": "borrow_power_assumed" if assume_borrow_power else None,
        "capture_source": result.capture_source, "drag_source": result.drag_source,
        "settlements_crossed": result.settlements_crossed,
        "funding_info_source": FUNDING_INFO_FILENAME if funding_input is not None else "absent"}
    if isinstance(result, reverse.ReverseRungIntent):
        row.update({
            "borrow_qty": _text(result.borrow_qty), "spot_short_qty": _text(result.spot_short_qty),
            "perp_long_qty": _text(result.perp_long_qty), "quote_notional": _text(result.quote_notional),
            "expected_capture_pct": _text(result.expected_capture_pct),
            "drag_pct": _text(result.funding_and_interest_drag_pct), "intent_id": result.intent_id})
    else: row["first_failing_gate"] = result.gate
    return row


def _append_rows(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path = Path(path)
    if not path.parent.is_dir():
        raise ReverseShadowError(f"reverse shadow receipt directory is unavailable: {path.parent}")
    if not rows: return
    try:
        encoded = tuple((json.dumps(dict(row), separators=(",", ":")) + "\n").encode("utf-8") for row in rows)
        if path.is_symlink():
            raise ReverseShadowError("reverse shadow receipt ledger must not be a symlink")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            for payload in encoded:
                if os.write(descriptor, payload) != len(payload): raise OSError("reverse shadow receipt append was incomplete")
            os.fsync(descriptor)
        finally: os.close(descriptor)
    except ReverseShadowError: raise
    except (OSError, TypeError, ValueError):
        raise ReverseShadowError(f"reverse shadow receipt append failed: {path}") from None


def run_once(paths: RuntimePaths = PATHS, *, assume_borrow_power: bool = False,
             ladder_config: basis_ladder.LadderConfig
             = DEFAULT_LADDER_CONFIG) -> ShadowPass:
    if not paths.receipt_dir.is_dir():
        raise ReverseShadowError(f"reverse shadow receipt directory is unavailable: {paths.receipt_dir}")
    candidates = latest_ok_candidates(paths.receipt_dir / GAP_FILENAME)
    state, metadata = load_stream_inputs(paths)
    depth_rows = _optional_rows(paths.receipt_dir / DEPTH_FILENAME, "reverse depth")
    funding_rows = _optional_rows(paths.receipt_dir / FUNDING_INFO_FILENAME, "funding info")
    selected = tuple(row for row in candidates if row.reading.exit_gap_pct < ZERO)
    rows = tuple(evaluate_candidate(
        row, _stream_record(row.reading.symbol, state, metadata),
        assume_borrow_power, ladder_config,
        _depth_input(row.reading.symbol, depth_rows),
        _funding_input(row.reading.symbol, funding_rows)) for row in selected)
    intents = tuple(row for row in rows if row["would_intent"])
    ranked = sorted(((row["symbol"], Decimal(row["expected_capture_pct"])) for row in intents),
                    key=lambda item: (-item[1], item[0]))
    histogram = tuple(
        (gate, sum(row.get("first_failing_gate") == gate for row in rows))
        for gate in reverse.GATE_ORDER
        if any(row.get("first_failing_gate") == gate for row in rows))
    summary = ShadowSummary(len(candidates), len(selected), len(intents), tuple(ranked[:5]), histogram)
    _append_rows(paths.receipt_dir / OUTPUT_FILENAME, rows)
    return ShadowPass(rows, summary)


def format_summary(summary: ShadowSummary) -> str:
    top = ",".join(f"{symbol}:{_text(value)}" for symbol, value in summary.top_5) or "-"
    gates = ",".join(f"{gate}:{count}" for gate, count in summary.gate_histogram) or "-"
    return (
        "REVERSE-LOCKIN-SHADOW | symbols_evaluated=%d reverse_direction=%d "
        "would_intent=%d top_5_expected_capture_pct=%s gate_histogram=%s"
        % (summary.symbols_evaluated, summary.reverse_direction, summary.would_intent, top, gates))


def main(argv: Optional[Sequence[str]] = None, *, paths: RuntimePaths = PATHS) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--assume-borrow-power", action="store_true")
    args = parser.parse_args(argv)
    if not args.once: parser.error("--once is required")
    try: completed = run_once(paths, assume_borrow_power=args.assume_borrow_power)
    except ReverseShadowError as exc:
        print(f"REVERSE-LOCKIN-SHADOW | ERROR | {exc}", file=sys.stderr)
        return 2
    print(format_summary(completed.summary)); return 0


if __name__ == "__main__": raise SystemExit(main())
