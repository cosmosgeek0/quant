#!/usr/bin/env python3
"""Offline fixture tests for the historical momentum replay."""

import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest import mock


from study import replay_detector as replay


BASE_MS = 1_767_225_600_000
SYMBOL = "AAAUSDT"
DAY = "2026-01-01"
FIXTURE_SYMBOL = json.loads(
    (Path(__file__).parent / "fixtures" / "stage_a_line197191_event.json")
    .read_text(encoding="utf-8")
)["symbol"]


def kline(index, close, quote_volume):
    open_ms = BASE_MS + (index * 60_000)
    close_ms = open_ms + 59_999
    return [
        str(open_ms),
        str(close),
        str(close),
        str(close),
        str(close),
        "1",
        str(close_ms),
        str(quote_volume),
        "1",
        "0.5",
        str(Decimal(str(quote_volume)) / Decimal("2")),
        "0",
    ]


def write_tape(root, *, surge):
    data_dir = root / "data"
    data_dir.mkdir()
    rows = []
    for index in range(301):
        close = "99"
        quote_volume = "100"
        if surge and index == 240:
            close = "100"
            quote_volume = "900"
        elif surge and index == 241:
            close = "101"
            quote_volume = "900"
        elif surge and index == 245:
            close = "110"
        elif surge and index == 255:
            close = "120"
        elif surge and index == 300:
            close = "160"
        rows.append(",".join(kline(index, close, quote_volume)))
    archive = data_dir / f"{SYMBOL}-1m-2026-01-01.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(
            f"{SYMBOL}-1m-2026-01-01.csv", "\n".join(rows) + "\n"
        )
    return data_dir


class ReplayDetectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "out" / "replay-2026-01-01.jsonl"
        self.config = replace(
            replay.momentum_lifecycle.LifecycleConfig(),
            ignition_surge_ratio=Decimal("3"),
        )

    def test_filename_symbols_use_shared_validation(self):
        self.assertEqual(
            replay._symbol_from_name(
                f"{FIXTURE_SYMBOL}-1m-{DAY}.zip"
            ),
            FIXTURE_SYMBOL,
        )
        self.assertEqual(
            replay._symbol_from_name(f"{SYMBOL}-1m-{DAY}.zip"),
            SYMBOL,
        )
        self.assertEqual(
            replay._symbol_from_name(f"aaausdt-1M-{DAY}.zip"),
            SYMBOL,
        )
        self.assertIsNone(
            replay._symbol_from_name(f"../{SYMBOL}-1m-{DAY}.zip")
        )

    def disk_rows(self):
        return [
            json.loads(line)
            for line in self.output.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_known_surge_emits_one_ignition_with_forward_returns(self):
        data_dir = write_tape(self.root, surge=True)
        returned = replay.run_replay(
            data_dir, self.output, config=self.config
        )
        rows = self.disk_rows()

        self.assertEqual(list(returned), rows)
        ignitions = [row for row in rows if row["kind"] == "ignition"]
        self.assertEqual(len(ignitions), 1)
        self.assertEqual(
            ignitions[0],
            {
                "kind": "ignition",
                "symbol": SYMBOL,
                "utc": "2026-01-01T04:00:59.999Z",
                "surge_ratio": "9",
                "price_move_next_5m_pct": "10.0",
                "price_move_next_15m_pct": "20.0",
                "price_move_next_60m_pct": "60.0",
            },
        )
        summary = rows[-1]
        self.assertEqual(summary["kind"], "summary")
        self.assertEqual(summary["comparison_basis"], "ignition_surge_ratio")
        self.assertEqual(summary["ignitions_per_day_bar_3"], "1")
        self.assertEqual(summary["ignitions_per_day_bar_10"], "0")
        self.assertEqual(summary["current_ignitions"], 1)
        self.assertEqual(
            sorted(path.name for path in self.output.parent.iterdir()),
            [self.output.name],
        )

    def test_no_surge_emits_only_zero_summary(self):
        data_dir = write_tape(self.root, surge=False)
        replay.run_replay(data_dir, self.output, config=self.config)
        rows = self.disk_rows()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "summary")
        self.assertEqual(rows[0]["current_ignitions"], 0)
        self.assertEqual(rows[0]["ignitions_bar_3"], 0)
        self.assertEqual(rows[0]["ignitions_per_day_bar_3"], "0")
        self.assertEqual(rows[0]["ignitions_bar_10"], 0)
        self.assertEqual(rows[0]["ignitions_per_day_bar_10"], "0")

    def test_archive_member_uses_shared_verified_reader(self):
        data_dir = write_tape(self.root, surge=False)
        shared_reader = replay.tape_pull.stream_verified_member

        with mock.patch.object(
            replay.tape_pull,
            "stream_verified_member",
            wraps=shared_reader,
        ) as verified_reader:
            tape = replay.load_tape(data_dir)

        self.assertEqual(len(tape[SYMBOL]), 301)
        archive, member, bound = verified_reader.call_args.args
        self.assertIsInstance(archive, zipfile.ZipFile)
        self.assertEqual(member.filename, f"{SYMBOL}-1m-{DAY}.csv")
        self.assertEqual(bound, replay.tape_pull.MAX_MEMBER_BYTES)

    def test_shared_ceiling_refusal_has_archive_and_member_identity(self):
        data_dir = write_tape(self.root, surge=False)
        ceiling = replay.tape_pull.MAX_MEMBER_BYTES
        shared_error = replay.tape_pull.ArchiveError(
            "archive member size is outside bounds",
            measured_value=ceiling + 1,
            bound=ceiling,
        )

        with mock.patch.object(
            replay.tape_pull,
            "stream_verified_member",
            side_effect=shared_error,
        ):
            with self.assertRaises(replay.ReplayError) as raised:
                replay.load_tape(data_dir)

        identity = f"{SYMBOL}-1m-{DAY}.zip:{SYMBOL}-1m-{DAY}.csv"
        message = str(raised.exception)
        self.assertIn(identity, message)
        self.assertIn("archive member size is outside bounds", message)
        self.assertIn(f"measured={ceiling + 1}", message)
        self.assertIn(f"bound={ceiling}", message)


if __name__ == "__main__":
    unittest.main()
