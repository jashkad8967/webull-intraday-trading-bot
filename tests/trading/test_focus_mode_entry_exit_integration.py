"""Full sanity check, by explicit request: "a full sanity check on the
logic... for every part of this new strategy" - one focus stock,
traded with valid/affordable/volatile/volume-backed calls and puts as
momentum shifts, using the entire account value, recovering every
available contract.

Every other test file covers ONE function in isolation. This one
drives _evaluate_option_entry/_evaluate_option_exit as WHOLE
functions through a faithful fake bot (real collaborator functions
bound via __get__ wherever they exist, not reimplemented), the same
way the real main loop actually calls them - the only place today's
many individual gate fixes get exercised together instead of one at
a time.
"""
import unittest
from collections import defaultdict, deque
from decimal import Decimal
from types import SimpleNamespace

from webull_bot.bot import AutoTrader
from webull_bot.webull_api import WebullAPI

from test_focus_mode import focus_config


def _contract(underlying, symbol, option_type, strike="170", dte=20):
    from datetime import date, timedelta

    return {
        "underlying_symbol": underlying,
        "symbol": symbol,
        "option_type": option_type,
        "strike_price": strike,
        "expiration_date": (date.today() + timedelta(days=dte)).isoformat(),
        "tradable_status": "OC",
    }


class FocusModeIntegrationTestCase(unittest.TestCase):
    """Shared fake-bot builder. Real collaborator logic (cooldowns,
    price sanity, symbol quarantine, trade recording, option pricing)
    is bound from the actual source, not stubbed - a stub would only
    ever test my own assumptions about that logic, not the logic
    itself.
    """

    def _build(self, **config_overrides):
        config_overrides.setdefault("focus_mode_enabled", True)
        config = focus_config(**config_overrides)

        placed_orders: list[tuple] = []

        class FakeApi:
            config_ = None

            @staticmethod
            def quote_bid(quote):
                return quote.get("bid")

            @staticmethod
            def quote_ask(quote):
                return quote.get("ask")

            @staticmethod
            def option_delta(quote):
                return None  # confirmed inert on this account - always fails open

            @staticmethod
            def place_option(contract, side, quantity, limit_price, intent):
                order_id = f"order-{len(placed_orders) + 1}"
                placed_orders.append(
                    (contract["symbol"], side, quantity, limit_price, intent)
                )
                return order_id

        real_api = WebullAPI.__new__(WebullAPI)
        real_api.config = SimpleNamespace(
            option_limit_offset=Decimal("0.03"),
            quote_price_sanity_percent=Decimal("0.08"),
        )
        fake_api = FakeApi()
        # Real pricing/tick-quantization math, not reimplemented.
        fake_api.option_limit_price = real_api.option_limit_price.__get__(real_api)
        fake_api._quantize_to_option_tick = (
            real_api._quantize_to_option_tick.__get__(real_api)
        )

        status = SimpleNamespace(record_trade=lambda *a, **k: None)

        bot = SimpleNamespace(
            config=config,
            api=fake_api,
            status=status,
            focus_cohort=["NVDA"],
            focus_symbol_no_chain=set(),
            option_contracts=[],
            option_discovery_attempted=set(),
            option_average_down_count=defaultdict(int),
            last_option_average_down={},
            option_last_buy_price={},
            option_peak_price={},
            option_contracts_state=SimpleNamespace(save=lambda *a, **k: None),
            option_gate_rejections=defaultdict(int),
            gate_rejections=defaultdict(int),
            option_momentum_flip={},
            option_iv_history=defaultdict(lambda: deque(maxlen=30)),
            vixy_history=deque(maxlen=30),
            wash_sales=SimpleNamespace(
                blocked_until=lambda key: None, block=lambda key, reason: None
            ),
            wash_skip_logged=set(),
            pending_option_exits=set(),
            cached_option_buying_power=Decimal("10000"),
            profit_throttle_armed=False,
            symbol_quarantine_until={},
            symbol_pnl_history=defaultdict(deque),
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            price_sanity_rejected_at={},
            position_opened_at={},
            consecutive_exit_failures={},
            manual_touch_at={},
            submitted_order_ids_today=set(),
            working_orders={},
            last_capital_deployed_at=0.0,
            option_entry_occurred_today=False,
            cached_positions=[],
            strategy=SimpleNamespace(
                prices={"NVDA": Decimal("170.00")},
                option_order_quantity=lambda price, bp: (
                    int(bp // (price * 100)),
                    price * 100,
                ),
                pressure_supports_entry=lambda underlying, option_type: True,
                pressure_flipped_against=lambda underlying, option_type: False,
                rsi_divergence=lambda underlying, price, moment: "NONE",
                approaching_resistance=lambda underlying, price, direction: False,
                is_volatility_scalp_eligible=lambda underlying: True,
                realized_volatility_percent=lambda underlying: Decimal("0.03"),
                relative_volume_ok=lambda underlying: True,
                entry_extension_ok=lambda underlying, price, direction: True,
                volatility_scalp_dip_signal=lambda underlying, price: False,
                volatility_scalp_rip_signal=lambda underlying, price: False,
                option_delta_ok=lambda delta: True,
                option_iv_percentile_ok=lambda history, iv: True,
                option_market_regime_ok=lambda history, vixy: True,
                option_average_down_signal=lambda price, cost, level=0: False,
                option_decision=None,  # set per-test where exit is exercised
            ),
        )
        bot.new_entries_blocked = AutoTrader.new_entries_blocked.__get__(bot)
        bot.record_realized_exit = lambda cost, price, qty, multiplier=100: (
            (price - cost) * qty * multiplier
        )
        # Real collaborator logic, bound from the actual source.
        from webull_bot.trading.guards.price_sanity import (
            price_sanity_cooldown_ready,
            price_sanity_ok,
        )
        from webull_bot.trading.guards.symbol_quarantine import symbol_quarantined
        from webull_bot.trading.orders.trade_recording import record_trade
        from webull_bot.trading.util.cooldowns import (
            cooldown_ready,
            rate_capped,
            reentry_cooldown_ready,
        )

        bot.cooldown_ready = cooldown_ready.__get__(bot)
        bot.rate_capped = rate_capped.__get__(bot)
        bot.reentry_cooldown_ready = reentry_cooldown_ready.__get__(bot)
        bot.price_sanity_ok = price_sanity_ok.__get__(bot)
        bot.price_sanity_cooldown_ready = price_sanity_cooldown_ready.__get__(bot)
        bot.symbol_quarantined = symbol_quarantined.__get__(bot)
        bot.record_trade = record_trade.__get__(bot)
        from webull_bot.trading.options.option_entry_exit import (
            _evaluate_option_entry,
            _evaluate_option_exit,
        )

        bot._evaluate_option_entry = _evaluate_option_entry.__get__(bot)
        bot._evaluate_option_exit = _evaluate_option_exit.__get__(bot)
        return bot, placed_orders

    def _quote(self, bid, ask):
        # "price" (the last-trade print) is the midpoint, not the raw
        # bid - option_limit_price routes bid/ask through
        # _sane_bid_or_ask, which rejects either side as implausible
        # if it deviates from "price" by more than quote_price_sanity_
        # percent (8%). A realistic last trade sits near the middle of
        # the spread, not pinned to one edge.
        bid_d, ask_d = Decimal(bid), Decimal(ask)
        return {"bid": bid_d, "ask": ask_d, "price": (bid_d + ask_d) / 2}

    def _enter(
        self,
        bot,
        contract,
        quote,
        directions,
        open_count=0,
        buying_power=None,
        positions=None,
    ):
        option_symbol = contract["symbol"]
        key = f"OPTION:{option_symbol}"
        return bot._evaluate_option_entry(
            contract,
            option_symbol,
            key,
            quote,
            quote["price"],
            days_to_expiration=20,
            current_iv=Decimal("0.5"),
            directions=directions,
            guard_active=False,
            current_vixy=None,
            open_count=open_count,
            buying_power=buying_power
            if buying_power is not None
            else bot.cached_option_buying_power,
            positions=positions if positions is not None else [],
        )


class CallAndPutEntryTests(FocusModeIntegrationTestCase):
    """The core requirement: one focus stock, trading valid,
    affordable, volume/volatility-backed calls and puts as momentum
    shifts up and down.
    """

    def test_a_call_enters_on_a_bullish_direction_signal(self):
        bot, placed = self._build()
        contract = _contract("NVDA", "NVDAC", "CALL")
        quote = self._quote("1.90", "2.00")
        open_count, buying_power = self._enter(
            bot, contract, quote, directions={"NVDA": "CALL"}
        )
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0][0], "NVDAC")
        self.assertEqual(placed[0][1], "BUY")
        self.assertEqual(open_count, 1)

    def test_a_put_enters_on_a_bearish_direction_signal(self):
        bot, placed = self._build()
        contract = _contract("NVDA", "NVDAP", "PUT")
        quote = self._quote("1.80", "1.90")
        self._enter(bot, contract, quote, directions={"NVDA": "PUT"})
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0][1], "BUY")

    def test_a_call_never_enters_on_a_bearish_signal(self):
        bot, placed = self._build()
        contract = _contract("NVDA", "NVDAC", "CALL")
        quote = self._quote("1.90", "2.00")
        self._enter(bot, contract, quote, directions={"NVDA": "PUT"})
        self.assertEqual(placed, [])
        self.assertEqual(
            bot.option_gate_rejections["no direction signal for this underlying"], 1
        )

    def test_no_signal_at_all_produces_no_entry(self):
        bot, placed = self._build()
        contract = _contract("NVDA", "NVDAC", "CALL")
        quote = self._quote("1.90", "2.00")
        self._enter(bot, contract, quote, directions={"NVDA": "HOLD"})
        self.assertEqual(placed, [])

    def test_a_symbol_outside_the_cohort_never_enters_even_on_a_perfect_signal(self):
        """Structural gate - the whole point of focus mode."""
        bot, placed = self._build()
        contract = _contract("AAPL", "AAPLC", "CALL")
        quote = self._quote("1.90", "2.00")
        self._enter(bot, contract, quote, directions={"AAPL": "CALL"})
        self.assertEqual(placed, [])
        self.assertEqual(
            bot.option_gate_rejections["not in today's focus cohort"], 1
        )

    def test_dip_signal_enters_a_call_even_with_a_hold_direction(self):
        """By request: "it doesn't buy puts while there is a dip, or
        a call on a dip entry" - the mean-reversion path.
        """
        bot, placed = self._build()
        bot.strategy.volatility_scalp_dip_signal = (
            lambda underlying, price: underlying == "NVDA"
        )
        contract = _contract("NVDA", "NVDAC", "CALL")
        quote = self._quote("1.90", "2.00")
        self._enter(bot, contract, quote, directions={"NVDA": "HOLD"})
        self.assertEqual(len(placed), 1)

    def test_momentum_flip_lets_a_put_enter_right_after_a_call_exits_on_resistance(
        self,
    ):
        """By explicit request: "how to immediately sell call and buy
        a put at the tip of momentum and vice versa" - the core
        momentum-shift vision this whole strategy is built around.
        """
        bot, placed = self._build()
        from webull_bot.strategy_logic.types import Decision

        # A held CALL exits on resistance (a momentum-exhaustion
        # PROFIT, not the flat target).
        held_call = _contract("NVDA", "NVDAC", "CALL")
        bot.option_contracts.append(held_call)
        bot.strategy.option_decision = lambda *a, **k: Decision(
            "HOLD", "waiting", None
        )
        bot.strategy.approaching_resistance = (
            lambda underlying, price, direction: True
        )
        exit_quote = self._quote("2.05", "2.15")
        bot._evaluate_option_exit(
            held_call,
            "NVDAC",
            "OPTION:NVDAC",
            exit_quote,
            Decimal("2.05"),
            quantity=1,
            cost=Decimal("1.50"),
            days_to_expiration=20,
            buying_power=bot.cached_option_buying_power,
        )
        self.assertIn("NVDA", bot.option_momentum_flip)
        self.assertEqual(bot.option_momentum_flip["NVDA"][0], "PUT")

        # The exit itself already placed one order (the CALL SELL);
        # the flip alone (direction still HOLD, no dip/rip signal) is
        # now enough to let a PUT enter as a second, separate order.
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0][1], "SELL")
        put_contract = _contract("NVDA", "NVDAP", "PUT")
        entry_quote = self._quote("1.80", "1.90")
        self._enter(bot, put_contract, entry_quote, directions={"NVDA": "HOLD"})
        self.assertEqual(len(placed), 2)
        self.assertEqual(placed[1][0], "NVDAP")
        self.assertEqual(placed[1][1], "BUY")


class FullAccountUtilizationTests(FocusModeIntegrationTestCase):
    """By explicit request: "it utilizes the entire account value."""

    def test_quantity_uses_full_buying_power_not_a_flat_cap(self):
        bot, placed = self._build()
        contract = _contract("NVDA", "NVDAC", "CALL")
        # $2.00 ask -> $200/contract; $10,000 buying power -> 50
        # contracts fully affordable. option_quantity (20 in the
        # shared fixture) and max_order_notional must not clamp this
        # in focus mode.
        quote = self._quote("1.90", "2.00")
        self._enter(
            bot,
            contract,
            quote,
            directions={"NVDA": "CALL"},
            buying_power=Decimal("10000"),
        )
        self.assertEqual(len(placed), 1)
        # limit_price is the 1.90/2.00 midpoint (1.95), not the raw
        # ask - $10,000 / $195/contract = 51 contracts, fully
        # affordable, no artificial cap in the way.
        self.assertEqual(placed[0][2], 51)

    def test_too_cheap_to_survive_its_own_noise_still_blocks_entry(self):
        """Full utilization is not a license to buy a lottery ticket -
        the premium floor is a real, structural risk control, unaffected
        by how much buying power is available.
        """
        bot, placed = self._build(option_min_premium_dollars=Decimal("0.50"))
        contract = _contract("NVDA", "NVDAC", "CALL")
        # Tight spread (10%, well inside the 25% floor) so this fails
        # ONLY on premium, not on liquidity too.
        quote = self._quote("0.10", "0.11")
        self._enter(bot, contract, quote, directions={"NVDA": "CALL"})
        self.assertEqual(placed, [])
        self.assertEqual(
            bot.option_gate_rejections[
                "premium below the minimum (too cheap to survive its own noise)"
            ],
            1,
        )


class LiquidityAndStructuralGateTests(FocusModeIntegrationTestCase):
    """Structural gates that must stay active regardless of focus
    mode - liquidity and DTE protect against a contract that can't be
    exited, not against a bad directional call.
    """

    def test_a_wide_spread_contract_never_enters(self):
        bot, placed = self._build(option_max_entry_spread_percent=Decimal("25"))
        contract = _contract("NVDA", "NVDAC", "CALL")
        quote = self._quote("1.00", "2.00")  # 100% spread
        self._enter(bot, contract, quote, directions={"NVDA": "CALL"})
        self.assertEqual(placed, [])
        self.assertEqual(
            bot.option_gate_rejections[
                "contract bid/ask spread too wide to liquidate reliably"
            ],
            1,
        )

    def test_a_near_expiry_contract_never_enters(self):
        bot, placed = self._build(option_min_hold_dte=7)
        contract = _contract("NVDA", "NVDAC", "CALL", dte=3)
        quote = self._quote("1.90", "2.00")
        option_symbol = contract["symbol"]
        bot._evaluate_option_entry(
            contract,
            option_symbol,
            f"OPTION:{option_symbol}",
            quote,
            quote["price"],
            days_to_expiration=3,
            current_iv=Decimal("0.5"),
            directions={"NVDA": "CALL"},
            guard_active=False,
            current_vixy=None,
            open_count=0,
            buying_power=bot.cached_option_buying_power,
            positions=[],
        )
        self.assertEqual(placed, [])
        self.assertEqual(bot.option_gate_rejections["too close to expiration"], 1)


class DailyProfitThrottleTests(FocusModeIntegrationTestCase):
    """By request: "once you hit a certain profit slow down" - stops
    NEW risk, never exits or averaging down.
    """

    def test_armed_throttle_blocks_a_fresh_entry(self):
        bot, placed = self._build()
        bot.profit_throttle_armed = True
        contract = _contract("NVDA", "NVDAC", "CALL")
        quote = self._quote("1.90", "2.00")
        self._enter(bot, contract, quote, directions={"NVDA": "CALL"})
        self.assertEqual(placed, [])
        self.assertEqual(
            bot.option_gate_rejections[
                "daily profit target reached - new entries throttled"
            ],
            1,
        )

    def test_armed_throttle_does_not_block_averaging_down(self):
        """By explicit request, averaging down manages an ALREADY-open
        position rather than adding new risk - deliberately exempt.
        """
        bot, placed = self._build(
            option_averaging_down_dip_percent=Decimal("0.10"),
            option_averaging_step_multiplier=Decimal("0.5"),
            option_max_averaging_buys=2,
            option_averaging_reentry_cooldown_seconds=Decimal("0"),
        )
        bot.profit_throttle_armed = True
        from webull_bot.strategy_logic.types import Decision

        bot.strategy.option_decision = lambda *a, **k: Decision(
            "HOLD", "waiting", None
        )
        bot.strategy.option_average_down_signal = lambda price, cost, level=0: True
        contract = _contract("NVDA", "NVDAC", "CALL")
        quote = self._quote("0.90", "1.00")
        bot._evaluate_option_exit(
            contract,
            "NVDAC",
            "OPTION:NVDAC",
            quote,
            Decimal("0.90"),
            quantity=1,
            cost=Decimal("1.50"),
            days_to_expiration=20,
            buying_power=bot.cached_option_buying_power,
        )
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0][1], "BUY")


class EntryClockSurvivesRestartTests(FocusModeIntegrationTestCase):
    """position_opened_at is in-memory only, so every restart wipes it
    for positions that are still open.

    Live 2026-09-22: three positions opened at 10:46 were still open at
    13:05 when a deploy restarted the container. With no recorded open
    time, seconds_since_entry was None - and None never triggers the
    stale exit, so the positions the timer exists for were the exact
    ones permanently immune to it.
    """

    def _bot(self):
        from webull_bot.strategy_logic.decision.stock_option_decision import (
            option_decision,
        )

        bot, placed = self._build()
        bot.strategy.option_decision = option_decision.__get__(bot.strategy)
        bot.strategy.config = bot.config
        return bot, placed

    def test_a_position_with_no_recorded_entry_time_starts_its_clock(self):
        bot, placed = self._bot()
        contract = _contract("NVDA", "NVDAC", "CALL")
        self.assertEqual(bot.position_opened_at, {})
        bot._evaluate_option_exit(
            contract, "NVDAC", "OPTION:NVDAC",
            self._quote("1.00", "1.00"), Decimal("1.00"),
            quantity=1, cost=Decimal("1.00"), days_to_expiration=20,
            buying_power=bot.cached_option_buying_power,
        )
        self.assertIn(
            "OPTION:NVDAC",
            bot.position_opened_at,
            "a held position seen without an entry timestamp must start "
            "its clock, or the stale exit can never reach it",
        )

    def test_the_seeded_clock_is_not_overwritten_on_later_cycles(self):
        """Re-seeding every cycle would hold the age at zero forever,
        which is the same bug wearing a different hat.
        """
        bot, placed = self._bot()
        contract = _contract("NVDA", "NVDAC", "CALL")
        for _ in range(3):
            bot._evaluate_option_exit(
                contract, "NVDAC", "OPTION:NVDAC",
                self._quote("1.00", "1.00"), Decimal("1.00"),
                quantity=1, cost=Decimal("1.00"), days_to_expiration=20,
                buying_power=bot.cached_option_buying_power,
            )
        first = bot.position_opened_at["OPTION:NVDAC"]
        bot._evaluate_option_exit(
            contract, "NVDAC", "OPTION:NVDAC",
            self._quote("1.00", "1.00"), Decimal("1.00"),
            quantity=1, cost=Decimal("1.00"), days_to_expiration=20,
            buying_power=bot.cached_option_buying_power,
        )
        self.assertEqual(bot.position_opened_at["OPTION:NVDAC"], first)


class ProfitLockTrailIntegrationTests(FocusModeIntegrationTestCase):
    """By request: "make sure when there is a profit to not let on too
    much loss" - exercised through the real exit function, not just
    option_decision in isolation.
    """

    def test_a_pullback_from_a_real_peak_exits_as_profit_above_cost(self):
        bot, placed = self._build(
            profit_lock_arm_percent=Decimal("0.10"),
            profit_lock_giveback_fraction=Decimal("0.50"),
            option_take_profit_percent=Decimal("5"),  # push the flat target out of reach
        )
        from webull_bot.strategy_logic.decision.stock_option_decision import (
            option_decision,
        )

        bot.strategy.option_decision = option_decision.__get__(bot.strategy)
        bot.strategy.config = bot.config
        contract = _contract("NVDA", "NVDAC", "CALL")
        cost = Decimal("1.00")
        # The trail rides the BID (sell_realizable_price), not the
        # `price` argument - a tight/no-spread quote here makes the
        # recorded peak exactly 1.40, so the floor math is exact:
        # floor = 1.00 + (1.40-1.00)*0.5 = 1.20. Pulled back to 1.15,
        # clearly below that floor.
        bot._evaluate_option_exit(
            contract, "NVDAC", "OPTION:NVDAC",
            self._quote("1.40", "1.40"), Decimal("1.40"),
            quantity=1, cost=cost, days_to_expiration=20,
            buying_power=bot.cached_option_buying_power,
        )
        bot._evaluate_option_exit(
            contract, "NVDAC", "OPTION:NVDAC",
            self._quote("1.15", "1.20"), Decimal("1.15"),
            quantity=1, cost=cost, days_to_expiration=20,
            buying_power=bot.cached_option_buying_power,
        )
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0][1], "SELL")
        self.assertGreater(placed[0][3], cost)


if __name__ == "__main__":
    unittest.main()
