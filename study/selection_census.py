#!/usr/bin/env python3
"""Measure whether banked pre-base facts lift the census HELD rate."""

from __future__ import annotations

import argparse, bisect, json, os, sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation, getcontext
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FINE, DEFAULT_CENSUS = ROOT / "lane-logs/AB24/fine-slices.jsonl", ROOT / "lane-logs/AB13/pump-census-6mo.json"
DEFAULT_LOWS, DEFAULT_FRESH, DEFAULT_REPORT = (ROOT / "lane-logs/AB5/long-low-positions.json",
                                               ROOT / "lane-logs/AB16/fresh-listing-summary.json", ROOT / "lane-logs/AB24-report.txt")
FEATURES = ("trade_size_p99_quote", "price_level_churn_per_1000")
FACTS = (("yearly_low", ("under_0.10", "not_under_0.10")), ("listing_class", ("veteran", "fresh")),
         ("signature_pair", ("low", "high")))
OUTCOMES, TERCILES = ("HELD", "COLLAPSED"), ("low_rise", "middle_rise", "high_rise")
LOW_CUT, PAIR_HALF, REQUIRED_LIFT, MIN_N = Decimal("0.10"), Decimal("0.5"), Decimal("0.10"), 100


class StudyError(Exception): pass


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StudyError(f"cannot read {path.name}: {error}") from None


def _decimal(raw: Any, label: str) -> Decimal:
    if raw is None or isinstance(raw, bool):
        raise StudyError(f"{label} is not proved")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise StudyError(f"{label} is not Decimal") from None
    if not value.is_finite():
        raise StudyError(f"{label} is not finite")
    return value


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    if not ordered: raise StudyError("median has no measured values")
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _rank(value: Decimal, ordered: Sequence[Decimal]) -> Decimal:
    if len(ordered) < 2: raise StudyError("rank pool has fewer than two episodes")
    left = bisect.bisect_left(ordered, value)
    right = bisect.bisect_right(ordered, value)
    return Decimal(left + right - 1) / Decimal(2 * (len(ordered) - 1))


def _rate(rows: Sequence[Mapping[str, Any]]) -> Optional[Decimal]:
    if not rows: return None
    return Decimal(sum(row["outcome"] == "HELD" for row in rows)) / Decimal(len(rows))


def _cell(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {"n": len(rows), "held": sum(row["outcome"] == "HELD" for row in rows), "rate": _rate(rows)}


def _pct(value: Optional[Decimal]) -> str: return "NA" if value is None else f"{value * 100:.2f}%"


def _cell_text(bucket: str, rows: Sequence[Mapping[str, Any]]) -> str:
    stats = _cell(rows)
    return f"{bucket} n={stats['n']} HELD={stats['held']} rate={_pct(stats['rate'])}"


def _fine_rows(path: Path) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    try:
        handle = open(path, "r", encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise StudyError(f"cannot read {path.name}: {error}") from None
    with handle:
        for number, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                raise StudyError(f"fine row {number} is not JSON") from None
            identity = row.get("episode_id") if isinstance(row, Mapping) else None
            if not isinstance(row, Mapping) or row.get("schema") != "fine-slice-study-v1" or not isinstance(identity, str):
                raise StudyError(f"fine row {number} identity differs")
            grouped[identity].append(row)
    if not grouped: raise StudyError("fine slices have no episodes")
    return grouped


def _inputs(census: Any, lows: Any, fresh: Any) -> tuple[dict[str, dict[str, Any]], Mapping[str, Any]]:
    if not isinstance(census, Mapping) or census.get("header", {}).get("schema") != "pump-census-6mo-v1": raise StudyError("AB13 census schema differs")
    episodes = census.get("episodes")
    if not isinstance(episodes, list) or census.get("summary", {}).get("episodes") != len(episodes):
        raise StudyError("AB13 census population differs")
    if not isinstance(lows, Mapping) or lows.get("schema") != "long-low-positions-v1": raise StudyError("AB5 yearly-position schema differs")
    symbols = lows.get("symbols")
    if not isinstance(symbols, Mapping) or lows.get("symbol_count") != len(symbols):
        raise StudyError("AB5 symbol population differs")
    if not isinstance(fresh, Mapping) or fresh.get("schema") != "fresh-listing-study-v1": raise StudyError("AB16 listing schema differs")
    indexed: dict[str, dict[str, Any]] = {}
    counts = {group: {"episodes": 0, "held": 0} for group in ("fresh", "veteran")}
    for row in episodes:
        if not isinstance(row, Mapping): raise StudyError("AB13 episode row differs")
        identity = row.get("episode_id")
        group, outcome = row.get("listing_age_class"), row.get("class")
        if not isinstance(identity, str) or identity in indexed or group not in counts or outcome not in OUTCOMES:
            raise StudyError("AB13 episode identity or label differs")
        counts[group]["episodes"] += 1
        counts[group]["held"] += outcome == "HELD"
        indexed[identity] = dict(row)
    for group in counts:
        prior = fresh.get("held_rate", {}).get(group, {})
        if prior.get("episodes") != counts[group]["episodes"] or prior.get("held") != counts[group]["held"]:
            raise StudyError(f"AB16 {group} labels differ from AB13")
    return indexed, symbols


def _episodes(fine: Mapping[str, Sequence[Mapping[str, Any]]], census: Mapping[str, dict[str, Any]],
              symbols: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    result, absent, invalid, incomplete = [], 0, 0, 0
    for identity, slices in sorted(fine.items()):
        source = census.get(identity)
        if source is None or len(slices) != 24 or {row.get("slice_index") for row in slices} != set(range(24)): raise StudyError(f"{identity} fine-slice coverage differs")
        facts = {(row.get("symbol"), row.get("venue"), row.get("class"), row.get("base_utc")) for row in slices}
        expected = (source.get("symbol"), source.get("venue"), source.get("class"), source.get("base_utc"))
        if facts != {expected}: raise StudyError(f"{identity} fine and AB13 facts differ")
        statuses = {row.get("episode_status") for row in slices}
        if statuses != {"FULLY_BANKED"}:
            if statuses == {"TAPE_ABSENT"}: absent += 1
            elif statuses == {"TAPE_INVALID"}: invalid += 1
            else: raise StudyError(f"{identity} episode status differs")
            continue
        values = {feature: [] for feature in FEATURES}
        for row in slices:
            if row.get("status") not in {"MEASURED", "NO_TRADES"} or not isinstance(row.get("features"), Mapping):
                raise StudyError(f"{identity} measured slice differs")
            for feature in FEATURES:
                raw = row["features"].get(feature)
                if raw is not None:
                    values[feature].append(_decimal(raw, f"{identity} {feature}"))
        if any(not values[feature] for feature in FEATURES): incomplete += 1; continue
        symbol = str(source["symbol"])
        low_row = symbols.get(symbol)
        if not isinstance(low_row, Mapping): raise StudyError(f"{identity} lacks its AB5 symbol")
        days = low_row.get("days_of_history")
        listing = "veteran" if isinstance(days, int) and days >= 90 else "fresh"
        if not isinstance(days, int) or source.get("listing_age_days") != days or source.get("listing_age_class") != listing:
            raise StudyError(f"{identity} AB5 and AB16 listing class differs")
        latest = _decimal(low_row.get("low_position_365"), f"{identity} AB5 latest position")
        if latest != _decimal(source.get("ab5_reported_latest_low_position_365"), f"{identity} AB13 AB5 position"):
            raise StudyError(f"{identity} AB5 position join differs")
        position = _decimal(source.get("low_position_365"), f"{identity} episode yearly position")
        if not Decimal(0) <= position <= Decimal(1): raise StudyError(f"{identity} yearly position is outside zero through one")
        result.append({"episode_id": identity, "symbol": symbol, "outcome": source["class"],
                       "rise": _decimal(source.get("rise_pct"), f"{identity} rise"),
                       "yearly_low": "under_0.10" if position < LOW_CUT else "not_under_0.10",
                       "listing_class": listing, FEATURES[0]: _median(values[FEATURES[0]]),
                       FEATURES[1]: _median(values[FEATURES[1]])})
    return result, {"census_episodes": len(census), "fine_episodes": len(fine), "tape_absent": absent, "tape_invalid": invalid, "feature_incomplete": incomplete}


def _rank_episodes(rows: list[dict[str, Any]]) -> dict[str, int]:
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows: by_symbol[row["symbol"]].append(row)
    global_pools = {feature: sorted(row[feature] for row in rows) for feature in FEATURES}
    sources = {"symbol_episode_set": 0, "census_pool_fallback": 0}
    for row in rows:
        local = by_symbol[row["symbol"]]
        own = len(local) >= 2
        pools = {feature: sorted(item[feature] for item in local) if own else global_pools[feature] for feature in FEATURES}
        ranks = [_rank(row[feature], pools[feature]) for feature in FEATURES]
        row["rank_source"] = "symbol_episode_set" if own else "census_pool_fallback"
        row["signature_pair"] = "low" if max(ranks) <= PAIR_HALF else "high"
        sources[row["rank_source"]] += 1
    rise_pool = sorted(row["rise"] for row in rows)
    for row in rows:
        rank = _rank(row["rise"], rise_pool)
        row["rise_tercile"] = TERCILES[0] if rank < Decimal(1) / Decimal(3) else TERCILES[1] if rank < Decimal(2) / Decimal(3) else TERCILES[2]
    return sources


def _report(rows: list[dict[str, Any]], coverage: Mapping[str, int], sources: Mapping[str, int]) -> list[str]:
    base = _rate(rows)
    if base is None: raise StudyError("complete population is empty")
    lines = [f"overall | n={len(rows)} HELD={sum(row['outcome'] == 'HELD' for row in rows)} rate={_pct(base)} | MEASURED"]
    singles = []
    for fact, buckets in FACTS:
        all_cells = [_cell_text(bucket, [row for row in rows if row[fact] == bucket]) for bucket in buckets]
        split = []
        for tercile in TERCILES:
            split.extend(_cell_text(f"{tercile}/{bucket}", [row for row in rows if row["rise_tercile"] == tercile and row[fact] == bucket]) for bucket in buckets)
        lines.append(f"{fact} | all: {'; '.join(all_cells)} | rise terciles: {'; '.join(split)} | MEASURED")
        singles.extend(({"description": f"{fact}={bucket}"} | _cell([row for row in rows if row[fact] == bucket])) for bucket in buckets)
    paired = []
    for (left, left_buckets), (right, right_buckets) in combinations(FACTS, 2):
        for left_bucket in left_buckets:
            for right_bucket in right_buckets:
                selected = [row for row in rows if row[left] == left_bucket and row[right] == right_bucket]
                paired.append({"description": f"{left}={left_bucket} + {right}={right_bucket}"} | _cell(selected))
    eligible_pairs = [cell for cell in paired if cell["n"] >= MIN_N and cell["rate"] is not None]
    best = max(eligible_pairs, key=lambda cell: (cell["rate"], cell["n"], cell["description"])) if eligible_pairs else None
    lines.append("best two-fact combination | none at n>=100 | MEASURED" if best is None else f"best two-fact combination | {best['description']} | n={best['n']} HELD={best['held']} rate={_pct(best['rate'])} | MEASURED")
    eligible = [cell for cell in singles + paired if cell["n"] >= MIN_N and cell["rate"] is not None]
    lifted = [cell for cell in eligible if cell["rate"] - base >= REQUIRED_LIFT]
    best_any = max(eligible, key=lambda cell: (cell["rate"], cell["n"], cell["description"])) if eligible else None
    answer = "YES" if lifted else "NO"
    best_text = "best eligible=none" if best_any is None else f"best eligible={best_any['description']} n={best_any['n']} rate={_pct(best_any['rate'])} lift={(best_any['rate'] - base) * Decimal(100):.2f} points"
    lines.append(f"flat answer | {answer}; any single fact or pair at n>=100 lifts HELD by 10+ points above base={_pct(base)}; {best_text} | MEASURED")
    lines.append(f"coverage | AB13 episodes={coverage['census_episodes']}; fine episodes={coverage['fine_episodes']}; complete signatures={len(rows)}; tape absent={coverage['tape_absent']}; tape invalid={coverage['tape_invalid']}; feature incomplete={coverage['feature_incomplete']}; rank source symbol/census={sources['symbol_episode_set']}/{sources['census_pool_fallback']} | MEASURED")
    lines.append("method | yearly-low uses the AB13 episode-base AB5 field with the AB5 snapshot join validated; listing labels are checked against AB16; signature is the Decimal final-12h median p99 size and churn; tie-midrank uses own-symbol episodes when n>=2, otherwise census; low means both ranks are in the lower half; rise uses census rank terciles; candidate thresholds proposed=0 | MEASURED")
    return lines


def _write(path: Path, lines: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def run(fine_path: Path, census_path: Path, lows_path: Path, fresh_path: Path, report_path: Path) -> list[str]:
    census, lows, fresh = _read_json(census_path), _read_json(lows_path), _read_json(fresh_path)
    indexed, symbols = _inputs(census, lows, fresh)
    rows, coverage = _episodes(_fine_rows(fine_path), indexed, symbols)
    sources = _rank_episodes(rows)
    lines = _report(rows, coverage, sources)
    _write(report_path, lines)
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag, default in (("--fine-slices", DEFAULT_FINE), ("--census", DEFAULT_CENSUS), ("--long-lows", DEFAULT_LOWS),
                          ("--fresh-summary", DEFAULT_FRESH), ("--report", DEFAULT_REPORT)):
        parser.add_argument(flag, type=Path, default=default)
    args = parser.parse_args(argv)
    getcontext().prec = 50
    try:
        lines = run(args.fine_slices, args.census, args.long_lows, args.fresh_summary, args.report)
    except (StudyError, OSError, UnicodeError, TypeError, ValueError) as error:
        print(f"selection census: {error}", file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
