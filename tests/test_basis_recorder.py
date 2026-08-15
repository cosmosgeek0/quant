#!/usr/bin/env python3
"""Offline tests for the local basis-history recorder."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import basis_recorder as recorder


FIXED_UTC = "2026-08-15T06:00:00.000Z"


def stream_record(venues="both", **updates):
    row = {
        "utc": FIXED_UTC,
        "venues": venues,
        "spot_bid": "99.9" if venues in {"spot", "both"} else None,
        "spot_ask": "100.0" if venues in {"spot", "both"} else None,
        "perp_bid": "100.2" if venues in {"perp", "both"} else None,
        "perp_ask": "100.3" if venues in {"perp", "both"} else None,
        "mark": "100.2500" if venues in {"perp", "both"} else None,
        "oi": "12345.6700" if venues in {"perp", "both"} else None,
        "funding_rate": "0.000100" if venues in {"perp", "both"} else None,
        "next_funding_ms": "1786771200000"
        if venues in {"perp", "both"}
        else None,
        "basis_gap_pct": "0.200" if venues == "both" else None,
    }
    row.update(updates)
    return row


class BasisRecorderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.history = self.root / recorder.HISTORY_FILENAME

    def read_rows(self, path=None):
        selected = self.history if path is None else Path(path)
        return [
            json.loads(line)
            for line in selected.read_text(encoding="utf-8").splitlines()
        ]

    def test_rows_are_built_from_stream_fixture_for_every_perpetual(self):
        rows = recorder.build_rows(
            {
                "SPOTUSDT": stream_record("spot"),
                "PERPUSDT": stream_record("perp"),
                "BOTHUSDT": stream_record("both"),
            }
        )

        self.assertEqual(
            [row["symbol"] for row in rows], ["BOTHUSDT", "PERPUSDT"]
        )
        both, perp = rows
        self.assertEqual(both["status"], recorder.STATUS_RECORDED)
        self.assertEqual(both["funding_rate"], "0.000100")
        self.assertEqual(both["next_funding_ms"], "1786771200000")
        self.assertEqual(both["basis_gap_pct"], "0.200")
        self.assertEqual(both["oi"], "12345.6700")
        self.assertEqual(both["mark"], "100.2500")
        self.assertEqual(both["unavailable"], [])
        self.assertEqual(perp["status"], recorder.STATUS_SKIPPED)
        self.assertEqual(perp["unavailable"], ["basis_gap_pct"])
        for row in rows:
            for field in recorder.METRIC_FIELDS:
                self.assertTrue(
                    row[field] is None or isinstance(row[field], str),
                    (row["symbol"], field, row[field]),
                )

    def test_absent_or_invalid_input_is_an_honest_skip_not_zero(self):
        row = recorder.build_rows(
            {
                "MISSUSDT": stream_record(
                    oi=None, mark="nan", funding_rate=0
                )
            }
        )[0]

        self.assertEqual(row["status"], recorder.STATUS_SKIPPED)
        self.assertEqual(row["funding_rate"], None)
        self.assertEqual(row["oi"], None)
        self.assertEqual(row["mark"], None)
        self.assertEqual(
            row["unavailable"], ["funding_rate", "oi", "mark"]
        )
        self.assertNotIn(0, row.values())

    def test_record_pass_appends_jsonl_without_any_external_reader(self):
        state_path = self.root / "stream_state.json"
        state_path.write_text(
            json.dumps({"BOTHUSDT": stream_record()}), encoding="utf-8"
        )

        first = recorder.record_pass(state_path, self.root)
        second = recorder.record_pass(state_path, self.root)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, first)
        self.assertEqual(self.read_rows(), first + second)

    def test_rotation_stages_complete_segments_and_survives_interruption(self):
        first = recorder.build_rows({"FIRSTUSDT": stream_record()})[0]
        second = recorder.build_rows({"SECONDUSDT": stream_record()})[0]
        recorder.append_history(self.history, [first])
        original = self.history.read_bytes()
        incoming = recorder._encode_rows([second])
        threshold = len(original) + len(incoming) - 1
        original_replace = recorder.os.replace
        staged = []

        def interrupt_before_current(source, target):
            if Path(target) == self.history:
                payload = Path(source).read_bytes()
                self.assertTrue(payload.endswith(b"\n"))
                self.assertEqual(
                    [json.loads(line) for line in payload.splitlines()], [second]
                )
                staged.append(True)
                raise OSError("simulated interruption")
            return original_replace(source, target)

        with mock.patch.object(
            recorder.os, "replace", side_effect=interrupt_before_current
        ):
            with self.assertRaises(recorder.BasisRecorderError):
                recorder.append_history(
                    self.history, [second], rotate_bytes=threshold
                )

        self.assertEqual(staged, [True])
        self.assertEqual(self.history.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".basis_history.jsonl.*.tmp")), [])

        recorder.append_history(self.history, [second], rotate_bytes=threshold)
        backup = self.history.with_name(self.history.name + ".1")
        self.assertEqual(self.read_rows(backup), [first])
        self.assertEqual(self.read_rows(), [second])
        self.assertLessEqual(self.history.stat().st_size, threshold)
        self.assertLessEqual(backup.stat().st_size, threshold)

    def test_runtime_stage_is_once_after_stream_and_outside_seals(self):
        source = (
            Path(__file__).resolve().parents[1] / "scripts/runtime_tick.sh"
        ).read_text(encoding="utf-8")

        self.assertEqual(source.count("run_stage basis_recorder"), 1)
        recorder_at = source.index("run_stage basis_recorder")
        self.assertGreater(recorder_at, source.index('run_stage stream "0"'))
        self.assertLess(
            recorder_at,
            source.index(
                'if [ -e "$STOP_FILE" ] || [ -e "$STOP_JOBS_FILE" ]; then'
            ),
        )
        block = source[recorder_at:source.index("\n\n", recorder_at)]
        self.assertIn('"$DEPLOY_DIR/src/basis_recorder.py"', block)
        self.assertIn('--stream-state "$STREAM_STATE"', block)
        self.assertIn('--receipt-dir "$RECEIPT_DIR"', block)


if __name__ == "__main__":
    unittest.main()
