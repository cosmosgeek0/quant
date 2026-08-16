#!/usr/bin/env python3
"""Aggregate banked pre-base facts against the six-month pump outcomes."""

from __future__ import annotations

import argparse, json, os, sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation, localcontext
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRIORS = ROOT / "lane-logs/AB8/pump-priors.jsonl"
DEFAULT_CENSUS = ROOT / "lane-logs/AB13/pump-census-6mo.json"
DEFAULT_FEATURES = ROOT / "lane-logs/AB11/slice-features.jsonl"
DEFAULT_RANKS = ROOT / "lane-logs/AB12/ranked-slices.jsonl"
DEFAULT_PRECISION = ROOT / "lane-logs/AB12/precision-grid.json"
DEFAULT_LOWS = ROOT / "lane-logs/AB5/long-low-positions.json"
DEFAULT_FRESH = ROOT / "lane-logs/AB16/fresh-listing-summary.json"
DEFAULT_OUTPUT = ROOT / "lane-logs/AB22"
DEFAULT_REPORT = ROOT / "lane-logs/AB22-report.txt"
OUTCOMES, LISTING_CLASSES = ("HELD", "COLLAPSED"), ("fresh", "veteran")
FACTS = ("yearly_low_bucket", "listing_age_class", "signature_pair_bucket", "component_count_48h")
LOW_CUT, STATED_BASE, REQUIRED_LIFT, MIN_N = Decimal("0.10"), Decimal("0.25"), Decimal("0.10"), 100


class StudyError(Exception): pass


def _read_json(path: Path) -> Any:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StudyError(f"cannot read {path.name}: {error}") from None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try: lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise StudyError(f"cannot read {path.name}: {error}") from None
    rows = []
    for number, line in enumerate(lines, 1):
        if not line.strip(): continue
        try: row = json.loads(line)
        except json.JSONDecodeError: raise StudyError(f"{path.name} row {number} is not JSON") from None
        if not isinstance(row, dict): raise StudyError(f"{path.name} row {number} differs")
        rows.append(row)
    return rows


def _decimal(raw: Any, label: str) -> Decimal:
    if raw is None or isinstance(raw, bool): raise StudyError(f"{label} is not proved")
    try: value = Decimal(str(raw))
    except (InvalidOperation, ValueError): raise StudyError(f"{label} is not Decimal") from None
    if not value.is_finite(): raise StudyError(f"{label} is not finite")
    return value


def _rate(held: int, population: int) -> Optional[Decimal]:
    if population == 0: return None
    with localcontext() as context:
        context.prec = 50; return Decimal(held) / Decimal(population)


def _stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    held = sum(row["outcome"] == "HELD" for row in rows); return {"n": len(rows), "held": held, "held_rate": _rate(held, len(rows))}


def _serial(value: Any) -> Any:
    if isinstance(value, Decimal): return format(value, "f")
    if isinstance(value, Mapping): return {key: _serial(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)): return [_serial(item) for item in value]
    return value


def _write(path: Path, payload: Any, *, lines: bool = False, text: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        if lines:
            for row in payload: handle.write(json.dumps(_serial(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        elif text: handle.write(payload)
        else:
            json.dump(_serial(payload), handle, ensure_ascii=False, sort_keys=True, separators=(",", ":")); handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def _pct(value: Optional[Decimal]) -> str: return "NA" if value is None else f"{value * 100:.2f}%"


def _validate_static(census: Any, lows: Any, fresh: Any) -> tuple[list[dict[str, Any]], Mapping[str, Any]]:
    if not isinstance(census, Mapping) or census.get("header", {}).get("schema") != "pump-census-6mo-v1":
        raise StudyError("AB13 census schema differs")
    episodes = census.get("episodes")
    if not isinstance(episodes, list) or census.get("summary", {}).get("episodes") != len(episodes): raise StudyError("AB13 census population differs")
    if not isinstance(lows, Mapping) or lows.get("schema") != "long-low-positions-v1": raise StudyError("AB5 long-low schema differs")
    symbols = lows.get("symbols")
    if not isinstance(symbols, Mapping) or lows.get("symbol_count") != len(symbols): raise StudyError("AB5 symbol population differs")
    if not isinstance(fresh, Mapping) or fresh.get("schema") != "fresh-listing-study-v1": raise StudyError("AB16 fresh-listing schema differs")
    seen, measured = set(), {group: {"episodes": 0, "held": 0} for group in LISTING_CLASSES}
    for row in episodes:
        if not isinstance(row, dict): raise StudyError("AB13 episode row differs")
        identity, symbol = row.get("episode_id"), row.get("symbol")
        group, outcome = row.get("listing_age_class"), row.get("class")
        if not isinstance(identity, str) or identity in seen or symbol not in symbols: raise StudyError("AB13 episode identity differs")
        if group not in LISTING_CLASSES or outcome not in OUTCOMES: raise StudyError(f"{identity} class differs")
        position = _decimal(row.get("low_position_365"), f"{identity} yearly position")
        if not Decimal("0") <= position <= Decimal("1"): raise StudyError(f"{identity} yearly position is outside zero through one")
        measured[group]["episodes"] += 1; measured[group]["held"] += outcome == "HELD"; seen.add(identity)
    for group in LISTING_CLASSES:
        prior = fresh.get("held_rate", {}).get(group, {})
        if prior.get("episodes") != measured[group]["episodes"] or prior.get("held") != measured[group]["held"]: raise StudyError(f"AB16 {group} census counts differ")
    return episodes, symbols


def _fact_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    orders = {
        "yearly_low_bucket": ("under_0.10", "at_or_above_0.10"),
        "listing_age_class": LISTING_CLASSES,
        "signature_pair_bucket": ("low_low", "other"),
        "component_count_48h": tuple(sorted({str(row["component_count_48h"]) for row in rows}, key=int)),
    }
    results = []
    for fact in FACTS:
        for bucket in orders[fact]:
            selected = [row for row in rows if str(row[fact]) == bucket]; results.append({"fact": fact, "bucket": bucket} | _stats(selected))
    return results


def _two_fact_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for left, right in combinations(FACTS, 2):
        grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows: grouped[(str(row[left]), str(row[right]))].append(row)
        for (left_bucket, right_bucket), selected in sorted(grouped.items()):
            results.append({"left_fact": left, "left_bucket": left_bucket,
                            "right_fact": right, "right_bucket": right_bucket} | _stats(selected))
    return results


def run(priors_path: Path, census_path: Path, features_path: Path, ranks_path: Path,
        precision_path: Path, lows_path: Path, fresh_path: Path, output: Path, report_path: Path) -> Mapping[str, Any]:
    priors, features, ranks = _read_jsonl(priors_path), _read_jsonl(features_path), _read_jsonl(ranks_path)
    census, lows, fresh, precision = (_read_json(census_path), _read_json(lows_path),
                                      _read_json(fresh_path), _read_json(precision_path))
    episodes, symbols = _validate_static(census, lows, fresh)
    best_cell = precision.get("best_at_or_below_one_alarm_per_symbol_day") if isinstance(precision, Mapping) else None
    if not isinstance(precision, Mapping) or precision.get("schema") != "tape-signature-precision-grid-v1" or not isinstance(best_cell, Mapping): raise StudyError("AB12 precision definition differs")
    p99_cut, churn_cut = _decimal(best_cell.get("p99_rank_lte"), "AB12 p99 cut"), _decimal(best_cell.get("churn_rank_lte"), "AB12 churn cut")
    prior_by_id = {}
    for row in priors:
        identity = row.get("episode_id")
        if not isinstance(identity, str) or identity in prior_by_id or row.get("class") not in OUTCOMES: raise StudyError("AB8 prior identity differs")
        prior_by_id[identity] = row
    feature_by_key, metadata_by_id = {}, {}
    for row in features:
        if row.get("group") != "pump_prior": continue
        identity, slice_id, metadata = row.get("stretch_id"), row.get("slice_id"), row.get("metadata")
        if row.get("schema") != "tape-shape-slice-v1" or not isinstance(identity, str) or not isinstance(slice_id, str) or not isinstance(metadata, Mapping): raise StudyError("AB11 pump slice identity differs")
        key = (identity, slice_id)
        if key in feature_by_key: raise StudyError("AB11 pump slice is duplicated")
        feature_by_key[key] = row
        facts = (row.get("symbol"), row.get("venue"), metadata.get("base_utc"), metadata.get("class"))
        if not isinstance(facts[2], str) or not isinstance(row.get("slice_end_utc_exclusive"), str) or row["slice_end_utc_exclusive"] > facts[2]: raise StudyError(f"{identity} AB11 slice is not pre-base")
        if identity in metadata_by_id and metadata_by_id[identity] != facts: raise StudyError(f"{identity} AB11 metadata differs across slices")
        metadata_by_id[identity] = facts
    ranked_by_id: dict[str, list[tuple[dict[str, Any], Decimal, Decimal]]] = defaultdict(list)
    for row in ranks:
        if row.get("group") != "pump_prior": continue
        identity, slice_id = row.get("stretch_id"), row.get("slice_id")
        source = feature_by_key.get((identity, slice_id))
        if row.get("schema") != "tape-signature-ranked-slice-v1" or source is None or source.get("status") != "MEASURED" or not isinstance(source.get("features"), Mapping): raise StudyError("AB12 ranked pump slice lacks its AB11 measured row")
        p99, churn = _decimal(row.get("p99_rank"), f"{slice_id} p99 rank"), _decimal(row.get("churn_rank"), f"{slice_id} churn rank")
        if not Decimal("0") <= p99 <= Decimal("1") or not Decimal("0") <= churn <= Decimal("1"): raise StudyError(f"{slice_id} rank is outside zero through one")
        raw = source["features"]
        if (_decimal(row.get("trade_size_p99_quote"), f"{slice_id} ranked p99") !=
                _decimal(raw.get("trade_size_p99_quote"), f"{slice_id} AB11 p99") or
                _decimal(row.get("price_level_churn_per_1000"), f"{slice_id} ranked churn") !=
                _decimal(raw.get("price_level_churn_per_1000"), f"{slice_id} AB11 churn")):
            raise StudyError(f"{slice_id} AB11 and AB12 features differ")
        ranked_by_id[identity].append((row, p99, churn))
    census_by_key = {}
    for row in episodes:
        key = (row.get("symbol"), row.get("venue"), row.get("base_utc"))
        if key in census_by_key: raise StudyError("AB13 base identity is duplicated")
        census_by_key[key] = row
    table, unmatched = [], []
    for prior_id, ranked in sorted(ranked_by_id.items()):
        prior, metadata = prior_by_id.get(prior_id), metadata_by_id.get(prior_id)
        if prior is None or metadata is None: raise StudyError(f"{prior_id} prior inputs differ")
        symbol, venue, base_utc, prior_class = metadata
        if prior.get("class") != prior_class: raise StudyError(f"{prior_id} prior class differs")
        census_row = census_by_key.get((symbol, venue, base_utc))
        if census_row is None: unmatched.append(prior_id); continue
        if census_row["class"] != prior_class: raise StudyError(f"{prior_id} AB13 outcome differs")
        low_record = symbols[symbol]
        days = low_record.get("days_of_history") if isinstance(low_record, Mapping) else None
        expected_listing = "veteran" if isinstance(days, int) and days >= 90 else "fresh"
        if not isinstance(days, int) or census_row.get("listing_age_days") != days or census_row["listing_age_class"] != expected_listing: raise StudyError(f"{prior_id} listing class differs")
        latest_position = _decimal(low_record.get("low_position_365"), f"{symbol} AB5 position")
        if latest_position != _decimal(census_row.get("ab5_reported_latest_low_position_365"), f"{prior_id} AB13 reported position"): raise StudyError(f"{prior_id} AB5 position differs")
        for ranked_row, _p99, _churn in ranked:
            if latest_position != _decimal(ranked_row.get("yearly_low_position_365"), f"{prior_id} AB12 position"): raise StudyError(f"{prior_id} AB12 position differs")
        components = prior.get("component_elevated")
        if not isinstance(components, Mapping) or len(components) != 5 or any(not isinstance(value, bool) for value in components.values()): raise StudyError(f"{prior_id} component facts differ")
        chosen, p99, churn = min(ranked, key=lambda item: (max(item[1], item[2]), item[1] + item[2], str(item[0].get("slice_end_utc_exclusive"))))
        position = _decimal(census_row["low_position_365"], f"{prior_id} base position")
        table.append({"schema": "base-selection-episode-v1", "census_episode_id": census_row["episode_id"],
                      "prior_episode_id": prior_id, "symbol": symbol, "venue": venue, "base_utc": base_utc,
                      "outcome": census_row["class"], "yearly_low_position_365": position,
                      "yearly_low_bucket": "under_0.10" if position < LOW_CUT else "at_or_above_0.10",
                      "listing_age_class": census_row["listing_age_class"],
                      "signature_slice_id": chosen.get("slice_id"), "signature_p99_rank": p99,
                      "signature_churn_rank": churn,
                      "signature_pair_bucket": "low_low" if p99 <= p99_cut and churn <= churn_cut else "other",
                      "ranked_prior_slices": len(ranked),
                      "component_count_48h": sum(value is True for value in components.values()),
                      "prior_windows_tape_ready": prior.get("windows_tape_ready")})
    table.sort(key=lambda row: (row["base_utc"], row["symbol"], row["census_episode_id"]))
    single, paired = _fact_rows(table), _two_fact_rows(table)
    qualifying_pairs = [row for row in paired if row["n"] >= MIN_N and row["held_rate"] is not None]
    best_pair = max(qualifying_pairs, key=lambda row: (row["held_rate"], row["n"], row["left_fact"], row["right_fact"])) if qualifying_pairs else None
    target = STATED_BASE + REQUIRED_LIFT
    edges = [row for row in single if row["n"] >= MIN_N and row["held_rate"] is not None and row["held_rate"] >= target]
    census_stats = {"n": len(episodes), "held": sum(row["class"] == "HELD" for row in episodes)}; census_stats["held_rate"] = _rate(census_stats["held"], census_stats["n"])
    tape_ready = sum(isinstance(row.get("windows_tape_ready"), int) and row["windows_tape_ready"] > 0 for row in priors)
    coverage = {"prior_rows": len(priors), "tape_ready_prior_rows": tape_ready,
                "without_tape_ready_prior": len(priors) - tape_ready, "tape_ready_without_ranked_pair": tape_ready - len(ranked_by_id),
                "ranked_prior_episodes": len(ranked_by_id), "exact_ab13_joins": len(table),
                "ranked_without_exact_ab13_base": len(unmatched), "ranked_without_exact_ab13_base_ids": unmatched,
                "archive_reads": 0}
    summary = {"schema": "base-selection-study-v1",
               "question": "do measured pre-signals beat the stated base at selecting which base to enter",
               "definitions": {"yearly_low": "AB13 episode base position under the banked 0.10 cut",
                               "listing_class": "AB13 class validated against AB5 and AB16; fresh is under 90 observed history days",
                               "signature_pair": f"AB12 saved pair bounds p99<={p99_cut} and churn<={churn_cut}; representative slice minimizes its higher rank within the prior 48h",
                               "component_count_48h": "count of the five AB8 components elevated in any tape-ready hourly prior window",
                               "complete_case": "ranked AB12 pump episode with an exact symbol, venue, and base-time match in AB13",
                               "edge_rule": f"single fact HELD rate at least {target} with n at least {MIN_N}"},
               "census_base": census_stats, "complete_case_population": _stats(table), "coverage": coverage,
               "single_fact_buckets": single, "two_fact_cells": paired,
               "best_two_fact_at_n_gte_100": best_pair,
               "selection_edge": {"present": bool(edges), "qualifying_buckets": edges,
                                  "stated_base": STATED_BASE, "required_lift": REQUIRED_LIFT, "required_n": MIN_N},
               "limits": "AB8 selected the largest 40 rises; exact complete cases are in-sample and do not include a fresh listing"}
    by_fact = defaultdict(list)
    for row in single: by_fact[row["fact"]].append(row)
    def cell(row: Mapping[str, Any]) -> str: return f"{row['bucket']} n={row['n']} HELD={row['held']} rate={_pct(row['held_rate'])}"
    lines = [f"scope | AB13 census n={census_stats['n']} HELD={census_stats['held']} rate={_pct(census_stats['held_rate'])}; complete pre-fact table n={len(table)} HELD={summary['complete_case_population']['held']} rate={_pct(summary['complete_case_population']['held_rate'])} | MEASURED",
             "yearly-low | " + "; ".join(cell(row) for row in by_fact["yearly_low_bucket"]) + " | MEASURED",
             "listing class | " + "; ".join(cell(row) for row in by_fact["listing_age_class"]) + " | MEASURED",
             "signature pair | " + "; ".join(cell(row) for row in by_fact["signature_pair_bucket"]) + " | MEASURED",
             "components fired in prior 48h | " + "; ".join(cell(row) for row in by_fact["component_count_48h"]) + " | MEASURED",
             ("two-fact combination n>=100 | none; complete-case population is " + str(len(table)) if best_pair is None else
              f"two-fact combination n>=100 | {best_pair['left_fact']}={best_pair['left_bucket']} + {best_pair['right_fact']}={best_pair['right_bucket']}; n={best_pair['n']} HELD={best_pair['held']} rate={_pct(best_pair['held_rate'])}") + " | MEASURED",
             ("selection edge | YES; at least one single-fact bucket reaches the stated 35.00% HELD rate at n>=100" if edges else
              "selection edge | NO; no single-fact bucket reaches n>=100, so none proves at least 35.00% HELD against the stated 25.00% base; this measured door closes") + " | MEASURED",
             f"coverage | priors={coverage['prior_rows']}; tape-ready={coverage['tape_ready_prior_rows']}; ranked episodes={coverage['ranked_prior_episodes']}; exact AB13 base joins={coverage['exact_ab13_joins']}; excluded no-tape/no-rank/rescan-base-mismatch={coverage['without_tape_ready_prior']}/{coverage['tape_ready_without_ranked_pair']}/{coverage['ranked_without_exact_ab13_base']} | MEASURED",
             f"definitions | yearly-low uses AB13 base position <0.10; signature low-low reuses AB12 saved {p99_cut}/{churn_cut} pair and the slice minimizing its higher rank; component count is unique AB8 components elevated within 48h | MEASURED",
             f"limits | AB8 prior cohort was capped at the largest 40 rises; fresh complete cases={sum(row['listing_age_class'] == 'fresh' for row in table)}; archives opened=0; thresholds proposed=0 | MEASURED"]
    _write(output / "episode-facts.jsonl", table, lines=True); _write(output / "base-selection-summary.json", summary)
    _write(report_path, "\n".join(lines) + "\n", text=True)
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag, default in (("--priors", DEFAULT_PRIORS), ("--census", DEFAULT_CENSUS),
                          ("--features", DEFAULT_FEATURES), ("--ranks", DEFAULT_RANKS),
                          ("--precision", DEFAULT_PRECISION), ("--long-lows", DEFAULT_LOWS),
                          ("--fresh-summary", DEFAULT_FRESH), ("--output-dir", DEFAULT_OUTPUT),
                          ("--report", DEFAULT_REPORT)):
        parser.add_argument(flag, type=Path, default=default)
    args = parser.parse_args(argv)
    try:
        result = run(args.priors, args.census, args.features, args.ranks, args.precision,
                     args.long_lows, args.fresh_summary, args.output_dir, args.report)
    except (StudyError, OSError, UnicodeError, TypeError, ValueError) as error: print(f"base selection study: {error}", file=sys.stderr); return 2
    population = result["complete_case_population"]; print(f"AB22 | complete={population['n']} | held={population['held']} | edge={result['selection_edge']['present']} | output={args.output_dir}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
