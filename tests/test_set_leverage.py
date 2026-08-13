"""Deterministic offline tests for src/set_leverage_1x.py — no network."""

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from set_leverage_1x import run  # noqa: E402

SYMBOL = "BNBUSDT"


class Transport:
    def __init__(self, reads, post=("ok", {})):
        self.reads = list(reads)
        self.post = post
        self.posts = 0
        self.calls = []

    def __call__(self, method, base_url, path, params=None, signed=False):
        self.calls.append((method, path, dict(params or {}), signed))
        if method == "POST":
            self.posts += 1
            return self.post
        return self.reads.pop(0)


def config(leverage):
    return ("ok", [{"symbol": SYMBOL, "leverage": str(leverage)}])


def drive(transport):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = run(SYMBOL, transport)
    return code, buffer.getvalue()


class SetLeverageTests(unittest.TestCase):
    def test_already_1x_makes_no_mutation(self):
        transport = Transport([config(1)])
        code, out = drive(transport)
        self.assertEqual(code, 0)
        self.assertEqual(transport.posts, 0)
        self.assertIn("no mutation", out)

    def test_change_is_one_post_proven_by_reread(self):
        transport = Transport([config(10), config(1)])
        code, out = drive(transport)
        self.assertEqual(code, 0)
        self.assertEqual(transport.posts, 1)
        method, path, params, signed = transport.calls[1]
        self.assertEqual((method, path), ("POST", "/fapi/v1/leverage"))
        self.assertEqual(params["leverage"], "1")
        self.assertTrue(signed)
        self.assertIn("changed 10x -> 1x", out)

    def test_failed_post_exits_1_without_retry(self):
        transport = Transport([config(5)], post=("api_error", {"code": -1}))
        code, out = drive(transport)
        self.assertEqual(code, 1)
        self.assertEqual(transport.posts, 1)
        self.assertIn("no retry", out)

    def test_unproven_reread_exits_1(self):
        transport = Transport([config(5), config(5)])
        code, out = drive(transport)
        self.assertEqual(code, 1)
        self.assertIn("UNPROVEN", out)

    def test_unreadable_config_refuses_before_any_post(self):
        transport = Transport([("unclear", {})])
        code, _ = drive(transport)
        self.assertEqual(code, 1)
        self.assertEqual(transport.posts, 0)


if __name__ == "__main__":
    unittest.main()
