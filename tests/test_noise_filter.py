#!/usr/bin/env python3
"""Offline fixtures for adaptive replay-ignition noise filtering."""

import json
import tempfile
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path


from study import baselines
from study import noise_filter


BASE_MS = 1_767_225_600_000
RUNNER = "RUNUSDT"
SPIKE = "SPIKEUSDT"
SPAM = "SPAMUSDT"
EVENTS = {
    RUNNER: (90,),
    SPIKE: (90,),
    SPAM: (20, 50, 80, 110),
}


def prices(symbol, index):
    if symbol == SPIKE and index == 90:
        return "100", "120", "80", "100"
    if index in EVENTS[symbol]:
        return "100", "101.1", "99.9", "101"
    return "100", "100.1", "99.9", "100"


def volume(symbol, index):
    for event in EVENTS[symbol]:
        if index == event:
            return "100" if symbol != SPAM else "60"
        if index in {event + 1, event + 2} and symbol != SPIKE:
            return "80" if symbol == RUNNER else "50"
    return "10"


def kline(symbol, index):
    open_ms = BASE_MS + index * 60_000
    close_ms = open_ms + 59_999
    open_price, high, low, close = prices(symbol, index)
    quote_volume = volume(symbol, index)
    return [
        str(open_ms),
        open_price,
        high,
        low,
        close,
        "1",
        str(close_ms),
        quote_volume,
        "1",
        "0.5",
        "5",
        "0",
    ]


def write_tape(root):
    data_dir = root / "data"
    for symbol in (RUNNER, SPIKE, SPAM):
        archive_dir = (
            data_dir / "spot" / "daily" / "klines" / symbol / "1m"
        )
        archive_dir.mkdir(parents=True)
        rows = [",".join(kline(symbol, index)) for index in range(180)]
        archive = archive_dir / f"{symbol}-1m-2026-01-01.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr(
                f"{symbol}-1m-2026-01-01.csv", "\n".join(rows) + "\n"
            )
    return data_dir


def ignition(symbol, index, move):
    utc = baselines.utc_text(BASE_MS + index * 60_000 + 59_999)
    return {
        "kind": "ignition",
        "symbol": symbol,
        "utc": utc,
        "surge_ratio": "10",
        "price_move_next_5m_pct": move[0],
        "price_move_next_15m_pct": move[1],
        "price_move_next_60m_pct": move[2],
    }


def write_replay(path):
    rows = [
        ignition(RUNNER, 90, ("5", "10", "20")),
        ignition(SPIKE, 90, ("-2", "-3", "-5")),
    ]
    rows.extend(
        ignition(SPAM, index, ("0", "0", "0"))
        for index in EVENTS[SPAM]
    )
    rows.append({"kind": "summary", "current_ignitions": 6})
    path.write_text(
        "".join(
            json.dumps(row, sort_keys=True) + "\n" for row in rows
        ),
        encoding="utf-8",
    )


class AdaptiveNoiseFilterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data_dir = write_tape(self.root)
        self.output_dir = self.root / "out"
        self.output_dir.mkdir()
        self.baseline_path = self.output_dir / "baselines.json"
        baselines.run_baselines(self.data_dir, self.baseline_path)
        self.replay_path = self.output_dir / "replay-2026-01-01.jsonl"
        write_replay(self.replay_path)

    def test_runner_kept_spike_and_serial_spammer_dropped(self):
        rows, output_path = noise_filter.run_filter(
            self.replay_path, self.baseline_path
        )

        self.assertEqual(
            output_path, self.output_dir / "filtered-2026-01-01.jsonl"
        )
        decisions = rows[:-1]
        runner = [row for row in decisions if row["symbol"] == RUNNER]
        spike = [row for row in decisions if row["symbol"] == SPIKE]
        spam = [row for row in decisions if row["symbol"] == SPAM]
        self.assertEqual(
            [(row["decision"], row["reason"]) for row in runner],
            [("keep", "sustained_runner")],
        )
        self.assertEqual(
            [(row["decision"], row["reason"]) for row in spike],
            [("drop", "one_print_spike")],
        )
        self.assertTrue(spam)
        self.assertTrue(
            all(
                row["decision"] == "drop"
                and row["reason"] == "serial_igniter"
                for row in spam
            )
        )
        self.assertGreater(
            Decimal(runner[0]["features"]["followthrough_magnitude"]),
            Decimal("1"),
        )
        self.assertGreater(
            Decimal(spike[0]["features"]["range_load"]),
            Decimal(spike[0]["features"]["volume_load"]),
        )

        summary = rows[-1]
        self.assertEqual(summary["kind"], "summary")
        self.assertEqual(summary["kept"], 1)
        self.assertEqual(summary["dropped"], 5)
        self.assertEqual(summary["kept_per_day"], "1")
        self.assertEqual(
            summary["forward_returns"]["kept"]["15m"]["median_pct"],
            "10",
        )
        disk = [
            json.loads(line)
            for line in output_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(list(rows), disk)


if __name__ == "__main__":
    unittest.main()
