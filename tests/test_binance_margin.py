"""Offline contract tests for the shared Binance Margin adapter."""
import ast
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path
import sys
import unittest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import margin_ops  # noqa: E402
from execution import binance_margin as margin  # noqa: E402
from execution import binance_http as http  # noqa: E402

SHA = "a" * 40
BASE = "https://example.invalid"


def ok(payload):
    return http.HttpResult("ok", payload, None, None)


def history(tx_id=17, direction="REPAY", **changes):
    row = {"txId": tx_id, "asset": "USDT", "amount": "2.5000387",
           "principal": "2.5", "interest": "0.0000387",
           "status": "CONFIRMED", "type": "MANUAL",
           "timestamp": 1700000000000}
    if direction == "BORROW":
        row.update(amount="2.5", interest="0")
    row.update(changes)
    return row


class Feed:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, method, base_url, path, params, signed):
        self.calls.append((method, base_url, path, params, signed))
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


class MarginAdapterTests(unittest.TestCase):
    def test_source_keeps_one_statement_per_line_within_readable_cap(self):
        source = (SRC / "execution" / "binance_margin.py").read_text(encoding="utf-8")
        statement_lines = [
            node.lineno for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.stmt)]
        self.assertEqual(len(statement_lines), len(set(statement_lines)))
        self.assertLessEqual(sum(bool(line.strip()) for line in source.splitlines()), 191)

    def test_liability_and_repay_keep_the_exact_interest_split(self):
        with localcontext() as context:
            context.prec = 3
            liability = margin.Liability(
                "USDT", Decimal("2.5"), Decimal("0.0000387"), Decimal("2.5000387"))
            operation = margin.build_repay(liability, "intent-1", SHA)
        self.assertEqual(operation.amount, Decimal("2.5000387"))
        self.assertEqual(operation.principal, Decimal("2.5"))
        self.assertEqual(operation.interest, Decimal("0.0000387"))
        self.assertEqual(operation.intent.params,
                         (("asset", "USDT"), ("amount", "2.5000387"),
                          ("type", "REPAY")))
        with self.assertRaises(http.AdapterRefusal):
            margin.Liability("USDT", Decimal("2.5"), None, Decimal("2.5"))
        with self.assertRaises(http.AdapterRefusal):
            margin.Liability("USDT", Decimal("2.5"), Decimal("0.1"), Decimal("2.5"))

    def test_borrow_uses_the_owned_principal_cap(self):
        self.assertIs(margin._PRINCIPAL_CEILING, margin_ops.PRINCIPAL_CEILING)
        plan = margin.build_borrow("USDT", margin_ops.PRINCIPAL_CEILING,
                                   "intent-2", SHA)
        self.assertEqual(plan.amount, margin_ops.PRINCIPAL_CEILING)
        self.assertEqual((plan.intent.venue, plan.intent.operation),
                         ("BINANCE_MARGIN", "BORROW"))
        with self.assertRaises(http.AdapterRefusal):
            margin.build_borrow("USDT", margin_ops.PRINCIPAL_CEILING + Decimal("0.1"),
                                "intent-3", SHA)
        with self.assertRaises(http.AdapterRefusal):
            margin.build_borrow("BTC", Decimal("1"), "intent-4", SHA)

    def test_submit_refuses_direct_over_cap_before_effects(self):
        amount = margin_ops.PRINCIPAL_CEILING + Decimal("0.1")
        operation = margin.build_borrow(
            "USDT", margin_ops.PRINCIPAL_CEILING, "intent-cap", SHA)
        params = (("asset", "USDT"), ("amount", str(amount)), ("type", "BORROW"))
        operation = replace(operation, amount=amount, principal=amount,
                            intent=replace(operation.intent, params=params))
        feed, writes = Feed(ok({"tranId": 7})), []
        with self.assertRaises(http.AdapterRefusal):
            margin.submit_once(feed, BASE, operation, writes.append)
        self.assertEqual((writes, feed.calls), ([], []))

    def test_history_direction_comes_from_the_request(self):
        for direction in ("BORROW", "REPAY"):
            with self.subTest(direction=direction):
                feed = Feed(ok({"total": 1, "rows": [history(direction=direction)]}))
                result = margin.read_history_by_direction(
                    feed, BASE, "USDT", direction, sleep=lambda _delay: None)
                self.assertEqual(result.kind, "ok")
                self.assertEqual(result.value[0].direction, direction)
                self.assertEqual(result.value[0].origin_type, "MANUAL")
                self.assertEqual(dict(feed.calls[0][3])["type"], direction)
                self.assertIs(feed.calls[0][4], True)

    def test_history_non_text_origin_is_unclear_after_one_signed_read(self):
        feed = Feed(ok({"total": 1, "rows": [history(type=[])]}))
        result = margin.read_history_by_direction(
            feed, BASE, "USDT", "REPAY", sleep=lambda _delay: None)
        self.assertEqual((result.kind, result.value, len(feed.calls)),
                         ("unclear", None, 1))
        self.assertIs(feed.calls[0][4], True)

    def test_exact_id_absence_is_ok_while_conflicts_are_unclear(self):
        empty = margin.read_history_by_tx_id(
            Feed(ok({"total": 0, "rows": []})), BASE, "USDT", "BORROW", 17)
        self.assertEqual((empty.kind, empty.value), ("ok", None))
        for rows in ([history(18, "BORROW")],
                     [history(17, "BORROW"), history(17, "BORROW")]):
            with self.subTest(rows=len(rows)):
                result = margin.read_history_by_tx_id(
                    Feed(ok({"total": len(rows), "rows": rows})),
                    BASE, "USDT", "BORROW", 17)
                self.assertEqual(result.kind, "unclear")

    def test_exact_id_requires_total_to_match_returned_rows(self):
        feed = Feed(ok({"total": 1, "rows": []}))
        result = margin.read_history_by_tx_id(
            feed, BASE, "USDT", "BORROW", 17, sleep=lambda _delay: None)
        self.assertEqual((result.kind, result.value, len(feed.calls)),
                         ("unclear", None, 1))

    def test_await_retries_only_valid_absence(self):
        visible = Feed(ok({"total": 0, "rows": []}),
                       ok({"total": 0, "rows": []}),
                       ok({"total": 1, "rows": [history()]}))
        waits = []
        result = margin.await_history_by_tx_id(
            visible, BASE, "USDT", "REPAY", 17, sleep=waits.append)
        self.assertEqual(result.kind, "ok")
        self.assertEqual((len(visible.calls), waits), (3, [1, 2]))

        malformed = Feed(ok({"total": 0, "rows": []}), ok({"rows": "soft"}))
        waits = []
        result = margin.await_history_by_tx_id(
            malformed, BASE, "USDT", "REPAY", 17, sleep=waits.append)
        self.assertEqual(result.kind, "unclear")
        self.assertEqual((len(malformed.calls), waits), (2, [1]))

        absent = Feed(*[ok({"total": 0, "rows": []}) for _ in range(4)])
        waits = []
        result = margin.await_history_by_tx_id(
            absent, BASE, "USDT", "REPAY", 17, sleep=waits.append)
        self.assertEqual((result.kind, len(absent.calls), waits),
                         ("unclear", 4, [1, 2, 4]))

    def test_liability_read_proves_decimal_total_and_unique_asset(self):
        feed = Feed(ok({"userAssets": [
            {"asset": "USDT", "borrowed": "2.5", "interest": "0.0000387"}]}))
        result = margin.read_liability(feed, BASE, "USDT")
        self.assertEqual(result.value.total, Decimal("2.5000387"))
        self.assertEqual(feed.calls[0][2:],
                         ("/sapi/v1/margin/account", None, True))
        duplicate = {"userAssets": [
            {"asset": "USDT", "borrowed": "1", "interest": "0"},
            {"asset": "USDT", "borrowed": "1", "interest": "0"}]}
        self.assertEqual(margin.read_liability(Feed(ok(duplicate)), BASE, "USDT").kind,
                         "unclear")

    def test_liability_extreme_exponent_is_unclear(self):
        payload = {"userAssets": [
            {"asset": "USDT", "borrowed": "1e1000000", "interest": "0"}]}
        result = margin.read_liability(Feed(ok(payload)), BASE, "USDT")
        self.assertEqual((result.kind, result.value), ("unclear", None))

    def test_submit_writes_first_and_posts_once_with_integer_identity(self):
        operation = margin.build_borrow("USDT", Decimal("2.5"), "intent-5", SHA)
        events = []

        def transport(method, base_url, path, params, signed):
            events.append(("wire", method, path, params, signed))
            return ok({"tranId": 401344857108})

        result = margin.submit_once(
            transport, BASE, operation, lambda intent: events.append(("intent", intent)))
        self.assertEqual((result.kind, result.tx_id), ("accepted", 401344857108))
        self.assertEqual(events[0][0], "intent")
        self.assertEqual(sum(event[0] == "wire" for event in events), 1)
        self.assertEqual(events[1][1:3],
                         ("POST", "/sapi/v1/margin/borrow-repay"))
        self.assertNotIn("clientTag", dict(events[1][3]))

    def test_submit_keeps_unclear_identity_and_writer_failure_safe(self):
        operation = margin.build_borrow("USDT", Decimal("1"), "intent-6", SHA)
        for payload in ({"tranId": "7"}, {"txId": True},
                        {"tranId": 7, "txId": 7}):
            with self.subTest(payload=payload):
                feed = Feed(ok(payload))
                result = margin.submit_once(feed, BASE, operation, lambda _intent: None)
                self.assertEqual((result.kind, result.tx_id, len(feed.calls)),
                                 ("unclear", None, 1))
        feed = Feed(ok({"tranId": 7}))
        with self.assertRaises(http.IntentNotDurable):
            margin.submit_once(
                feed, BASE, operation,
                lambda _intent: (_ for _ in ()).throw(RuntimeError("write unavailable")))
        self.assertEqual(feed.calls, [])

    def test_submit_keeps_nested_only_identity_unclear(self):
        operation = margin.build_borrow("USDT", Decimal("1"), "intent-nested", SHA)
        response = ok({"detail": {"txId": 7}})
        result = margin.submit_once(Feed(response), BASE, operation, lambda _intent: None)
        self.assertEqual((result.kind, result.tx_id), ("unclear", None))
        self.assertIs(result.error, response)

    def test_confirmation_binds_history_split_and_liability_movement(self):
        zero = margin.Liability("USDT", Decimal("0"), Decimal("0"), Decimal("0"))
        borrowed = margin.Liability("USDT", Decimal("2.5"), Decimal("0"), Decimal("2.5"))
        borrow = margin.build_borrow("USDT", Decimal("2.5"), "intent-7", SHA)
        borrow_row = margin.MarginHistoryRow(
            "BORROW", 17, "USDT", Decimal("2.5"), Decimal("2.5"),
            Decimal("0"), "CONFIRMED", "MANUAL", 1700000000000)
        self.assertEqual(margin.confirm(borrow, 17, borrow_row, zero, borrowed).kind,
                         "confirmed")
        for field, value in (
                ("direction", "REPAY"),
                ("tx_id", 18),
                ("asset", "BTC"),
                ("status", "PENDING"),
                ("origin_type", "SYSTEM"),
                ("principal", Decimal("2.4")),
                ("interest", Decimal("0.1"))):
            with self.subTest(field=field):
                changed = replace(borrow_row, **{field: value})
                self.assertEqual(
                    margin.confirm(borrow, 17, changed, zero, borrowed).kind,
                    "unknown")

        before = margin.Liability(
            "USDT", Decimal("2.5"), Decimal("0.0000387"), Decimal("2.5000387"))
        repay = margin.build_repay(before, "intent-8", SHA)
        repay_row = margin.MarginHistoryRow(
            "REPAY", 18, "USDT", Decimal("2.5000387"), Decimal("2.5"),
            Decimal("0.0000387"), "CONFIRMED", "MANUAL", 1700000000001)
        self.assertEqual(margin.confirm(repay, 18, repay_row, before, zero).kind,
                         "confirmed")
        self.assertEqual(margin.confirm(
            repay, 18, replace(repay_row, amount=Decimal("2.5000386")),
            before, zero).kind, "unknown")
        self.assertEqual(margin.confirm(repay, 18, repay_row, before, borrowed).kind,
                         "unknown")

    def test_confirmation_requires_plain_integer_history_identity(self):
        zero = margin.Liability("USDT", Decimal("0"), Decimal("0"), Decimal("0"))
        borrowed = margin.Liability(
            "USDT", Decimal("2.5"), Decimal("0"), Decimal("2.5"))
        operation = margin.build_borrow("USDT", Decimal("2.5"), "intent-9", SHA)
        row = margin.MarginHistoryRow(
            "BORROW", Decimal("17"), "USDT", Decimal("2.5"), Decimal("2.5"),
            Decimal("0"), "CONFIRMED", "MANUAL", 1700000000000)
        self.assertEqual(margin.confirm(operation, 17, row, zero, borrowed).kind,
                         "unknown")


if __name__ == "__main__":
    unittest.main()
