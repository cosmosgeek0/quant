"""Deterministic offline tests for bounded Margin operation builders."""

import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import margin_ops  # noqa: E402
import margin_reconcile  # noqa: E402


NOW = datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)
EXPIRY = "2026-08-15T10:10:00Z"
ASSET = "USDT"
INTENT_ID = "mrgintent-" + ("a" * 24)


def job(op=margin_ops.MARGIN_BORROW, **changes):
    row = {
        "op": op,
        "asset": ASSET,
        "principal": "3",
        "intent_id": INTENT_ID,
        "expires_utc": EXPIRY,
        "operator_token": "owner-approved-token",
    }
    explicit_tag = changes.pop("client_tag", None)
    row.update(changes)
    if explicit_tag is None:
        row["client_tag"] = margin_ops.deterministic_client_tag(
            row["op"], row["asset"], row["principal"], row["expires_utc"]
        )
    else:
        row["client_tag"] = explicit_tag
    return row


class MarginOperationTests(unittest.TestCase):
    def test_hard_clamp_constants_are_exact(self):
        self.assertEqual(margin_ops.PRINCIPAL_CEILING, Decimal("3"))
        self.assertEqual(margin_ops.MAX_EXPIRY_AHEAD,
                         timedelta(minutes=15))
        self.assertEqual(
            margin_ops.MARGIN_OPERATIONS,
            frozenset(("MARGIN_BORROW", "MARGIN_REPAY")),
        )

    def test_borrow_request_and_ledger_shapes_are_exact(self):
        row = margin_ops.build_margin_borrow(job(), {ASSET}, NOW)
        expected_tag = margin_ops.deterministic_client_tag(
            "MARGIN_BORROW", ASSET, "3", EXPIRY
        )
        self.assertEqual(row, {
            "verdict": "READY",
            "op": "MARGIN_BORROW",
            "client_tag": expected_tag,
            "intent_id": INTENT_ID,
            "expires_utc": EXPIRY,
            "endpoint_path": "/sapi/v1/margin/borrow-repay",
            "params": {"asset": ASSET, "amount": "3", "type": "BORROW"},
            "expected_ledger_confirmation": {
                "asset": ASSET, "principal": "3", "interest": "0",
                "status": "BORROW_CONFIRMED",
            },
            "evidence": [
                "job.op", "asset_whitelist.single", "job.asset",
                "job.principal", "principal_ceiling=3", "job.expires_utc",
                "job.operator_token.present", "job.intent_id",
                "job.client_tag",
            ],
        })
        self.assertNotIn("owner-approved-token", repr(row))

    def test_repay_request_and_ledger_shapes_are_exact(self):
        value = job(margin_ops.MARGIN_REPAY, principal="2.1250")
        row = margin_ops.build_margin_repay(value, ASSET, NOW)
        self.assertEqual(row["params"], {
            "asset": ASSET, "amount": "2.125", "type": "REPAY",
        })
        self.assertEqual(row["expected_ledger_confirmation"], {
            "asset": ASSET, "principal": "2.125", "interest": "0",
            "status": "REPAY_CONFIRMED",
        })

    def test_expected_borrow_confirmation_is_consumed_by_reconciler(self):
        built = margin_ops.build_margin_borrow(job(), {ASSET}, NOW)
        rows = margin_reconcile.reconcile_margin(
            [{
                "asset": ASSET, "free": "3", "locked": "0",
                "borrowed": "3", "interest": "0", "netAsset": "0",
            }],
            [],
            [built["expected_ledger_confirmation"]],
        )
        self.assertEqual(rows[0]["verdict"], "LIABILITY_OPEN")
        self.assertEqual(rows[0]["total_liability"], "3")

    def test_dispatcher_accepts_only_the_two_named_operations(self):
        for op in (margin_ops.MARGIN_BORROW, margin_ops.MARGIN_REPAY):
            with self.subTest(op=op):
                self.assertEqual(
                    margin_ops.build_margin_operation(job(op), {ASSET}, NOW)[
                        "verdict"
                    ],
                    "READY",
                )
        unsupported = dict(job())
        unsupported["op"] = "MARGIN_TRANSFER"
        row = margin_ops.build_margin_operation(unsupported, {ASSET}, NOW)
        self.assertEqual((row["verdict"], row["reason"]),
                         ("REFUSED", "BAD_OP"))

    def test_operation_specific_builder_refuses_the_other_operation(self):
        row = margin_ops.build_margin_borrow(
            job(margin_ops.MARGIN_REPAY), {ASSET}, NOW
        )
        self.assertEqual((row["verdict"], row["reason"]),
                         ("REFUSED", "OP_MISMATCH"))

    def test_principal_ceiling_accepts_three_and_refuses_above_three(self):
        self.assertEqual(
            margin_ops.build_margin_operation(job(principal="3.000"),
                                              {ASSET}, NOW)["verdict"],
            "READY",
        )
        above = job()
        above["principal"] = "3.0000001"
        row = margin_ops.build_margin_operation(above, {ASSET}, NOW)
        self.assertEqual((row["verdict"], row["reason"]),
                         ("REFUSED", "PRINCIPAL_CEILING_EXCEEDED"))

    def test_nonpositive_and_malformed_principals_refuse(self):
        values = ("0", "0.0", "-1", "+1", "1e0", "NaN", "Infinity", "")
        for principal in values:
            with self.subTest(principal=principal):
                value = job()
                value["principal"] = principal
                row = margin_ops.build_margin_operation(value, {ASSET}, NOW)
                self.assertEqual((row["verdict"], row["reason"]),
                                 ("REFUSED", "BAD_PRINCIPAL"))

    def test_principal_must_be_a_string(self):
        value = job()
        value["principal"] = Decimal("1")
        row = margin_ops.build_margin_operation(value, {ASSET}, NOW)
        self.assertEqual((row["verdict"], row["reason"]),
                         ("REFUSED", "BAD_PRINCIPAL"))

    def test_single_asset_whitelist_is_enforced(self):
        cases = (
            (set(), "BAD_ASSET_WHITELIST"),
            ({"USDT", "BTC"}, "BAD_ASSET_WHITELIST"),
            ({"usdt"}, "BAD_ASSET_WHITELIST"),
            ({"BTC"}, "ASSET_NOT_ALLOWED"),
            (None, "BAD_ASSET_WHITELIST"),
        )
        for whitelist, reason in cases:
            with self.subTest(whitelist=whitelist):
                row = margin_ops.build_margin_operation(job(), whitelist, NOW)
                self.assertEqual((row["verdict"], row["reason"]),
                                 ("REFUSED", reason))

    def test_asset_must_be_well_formed(self):
        for asset in ("usdt", "", "USDT-USD", None):
            with self.subTest(asset=asset):
                value = job()
                value["asset"] = asset
                row = margin_ops.build_margin_operation(value, {ASSET}, NOW)
                self.assertEqual((row["verdict"], row["reason"]),
                                 ("REFUSED", "BAD_ASSET"))

    def test_every_trading_or_leverage_field_refuses(self):
        for field in sorted(margin_ops.FORBIDDEN_JOB_FIELDS):
            with self.subTest(field=field):
                value = job()
                value[field] = "present"
                row = margin_ops.build_margin_operation(value, {ASSET}, NOW)
                self.assertEqual((row["verdict"], row["reason"]),
                                 ("REFUSED", "FORBIDDEN_FIELD"))
                self.assertEqual(row["fields"], ["job." + field])

    def test_unknown_and_non_text_fields_refuse(self):
        value = job()
        value["extra"] = "value"
        row = margin_ops.build_margin_operation(value, {ASSET}, NOW)
        self.assertEqual((row["verdict"], row["reason"]),
                         ("REFUSED", "UNKNOWN_FIELD"))
        value = job()
        value[1] = "value"
        row = margin_ops.build_margin_operation(value, {ASSET}, NOW)
        self.assertEqual((row["verdict"], row["reason"]),
                         ("REFUSED", "BAD_FIELD_NAME"))

    def test_each_required_field_is_required(self):
        for field in sorted(margin_ops.JOB_FIELDS):
            with self.subTest(field=field):
                value = job()
                value.pop(field)
                row = margin_ops.build_margin_operation(value, {ASSET}, NOW)
                self.assertEqual((row["verdict"], row["reason"]),
                                 ("REFUSED", "MISSING_FIELD"))
                self.assertIn("job." + field, row["fields"])

    def test_client_tag_is_stable_and_binds_operation_facts(self):
        first = margin_ops.deterministic_client_tag(
            "MARGIN_BORROW", ASSET, "3.00", EXPIRY
        )
        second = margin_ops.deterministic_client_tag(
            "MARGIN_BORROW", ASSET, "3", EXPIRY
        )
        changed = margin_ops.deterministic_client_tag(
            "MARGIN_REPAY", ASSET, "3", EXPIRY
        )
        changed_asset = margin_ops.deterministic_client_tag(
            "MARGIN_BORROW", "BTC", "3", EXPIRY
        )
        changed_principal = margin_ops.deterministic_client_tag(
            "MARGIN_BORROW", ASSET, "2", EXPIRY
        )
        changed_expiry = margin_ops.deterministic_client_tag(
            "MARGIN_BORROW", ASSET, "3", "2026-08-15T10:10:00.000001Z"
        )
        self.assertEqual(first, second)
        for candidate in (
                changed, changed_asset, changed_principal, changed_expiry):
            self.assertNotEqual(first, candidate)
        self.assertRegex(first, re.compile(r"^mrg-[0-9a-f]{24}$"))
        value = job(client_tag="mrg-" + ("0" * 24))
        row = margin_ops.build_margin_operation(value, {ASSET}, NOW)
        self.assertEqual((row["verdict"], row["reason"]),
                         ("REFUSED", "CLIENT_TAG_MISMATCH"))

    def test_intent_id_shape_is_explicit_and_strict(self):
        ready = margin_ops.build_margin_operation(job(), {ASSET}, NOW)
        self.assertEqual(ready["intent_id"], INTENT_ID)
        self.assertRegex(INTENT_ID, re.compile(r"^mrgintent-[0-9a-f]{24}$"))
        malformed = (
            None,
            "mrgintent-" + ("A" * 24),
            "mrgintent-" + ("a" * 23),
            "intent-" + ("a" * 24),
        )
        for value in malformed:
            with self.subTest(value=value):
                row = margin_ops.build_margin_operation(
                    job(intent_id=value), {ASSET}, NOW
                )
                self.assertEqual(
                    (row["verdict"], row["reason"]),
                    ("REFUSED", "BAD_INTENT_ID"),
                )

    def test_repeated_build_is_identical(self):
        value = job()
        first = margin_ops.build_margin_operation(value, {ASSET}, NOW)
        second = margin_ops.build_margin_operation(value, {ASSET}, NOW)
        self.assertEqual(first, second)

    def test_expiry_boundary_and_expired_values(self):
        at_limit = job(expires_utc="2026-08-15T10:15:00Z")
        self.assertEqual(
            margin_ops.build_margin_operation(at_limit, {ASSET}, NOW)[
                "verdict"
            ],
            "READY",
        )
        for expires in ("2026-08-15T10:00:00Z",
                        "2026-08-15T09:59:59Z"):
            with self.subTest(expires=expires):
                row = margin_ops.build_margin_operation(
                    job(expires_utc=expires), {ASSET}, NOW
                )
                self.assertEqual((row["verdict"], row["reason"]),
                                 ("REFUSED", "EXPIRED"))

    def test_expiry_comparison_preserves_microseconds(self):
        expiry = "2026-08-15T10:00:00.500000Z"
        before = datetime(
            2026, 8, 15, 10, 0, 0, 499999, tzinfo=timezone.utc
        )
        at_expiry = datetime(
            2026, 8, 15, 10, 0, 0, 500000, tzinfo=timezone.utc
        )
        self.assertEqual(
            margin_ops.build_margin_operation(
                job(expires_utc=expiry), {ASSET}, before
            )["verdict"],
            "READY",
        )
        refused = margin_ops.build_margin_operation(
            job(expires_utc=expiry), {ASSET}, at_expiry
        )
        self.assertEqual(
            (refused["verdict"], refused["reason"]),
            ("REFUSED", "EXPIRED"),
        )

    def test_expiry_too_far_or_malformed_refuses(self):
        too_far = job(expires_utc="2026-08-15T10:15:00.000001Z")
        row = margin_ops.build_margin_operation(too_far, {ASSET}, NOW)
        self.assertEqual((row["verdict"], row["reason"]),
                         ("REFUSED", "EXPIRY_WINDOW_EXCEEDED"))
        for expires in ("2026-08-15T10:10:00+00:00", "soon", None):
            with self.subTest(expires=expires):
                value = job()
                value["expires_utc"] = expires
                row = margin_ops.build_margin_operation(value, {ASSET}, NOW)
                self.assertEqual((row["verdict"], row["reason"]),
                                 ("REFUSED", "BAD_EXPIRY"))

    def test_now_must_be_timezone_aware(self):
        row = margin_ops.build_margin_operation(
            job(), {ASSET}, datetime(2026, 8, 15, 10, 0)
        )
        self.assertEqual((row["verdict"], row["reason"]),
                         ("REFUSED", "BAD_NOW"))

    def test_operator_token_must_be_present_and_bounded(self):
        for token in ("", "   ", None, "x" * 257):
            with self.subTest(token_type=type(token).__name__):
                value = job()
                value["operator_token"] = token
                row = margin_ops.build_margin_operation(value, {ASSET}, NOW)
                self.assertEqual((row["verdict"], row["reason"]),
                                 ("REFUSED", "BAD_OPERATOR_TOKEN"))
        value = job(operator_token="x" * 256)
        self.assertEqual(
            margin_ops.build_margin_operation(value, {ASSET}, NOW)["verdict"],
            "READY",
        )

    def test_non_mapping_job_refuses(self):
        row = margin_ops.build_margin_operation([], {ASSET}, NOW)
        self.assertEqual(row, {
            "verdict": "REFUSED", "op": "", "reason": "BAD_JOB",
            "fields": ["job"], "evidence": [],
        })


if __name__ == "__main__":
    unittest.main()
