#!/usr/bin/env python3
"""Measure AB23 signed-basis lock-ins from executable trade-print proxies."""
from __future__ import annotations

import argparse, csv, io, json, os, sys, zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

try:
    from study import absorption, fine_slice_study as fine, tape_pull
except ModuleNotFoundError as error:
    if error.name != "study":
        raise
    import absorption  # type: ignore
    import fine_slice_study as fine  # type: ignore
    import tape_pull  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "lane-logs/AB23/basis-funding-cost.jsonl"
TAPE = ROOT / "study/data-episode-tape"
OUTPUT = ROOT / "lane-logs/AB26"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MINUTE_MS, DAY_MS = 60_000, 86_400_000
ENTRY_PCT, EXIT_PCT, FEE_PCT = Decimal("2"), Decimal("1"), Decimal("0.30")
SLIPPAGE_PER_LEG_SIDE_PCT, LEG_SIDES = Decimal("0.20"), 4
SLIPPAGE_PCT = SLIPPAGE_PER_LEG_SIDE_PCT * Decimal(LEG_SIDES)
MEASURED_STATUSES = {"MEASURED", "REVERSE_DIRECTION"}
DUPLICATE_FALLBACK_ROW_BOUND = 1_000_000
MAX_TRADE_MEMBER_BYTES = 2_147_483_648


class ExecutableBasisError(Exception): pass


def _decimal(raw: Any, source: str, *, positive: bool = False) -> Decimal:
    if raw is None or isinstance(raw, bool): raise ExecutableBasisError(f"{source} is not decimal text")
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise ExecutableBasisError(f"{source} is not decimal text") from None
    if not value.is_finite() or (positive and value <= 0): raise ExecutableBasisError(f"{source} is outside its valid range")
    return value


def _utc_ms(raw: Any, source: str) -> int:
    if not isinstance(raw, str): raise ExecutableBasisError(f"{source} is not canonical UTC text")
    try:
        value = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ExecutableBasisError(f"{source} is not canonical UTC text") from None
    delta = value - EPOCH
    result = (delta.days * 86_400 + delta.seconds) * 1_000 + delta.microseconds // 1_000
    if delta.microseconds % 1_000 or result % MINUTE_MS or absorption.utc_text(result) != raw: raise ExecutableBasisError(f"{source} is not a canonical minute")
    return result


def _load(path: Path) -> tuple[list[dict[str, Any]], int]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ExecutableBasisError("AB23 rows are unreadable") from error
    measured, identifiers = [], set()
    for ordinal, line in enumerate(lines, 1):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise ExecutableBasisError(f"AB23 row {ordinal} is invalid") from error
        if not isinstance(raw, Mapping): raise ExecutableBasisError(f"AB23 row {ordinal} is not an object")
        identifier = raw.get("episode_id")
        if not isinstance(identifier, str) or not identifier or identifier in identifiers: raise ExecutableBasisError("AB23 episode identifier differs")
        identifiers.add(identifier)
        if raw.get("status") != "MEASURED": continue
        try:
            symbol = tape_pull._validate_symbol(raw.get("symbol"))
        except tape_pull.TapePullError as error:
            raise ExecutableBasisError(f"{identifier} symbol differs: {error}") from None
        label, hedge = raw.get("class"), raw.get("hedge")
        if label not in {"HELD", "COLLAPSED"} or hedge not in {"LONG_SPOT_SHORT_PERP", "SHORT_SPOT_LONG_PERP"}: raise ExecutableBasisError(f"{identifier} class or hedge differs")
        base = _utc_ms(raw.get("base_utc"), f"{identifier} base")
        peak = _utc_ms(raw.get("peak_utc"), f"{identifier} peak")
        if not base <= peak <= base + DAY_MS: raise ExecutableBasisError(f"{identifier} rise window differs")
        row = dict(raw)
        row.update({
            "symbol": symbol,
            "_base_ms": base,
            "_peak_ms": peak,
            "_funding_pct": _decimal(raw.get("funding_cost_pct"), f"{identifier} funding"),
            "_close_capture_pct": _decimal(raw.get("gap_captured_pct"), f"{identifier} capture"),
        })
        measured.append(row)
    if not measured: raise ExecutableBasisError("AB23 has no measured rows")
    return measured, len(lines)


def _days(start_ms: int, end_ms: int) -> tuple[int, ...]:
    first, last = start_ms - start_ms % DAY_MS, end_ms - end_ms % DAY_MS
    return tuple(range(first, last + DAY_MS, DAY_MS))


def _archive(root: Path, venue: str, symbol: str, day_ms: int) -> Path:
    prefix = Path("spot") if venue == "spot" else Path("futures/um")
    day = absorption.utc_text(day_ms)[:10]
    return root / prefix / "daily/aggTrades" / symbol / f"{symbol}-aggTrades-{day}.zip"


def _base_result(row: Mapping[str, Any], tape: Path) -> tuple[dict[str, Any], tuple[int, ...]]:
    days = _days(row["_base_ms"], row["_peak_ms"] + DAY_MS)
    present = {
        venue: [day for day in days if _archive(tape, venue, row["symbol"], day).is_file()]
        for venue in ("spot", "futures")
    }
    complete = [venue for venue in present if len(present[venue]) == len(days)]
    seen = [venue for venue in present if present[venue]]
    if len(complete) == 2: coverage, detail = "FULL", "FULL_PAIR"
    elif seen:
        coverage = "PARTIAL"
        detail = f"{complete[0].upper()}_ONLY_COMPLETE" if len(complete) == 1 else "WINDOW_INCOMPLETE_" + "_AND_".join(venue.upper() for venue in seen)
    else: coverage, detail = "ABSENT", "NO_TAPE"
    status = "READY" if coverage == "FULL" else coverage
    direction = "FORWARD" if row["hedge"] == "LONG_SPOT_SHORT_PERP" else "REVERSE"
    result = {
        "episode_id": row["episode_id"], "symbol": row["symbol"], "class": row["class"],
        "base_utc": row["base_utc"], "peak_utc": row["peak_utc"], "ab23_hedge": row["hedge"],
        "execution_direction": direction,
        "tape_coverage": coverage, "coverage_detail": detail, "required_days": len(days),
        "spot_days_present": len(present["spot"]), "futures_days_present": len(present["futures"]),
        "status": status, "entry_utc": None, "entry_gap_pct": None, "exit_utc": None,
        "exit_gap_pct": None, "close_reason": None, "hold_minutes": None,
        "entry_observable_minutes": None, "entry_missing_proxy_minutes": None,
        "exit_observable_minutes": None, "exit_missing_proxy_minutes": None,
        "executable_capture_pct": None, "ab23_close_capture_pct": row["_close_capture_pct"],
        "ab23_funding_cost_pct": row["_funding_pct"], "fee_pct": FEE_PCT,
        "slippage_per_leg_side_pct": SLIPPAGE_PER_LEG_SIDE_PCT, "leg_sides": LEG_SIDES,
        "total_slippage_pct": SLIPPAGE_PCT, "net_pct": None,
    }
    return result, days


def _verified_trade_minutes(path: Path, day_ms: int, *, exact_duplicates: bool) -> tuple[dict[int, dict[str, Decimal]], int, int]:
    minutes: dict[int, dict[str, Decimal]] = {}; seen: dict[int, tuple[str, ...]] = {}
    count = duplicates = 0; previous = None; expected = path.stem + ".csv"; day_us = day_ms * 1_000
    try:
        with zipfile.ZipFile(path, "r") as archive:
            members = [member for member in archive.infolist() if not member.is_dir()]
            if len(members) != 1 or members[0].filename != expected or members[0].flag_bits & 0x1:
                raise ExecutableBasisError(f"{path.name} member differs")
            chunks = tape_pull.stream_verified_member(archive, members[0], MAX_TRADE_MEMBER_BYTES)
            raw = io.BufferedReader(fine._ChunkStream(chunks))
            with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as handle:
                for row in csv.reader(handle):
                    if not row or not any(cell.strip() for cell in row): continue
                    if not count and row[0].strip().lower().replace(" ", "_") in {"agg_trade_id", "aggregate_trade_id", "aggtradeid"}: continue
                    if len(row) < 7: raise ExecutableBasisError(f"{path.name} row width differs")
                    aggregate_id, first_id, last_id = int(row[0]), int(row[3]), int(row[4])
                    stamp = int(row[5]); stamp_us = stamp if stamp >= 100_000_000_000_000 else stamp * 1_000
                    maker = row[6].strip().lower(); identity = tuple(row)
                    if min(aggregate_id, first_id, last_id) < 0 or last_id < first_id or not day_us <= stamp_us < day_us + fine.shape.DAY_US:
                        raise ExecutableBasisError(f"{path.name} trade identity differs")
                    if maker not in {"true", "false"}: raise ExecutableBasisError(f"{path.name} maker field differs")
                    if exact_duplicates:
                        prior = seen.get(aggregate_id)
                        if prior is not None:
                            if prior != identity: raise ExecutableBasisError(f"{path.name} repeated trade differs")
                            duplicates += 1; continue
                        if len(seen) >= DUPLICATE_FALLBACK_ROW_BOUND:
                            raise ExecutableBasisError(f"{path.name} duplicate fallback row bound reached")
                    key = stamp_us, aggregate_id
                    if previous is not None and key <= previous: raise ExecutableBasisError(f"{path.name} unique trade ordering differs")
                    price, quantity = fine.shape._decimal(row[1]), fine.shape._decimal(row[2])
                    if exact_duplicates: seen[aggregate_id] = identity
                    previous = key; count += 1
                    minute = stamp_us // 60_000_000 * MINUTE_MS; side = "sell" if maker == "true" else "buy"
                    minutes.setdefault(minute, {})[side] = price
                    _ = quantity
        if not count: raise ExecutableBasisError(f"{path.name} has no trades")
        if exact_duplicates and not duplicates: raise ExecutableBasisError(f"{path.name} ordering differs without an exact repeated row")
        return minutes, count, duplicates
    except ExecutableBasisError: raise
    except tape_pull.ArchiveError as error:
        raise ExecutableBasisError(f"{path.name} shared verification differs: {error}") from None
    except fine.shape.StudyError as error:
        raise ExecutableBasisError(f"{path.name} trade value differs: {error}") from None
    except (OSError, UnicodeError, csv.Error, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise ExecutableBasisError(f"{path.name} cannot be parsed: {type(error).__name__}") from None


def _trade_minutes(path: Path, day_ms: int) -> tuple[dict[int, dict[str, Decimal]], int, int, int]:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            members = [member for member in archive.infolist() if not member.is_dir()]
            oversized = len(members) == 1 and members[0].file_size > tape_pull.MAX_MEMBER_BYTES
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise ExecutableBasisError(f"{path.name} cannot be inspected: {type(error).__name__}") from None
    if oversized:
        minutes, count, duplicates = _verified_trade_minutes(path, day_ms, exact_duplicates=False)
        return minutes, count, duplicates, 1
    minutes: dict[int, dict[str, Decimal]] = {}; count = 0
    try:
        for _, stamp_us, price, _, buyer_is_maker in fine._trade_rows(path, day_ms * 1_000):
            minute = stamp_us // 60_000_000 * MINUTE_MS
            side = "sell" if buyer_is_maker else "buy"
            minutes.setdefault(minute, {})[side] = price
            count += 1
    except fine.FineSliceError as error:
        if str(error).endswith(" ordering differs"):
            minutes, count, duplicates = _verified_trade_minutes(path, day_ms, exact_duplicates=True)
            return minutes, count, duplicates, 0
        raise ExecutableBasisError(f"{path.name} trade evidence differs: {error}") from None
    return minutes, count, 0, 0


def _gap(perp: Decimal, spot: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return (perp - spot) / spot * Decimal("100")


def _side(minutes: Mapping[int, Mapping[str, Decimal]], stamp: int, side: str) -> Optional[Decimal]:
    row = minutes.get(stamp); return None if row is None else row.get(side)


def _measure(row: Mapping[str, Any], spot: Mapping[int, Mapping[str, Decimal]], perp: Mapping[int, Mapping[str, Decimal]]) -> dict[str, Any]:
    reverse = row["hedge"] == "SHORT_SPOT_LONG_PERP"
    entry_stamp = entry_gap = None; entry_observed = entry_missing = 0
    for stamp in range(row["_base_ms"], row["_peak_ms"] + MINUTE_MS, MINUTE_MS):
        spot_price = _side(spot, stamp, "sell" if reverse else "buy")
        perp_price = _side(perp, stamp, "buy" if reverse else "sell")
        if spot_price is None or perp_price is None: entry_missing += 1; continue
        entry_observed += 1
        gap = _gap(perp_price, spot_price)
        if (not reverse and gap > ENTRY_PCT) or (reverse and gap < -ENTRY_PCT):
            entry_stamp, entry_gap = stamp, gap
            break
    base = {"entry_observable_minutes": entry_observed, "entry_missing_proxy_minutes": entry_missing}
    if entry_stamp is None or entry_gap is None: return base | {"status": "ENTRY_NOT_REPRODUCED"}
    cap = entry_stamp + DAY_MS
    exit_stamp = exit_gap = reason = None; exit_observed = exit_missing = 0
    for stamp in range(entry_stamp + MINUTE_MS, cap + MINUTE_MS, MINUTE_MS):
        spot_price = _side(spot, stamp, "buy" if reverse else "sell")
        perp_price = _side(perp, stamp, "sell" if reverse else "buy")
        if spot_price is None or perp_price is None: exit_missing += 1; continue
        exit_observed += 1
        gap = _gap(perp_price, spot_price)
        if (not reverse and gap < EXIT_PCT) or (reverse and gap > -EXIT_PCT):
            exit_stamp, exit_gap, reason = stamp, gap, "BELOW_1PCT"
            break
        if stamp == cap:
            exit_stamp, exit_gap, reason = stamp, gap, "CAPPED_24H"
    base.update({
        "entry_utc": absorption.utc_text(entry_stamp), "entry_gap_pct": entry_gap,
        "exit_observable_minutes": exit_observed, "exit_missing_proxy_minutes": exit_missing,
    })
    if exit_stamp is None or exit_gap is None or reason is None: return base | {"status": "EXIT_CAP_PROXY_INCOMPLETE"}
    capture = exit_gap - entry_gap if reverse else entry_gap - exit_gap
    net = capture - row["_funding_pct"] - FEE_PCT - SLIPPAGE_PCT
    return base | {
        "status": "REVERSE_DIRECTION" if reverse else "MEASURED",
        "exit_utc": absorption.utc_text(exit_stamp), "exit_gap_pct": exit_gap,
        "close_reason": reason, "hold_minutes": (exit_stamp - entry_stamp) // MINUTE_MS,
        "executable_capture_pct": capture, "net_pct": net,
    }


def _stats(values: Sequence[Decimal]) -> dict[str, Any]:
    return {
        "n": len(values), "min": min(values) if values else None,
        "median": absorption.quantile(values, 1, 2), "p90": absorption.quantile(values, 9, 10),
        "max": max(values) if values else None,
    }


def _group(rows: Sequence[Mapping[str, Any]], label: Optional[str]) -> dict[str, Any]:
    chosen = [row for row in rows if row["status"] in MEASURED_STATUSES and (label is None or row["class"] == label)]
    positives = sum(row["net_pct"] > 0 for row in chosen)
    return {
        "n": len(chosen), "executable_capture_pct": _stats([row["executable_capture_pct"] for row in chosen]),
        "net_pct": _stats([row["net_pct"] for row in chosen]),
        "hold_minutes": _stats([Decimal(row["hold_minutes"]) for row in chosen]),
        "net_positive": {"count": positives, "fraction": Decimal(positives) / Decimal(len(chosen)) if chosen else None},
    }


def _short(value: Any) -> str:
    return "NA" if value is None else format(value, ".8g") if isinstance(value, Decimal) else str(value)


def _plain(stats: Mapping[str, Any]) -> str:
    return "/".join(_short(stats[key]) for key in ("min", "median", "p90", "max"))


def _summary(rows: Sequence[Mapping[str, Any]], source_rows: int, evidence: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    coverage = Counter(row["tape_coverage"] for row in rows)
    details = Counter(row["coverage_detail"] for row in rows)
    statuses = Counter(row["status"] for row in rows)
    groups = {label: _group(rows, None if label == "ALL" else label) for label in ("ALL", "HELD", "COLLAPSED")}
    paired = [row for row in rows if row["status"] in MEASURED_STATUSES]
    close_stats = _stats([row["ab23_close_capture_pct"] for row in paired])
    executable_stats = _stats([row["executable_capture_pct"] for row in paired])
    difference_stats = _stats([row["ab23_close_capture_pct"] - row["executable_capture_pct"] for row in paired])
    source_directions = Counter(row["execution_direction"] for row in rows)
    measured_directions = Counter(row["execution_direction"] for row in paired)
    all_group = groups["ALL"]
    lines = [
        f"COVERAGE | AB23_rows={source_rows} measured_lock_ins={len(rows)} full_pair={coverage['FULL']} partial={coverage['PARTIAL']} absent={coverage['ABSENT']} partial_detail={dict(sorted(details.items()))} | MEASURED",
        f"DIRECTIONS | forward={measured_directions['FORWARD']} reverse={measured_directions['REVERSE']} source_forward={source_directions['FORWARD']} source_reverse={source_directions['REVERSE']} | MEASURED",
        f"EXECUTABLE_CAPTURE_PCT | n={all_group['n']} min/median/p90/max={_plain(all_group['executable_capture_pct'])} | MEASURED",
        f"NET_PCT | n={all_group['n']} min/median/p90/max={_plain(all_group['net_pct'])} fees={FEE_PCT} slippage={SLIPPAGE_PCT} | MEASURED",
        f"NET_POSITIVE | {all_group['net_positive']['count']}/{all_group['n']}({_short(None if all_group['net_positive']['fraction'] is None else all_group['net_positive']['fraction'] * Decimal('100'))}%) | MEASURED",
    ]
    for label in ("HELD", "COLLAPSED"):
        group = groups[label]
        lines.append(f"{label} | n={group['n']} capture={_plain(group['executable_capture_pct'])} net={_plain(group['net_pct'])} hold_minutes={_plain(group['hold_minutes'])} positive={group['net_positive']['count']}/{group['n']} | MEASURED")
    lines.extend([
        f"HOLD_MINUTES | n={all_group['n']} min/median/p90/max={_plain(all_group['hold_minutes'])} below_1pct={sum(row['close_reason'] == 'BELOW_1PCT' for row in paired)} capped_24h={sum(row['close_reason'] == 'CAPPED_24H' for row in paired)} | MEASURED",
        f"CLOSE_VS_EXECUTABLE | same_n={len(paired)} close={_plain(close_stats)} executable={_plain(executable_stats)} close_minus_executable={_plain(difference_stats)} | MEASURED",
        f"EVIDENCE | archives_read={evidence['archives_read']} trades_parsed={evidence['trades_parsed']} exact_duplicate_rows_ignored={evidence['exact_duplicate_rows_ignored']} members_streamed_to_2g={evidence['members_streamed_to_2g']} spot_bank={evidence['spot_bank']} futures_bank={evidence['futures_bank']} reader=study.tape_pull.stream_verified_member | MEASURED",
    ])
    payload = {
        "schema": "executable-basis-study-v2", "source_rows": source_rows, "ab23_measured_lock_ins": len(rows),
        "owner_direction_lock_ins": source_directions["FORWARD"],
        "source_directions": dict(source_directions), "measured_directions": dict(measured_directions),
        "coverage": dict(coverage), "coverage_detail": dict(details),
        "statuses": dict(statuses), "groups": groups,
        "comparison_same_episodes": {"n": len(paired), "ab23_close_capture_pct": close_stats,
                                     "executable_capture_pct": executable_stats,
                                     "close_minus_executable_pct": difference_stats},
        "evidence": dict(evidence),
        "definitions": {
            "coverage_window": "every daily archive from base through peak plus the possible 24-hour exit",
            "minute_ask_proxy": "last aggressive BUY print in the minute",
            "minute_bid_proxy": "last aggressive SELL print in the minute",
            "entry": "forward first exceeds 2 percent using perpetual bid and spot ask; reverse first falls below negative 2 percent using perpetual ask and spot bid",
            "exit": "forward first falls below 1 percent using perpetual ask and spot bid; reverse first rises above negative 1 percent using perpetual bid and spot ask; otherwise the exact 24-hour cap",
            "direction": "forward is long spot and short perpetual; REVERSE_DIRECTION is long perpetual and short spot and requires spot borrow",
            "exact_duplicate_rows": "byte-equivalent repeated aggregate trade rows are ignored only after the shared verified reader reports non-increasing order",
            "member_ceiling": "shared verified member streaming uses the 2 GiB task ceiling when a member exceeds the shared 1 GiB default",
            "net": "executable capture minus AB23 funding cost minus 0.30 percent fees minus four 0.20 percent leg-side slippage charges",
        },
    }
    return payload, lines


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal): return absorption.decimal_text(value)
    if isinstance(value, Mapping): return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)): return [_jsonable(item) for item in value]
    return value


def _write(path: Path, payload: Any, *, lines: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        if lines:
            for row in payload:
                text = row if isinstance(row, str) else json.dumps(_jsonable(row), sort_keys=True, separators=(",", ":"))
                handle.write(text + "\n")
        else: json.dump(_jsonable(payload), handle, sort_keys=True, separators=(",", ":")); handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=INPUT)
    parser.add_argument("--tape-dir", type=Path, default=TAPE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    try:
        if not args.tape_dir.is_dir(): raise ExecutableBasisError("episode tape directory is unreadable")
        if fine.tape_pull.stream_verified_member is not tape_pull.stream_verified_member: raise ExecutableBasisError("shared ZIP reader binding differs")
        source, source_rows = _load(args.input)
        prepared = []
        for row in source:
            result, days = _base_result(row, args.tape_dir); prepared.append((row, result, days))
        evidence = {
            "archives_read": 0, "trades_parsed": 0, "exact_duplicate_rows_ignored": 0,
            "members_streamed_to_2g": 0,
            "spot_bank": sum(1 for _ in (args.tape_dir / "spot").rglob("*.zip")),
            "futures_bank": sum(1 for _ in (args.tape_dir / "futures").rglob("*.zip")),
            "zip_reader": "study.tape_pull.stream_verified_member via study.fine_slice_study._trade_rows",
        }
        eligible = [item for item in prepared if item[1]["status"] == "READY"]
        grouped: dict[str, list[Any]] = defaultdict(list)
        for item in eligible: grouped[item[0]["symbol"]].append(item)
        for symbol, items in sorted(grouped.items()):
            required = sorted({day for _, _, days in items for day in days})
            books: dict[str, dict[int, dict[str, Decimal]]] = {"spot": {}, "futures": {}}
            for venue in ("spot", "futures"):
                for day in required:
                    minutes, trades, duplicates, extended = _trade_minutes(_archive(args.tape_dir, venue, symbol, day), day)
                    books[venue].update(minutes)
                    evidence["archives_read"] += 1
                    evidence["trades_parsed"] += trades
                    evidence["exact_duplicate_rows_ignored"] += duplicates
                    evidence["members_streamed_to_2g"] += extended
            for row, result, _ in items: result.update(_measure(row, books["spot"], books["futures"]))
        results = sorted((result for _, result, _ in prepared), key=lambda row: row["episode_id"])
        summary, lines = _summary(results, source_rows, evidence)
        _write(args.output_dir / "executable-basis.jsonl", results, lines=True)
        _write(args.output_dir / "executable-basis-summary.json", summary)
        _write(args.output_dir / "executable-basis-comparison.txt", lines, lines=True)
        print(f"DONE | ab23_measured={len(results)} | full_pair={summary['coverage'].get('FULL', 0)} | executable_measured={summary['groups']['ALL']['n']} | exit=0", flush=True)
        return 0
    except (ExecutableBasisError, OSError, UnicodeError, ValueError) as error:
        print(f"executable basis study: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
