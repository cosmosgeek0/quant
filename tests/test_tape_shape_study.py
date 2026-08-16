#!/usr/bin/env python3
"""Schema and refusal tests for the tape-shape archive reader."""

import tempfile
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path
from unittest import mock


from study import tape_shape_study as shape


SYMBOL = "AAAUSDT"
DAY = "2026-01-01"
STAMP_MS = 1_767_225_600_000


def write_tape(root):
    path = root / f"{SYMBOL}-aggTrades-{DAY}.zip"
    member = path.stem + ".csv"
    row = f"1,2.5,3,1,1,{STAMP_MS},false,true\n"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, row)
    return path


class TapeShapeStudyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_trade_row_schema_uses_shared_verified_reader(self):
        path = write_tape(self.root)
        shared_reader = shape.tape_pull.stream_verified_member

        with mock.patch.object(
            shape.tape_pull,
            "stream_verified_member",
            wraps=shared_reader,
        ) as verified_reader:
            rows = list(shape._trade_rows(path))

        self.assertEqual(
            rows,
            [(1, STAMP_MS * 1_000, Decimal("2.5"), Decimal("3"), False)],
        )
        archive, member, bound = verified_reader.call_args.args
        self.assertIsInstance(archive, zipfile.ZipFile)
        self.assertEqual(member.filename, path.stem + ".csv")
        self.assertEqual(bound, shape.tape_pull.MAX_MEMBER_BYTES)

    def test_shared_ceiling_refusal_has_archive_and_member_identity(self):
        path = write_tape(self.root)
        ceiling = shape.tape_pull.MAX_MEMBER_BYTES
        shared_error = shape.tape_pull.ArchiveError(
            "archive member size is outside bounds",
            measured_value=ceiling + 1,
            bound=ceiling,
        )

        with mock.patch.object(
            shape.tape_pull,
            "stream_verified_member",
            side_effect=shared_error,
        ):
            with self.assertRaises(shape.StudyError) as raised:
                list(shape._trade_rows(path))

        message = str(raised.exception)
        self.assertIn(f"{path.name}:{path.stem}.csv", message)
        self.assertIn("archive member size is outside bounds", message)
        self.assertIn(f"measured={ceiling + 1}", message)
        self.assertIn(f"bound={ceiling}", message)


if __name__ == "__main__":
    unittest.main()
