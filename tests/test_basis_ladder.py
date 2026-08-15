"""Deterministic offline tests for the laddered basis engine core."""

import sys
import unittest
from decimal import Decimal
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import basis_ladder as bl  # noqa: E402


def D(value):
    return Decimal(value)


CALM = bl.FundingContext(D("0.0001"), D("0.0001"))
FLIP_NORMAL = bl.FundingContext(D("0.0001"), D("-0.0001"))
FLIP_REVERSE = bl.FundingContext(D("-0.0001"), D("0.0001"))


def config(**overrides):
    values = {
        "schedule": (
            (D("2"), D("10")),
            (D("4"), D("20")),
            (D("7"), D("40")),
        ),
        "max_rungs": 3,
        "max_total_notional": D("100"),
        "min_notional": D("5"),
        "convergence_gap": D("0"),
    }
    values.update(overrides)
    return bl.LadderConfig(**values)


def run_tick(state, entry, exit_gap, funding=CALM, cfg=None,
             borrow_power=False):
    return bl.advance(
        state,
        D(entry) if entry is not None else None,
        D(exit_gap) if exit_gap is not None else None,
        funding,
        cfg or config(),
        borrow_power=borrow_power,
    )


def build_three(direction=bl.NORMAL, borrow_power=False, cfg=None):
    state = bl.LadderState.flat(direction)
    intents = []
    for gap in ("2", "4", "7"):
        result = run_tick(
            state, gap, gap, cfg=cfg, borrow_power=borrow_power
        )
        state = result.state
        intents.extend(result.intents)
    return state, intents


class LadderBuildTests(unittest.TestCase):
    def test_widening_series_builds_exact_schedule(self):
        state = bl.LadderState.flat()
        kinds = []
        for gap in ("1.9", "2", "3.9", "4", "6.9", "7"):
            result = run_tick(state, gap, gap)
            state = result.state
            kinds.append(result.intent.kind)
        self.assertEqual(
            kinds,
            [bl.HOLD, bl.ADD_RUNG, bl.HOLD, bl.ADD_RUNG,
             bl.HOLD, bl.ADD_RUNG],
        )
        self.assertEqual(
            [(rung.gap_at_entry, rung.size) for rung in state.rungs],
            [(D("2"), D("10")), (D("4"), D("20")),
             (D("7"), D("40"))],
        )

    def test_sizes_come_from_wider_gap_schedule_rows(self):
        state, intents = build_three()
        self.assertEqual([row.size for row in intents],
                         [D("10"), D("20"), D("40")])
        self.assertEqual([row.threshold for row in intents],
                         [D("2"), D("4"), D("7")])
        self.assertEqual(state.total_notional, D("70"))

    def test_gap_jump_still_builds_next_rung_only(self):
        state = bl.LadderState.flat()
        first = run_tick(state, "9", "9")
        second = run_tick(first.state, "9", "9")
        self.assertEqual(first.intent.size, D("10"))
        self.assertEqual(second.intent.size, D("20"))
        self.assertEqual(len(second.state.rungs), 2)

    def test_reverse_refuses_without_borrow_and_engages_with_it(self):
        state = bl.LadderState.flat(bl.REVERSE)
        refused = run_tick(state, "2", "2", borrow_power=False)
        self.assertEqual(refused.intent.kind, bl.REFUSE)
        self.assertEqual(refused.intent.reason, "BORROW_POWER_REQUIRED")
        self.assertEqual(refused.state, state)

        engaged = run_tick(state, "2", "2", borrow_power=True)
        self.assertEqual(engaged.intent.kind, bl.ADD_RUNG)
        self.assertEqual(engaged.state.rungs[0].size, D("10"))
        self.assertEqual(engaged.state.direction, bl.REVERSE)


class LadderUnwindTests(unittest.TestCase):
    def test_closing_gap_unwinds_lifo_at_prior_band_thresholds(self):
        state, _ = build_three()

        held = run_tick(state, "5", "4.1")
        self.assertEqual(held.intent.kind, bl.HOLD)

        top = run_tick(held.state, "4", "4")
        middle = run_tick(top.state, "2", "2")
        bottom = run_tick(middle.state, "0", "0")
        self.assertEqual([top.intent.size, middle.intent.size,
                          bottom.intent.size],
                         [D("40"), D("20"), D("10")])
        self.assertEqual([top.intent.gap_at_entry,
                          middle.intent.gap_at_entry,
                          bottom.intent.gap_at_entry],
                         [D("7"), D("4"), D("2")])
        self.assertTrue(bottom.state.is_flat)

    def test_full_convergence_flattens_all_rungs_in_one_tick(self):
        state, _ = build_three()
        result = run_tick(state, None, "-0.01", funding=None)
        self.assertEqual([row.size for row in result.intents],
                         [D("40"), D("20"), D("10")])
        self.assertTrue(all(row.kind == bl.UNWIND_RUNG
                            for row in result.intents))
        self.assertTrue(all(row.reason == "FULL_CONVERGENCE"
                            for row in result.intents))
        self.assertTrue(result.state.is_flat)

    def test_partial_unwind_is_decimal_exact_and_remains_lifo(self):
        cfg = config(max_unwind_notional_per_tick=D("12.5"))
        state, _ = build_three(cfg=cfg)
        result = run_tick(state, "4", "4", cfg=cfg)
        self.assertEqual(result.intent.size, D("12.5"))
        self.assertEqual(result.state.rungs[-1],
                         bl.Rung(D("7"), D("27.5")))
        self.assertEqual(result.state.total_notional, D("57.5"))

    def test_funding_flip_accelerates_unwind_one_schedule_band(self):
        state = bl.LadderState.flat()
        state = run_tick(state, "2", "2").state
        state = run_tick(state, "4", "4").state

        calm = run_tick(state, "3", "3", funding=CALM)
        self.assertEqual(calm.intent.kind, bl.HOLD)

        flipped = run_tick(state, "3", "3", funding=FLIP_NORMAL)
        self.assertEqual(flipped.intent.kind, bl.UNWIND_RUNG)
        self.assertEqual(flipped.intent.size, D("20"))
        self.assertEqual(flipped.intent.threshold, D("4"))
        self.assertEqual(
            flipped.intent.reason,
            "ADVERSE_FUNDING_FLIP_ACCELERATION",
        )

    def test_reverse_funding_sign_is_the_mirror(self):
        state = bl.LadderState.flat(bl.REVERSE)
        state = run_tick(state, "2", "2", borrow_power=True).state
        state = run_tick(state, "4", "4", borrow_power=True).state
        result = run_tick(
            state,
            "3",
            "3",
            funding=FLIP_REVERSE,
            borrow_power=True,
        )
        self.assertEqual(result.intent.kind, bl.UNWIND_RUNG)
        self.assertEqual(result.intent.size, D("20"))


class BoundsAndRefusalTests(unittest.TestCase):
    def test_cap_clamps_rung_then_exact_size_unwinds(self):
        cfg = config(max_total_notional=D("25"))
        state = bl.LadderState.flat()
        first = run_tick(state, "2", "2", cfg=cfg)
        second = run_tick(first.state, "4", "4", cfg=cfg)
        self.assertEqual(second.intent.kind, bl.ADD_RUNG)
        self.assertEqual(second.intent.reason, "SCHEDULE_RUNG_CAP_CLAMPED")
        self.assertEqual(second.intent.scheduled_size, D("20"))
        self.assertEqual(second.intent.size, D("15"))
        self.assertEqual(second.state.total_notional, D("25"))

        unwound = run_tick(second.state, "2", "2", cfg=cfg)
        self.assertEqual(unwound.intent.size, D("15"))
        self.assertEqual(unwound.state.total_notional, D("10"))

    def test_never_exceeds_max_rungs_or_total_notional(self):
        cfg = config(max_rungs=2, max_total_notional=D("25"))
        state = bl.LadderState.flat()
        rows = []
        for gap in ("2", "4", "7", "20"):
            result = run_tick(state, gap, gap, cfg=cfg)
            state = result.state
            rows.append(result.intent)
            self.assertLessEqual(len(state.rungs), 2)
            self.assertLessEqual(state.total_notional, D("25"))
        self.assertEqual(rows[2].kind, bl.REFUSE)
        self.assertEqual(rows[2].reason, "MAX_RUNGS")
        self.assertEqual(rows[3].kind, bl.REFUSE)

    def test_remaining_capacity_below_min_notional_refuses(self):
        cfg = config(max_total_notional=D("12"))
        state = run_tick(bl.LadderState.flat(), "2", "2", cfg=cfg).state
        result = run_tick(state, "4", "4", cfg=cfg)
        self.assertEqual(result.intent.kind, bl.REFUSE)
        self.assertEqual(result.intent.reason, "MIN_NOTIONAL_INFEASIBLE")
        self.assertEqual(result.state.total_notional, D("10"))

    def test_unknown_money_facts_refuse_add_but_not_known_convergence(self):
        flat = bl.LadderState.flat()
        unknown = bl.advance(flat, None, D("2"), CALM, config())
        self.assertEqual(unknown.intent.kind, bl.REFUSE)
        self.assertEqual(unknown.intent.reason, "ENTRY_GAP_UNKNOWN")

        no_exit = bl.advance(flat, D("2"), None, CALM, config())
        self.assertEqual(no_exit.intent.kind, bl.REFUSE)
        self.assertEqual(no_exit.intent.reason, "EXIT_GAP_UNKNOWN")

        held = run_tick(flat, "2", "2").state
        converged = bl.advance(held, None, D("0"), None, config())
        self.assertTrue(converged.state.is_flat)
        self.assertEqual(converged.intent.kind, bl.UNWIND_RUNG)


class PersistenceAndPurityTests(unittest.TestCase):
    def test_serialize_restore_identity_uses_decimal_text(self):
        state = bl.LadderState(
            bl.REVERSE,
            (
                (D("2.00"), D("10.500")),
                (D("4.250"), D("15.125")),
            ),
        )
        payload = bl.serialize_state(state)
        self.assertEqual(payload, {
            "version": 1,
            "direction": "reverse",
            "rungs": [
                {"gap_at_entry": "2.00", "size": "10.500"},
                {"gap_at_entry": "4.250", "size": "15.125"},
            ],
        })
        restored = bl.restore_state(payload)
        self.assertEqual(restored, state)
        self.assertEqual(bl.serialize_state(restored), payload)

    def test_restore_rejects_non_text_decimal(self):
        payload = {
            "version": 1,
            "direction": "normal",
            "rungs": [{"gap_at_entry": D("2"), "size": "10"}],
        }
        with self.assertRaises(bl.LadderError):
            bl.restore_state(payload)

    def test_config_refuses_floats_and_non_increasing_schedule(self):
        with self.assertRaises(bl.LadderError):
            bl.LadderConfig(((D("2"), 10.0),), 1, D("10"), D("5"))
        with self.assertRaises(bl.LadderError):
            bl.LadderConfig(
                ((D("2"), D("10")), (D("4"), D("10"))),
                2,
                D("20"),
                D("5"),
            )

    def test_module_has_no_transport_or_order_surface(self):
        source = Path(bl.__file__).read_text(encoding="utf-8")
        for token in ("urllib", "socket", "requests", "hmac", "urlopen"):
            self.assertNotIn(token, source)
        self.assertFalse(hasattr(bl, "place_order"))


if __name__ == "__main__":
    unittest.main()
