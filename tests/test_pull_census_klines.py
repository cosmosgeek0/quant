#!/usr/bin/env python3
"""Focused tests for the census kline archive verifier."""

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from study import pull_census_klines as census_pull


class CensusKlineReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.job = census_pull.Job("BTCUSDT", "spot", "2026-02")
        self.path = self.root / self.job.filename()
        with zipfile.ZipFile(self.path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(self.job.member(), b"1,1,2,0.5,1.5,0,59999,10\n")

    def tearDown(self):
        self.temporary.cleanup()

    def test_verify_uses_shared_reader_with_monthly_ceiling(self):
        shared_reader = census_pull.tape_pull.stream_verified_member
        with mock.patch.object(
            census_pull.tape_pull, "stream_verified_member", wraps=shared_reader
        ) as verified_reader:
            size = census_pull._verify(self.job, self.path)

        self.assertEqual(size, self.path.stat().st_size)
        archive, member, bound = verified_reader.call_args.args
        self.assertIsInstance(archive, zipfile.ZipFile)
        self.assertEqual(member.filename, self.job.member())
        self.assertEqual(bound, census_pull.MAX_BYTES)

    def test_shared_member_ceiling_maps_to_local_result(self):
        shared_error = census_pull.tape_pull.ArchiveError(
            "archive member size is outside bounds",
            measured_value=census_pull.MAX_BYTES + 1,
            bound=census_pull.MAX_BYTES,
        )
        with mock.patch.object(
            census_pull.tape_pull, "stream_verified_member", side_effect=shared_error
        ) as verified_reader:
            with self.assertRaises(census_pull.Refused) as raised:
                census_pull._verify(self.job, self.path)

        self.assertEqual(raised.exception.args[0], census_pull.MAX_BYTES + 1)
        self.assertEqual(verified_reader.call_args.args[2], census_pull.MAX_BYTES)


if __name__ == "__main__":
    unittest.main()
