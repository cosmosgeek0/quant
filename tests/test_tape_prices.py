#!/usr/bin/env python3
"""Synthetic archive tests for the policy tape-price producer."""

import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


from study import policy_sim as sim
from study import tape_prices


BASE_MS = 1_767_268_800_000
DAY = datetime.fromtimestamp(BASE_MS / 1000, timezone.utc).date().isoformat()


def event_row(event_id, symbol, event_ms=BASE_MS, venue="spot"):
    venue_class = "spot_only" if venue == "spot" else "futures_only"
    return {
        "event_id": event_id,
        "symbol": symbol,
        "label": "GenuineRunner",
        "venue_class": venue_class,
        "source_venue": venue,
        "open_ms": event_ms,
    }


def write_events(path, rows):
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def write_archive(root, symbol, rows, venue="spot"):
    if venue == "spot":
        parent = root / "spot" / "daily" / "aggTrades" / symbol
    else:
        parent = root / "futures" / "um" / "daily" / "aggTrades" / symbol
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / f"{symbol}-aggTrades-{DAY}.zip"
    lines = []
    for aggregate_id, timestamp_ms, price in rows:
        lines.append(
            f"{aggregate_id},{price},1,{aggregate_id},{aggregate_id},"
            f"{timestamp_ms},false,true"
        )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(path.stem + ".csv", "\n".join(lines) + "\n")
    return path


class TapePricesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.events = self.root / "events.jsonl"
        self.output = self.root / "tape.jsonl"
        self.data = self.root / "data"

    def rows(self):
        return [json.loads(line) for line in self.output.read_text().splitlines()]

    def test_rows_exactly_match_consumer_schema_and_raw_trade_granularity(self):
        start_ms = BASE_MS - 60 * tape_prices.MINUTE_MS
        end_ms = BASE_MS + 120 * tape_prices.MINUTE_MS
        write_events(
            self.events,
            [event_row("perp-1", "PERPUSDT", venue="futures")],
        )
        write_archive(
            self.data,
            "PERPUSDT",
            [
                (1, start_ms - 1, "9"),
                (2, start_ms, "1.2300"),
                (3, BASE_MS + 1_000, "1.24"),
                (4, BASE_MS + 2_000, "1.25"),
                (5, end_ms, "1.260"),
                (6, end_ms + 1, "10"),
            ],
            venue="futures",
        )

        summary = tape_prices.run_tape_prices(
            self.events, self.data, self.output
        )
        rows = self.rows()

        self.assertEqual(summary, {"events": 1, "absent": 0, "rows": 4})
        self.assertEqual(
            [set(row) for row in rows],
            [{"symbol", "venue", "timestamp_ms", "price"}] * 4,
        )
        self.assertEqual(
            [row["price"] for row in rows],
            ["1.23", "1.24", "1.25", "1.26"],
        )
        self.assertEqual({row["venue"] for row in rows}, {"perp"})
        loaded = sim.load_tape_prices(self.output)
        self.assertEqual(
            [point.price for point in loaded],
            [
                Decimal("1.23"),
                Decimal("1.24"),
                Decimal("1.25"),
                Decimal("1.26"),
            ],
        )

    def test_absent_archive_omits_prices_and_consumer_marks_event_unknown(self):
        write_events(
            self.events,
            [event_row("missing", "MISSUSDT"), event_row("ready", "READYUSDT")],
        )
        write_archive(
            self.data,
            "READYUSDT",
            [(1, BASE_MS, "2"), (2, BASE_MS + 60_000, "2.1")],
        )

        summary = tape_prices.run_tape_prices(
            self.events, self.data, self.output
        )
        events = sim.load_events(self.events)
        prices = sim.load_tape_prices(self.output)
        policy = sim.Policy(
            entry_delay_min=0,
            stages=((Decimal("1"), 0),),
            max_alloc_usdt=Decimal("1"),
            stop_pct=Decimal("50"),
            partials=(),
            final_exit=1,
            spread_cost=Decimal("0"),
            slippage_conservative=Decimal("0"),
        )
        trades = sim.simulate_policy(events, prices, policy)

        self.assertEqual(summary, {"events": 2, "absent": 1, "rows": 2})
        self.assertEqual(trades[0]["status"], "UNKNOWN")
        self.assertEqual(trades[0]["unknown_reason"], "no_matching_tape_prices")
        self.assertEqual(trades[1]["status"], "CLOSED")

    def test_overlapping_unordered_events_emit_each_trade_once_in_order(self):
        write_events(
            self.events,
            [
                event_row("later", "ORDERUSDT", BASE_MS + 30 * 60_000),
                event_row("earlier", "ORDERUSDT", BASE_MS),
            ],
        )
        trade_rows = [
            (1, BASE_MS - 30_000, "3"),
            (2, BASE_MS, "3.1"),
            (3, BASE_MS + 30 * 60_000, "3.2"),
        ]
        write_archive(self.data, "ORDERUSDT", trade_rows)

        tape_prices.run_tape_prices(self.events, self.data, self.output)

        self.assertEqual(
            [row["timestamp_ms"] for row in self.rows()],
            [item[1] for item in trade_rows],
        )

    def test_limit_selects_only_the_first_labelled_event(self):
        write_events(
            self.events,
            [event_row("first", "FIRSTUSDT"), event_row("second", "SECONDUSDT")],
        )
        write_archive(self.data, "FIRSTUSDT", [(1, BASE_MS, "4")])

        summary = tape_prices.run_tape_prices(
            self.events, self.data, self.output, limit=1
        )

        self.assertEqual(summary, {"events": 1, "absent": 0, "rows": 1})
        self.assertEqual(self.rows()[0]["symbol"], "FIRSTUSDT")


if __name__ == "__main__":
    unittest.main()
