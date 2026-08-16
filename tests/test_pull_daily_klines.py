#!/usr/bin/env python3
"""Focused tests for the daily kline archive reader."""

import tempfile
import unittest
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from study import pull_daily_klines as daily


class DailyKlineReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.job = daily.Job("BTCUSDT", "spot", "daily", "2026-08-14")
        self.path = self.root / self.job.filename()
        payload = b"1786665600000,1,2,0.5,1.5,0,1786751999999,0\n"
        with zipfile.ZipFile(self.path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(self.job.member(), payload)

    def tearDown(self):
        self.temporary.cleanup()

    def test_verify_and_rows_use_shared_reader_with_small_ceiling(self):
        shared_reader = daily.tape_pull.stream_verified_member
        with mock.patch.object(
            daily.tape_pull, "stream_verified_member", wraps=shared_reader
        ) as verified_reader:
            size = daily.verify(self.job, self.path)
            rows = list(daily.archive_rows(self.job, self.path))

        self.assertEqual(size, self.path.stat().st_size)
        self.assertEqual(
            rows,
            [(date(2026, 8, 14), Decimal("2"), Decimal("0.5"), Decimal("1.5"))],
        )
        self.assertEqual(verified_reader.call_count, 2)
        for call in verified_reader.call_args_list:
            self.assertEqual(call.args[1].filename, self.job.member())
            self.assertEqual(call.args[2], daily.MAX_BYTES)

    def test_shared_member_ceiling_maps_to_local_result(self):
        shared_error = daily.tape_pull.ArchiveError(
            "archive member size is outside bounds",
            measured_value=daily.MAX_BYTES + 1,
            bound=daily.MAX_BYTES,
        )
        with mock.patch.object(
            daily.tape_pull, "stream_verified_member", side_effect=shared_error
        ) as verified_reader:
            with self.assertRaises(daily.Refused) as raised:
                daily.verify(self.job, self.path)

        self.assertEqual(raised.exception.measured, daily.MAX_BYTES + 1)
        self.assertEqual(verified_reader.call_args.args[2], daily.MAX_BYTES)


if __name__ == "__main__":
    unittest.main()
