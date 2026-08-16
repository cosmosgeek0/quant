#!/usr/bin/env python3
"""Deterministic offline tests for executable touch gaps and history."""

import sys
import unittest
from decimal import Decimal
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import executable_gap as gap  # noqa: E402


SYMBOL = "TESTUSDT"
NOW_MS = 1_800_000_000_000
HOUR_MS = 3_600_000


def stream_record(**updates):
    record = {
        "spot_bid": "100",
        "spot_ask": "101",
        "perp_bid": "111.1",
        "perp_ask": "112",
        "mark": "999999",
    }
    record.update(updates)
    return record


def read(record=None, *, spot_age=100, perp_age=200, **kwargs):
    spot, perp = gap.quotes_from_stream_record(
        stream_record() if record is None else record,
        spot_observed_ms=NOW_MS - spot_age,
        perp_observed_ms=NOW_MS - perp_age,
    )
    return gap.read_executable_gap(SYMBOL, spot, perp, NOW_MS, **kwargs)


def reading(at_ms, entry, exit_gap=None):
    entry_value = Decimal(str(entry))
    exit_value = entry_value + Decimal("100") \
        if exit_gap is None else Decimal(str(exit_gap))
    return gap.ExecutableGapReading(
        SYMBOL, at_ms, entry_value, exit_value, 0, 0)


class ExecutableGapLawTests(unittest.TestCase):
    def test_stream_touch_shape_uses_owner_entry_and_exit_laws(self):
        result = read()

        self.assertIsInstance(result, gap.ExecutableGapReading)
        self.assertEqual(result.entry_gap_pct, Decimal("10"))
        self.assertEqual(result.exit_gap_pct, Decimal("12"))
        self.assertEqual((result.spot_age_ms, result.perp_age_ms), (100, 200))
        self.assertEqual(result.verdict, "READY")
        self.assertTrue(result.skew_checked)
        self.assertNotEqual(result.entry_gap_pct, Decimal("999999"))
        self.assertTrue(all(isinstance(value, Decimal) for value in (
            result.entry_gap_pct, result.exit_gap_pct)))

    def test_quote_at_age_bound_is_fresh_and_older_quote_refuses(self):
        accepted = read(spot_age=gap.DEFAULT_MAX_QUOTE_AGE_MS,
                        perp_age=gap.DEFAULT_MAX_QUOTE_AGE_MS)
        refused = read(spot_age=gap.DEFAULT_MAX_QUOTE_AGE_MS + 1,
                       perp_age=gap.DEFAULT_MAX_QUOTE_AGE_MS)

        self.assertIsInstance(accepted, gap.ExecutableGapReading)
        self.assertIsInstance(refused, gap.GapRefusal)
        self.assertEqual(refused.reason, "SPOT_QUOTE_STALE")
        self.assertEqual(refused.spot_age_ms, 5001)
        self.assertFalse(hasattr(refused, "entry_gap_pct"))

    def test_crossed_or_locked_book_refuses_without_gap_values(self):
        cases = (
            (stream_record(spot_bid="101", spot_ask="101"),
             "SPOT_BOOK_CROSSED_OR_LOCKED"),
            (stream_record(spot_bid="102", spot_ask="101"),
             "SPOT_BOOK_CROSSED_OR_LOCKED"),
            (stream_record(perp_bid="112", perp_ask="112"),
             "PERP_BOOK_CROSSED_OR_LOCKED"),
            (stream_record(perp_bid="113", perp_ask="112"),
             "PERP_BOOK_CROSSED_OR_LOCKED"),
        )
        for record, reason in cases:
            with self.subTest(reason=reason, record=record):
                result = read(record)
                self.assertIsInstance(result, gap.GapRefusal)
                self.assertEqual(result.reason, reason)
                self.assertFalse(hasattr(result, "exit_gap_pct"))

    def test_excess_pair_skew_and_float_price_refuse(self):
        skewed = read(spot_age=0, perp_age=1001)
        floating = read(stream_record(spot_bid=100.0))

        self.assertEqual(skewed.reason, "QUOTE_SKEW_EXCEEDED")
        self.assertTrue(skewed.skew_checked)
        self.assertEqual(floating.reason, "SPOT_QUOTE_INVALID")
        self.assertEqual((skewed.spot_age_ms, skewed.perp_age_ms), (0, 1001))

    def test_shared_observation_records_that_skew_was_not_checked(self):
        result = gap.read_executable_gap_shared_observation(
            SYMBOL, stream_record(), NOW_MS - 100, NOW_MS)

        self.assertIsInstance(result, gap.ExecutableGapReading)
        self.assertFalse(result.skew_checked)
        self.assertEqual((result.spot_age_ms, result.perp_age_ms), (100, 100))

    def test_equal_caller_times_do_not_claim_per_venue_skew_proof(self):
        spot, perp = gap.quotes_from_stream_record(
            stream_record(),
            spot_observed_ms=NOW_MS - 100,
            perp_observed_ms=NOW_MS - 100,
        )

        result = gap.read_executable_gap(SYMBOL, spot, perp, NOW_MS)

        self.assertIsInstance(result, gap.ExecutableGapReading)
        self.assertFalse(result.skew_checked)
        self.assertIs(spot.source, perp.source)

    def test_typed_venue_quotes_use_source_object_provenance(self):
        shared_source = object()
        spot = gap.VenueQuote(
            "spot", "100", "101", NOW_MS - 100, shared_source)
        shared_perp = gap.VenueQuote(
            "perp", "111.1", "112", NOW_MS - 100, shared_source)
        shared_perp_later = gap.VenueQuote(
            "perp", "111.1", "112", NOW_MS - 200, shared_source)
        independent_perp = gap.VenueQuote(
            "perp", "111.1", "112", NOW_MS - 100, object())
        spot_tag = "venue-feed"
        perp_tag = "".join(("venue", "-feed"))
        tagged_spot = gap.VenueQuote(
            "spot", "100", "101", NOW_MS - 100, spot_tag)
        tagged_perp = gap.VenueQuote(
            "perp", "111.1", "112", NOW_MS - 200, perp_tag)

        shared = gap.read_executable_gap(
            SYMBOL, spot, shared_perp, NOW_MS)
        shared_later = gap.read_executable_gap(
            SYMBOL, spot, shared_perp_later, NOW_MS)
        independent = gap.read_executable_gap(
            SYMBOL, spot, independent_perp, NOW_MS)
        tagged = gap.read_executable_gap(
            SYMBOL, tagged_spot, tagged_perp, NOW_MS)

        self.assertIsInstance(shared, gap.ExecutableGapReading)
        self.assertIsInstance(shared_later, gap.ExecutableGapReading)
        self.assertIsInstance(independent, gap.ExecutableGapReading)
        self.assertIsInstance(tagged, gap.ExecutableGapReading)
        self.assertFalse(shared.skew_checked)
        self.assertFalse(shared_later.skew_checked)
        self.assertTrue(independent.skew_checked)
        self.assertEqual(spot_tag, perp_tag)
        self.assertIsNot(spot_tag, perp_tag)
        self.assertFalse(tagged.skew_checked)

    def test_legacy_quote_pair_never_claims_a_skew_check(self):
        spot = gap.TimedQuote("100", "101", NOW_MS - 100)
        perp = gap.TimedQuote("111.1", "112", NOW_MS - 200)

        result = gap.read_executable_gap(SYMBOL, spot, perp, NOW_MS)

        self.assertIsInstance(result, gap.ExecutableGapReading)
        self.assertFalse(result.skew_checked)

    def test_distinct_venue_sources_enforce_the_skew_bound(self):
        spot = gap.VenueQuote(
            "spot", "100", "101", NOW_MS, object())
        perp = gap.VenueQuote(
            "perp", "111.1", "112",
            NOW_MS - gap.DEFAULT_MAX_QUOTE_SKEW_MS - 1,
            object(),
        )

        result = gap.read_executable_gap(SYMBOL, spot, perp, NOW_MS)

        self.assertIsInstance(result, gap.GapRefusal)
        self.assertEqual(result.reason, "QUOTE_SKEW_EXCEEDED")
        self.assertTrue(result.skew_checked)

    def test_reverse_dislocation_with_excess_skew_refuses(self):
        result = read(
            stream_record(
                spot_bid="110", spot_ask="111",
                perp_bid="99", perp_ask="100",
            ),
            spot_age=0,
            perp_age=gap.DEFAULT_MAX_QUOTE_SKEW_MS + 1,
        )

        self.assertIsInstance(result, gap.GapRefusal)
        self.assertEqual(result.reason, "QUOTE_SKEW_EXCEEDED")
        self.assertTrue(result.skew_checked)

    def test_reverse_dislocation_stays_normal_signed(self):
        """src/reverse_rung_intent.py recomputes the reverse capture."""
        result = read(stream_record(
            spot_bid="110", spot_ask="111", perp_bid="99", perp_ask="100"))

        self.assertIsInstance(result, gap.ExecutableGapReading)
        self.assertEqual(
            result.entry_gap_pct,
            Decimal("-10.810810810810810810810810810810810810810810810811"),
        )
        self.assertEqual(
            result.exit_gap_pct,
            Decimal("-9.0909090909090909090909090909090909090909090909091"),
        )


class GapHistoryRingTests(unittest.TestCase):
    def test_named_windows_are_inclusive_and_decimal_only(self):
        history = gap.GapHistoryRing()
        start = NOW_MS - 24 * HOUR_MS
        for value in range(25):
            history.record(reading(start + value * HOUR_MS, value))

        windows = history.named_windows(SYMBOL)

        expected = {
            "1h": (2, Decimal("23"), Decimal("24"), Decimal("23.5")),
            "5h": (6, Decimal("19"), Decimal("24"), Decimal("21.5")),
            "15h": (16, Decimal("9"), Decimal("24"), Decimal("16.5")),
            "24h": (25, Decimal("0"), Decimal("24"), Decimal("12")),
        }
        self.assertEqual(tuple(windows), gap.NAMED_WINDOWS)
        for name, (samples, minimum, maximum, mean) in expected.items():
            with self.subTest(window=name):
                stats = windows[name]
                self.assertEqual(stats.samples, samples)
                self.assertEqual(stats.gap_min, minimum)
                self.assertEqual(stats.gap_max, maximum)
                self.assertEqual(stats.gap_mean, mean)
                self.assertEqual(stats.gap_latest, Decimal("24"))
                self.assertTrue(all(isinstance(value, Decimal) for value in (
                    stats.gap_min, stats.gap_max, stats.gap_mean,
                    stats.gap_latest)))

    def test_exit_metric_uses_the_same_window_shape(self):
        history = gap.GapHistoryRing()
        history.record(reading(NOW_MS - HOUR_MS, "1", "7"))
        history.record(reading(NOW_MS, "2", "9"))

        stats = history.window_stats(SYMBOL, "1h", metric="exit")

        self.assertEqual(stats.samples, 2)
        self.assertEqual(stats.gap_min, Decimal("7"))
        self.assertEqual(stats.gap_max, Decimal("9"))
        self.assertEqual(stats.gap_mean, Decimal("8"))
        self.assertEqual(stats.gap_latest, Decimal("9"))

    def test_ring_is_capacity_and_time_bounded(self):
        history = gap.GapHistoryRing(capacity=3)
        for value in range(4):
            history.record(reading(NOW_MS + value, value))
        self.assertEqual(
            [row.entry_gap_pct for row in history.series_for(SYMBOL)],
            [Decimal("1"), Decimal("2"), Decimal("3")],
        )

        elapsed = gap.GapHistoryRing()
        elapsed.record(reading(NOW_MS - 24 * HOUR_MS - 1, "1"))
        elapsed.record(reading(NOW_MS, "2"))
        self.assertEqual(len(elapsed.series_for(SYMBOL)), 1)

    def test_history_refuses_non_decimal_gap_values(self):
        history = gap.GapHistoryRing()
        invalid = gap.ExecutableGapReading(SYMBOL, NOW_MS, 1.0,
                                           Decimal("2"), 0, 0)
        with self.assertRaisesRegex(ValueError, "finite Decimal"):
            history.record(invalid)

    def test_history_cut_preserves_skew_honesty(self):
        history = gap.GapHistoryRing()
        source = gap.ExecutableGapReading(
            SYMBOL, NOW_MS, Decimal("1"), Decimal("2"), 0, 0,
            skew_checked=False,
        )

        recorded = history.record(source)
        restored = history.series_for(SYMBOL)[0]

        self.assertFalse(recorded.skew_checked)
        self.assertFalse(restored.skew_checked)


if __name__ == "__main__":
    unittest.main()
