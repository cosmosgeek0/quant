"""Offline pins for the thin leverage CLI."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import set_leverage_1x as cli
from execution import binance_futures as futures
from execution import binance_http as http

SYMBOL, SHA = "BTCUSDT", "1aa1e018740ae9dc012c1ebc58c4cf81a518df62"


def ok(payload):
    return http.HttpResult("ok", payload, None, None)


class Transport:
    def __init__(self, root, identity, leverages):
        self.path = os.path.join(root, "leverage-intent-" + identity + ".json")
        self.leverages, self.calls = list(leverages), []

    def __call__(self, method, base_url, path, params, signed):
        self.calls.append((method, path, params, signed))
        if method == "POST":
            if not os.path.isfile(self.path):
                raise AssertionError("durable intent must exist before POST")
            return ok({"leverage": 1})
        return ok([{"symbol": SYMBOL, "leverage": str(self.leverages.pop(0))}])


class SetLeverageCliTest(unittest.TestCase):
    def test_run_calls_adapter_once_with_synced_intent(self):
        with tempfile.TemporaryDirectory() as root:
            identity, real_fsync = "lev-cli-1", os.fsync

            def adapter(transport, base_url, symbol, mutation_id, sha, writer):
                self.assertIs(transport, mock.sentinel.transport)
                writer(http.MutationIntent("BINANCE_USDM", "SET_LEVERAGE",
                                           mutation_id, sha, (("symbol", symbol),
                                                              ("leverage", "1"))))
                return futures.LeverageResult("proven", 2, 1, True, None)

            writer = cli.durable_intent_writer(root, identity)
            with mock.patch.object(cli.futures, "set_leverage_1x_once", side_effect=adapter) as call, \
                    mock.patch.object(cli.os, "fsync", side_effect=real_fsync) as fsync, \
                    redirect_stdout(io.StringIO()):
                code = cli.run(SYMBOL, mock.sentinel.transport, identity, SHA, writer)
            self.assertEqual((code, call.call_count, fsync.call_count), (0, 1, 2))
            with open(os.path.join(root, "leverage-intent-" + identity + ".json"), encoding="utf-8") as handle:
                receipt = json.load(handle)
            self.assertEqual((receipt["identity"], receipt["operation"], receipt["git_sha"], receipt["params"][-1]),
                             (identity, "SET_LEVERAGE", SHA, ["leverage", "1"]))

    def test_adapter_posts_once_for_non_one_and_none_for_one(self):
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()):
            changed = Transport(root, "lev-cli-2", [5, 1])
            same = Transport(root, "lev-cli-3", [1])
            self.assertEqual(cli.run(SYMBOL, changed, "lev-cli-2", SHA,
                                     cli.durable_intent_writer(root, "lev-cli-2")), 0)
            self.assertEqual(cli.run(SYMBOL, same, "lev-cli-3", SHA,
                                     cli.durable_intent_writer(root, "lev-cli-3")), 0)
        self.assertEqual(sum(call[0] == "POST" for call in changed.calls), 1)
        self.assertEqual(sum(call[0] == "POST" for call in same.calls), 0)


if __name__ == "__main__":
    unittest.main()
