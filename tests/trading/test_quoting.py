import sys
import unittest
import unittest.mock
from decimal import Decimal
from types import SimpleNamespace

from webull_bot.webull_api import WebullAPI


class PayloadSizingTests(unittest.TestCase):
    def test_payload_errors_are_detected(self):
        self.assertTrue(WebullAPI._payload_too_large("HTTP 413"))
        self.assertTrue(WebullAPI._payload_too_large("Payload Too Large"))
        self.assertFalse(WebullAPI._payload_too_large("INVALID_SYMBOL"))

    def test_local_snapshot_size_guard_is_treated_as_oversized(self):
        """Live incident: once options discovery grew past 100
        underlyings, trade_options' direction-signal quote fetch
        started failing every cycle with stock_quotes' own local
        pre-check message ("Webull stock snapshots accept at most 100
        symbols") - raised locally, before ever reaching the real API,
        not a server-reported 413. This must be recognized as
        "oversized" too so stock_quotes_resilient's existing recursive
        split actually engages instead of losing the whole batch.
        """
        self.assertTrue(
            WebullAPI._payload_too_large(
                "Webull stock snapshots accept at most 100 symbols"
            )
        )

    def test_local_snapshot_size_guard_triggers_a_real_bisect(self):
        api = WebullAPI.__new__(WebullAPI)
        calls = []

        def stock_quotes(symbols, category):
            calls.append(list(symbols))
            if len(symbols) > 2:
                raise ValueError(
                    "Webull stock snapshots accept at most 2 symbols"
                )
            return [{"symbol": symbol} for symbol in symbols]

        api.stock_quotes = stock_quotes
        quotes, invalid = api.stock_quotes_resilient(
            ["A", "B", "C", "D", "E"],
            "US_STOCK",
        )

        self.assertEqual(
            [item["symbol"] for item in quotes],
            ["A", "B", "C", "D", "E"],
        )
        self.assertEqual(invalid, set())
        self.assertGreater(len(calls), 1)

    def test_oversized_stock_snapshot_is_bisected(self):
        api = WebullAPI.__new__(WebullAPI)
        calls = []

        def stock_quotes(symbols, category):
            calls.append(list(symbols))
            if len(symbols) > 2:
                raise RuntimeError("Webull API error 413: payload too large")
            return [{"symbol": symbol} for symbol in symbols]

        api.stock_quotes = stock_quotes
        quotes, invalid = api.stock_quotes_resilient(
            ["A", "B", "C", "D", "E"],
            "US_STOCK",
        )

        self.assertEqual(
            [item["symbol"] for item in quotes],
            ["A", "B", "C", "D", "E"],
        )
        self.assertEqual(invalid, set())
        self.assertGreater(len(calls), 1)


class HistoryBarResponseShapeTests(unittest.TestCase):
    """Live incident: the real batch-history-bar response is
    {"result": [{"symbol": ..., "result": [...bars]}]} - one layer
    deeper than _parse_amplitudes/_parse_closes/recent_minute_closes
    ever assumed (they expected a flat list with bars under "bars" or
    "candles"). This silently gave VOLFILT's daily volatility pre-filter
    and the SMA trend filter 0/N coverage every single day (both fell
    back to their safe "no filtering" default, so nothing crashed - it
    just never worked), and broke volatility-scalp's M1 bar-seeding
    outright (recent_minute_closes always returned {}).
    """

    def test_history_bars_resilient_unwraps_the_outer_result_key(self):
        api = WebullAPI.__new__(WebullAPI)

        def fake_call(callback, group):
            return {
                "result": [
                    {
                        "symbol": "TBB",
                        "result": [{"close": "19.53"}, {"close": "19.50"}],
                        "instrument_id": "925401989",
                    }
                ]
            }

        api._call = fake_call
        page = api._history_bars_resilient(["TBB"], "US_STOCK", "M1", "20")
        self.assertEqual(page, [
            {
                "symbol": "TBB",
                "result": [{"close": "19.53"}, {"close": "19.50"}],
                "instrument_id": "925401989",
            }
        ])

    def test_history_bars_resilient_passes_through_an_already_flat_list(self):
        """Defensive: if the SDK ever hands back an already-unwrapped
        list (e.g. a test double, or a future SDK version), don't break.
        """
        api = WebullAPI.__new__(WebullAPI)

        def fake_call(callback, group):
            return [{"symbol": "TBB", "bars": [{"close": "19.53"}]}]

        api._call = fake_call
        page = api._history_bars_resilient(["TBB"], "US_STOCK", "M1", "20")
        self.assertEqual(page, [{"symbol": "TBB", "bars": [{"close": "19.53"}]}])

    def test_extract_bars_prefers_bars_then_candles_then_result(self):
        self.assertEqual(WebullAPI._extract_bars({"bars": [1]}), [1])
        self.assertEqual(WebullAPI._extract_bars({"candles": [2]}), [2])
        self.assertEqual(WebullAPI._extract_bars({"result": [3]}), [3])
        self.assertIsNone(WebullAPI._extract_bars({}))
        self.assertIsNone(WebullAPI._extract_bars({"result": "not-a-list"}))

    def test_recent_minute_closes_parses_the_real_nested_response_shape(self):
        api = WebullAPI.__new__(WebullAPI)
        fake_timespan = SimpleNamespace(M1=SimpleNamespace(name="M1"))

        def fake_call(callback, group):
            return {
                "result": [
                    {
                        "symbol": "TBB",
                        "result": [
                            {"time": "2026-08-21T17:43:00.000+0000", "close": "19.53"},
                            {"time": "2026-08-21T17:34:00.000+0000", "close": "19.54"},
                            {"time": "2026-08-21T17:30:00.000+0000", "close": "19.5285"},
                        ],
                        "instrument_id": "925401989",
                    }
                ]
            }

        api._call = fake_call
        api.data = SimpleNamespace(
            market_data=SimpleNamespace(get_batch_history_bar=lambda *a, **k: None)
        )

        with unittest.mock.patch.dict(
            sys.modules,
            {"webull.data.common.timespan": SimpleNamespace(Timespan=fake_timespan)},
        ):
            closes = api.recent_minute_closes(["TBB"], "US_STOCK", count=20)

        self.assertEqual(closes, {"TBB": [19.5285, 19.54, 19.53]})

    def test_sma_trend_parses_the_real_nested_response_shape(self):
        api = WebullAPI.__new__(WebullAPI)
        fake_category = SimpleNamespace(US_STOCK=SimpleNamespace(name="US_STOCK"))
        fake_timespan = SimpleNamespace(D=SimpleNamespace(name="DAY"))

        def fake_call(callback, group):
            return {
                "result": [
                    {"symbol": "NVDA", "result": [{"close": "100"}, {"close": "80"}]}
                ]
            }

        api._call = fake_call
        api.data = SimpleNamespace(
            market_data=SimpleNamespace(get_batch_history_bar=lambda *a, **k: None)
        )

        with unittest.mock.patch.dict(
            sys.modules,
            {
                "webull.data.common.category": SimpleNamespace(Category=fake_category),
                "webull.data.common.timespan": SimpleNamespace(Timespan=fake_timespan),
            },
        ):
            sma = api.sma_trend(["NVDA"], days=2)

        self.assertEqual(sma, {"NVDA": 90.0})


class StallExitPriceSpreadSanityTests(unittest.TestCase):
    """Live incident: TBB (1 share, cost=19.42) sat with bid=19.39/
    ask=19.89 - a ~2.5% spread, well past stock_entry_max_spread_percent's
    default 0.50%. The bid never cleared cost+min_profit+fee, so
    _stall_exit_price fell back to resting a passive SELL at the ask -
    but the ask was far above where TBB was actually trading (prints at
    19.41), so the order sat until order_timeout_seconds, got cancelled,
    and boost_stalled_positions resubmitted the identical unreachable
    limit next stall cycle. Forever, without ever filling. The fix:
    _stall_exit_price now refuses the ask fallback when the spread itself
    is wider than the same bound entries are held to, and simply waits
    (no order submitted this cycle) instead of spinning on a doomed one.
    """

    @staticmethod
    def _price_fn():
        from webull_bot.bot import AutoTrader

        from webull_bot.webull_api import WebullAPI

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(stock_entry_max_spread_percent=Decimal("0.50")),
            api=SimpleNamespace(
                quote_bid=lambda q: Decimal(str(q["bid"])) if q.get("bid") else None,
                quote_ask=lambda q: Decimal(str(q["ask"])) if q.get("ask") else None,
                quote_price=WebullAPI.quote_price,
                price_tick_size=WebullAPI.price_tick_size,
            ),
        )
        return AutoTrader._stall_exit_price.__get__(fake_bot)

    def test_refuses_to_rest_at_an_unreachable_ask_on_a_wide_spread(self):
        price_fn = self._price_fn()
        quote = {"bid": "19.39", "ask": "19.89"}
        result = price_fn(
            quote,
            average_cost=Decimal("19.42"),
            min_profit=Decimal("0.01"),
            fee_per_share=Decimal("0.02"),
        )
        self.assertIsNone(result)

    def test_still_uses_the_ask_when_the_spread_is_tight(self):
        price_fn = self._price_fn()
        quote = {"bid": "19.40", "ask": "19.48"}
        result = price_fn(
            quote,
            average_cost=Decimal("19.42"),
            min_profit=Decimal("0.01"),
            fee_per_share=Decimal("0.02"),
        )
        self.assertEqual(result, Decimal("19.48"))

    def test_bid_alone_clearing_the_floor_is_unaffected_by_the_spread_check(self):
        price_fn = self._price_fn()
        quote = {"bid": "19.89", "ask": "20.50"}
        result = price_fn(
            quote,
            average_cost=Decimal("19.42"),
            min_profit=Decimal("0.01"),
            fee_per_share=Decimal("0.02"),
        )
        self.assertEqual(result, Decimal("19.89"))

    def test_a_wider_explicit_max_spread_percent_allows_the_ask_fallback(self):
        """Live incident: GAUZ routinely quoted 2-7% spreads (its normal
        character, not a glitch) - the volatility-scalp cohort's own
        exit pricing passes a much wider max_spread_percent explicitly
        instead of falling back to the tight default, so the ask
        fallback stays usable on exactly the wide-spread names this
        cohort exists to trade.
        """
        price_fn = self._price_fn()
        # (19.89 - 19.39) / 19.39 * 100 = 2.58% - would be refused under
        # the default 0.50% bound (see test_refuses_to_rest_at_an_
        # unreachable_ask_on_a_wide_spread above), but is allowed here.
        quote = {"bid": "19.39", "ask": "19.89"}
        result = price_fn(
            quote,
            average_cost=Decimal("19.42"),
            min_profit=Decimal("0.01"),
            fee_per_share=Decimal("0.02"),
            max_spread_percent=Decimal("8"),
        )
        self.assertEqual(result, Decimal("19.89"))

    def test_caps_the_ask_fallback_near_the_last_trade_not_the_top_of_spread(self):
        """By request: "you cannot always go to the top of the spread
        when it is big, you must ask a reasonable price, not too far
        from the last [trade]." The spread here (2.58%) passes the
        wider explicit bound, and the raw ask alone would clear the
        floor - but real prints are at 19.41, far below the 19.89 ask,
        so resting there wouldn't reflect where the stock is actually
        trading. Caps at last_price * (1 + max_spread_percent/200).
        """
        price_fn = self._price_fn()
        # average_cost=19.42 keeps this consistent with the sibling
        # tests above: bid (19.39) alone does NOT clear the floor
        # (19.45), so this actually reaches the ask-fallback/cap logic
        # rather than returning the bid immediately.
        quote = {"bid": "19.39", "ask": "19.89", "price": "19.41"}
        result = price_fn(
            quote,
            average_cost=Decimal("19.42"),
            min_profit=Decimal("0.01"),
            fee_per_share=Decimal("0.02"),
            max_spread_percent=Decimal("8"),
        )
        # cap = 19.41 * 1.04 = 20.1864 -> quantized down to 20.18, still
        # above the raw ask (19.89) here, so this specific cap doesn't
        # bind - confirms the cap tracks the last price, not a fixed
        # ceiling, and doesn't wrongly reject a reachable ask.
        self.assertEqual(result, Decimal("19.89"))

    def test_cap_can_push_the_price_below_the_floor_and_skip_the_cycle(self):
        """When the ask sits far above the last print (a truly stale/
        unrealistic quote) even within the allowed spread, the capped
        price can end up below the profit floor - correctly skips
        rather than resting at an unrealistic price.
        """
        price_fn = self._price_fn()
        # ask=19.89 alone would clear a floor of ~19.33, but the last
        # print is only 19.00 - cap = 19.00 * 1.04 = 19.76, still above
        # floor here, so use a tighter floor scenario instead: push
        # average_cost up so only the raw ask (not the capped price)
        # would clear it.
        quote = {"bid": "19.39", "ask": "19.89", "price": "19.00"}
        result = price_fn(
            quote,
            average_cost=Decimal("19.80"),
            min_profit=Decimal("0.01"),
            fee_per_share=Decimal("0.02"),
            max_spread_percent=Decimal("8"),
        )
        # floor = 19.80 + 0.01 + 0.02 = 19.83. Raw ask (19.89) alone
        # would clear it, but the cap (19.00 * 1.04 = 19.76) does not -
        # must skip, not rest at an unrealistic price just because the
        # raw ask alone would have looked profitable.
        self.assertIsNone(result)


class StallBreakerWideSpreadResubmitTests(unittest.TestCase):
    def test_boost_stalled_positions_does_not_resubmit_at_an_unfillable_ask(self):
        from webull_bot.bot import AutoTrader

        calls = []

        class FakeApi:
            def stock_quotes_resilient(self, symbols, category):
                calls.extend(symbols)
                return [
                    {"symbol": s, "bid": "19.39", "ask": "19.89"} for s in symbols
                ], set()

            @staticmethod
            def quote_bid(q):
                return Decimal(str(q["bid"]))

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            def place_stock(self, *a, **k):
                raise AssertionError(
                    "must not rest a limit at an ask far above the last "
                    "trade price on a wide-spread, illiquid symbol"
                )

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                stall_breaker_enabled=True,
                stall_breaker_seconds=1,
                stall_breaker_min_profit=Decimal("0.01"),
                sell_fee_dollars=Decimal("0.02"),
                stock_entry_max_spread_percent=Decimal("0.50"),
            ),
            api=FakeApi(),
            stock_categories={},
            last_trade={},
            last_stall_boost=0.0,
            pending_stock_exits=set(),
            pending_option_exits=set(),
        )
        fake_bot.cooldown_ready = lambda key: True
        fake_bot.has_pending_sell_order = lambda key: False
        fake_bot.is_fractional_quantity = AutoTrader.is_fractional_quantity
        fake_bot._stall_equity_quotes = AutoTrader._stall_equity_quotes.__get__(fake_bot)
        fake_bot._stall_exit_price = AutoTrader._stall_exit_price.__get__(fake_bot)
        boost = AutoTrader.boost_stalled_positions.__get__(fake_bot)
        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "TBB",
                "quantity": "1",
                "cost_price": "19.42",
            }
        ]
        boost(positions, options_active=False, core_session_active=True)
        self.assertEqual(calls, ["TBB"])
        self.assertNotIn("TBB", fake_bot.pending_stock_exits)

    def test_boost_stalled_positions_downgrades_invalid_symbol_to_a_warning(self):
        """Live incident (BA): a 2-contract OPTION position's own
        top-level "symbol" field came back from Webull's positions()
        as "2BA260925C00220000" - the quantity itself prefixed onto
        the real OCC symbol. contract_from_position trusted it and the
        quote endpoint correctly rejected the malformed string, but
        that exception escaped uncaught as a raw ERROR every stall
        cycle. Recognized broker rejection code, same downgrade-and-
        move-on convention as every other classified rejection this
        session.
        """
        from webull_bot.bot import AutoTrader

        class FakeApi:
            @staticmethod
            def contract_from_position(position):
                return {"symbol": "2BA260925C00220000"}

            @staticmethod
            def option_quote(symbol):
                raise RuntimeError(
                    "HTTP Status: 417, Code: INVALID_SYMBOL, Msg: "
                    f"Invalid Symbol:[{symbol}]."
                )

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                stall_breaker_enabled=True,
                stall_breaker_seconds=1,
                stall_breaker_min_profit=Decimal("0.01"),
                sell_fee_dollars=Decimal("0.02"),
            ),
            api=FakeApi(),
            stock_categories={},
            last_trade={},
            last_stall_boost=0.0,
            pending_stock_exits=set(),
            pending_option_exits=set(),
        )
        fake_bot.cooldown_ready = lambda key: True
        fake_bot.has_pending_sell_order = lambda key: False
        fake_bot._stall_equity_quotes = AutoTrader._stall_equity_quotes.__get__(fake_bot)
        boost = AutoTrader.boost_stalled_positions.__get__(fake_bot)
        positions = [
            {
                "instrument_type": "OPTION",
                "symbol": "2BA260925C00220000",
                "quantity": "2",
                "cost_price": "2.20",
            }
        ]

        with self.assertLogs("webull-bot", level="WARNING") as logs:
            boost(positions, options_active=True, core_session_active=True)

        self.assertTrue(
            any("unresolvable option symbol" in message for message in logs.output)
        )
        self.assertFalse(any(r.levelname == "ERROR" for r in logs.records))


def _fake_webull_api(**config_overrides):
    """A SimpleNamespace standing in for WebullAPI, with the real
    _quote_decimal/_sane_bid_or_ask implementations bound to it - lets
    tests exercise the real pricing methods (stock_limit_price etc.)
    without a real WebullAPI/SDK instance. _sane_bid_or_ask needs self
    (it reads self.config and calls self._quote_decimal), so it's bound
    via __get__ after fake_api exists rather than assigned directly like
    _quote_decimal (a plain @staticmethod, callable either way).
    """
    config = SimpleNamespace(
        quote_price_sanity_percent=Decimal("0.08"), **config_overrides
    )
    fake_api = SimpleNamespace(
        config=config,
        _quote_decimal=WebullAPI._quote_decimal,
        price_tick_size=WebullAPI.price_tick_size,
    )
    fake_api._sane_bid_or_ask = WebullAPI._sane_bid_or_ask.__get__(fake_api)
    fake_api._bid_ask_last_midpoint = WebullAPI._bid_ask_last_midpoint.__get__(fake_api)
    return fake_api


class PriceTickSizeTests(unittest.TestCase):
    """By request: smaller/cheaper stocks quote with real sub-penny
    precision (a live GAUZ quote showed bid=0.4592) - blanket-rounding
    every computed price to whole cents threw away up to a cent of real
    value per share on exactly the stocks where a cent is a meaningful
    fraction of the price.
    """

    def test_under_a_dollar_uses_sub_penny_precision(self):
        self.assertEqual(
            WebullAPI.price_tick_size(Decimal("0.4592")), Decimal("0.0001")
        )

    def test_a_dollar_or_more_uses_whole_cents(self):
        self.assertEqual(WebullAPI.price_tick_size(Decimal("1.00")), Decimal("0.01"))
        self.assertEqual(WebullAPI.price_tick_size(Decimal("20.15")), Decimal("0.01"))

    def test_stock_limit_price_keeps_sub_penny_precision_under_a_dollar(self):
        fake_api = _fake_webull_api(stock_limit_offset=Decimal("0"))
        price_fn = WebullAPI.stock_limit_price.__get__(fake_api)
        # BUY uses the bid/ask/last midpoint = (0.4592 + 0.4839) / 2 =
        # 0.47155, rounded DOWN to the nearest 0.0001 = 0.4715 - not
        # flattened to a whole cent (0.47).
        quote = {"bid": "0.4592", "ask": "0.4839"}
        self.assertEqual(price_fn(quote, "BUY"), Decimal("0.4715"))

    def test_stall_exit_price_keeps_sub_penny_precision_under_a_dollar(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(stock_entry_max_spread_percent=Decimal("5")),
            api=SimpleNamespace(
                quote_bid=lambda q: Decimal(str(q["bid"])),
                quote_ask=lambda q: Decimal(str(q["ask"])),
                price_tick_size=WebullAPI.price_tick_size,
            ),
        )
        price_fn = AutoTrader._stall_exit_price.__get__(fake_bot)
        # bid 0.4592 clears cost(0.41) + min_profit(0.01) + fee(0.001) -
        # must return the real bid, not a cent-flattened 0.45.
        result = price_fn(
            {"bid": "0.4592", "ask": "0.4839"},
            average_cost=Decimal("0.41"),
            min_profit=Decimal("0.01"),
            fee_per_share=Decimal("0.001"),
        )
        self.assertEqual(result, Decimal("0.4592"))


class ShortPricingTests(unittest.TestCase):
    def test_short_entry_uses_passive_mid_price(self):
        fake_api = _fake_webull_api(stock_limit_offset=Decimal("0.005"))
        price_fn = WebullAPI.stock_limit_price.__get__(fake_api)
        quote = {"bid": "10.00", "ask": "10.10"}
        self.assertEqual(price_fn(quote, "SHORT"), Decimal("10.05"))

    def test_cover_crosses_above_the_ask(self):
        fake_api = _fake_webull_api(stock_limit_offset=Decimal("0.01"))
        price_fn = WebullAPI.stock_limit_price.__get__(fake_api)
        quote = {"bid": "10.00", "ask": "10.10", "price": "10.05"}
        # 10.10 * 1.01 = 10.2010, quantized up to the next cent = 10.21.
        self.assertEqual(price_fn(quote, "COVER"), Decimal("10.21"))

    def test_sell_side_falls_back_to_price_when_bid_is_a_broken_quote(self):
        """Regression test for a live incident: FPE's ask sat at $20.08
        while its last-trade price stayed ~$17.7-17.8 for hours, and every
        exit-pricing path that trusted the raw ask submitted a limit order
        that could never fill. bid/ask readers must fall through to the
        quote's own last-trade price instead of trusting a bid/ask that
        diverges implausibly from it.
        """
        fake_api = _fake_webull_api(stock_limit_offset=Decimal("0.01"))
        price_fn = WebullAPI.stock_limit_price.__get__(fake_api)
        # bid (2.00) is wildly below price (10.00) - an insane read, not a
        # real market. Falls through to price, not the broken bid.
        quote = {"bid": "2.00", "ask": "10.10", "price": "10.00"}
        # 10.00 * (1 - 0.01) = 9.90.
        self.assertEqual(price_fn(quote, "SELL"), Decimal("9.90"))

    def test_cover_falls_back_to_price_when_ask_is_a_broken_quote(self):
        fake_api = _fake_webull_api(stock_limit_offset=Decimal("0.01"))
        price_fn = WebullAPI.stock_limit_price.__get__(fake_api)
        # ask (20.08) is wildly above price (17.81) - the exact FPE shape.
        quote = {"bid": "17.70", "ask": "20.08", "price": "17.81"}
        # 17.81 * 1.01 = 17.9881, quantized up to the next cent = 17.99.
        self.assertEqual(price_fn(quote, "COVER"), Decimal("17.99"))

    def test_quote_ask_returns_none_for_a_quote_that_diverges_from_price(self):
        fake_api = _fake_webull_api()
        ask_fn = WebullAPI.quote_ask.__get__(fake_api)
        self.assertIsNone(ask_fn({"ask": "20.08", "price": "17.81"}))

    def test_quote_bid_returns_none_for_a_quote_that_diverges_from_price(self):
        fake_api = _fake_webull_api()
        bid_fn = WebullAPI.quote_bid.__get__(fake_api)
        self.assertIsNone(bid_fn({"bid": "16.11", "price": "17.81"}))

    def test_quote_ask_passes_through_a_normal_quote(self):
        fake_api = _fake_webull_api()
        ask_fn = WebullAPI.quote_ask.__get__(fake_api)
        self.assertEqual(ask_fn({"ask": "17.85", "price": "17.81"}), Decimal("17.85"))
