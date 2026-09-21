import logging
import shutil
import unittest
import unittest.mock
from collections import deque
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from webull_bot.webull_api import WebullAPI


class PrepareOptionScanBatchHeldPositionTests(unittest.TestCase):
    """By request: "why is UBER not averaging down." Live incident: a
    restart re-discovered a DIFFERENT UBER strike than the one
    actually held (the underlying's price had moved), and since
    discover_option_contracts treats "any contract for this
    underlying" as already-discovered, the originally-held strike
    never got added back - trade_options' whole per-contract loop
    only ever iterates self.option_contracts, so the held position
    became completely invisible to PROFIT/LOSS/averaging-down checks.
    _prepare_option_scan_batch now backfills any held OPTION
    position's contract via contract_from_position, and prioritizes
    held contracts into this cycle's batch ahead of the rotation.
    """

    def _fake_bot(self, option_contracts, held_position, backfilled_contract):
        from webull_bot.strategy_logic.market_state.snapshot import rotating_batch

        underlying_quote = {"symbol": "UBER", "price": "50.00"}

        class FakeApi:
            @staticmethod
            def stock_quotes_resilient(symbols, category):
                return ([underlying_quote], set())

            @staticmethod
            def quote_price(quote):
                return Decimal(str(quote["price"]))

            @staticmethod
            def option_quotes(symbols):
                return []

            @staticmethod
            def contract_from_position(position):
                return backfilled_contract

        fake_bot = SimpleNamespace(
            option_contracts=list(option_contracts),
            option_cursor=0,
            vixy_history=deque(maxlen=30),
            api=FakeApi(),
            strategy=SimpleNamespace(
                open_position_count=lambda positions: 1,
                option_direction_signal=lambda key, price: "HOLD",
                rotating_batch=rotating_batch,
            ),
            stop_loss_guard_active=lambda: False,
            config=SimpleNamespace(option_batch_size=20),
        )
        return fake_bot, [held_position]

    def test_backfills_a_held_contract_missing_from_discovery(self):
        from webull_bot.bot import AutoTrader

        held_contract = {
            "symbol": "UBER260925C00075000",
            "underlying_symbol": "UBER",
            "strike_price": "75",
            "expiration_date": "2026-09-25",
            "option_type": "CALL",
        }
        other_contract = {
            "symbol": "UBER260925C00073000",
            "underlying_symbol": "UBER",
            "strike_price": "73",
            "expiration_date": "2026-09-25",
            "option_type": "CALL",
        }
        held_position = {
            "instrument_type": "OPTION",
            "symbol": "UBER260925C00075000",
            "quantity": "1",
            "cost_price": "1.30",
        }
        fake_bot, positions = self._fake_bot(
            [other_contract], held_position, held_contract
        )
        prepare = AutoTrader._prepare_option_scan_batch.__get__(fake_bot)

        prepare(positions)

        symbols = {c["symbol"] for c in fake_bot.option_contracts}
        self.assertIn("UBER260925C00075000", symbols)

    def test_held_contract_wins_a_batch_slot_over_rotation(self):
        from webull_bot.bot import AutoTrader

        held_contract = {
            "symbol": "UBER260925C00075000",
            "underlying_symbol": "UBER",
            "strike_price": "75",
            "expiration_date": "2026-09-25",
            "option_type": "CALL",
        }
        # Fill the rest of the pool with far more than option_batch_
        # size (20) unrelated contracts, so the held one would have
        # near-zero chance of winning the rotation on luck alone if
        # it weren't explicitly prioritized.
        crowd = [
            {
                "symbol": f"XYZ{i}250101C00100000",
                "underlying_symbol": f"XYZ{i}",
                "strike_price": "100",
                "expiration_date": "2026-01-01",
                "option_type": "CALL",
            }
            for i in range(50)
        ]
        held_position = {
            "instrument_type": "OPTION",
            "symbol": "UBER260925C00075000",
            "quantity": "1",
            "cost_price": "1.30",
        }
        fake_bot, positions = self._fake_bot(
            [held_contract] + crowd, held_position, held_contract
        )
        prepare = AutoTrader._prepare_option_scan_batch.__get__(fake_bot)

        result = prepare(positions)

        batch = result[3]
        batch_symbols = {c["symbol"] for c in batch}
        self.assertIn("UBER260925C00075000", batch_symbols)

    def test_matches_a_position_whose_symbol_is_the_bare_underlying_via_legs(self):
        """Live incident ("check uber now"): Webull's positions()
        response puts the UNDERLYING symbol (e.g. "UBER"), not the
        full OCC contract symbol, in a position's top-level "symbol"
        field - the real contract identity lives in position["legs"].
        Comparing the bare symbol directly against contract dicts'
        "symbol" field (as an earlier version of this code did) can
        never match, silently making the whole held-position
        guarantee (and the backfill dedup) a no-op. This is the
        already-discovered (no contract_from_position call needed)
        case - matched purely via legs against self.option_contracts.
        """
        from webull_bot.bot import AutoTrader

        known_contract = {
            "symbol": "UBER260925C00075000",
            "underlying_symbol": "UBER",
            "strike_price": "75",
            "expiration_date": "2026-09-25",
            "option_type": "CALL",
        }
        held_position = {
            "instrument_type": "OPTION",
            "symbol": "UBER",
            "quantity": "1",
            "cost_price": "1.30",
            "legs": [
                {
                    "symbol": "UBER",
                    "option_type": "CALL",
                    "option_expire_date": "2026-09-25",
                    "option_exercise_price": "75",
                }
            ],
        }
        fake_bot, positions = self._fake_bot(
            [known_contract], held_position, None
        )
        prepare = AutoTrader._prepare_option_scan_batch.__get__(fake_bot)

        result = prepare(positions)

        batch = result[3]
        batch_symbols = {c["symbol"] for c in batch}
        self.assertIn("UBER260925C00075000", batch_symbols)
        # Already known via legs - contract_from_position (which can
        # make a real API call) must never be reached, and nothing
        # gets appended a second time.
        self.assertEqual(fake_bot.option_contracts, [known_contract])


class OptionContractsStateStoreTests(unittest.TestCase):
    """By request: "is there a way to save these option contracts" -
    discover_option_contracts used to rebuild self.option_contracts
    from scratch, in memory only, every process start.
    """

    def _store(self, path):
        from webull_bot.option_contracts_state import OptionContractsStateStore

        return OptionContractsStateStore(str(path), logging.getLogger("test-opt-state"))

    def test_save_then_load_round_trips_contracts_and_attempted(self):
        path = Path("tests/.generated_option_state/roundtrip.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            from datetime import date

            store = self._store(path)
            future = (date.today() + timedelta(days=20)).isoformat()
            contracts = [
                {
                    "symbol": "XYZ260101C00100000",
                    "underlying_symbol": "XYZ",
                    "strike_price": "100",
                    "expiration_date": future,
                    "option_type": "CALL",
                }
            ]
            store.save(contracts, {"XYZ", "ABC"})
            loaded_contracts, loaded_attempted, loaded_averaging = (
                self._store(path).load()
            )
            self.assertEqual(loaded_contracts, contracts)
            self.assertEqual(loaded_attempted, {"XYZ", "ABC"})
            self.assertEqual(loaded_averaging, {})
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_load_drops_already_expired_contracts(self):
        path = Path("tests/.generated_option_state/expired.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            from datetime import date

            store = self._store(path)
            past = (date.today() - timedelta(days=1)).isoformat()
            future = (date.today() + timedelta(days=20)).isoformat()
            store.save(
                [
                    {
                        "symbol": "OLD",
                        "expiration_date": past,
                    },
                    {
                        "symbol": "STILL_GOOD",
                        "expiration_date": future,
                    },
                ],
                set(),
            )
            loaded_contracts, _, _ = self._store(path).load()
            self.assertEqual([c["symbol"] for c in loaded_contracts], ["STILL_GOOD"])
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_load_with_no_file_yet_returns_empty(self):
        path = Path("tests/.generated_option_state/missing.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        contracts, attempted, averaging = self._store(path).load()
        self.assertEqual(contracts, [])
        self.assertEqual(attempted, set())
        self.assertEqual(averaging, {})

    def test_save_then_load_round_trips_averaging_down_state(self):
        path = Path("tests/.generated_option_state/averaging.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            store = self._store(path)
            store.save(
                [],
                set(),
                {"XYZ260101C00100000": {"count": 1, "last_buy_price": Decimal("0.85")}},
            )
            _, _, loaded_averaging = self._store(path).load()
            self.assertEqual(
                loaded_averaging,
                {
                    "XYZ260101C00100000": {
                        "count": 1,
                        "last_buy_price": Decimal("0.85"),
                    }
                },
            )
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_load_drops_a_zero_count_averaging_entry(self):
        path = Path("tests/.generated_option_state/averaging_zero.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            store = self._store(path)
            store.save(
                [],
                set(),
                {"XYZ260101C00100000": {"count": 0, "last_buy_price": Decimal("0.85")}},
            )
            _, _, loaded_averaging = self._store(path).load()
            self.assertEqual(loaded_averaging, {})
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)


class SelectAtmOptionsAffordabilityTests(unittest.TestCase):
    """WebullAPI.select_atm_options's affordability shortlist - by
    request: "look for cheaper options to buy in to." Always picking
    the single nearest-to-ATM strike is right for delta but often
    unaffordable outright on a small account (option_order_quantity
    then silently rounds to 0 contracts). When max_contract_cost is
    given, this should quote a shortlist of near-the-money strikes and
    pick the closest-to-ATM one that still fits, rather than always
    the true ATM strike regardless of premium.
    """

    @staticmethod
    def _contract(symbol, strike, expiration, option_type="CALL"):
        return {
            "symbol": symbol,
            "strike_price": str(strike),
            "expiration_date": expiration,
            "option_type": option_type,
            "tradable_status": "OC",
        }

    def _fake_api(
        self,
        contracts,
        quotes_by_symbol,
        shortlist_size=6,
        volumes_by_symbol=None,
        moneyness_percent=Decimal("1"),
        min_premium_dollars=Decimal("0"),
    ):
        from datetime import date, timedelta

        from webull_bot.webull_api import WebullAPI

        volumes_by_symbol = volumes_by_symbol or {}
        expiration = (date.today() + timedelta(days=20)).isoformat()
        contract_list = [
            self._contract(symbol, strike, expiration)
            for symbol, strike in contracts
        ]

        def _quote(s):
            quote = {"symbol": s, "ask": str(quotes_by_symbol[s])}
            if s in volumes_by_symbol:
                quote["volume"] = str(volumes_by_symbol[s])
            return quote

        fake_api = SimpleNamespace(
            config=SimpleNamespace(
                option_min_dte=7,
                option_max_dte=45,
                option_type="CALL",
                option_affordability_shortlist_size=shortlist_size,
                # These tests exercise affordability-fallback
                # mechanics specifically, not the moneyness cap -
                # effectively unbounded here so existing strikes
                # (up to 20% away) aren't excluded before reaching
                # the logic under test. See the dedicated moneyness
                # cap tests below for that behavior.
                option_max_moneyness_percent=moneyness_percent,
                # Same reasoning as moneyness_percent above - defaults
                # to unbounded (0) here so existing affordability-
                # mechanics tests aren't affected; see the dedicated
                # minimum-premium tests below for that behavior.
                option_min_premium_dollars=min_premium_dollars,
            ),
            option_contracts=lambda underlying: contract_list,
            option_quotes=lambda symbols: [
                _quote(s) for s in symbols if s in quotes_by_symbol
            ],
            quote_price=WebullAPI.quote_price,
        )
        fake_api._option_greek_field = lambda quote, fields: (
            WebullAPI._option_greek_field(fake_api, quote, fields)
        )
        fake_api.option_volume = lambda quote: WebullAPI.option_volume(
            fake_api, quote
        )
        return WebullAPI.select_atm_options.__get__(fake_api)

    def test_falls_back_to_pure_atm_pick_without_a_cost_cap(self):
        select = self._fake_api(
            contracts=[("XYZ260101C00095000", 95), ("XYZ260101C00100000", 100)],
            quotes_by_symbol={},
        )
        result = select("XYZ", Decimal("100"))
        self.assertEqual(result[0]["symbol"], "XYZ260101C00100000")

    def test_picks_the_nearest_affordable_strike_when_atm_is_too_expensive(self):
        # ATM (strike 100) premium is $8.00 -> $800/contract, unaffordable
        # on a ~$110 account. The next-nearest strike (105) is cheaper
        # ($1.00 -> $100/contract) and should be picked instead.
        select = self._fake_api(
            contracts=[
                ("XYZ260101C00100000", 100),
                ("XYZ260101C00105000", 105),
                ("XYZ260101C00110000", 110),
            ],
            quotes_by_symbol={
                "XYZ260101C00100000": "8.00",
                "XYZ260101C00105000": "1.00",
                "XYZ260101C00110000": "0.20",
            },
        )
        result = select("XYZ", Decimal("100"), max_contract_cost=Decimal("110"))
        self.assertEqual(result[0]["symbol"], "XYZ260101C00105000")

    def test_picks_the_cheapest_quoted_strike_when_nothing_fits(self):
        select = self._fake_api(
            contracts=[
                ("XYZ260101C00100000", 100),
                ("XYZ260101C00105000", 105),
            ],
            quotes_by_symbol={
                "XYZ260101C00100000": "8.00",
                "XYZ260101C00105000": "5.00",
            },
        )
        result = select("XYZ", Decimal("100"), max_contract_cost=Decimal("110"))
        self.assertEqual(result[0]["symbol"], "XYZ260101C00105000")

    def test_prefers_higher_volume_among_affordable_contracts_over_pure_atm_proximity(
        self,
    ):
        # By request: "look at contracts in those stocks with high
        # volume movement and volatility" - both 100 and 105 strikes
        # are affordable, but 105 has far more contract volume, so it
        # should win over the nearer-to-ATM 100 strike.
        select = self._fake_api(
            contracts=[
                ("XYZ260101C00100000", 100),
                ("XYZ260101C00105000", 105),
            ],
            quotes_by_symbol={
                "XYZ260101C00100000": "1.00",
                "XYZ260101C00105000": "1.00",
            },
            volumes_by_symbol={
                "XYZ260101C00100000": 50,
                "XYZ260101C00105000": 5000,
            },
        )
        result = select("XYZ", Decimal("100"), max_contract_cost=Decimal("110"))
        self.assertEqual(result[0]["symbol"], "XYZ260101C00105000")

    def test_atm_proximity_still_breaks_a_volume_tie(self):
        select = self._fake_api(
            contracts=[
                ("XYZ260101C00100000", 100),
                ("XYZ260101C00105000", 105),
            ],
            quotes_by_symbol={
                "XYZ260101C00100000": "1.00",
                "XYZ260101C00105000": "1.00",
            },
            volumes_by_symbol={
                "XYZ260101C00100000": 500,
                "XYZ260101C00105000": 500,
            },
        )
        result = select("XYZ", Decimal("100"), max_contract_cost=Decimal("110"))
        self.assertEqual(result[0]["symbol"], "XYZ260101C00100000")

    def test_falls_back_to_atm_pick_when_the_quote_batch_fails(self):
        from datetime import date, timedelta

        expiration = (date.today() + timedelta(days=20)).isoformat()
        fake_api = SimpleNamespace(
            config=SimpleNamespace(
                option_min_dte=7,
                option_max_dte=45,
                option_type="CALL",
                option_affordability_shortlist_size=6,
                option_max_moneyness_percent=Decimal("1"),
            ),
            option_contracts=lambda underlying: [
                self._contract("XYZ260101C00100000", 100, expiration),
                self._contract("XYZ260101C00105000", 105, expiration),
            ],
            option_quotes=unittest.mock.Mock(side_effect=RuntimeError("boom")),
        )
        from webull_bot.webull_api import WebullAPI

        select = WebullAPI.select_atm_options.__get__(fake_api)
        result = select("XYZ", Decimal("100"), max_contract_cost=Decimal("110"))
        self.assertEqual(result[0]["symbol"], "XYZ260101C00100000")

    def test_searches_further_otm_when_the_whole_near_atm_shortlist_is_unaffordable(
        self,
    ):
        # By request: "these are definitely not the most volatile
        # contracts, I know TSLA contracts move like crazy" - a high-
        # priced underlying's near-ATM strikes (100/105/110 here) can
        # ALL be unaffordable on a small account; the contract must
        # still be tradable, so this should walk further OTM (120)
        # instead of giving up on the unaffordable near-ATM cluster.
        select = self._fake_api(
            contracts=[
                ("XYZ260101C00100000", 100),
                ("XYZ260101C00105000", 105),
                ("XYZ260101C00110000", 110),
                ("XYZ260101C00120000", 120),
            ],
            quotes_by_symbol={
                "XYZ260101C00100000": "8.00",
                "XYZ260101C00105000": "7.00",
                "XYZ260101C00110000": "6.00",
                "XYZ260101C00120000": "1.00",
            },
            shortlist_size=3,
        )
        result = select("XYZ", Decimal("100"), max_contract_cost=Decimal("110"))
        self.assertEqual(result[0]["symbol"], "XYZ260101C00120000")

    def test_still_falls_back_to_cheapest_near_atm_when_nothing_anywhere_fits(self):
        select = self._fake_api(
            contracts=[
                ("XYZ260101C00100000", 100),
                ("XYZ260101C00105000", 105),
            ],
            quotes_by_symbol={
                "XYZ260101C00100000": "80.00",
                "XYZ260101C00105000": "70.00",
            },
        )
        result = select("XYZ", Decimal("100"), max_contract_cost=Decimal("10"))
        self.assertEqual(result[0]["symbol"], "XYZ260101C00105000")

    def test_moneyness_cap_excludes_a_strike_too_far_from_the_underlying(self):
        """By explicit request ("is something wrong with the options
        strategy... do research online to see what you are missing"):
        research confirmed buying cheap, far-OTM options is a well-
        documented small-account failure mode (the "lottery ticket"
        trap) - option_delta_ok was meant to guard against exactly
        this but turned out to be inert (Webull never returns delta
        on this account, so it always fails open). Moneyness is the
        reliable substitute: a strike more than option_max_moneyness_
        percent away from the underlying is excluded outright, even
        if it's the cheapest/most affordable one available.
        """
        select = self._fake_api(
            contracts=[
                ("XYZ260101C00100000", 100),  # ATM, unaffordable
                ("XYZ260101C00130000", 130),  # 30% OTM, cheap, affordable
            ],
            quotes_by_symbol={
                "XYZ260101C00100000": "8.00",
                "XYZ260101C00130000": "0.10",
            },
            moneyness_percent=Decimal("0.15"),
        )
        result = select("XYZ", Decimal("100"), max_contract_cost=Decimal("110"))
        # The far-OTM lottery ticket is affordable, but excluded by
        # the moneyness cap - falls back to the nearest (ATM) strike
        # instead of taking it, even though the ATM one doesn't fit
        # the cost cap either.
        self.assertEqual(result[0]["symbol"], "XYZ260101C00100000")

    def test_moneyness_cap_still_allows_an_affordable_strike_within_range(self):
        select = self._fake_api(
            contracts=[
                ("XYZ260101C00100000", 100),
                ("XYZ260101C00110000", 110),  # 10% OTM, within a 15% cap
            ],
            quotes_by_symbol={
                "XYZ260101C00100000": "8.00",
                "XYZ260101C00110000": "1.00",
            },
            moneyness_percent=Decimal("0.15"),
        )
        result = select("XYZ", Decimal("100"), max_contract_cost=Decimal("110"))
        self.assertEqual(result[0]["symbol"], "XYZ260101C00110000")

    def test_moneyness_cap_excludes_the_fallback_search_too(self):
        """The further-OTM affordability fallback search must also
        respect the moneyness cap, not just the primary shortlist -
        it used to have no distance ceiling at all, only a count cap.
        """
        select = self._fake_api(
            contracts=[
                ("XYZ260101C00100000", 100),  # ATM, unaffordable
                ("XYZ260101C00105000", 105),  # near, also unaffordable
                ("XYZ260101C00150000", 150),  # 50% OTM, cheap but excluded
            ],
            quotes_by_symbol={
                "XYZ260101C00100000": "8.00",
                "XYZ260101C00105000": "7.00",
                "XYZ260101C00150000": "0.05",
            },
            shortlist_size=2,
            moneyness_percent=Decimal("0.15"),
        )
        result = select("XYZ", Decimal("100"), max_contract_cost=Decimal("110"))
        batch_symbols = {"XYZ260101C00100000", "XYZ260101C00105000"}
        self.assertIn(result[0]["symbol"], batch_symbols)

    def test_minimum_premium_excludes_a_lottery_ticket_cheap_contract(self):
        """Live incident: real account data confirmed 15 of 19 recent
        exits were losses, every single one an option bought under
        $0.20/share, averaging -$3.90 against $1.02 average wins -
        cheap premiums swing 30-50%+ on routine noise. A too-cheap
        contract must be excluded outright, not merely deprioritized -
        even when it's the only "affordable" one on a small account.
        """
        select = self._fake_api(
            contracts=[
                ("XYZ260101C00100000", 100),  # ATM, unaffordable
                ("XYZ260101C00110000", 110),  # affordable but $0.10 - too cheap
            ],
            quotes_by_symbol={
                "XYZ260101C00100000": "8.00",
                "XYZ260101C00110000": "0.10",
            },
            min_premium_dollars=Decimal("0.50"),
        )
        result = select("XYZ", Decimal("100"), max_contract_cost=Decimal("110"))
        # The cheap contract is excluded by the premium floor - falls
        # back to the ATM strike instead, even though it doesn't fit
        # the cost cap either (better than a lottery ticket).
        self.assertEqual(result[0]["symbol"], "XYZ260101C00100000")

    def test_minimum_premium_still_allows_a_contract_above_the_floor(self):
        select = self._fake_api(
            contracts=[
                ("XYZ260101C00100000", 100),
                ("XYZ260101C00110000", 110),
            ],
            quotes_by_symbol={
                "XYZ260101C00100000": "8.00",
                "XYZ260101C00110000": "0.75",
            },
            min_premium_dollars=Decimal("0.50"),
        )
        result = select("XYZ", Decimal("100"), max_contract_cost=Decimal("110"))
        self.assertEqual(result[0]["symbol"], "XYZ260101C00110000")

    def test_minimum_premium_excludes_the_fallback_search_too(self):
        """The further-OTM affordability fallback search must also
        respect the premium floor, not just the primary shortlist."""
        select = self._fake_api(
            contracts=[
                ("XYZ260101C00100000", 100),
                ("XYZ260101C00105000", 105),
                ("XYZ260101C00150000", 150),  # affordable but $0.05 - too cheap
            ],
            quotes_by_symbol={
                "XYZ260101C00100000": "8.00",
                "XYZ260101C00105000": "7.00",
                "XYZ260101C00150000": "0.05",
            },
            shortlist_size=2,
            min_premium_dollars=Decimal("0.50"),
        )
        result = select("XYZ", Decimal("100"), max_contract_cost=Decimal("110"))
        batch_symbols = {"XYZ260101C00100000", "XYZ260101C00105000"}
        self.assertIn(result[0]["symbol"], batch_symbols)

    def test_nothing_clearing_both_floors_skips_this_underlying(self):
        """By explicit request ("still make sure to try and make
        profit, not sell a loss for a profit" / real account data
        confirming the lottery-ticket pattern): when NOTHING clears
        both the moneyness cap and the minimum premium, the correct
        outcome is skipping this underlying entirely this cycle - not
        the old unconditional last-resort fallback to an unquoted,
        possibly-far-too-cheap pool_sorted[0].
        """
        # Two candidates (not one) so this exercises the real
        # shortlist/quoting path instead of the single-candidate
        # shortcut (which skips affordability/premium checks
        # entirely).
        select = self._fake_api(
            contracts=[
                ("XYZ260101C00100000", 100),
                ("XYZ260101C00105000", 105),
            ],
            quotes_by_symbol={
                "XYZ260101C00100000": "0.10",
                "XYZ260101C00105000": "0.05",
            },
            min_premium_dollars=Decimal("0.50"),
        )
        with self.assertRaises(RuntimeError):
            select("XYZ", Decimal("100"), max_contract_cost=Decimal("110"))


class OptionPriceTickSizeTests(unittest.TestCase):
    """WebullAPI.option_price_tick_size / option_limit_price - live
    incident: a real order attempt at $7.47 (AAPL, premium >= $3) was
    rejected outright with OPENAPI_OPTION_PRICE_STEP_GTE ("Orders
    placed with a premium of $3 or more must be in increments of
    0.05"). option_limit_price used to always round to a flat $0.01
    regardless of premium level.

    Second live incident: OPENAPI_OPTION_PRICE_STEP_LT hit repeatedly
    on a different underlying (CD) - "Orders placed with a premium of
    less than $3 must be in increments of 0.05," directly contradicting
    the $0.01-below-$3 assumption a $3 threshold used to make (that
    finer grid only applies to Penny Pilot-enrolled underlyings, not
    every symbol). $0.05 is always valid on the $0.01 grid too, so the
    tick is now unconditionally $0.05 regardless of premium.
    """

    def test_tick_is_always_a_nickel_regardless_of_premium(self):
        self.assertEqual(
            WebullAPI.option_price_tick_size(Decimal("0.50")), Decimal("0.05")
        )
        self.assertEqual(
            WebullAPI.option_price_tick_size(Decimal("2.99")), Decimal("0.05")
        )
        self.assertEqual(
            WebullAPI.option_price_tick_size(Decimal("3.00")), Decimal("0.05")
        )
        self.assertEqual(
            WebullAPI.option_price_tick_size(Decimal("7.47")), Decimal("0.05")
        )

    def test_buy_limit_price_rounds_down_to_the_nearest_nickel_above_three(self):
        api = WebullAPI.__new__(WebullAPI)
        api.config = SimpleNamespace(
            option_limit_offset=Decimal("0.01"),
            quote_price_sanity_percent=Decimal("0.08"),
        )
        # Live incident's exact numbers: bid=7.35/ask=7.59 averages to
        # 7.47, which is NOT a multiple of 0.05 and was rejected live.
        price = api.option_limit_price(
            {"bid": "7.35", "ask": "7.59"}, "BUY"
        )
        self.assertEqual(price, Decimal("7.45"))
        self.assertEqual(price % Decimal("0.05"), Decimal("0"))

    def test_buy_limit_price_rounds_down_to_the_nearest_nickel_under_three(self):
        api = WebullAPI.__new__(WebullAPI)
        api.config = SimpleNamespace(
            option_limit_offset=Decimal("0.01"),
            quote_price_sanity_percent=Decimal("0.08"),
        )
        price = api.option_limit_price(
            {"bid": "1.20", "ask": "1.23"}, "BUY"
        )
        self.assertEqual(price, Decimal("1.20"))
        self.assertEqual(price % Decimal("0.05"), Decimal("0"))

    def test_sell_limit_price_rounds_down_to_the_nearest_nickel_above_three(self):
        api = WebullAPI.__new__(WebullAPI)
        api.config = SimpleNamespace(
            option_limit_offset=Decimal("0.01"),
            quote_price_sanity_percent=Decimal("0.08"),
        )
        price = api.option_limit_price(
            {"bid": "5.51", "price": "5.51"}, "SELL"
        )
        # base (5.51) * (1 - 1% offset) = 5.4549, rounded DOWN to the
        # nearest nickel = 5.45 - by request ("the exit price is not
        # competitive"): rounding UP here used to be able to push the
        # crossing price back ABOVE the original bid (see the
        # dedicated regression test below), defeating the whole point
        # of an aggressive-crossing exit price.
        self.assertEqual(price, Decimal("5.45"))
        self.assertLess(price, Decimal("5.51"))
        self.assertEqual(price % Decimal("0.05"), Decimal("0"))

    def test_sell_limit_price_never_rounds_back_above_the_bid(self):
        """Live incident: a $0.14 bid (AMC/SNAP-style cheap contract),
        3% below is 0.1358 - the old ROUND_UP quantization gave 0.15,
        ABOVE the original bid, turning an urgent stop-loss into a
        passive order that sat unfilled ("never filled (CANCELLED)"
        repeated live on AMC/SNAP/ORCL). The quantized SELL price must
        never exceed the bid it was crossing below.
        """
        api = WebullAPI.__new__(WebullAPI)
        api.config = SimpleNamespace(
            option_limit_offset=Decimal("0.03"),
            quote_price_sanity_percent=Decimal("0.08"),
        )
        price = api.option_limit_price({"bid": "0.14", "price": "0.14"}, "SELL")
        self.assertLessEqual(price, Decimal("0.14"))
        self.assertEqual(price, Decimal("0.10"))

    def test_buy_limit_price_rounds_to_the_nearest_nickel_not_always_down(self):
        """Live incident: bid=0.10/ask=0.15, a normal 33%-wide but
        genuinely liquid spread on a cheap contract - the true
        midpoint is 0.125. Always rounding DOWN collapsed this to
        0.10, i.e. the raw bid itself, not a real midpoint - by
        request ("the entry price is... not competitive"). Nearest-
        tick rounding keeps it representative of the actual midpoint.
        """
        api = WebullAPI.__new__(WebullAPI)
        api.config = SimpleNamespace(
            option_limit_offset=Decimal("0.03"),
            quote_price_sanity_percent=Decimal("0.08"),
        )
        price = api.option_limit_price({"bid": "0.10", "ask": "0.15"}, "BUY")
        self.assertEqual(price, Decimal("0.15"))
        self.assertGreater(price, Decimal("0.10"))
