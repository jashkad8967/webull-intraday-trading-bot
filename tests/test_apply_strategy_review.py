"""Tests for scripts/apply_strategy_review.py - specifically the regex
read/write helpers, since a bad pattern there could corrupt config.py
in a way that only shows up once the auto-apply pipeline is already
running unattended. Each test constructs a small text snippet in the
same shape config.py actually uses (single-line and multi-line Field(...)
declarations both appear there) rather than depending on the real file,
so these stay correct even as config.py grows.
"""

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import apply_strategy_review as script  # noqa: E402


class DecimalFieldReadWriteTests(unittest.TestCase):
    def test_reads_a_single_line_decimal_default(self):
        text = 'max_order_notional: Decimal = Field(default=Decimal("1000"), gt=0)\n'
        self.assertEqual(
            script._read_current_decimal_or_int(text, "max_order_notional"),
            Decimal("1000"),
        )

    def test_reads_a_multi_line_decimal_default(self):
        text = (
            "stock_target_stop_multiple: Decimal = Field(\n"
            '        default=Decimal("1.8"),\n'
            "        ge=Decimal(\"0.5\"),\n"
            "        le=Decimal(\"5\"),\n"
            "    )\n"
        )
        self.assertEqual(
            script._read_current_decimal_or_int(text, "stock_target_stop_multiple"),
            Decimal("1.8"),
        )

    def test_reads_a_plain_int_default(self):
        text = "reenter_confirmation_polls: int = Field(default=2, ge=1, le=20)\n"
        self.assertEqual(
            script._read_current_decimal_or_int(text, "reenter_confirmation_polls"),
            Decimal("2"),
        )

    def test_writes_a_single_line_decimal_default_without_touching_other_kwargs(self):
        text = 'max_order_notional: Decimal = Field(default=Decimal("1000"), gt=0)\n'
        new_text = script._write_decimal_or_int(
            text, "max_order_notional", Decimal("1100"), is_int=False
        )
        self.assertIn('default=Decimal("1100")', new_text)
        self.assertIn("gt=0", new_text)
        self.assertNotIn('Decimal("1000")', new_text)

    def test_writes_a_multi_line_decimal_default_preserving_its_bounds(self):
        text = (
            "stock_target_stop_multiple: Decimal = Field(\n"
            '        default=Decimal("1.8"),\n'
            "        ge=Decimal(\"0.5\"),\n"
            "        le=Decimal(\"5\"),\n"
            "    )\n"
        )
        new_text = script._write_decimal_or_int(
            text, "stock_target_stop_multiple", Decimal("2.25"), is_int=False
        )
        self.assertIn('default=Decimal("2.25")', new_text)
        self.assertIn('ge=Decimal("0.5")', new_text)
        self.assertIn('le=Decimal("5")', new_text)

    def test_writes_a_plain_int_default(self):
        text = "reenter_confirmation_polls: int = Field(default=2, ge=1, le=20)\n"
        new_text = script._write_decimal_or_int(
            text, "reenter_confirmation_polls", Decimal("3"), is_int=True
        )
        self.assertIn("default=3,", new_text)

    def test_only_touches_the_named_field_not_a_similarly_prefixed_one(self):
        """Regression guard: stock_stop_loss_min_percent and
        stock_stop_loss_max_percent share a long common prefix with each
        other and with stock_stop_loss_range_multiplier - a loose regex
        could edit the wrong one.
        """
        text = (
            'stock_stop_loss_min_percent: Decimal = Field(default=Decimal("0.009"), gt=0, le=1)\n'
            'stock_stop_loss_max_percent: Decimal = Field(default=Decimal("0.015"), gt=0, le=1)\n'
            "stock_stop_loss_range_multiplier: Decimal = Field(\n"
            '    default=Decimal("0.35"),\n'
            "    ge=0,\n"
            "    le=Decimal(\"5\"),\n"
            ")\n"
        )
        new_text = script._write_decimal_or_int(
            text, "stock_stop_loss_range_multiplier", Decimal("0.40"), is_int=False
        )
        self.assertIn('stock_stop_loss_min_percent: Decimal = Field(default=Decimal("0.009")', new_text)
        self.assertIn('stock_stop_loss_max_percent: Decimal = Field(default=Decimal("0.015")', new_text)
        self.assertIn('default=Decimal("0.40")', new_text)

    def test_read_returns_none_for_a_field_not_present(self):
        self.assertIsNone(
            script._read_current_decimal_or_int("no_such_field_here", "max_order_notional")
        )

    def test_write_raises_when_the_field_cannot_be_found(self):
        with self.assertRaises(RuntimeError):
            script._write_decimal_or_int("nothing here", "max_order_notional", Decimal("1"), False)


class BoolFieldReadWriteTests(unittest.TestCase):
    def test_reads_and_writes_a_bool_default(self):
        text = "time_aware_stop_enabled: bool = True\n"
        self.assertTrue(script._read_current_bool(text, "time_aware_stop_enabled"))
        new_text = script._write_bool(text, "time_aware_stop_enabled", False)
        self.assertIn("time_aware_stop_enabled: bool = False", new_text)

    def test_only_touches_the_named_bool_field(self):
        text = "symbol_quarantine_enabled: bool = False\ntime_aware_stop_enabled: bool = True\n"
        new_text = script._write_bool(text, "symbol_quarantine_enabled", True)
        self.assertIn("symbol_quarantine_enabled: bool = True", new_text)
        self.assertIn("time_aware_stop_enabled: bool = True", new_text)


class LiveEnvOverrideTests(unittest.TestCase):
    def test_detects_an_override(self):
        env_text = "AGENT_ENABLED=true\nMAX_ORDER_NOTIONAL=1500\n"
        self.assertTrue(script._is_overridden_in_live_env("max_order_notional", env_text))

    def test_no_false_positive_on_a_prefix_match(self):
        """MAX_ORDER_NOTIONAL_EXTRA (hypothetical) must not register as an
        override of MAX_ORDER_NOTIONAL - the match is anchored to the
        full VAR=  form, not a substring.
        """
        env_text = "MAX_ORDER_NOTIONAL_EXTRA=1\n"
        self.assertFalse(script._is_overridden_in_live_env("max_order_notional", env_text))

    def test_absent_field_is_not_an_override(self):
        env_text = "AGENT_ENABLED=true\n"
        self.assertFalse(script._is_overridden_in_live_env("max_order_notional", env_text))


class SuggestionHashTests(unittest.TestCase):
    def test_identical_suggestions_hash_the_same(self):
        review = {"severity": "moderate", "suggested_changes": [{"lever": "position size"}]}
        self.assertEqual(script._suggestion_hash(review), script._suggestion_hash(dict(review)))

    def test_different_suggestions_hash_differently(self):
        a = {"severity": "moderate"}
        b = {"severity": "severe"}
        self.assertNotEqual(script._suggestion_hash(a), script._suggestion_hash(b))


if __name__ == "__main__":
    unittest.main()
