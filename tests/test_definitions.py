#!/usr/bin/env python3
"""Synthetic tests for the frozen Volume Dataset V2 label definitions."""

import unittest
from dataclasses import FrozenInstanceError, fields, is_dataclass
from decimal import Decimal


from study import definitions


EXPECTED = {
    "DeadRegime": definitions.DeadRegime,
    "Ignition": definitions.Ignition,
    "GenuineRunner": definitions.GenuineRunner,
    "FalseIgnition": definitions.FalseIgnition,
    "SlowRamp": definitions.SlowRamp,
    "AlreadyPumped": definitions.AlreadyPumped,
}


CASES = (
    (
        "DeadRegime",
        definitions.is_dead_regime,
        {
            "flat_candle_pct_1h": Decimal("85"),
            "flat_candle_pct_6h": Decimal("75"),
            "flat_candle_pct_24h": Decimal("65"),
            "minutes_since_meaningful_move": 90,
            "volume_percentile_30d": Decimal("15"),
            "activity_percentile_30d": Decimal("10"),
        },
        ("volume_percentile_30d", Decimal("21")),
    ),
    (
        "Ignition",
        definitions.is_ignition,
        {
            "volume_percentile_30d": Decimal("98"),
            "activity_percentile_30d": Decimal("95"),
            "volume_acceleration_1m": Decimal("6"),
            "volume_acceleration_5m": Decimal("4"),
            "volume_acceleration_10m": Decimal("2.5"),
            "volume_acceleration_15m": Decimal("2"),
            "volume_acceleration_60m": Decimal("1.5"),
            "price_acceleration_5m_pct": Decimal("0.8"),
        },
        ("volume_acceleration_5m", Decimal("2.99")),
    ),
    (
        "GenuineRunner",
        definitions.is_genuine_runner,
        {
            "ignition": True,
            "forward_return_15m_pct": Decimal("2.5"),
            "forward_return_60m_pct": Decimal("4"),
            "max_favorable_excursion_60m_pct": Decimal("7"),
            "max_adverse_excursion_60m_pct": Decimal("-1.5"),
            "positive_candles_next_15m": 10,
        },
        ("max_adverse_excursion_60m_pct", Decimal("-2.01")),
    ),
    (
        "FalseIgnition",
        definitions.is_false_ignition,
        {
            "ignition": True,
            "forward_return_15m_pct": Decimal("-0.2"),
            "max_favorable_excursion_15m_pct": Decimal("0.8"),
            "active_minutes_next_5m": 2,
        },
        ("active_minutes_next_5m", 3),
    ),
    (
        "SlowRamp",
        definitions.is_slow_ramp,
        {
            "ignition": False,
            "price_change_60m_pct": Decimal("3.5"),
            "max_1m_price_change_60m_pct": Decimal("0.6"),
            "positive_candles_60m": 40,
            "volume_acceleration_1m": Decimal("2"),
            "volume_acceleration_60m": Decimal("1.5"),
        },
        ("ignition", True),
    ),
    (
        "AlreadyPumped",
        definitions.is_already_pumped,
        {
            "ignition": True,
            "price_change_prior_60m_pct": Decimal("4"),
            "minutes_since_meaningful_move": 10,
            "volume_percentile_30d": Decimal("90"),
            "volume_acceleration_60m": Decimal("2"),
        },
        ("minutes_since_meaningful_move", 16),
    ),
)


class DefinitionRegistryTests(unittest.TestCase):
    def test_registry_is_complete_typed_and_immutable(self):
        self.assertEqual(set(definitions.DEFINITIONS), set(EXPECTED))
        self.assertEqual(len(definitions.DEFINITIONS), 6)
        for label, expected_type in EXPECTED.items():
            with self.subTest(label=label):
                self.assertIsInstance(
                    definitions.DEFINITIONS[label], expected_type
                )
        with self.assertRaises(TypeError):
            definitions.DEFINITIONS["extra"] = definitions.DeadRegime()
        with self.assertRaises(FrozenInstanceError):
            definitions.DEFINITIONS["Ignition"].percentile_history_days = 1

    def test_every_threshold_has_why_and_decimal_or_int_default(self):
        for label, definition in definitions.DEFINITIONS.items():
            with self.subTest(label=label):
                self.assertTrue(is_dataclass(definition))
                for item in fields(definition):
                    value = getattr(definition, item.name)
                    self.assertTrue(item.metadata["why"].startswith("WHY:"))
                    self.assertTrue(
                        isinstance(value, Decimal) or type(value) is int
                    )
                    self.assertNotIsInstance(value, float)


class LabelPredicateTests(unittest.TestCase):
    def test_synthetic_rows_are_pure_deterministic_and_discriminating(self):
        for label, predicate, positive, negative_change in CASES:
            with self.subTest(label=label):
                original = dict(positive)
                first = predicate(positive)
                second = predicate(positive)
                self.assertIs(first, True)
                self.assertEqual(first, second)
                self.assertEqual(positive, original)
                self.assertIs(
                    definitions.DEFINITIONS[label].matches(positive), True
                )
                self.assertIs(
                    definitions.DEFINITIONS[label].predicate(positive), True
                )
                negative = dict(positive)
                negative[negative_change[0]] = negative_change[1]
                self.assertIs(predicate(negative), False)

    def test_missing_none_float_and_numeric_string_fail_closed(self):
        for label, predicate, positive, _negative_change in CASES:
            with self.subTest(label=label):
                first_key = next(
                    key
                    for key, value in positive.items()
                    if type(value) is not bool
                )
                missing = dict(positive)
                del missing[first_key]
                unknown = dict(positive)
                unknown[first_key] = None
                floating = dict(positive)
                floating[first_key] = 1.0
                text = dict(positive)
                text[first_key] = "1"
                self.assertIs(predicate(missing), False)
                self.assertIs(predicate(unknown), False)
                self.assertIs(predicate(floating), False)
                self.assertIs(predicate(text), False)


if __name__ == "__main__":
    unittest.main()
