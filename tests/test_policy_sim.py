#!/usr/bin/env python3
"""Exact offline tests for the Volume Dataset v2 policy simulator."""

import json
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock


from study import policy_sim as sim
from study import validate


BASE_MS = 1_767_225_600_000
SYMBOL = "RUNUSDT"
FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "stage_a_line197191_event.json"
)


def event(*, symbol=SYMBOL, venue_class="spot_only", venue=None):
    return sim.LabelledEvent(
        event_id="runner-1",
        symbol=symbol,
        label="GenuineRunner",
        venue_class=venue_class,
        event_time_ms=BASE_MS,
        venue=venue,
        runner_score=Decimal("0.75"),
    )


def price(minute, value, *, symbol=SYMBOL, venue="spot"):
    return sim.TapePrice(
        symbol=symbol,
        venue=venue,
        timestamp_ms=BASE_MS + minute * sim.MINUTE_MS,
        price=Decimal(value),
    )


def policy(**overrides):
    values = {
        "entry_delay_min": 0,
        "stages": ((Decimal("1"), 0),),
        "max_alloc_usdt": Decimal("1000"),
        "stop_pct": Decimal("5"),
        "partials": (),
        "final_exit": 5,
        "spread_cost": Decimal("0"),
        "slippage_conservative": Decimal("0"),
        "spot_taker_fee": Decimal("0"),
        "perp_taker_fee": Decimal("0"),
    }
    values.update(overrides)
    return sim.Policy(**values)


def assert_no_float(test, value):
    test.assertNotIsInstance(value, float)
    if isinstance(value, dict):
        for nested in value.values():
            assert_no_float(test, nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            assert_no_float(test, nested)


def cli_fixture(root, event_count):
    events_path = root / "events.jsonl"
    tape_path = root / "tape.jsonl"
    output_path = root / "trades.jsonl"
    event_rows = [
        {
            "event_id": f"runner-{index}",
            "symbol": SYMBOL,
            "label": "GenuineRunner",
            "venue_class": "spot_only",
            "event_time_ms": BASE_MS,
        }
        for index in range(1, event_count + 1)
    ]
    tape_rows = [
        {
            "symbol": SYMBOL,
            "venue": "spot",
            "timestamp_ms": BASE_MS,
            "price": "100",
        },
        {
            "symbol": SYMBOL,
            "venue": "spot",
            "timestamp_ms": BASE_MS + 5 * sim.MINUTE_MS,
            "price": "100",
        },
    ]
    events_path.write_text(
        "".join(json.dumps(row) + "\n" for row in event_rows),
        encoding="utf-8",
    )
    tape_path.write_text(
        "".join(json.dumps(row) + "\n" for row in tape_rows),
        encoding="utf-8",
    )
    return events_path, tape_path, output_path


def cli_arguments(events_path, tape_path, output_path):
    return [
        "--events",
        str(events_path),
        "--tape",
        str(tape_path),
        "--output",
        str(output_path),
        "--entry-delay-min",
        "0",
        "--stage",
        "1:0",
        "--max-alloc-usdt",
        "1000",
        "--stop-pct",
        "5",
        "--final-exit",
        "5",
        "--spot-taker-fee",
        "0",
        "--perp-taker-fee",
        "0",
        "--spread-cost",
        "0",
        "--slippage-conservative",
        "0",
    ]


class PolicySymbolValidationTests(unittest.TestCase):
    def test_fixture_and_plain_ascii_symbols_pass_value_validation(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        fixture_symbol = fixture["symbol"]

        self.assertEqual(event(symbol=fixture_symbol).symbol, fixture_symbol)
        self.assertEqual(price(0, "1", symbol=fixture_symbol).symbol, fixture_symbol)
        self.assertEqual(event().symbol, SYMBOL)
        self.assertEqual(price(0, "1").symbol, SYMBOL)

    def test_traversal_symbol_keeps_policy_error_types_and_messages(self):
        with self.assertRaisesRegex(
            sim.PolicySimError, "^event symbol is invalid$"
        ):
            event(symbol="../RUNUSDT")
        with self.assertRaisesRegex(
            sim.PolicySimError, "^tape symbol is invalid$"
        ):
            price(0, "1", symbol="../RUNUSDT")

    def test_fixture_symbol_loads_from_synthetic_data_files(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        symbol = fixture["symbol"]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        events_path = root / f"{symbol}-events.jsonl"
        tape_path = root / f"{symbol}-tape.jsonl"
        events_path.write_text(json.dumps(fixture) + "\n", encoding="utf-8")
        tape_path.write_text(
            json.dumps(
                {
                    "symbol": symbol,
                    "venue": "spot",
                    "timestamp_ms": fixture["open_ms"],
                    "price": "1",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        loaded_event = sim.load_events(events_path)[0]
        loaded_price = sim.load_tape_prices(tape_path)[0]

        self.assertEqual(loaded_event.symbol, symbol)
        self.assertEqual(loaded_price.symbol, symbol)


class PolicySimulationTests(unittest.TestCase):
    def test_reconciling_costs_keep_the_exact_result(self):
        exact_costs = policy(
            spread_cost=Decimal("0.125"),
            slippage_conservative=Decimal("0.125"),
            spot_taker_fee=Decimal("0.01"),
            perp_taker_fee=Decimal("0.01"),
            stop_pct=Decimal("50"),
        )

        row = sim.simulate_event(
            event(),
            (price(0, "100"), price(5, "125")),
            exact_costs,
        )

        self.assertEqual(row["status"], "CLOSED")
        self.assertEqual(row["gross_pnl_usdt"], "200")
        self.assertEqual(row["total_cost_usdt"], "467.5")
        self.assertEqual(row["net_pnl_usdt"], "-267.5")

    def test_scripted_runner_matches_hand_computed_net_after_every_cost(self):
        fixed = policy(
            entry_delay_min=1,
            stages=((Decimal("0.5"), 0), (Decimal("0.5"), 1)),
            max_alloc_usdt=Decimal("1010"),
            stop_pct=Decimal("20"),
            partials=((Decimal("0.5"), Decimal("10")),),
            spread_cost=Decimal("0.002"),
            slippage_conservative=Decimal("0.008"),
            spot_taker_fee=Decimal("0.001"),
            perp_taker_fee=Decimal("0.0005"),
        )
        tape = (
            price(0, "99"),
            price(1, "100"),
            price(2, "100"),
            price(3, "109"),
            price(4, "110"),
            price(5, "130"),
        )

        row = sim.simulate_event(event(), tape, fixed)

        # Hand calculation: gross PnL 200; fees 2.198; spread 4.4;
        # conservative slippage 17.6; net = 200 - 24.198 = 175.802.
        self.assertEqual(row["status"], "CLOSED")
        self.assertEqual(row["exit_reason"], "final_exit")
        self.assertEqual(row["entry_latency_min"], "1")
        self.assertEqual(row["allocation_cap_usdt"], "1010")
        self.assertEqual(row["allocation_used_usdt"], "1010")
        self.assertTrue(row["allocation_cap_respected"])
        self.assertEqual(row["gross_pnl_usdt"], "200")
        self.assertEqual(row["runner_score"], "0.75")
        self.assertEqual(row["mfe_pct"], "30")
        self.assertEqual(row["mae_pct"], "0")
        self.assertEqual(row["time_to_peak_min"], "4")
        self.assertEqual(row["entry_fee_usdt"], "1.01")
        self.assertEqual(row["exit_fee_usdt"], "1.188")
        self.assertEqual(row["fee_cost_usdt"], "2.198")
        self.assertEqual(row["spread_cost_usdt"], "4.4")
        self.assertEqual(row["slippage_cost_usdt"], "17.6")
        self.assertEqual(row["total_cost_usdt"], "24.198")
        self.assertEqual(row["net_pnl_usdt"], "175.802")
        self.assertEqual(
            Decimal(row["gross_return_pct"])
            - Decimal(row["all_costs_pct"]),
            Decimal(row["net_return_pct"]),
        )
        self.assertEqual(row["taker_fee_rate_per_side"], "0.001")
        self.assertEqual(
            [fill["timestamp_ms"] for fill in row["entry_fills"]],
            [BASE_MS + sim.MINUTE_MS, BASE_MS + 2 * sim.MINUTE_MS],
        )
        self.assertEqual(
            [fill["reason"] for fill in row["exit_fills"]],
            ["partial", "final_exit"],
        )
        for field in (
            "entry_fee_usdt",
            "exit_fee_usdt",
            "fee_cost_usdt",
            "entry_spread_cost_usdt",
            "exit_spread_cost_usdt",
            "spread_cost_usdt",
            "entry_slippage_cost_usdt",
            "exit_slippage_cost_usdt",
            "slippage_cost_usdt",
            "total_cost_usdt",
        ):
            self.assertIsNotNone(row[field])
        assert_no_float(self, row)

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "trades.jsonl"
        sim.write_trades(output, (row,))
        disk = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertEqual(disk, [row])
        validated = validate.load_trades(output)
        self.assertEqual(len(validated), 1)
        self.assertEqual(validated[0].net_return_pct, Decimal(row["net_return_pct"]))

    def test_stop_triggers_at_the_exact_threshold(self):
        stopped = policy(
            stages=((Decimal("0.5"), 0), (Decimal("0.5"), 2)),
            partials=((Decimal("1"), Decimal("10")),),
        )
        tape = (
            price(0, "100"),
            price(1, "96"),
            price(2, "95"),
            price(5, "200"),
        )

        row = sim.simulate_event(event(), tape, stopped)

        self.assertEqual(row["status"], "CLOSED")
        self.assertEqual(row["exit_reason"], "stop")
        self.assertTrue(row["stop_triggered"])
        self.assertEqual(len(row["entry_fills"]), 1)
        self.assertEqual(len(row["exit_fills"]), 1)
        stop_fill = row["exit_fills"][0]
        self.assertEqual(stop_fill["reason"], "stop")
        self.assertEqual(stop_fill["timestamp_ms"], BASE_MS + 2 * sim.MINUTE_MS)
        self.assertEqual(stop_fill["tape_price"], "95")
        self.assertEqual(stop_fill["trigger_price"], "95")

    def test_each_partial_triggers_once_at_its_exact_price(self):
        partials = policy(
            stop_pct=Decimal("50"),
            partials=(
                (Decimal("0.25"), Decimal("10")),
                (Decimal("0.5"), Decimal("20")),
            ),
        )
        tape = (
            price(0, "100"),
            price(1, "109"),
            price(2, "110"),
            price(3, "110"),
            price(4, "120"),
            price(5, "115"),
        )

        row = sim.simulate_event(event(), tape, partials)

        self.assertEqual(row["exit_reason"], "final_exit")
        self.assertFalse(row["stop_triggered"])
        self.assertEqual(
            [fill["reason"] for fill in row["exit_fills"]],
            ["partial", "partial", "final_exit"],
        )
        self.assertEqual(
            [fill["timestamp_ms"] for fill in row["exit_fills"][:2]],
            [BASE_MS + 2 * sim.MINUTE_MS, BASE_MS + 4 * sim.MINUTE_MS],
        )
        self.assertEqual(
            [fill["quantity"] for fill in row["exit_fills"]],
            ["2.5", "5", "2.5"],
        )
        self.assertEqual(
            [fill["trigger_price"] for fill in row["exit_fills"][:2]],
            ["110", "120"],
        )

    def test_perp_event_selects_the_lower_default_fee(self):
        futures_policy = policy(
            spot_taker_fee=Decimal("0.001"),
            perp_taker_fee=Decimal("0.0005"),
        )
        futures_event = event(venue_class="futures_only")
        futures_tape = (
            price(0, "100", venue="perp"),
            price(5, "100", venue="perp"),
        )

        row = sim.simulate_event(futures_event, futures_tape, futures_policy)

        self.assertEqual(row["venue"], "perp")
        self.assertEqual(row["taker_fee_rate_per_side"], "0.0005")
        self.assertEqual(row["fee_cost_usdt"], "1")
        self.assertEqual(row["net_pnl_usdt"], "-1")


class PolicyDiagnosticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_cost_refusal_names_every_compared_number(self):
        events_path, tape_path, output_path = cli_fixture(self.root, 1)
        original_exit_fill = sim._exit_fill

        def adjusted_exit_fill(*args, **kwargs):
            fill, values = original_exit_fill(*args, **kwargs)
            adjusted = dict(values)
            adjusted["executed"] -= Decimal("1")
            return fill, adjusted

        stderr = io.StringIO()
        with mock.patch.object(sim, "_exit_fill", adjusted_exit_fill):
            with redirect_stderr(stderr):
                result = sim.main(
                    cli_arguments(events_path, tape_path, output_path)
                )

        expected = (
            "POLICY-SIM | REFUSED | cost decomposition does not reconcile | "
            "event_id=runner-1 | stage_index=0 | fee_in=0 | fee_out=0 | "
            "fee=0 | spread_in=0 | spread_out=0 | spread=0 | "
            "slippage_in=0 | slippage_out=0 | slippage=0 | total_cost=0 | "
            "gross_in=1000 | gross_out=1000 | gross=0 | executed_in=1000 | "
            "executed_out=999 | net=0 | cash_net=-1 | "
            "compared_total_costs=0 | compared_total_cash=-1 | difference=-1"
        )
        self.assertEqual(result, 2)
        self.assertEqual(stderr.getvalue().strip(), expected)
        self.assertFalse(output_path.exists())

    def test_limit_two_processes_exactly_the_first_two_events(self):
        events_path, tape_path, output_path = cli_fixture(self.root, 3)
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = sim.main(
                cli_arguments(events_path, tape_path, output_path)
                + ["--limit", "2"]
            )

        rows = [
            json.loads(line)
            for line in output_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(result, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual([row["event_id"] for row in rows], ["runner-1", "runner-2"])
        self.assertIn("POLICY-SIM | events=2 | closed=2 | unknown=0", stdout.getvalue())
        self.assertIn("diagnostic_only=true", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
