#!/usr/bin/env python3
"""Offline tests for momentum decisions-to-runner-job routing."""

import io
import json
import socket
import sys
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import momentum_lifecycle as lifecycle  # noqa: E402
import momentum_live as live  # noqa: E402
import job_runner as runner  # noqa: E402


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
    venues="both",
):
    price = Decimal(str(price))
    spread = Decimal(str(spread_pct))
    half = spread / Decimal("2")
    spot_bid = price * (Decimal("1") - half)
    spot_ask = price * (Decimal("1") + half)
    perp_mid = price * Decimal("1.001")
    perp_bid = perp_mid * (Decimal("1") - half)
    perp_ask = perp_mid * (Decimal("1") + half)

    def text(value):
        return None if value is None else format(Decimal(str(value)), "f")

    return {
        "utc": f"2026-08-14T10:{minute:02d}:00.000Z",
        "venues": venues,
        "spot_bid": text(spot_bid) if venues in {"spot", "both"} else None,
        "spot_ask": text(spot_ask) if venues in {"spot", "both"} else None,
        "perp_bid": text(perp_bid) if venues in {"perp", "both"} else None,
        "perp_ask": text(perp_ask) if venues in {"perp", "both"} else None,
        "mark": text(perp_mid) if venues in {"perp", "both"} else text(price),
        "vol_1m_quote": text(volume),
        "vol_med_1m": "100",
        "surge_ratio": text(surge),
        "oi": text(oi),
        "oi_change_pct": text(oi_change_pct),
        "funding_rate": "0.0001" if venues in {"perp", "both"} else None,
        "next_funding_ms": (
            "1786636800000" if venues in {"perp", "both"} else None
        ),
        "basis_gap_pct": "0.1",
        "freshness_ms": text(freshness_ms),
    }


def test_config(initial_notional=Decimal("8")):
    return lifecycle.LifecycleConfig(
        ignition_surge_ratio=Decimal("10"),
        persistence_surge_ratio=Decimal("8"),
        persistence_passes=2,
        initial_notional=initial_notional,
        add_notional=Decimal("4"),
        max_total_notional=Decimal("24"),
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
        self.jobs = self.root / "runner" / "jobs"
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

    def enter(self, *, venues="both", job_writer=live.write_job_record):
        results = [
            self.tick(stream_payload(0, "100", "2", "100", "1000", venues=venues)),
            self.tick(
                stream_payload(
                    1, "105", "70", "7000", "1050",
                    spread_pct="0.008", venues=venues,
                )
            ),
            self.tick(
                stream_payload(
                    2, "108", "75", "7600", "1070",
                    spread_pct="0.007", venues=venues,
                ),
                job_writer=job_writer,
            ),
        ]
        return results

    def test_full_lifecycle_is_persisted_and_every_decision_has_evidence(self):
        results = self.enter()
        results.append(
            self.tick(
                stream_payload(
                    3, "120", "90", "9000", "1100",
                    spread_pct="0.006", oi_change_pct="3",
                )
            )
        )
        results.append(
            self.tick(
                stream_payload(
                    4, "121", "90", "9000", "1100",
                    spread_pct="0.006", oi_change_pct="1",
                )
            )
        )
        results.append(
            self.tick(
                stream_payload(
                    5, "116", "50", "5000", "1000",
                    spread_pct="0.009", oi_change_pct="-6",
                )
            )
        )
        results.append(
            self.tick(
                stream_payload(
                    6, "90", "30", "3000", "900",
                    spread_pct="0.009", oi_change_pct="-8",
                )
            )
        )

        actions = [result.decisions[0]["action"] for result in results]
        self.assertEqual(
            actions,
            ["WATCH", "WATCH", "ENTER_SMALL", "ADD", "TRAIL",
             "REDUCE_TRAIL", "EXIT"],
        )
        rows = self.decision_rows()
        self.assertEqual([row["action"] for row in rows], actions)
        self.assertEqual(len({row["decision_id"] for row in rows}), len(rows))
        for index, row in enumerate(rows):
            self.assertEqual(row["decision_utc"], FIXED_NOW)
            self.assertEqual(row["stream_utc"], f"2026-08-14T10:{index:02d}:00.000Z")
            self.assertIsNotNone(row["surge_ratio"])
            self.assertGreater(Decimal(row["price"]), 0)
            self.assertGreaterEqual(Decimal(row["vol_1m_quote"]), 0)
            self.assertGreaterEqual(Decimal(row["oi"]), 0)
            self.assertIn(row["price_venue"], {"spot", "perp"})
            self.assertIsInstance(row["lifecycle_state"], dict)
        self.assertEqual(rows[1]["ignition_utc"], "2026-08-14T10:01:00.000Z")
        self.assertEqual(rows[-1]["ignition_utc"], "2026-08-14T10:01:00.000Z")

        persisted = lifecycle.load_state(self.state)
        token = persisted.tokens["AAAUSDT"]
        self.assertEqual(token["phase"], lifecycle.PHASE_WATCH)
        self.assertEqual(token["qty"], "0")
        self.assertTrue(token["rearm_required"])

        paths = self.job_paths()
        self.assertEqual(len(paths), 1)
        job = json.loads(paths[0].read_text(encoding="utf-8"))
        self.assertEqual(set(job), set(live.JOB_FIELDS))
        self.assertEqual(job["op"], "MOMENTUM_ROUNDTRIP")
        self.assertEqual(job["symbol"], "AAAUSDT")
        self.assertEqual(job["max_notional"], 8)
        self.assertEqual(job["requested_by"], "momentum_live")
        self.assertEqual(
            job["job_id"],
            live.deterministic_job_id(
                "AAAUSDT", "2026-08-14T10:01:00.000Z"
            ),
        )
        self.assertEqual(job["created_utc"], FIXED_NOW)
        self.assertEqual(job["expires_utc"], "2026-08-14T12:10:00.000Z")
        accepted = runner._validate_job(
            runner._load_job(str(paths[0])),
            job["job_id"],
            datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(accepted.op, "MOMENTUM_ROUNDTRIP")
        self.assertEqual(accepted.effective_notional, Decimal("8"))

    def test_spot_only_enter_is_recorded_but_never_queued(self):
        results = self.enter(venues="spot")

        entry = results[-1].decisions[0]
        self.assertEqual(entry["action"], "ENTER_SMALL")
        self.assertFalse(entry["job_eligible"])
        self.assertEqual(entry["job_reason"], "token has no perpetual venue")
        self.assertIsNone(entry["job"])
        self.assertEqual(self.job_paths(), [])

    def test_entry_above_eight_is_recorded_but_never_queued(self):
        self.config = test_config(initial_notional=Decimal("9"))
        results = self.enter(venues="perp")

        entry = results[-1].decisions[0]
        self.assertEqual(entry["action"], "ENTER_SMALL")
        self.assertFalse(entry["job_eligible"])
        self.assertEqual(
            entry["job_reason"], "entry notional exceeds the runner cap"
        )
        self.assertEqual(self.job_paths(), [])

    def test_restart_after_enter_decision_recovers_exactly_one_job(self):
        self.tick(stream_payload(0, "100", "2", "100", "1000", venues="perp"))
        ignition = "2026-08-14T10:01:00.000Z"
        self.tick(
            stream_payload(
                1, "105", "70", "7000", "1050",
                spread_pct="0.008", venues="perp",
            )
        )

        def killed_between_decision_and_job(_jobs_dir, _payload):
            raise SimulatedKill("killed before runner job publication")

        entry_payload = stream_payload(
            2, "108", "75", "7600", "1070",
            spread_pct="0.007", venues="perp",
        )
        with self.assertRaises(SimulatedKill):
            self.tick(entry_payload, job_writer=killed_between_decision_and_job)

        self.assertEqual(self.job_paths(), [])
        entry_rows = [
            row for row in self.decision_rows()
            if row["action"] == lifecycle.ACTION_ENTER_SMALL
        ]
        self.assertEqual(len(entry_rows), 1)
        expected_id = live.deterministic_job_id("AAAUSDT", ignition)
        self.assertEqual(entry_rows[0]["job"]["job_id"], expected_id)

        recovered = self.tick(entry_payload)
        self.assertEqual(recovered.replayed_receipts, 1)
        self.assertEqual(recovered.recovered_job_ids, (expected_id,))
        self.assertEqual(recovered.decisions[0]["action"], "DUPLICATE")
        self.assertEqual(len(self.job_paths()), 1)

        replay = self.tick(
            entry_payload,
            now_fn=lambda: "2026-08-14T12:01:00.000Z",
        )
        self.assertEqual(replay.recovered_job_ids, ())
        self.assertEqual(replay.written_job_ids, ())
        self.assertEqual(len(self.job_paths()), 1)
        job = json.loads(self.job_paths()[0].read_text(encoding="utf-8"))
        self.assertEqual(job["job_id"], expected_id)

    def test_restart_recovers_if_killed_before_decision_append(self):
        self.tick(stream_payload(0, "100", "2", "100", "1000", venues="perp"))
        ignition = "2026-08-14T10:01:00.000Z"
        self.tick(
            stream_payload(
                1, "105", "70", "7000", "1050",
                spread_pct="0.008", venues="perp",
            )
        )
        entry_payload = stream_payload(
            2, "108", "75", "7600", "1070",
            spread_pct="0.007", venues="perp",
        )
        with mock.patch.object(
            live,
            "append_decision",
            side_effect=SimulatedKill("killed after lifecycle decision"),
        ):
            with self.assertRaises(SimulatedKill):
                self.tick(entry_payload)

        self.assertEqual(self.job_paths(), [])
        self.assertNotIn(
            lifecycle.ACTION_ENTER_SMALL,
            [row["action"] for row in self.decision_rows()],
        )

        expected_id = live.deterministic_job_id("AAAUSDT", ignition)
        recovered = self.tick(entry_payload)
        self.assertEqual(recovered.replayed_receipts, 1)
        self.assertEqual(recovered.recovered_job_ids, (expected_id,))
        self.assertEqual(recovered.decisions[0]["action"], "DUPLICATE")
        self.assertEqual(len(self.job_paths()), 1)
        entry_rows = [
            row for row in self.decision_rows()
            if row["action"] == lifecycle.ACTION_ENTER_SMALL
        ]
        self.assertEqual(len(entry_rows), 1)
        self.assertEqual(entry_rows[0]["job"]["job_id"], expected_id)

    def test_conflicting_deterministic_job_is_fail_closed(self):
        ignition = "2026-08-14T10:01:00.000Z"
        job_id = live.deterministic_job_id("AAAUSDT", ignition)
        payload = {
            "job_id": job_id,
            "op": live.JOB_OP,
            "symbol": "AAAUSDT",
            "max_notional": Decimal("8"),
            "created_utc": FIXED_NOW,
            "expires_utc": "2026-08-14T12:10:00.000Z",
            "requested_by": live.REQUESTED_BY,
        }
        self.assertTrue(live.write_job_record(self.jobs, payload))
        conflict = dict(payload, requested_by="different")
        with self.assertRaises(live.MomentumLiveError):
            live.write_job_record(self.jobs, conflict)
        self.assertEqual(len(self.job_paths()), 1)

    def test_cli_writes_only_artifacts_and_reports_counts(self):
        self.config = test_config()
        self.tick(stream_payload(0, "100", "2", "100", "1000"))
        self.tick(
            stream_payload(
                1, "105", "70", "7000", "1050", spread_pct="0.008"
            )
        )
        self.stream.write_text(
            json.dumps(
                {
                    "AAAUSDT": stream_payload(
                        2, "108", "75", "7600", "1070", spread_pct="0.007"
                    )
                }
            ),
            encoding="utf-8",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        args = [
            "--tick", str(self.stream),
            "--state", str(self.state),
            "--receipts", str(self.receipts),
            "--decisions", str(self.decisions),
            "--jobs-dir", str(self.jobs),
            "--initial-notional", "8",
        ]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = live.main(args)

        self.assertEqual(code, 0, stderr.getvalue())
        summary = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(summary["decisions"], 1)
        self.assertEqual(summary["jobs_written"], 1)
        self.assertEqual(len(self.job_paths()), 1)

    def test_runtime_caps_reject_non_decimal_or_oversize_values(self):
        self.stream.write_text("{}", encoding="utf-8")
        common = {
            "state_path": self.state,
            "receipt_path": self.receipts,
            "decisions_path": self.decisions,
            "jobs_dir": self.jobs,
            "now_fn": fixed_now,
        }
        for value in (Decimal("8.01"), Decimal("0"), None, True, 8, "8"):
            with self.subTest(value=value):
                with self.assertRaises(live.MomentumLiveError):
                    live.run_tick(
                        self.stream, max_job_notional=value, **common
                    )
        with self.assertRaises(live.MomentumLiveError):
            live.run_tick(
                self.stream,
                job_ttl=timedelta(minutes=16),
                **common,
            )


if __name__ == "__main__":
    unittest.main()
