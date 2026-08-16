"""Offline contract tests for the shared Binance Transfer adapter."""
from decimal import Decimal
from pathlib import Path
import sys
import unittest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import limits  # noqa: E402
from execution import binance_http as http  # noqa: E402
from execution import binance_transfer as transfer  # noqa: E402

SHA = "b" * 40
BASE = "https://spot.invalid"
FUTURES = "https://futures.invalid"


def result(kind, payload=None, code=None):
    return http.HttpResult(kind, payload, code, None)


def row(tran_id, amount="2", asset="USDT"):
    return {"type": "MAIN_UMFUTURE", "asset": asset, "amount": amount,
            "tranId": tran_id, "status": "CONFIRMED", "timestamp": 1700000000000}


class Feed:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, method, base_url, path, params, signed):
        self.calls.append((method, base_url, path, params, signed))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


class TransferAdapterTests(unittest.TestCase):
    def test_builder_maps_directions_and_uses_the_owned_cap(self):
        self.assertIs(transfer._PER_LEG_CAP_USDT, limits.PER_LEG_CAP_USDT)
        expected = {"spot_to_usdm": "MAIN_UMFUTURE",
                    "usdm_to_spot": "UMFUTURE_MAIN"}
        for direction, transfer_type in expected.items():
            intent = transfer.build_transfer(
                direction, "USDT", limits.PER_LEG_CAP_USDT, "l3wrequest1", SHA)
            self.assertEqual(intent.params,
                             (("type", transfer_type), ("asset", "USDT"),
                              ("amount", "32")))
            self.assertEqual((intent.venue, intent.operation),
                             ("BINANCE_TRANSFER", "TRANSFER"))
        with self.assertRaises(http.AdapterRefusal):
            transfer.build_transfer("spot_to_usdm", "USDT",
                                    limits.PER_LEG_CAP_USDT + Decimal("0.1"),
                                    "l3wrequest2", SHA)

    def test_balance_read_requires_one_row_from_each_account(self):
        feed = Feed(
            result("ok", {"balances": [{"asset": "USDT", "free": "8.25"}]}),
            result("ok", [{"asset": "USDT", "availableBalance": "7.5"}]))
        read = transfer.read_balances(feed, BASE, FUTURES, "USDT")
        self.assertEqual(read.value, transfer.TransferBalances(
            "USDT", Decimal("8.25"), Decimal("7.5")))
        self.assertEqual([(call[1], call[2], call[4]) for call in feed.calls], [
            (BASE, "/api/v3/account", True),
            (FUTURES, "/fapi/v3/balance", True)])
        missing = transfer.read_balances(
            Feed(result("ok", {"balances": []})), BASE, FUTURES, "USDT")
        self.assertEqual((missing.kind, missing.value), ("unclear", None))

    def test_history_accepts_total_only_empty_and_uses_bounded_params(self):
        feed = Feed(result("ok", {"total": 0}))
        read = transfer.read_transfer_history(
            feed, BASE, "MAIN_UMFUTURE", 10, 20)
        self.assertEqual(read.value, ())
        self.assertEqual(feed.calls[0][2:], (
            "/sapi/v1/asset/transfer",
            (("type", "MAIN_UMFUTURE"), ("startTime", "10"),
             ("endTime", "20"), ("current", "1"), ("size", "100")), True))

    def test_history_stops_at_twenty_pages(self):
        calls = []

        def transport(method, base_url, path, params, signed):
            page = int(dict(params)["current"])
            calls.append(page)
            rows = [row((page - 1) * 100 + offset + 1) for offset in range(100)]
            return result("ok", {"total": 2001, "rows": rows})

        read = transfer.read_transfer_history(
            transport, BASE, "MAIN_UMFUTURE", 0, 1)
        self.assertEqual((read.kind, len(calls), calls[-1]), ("unclear", 20, 20))

    def test_submit_writes_first_posts_once_and_preserves_tran_id(self):
        intent = transfer.build_transfer(
            "spot_to_usdm", "USDT", Decimal("2"), "l3wrequest3", SHA)
        events = []

        def transport(method, base_url, path, params, signed):
            events.append(("wire", method, path, params, signed))
            return result("ok", {"tranId": 321})

        submitted = transfer.submit_transfer_once(
            transport, BASE, intent, lambda value: events.append(("intent", value)))
        self.assertEqual((submitted.kind, submitted.tran_id), ("accepted", 321))
        self.assertEqual(events[0][0], "intent")
        self.assertEqual(sum(event[0] == "wire" for event in events), 1)
        self.assertEqual(events[1][1:3],
                         ("POST", "/sapi/v1/asset/transfer"))
        self.assertNotIn("request_id", dict(events[1][3]))
        feed = Feed(result("ok", {"tranId": "321"}))
        self.assertEqual(transfer.submit_transfer_once(
            feed, BASE, intent, lambda _intent: None).kind, "unclear")
        self.assertEqual(len(feed.calls), 1)

    def test_submit_writer_failure_sends_no_request(self):
        intent = transfer.build_transfer(
            "spot_to_usdm", "USDT", Decimal("2"), "l3wrequest4", SHA)
        feed = Feed(result("ok", {"tranId": 1}))
        with self.assertRaises(http.IntentNotDurable):
            transfer.submit_transfer_once(
                feed, BASE, intent,
                lambda _intent: (_ for _ in ()).throw(RuntimeError("write unavailable")))
        self.assertEqual(feed.calls, [])

    def test_query_keeps_exact_identity_and_new_row_ambiguity(self):
        intent = transfer.build_transfer(
            "spot_to_usdm", "USDT", Decimal("2"), "l3wrequest5", SHA)
        exact = Feed(result("ok", {"total": 3, "rows": [
            row(40, asset="BTC"), row(41), row(42)]}))
        found = transfer.query_transfer(
            exact, BASE, intent, frozenset(), 42, 0, 1)
        self.assertEqual((found.kind, found.row.tran_id), ("found", 42))

        ambiguous = Feed(result("ok", {"total": 2, "rows": [row(51), row(52)]}))
        query = transfer.query_transfer(
            ambiguous, BASE, intent, frozenset(), None, 0, 1)
        self.assertEqual((query.kind, query.row), ("ambiguous", None))

        unique = Feed(result("ok", {"total": 2, "rows": [row(51), row(52)]}))
        query = transfer.query_transfer(
            unique, BASE, intent, frozenset({51}), None, 0, 1)
        self.assertEqual((query.kind, query.row.tran_id), ("found", 52))

    def test_query_has_eight_rounds_and_two_second_waits(self):
        intent = transfer.build_transfer(
            "spot_to_usdm", "USDT", Decimal("2"), "l3wrequest6", SHA)
        feed = Feed(*[result("ok", {"total": 0}) for _ in range(8)])
        waits = []
        query = transfer.query_transfer(
            feed, BASE, intent, frozenset(), None, 0, 1, sleep=waits.append)
        self.assertEqual((query.kind, len(feed.calls), waits),
                         ("not_found", 8, [2] * 7))


if __name__ == "__main__":
    unittest.main()
