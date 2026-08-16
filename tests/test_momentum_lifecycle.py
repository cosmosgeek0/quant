#!/usr/bin/env python3
"""Offline tests for runner-backed momentum signal and HOLD lifecycle."""

import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr
from decimal import Decimal
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import limits
import momentum_lifecycle as ml
import momentum_live as live


FIXED_NOW = "2026-08-14T12:00:00.000Z"


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
        "utc": f"2026-08-14T10:{minute:02d}:00.000Z",
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
        ignition_surge_ratio=ml.MOMENTUM_IGNITION_SURGE_RATIO,
        persistence_surge_ratio=Decimal("8"),
        persistence_passes=2,
        initial_notional=Decimal("8"),
        add_notional=Decimal("2"),
        max_total_notional=Decimal("16"),
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
        self.receipts = self.root / "momentum-decisions.jsonl"
        self.state_path = self.root / "momentum-state.json"
        self.state = ml.LifecycleState()
        self.config = test_config()

    def apply(self, payload):
        return ml.process_record(
            self.state,
            ml.StreamRecord.from_payload("AAAUSDT", payload),
            self.config,
            self.receipts,
            fixed_now,
        )

    def rows(self):
        if not self.receipts.exists():
            return []
        return [
            json.loads(line)
            for line in self.receipts.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def signal(self):
        decisions = [
            self.apply(stream_payload(0, "100", "2", "100", "1000")),
            self.apply(stream_payload(1, "105", "70", "7000", "1050", spread_pct="0.008")),
            self.apply(stream_payload(2, "108", "75", "7600", "1070", spread_pct="0.007")),
        ]
        self.assertEqual(
            [row["action"] for row in decisions],
            [ml.ACTION_WATCH, ml.ACTION_WATCH, ml.ACTION_ENTER_SMALL],
        )
        return self.state.tokens["AAAUSDT"]

    def fill(self, price=Decimal("111"), qty=Decimal("0.07")):
        token = self.signal()
        ml.bind_job_id(self.state, "AAAUSDT", ml.ACTION_ENTER_SMALL, "open-job")
        changed = ml.apply_open_fill(
            self.state, "AAAUSDT", "open-job", "open-result", qty, price,
            self.config,
        )
        self.assertTrue(changed)
        return token

    def test_signal_is_not_a_fill_and_actual_fill_becomes_entry(self):
        token = self.signal()
        self.assertEqual(token["phase"], ml.PHASE_ENTER_SMALL)
        self.assertEqual(token["qty"], "0")
        self.assertIsNone(token["avg_entry_price"])
        self.assertEqual(Decimal(token["entry_structure"]["signal_price"]), Decimal("108.3780"))

        ml.bind_job_id(self.state, "AAAUSDT", ml.ACTION_ENTER_SMALL, "open-job")
        self.assertTrue(ml.apply_open_fill(
            self.state, "AAAUSDT", "open-job", "open-result",
            Decimal("0.071"), Decimal("112.5"), self.config,
        ))
        self.assertEqual(token["phase"], ml.PHASE_HOLD)
        self.assertEqual(token["qty"], "0.071")
        self.assertEqual(token["avg_entry_price"], "112.5")
        self.assertEqual(token["entry_structure"]["price"], "112.5")
        self.assertEqual(Decimal(token["entry_structure"]["signal_price"]), Decimal("108.3780"))

    def test_hold_tracks_peak_and_add_reduce_are_decision_only(self):
        token = self.fill()
        original_qty = token["qty"]
        original_cost = token["paper_cost"]
        added = self.apply(
            stream_payload(3, "120", "90", "9000", "1100", spread_pct="0.006", oi_change_pct="3")
        )
        self.assertEqual(added["action"], ml.ACTION_ADD)
        self.assertEqual(Decimal(token["peak_price"]), Decimal("120"))
        self.assertEqual((token["qty"], token["paper_cost"]), (original_qty, original_cost))

        reduced = self.apply(
            stream_payload(4, "116", "50", "5000", "1000", spread_pct="0.009", oi_change_pct="-6")
        )
        self.assertEqual(reduced["action"], ml.ACTION_REDUCE_TRAIL)
        self.assertEqual((token["qty"], token["paper_cost"]), (original_qty, original_cost))
        rows = self.rows()
        self.assertEqual(rows[-2]["execution"], "DECISION_ONLY")
        self.assertEqual(rows[-1]["execution"], "DECISION_ONLY")
        self.assertTrue(all(row["decision_only"] is True for row in rows))

    def test_trail_exit_retains_position_until_runner_confirms_flat(self):
        token = self.fill()
        self.apply(stream_payload(3, "150", "90", "9000", "1100", spread_pct="0.006", oi_change_pct="3"))
        exited = self.apply(stream_payload(4, "119", "30", "3000", "900", oi_change_pct="-8"))
        self.assertEqual(exited["action"], ml.ACTION_EXIT)
        self.assertEqual(exited["reason"], "price reached trailing stop")
        self.assertEqual(token["phase"], ml.PHASE_EXIT_PENDING)
        self.assertEqual(token["qty"], "0.07")

        ml.bind_job_id(self.state, "AAAUSDT", ml.ACTION_EXIT, "exit-job")
        self.assertTrue(ml.confirm_position_flat(
            self.state, "AAAUSDT", "exit-job", "exit-result"
        ))
        self.assertEqual(token["phase"], ml.PHASE_WATCH)
        self.assertEqual(token["qty"], "0")
        self.assertTrue(token["rearm_required"])

    def test_freshness_loss_is_fail_closed_exit(self):
        token = self.fill()
        stale = self.apply(
            stream_payload(3, "115", "80", "8000", "1080", freshness_ms="5001")
        )
        self.assertEqual(stale["action"], ml.ACTION_EXIT)
        self.assertEqual(token["phase"], ml.PHASE_EXIT_PENDING)
        self.assertEqual(token["qty"], "0.07")
        self.assertEqual(self.rows()[-1]["exit_reason"], "freshness_lost")

    def test_missing_token_is_fail_closed_exit(self):
        token = self.fill()
        decision = ml.process_missing_token(
            self.state, "AAAUSDT", self.config, self.receipts, fixed_now
        )
        self.assertEqual(decision["action"], ml.ACTION_EXIT)
        self.assertEqual(token["phase"], ml.PHASE_EXIT_PENDING)
        self.assertEqual(self.rows()[-1]["exit_reason"], "freshness_lost_missing_token")

    def test_receipt_reconciliation_preserves_real_position_and_decision_only_add(self):
        token = self.fill()
        ml.save_state(self.state_path, self.state, fixed_now)
        prior = self.state_path.read_bytes()
        added = self.apply(
            stream_payload(3, "120", "90", "9000", "1100", spread_pct="0.006", oi_change_pct="3")
        )
        self.assertEqual(added["action"], ml.ACTION_ADD)
        self.assertEqual(self.state_path.read_bytes(), prior)

        reloaded = ml.load_state(self.state_path)
        self.assertEqual(reloaded.tokens["AAAUSDT"]["adds"], 0)
        self.assertEqual(ml.reconcile_receipts(reloaded, self.receipts), 1)
        recovered = reloaded.tokens["AAAUSDT"]
        self.assertEqual(recovered["adds"], 1)
        self.assertEqual(recovered["qty"], token["qty"])
        self.assertEqual(recovered["avg_entry_price"], "111")

    def test_duplicate_fill_is_idempotent_and_conflict_fails_closed(self):
        self.fill()
        self.assertFalse(ml.apply_open_fill(
            self.state, "AAAUSDT", "open-job", "open-result",
            Decimal("0.07"), Decimal("111"), self.config,
        ))
        with self.assertRaises(ml.StreamDataError):
            ml.apply_open_fill(
                self.state, "AAAUSDT", "open-job", "other-result",
                Decimal("0.07"), Decimal("111"), self.config,
            )

    def test_stream_schema_rejects_numeric_money_value(self):
        payload = stream_payload(0, "100", "2", "100", "1000")
        payload["mark"] = 100
        with self.assertRaises(ml.StreamDataError):
            ml.StreamRecord.from_payload("AAAUSDT", payload)

    def test_additive_tail_keeps_observation_identity_and_decision(self):
        baseline = stream_payload(0, "100", "2", "100", "1000")
        ignition = stream_payload(1, "105", "70", "7000", "1050")
        ignition_with_tail = dict(ignition, book_age_ms="100")
        plain_record = ml.StreamRecord.from_payload("AAAUSDT", ignition)
        tail_record = ml.StreamRecord.from_payload("AAAUSDT", ignition_with_tail)

        self.assertEqual(
            ml.STATE_IDENTITY_FIELDS,
            ("utc", "venues") + ml.NUMERIC_FIELDS,
        )
        self.assertEqual(plain_record.observation_id, tail_record.observation_id)

        decisions = []
        for payload in (ignition, ignition_with_tail):
            state = ml.LifecycleState()
            ml.process_record(
                state,
                ml.StreamRecord.from_payload("AAAUSDT", baseline),
                self.config,
                self.receipts,
                fixed_now,
            )
            decisions.append(
                ml.process_record(
                    state,
                    ml.StreamRecord.from_payload("AAAUSDT", payload),
                    self.config,
                    self.receipts,
                    fixed_now,
                )
            )
        self.assertEqual(decisions[0], decisions[1])

        duplicate = ml.process_record(
            state,
            plain_record,
            self.config,
            self.receipts,
            fixed_now,
        )
        self.assertEqual(duplicate["action"], ml.ACTION_DUPLICATE)

    def test_ignition_default_flows_from_limits(self):
        expected = getattr(
            limits, "MOMENTUM_IGNITION_SURGE_RATIO", Decimal("3")
        )
        self.assertEqual(ml.LifecycleConfig().ignition_surge_ratio, expected)
        self.assertEqual(
            live.default_lifecycle_config().ignition_surge_ratio, expected
        )

    def test_live_cli_ignition_surge_override_wins(self):
        parser = live.build_parser()
        default_args = parser.parse_args(["--tick", str(self.root / "stream.json")])
        self.assertEqual(
            ml._config_from_args(default_args).ignition_surge_ratio,
            live.default_lifecycle_config().ignition_surge_ratio,
        )

        override_args = parser.parse_args(
            ["--tick", str(self.root / "stream.json"), "--ignition-surge", "5.5"]
        )
        self.assertEqual(
            ml._config_from_args(override_args).ignition_surge_ratio,
            Decimal("5.5"),
        )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(
                ["--tick", str(self.root / "stream.json"), "--ignition-surge", "NaN"]
            )

    def test_env_ignition_value_reaches_live_tick_decision(self):
        script = textwrap.dedent(
            """
            import json
            import tempfile
            from pathlib import Path

            from tests.test_momentum_lifecycle import fixed_now, stream_payload
            import limits
            import momentum_live as live

            provided = hasattr(limits, "MOMENTUM_IGNITION_SURGE_RATIO")
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stream = root / "stream.json"
                common = {
                    "state_path": root / "state.json",
                    "receipt_path": root / "receipts.jsonl",
                    "decisions_path": root / "decisions.jsonl",
                    "jobs_dir": root / "jobs",
                    "position_path": root / "position.json",
                    "results_path": root / "results.jsonl",
                    "now_fn": fixed_now,
                    "decision_only": True,
                }
                stream.write_text(json.dumps({
                    "AAAUSDT": stream_payload(0, "100", "2", "100", "1000")
                }), encoding="utf-8")
                live.run_tick(stream, **common)
                stream.write_text(json.dumps({
                    "AAAUSDT": stream_payload(1, "101", "5", "500", "1010")
                }), encoding="utf-8")
                result = live.run_tick(stream, **common)

            print(json.dumps({
                "action": result.decisions[0]["action"],
                "provided": provided,
                "reason": result.decisions[0]["reason"],
                "threshold": str(
                    live.default_lifecycle_config().ignition_surge_ratio
                ),
            }))
            """
        )
        environment = dict(
            os.environ,
            PYTHONPYCACHEPREFIX="/private/tmp/basis-w3-env-test-pycache",
            QUANT_IGNITION_SURGE="4",
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["threshold"], "4" if result["provided"] else "3")
        self.assertEqual(result["action"], ml.ACTION_WATCH)
        self.assertEqual(
            result["reason"], "ignition edge observed; persistence started"
        )


if __name__ == "__main__":
    unittest.main()
