#!/usr/bin/env python3
"""Offline synthetic-stream tests for the dynamic PAPER momentum lifecycle."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import momentum_lifecycle as ml


FIXED_NOW = "2026-08-13T12:00:00.000Z"


def fixed_now():
    return FIXED_NOW


def stream_payload(
    minute,
    price,
    surge,
    volume,
    oi,
    *,
    spread_pct="0.006",
    oi_change_pct="1",
    freshness_ms="100",
    basis_gap_pct="0.1",
    venues="both",
):
    """One complete frozen-schema record; every number is a Decimal string."""
    price = Decimal(str(price))
    spread = Decimal(str(spread_pct))
    half = spread / Decimal("2")
    bid = price * (Decimal("1") - half)
    ask = price * (Decimal("1") + half)
    perp_mid = price * Decimal("1.001")
    perp_bid = perp_mid * (Decimal("1") - half)
    perp_ask = perp_mid * (Decimal("1") + half)

    def text(value):
        return None if value is None else format(Decimal(str(value)), "f")

    return {
        "utc": f"2026-08-13T10:{minute:02d}:00.000Z",
        "venues": venues,
        "spot_bid": text(bid) if venues in {"spot", "both"} else None,
        "spot_ask": text(ask) if venues in {"spot", "both"} else None,
        "perp_bid": text(perp_bid) if venues in {"perp", "both"} else None,
        "perp_ask": text(perp_ask) if venues in {"perp", "both"} else None,
        "mark": text(perp_mid) if venues in {"perp", "both"} else text(price),
        "vol_1m_quote": text(volume),
        "vol_med_1m": "100",
        "surge_ratio": text(surge),
        "oi": text(oi),
        "oi_change_pct": text(oi_change_pct),
        "funding_rate": "0.0001" if venues in {"perp", "both"} else None,
        "next_funding_ms": "1786636800000" if venues in {"perp", "both"} else None,
        "basis_gap_pct": text(basis_gap_pct),
        "freshness_ms": text(freshness_ms),
    }


def record(symbol, *args, **kwargs):
    return ml.StreamRecord.from_payload(symbol, stream_payload(*args, **kwargs))


def test_config(**overrides):
    values = dict(
        ignition_surge_ratio=Decimal("10"),
        persistence_surge_ratio=Decimal("8"),
        persistence_passes=2,
        initial_notional=Decimal("10"),
        add_notional=Decimal("5"),
        max_total_notional=Decimal("30"),
        max_adds=4,
        min_volume_growth_pct=Decimal("0.05"),
        min_surge_growth_pct=Decimal("0.02"),
        min_price_growth_pct=Decimal("0.01"),
        max_spread_pct=Decimal("0.01"),
        min_depth_improvement_pct=Decimal("0.05"),
        min_oi_growth_pct=Decimal("0.01"),
        min_oi_change_pct=Decimal("0"),
        trail_pct=Decimal("0.20"),
        soft_price_fade_pct=Decimal("0.10"),
        volume_fade_pct=Decimal("0.25"),
        oi_fade_pct=Decimal("0.05"),
        oi_change_fade_pct=Decimal("-5"),
        depth_worsening_pct=Decimal("0.30"),
        reduce_fraction=Decimal("0.25"),
        ignition_invalidation_surge_ratio=Decimal("1"),
        basis_material_pct=Decimal("0.50"),
    )
    values.update(overrides)
    return ml.LifecycleConfig(**values)


class MomentumLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state_path = self.root / "momentum_state.json"
        self.receipts = self.root / "paper_receipts.jsonl"
        self.state = ml.LifecycleState()
        self.config = test_config()

    def apply(self, symbol, payload):
        observed = ml.StreamRecord.from_payload(symbol, payload)
        return ml.process_record(
            self.state, observed, self.config, self.receipts, fixed_now
        )

    def receipt_rows(self):
        if not self.receipts.exists():
            return []
        return [
            json.loads(line)
            for line in self.receipts.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("{")
        ]

    def enter_fixture(self, symbol="AAAUSDT"):
        decisions = [
            self.apply(symbol, stream_payload(0, "100", "2", "100", "1000", spread_pct="0.009")),
            self.apply(symbol, stream_payload(1, "105", "70", "7000", "1050", spread_pct="0.008")),
            self.apply(symbol, stream_payload(2, "108", "75", "7600", "1070", spread_pct="0.007")),
        ]
        self.assertEqual([row["action"] for row in decisions], ["WATCH", "WATCH", "ENTER_SMALL"])
        return decisions

    def test_full_transition_ladder(self):
        decisions = self.enter_fixture()
        decisions.append(
            self.apply(
                "AAAUSDT",
                stream_payload(3, "120", "90", "9000", "1100", spread_pct="0.006", oi_change_pct="3"),
            )
        )
        # Flat acceleration closes the add window but does not manufacture a
        # reduction; the configured trail remains live.
        decisions.append(
            self.apply(
                "AAAUSDT",
                stream_payload(4, "121", "90", "9000", "1100", spread_pct="0.006", oi_change_pct="1"),
            )
        )
        decisions.append(
            self.apply(
                "AAAUSDT",
                stream_payload(5, "116", "50", "5000", "1000", spread_pct="0.009", oi_change_pct="-6"),
            )
        )
        decisions.append(
            self.apply(
                "AAAUSDT",
                stream_payload(6, "90", "30", "3000", "900", spread_pct="0.009", oi_change_pct="-8"),
            )
        )
        self.assertEqual(
            [row["action"] for row in decisions],
            ["WATCH", "WATCH", "ENTER_SMALL", "ADD", "TRAIL", "REDUCE_TRAIL", "EXIT"],
        )
        self.assertEqual(decisions[3]["add_checks"], {
            "volume": True,
            "price_structure": True,
            "depth": True,
            "participation": True,
            "within_ladder": True,
        })
        self.assertEqual(decisions[-1]["reason"], "price reached trailing stop")
        self.assertEqual(self.state.tokens["AAAUSDT"]["phase"], "WATCH")
        self.assertEqual(self.state.tokens["AAAUSDT"]["qty"], "0")
        rows = self.receipt_rows()
        self.assertEqual(
            [row["kind"] for row in rows],
            ["paper_enter_small", "paper_add", "paper_reduce", "paper_exit"],
        )
        self.assertTrue(all(row["paper"] is True for row in rows))
        self.assertEqual(set(rows[0]["entry_structure"]), {
            "utc", "venue", "price", "structure_price", "surge_ratio", "oi", "vol_1m_quote", "spread_pct"
        })

    def test_first_high_read_is_not_invented_as_a_threshold_cross(self):
        first = self.apply(
            "AAAUSDT",
            stream_payload(0, "105", "70", "7000", "1050", spread_pct="0.008"),
        )
        second = self.apply(
            "AAAUSDT",
            stream_payload(1, "108", "75", "7600", "1070", spread_pct="0.007"),
        )
        self.assertEqual([first["action"], second["action"]], ["WATCH", "WATCH"])
        self.assertFalse(self.state.tokens["AAAUSDT"]["candidate"])
        self.assertEqual(self.receipt_rows(), [])

    def test_add_gating_is_four_way_explicit_and_configurable(self):
        self.enter_fixture()
        token = self.state.tokens["AAAUSDT"]
        strong = record(
            "AAAUSDT", 3, "120", "90", "9000", "1100", spread_pct="0.006", oi_change_pct="3"
        )
        self.assertTrue(all(ml.add_strength_checks(token, strong, self.config).values()))

        cases = {
            "volume": record("AAAUSDT", 3, "120", "75", "7600", "1100", spread_pct="0.006", oi_change_pct="3"),
            "price_structure": record("AAAUSDT", 3, "108", "90", "9000", "1100", spread_pct="0.006", oi_change_pct="3"),
            "depth": record("AAAUSDT", 3, "120", "90", "9000", "1100", spread_pct="0.009", oi_change_pct="3"),
            "participation": record("AAAUSDT", 3, "120", "90", "9000", "1070", spread_pct="0.006", oi_change_pct="0"),
        }
        for gate, observed in cases.items():
            with self.subTest(gate=gate):
                checks = ml.add_strength_checks(token, observed, self.config)
                self.assertFalse(checks[gate])

        stricter = test_config(min_price_growth_pct=Decimal("0.20"))
        self.assertFalse(ml.add_strength_checks(token, strong, stricter)["price_structure"])

    def test_trail_math_tracks_peak_not_entry(self):
        self.enter_fixture()
        add = self.apply(
            "AAAUSDT",
            stream_payload(3, "150", "90", "9000", "1100", spread_pct="0.006", oi_change_pct="3"),
        )
        self.assertEqual(add["action"], "ADD")
        self.assertEqual(ml.trail_stop_price(Decimal("150"), Decimal("0.20")), Decimal("120.00"))
        self.assertEqual(
            Decimal(self.state.tokens["AAAUSDT"]["trail_stop_price"]),
            Decimal("120.00"),
        )
        keep = self.apply(
            "AAAUSDT",
            stream_payload(4, "121", "80", "8000", "1080", spread_pct="0.006", oi_change_pct="0"),
        )
        self.assertNotEqual(keep["action"], "EXIT")
        hit = self.apply(
            "AAAUSDT",
            stream_payload(5, "120", "70", "7000", "1050", spread_pct="0.006", oi_change_pct="0"),
        )
        self.assertEqual(hit["action"], "EXIT")
        self.assertEqual(
            Decimal(self.receipt_rows()[-1]["trail_stop_price"]),
            Decimal("120.00"),
        )

    def test_move_break_exits_on_ignition_invalidation(self):
        self.config = test_config(trail_pct=Decimal("0.50"))
        self.enter_fixture()
        broken = self.apply(
            "AAAUSDT",
            stream_payload(3, "99", "0.5", "50", "900", spread_pct="0.007", oi_change_pct="-4"),
        )
        self.assertEqual(broken["action"], "EXIT")
        self.assertEqual(self.receipt_rows()[-1]["exit_reason"], "ignition_invalidated")

    def test_freshness_loss_fail_closed(self):
        self.enter_fixture()
        stale = self.apply(
            "AAAUSDT",
            stream_payload(3, "110", "80", "8000", "1080", freshness_ms="5001"),
        )
        self.assertEqual(stale["action"], "EXIT")
        exit_receipt = self.receipt_rows()[-1]
        self.assertEqual(exit_receipt["exit_reason"], "freshness_lost")
        self.assertTrue(exit_receipt["paper"])
        self.assertEqual(self.state.tokens["AAAUSDT"]["qty"], "0")

    def test_restart_continuity_and_receipt_crash_reconciliation(self):
        # Persistence survives a normal restart.
        self.apply("AAAUSDT", stream_payload(0, "100", "2", "100", "1000", spread_pct="0.009"))
        self.apply("AAAUSDT", stream_payload(1, "105", "70", "7000", "1050", spread_pct="0.008"))
        ml.save_state(self.state_path, self.state, fixed_now)
        self.state = ml.load_state(self.state_path)
        entered = self.apply("AAAUSDT", stream_payload(2, "108", "75", "7600", "1070", spread_pct="0.007"))
        self.assertEqual(entered["action"], "ENTER_SMALL")
        ml.save_state(self.state_path, self.state, fixed_now)

        # Simulate a kill after durable ADD receipt append but before state save.
        prior_disk = self.state_path.read_bytes()
        added = self.apply(
            "AAAUSDT",
            stream_payload(3, "120", "90", "9000", "1100", spread_pct="0.006", oi_change_pct="3"),
        )
        self.assertEqual(added["action"], "ADD")
        crashed_memory = json.loads(json.dumps(self.state.tokens["AAAUSDT"]))
        self.assertEqual(self.state_path.read_bytes(), prior_disk)
        reloaded = ml.load_state(self.state_path)
        self.assertEqual(reloaded.tokens["AAAUSDT"]["adds"], 0)
        replayed = ml.reconcile_receipts(reloaded, self.receipts)
        self.assertEqual(replayed, 1)
        self.assertEqual(reloaded.tokens["AAAUSDT"], crashed_memory)
        duplicate = ml.process_record(
            reloaded,
            record("AAAUSDT", 3, "120", "90", "9000", "1100", spread_pct="0.006", oi_change_pct="3"),
            self.config,
            self.receipts,
            fixed_now,
        )
        self.assertEqual(duplicate["action"], "DUPLICATE")
        self.assertEqual(len(self.receipt_rows()), 2)

    def test_no_negative_or_zero_phantom_sizing(self):
        self.assertEqual(
            ml.paper_quantity(Decimal("0.000001"), Decimal("1000000")),
            Decimal("0"),
        )
        with self.assertRaises(ml.ConfigurationError):
            test_config(initial_notional=Decimal("-1"))

        self.config = test_config(initial_notional=Decimal("0.000001"), max_total_notional=Decimal("1"))
        self.apply("BIGUSDT", stream_payload(0, "1000000", "2", "100", "1000"))
        self.apply("BIGUSDT", stream_payload(1, "1050000", "70", "7000", "1050"))
        blocked = self.apply("BIGUSDT", stream_payload(2, "1080000", "75", "7600", "1070"))
        self.assertEqual(blocked["action"], "WATCH")
        self.assertEqual(self.state.tokens["BIGUSDT"]["qty"], "0")
        self.assertEqual(self.receipt_rows(), [])

    def test_basis_available_is_per_pass_fact_not_auto_switch(self):
        available = self.apply(
            "AAAUSDT",
            stream_payload(0, "100", "2", "100", "1000", basis_gap_pct="0.75"),
        )
        self.assertTrue(available["basis_available"])
        self.assertIn("meets material_pct=0.50", available["basis_note"])
        self.assertEqual(available["action"], "WATCH")
        self.assertEqual(available["trade_router_note"], "factual alternative only; no automatic switch")

        unavailable = self.apply(
            "AAAUSDT",
            stream_payload(1, "100", "2", "100", "1000", basis_gap_pct="0.49"),
        )
        self.assertFalse(unavailable["basis_available"])
        spot_only = self.apply(
            "SPOTUSDT",
            stream_payload(2, "10", "2", "100", None, venues="spot", basis_gap_pct="3"),
        )
        self.assertFalse(spot_only["basis_available"])
        self.assertIn("not both", spot_only["basis_note"])

    def test_missing_token_in_next_stream_exits_fail_closed(self):
        self.enter_fixture()
        decisions = ml.process_stream(self.state, {}, self.config, self.receipts, fixed_now)
        self.assertEqual(decisions[0]["action"], "EXIT")
        self.assertEqual(self.receipt_rows()[-1]["exit_reason"], "freshness_lost_missing_token")


def aprusdt_fixture():
    """APR class: 70x ignition, early +5%, 3x run, then -36.5% from peak."""
    return [
        stream_payload(0, "100", "2", "100", "1000", spread_pct="0.010"),
        stream_payload(1, "105", "70", "7000", "1070", spread_pct="0.009"),
        stream_payload(2, "108", "75", "7600", "1090", spread_pct="0.008"),
        stream_payload(3, "135", "90", "9000", "1120", spread_pct="0.007"),
        stream_payload(4, "210", "115", "12000", "1170", spread_pct="0.006"),
        stream_payload(5, "300", "150", "16000", "1240", spread_pct="0.005"),
        stream_payload(6, "315", "180", "20000", "1320", spread_pct="0.004"),
        stream_payload(7, "200.025", "100", "10000", "1200", spread_pct="0.006", oi_change_pct="-8"),
        stream_payload(8, "180", "80", "8000", "1100", spread_pct="0.007", oi_change_pct="-9"),
    ]


class AprusdtRegressionTest(unittest.TestCase):
    def test_aprusdt_enters_early_adds_strength_and_never_chases_break(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        receipts = Path(temporary.name) / "apr_receipts.jsonl"
        state = ml.LifecycleState()
        config = test_config(max_total_notional=Decimal("30"), max_adds=4)
        decisions = []
        for payload in aprusdt_fixture():
            decisions.append(
                ml.process_record(
                    state,
                    ml.StreamRecord.from_payload("APRUSDT", payload),
                    config,
                    receipts,
                    fixed_now,
                )
            )

        actions = [row["action"] for row in decisions]
        self.assertEqual(actions[:3], ["WATCH", "WATCH", "ENTER_SMALL"])
        self.assertEqual(actions[3:7], ["ADD", "ADD", "ADD", "ADD"])
        self.assertEqual(actions[7], "EXIT")
        self.assertEqual(actions[8], "WATCH")
        rows = [json.loads(line) for line in receipts.read_text(encoding="utf-8").splitlines()]
        entry = rows[0]
        self.assertLessEqual(Decimal(entry["price"]), Decimal("105") * Decimal("1.04"))
        self.assertEqual(entry["entry_structure"]["surge_ratio"], "75")
        self.assertEqual(len([row for row in rows if row["kind"] == "paper_add"]), 4)
        exit_row = rows[-1]
        self.assertEqual(exit_row["kind"], "paper_exit")
        self.assertEqual(exit_row["exit_reason"], "trail_hit")
        self.assertEqual(Decimal(exit_row["peak_price"]), Decimal("315"))
        self.assertEqual(Decimal(exit_row["trail_stop_price"]), Decimal("252.00"))
        collapse = (Decimal("315") - Decimal("200.025")) / Decimal("315")
        self.assertEqual(collapse, Decimal("0.365"))
        self.assertEqual(len(rows), 6)  # enter + four adds + one exit; no chase
        self.assertTrue(state.tokens["APRUSDT"]["rearm_required"])


class CliAndSchemaTests(unittest.TestCase):
    def test_tick_cli_reads_frozen_stream_and_persists_atomic_state(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        stream_path = root / "stream_state.json"
        state_path = root / "state.json"
        receipts = root / "receipts.jsonl"
        stream_path.write_text(
            json.dumps({"AAAUSDT": stream_payload(0, "100", "2", "100", "1000")}),
            encoding="utf-8",
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = ml.main(
                [
                    "--tick",
                    str(stream_path),
                    "--state",
                    str(state_path),
                    "--receipts",
                    str(receipts),
                ]
            )
        self.assertEqual(code, 0, stderr.getvalue())
        printed = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(printed[0], {"paper": True, "replayed_receipts": 0})
        self.assertEqual(printed[1]["action"], "WATCH")
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertTrue(persisted["paper"])
        self.assertTrue(persisted["tokens"]["AAAUSDT"]["paper"])
        self.assertFalse(state_path.with_name("state.json.tmp").exists())

    def test_schema_rejects_non_string_decimal(self):
        payload = stream_payload(0, "100", "2", "100", "1000")
        payload["surge_ratio"] = 2
        with self.assertRaises(ml.StreamDataError):
            ml.StreamRecord.from_payload("AAAUSDT", payload)


if __name__ == "__main__":
    unittest.main()
