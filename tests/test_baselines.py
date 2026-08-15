#!/usr/bin/env python3
"""Offline fixture tests for adaptive per-token tape baselines."""

import json
import tempfile
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path


from study import baselines


BASE_MS = 1_767_225_600_000
SYMBOL = "AAAUSDT"


def kline(index, volume):
    open_ms = BASE_MS + index * 60_000
    close_ms = open_ms + 59_999
    return [
        str(open_ms),
        "100",
        "100.1",
        "99.9",
        "100",
        "1",
        str(close_ms),
        str(volume),
        "1",
        "0.5",
        str(Decimal(str(volume)) / Decimal("2")),
        "0",
    ]


def write_tape(root):
    data_dir = root / "data"
    archive_dir = data_dir / "spot" / "daily" / "klines" / SYMBOL / "1m"
    archive_dir.mkdir(parents=True)
    rows = [
        ",".join(kline(index, "10" if index < 60 else "20"))
        for index in range(120)
    ]
    archive = archive_dir / f"{SYMBOL}-1m-2026-01-01.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(
            f"{SYMBOL}-1m-2026-01-01.csv", "\n".join(rows) + "\n"
        )
    return data_dir


def assert_no_float(test, value):
    test.assertNotIsInstance(value, float)
    if isinstance(value, dict):
        for nested in value.values():
            assert_no_float(test, nested)
    elif isinstance(value, list):
        for nested in value:
            assert_no_float(test, nested)


class AdaptiveBaselineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data_dir = write_tape(self.root)
        self.output_dir = self.root / "out"

    def test_synthetic_tape_derives_token_regimes_and_seasonality(self):
        output = self.output_dir / "baselines.json"
        payload = baselines.run_baselines(self.data_dir, output)
        disk = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload, disk)
        self.assertEqual(payload["schema"], "adaptive-volume-baselines-v1")
        self.assertEqual(payload["windows_minutes"], [1, 10, 15, 60])
        token = payload["symbols"][SYMBOL]
        one_minute = token["rolling_volume_regimes"]["1m"]
        self.assertEqual(one_minute["quiet_floor"], "10")
        self.assertEqual(one_minute["typical"], "15")
        self.assertEqual(one_minute["hot"], "20")
        self.assertEqual(
            token["ignition_threshold"]["quiet_floor_multiple"], "2"
        )
        self.assertEqual(
            token["time_of_day_utc"]["00"]["ignition_quote_volume"],
            "20",
        )
        self.assertEqual(
            token["time_of_day_utc"]["01"]["ignition_quote_volume"],
            "40",
        )
        self.assertEqual(
            token["rolling_volume_regimes"]["60m"]["sample_count"], 61
        )
        assert_no_float(self, disk)

    def test_derivation_and_serialization_are_deterministic(self):
        first_path = self.output_dir / "first.json"
        second_path = self.output_dir / "second.json"
        first = baselines.run_baselines(self.data_dir, first_path)
        second = baselines.run_baselines(self.data_dir, second_path)

        self.assertEqual(first, second)
        self.assertEqual(
            first_path.read_bytes(), second_path.read_bytes()
        )
        self.assertEqual(first["source"]["data_dir"], "../data")


if __name__ == "__main__":
    unittest.main()
