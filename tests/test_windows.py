#!/usr/bin/env python3
"""Offline synthetic tests for Volume Dataset V2 window matching."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


from study import windows


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def utc(day, hour=0):
    value = BASE + timedelta(days=day, hours=hour)
    return value.isoformat().replace("+00:00", "Z")


def event(
    event_id,
    symbol,
    day,
    label,
    venue_class,
    dead_regime,
    volume_percentile,
    *,
    hour=0,
    blocks_controls=None,
):
    timestamp = BASE + timedelta(days=day, hours=hour)
    open_ms = int(timestamp.timestamp()) * 1_000
    row = {
        "event_id": event_id,
        "symbol": symbol,
        "open_ms": open_ms,
        "utc": utc(day, hour),
        "label": label,
        "labels": [label],
        "venue_class": venue_class,
        "dead_regime": dead_regime,
        "pre_state": {"volume_percentile": str(volume_percentile)},
    }
    if blocks_controls is not None:
        row["blocks_controls"] = blocks_controls
    return row


def parsed(rows):
    return tuple(
        windows.parse_event(row, line_number)
        for line_number, row in enumerate(rows, start=1)
    )


class WindowManifestTests(unittest.TestCase):
    def test_matching_constraints_and_exact_window_bounds(self):
        rows = [
            event(
                "runner-spot",
                "RUNSPOT",
                2,
                "GenuineRunner",
                "spot_only",
                True,
                "50",
            ),
            event(
                "runner-futures",
                "RUNFUT",
                3,
                "GenuineRunner",
                "futures_only",
                False,
                "80",
            ),
            event(
                "spot-good-band-edge",
                "CTLSPOT",
                10,
                "FalseIgnition",
                "spot_only",
                True,
                "60",
            ),
            event(
                "futures-good-band-edge",
                "CTLFUT",
                10,
                "FalseIgnition",
                "futures_only",
                False,
                "70",
            ),
            event(
                "wrong-venue",
                "WRONGVENUE",
                11,
                "FalseIgnition",
                "both",
                True,
                "50",
            ),
            event(
                "wrong-regime",
                "WRONGREGIME",
                11,
                "FalseIgnition",
                "spot_only",
                False,
                "50",
            ),
            event(
                "outside-band",
                "OUTSIDEBAND",
                11,
                "FalseIgnition",
                "spot_only",
                True,
                "60.0001",
            ),
            event(
                "near-event-control",
                "NEARUSDT",
                20,
                "Control",
                "spot_only",
                True,
                "50",
                blocks_controls=False,
            ),
            event(
                "nearby-event",
                "NEARUSDT",
                20,
                "Ignition",
                "spot_only",
                True,
                "50",
                hour=23,
                blocks_controls=True,
            ),
        ]
        manifest = windows.build_windows_manifest(parsed(rows))

        self.assertEqual(manifest["counts"]["runner_events"], 2)
        self.assertEqual(manifest["counts"]["controls_matched"], 2)
        self.assertEqual(
            manifest["counts"]["control_candidates_excluded_near_event"],
            1,
        )
        pairs = {
            match["runner_event_id"]: match["control_event_id"]
            for match in manifest["matches"]
        }
        self.assertEqual(
            pairs,
            {
                "runner-spot": "spot-good-band-edge",
                "runner-futures": "futures-good-band-edge",
            },
        )
        self.assertNotIn("near-event-control", pairs.values())
        self.assertNotIn("wrong-venue", pairs.values())
        self.assertNotIn("wrong-regime", pairs.values())
        self.assertNotIn("outside-band", pairs.values())

        runner_window = next(
            row
            for row in manifest["windows"]
            if row["source_event_id"] == "runner-spot"
        )
        self.assertEqual(
            runner_window["anchor_ms"] - runner_window["start_ms"],
            60 * 60_000,
        )
        self.assertEqual(
            runner_window["end_ms"] - runner_window["anchor_ms"],
            120 * 60_000,
        )

    def test_maximum_matching_reassigns_instead_of_claiming_shortfall(self):
        rows = [
            event(
                "runner-50",
                "RUNNER50",
                1,
                "GenuineRunner",
                "both",
                True,
                "50",
            ),
            event(
                "runner-60",
                "RUNNER60",
                2,
                "GenuineRunner",
                "both",
                True,
                "60",
            ),
            event(
                "control-50",
                "CONTROL50",
                10,
                "FalseIgnition",
                "both",
                True,
                "50",
            ),
            event(
                "control-40",
                "CONTROL40",
                11,
                "FalseIgnition",
                "both",
                True,
                "40",
            ),
        ]
        manifest = windows.build_windows_manifest(parsed(rows))

        self.assertEqual(manifest["counts"]["controls_matched"], 2)
        self.assertEqual(manifest["counts"]["control_shortfall"], 0)
        self.assertEqual(
            {match["control_event_id"] for match in manifest["matches"]},
            {"control-50", "control-40"},
        )

    def test_runner_cannot_match_itself(self):
        rows = [
            event(
                "only-runner",
                "SELFRUNNER",
                1,
                "GenuineRunner",
                "spot_only",
                True,
                "55",
            )
        ]
        manifest = windows.build_windows_manifest(parsed(rows))

        self.assertEqual(manifest["counts"]["controls_matched"], 0)
        self.assertEqual(manifest["counts"]["control_shortfall"], 1)
        self.assertEqual(manifest["matches"], [])
        self.assertEqual(len(manifest["windows"]), 1)
        self.assertEqual(manifest["windows"][0]["kind"], "runner")

    def test_shortfall_is_counted_and_output_matches_return_value(self):
        rows = [
            event(
                "runner-a",
                "RUNNERA",
                1,
                "GenuineRunner",
                "spot_only",
                True,
                "50",
            ),
            event(
                "runner-b",
                "RUNNERB",
                2,
                "GenuineRunner",
                "spot_only",
                True,
                "51",
            ),
            event(
                "one-control",
                "CONTROL",
                10,
                "FalseIgnition",
                "spot_only",
                True,
                "50",
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events_path = root / "events.jsonl"
            output_path = root / "windows_manifest.json"
            events_path.write_text(
                "".join(
                    json.dumps(row, sort_keys=True) + "\n" for row in rows
                ),
                encoding="utf-8",
            )
            payload = windows.run_windows(events_path, output_path)
            on_disk = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(payload, on_disk)
        self.assertEqual(payload["counts"]["controls_requested"], 2)
        self.assertEqual(payload["counts"]["controls_matched"], 1)
        self.assertEqual(payload["counts"]["control_shortfall"], 1)
        self.assertEqual(payload["shortfall_counts"]["total"], 1)
        self.assertEqual(
            payload["shortfall_counts"]["by_venue_class"]["spot_only"],
            1,
        )
        self.assertEqual(len(payload["unmatched_runners"]), 1)
        control_ids = [
            row["source_event_id"]
            for row in payload["windows"]
            if row["kind"] == "control"
        ]
        self.assertEqual(control_ids, ["one-control"])
        self.assertIsInstance(
            payload["matches"][0]["volume_percentile_delta"], str
        )


if __name__ == "__main__":
    unittest.main()
