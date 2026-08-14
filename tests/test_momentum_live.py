#!/usr/bin/env python3
"""Offline end-to-end tests for momentum OPEN, HOLD, and EXIT routing."""

import io
import json
import socket
import sys
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import momentum_lifecycle as lifecycle
import momentum_live as live


FIXED_NOW = "2026-08-14T12:00:00.000Z"


def fixed_now():
    return FIXED_NOW


class AdvancingClock:
    def __init__(self, value=FIXED_NOW):
        self.value = datetime.fromisoformat(value.replace("Z", "+00:00"))

    def __call__(self):
        return self.value.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )

    def advance(self, minutes=1):
        self.value += timedelta(minutes=minutes)


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
        "basis_gap_pct": "0.1",
        "freshness_ms": text(freshness_ms),
    }


def test_config(initial_notional=Decimal("8")):
    return lifecycle.LifecycleConfig(
        ignition_surge_ratio=Decimal("10"),
        persistence_surge_ratio=Decimal("8"),
        persistence_passes=2,
        initial_notional=initial_notional,
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


class SimulatedKill(Exception):
    pass


class MomentumLiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stream = self.root / "stream_state.json"
        self.state = self.root / "momentum_state.json"
        self.receipts = self.root / "momentum_receipts.jsonl"
        self.decisions = self.root / "decisions.jsonl"
        self.runner = self.root / "runner"
        self.jobs = self.runner / "jobs"
        self.position = self.runner / "momentum-position.json"
        self.results = self.jobs / "results.jsonl"
        self.config = test_config()

        def deny(*_args, **_kwargs):
            raise AssertionError("momentum_live test attempted network access")

        for target, name in (
            (urllib.request, "urlopen"),
            (socket, "socket"),
            (socket, "create_connection"),
        ):
            patcher = mock.patch.object(target, name, deny)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tick(self, payload, **overrides):
        self.stream.write_text(
            json.dumps({"AAAUSDT": payload}), encoding="utf-8"
        )
        values = {
            "state_path": self.state,
            "receipt_path": self.receipts,
            "decisions_path": self.decisions,
            "jobs_dir": self.jobs,
            "position_path": self.position,
            "results_path": self.results,
            "config": self.config,
            "now_fn": fixed_now,
        }
        values.update(overrides)
        return live.run_tick(self.stream, **values)

    def decision_rows(self):
        if not self.decisions.exists():
            return []
        return [
            json.loads(line)
            for line in self.decisions.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def job_paths(self):
        return sorted(self.jobs.glob("job-*.json")) if self.jobs.exists() else []

    def jobs_by_op(self):
        result = {}
        for path in self.job_paths():
            payload = json.loads(path.read_text(encoding="utf-8"))
            result[payload["op"]] = payload
        return result

    def signal(self, *, job_writer=live.write_job_record, venues="both"):
        results = [
            self.tick(stream_payload(0, "100", "2", "100", "1000", venues=venues)),
            self.tick(stream_payload(1, "105", "70", "7000", "1050", spread_pct="0.008", venues=venues)),
            self.tick(
                stream_payload(2, "108", "75", "7600", "1070", spread_pct="0.007", venues=venues),
                job_writer=job_writer,
            ),
        ]
        return results

    def inject_open(self, job, *, qty="0.071", price="112.5", fills=None):
        self.position.parent.mkdir(parents=True, exist_ok=True)
        if fills is None:
            fills = [{
                "trade_id": "91", "order_id": "1101", "price": price,
                "qty": qty, "realized_pnl": "0", "commission": "0.003",
                "commission_asset": "USDT",
            }]
        self.position.write_text(
            json.dumps({
                "version": 1,
                "revision": 2,
                "state": "OPEN",
                "symbol": "AAAUSDT",
                "job_id": job["job_id"],
                "measured_qty": qty,
                "entry": {"qty": qty, "price": price, "fills": fills},
            }),
            encoding="utf-8",
        )
        self.results.parent.mkdir(parents=True, exist_ok=True)
        receipt = self.jobs / "receipts" / f"job-{job['job_id']}.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({
            "job_id": job["job_id"], "op": live.JOB_OPEN_OP,
            "symbol": "AAAUSDT", "verdict": "OPEN", "reason": "",
            "utc": FIXED_NOW, "fills": fills, "fees": [], "realized": {},
        }), encoding="utf-8")
        self.results.write_text(
            json.dumps({
                "job_id": job["job_id"],
                "op": live.JOB_OPEN_OP,
                "symbol": "AAAUSDT",
                "verdict": "OPEN",
                "exit_code": 0,
                "receipt_path": str(receipt),
                "utc": FIXED_NOW,
                "fills": fills,
                "fees": [],
                "realized": {"gross_usdt": "0", "net_usdt": "UNKNOWN"},
            }) + "\n",
            encoding="utf-8",
        )

    def inject_closed(self, exit_job):
        prior = json.loads(self.position.read_text(encoding="utf-8"))
        self.position.write_text(
            json.dumps({
                **prior,
                "revision": prior["revision"] + 2,
                "state": "CLOSED",
                "job_id": exit_job["job_id"],
                "measured_qty": "0",
                "exit": {"qty": prior["measured_qty"], "price": "118"},
            }),
            encoding="utf-8",
        )
        fill = {
            "trade_id": "92", "order_id": "2202", "price": "118",
            "qty": prior["measured_qty"], "realized_pnl": "0.2",
            "commission": "0.002", "commission_asset": "USDT",
        }
        receipt = self.jobs / "receipts" / f"job-{exit_job['job_id']}.json"
        receipt.write_text(json.dumps({
            "job_id": exit_job["job_id"], "op": live.JOB_EXIT_OP,
            "symbol": "AAAUSDT", "verdict": "CLOSED", "reason": "",
            "utc": FIXED_NOW, "fills": [fill], "fees": [], "realized": {},
        }), encoding="utf-8")
        with open(self.results, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "job_id": exit_job["job_id"],
                "op": live.JOB_EXIT_OP,
                "symbol": "AAAUSDT",
                "verdict": "CLOSED",
                "exit_code": 0,
                "receipt_path": str(receipt),
                "utc": FIXED_NOW,
                "fills": [fill],
                "fees": [],
                "realized": {"gross_usdt": "0.2", "net_usdt": "0.195"},
            }) + "\n")

    def test_full_enter_hold_exit_across_ticks_uses_actual_fill(self):
        signalled = self.signal()
        self.assertEqual(
            [result.decisions[0]["action"] for result in signalled],
            [lifecycle.ACTION_WATCH, lifecycle.ACTION_WATCH, lifecycle.ACTION_ENTER_SMALL],
        )
        jobs = self.jobs_by_op()
        self.assertEqual(set(jobs), {live.JOB_OPEN_OP})
        open_job = jobs[live.JOB_OPEN_OP]
        self.inject_open(open_job, qty="0.071", price="112.5")

        held = self.tick(
            stream_payload(3, "150", "90", "9000", "1100", spread_pct="0.006", oi_change_pct="3")
        )
        self.assertEqual(held.decisions[0]["action"], lifecycle.ACTION_ADD)
        token = lifecycle.load_state(self.state).tokens["AAAUSDT"]
        self.assertEqual(token["phase"], lifecycle.PHASE_HOLD)
        self.assertEqual(token["qty"], "0.071")
        self.assertEqual(token["avg_entry_price"], "112.5")
        self.assertEqual(token["entry_structure"]["price"], "112.5")
        self.assertNotEqual(token["entry_structure"]["signal_price"], "112.5")

        exited = self.tick(
            stream_payload(4, "119", "30", "3000", "900", oi_change_pct="-8")
        )
        self.assertEqual(exited.decisions[0]["action"], lifecycle.ACTION_EXIT)
        jobs = self.jobs_by_op()
        self.assertEqual(set(jobs), {live.JOB_OPEN_OP, live.JOB_EXIT_OP})
        exit_job = jobs[live.JOB_EXIT_OP]
        token = lifecycle.load_state(self.state).tokens["AAAUSDT"]
        self.assertEqual(token["phase"], lifecycle.PHASE_EXIT_PENDING)
        self.assertEqual(token["qty"], "0.071")

        pending = self.tick(
            stream_payload(5, "118", "25", "2500", "880", oi_change_pct="-8")
        )
        self.assertEqual(pending.decisions[0]["action"], lifecycle.ACTION_HOLD)
        self.assertEqual(len(self.job_paths()), 2)

        self.inject_closed(exit_job)
        closed = self.tick(stream_payload(6, "117", "2", "100", "800"))
        self.assertEqual(closed.decisions[0]["action"], lifecycle.ACTION_WATCH)
        token = lifecycle.load_state(self.state).tokens["AAAUSDT"]
        self.assertEqual(token["position_status"], "FLAT")
        self.assertEqual(token["qty"], "0")
        self.assertEqual(len(self.job_paths()), 2)

    def test_multilevel_fill_accepts_exchange_rounded_entry_price(self):
        self.signal()
        open_job = self.jobs_by_op()[live.JOB_OPEN_OP]
        fills = [
            {
                "trade_id": "91", "order_id": "1101", "price": "100.01",
                "qty": "0.5", "realized_pnl": "0", "commission": "0.001",
                "commission_asset": "USDT",
            },
            {
                "trade_id": "92", "order_id": "1101", "price": "100.02",
                "qty": "0.5", "realized_pnl": "0", "commission": "0.001",
                "commission_asset": "USDT",
            },
        ]
        self.inject_open(open_job, qty="1", price="100.01", fills=fills)

        held = self.tick(
            stream_payload(
                3, "150", "90", "9000", "1100",
                spread_pct="0.006", oi_change_pct="3",
            )
        )
        self.assertEqual(held.decisions[0]["action"], lifecycle.ACTION_ADD)
        token = lifecycle.load_state(self.state).tokens["AAAUSDT"]
        self.assertEqual(Decimal(token["qty"]), Decimal("1"))
        self.assertEqual(token["avg_entry_price"], "100.01")

    def test_multilevel_fill_entry_price_beyond_tick_fails_closed(self):
        self.signal()
        open_job = self.jobs_by_op()[live.JOB_OPEN_OP]
        fills = [
            {
                "trade_id": "91", "order_id": "1101", "price": "100.01",
                "qty": "0.5", "realized_pnl": "0", "commission": "0.001",
                "commission_asset": "USDT",
            },
            {
                "trade_id": "92", "order_id": "1101", "price": "100.02",
                "qty": "0.5", "realized_pnl": "0", "commission": "0.001",
                "commission_asset": "USDT",
            },
        ]
        self.inject_open(open_job, qty="1", price="100.03", fills=fills)

        with self.assertRaisesRegex(
            live.MomentumLiveError, "entry price conflict"
        ):
            self.tick(stream_payload(3, "150", "90", "9000", "1100"))

    def test_open_and_exit_ids_are_deterministic_per_ignition(self):
        self.signal()
        jobs = self.jobs_by_op()
        ignition = "2026-08-14T10:01:00.000Z"
        self.assertEqual(
            jobs[live.JOB_OPEN_OP]["job_id"],
            live.deterministic_job_id("AAAUSDT", ignition, live.JOB_OPEN_OP),
        )
        self.inject_open(jobs[live.JOB_OPEN_OP])
        self.tick(stream_payload(3, "80", "0.5", "50", "900", oi_change_pct="-8"))
        jobs = self.jobs_by_op()
        self.assertEqual(
            jobs[live.JOB_EXIT_OP]["job_id"],
            live.deterministic_job_id("AAAUSDT", ignition, live.JOB_EXIT_OP),
        )
        self.assertNotEqual(
            jobs[live.JOB_OPEN_OP]["job_id"], jobs[live.JOB_EXIT_OP]["job_id"]
        )

    def test_crash_after_open_decision_recovers_exactly_one_job(self):
        self.tick(stream_payload(0, "100", "2", "100", "1000"))
        self.tick(stream_payload(1, "105", "70", "7000", "1050", spread_pct="0.008"))

        def killed(_jobs_dir, _payload):
            raise SimulatedKill("killed before job publication")

        payload = stream_payload(2, "108", "75", "7600", "1070", spread_pct="0.007")
        with self.assertRaises(SimulatedKill):
            self.tick(payload, job_writer=killed)
        self.assertEqual(self.job_paths(), [])

        recovered = self.tick(payload)
        expected = live.deterministic_job_id(
            "AAAUSDT", "2026-08-14T10:01:00.000Z", live.JOB_OPEN_OP
        )
        self.assertEqual(recovered.recovered_job_ids, (expected,))
        self.assertEqual(len(self.job_paths()), 1)
        replay = self.tick(payload)
        self.assertEqual(replay.recovered_job_ids, ())
        self.assertEqual(replay.written_job_ids, ())
        self.assertEqual(len(self.job_paths()), 1)

    def test_crash_after_job_publication_before_save_state_is_idempotent(self):
        clock = AdvancingClock()
        self.tick(
            stream_payload(0, "100", "2", "100", "1000"), now_fn=clock
        )
        clock.advance()
        self.tick(
            stream_payload(
                1, "105", "70", "7000", "1050", spread_pct="0.008"
            ),
            now_fn=clock,
        )
        clock.advance()
        payload = stream_payload(
            2, "108", "75", "7600", "1070", spread_pct="0.007"
        )

        def killed_after_publication(*_args, **_kwargs):
            job_rows = [
                row for row in self.decision_rows() if row["job"] is not None
            ]
            self.assertEqual(len(job_rows), 1)
            self.assertTrue(live.write_job_record(self.jobs, job_rows[0]["job"]))
            self.assertEqual(len(self.job_paths()), 1)
            raise SimulatedKill("killed after job publication")

        with mock.patch.object(
            live.lifecycle, "save_state", side_effect=killed_after_publication
        ):
            with self.assertRaises(SimulatedKill):
                self.tick(payload, now_fn=clock)
        self.assertEqual(len(self.job_paths()), 1)

        clock.advance()
        replay = self.tick(payload, now_fn=clock)
        self.assertEqual(replay.recovered_job_ids, ())
        self.assertEqual(replay.written_job_ids, ())
        self.assertEqual(len(self.job_paths()), 1)
        enter_rows = [
            row for row in self.decision_rows()
            if row["action"] == lifecycle.ACTION_ENTER_SMALL
        ]
        self.assertEqual(len(enter_rows), 1)

    def test_crash_after_state_save_before_job_publication_republishes_once(self):
        self.tick(stream_payload(0, "100", "2", "100", "1000"))
        self.tick(
            stream_payload(
                1, "105", "70", "7000", "1050", spread_pct="0.008"
            )
        )
        payload = stream_payload(
            2, "108", "75", "7600", "1070", spread_pct="0.007"
        )
        expected = live.deterministic_job_id(
            "AAAUSDT", "2026-08-14T10:01:00.000Z", live.JOB_OPEN_OP
        )

        def killed_before_publication(_jobs_dir, job):
            saved = lifecycle.load_state(self.state).tokens["AAAUSDT"]
            self.assertEqual(saved["open_job_id"], job["job_id"])
            self.assertEqual(saved["phase"], lifecycle.PHASE_ENTER_SMALL)
            raise SimulatedKill("killed after state save before publication")

        with self.assertRaises(SimulatedKill):
            self.tick(payload, job_writer=killed_before_publication)
        self.assertEqual(self.job_paths(), [])
        self.assertEqual(
            lifecycle.load_state(self.state).tokens["AAAUSDT"]["open_job_id"],
            expected,
        )

        created = []

        def counting_writer(jobs_dir, job):
            published = live.write_job_record(jobs_dir, job)
            if published:
                created.append(job["job_id"])
            return published

        recovered = self.tick(payload, job_writer=counting_writer)
        self.assertEqual(recovered.recovered_job_ids, (expected,))
        replay = self.tick(payload, job_writer=counting_writer)
        self.assertEqual(replay.recovered_job_ids, ())
        self.assertEqual(replay.written_job_ids, ())
        self.assertEqual(created, [expected])
        self.assertEqual(len(self.job_paths()), 1)

    def test_crash_after_job_publication_keeps_ledger_reconcile_unchanged(self):
        self.tick(stream_payload(0, "100", "2", "100", "1000"))
        self.tick(
            stream_payload(
                1, "105", "70", "7000", "1050", spread_pct="0.008"
            )
        )
        payload = stream_payload(
            2, "108", "75", "7600", "1070", spread_pct="0.007"
        )

        def publish_then_kill(jobs_dir, job):
            self.assertTrue(live.write_job_record(jobs_dir, job))
            saved = lifecycle.load_state(self.state).tokens["AAAUSDT"]
            self.assertEqual(saved["open_job_id"], job["job_id"])
            raise SimulatedKill("killed after job publication")

        with self.assertRaises(SimulatedKill):
            self.tick(payload, job_writer=publish_then_kill)
        job_rows_before = [
            row for row in self.decision_rows() if row["job"] is not None
        ]
        self.assertEqual(len(job_rows_before), 1)
        self.assertEqual(len(self.job_paths()), 1)

        replay = self.tick(payload)
        job_rows_after = [
            row for row in self.decision_rows() if row["job"] is not None
        ]
        self.assertEqual(replay.recovered_job_ids, ())
        self.assertEqual(replay.written_job_ids, ())
        self.assertEqual(job_rows_after, job_rows_before)
        self.assertEqual(len(self.job_paths()), 1)

    def test_crash_before_open_decision_append_recovers_receipt_and_job(self):
        self.tick(stream_payload(0, "100", "2", "100", "1000"))
        self.tick(stream_payload(1, "105", "70", "7000", "1050", spread_pct="0.008"))
        payload = stream_payload(2, "108", "75", "7600", "1070", spread_pct="0.007")
        with mock.patch.object(
            live, "append_decision", side_effect=SimulatedKill("after lifecycle receipt")
        ):
            with self.assertRaises(SimulatedKill):
                self.tick(payload)
        self.assertEqual(self.job_paths(), [])

        recovered = self.tick(payload)
        self.assertEqual(len(recovered.recovered_job_ids), 1)
        self.assertEqual(len(self.job_paths()), 1)
        enter_rows = [
            row for row in self.decision_rows()
            if row["action"] == lifecycle.ACTION_ENTER_SMALL
        ]
        self.assertEqual(len(enter_rows), 1)

    def test_crash_after_exit_decision_recovers_exactly_one_exit_job(self):
        self.signal()
        open_job = self.jobs_by_op()[live.JOB_OPEN_OP]
        self.inject_open(open_job)
        self.tick(
            stream_payload(3, "150", "90", "9000", "1100", spread_pct="0.006", oi_change_pct="3")
        )

        def killed_on_exit(jobs_dir, payload):
            if payload["op"] == live.JOB_EXIT_OP:
                raise SimulatedKill("killed before EXIT publication")
            return live.write_job_record(jobs_dir, payload)

        exit_payload = stream_payload(
            4, "119", "30", "3000", "900", oi_change_pct="-8"
        )
        with self.assertRaises(SimulatedKill):
            self.tick(exit_payload, job_writer=killed_on_exit)
        self.assertEqual(set(self.jobs_by_op()), {live.JOB_OPEN_OP})

        recovered = self.tick(exit_payload)
        expected = live.deterministic_job_id(
            "AAAUSDT", "2026-08-14T10:01:00.000Z", live.JOB_EXIT_OP
        )
        self.assertEqual(recovered.recovered_job_ids, (expected,))
        self.assertEqual(set(self.jobs_by_op()), {live.JOB_OPEN_OP, live.JOB_EXIT_OP})
        replay = self.tick(exit_payload)
        self.assertEqual(replay.recovered_job_ids, ())
        self.assertEqual(len(self.job_paths()), 2)

    def test_freshness_loss_queues_one_exit_and_keeps_position(self):
        self.signal()
        open_job = self.jobs_by_op()[live.JOB_OPEN_OP]
        self.inject_open(open_job)
        stale_payload = stream_payload(
            3, "115", "80", "8000", "1080", freshness_ms="5001"
        )
        first = self.tick(stale_payload)
        self.assertEqual(first.decisions[0]["action"], lifecycle.ACTION_EXIT)
        second = self.tick(stale_payload)
        self.assertIn(
            second.decisions[0]["action"],
            {lifecycle.ACTION_DUPLICATE, lifecycle.ACTION_HOLD},
        )
        self.assertEqual(len(self.job_paths()), 2)
        token = lifecycle.load_state(self.state).tokens["AAAUSDT"]
        self.assertEqual(token["qty"], "0.071")
        self.assertEqual(token["position_status"], "EXIT_PENDING")

    def test_position_without_matching_result_fails_closed(self):
        self.signal()
        open_job = self.jobs_by_op()[live.JOB_OPEN_OP]
        self.position.parent.mkdir(parents=True, exist_ok=True)
        self.position.write_text(json.dumps({
            "version": 1,
            "revision": 2,
            "state": "OPEN",
            "symbol": "AAAUSDT",
            "job_id": open_job["job_id"],
            "measured_qty": "0.071",
            "entry": {"qty": "0.071", "price": "112.5"},
        }), encoding="utf-8")
        with self.assertRaises(live.MomentumLiveError):
            self.tick(stream_payload(3, "120", "90", "9000", "1100"))

    def test_conflicting_deterministic_job_is_fail_closed(self):
        job_id = live.deterministic_job_id(
            "AAAUSDT", "2026-08-14T10:01:00.000Z", live.JOB_OPEN_OP
        )
        payload = {
            "job_id": job_id,
            "op": live.JOB_OPEN_OP,
            "symbol": "AAAUSDT",
            "max_notional": Decimal("8"),
            "created_utc": FIXED_NOW,
            "expires_utc": "2026-08-14T12:10:00.000Z",
            "requested_by": live.REQUESTED_BY,
        }
        self.assertTrue(live.write_job_record(self.jobs, payload))
        with self.assertRaises(live.MomentumLiveError):
            live.write_job_record(self.jobs, dict(payload, requested_by="other"))
        self.assertEqual(len(self.job_paths()), 1)

    def test_cli_accepts_injected_runner_paths_and_reports_open(self):
        self.tick(stream_payload(0, "100", "2", "100", "1000"))
        self.tick(stream_payload(1, "105", "70", "7000", "1050", spread_pct="0.008"))
        self.stream.write_text(json.dumps({
            "AAAUSDT": stream_payload(2, "108", "75", "7600", "1070", spread_pct="0.007")
        }), encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        args = [
            "--tick", str(self.stream),
            "--state", str(self.state),
            "--receipts", str(self.receipts),
            "--decisions", str(self.decisions),
            "--jobs-dir", str(self.jobs),
            "--position", str(self.position),
            "--results", str(self.results),
            "--initial-notional", "8",
        ]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = live.main(args)
        self.assertEqual(code, 0, stderr.getvalue())
        summary = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(summary["jobs_written"], 1)
        self.assertEqual(self.jobs_by_op()[live.JOB_OPEN_OP]["op"], live.JOB_OPEN_OP)

    def test_manage_only_publishes_exit_for_standing_position(self):
        self.signal()
        open_job = self.jobs_by_op()[live.JOB_OPEN_OP]
        self.inject_open(open_job)

        result = self.tick(
            stream_payload(
                3, "80", "0.5", "50", "900", oi_change_pct="-8"
            ),
            manage_only=True,
        )

        self.assertEqual(result.decisions[0]["action"], lifecycle.ACTION_EXIT)
        jobs = self.jobs_by_op()
        self.assertEqual(set(jobs), {live.JOB_OPEN_OP, live.JOB_EXIT_OP})
        self.assertEqual(result.written_job_ids, (jobs[live.JOB_EXIT_OP]["job_id"],))

    def test_manage_only_refuses_entry_publication(self):
        self.tick(stream_payload(0, "100", "2", "100", "1000"))
        self.tick(
            stream_payload(
                1, "105", "70", "7000", "1050", spread_pct="0.008"
            )
        )

        result = self.tick(
            stream_payload(
                2, "108", "75", "7600", "1070", spread_pct="0.007"
            ),
            manage_only=True,
        )

        self.assertEqual(len(result.decisions), 1)
        self.assertEqual(result.decisions[0]["action"], "REFUSED")
        self.assertEqual(result.decisions[0]["reason"], "manage_only")
        self.assertIsNone(result.decisions[0]["job"])
        self.assertEqual(self.job_paths(), [])
        self.assertNotIn(
            lifecycle.ACTION_ENTER_SMALL,
            [row["action"] for row in self.decision_rows()],
        )
        token = lifecycle.load_state(self.state).tokens["AAAUSDT"]
        self.assertEqual(token["phase"], lifecycle.PHASE_WATCH)
        self.assertIsNone(token["open_job_id"])

    def test_manage_only_refuses_persisted_open_recovery(self):
        self.tick(stream_payload(0, "100", "2", "100", "1000"))
        self.tick(
            stream_payload(
                1, "105", "70", "7000", "1050", spread_pct="0.008"
            )
        )
        payload = stream_payload(
            2, "108", "75", "7600", "1070", spread_pct="0.007"
        )

        def killed(_jobs_dir, _job):
            raise SimulatedKill("killed before OPEN publication")

        with self.assertRaises(SimulatedKill):
            self.tick(payload, job_writer=killed)
        enter_rows = [
            row for row in self.decision_rows()
            if row["action"] == lifecycle.ACTION_ENTER_SMALL
        ]
        calls = []

        def recording_writer(_jobs_dir, job):
            calls.append(job)
            return False

        result = self.tick(
            payload, manage_only=True, job_writer=recording_writer
        )

        self.assertEqual(calls, [])
        self.assertEqual(result.decisions[0]["action"], "REFUSED")
        self.assertEqual(result.decisions[0]["reason"], "manage_only")
        self.assertEqual(self.job_paths(), [])
        self.assertEqual(
            [
                row for row in self.decision_rows()
                if row["action"] == lifecycle.ACTION_ENTER_SMALL
            ],
            enter_rows,
        )

    def test_manage_only_flat_cli_reports_noop_and_exits_zero(self):
        self.stream.write_text(
            json.dumps({
                "AAAUSDT": stream_payload(0, "100", "2", "100", "1000")
            }),
            encoding="utf-8",
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        args = [
            "--tick", str(self.stream),
            "--state", str(self.state),
            "--receipts", str(self.receipts),
            "--decisions", str(self.decisions),
            "--jobs-dir", str(self.jobs),
            "--position", str(self.position),
            "--results", str(self.results),
            "--initial-notional", "8",
            "--manage-only",
        ]

        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = live.main(args)

        self.assertEqual(code, 0, stderr.getvalue())
        output = stdout.getvalue().splitlines()
        self.assertEqual(json.loads(output[0])["jobs_written"], 0)
        row = json.loads(output[1])
        self.assertEqual(row["action"], "NOOP")
        self.assertEqual(row["reason"], "no_standing_position")
        self.assertEqual(self.job_paths(), [])

    def test_normal_mode_still_publishes_open(self):
        self.tick(stream_payload(0, "100", "2", "100", "1000"))
        self.tick(
            stream_payload(
                1, "105", "70", "7000", "1050", spread_pct="0.008"
            )
        )

        result = self.tick(
            stream_payload(
                2, "108", "75", "7600", "1070", spread_pct="0.007"
            )
        )

        self.assertEqual(
            result.decisions[0]["action"], lifecycle.ACTION_ENTER_SMALL
        )
        self.assertEqual(set(self.jobs_by_op()), {live.JOB_OPEN_OP})

    def test_decision_only_records_open_without_runner_or_receipt_writes(self):
        def denied(*_args, **_kwargs):
            raise AssertionError("decision-only touched the runner")

        with mock.patch.object(
            live, "read_runner_position", side_effect=denied
        ), mock.patch.object(
            live, "read_results_ledger", side_effect=denied
        ), mock.patch.object(
            live, "reconcile_runner_truth", side_effect=denied
        ):
            results = [
                self.tick(
                    stream_payload(0, "100", "2", "100", "1000"),
                    decision_only=True,
                    job_writer=denied,
                ),
                self.tick(
                    stream_payload(
                        1, "105", "70", "7000", "1050",
                        spread_pct="0.008",
                    ),
                    decision_only=True,
                    job_writer=denied,
                ),
                self.tick(
                    stream_payload(
                        2, "108", "75", "7600", "1070",
                        spread_pct="0.007",
                    ),
                    decision_only=True,
                    job_writer=denied,
                ),
            ]

        self.assertEqual(
            [result.decisions[0]["action"] for result in results],
            [
                lifecycle.ACTION_WATCH,
                lifecycle.ACTION_WATCH,
                lifecycle.ACTION_ENTER_SMALL,
            ],
        )
        self.assertNotIn("decision_only", results[0].decisions[0])
        row = results[-1].decisions[0]
        self.assertIs(row["decision_only"], True)
        self.assertEqual(row["job"]["op"], live.JOB_OPEN_OP)
        self.assertEqual(results[-1].recovered_job_ids, ())
        self.assertEqual(results[-1].written_job_ids, ())
        self.assertEqual(self.job_paths(), [])
        self.assertFalse(self.receipts.exists())
        self.assertIs(self.decision_rows()[-1]["decision_only"], True)

        resumed = self.tick(
            stream_payload(
                3, "110", "80", "8000", "1080", spread_pct="0.007"
            )
        )
        self.assertEqual(resumed.recovered_job_ids, ())
        self.assertEqual(resumed.written_job_ids, ())
        self.assertEqual(self.job_paths(), [])

    def test_decision_only_records_standing_exit_without_publication(self):
        self.signal()
        open_job = self.jobs_by_op()[live.JOB_OPEN_OP]
        self.inject_open(open_job)
        self.tick(
            stream_payload(
                3, "150", "90", "9000", "1100",
                spread_pct="0.006", oi_change_pct="3",
            )
        )
        jobs_before = self.job_paths()
        receipts_before = self.receipts.read_bytes()

        def denied(*_args, **_kwargs):
            raise AssertionError("decision-only touched the runner")

        with mock.patch.object(
            live, "read_runner_position", side_effect=denied
        ), mock.patch.object(
            live, "read_results_ledger", side_effect=denied
        ), mock.patch.object(
            live, "reconcile_runner_truth", side_effect=denied
        ):
            result = self.tick(
                stream_payload(
                    4, "80", "0.5", "50", "900", oi_change_pct="-8"
                ),
                decision_only=True,
                job_writer=denied,
            )

        row = result.decisions[0]
        self.assertEqual(row["action"], lifecycle.ACTION_EXIT)
        self.assertIs(row["decision_only"], True)
        self.assertEqual(row["job"]["op"], live.JOB_EXIT_OP)
        self.assertEqual(result.written_job_ids, ())
        self.assertEqual(self.job_paths(), jobs_before)
        self.assertEqual(self.receipts.read_bytes(), receipts_before)
        token = lifecycle.load_state(self.state).tokens["AAAUSDT"]
        self.assertEqual(token["exit_job_id"], row["job"]["job_id"])

    def test_manage_only_publishes_standing_exit_when_both_flags_are_set(self):
        self.signal()
        open_job = self.jobs_by_op()[live.JOB_OPEN_OP]
        self.inject_open(open_job)
        self.tick(
            stream_payload(
                3, "150", "90", "9000", "1100",
                spread_pct="0.006", oi_change_pct="3",
            )
        )

        result = self.tick(
            stream_payload(
                4, "80", "0.5", "50", "900", oi_change_pct="-8"
            ),
            manage_only=True,
            decision_only=True,
        )

        row = result.decisions[0]
        self.assertEqual(row["action"], lifecycle.ACTION_EXIT)
        self.assertNotIn("decision_only", row)
        jobs = self.jobs_by_op()
        self.assertEqual(set(jobs), {live.JOB_OPEN_OP, live.JOB_EXIT_OP})
        self.assertEqual(
            result.written_job_ids, (jobs[live.JOB_EXIT_OP]["job_id"],)
        )

    def test_decision_only_governs_flat_symbols_when_manage_only_is_set(self):
        self.tick(stream_payload(0, "100", "2", "100", "1000"))
        self.tick(
            stream_payload(
                1, "105", "70", "7000", "1050", spread_pct="0.008"
            )
        )

        def denied(*_args, **_kwargs):
            raise AssertionError("decision-only touched the runner")

        result = self.tick(
            stream_payload(
                2, "108", "75", "7600", "1070",
                spread_pct="0.007",
            ),
            manage_only=True,
            decision_only=True,
            job_writer=denied,
        )

        row = result.decisions[0]
        self.assertEqual(row["action"], lifecycle.ACTION_ENTER_SMALL)
        self.assertEqual(row["reason"], "configured persistence satisfied")
        self.assertIs(row["decision_only"], True)
        self.assertEqual(self.job_paths(), [])

    def test_300_symbol_tick_decision_work_stays_bounded_across_history(self):
        symbols = [f"S{index:03d}USDT" for index in range(300)]
        clock = AdvancingClock()
        read_counts = []
        decision_times = []
        original_read = live._read_decisions
        threshold = 64 * 1024
        common = {
            "state_path": self.state,
            "receipt_path": self.receipts,
            "decisions_path": self.decisions,
            "jobs_dir": self.jobs,
            "position_path": self.position,
            "results_path": self.results,
            "config": self.config,
            "now_fn": clock,
        }
        with mock.patch.object(
            live, "DECISIONS_ROTATE_BYTES", threshold
        ), mock.patch.object(
            live, "_read_decisions", wraps=original_read
        ) as reads, mock.patch.object(
            live.os, "fsync", return_value=None
        ), mock.patch.object(
            live, "_fsync_directory", return_value=None
        ):
            for minute in range(12):
                payload = {
                    symbol: stream_payload(
                        minute, "100", "2", "100", "1000"
                    )
                    for symbol in symbols
                }
                self.stream.write_text(json.dumps(payload), encoding="utf-8")
                before = reads.call_count
                result = live.run_tick(self.stream, **common)
                read_counts.append(reads.call_count - before)
                decision_times.append(result.decisions[0]["decision_utc"])
                clock.advance()

        backup = self.decisions.with_name(self.decisions.name + ".1")
        self.assertTrue(backup.exists())
        self.assertLessEqual(self.decisions.stat().st_size, threshold)
        self.assertLessEqual(backup.stat().st_size, threshold)
        self.assertLess(max(read_counts), 20)
        self.assertLessEqual(max(read_counts[2:]) - min(read_counts[2:]), 2)
        self.assertEqual(len(set(decision_times)), 12)
        self.assertLess(len(live._decision_index(self.decisions).rows_by_id), 600)

    def test_runtime_caps_remain_decimal_only(self):
        self.stream.write_text("{}", encoding="utf-8")
        common = {
            "state_path": self.state,
            "receipt_path": self.receipts,
            "decisions_path": self.decisions,
            "jobs_dir": self.jobs,
            "position_path": self.position,
            "results_path": self.results,
            "now_fn": fixed_now,
        }
        for value in (Decimal("32.01"), Decimal("0"), None, True, 8, "8"):
            with self.subTest(value=value):
                with self.assertRaises(live.MomentumLiveError):
                    live.run_tick(self.stream, max_job_notional=value, **common)
        with self.assertRaises(live.MomentumLiveError):
            live.run_tick(
                self.stream, job_ttl=timedelta(minutes=16), **common
            )

    def test_max_job_notional_defaults_to_the_compiled_cap(self):
        args = live.build_parser().parse_args([
            "--tick", str(self.stream), "--decision-only",
        ])
        self.assertEqual(args.max_job_notional, Decimal("32"))
        self.assertTrue(args.decision_only)
        self.assertEqual(live.HARD_MAX_NOTIONAL, Decimal("32"))


if __name__ == "__main__":
    unittest.main()
