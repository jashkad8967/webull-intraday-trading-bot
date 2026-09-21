import json
import logging
import sys
import time
import unittest
import unittest.mock
from datetime import datetime, timezone
from datetime import time as datetime_time
from decimal import Decimal
from types import SimpleNamespace

from webull_bot.config import Settings
from webull_bot.market_agent import MarketResearchAgent
from webull_bot.webull_api import WebullAPI

from support.fixtures import StrategyConfigMixin


class RefreshPremarketGainersTests(unittest.TestCase):
    """refresh_premarket_gainers - by request: "get the top gainers
    before the day starts and look to invest in that for quick
    profit." Webull's screener supports rank_type="PRE_MARKET"
    directly, distinct from the DAY_1/regular-session gainers already
    anonymously folded into the daily universe rebuild.
    """

    @staticmethod
    def _fake_bot(gainers_result=None, limit=50):
        calls = {"n": 0}

        def fake_safe_premarket_gainers(fetch_limit, page_size):
            calls["n"] += 1
            return gainers_result if gainers_result is not None else {}

        return (
            SimpleNamespace(
                premarket_gainers=set(),
                premarket_gainers_date=None,
                seed_popular_symbols=set(),
                stock_symbols=["AAPL", "MSFT"],
                stock_categories={"AAPL": "US_STOCK", "MSFT": "US_STOCK"},
                config=SimpleNamespace(
                    premarket_gainers_limit=limit,
                    stock_universe_page_size=200,
                ),
                safe_premarket_gainers=fake_safe_premarket_gainers,
            ),
            calls,
        )

    def test_fetches_and_merges_new_symbols_into_the_universe(self):
        from webull_bot.bot import AutoTrader

        fake_bot, calls = self._fake_bot(
            gainers_result={"NCPL": {}, "CHOW": {}}
        )
        refresh = AutoTrader.refresh_premarket_gainers.__get__(fake_bot)
        moment = datetime(2026, 8, 28)

        refresh(moment)

        self.assertEqual(fake_bot.premarket_gainers, {"NCPL", "CHOW"})
        self.assertTrue({"NCPL", "CHOW"} <= fake_bot.seed_popular_symbols)
        self.assertIn("NCPL", fake_bot.stock_symbols)
        self.assertIn("CHOW", fake_bot.stock_symbols)
        self.assertEqual(fake_bot.premarket_gainers_date, moment.date())
        self.assertEqual(calls["n"], 1)

    def test_does_not_duplicate_a_symbol_already_in_the_universe(self):
        from webull_bot.bot import AutoTrader

        fake_bot, _ = self._fake_bot(gainers_result={"AAPL": {}})
        refresh = AutoTrader.refresh_premarket_gainers.__get__(fake_bot)
        refresh(datetime(2026, 8, 28))

        self.assertEqual(fake_bot.stock_symbols.count("AAPL"), 1)

    def test_only_fetches_once_per_day(self):
        from webull_bot.bot import AutoTrader

        fake_bot, calls = self._fake_bot(gainers_result={"NCPL": {}})
        refresh = AutoTrader.refresh_premarket_gainers.__get__(fake_bot)
        moment = datetime(2026, 8, 28, 4, 0)

        refresh(moment)
        refresh(datetime(2026, 8, 28, 9, 0))

        self.assertEqual(calls["n"], 1)

    def test_refetches_on_a_new_day(self):
        from webull_bot.bot import AutoTrader

        fake_bot, calls = self._fake_bot(gainers_result={"NCPL": {}})
        refresh = AutoTrader.refresh_premarket_gainers.__get__(fake_bot)

        refresh(datetime(2026, 8, 28))
        refresh(datetime(2026, 8, 29))

        self.assertEqual(calls["n"], 2)

    def test_disabled_when_limit_is_zero(self):
        from webull_bot.bot import AutoTrader

        fake_bot, calls = self._fake_bot(gainers_result={"NCPL": {}}, limit=0)
        refresh = AutoTrader.refresh_premarket_gainers.__get__(fake_bot)

        refresh(datetime(2026, 8, 28))

        self.assertEqual(calls["n"], 0)
        self.assertEqual(fake_bot.premarket_gainers, set())

    def test_a_failed_screener_leaves_state_untouched(self):
        from webull_bot.bot import AutoTrader

        fake_bot, _ = self._fake_bot(gainers_result={})
        refresh = AutoTrader.refresh_premarket_gainers.__get__(fake_bot)

        refresh(datetime(2026, 8, 28))

        self.assertEqual(fake_bot.premarket_gainers, set())
        self.assertEqual(fake_bot.seed_popular_symbols, set())


class RefreshAgentPredictedGainersTests(unittest.TestCase):
    """refresh_agent_predicted_gainers - by request: "have the
    research agent return stocks likely to be top gainers before core
    hours start, then put those stocks in some sort of priority list
    to also look at." Complementary to refresh_premarket_gainers (the
    same priority-list mechanism, fed by the research agent instead of
    Webull's own screener).
    """

    @staticmethod
    def _fake_bot(predicted_symbols=None, agent=None):
        calls = {"n": 0}

        class FakeAgent:
            def predict_likely_gainers(self):
                calls["n"] += 1
                return predicted_symbols

        return (
            SimpleNamespace(
                agent_predicted_gainers=set(),
                agent_predicted_gainers_date=None,
                seed_popular_symbols=set(),
                stock_symbols=["AAPL", "MSFT"],
                stock_categories={"AAPL": "US_STOCK", "MSFT": "US_STOCK"},
                market_agent=FakeAgent() if agent is None else agent,
            ),
            calls,
        )

    def test_fetches_and_merges_new_symbols_into_the_universe(self):
        from webull_bot.bot import AutoTrader

        fake_bot, calls = self._fake_bot(predicted_symbols=["NCPL", "chow"])
        refresh = AutoTrader.refresh_agent_predicted_gainers.__get__(fake_bot)
        moment = datetime(2026, 8, 28)

        refresh(moment)

        self.assertEqual(fake_bot.agent_predicted_gainers, {"NCPL", "CHOW"})
        self.assertTrue({"NCPL", "CHOW"} <= fake_bot.seed_popular_symbols)
        self.assertIn("NCPL", fake_bot.stock_symbols)
        self.assertIn("CHOW", fake_bot.stock_symbols)
        self.assertEqual(fake_bot.agent_predicted_gainers_date, moment.date())
        self.assertEqual(calls["n"], 1)

    def test_only_fetches_once_per_day(self):
        from webull_bot.bot import AutoTrader

        fake_bot, calls = self._fake_bot(predicted_symbols=["NCPL"])
        refresh = AutoTrader.refresh_agent_predicted_gainers.__get__(fake_bot)

        refresh(datetime(2026, 8, 28, 4, 0))
        refresh(datetime(2026, 8, 28, 9, 0))

        self.assertEqual(calls["n"], 1)

    def test_refetches_on_a_new_day(self):
        from webull_bot.bot import AutoTrader

        fake_bot, calls = self._fake_bot(predicted_symbols=["NCPL"])
        refresh = AutoTrader.refresh_agent_predicted_gainers.__get__(fake_bot)

        refresh(datetime(2026, 8, 28))
        refresh(datetime(2026, 8, 29))

        self.assertEqual(calls["n"], 2)

    def test_disabled_when_agent_is_none(self):
        from webull_bot.bot import AutoTrader

        fake_bot, _ = self._fake_bot()
        fake_bot.market_agent = None
        refresh = AutoTrader.refresh_agent_predicted_gainers.__get__(fake_bot)

        refresh(datetime(2026, 8, 28))

        self.assertEqual(fake_bot.agent_predicted_gainers, set())
        self.assertEqual(fake_bot.agent_predicted_gainers_date, datetime(2026, 8, 28).date())

    def test_none_result_leaves_state_untouched(self):
        from webull_bot.bot import AutoTrader

        fake_bot, _ = self._fake_bot(predicted_symbols=None)
        refresh = AutoTrader.refresh_agent_predicted_gainers.__get__(fake_bot)

        refresh(datetime(2026, 8, 28))

        self.assertEqual(fake_bot.agent_predicted_gainers, set())
        self.assertEqual(fake_bot.seed_popular_symbols, set())

    def test_agent_exception_is_swallowed(self):
        from webull_bot.bot import AutoTrader

        class BrokenAgent:
            def predict_likely_gainers(self):
                raise RuntimeError("boom")

        fake_bot, _ = self._fake_bot(agent=BrokenAgent())
        refresh = AutoTrader.refresh_agent_predicted_gainers.__get__(fake_bot)

        refresh(datetime(2026, 8, 28))  # must not raise

        self.assertEqual(fake_bot.agent_predicted_gainers, set())


class PredictLikelyGainersTests(unittest.TestCase):
    """MarketResearchAgent.predict_likely_gainers - by request: "have
    the research agent return stocks likely to be top gainers before
    core hours start." One-shot per day, shares the same daily
    request/token budget as submit_strategy_review.
    """

    _FIXED_MOMENT = datetime(2026, 8, 30, 14, 0, tzinfo=timezone.utc)

    @classmethod
    def _fake_agent(cls, response_content, requests_today=0, token_usage=None):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            agent_daily_token_budget=100000,
            groq_model="openai/gpt-oss-120b",
            groq_reasoning_effort="low",
            market_open_time="04:00",
        )
        agent.config.session_time = (
            lambda value: datetime_time(*(int(p) for p in value.split(":")))
        )
        agent.log = logging.getLogger("test-agent")
        agent._requests_today = requests_today
        # Same session as _FIXED_MOMENT (both after 04:00 UTC-session-
        # open on the same calendar day) - so predict_likely_gainers'
        # own new-day reset never fires, and requests_today/token_usage
        # passed in above are exactly what the real check sees.
        agent._request_date = cls._FIXED_MOMENT.date()
        agent._limit_logged_date = None
        agent._rate_limit_blocked = False
        agent._token_limit_logged_at = 0.0
        agent._token_usage_log = list(token_usage or [])
        agent._timezone = timezone.utc

        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChoice:
            def __init__(self, content):
                self.message = FakeMessage(content)

        class FakeResponse:
            def __init__(self, content):
                self.choices = [FakeChoice(content)]
                self.usage = None

        def fake_create(**kwargs):
            return FakeResponse(response_content)

        agent.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
        )
        return agent

    def _predict(self, agent):
        with unittest.mock.patch(
            "webull_bot.market_agent.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = self._FIXED_MOMENT
            return agent.predict_likely_gainers()

    def test_parses_a_valid_symbol_list(self):
        agent = self._fake_agent('{"symbols":["AAPL","tsla"]}')
        result = self._predict(agent)
        self.assertEqual(result, ["AAPL", "TSLA"])
        self.assertEqual(agent._requests_today, 1)

    def test_caps_at_15_symbols(self):
        symbols = [f"SYM{i}" for i in range(20)]
        agent = self._fake_agent(json.dumps({"symbols": symbols}))
        result = self._predict(agent)
        self.assertEqual(len(result), 15)

    def test_invalid_json_returns_none_without_crashing(self):
        agent = self._fake_agent("not json")
        result = self._predict(agent)
        self.assertIsNone(result)

    def test_missing_symbols_key_returns_none(self):
        agent = self._fake_agent('{"other":[]}')
        result = self._predict(agent)
        self.assertIsNone(result)

    def test_skipped_when_daily_request_budget_exhausted(self):
        agent = self._fake_agent(
            '{"symbols":["AAPL"]}', requests_today=250
        )
        result = self._predict(agent)
        self.assertIsNone(result)
        self.assertEqual(agent._requests_today, 250)

    def test_skipped_when_rolling_token_budget_exhausted(self):
        agent = self._fake_agent(
            '{"symbols":["AAPL"]}',
            token_usage=[(time.monotonic(), 100000)],
        )
        result = self._predict(agent)
        self.assertIsNone(result)


class StockScreenerTests(StrategyConfigMixin, unittest.TestCase):
    def test_default_stock_capital_fractions_are_popular_penny_discovery(self):
        config = Settings()
        self.assertEqual(
            config.stock_capital_fractions(),
            {
                "POPULAR": Decimal("0.70"),
                "PENNY": Decimal("0.10"),
                "DISCOVERY": Decimal("0.20"),
            },
        )

    def test_screener_number_handles_bad_and_nonfinite_values(self):
        self.assertEqual(
            WebullAPI._screener_number({"market_value": "1.5e11"}, "market_value"),
            1.5e11,
        )
        self.assertEqual(
            WebullAPI._screener_number({"market_value": "nope"}, "market_value"),
            0.0,
        )
        self.assertEqual(
            WebullAPI._screener_number({}, "market_value"),
            0.0,
        )
        self.assertEqual(
            WebullAPI._screener_number({"volume": "inf"}, "volume"),
            0.0,
        )

    def test_top_active_stocks_pages_until_limit_or_data_runs_out(self):
        api = WebullAPI.__new__(WebullAPI)
        fake_category = SimpleNamespace(
            US_STOCK=SimpleNamespace(name="US_STOCK"),
        )
        pages = {
            1: [
                {"symbol": "big1", "market_value": "2e11", "volume": "1000"},
                {"symbol": "big2", "market_value": "1.5e11", "volume": "900"},
            ],
            2: [
                {"symbol": "small1", "market_value": "5e8", "volume": "500"},
            ],
        }
        calls = []

        def fake_call(callback, group):
            calls.append(group)
            return callback()

        def fake_get_most_active(**kwargs):
            return pages.get(kwargs["page_index"], [])

        api._call = fake_call
        api.data = SimpleNamespace(
            screener=SimpleNamespace(get_most_active=fake_get_most_active)
        )

        with unittest.mock.patch.dict(
            sys.modules,
            {"webull.data.common.category": SimpleNamespace(Category=fake_category)},
        ):
            universe = api.top_active_stocks(total_limit=10, page_size=2)

        self.assertEqual(set(universe), {"BIG1", "BIG2", "SMALL1"})
        self.assertEqual(universe["BIG1"]["market_value"], 2e11)
        self.assertEqual(universe["SMALL1"]["market_value"], 5e8)
        self.assertTrue(all(call == "market" for call in calls))

    def test_top_active_stocks_stops_at_requested_limit(self):
        api = WebullAPI.__new__(WebullAPI)
        fake_category = SimpleNamespace(
            US_STOCK=SimpleNamespace(name="US_STOCK"),
        )

        def fake_call(callback, group):
            return callback()

        def fake_get_most_active(**kwargs):
            size = kwargs["page_size"]
            index = kwargs["page_index"]
            return [
                {"symbol": f"s{index}-{i}", "market_value": "1e9"}
                for i in range(size)
            ]

        api._call = fake_call
        api.data = SimpleNamespace(
            screener=SimpleNamespace(get_most_active=fake_get_most_active)
        )

        with unittest.mock.patch.dict(
            sys.modules,
            {"webull.data.common.category": SimpleNamespace(Category=fake_category)},
        ):
            universe = api.top_active_stocks(total_limit=5, page_size=2)

        self.assertEqual(len(universe), 5)

    def test_top_gainers_pages_using_change_ratio_screener(self):
        api = WebullAPI.__new__(WebullAPI)
        fake_category = SimpleNamespace(US_STOCK=SimpleNamespace(name="US_STOCK"))
        calls = []

        def fake_call(callback, group):
            calls.append(group)
            return callback()

        def fake_get_gainers_losers(**kwargs):
            self.assertEqual(kwargs["sort_by"], "CHANGE_RATIO")
            self.assertEqual(kwargs["direction"], "DESC")
            if kwargs["page_index"] == 1:
                return [{"symbol": "mover1", "market_value": "5e9", "change_ratio": "12.5"}]
            return []

        api._call = fake_call
        api.data = SimpleNamespace(
            screener=SimpleNamespace(get_gainers_losers=fake_get_gainers_losers)
        )

        with unittest.mock.patch.dict(
            sys.modules,
            {"webull.data.common.category": SimpleNamespace(Category=fake_category)},
        ):
            gainers = api.top_gainers(total_limit=10, page_size=5)

        self.assertEqual(set(gainers), {"MOVER1"})
        self.assertEqual(gainers["MOVER1"]["change_ratio"], 12.5)
        self.assertTrue(all(call == "market" for call in calls))

    def test_top_losers_pages_using_ascending_change_ratio_screener(self):
        api = WebullAPI.__new__(WebullAPI)
        fake_category = SimpleNamespace(US_STOCK=SimpleNamespace(name="US_STOCK"))

        def fake_call(callback, group):
            return callback()

        def fake_get_gainers_losers(**kwargs):
            self.assertEqual(kwargs["sort_by"], "CHANGE_RATIO")
            self.assertEqual(kwargs["direction"], "ASC")
            if kwargs["page_index"] == 1:
                return [{"symbol": "dropper1", "market_value": "5e9", "change_ratio": "-8.2"}]
            return []

        api._call = fake_call
        api.data = SimpleNamespace(
            screener=SimpleNamespace(get_gainers_losers=fake_get_gainers_losers)
        )

        with unittest.mock.patch.dict(
            sys.modules,
            {"webull.data.common.category": SimpleNamespace(Category=fake_category)},
        ):
            losers = api.top_losers(total_limit=10, page_size=5)

        self.assertEqual(set(losers), {"DROPPER1"})
        self.assertEqual(losers["DROPPER1"]["change_ratio"], -8.2)

    def test_filter_with_popular_reinstated_keeps_configured_names(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)
        fake_bot.config = SimpleNamespace(
            popular_stocks=lambda: ["AAPL", "MSFT"],
        )
        # Simulate the volatility filter dropping the two well-known,
        # lower-amplitude names while keeping an obscure volatile one.
        fake_bot.filter_by_historical_volatility = lambda candidates: [
            symbol for symbol in candidates if symbol not in ("AAPL", "MSFT")
        ]
        reinstate = AutoTrader.filter_with_popular_reinstated.__get__(fake_bot)

        result = reinstate(["AAPL", "MSFT", "OBSCUREVOL", "OBSCUREFLAT"])

        self.assertEqual(
            result,
            ["AAPL", "MSFT", "OBSCUREVOL", "OBSCUREFLAT"],
        )

    def test_filter_with_popular_reinstated_does_not_add_unavailable_symbols(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)
        fake_bot.config = SimpleNamespace(
            popular_stocks=lambda: ["NOTINUNIVERSE"],
        )
        fake_bot.filter_by_historical_volatility = lambda candidates: list(candidates)
        reinstate = AutoTrader.filter_with_popular_reinstated.__get__(fake_bot)

        result = reinstate(["ONE", "TWO"])

        self.assertEqual(result, ["ONE", "TWO"])

    def test_safe_top_gainers_survives_screener_failure(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)

        def boom(*args, **kwargs):
            raise RuntimeError("Webull API error 500: boom")

        fake_bot.api = SimpleNamespace(top_gainers=boom)
        safe_call = AutoTrader.safe_top_gainers.__get__(fake_bot)

        self.assertEqual(safe_call(100, 50), {})

    def test_safe_top_losers_survives_screener_failure(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)

        def boom(*args, **kwargs):
            raise RuntimeError("Webull API error 500: boom")

        fake_bot.api = SimpleNamespace(top_losers=boom)
        safe_call = AutoTrader.safe_top_losers.__get__(fake_bot)

        self.assertEqual(safe_call(5, 5), {})

    def test_safe_market_pulse_active_falls_back_to_empty_not_prior_universe(self):
        """market_pulse must stay small on a screener failure, not balloon
        to the whole trading universe.
        """
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)

        def boom(*args, **kwargs):
            raise RuntimeError("Webull API error 503: boom")

        fake_bot.api = SimpleNamespace(top_active_stocks=boom)
        fake_bot.stock_symbols = ["OLD1", "OLD2", "OLD3"]
        safe_call = AutoTrader.safe_market_pulse_active.__get__(fake_bot)

        self.assertEqual(safe_call(5, 5), {})

    def test_market_pulse_entries_compacts_screener_rows(self):
        from webull_bot.bot import AutoTrader

        entries = AutoTrader._market_pulse_entries(
            {"NVDA": {"change_ratio": 0.125, "volume": 1_000_000}}
        )
        self.assertEqual(entries, [{"symbol": "NVDA", "chg": 12.5, "vol": 1_000_000}])

    def test_refresh_market_pulse_is_throttled_and_small(self):
        from webull_bot.bot import AutoTrader

        # A fixed monotonic clock, not the real one - refresh_market_pulse
        # compares elapsed-since-last-refresh against MARKET_PULSE_REFRESH_
        # SECONDS, and the real clock's absolute value depends on how long
        # the host has been up (not guaranteed to already be past that
        # threshold on every CI runner), same pattern used elsewhere in
        # this file for time.monotonic()-based throttles.
        now = 100_000.0

        fake_bot = AutoTrader.__new__(AutoTrader)
        fake_bot.config = SimpleNamespace(agent_market_pulse_symbols=2)
        fake_bot.last_market_pulse_refresh = now - 999
        fake_bot.market_pulse_cache = {"gainers": [], "losers": [], "most_active": []}
        fake_bot.strategy = SimpleNamespace(most_active_symbols=set())
        calls = []
        fake_bot.safe_top_gainers = lambda limit, page: (
            calls.append("gainers"),
            {"G1": {"change_ratio": 0.05, "volume": 100}},
        )[1]
        fake_bot.safe_top_losers = lambda limit, page: (
            calls.append("losers"),
            {"L1": {"change_ratio": -0.05, "volume": 200}},
        )[1]
        fake_bot.safe_market_pulse_active = lambda limit, page: (
            calls.append("most_active"),
            {"A1": {"change_ratio": 0.01, "volume": 300}},
        )[1]
        refresh = AutoTrader.refresh_market_pulse.__get__(fake_bot)

        with unittest.mock.patch("time.monotonic", return_value=now):
            refresh()
        self.assertEqual(sorted(calls), ["gainers", "losers", "most_active"])
        self.assertEqual(fake_bot.market_pulse_cache["gainers"][0]["symbol"], "G1")
        self.assertEqual(fake_bot.market_pulse_cache["losers"][0]["symbol"], "L1")
        self.assertEqual(fake_bot.market_pulse_cache["most_active"][0]["symbol"], "A1")
        # Regression coverage: safe_market_pulse_active returns a dict
        # keyed by symbol (not a list of {"symbol": ...} dicts) - reading
        # it the wrong way here would raise instead of silently no-op'ing,
        # since a bare string has no .get().
        self.assertEqual(fake_bot.strategy.most_active_symbols, {"A1"})

        # A second call within MARKET_PULSE_REFRESH_SECONDS makes no new
        # screener calls - this must stay off the ~4x/second poll loop.
        calls.clear()
        with unittest.mock.patch("time.monotonic", return_value=now):
            refresh()
        self.assertEqual(calls, [])

    def test_refresh_agent_discoveries_sources_from_market_pulse(self):
        """agent_popular_symbols must keep working from the deterministic
        screener data even when the research agent itself is disabled -
        that's the whole point of decoupling discovery from the LLM call.
        """
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)
        fake_bot.market_agent = None
        fake_bot.stock_symbols = ["NVDA", "TSLA", "AMD"]
        fake_bot.market_pulse_cache = {
            "gainers": [{"symbol": "NVDA", "chg": 5.0, "vol": 1}],
            "losers": [{"symbol": "TSLA", "chg": -3.0, "vol": 1}],
            "most_active": [{"symbol": "UNKNOWN", "chg": 0.1, "vol": 1}],
        }
        fake_bot.refresh_market_pulse = lambda: None
        refresh = AutoTrader.refresh_agent_discoveries.__get__(fake_bot)

        refresh()

        self.assertEqual(fake_bot.agent_popular_symbols, {"NVDA", "TSLA"})


class DiscoverOptionContractsCandidatePoolTests(unittest.TestCase):
    """By request: "we want options for more popular stocks only like
    in snp and dow" / "make sure the stocks selected for options are
    popular like snp500" / "we want options with volume and
    volatility to be chosen." discover_option_contracts now draws its
    candidate pool from config.option_candidates() (a curated large-
    cap list), not self.stock_symbols (which can be the entire scanned
    universe), and ranks that pool by volume + realized volatility
    before examining any of it each cycle.
    """

    @staticmethod
    def _fake_bot(
        candidates,
        metrics,
        volatility,
        discovery_order,
        agent_popular_symbols=frozenset(),
        agent_predicted_gainers=frozenset(),
    ):
        from webull_bot.bot import AutoTrader

        def select_atm_options(underlying, price, max_contract_cost=None):
            discovery_order.append(underlying)
            return []

        all_symbols = (
            set(candidates)
            | set(agent_popular_symbols)
            | set(agent_predicted_gainers)
        )
        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                option_candidates=lambda: candidates,
                option_discovery_seconds=0,
                option_discovery_per_cycle=len(all_symbols),
            ),
            invalid_symbols=set(),
            options_enabled=True,
            discover_all_options=True,
            last_option_discovery=0.0,
            option_discovery_cursor=0,
            option_discovery_attempted=set(),
            option_contracts=[],
            cached_option_buying_power=Decimal("100"),
            agent_popular_symbols=set(agent_popular_symbols),
            agent_predicted_gainers=set(agent_predicted_gainers),
            strategy=SimpleNamespace(
                metrics={
                    symbol: {"volume": volume}
                    for symbol, volume in metrics.items()
                },
                realized_volatility_percent=lambda symbol: volatility.get(symbol),
                prices={symbol: Decimal("10") for symbol in all_symbols},
            ),
            api=SimpleNamespace(select_atm_options=select_atm_options),
        )
        return AutoTrader.discover_option_contracts.__get__(fake_bot)

    def test_candidates_come_from_option_candidates_not_stock_symbols(self):
        """A symbol only present in self.stock_symbols (not in the
        curated option_candidates() list) must never be examined -
        confirmed by never assigning self.stock_symbols on the fixture
        at all, so any attempt to read it would raise AttributeError.
        """
        discovery_order = []
        discover = self._fake_bot(
            candidates=["AAPL", "MSFT"],
            metrics={"AAPL": 1_000_000, "MSFT": 500_000},
            volatility={},
            discovery_order=discovery_order,
        )

        discover()

        self.assertEqual(set(discovery_order), {"AAPL", "MSFT"})

    def test_higher_volume_is_examined_before_lower_volume(self):
        discovery_order = []
        discover = self._fake_bot(
            candidates=["LOWVOL", "HIGHVOL"],
            metrics={"LOWVOL": 100, "HIGHVOL": 10_000},
            volatility={},
            discovery_order=discovery_order,
        )

        discover()

        self.assertEqual(discovery_order, ["HIGHVOL", "LOWVOL"])

    def test_higher_volatility_breaks_a_volume_tie(self):
        discovery_order = []
        discover = self._fake_bot(
            candidates=["CALM", "CHOPPY"],
            metrics={"CALM": 1000, "CHOPPY": 1000},
            volatility={"CALM": Decimal("0.01"), "CHOPPY": Decimal("0.05")},
            discovery_order=discovery_order,
        )

        discover()

        self.assertEqual(discovery_order, ["CHOPPY", "CALM"])

    def test_a_never_scanned_symbol_is_still_examined_just_last(self):
        """No metrics/volatility yet (never scanned) sorts to the
        bottom of the priority ranking, but is NOT excluded from
        discovery entirely - it still gets examined this same cycle
        since option_discovery_per_cycle covers the whole small list.
        """
        discovery_order = []
        discover = self._fake_bot(
            candidates=["SCANNED", "UNSCANNED"],
            metrics={"SCANNED": 500},
            volatility={},
            discovery_order=discovery_order,
        )

        discover()

        self.assertEqual(discovery_order, ["SCANNED", "UNSCANNED"])

    def test_todays_top_gainers_are_included_alongside_the_curated_list(self):
        """By request: "look at the top gainers, most volatile,
        similar criteria for stocks, and then look the popular
        options contracts from those." agent_popular_symbols (today's
        actual top-gainer/most-active movers, from the same
        deterministic market_pulse screeners refresh_agent_
        discoveries already uses) is unioned into the candidate pool
        alongside the curated option_candidates() list, not limited
        to it.
        """
        discovery_order = []
        discover = self._fake_bot(
            candidates=["AAPL"],
            metrics={"AAPL": 100, "HOTMOVER": 100},
            volatility={},
            discovery_order=discovery_order,
            agent_popular_symbols={"HOTMOVER"},
        )

        discover()

        self.assertEqual(set(discovery_order), {"AAPL", "HOTMOVER"})

    def test_agent_predicted_gainers_are_included_alongside_the_curated_list(self):
        """By request: "perhaps the correct stocks are not being
        chosen, maybe we could ask the research agent to provide
        stocks to do analysis on." agent_predicted_gainers (the AI
        research agent's own daily pick list) already fed the STOCK
        universe but never the option candidate pool - now unioned
        in the same way agent_popular_symbols already was.
        """
        discovery_order = []
        discover = self._fake_bot(
            candidates=["AAPL"],
            metrics={"AAPL": 100, "AGENTPICK": 100},
            volatility={},
            discovery_order=discovery_order,
            agent_predicted_gainers={"AGENTPICK"},
        )

        discover()

        self.assertEqual(set(discovery_order), {"AAPL", "AGENTPICK"})
