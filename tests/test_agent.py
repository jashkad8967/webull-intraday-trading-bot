import json
import logging
import queue
import threading
import unittest
import unittest.mock
from datetime import datetime, timezone
from datetime import time as datetime_time
from decimal import Decimal
from types import SimpleNamespace

from webull_bot.market_agent import MarketResearchAgent


class AnalystDataServiceTests(unittest.TestCase):
    """Constructed via __new__ throughout - never calls __init__, so no
    real background thread ever starts in these tests. request()/
    snapshot() are the only two methods the main trading thread actually
    touches; _fetch() is exercised directly, standing in for what the
    (untested-here) worker thread does with a dequeued item.
    """

    def _service(self, **config_overrides):
        from webull_bot.analyst_data import AnalystDataService

        service = AnalystDataService.__new__(AnalystDataService)
        defaults = dict(
            analyst_priority_enabled=True,
            analyst_data_cache_seconds=43200,
            analyst_priority_bonus_max=Decimal("5"),
        )
        defaults.update(config_overrides)
        service.config = SimpleNamespace(**defaults)
        service.log = logging.getLogger("test-analyst-data")
        service._lock = threading.Lock()
        service._bonus = {}
        service._fetched_at = {}
        service._queued = set()
        service._queue = queue.Queue(maxsize=50)
        return service

    def test_request_enqueues_a_never_seen_symbol(self):
        service = self._service()
        service.request("AAPL", Decimal("100"))
        self.assertIn("AAPL", service._queued)
        self.assertEqual(service._queue.qsize(), 1)

    def test_request_enqueues_a_never_seen_symbol_even_when_monotonic_is_small(self):
        """Regression test: time.monotonic() is relative to an arbitrary
        reference point (often host boot), not the epoch - on a
        freshly-booted host it can be well under
        ANALYST_DATA_CACHE_SECONDS. A never-fetched symbol must still be
        eligible in that case; treating "never fetched" as "fetched at
        monotonic time zero" (a 0.0 default) silently broke every
        first-ever fetch on a fresh CI runner.
        """
        service = self._service()
        with unittest.mock.patch("time.monotonic", return_value=5.0):
            service.request("AAPL", Decimal("100"))
        self.assertIn("AAPL", service._queued)
        self.assertEqual(service._queue.qsize(), 1)

    def test_request_skips_a_symbol_already_queued(self):
        service = self._service()
        service.request("AAPL", Decimal("100"))
        service.request("AAPL", Decimal("101"))
        self.assertEqual(service._queue.qsize(), 1)

    def test_request_skips_a_symbol_still_within_the_cache_window(self):
        service = self._service()
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            service._fetched_at["AAPL"] = 100.0
        with unittest.mock.patch("time.monotonic", return_value=200.0):
            service.request("AAPL", Decimal("100"))
        self.assertEqual(service._queue.qsize(), 0)

    def test_request_refetches_once_the_cache_window_elapses(self):
        service = self._service(analyst_data_cache_seconds=50)
        service._fetched_at["AAPL"] = 100.0
        with unittest.mock.patch("time.monotonic", return_value=200.0):
            service.request("AAPL", Decimal("100"))
        self.assertEqual(service._queue.qsize(), 1)

    def test_request_disabled_is_a_full_noop(self):
        service = self._service(analyst_priority_enabled=False)
        service.request("AAPL", Decimal("100"))
        self.assertEqual(service._queue.qsize(), 0)
        self.assertNotIn("AAPL", service._queued)

    def test_fetch_populates_the_bonus_and_snapshot_reflects_it(self):
        service = self._service()
        # 50% upside (the formula's clip boundary) + unanimous strong_buy:
        # both signals max out at 1.0, so bonus == bonus_max exactly -
        # see AnalystPriorityBonusTests for the formula itself.
        service.api = SimpleNamespace(
            analyst_target_price=lambda symbol: Decimal("150"),
            analyst_rating=lambda symbol: {
                "strong_buy": 10,
                "buy": 0,
                "hold": 0,
                "sell": 0,
                "under_perform": 0,
            },
        )
        service._fetch("AAPL", Decimal("100"))
        self.assertEqual(service.snapshot(), {"AAPL": 5.0})

    def test_fetch_failure_is_swallowed_by_the_worker_not_fetch_itself(self):
        # _fetch() itself propagates - the worker loop (not under test
        # here) is what catches it, matching MarketResearchAgent's
        # _worker/_research split.
        service = self._service()
        service.api = SimpleNamespace(
            analyst_target_price=lambda symbol: (_ for _ in ()).throw(
                RuntimeError("no coverage")
            ),
            analyst_rating=lambda symbol: None,
        )
        with self.assertRaises(RuntimeError):
            service._fetch("AAPL", Decimal("100"))
        self.assertEqual(service.snapshot(), {})


class TradeEventStreamServiceTests(unittest.TestCase):
    """Phase 0 of the polling-to-streaming migration (see the plan) -
    constructed via __new__ throughout, so no real gRPC connection or
    background thread ever starts in these tests. _on_events_message()/
    drain() are the only two touchpoints the rest of the bot actually
    uses; the SDK-facing pieces (_run, _silence_sdk_prints) aren't
    exercised here.
    """

    def _service(self):
        from webull_bot.trade_events import TradeEventStreamService

        service = TradeEventStreamService.__new__(TradeEventStreamService)
        service.config = SimpleNamespace(
            webull_app_key="key", webull_app_secret="secret",
            webull_region_id="us", account_id="acct-1",
        )
        service.log = logging.getLogger("test-trade-events")
        service._queue = queue.Queue(maxsize=3)
        return service

    def test_on_events_message_enqueues(self):
        service = self._service()
        service._on_events_message(1024, 1, {"a": 1}, raw_message=None)
        self.assertEqual(service.drain(), [(1024, 1, {"a": 1})])

    def test_drain_returns_events_in_order_and_clears_the_queue(self):
        service = self._service()
        service._on_events_message(1024, 1, {"n": 1}, raw_message=None)
        service._on_events_message(1028, 2, {"n": 2}, raw_message=None)
        self.assertEqual(
            service.drain(), [(1024, 1, {"n": 1}), (1028, 2, {"n": 2})]
        )
        self.assertEqual(service.drain(), [])

    def test_a_full_queue_drops_the_oldest_not_the_newest(self):
        service = self._service()
        for n in range(4):
            service._on_events_message(1024, 1, {"n": n}, raw_message=None)
        # maxsize=3: the oldest (n=0) must be the one dropped.
        self.assertEqual(
            service.drain(), [(1024, 1, {"n": 1}), (1024, 1, {"n": 2}), (1024, 1, {"n": 3})]
        )


class StrategyReviewAgentTests(unittest.TestCase):
    def test_empty_or_truncated_completion_does_not_raise(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        self.assertEqual(agent._parse_response(""), {})
        self.assertEqual(agent._parse_response(None), {})

    def test_salvage_json_objects_extracts_complete_entries_before_a_cutoff(self):
        # Two complete suggested_changes entries, then a third cut off
        # mid-string - exactly the "Unterminated string" shape a real
        # truncation produces.
        truncated = (
            '{"assessment":"x","severity":"minor","suggested_changes":['
            '{"lever":"position size","direction":"decrease"},'
            '{"lever":"entry selectivity","direction":"increase"},'
            '{"lever":"stop-loss tightness","reasoning":"unterminat'
        )
        salvaged = MarketResearchAgent._salvage_json_objects(truncated, "lever")
        self.assertEqual(
            [item["lever"] for item in salvaged],
            ["position size", "entry selectivity"],
        )

    def test_salvage_json_objects_ignores_objects_without_the_required_key(self):
        text = '{"assessment":"x","nested":{"foo":"bar"}}'
        self.assertEqual(
            MarketResearchAgent._salvage_json_objects(text, "lever"), []
        )

    def test_parse_response_recovers_partial_suggested_changes_from_truncation(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.log = logging.getLogger("test-agent")
        truncated = (
            '{"assessment":"x","severity":"minor","suggested_changes":['
            '{"lever":"position size","direction":"decrease"},'
            '{"lever":"entry selectivity","direction":"increase"},'
            '{"lever":"stop-loss tightness","reasoning":"unterminat'
        )
        with self.assertLogs("test-agent", level="WARNING") as logs:
            parsed = agent._parse_response(truncated)
        self.assertEqual(
            [item["lever"] for item in parsed["suggested_changes"]],
            ["position size", "entry selectivity"],
        )
        self.assertIn("salvaged", logs.output[0])

    def test_parse_response_still_raises_when_nothing_is_salvageable(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.log = logging.getLogger("test-agent")
        with self.assertRaises(json.JSONDecodeError):
            agent._parse_response('{"assessment": "unterminat')

    def test_parse_response_salvages_a_balanced_but_internally_broken_object(self):
        """Regression test: a top-level object whose braces are perfectly
        balanced but has a syntax error INSIDE it (e.g. a missing comma
        between two suggested_changes entries - "Expecting ',' delimiter"
        from a real production response) used to propagate straight out
        of _parse_response uncaught. _extract_json_object found a
        candidate (braces balance fine), but json.loads(candidate) itself
        raised and that call sat outside any try/except - the salvage
        path never even ran for this failure shape, only for a genuinely
        truncated one.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.log = logging.getLogger("test-agent")
        # Balanced overall, but missing the comma between the two
        # suggested_changes entries in the array.
        broken = (
            '{"assessment":"x","severity":"minor","suggested_changes":['
            '{"lever":"position size","direction":"decrease"}'
            '{"lever":"entry selectivity","direction":"increase"}'
            ']}'
        )
        with self.assertLogs("test-agent", level="WARNING") as logs:
            parsed = agent._parse_response(broken)
        self.assertEqual(
            [item["lever"] for item in parsed["suggested_changes"]],
            ["position size", "entry selectivity"],
        )
        self.assertIn("salvaged", logs.output[0])

    def test_normalize_review_drops_an_unrecognized_lever(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        payload = agent._normalize_review(
            {
                "assessment": "x",
                "severity": "moderate",
                "confidence": 0.5,
                "suggested_changes": [
                    {
                        "lever": "made up lever",
                        "direction": "increase",
                        "reasoning": "not real",
                    },
                    {
                        "lever": "position size",
                        "direction": "decrease",
                        "reasoning": "ok",
                    },
                ],
            }
        )
        self.assertEqual(len(payload["suggested_changes"]), 1)
        self.assertEqual(payload["suggested_changes"][0]["lever"], "position size")

    def test_normalize_review_drops_an_unrecognized_direction(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        payload = agent._normalize_review(
            {
                "suggested_changes": [
                    {
                        "lever": "position size",
                        "direction": "explode",
                        "reasoning": "not real",
                    }
                ],
            }
        )
        self.assertEqual(payload["suggested_changes"], [])

    def test_normalize_review_defaults_safely_for_garbage_input(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        payload = agent._normalize_review("not a dict")
        self.assertEqual(payload["severity"], "none")
        self.assertEqual(payload["confidence"], 0)
        self.assertEqual(payload["suggested_changes"], [])

    def test_normalize_review_rejects_an_unrecognized_severity(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        payload = agent._normalize_review({"severity": "catastrophic"})
        self.assertEqual(payload["severity"], "none")

    def test_session_date_resets_at_market_open_not_midnight(self):
        """AGENT_DAILY_REQUEST_LIMIT budgets the extended trading day
        (MARKET_OPEN_TIME to end of session), not a calendar day - a
        moment before market open still belongs to the previous session's
        tail end, not a fresh budget.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(market_open_time="04:00")
        agent.config.session_time = (
            lambda value: datetime_time(*(int(p) for p in value.split(":")))
        )

        before_open = datetime(2026, 8, 6, 2, 30, tzinfo=timezone.utc)
        self.assertEqual(
            agent._session_date(before_open), datetime(2026, 8, 5).date()
        )

        at_open = datetime(2026, 8, 6, 4, 0, tzinfo=timezone.utc)
        self.assertEqual(
            agent._session_date(at_open), datetime(2026, 8, 6).date()
        )

        mid_session = datetime(2026, 8, 6, 21, 0, tzinfo=timezone.utc)
        self.assertEqual(
            agent._session_date(mid_session), datetime(2026, 8, 6).date()
        )

    def test_submit_strategy_review_resets_budget_at_the_new_sessions_market_open(self):
        import queue as queue_module

        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            agent_daily_token_budget=90000,
            market_open_time="04:00",
            strategy_review_interval_seconds=0,
        )
        agent.config.session_time = (
            lambda value: datetime_time(*(int(p) for p in value.split(":")))
        )
        agent._timezone = timezone.utc
        agent._requests_today = 200
        agent._limit_logged_date = None
        # Still "yesterday's" session per _session_date, even though the
        # calendar date has already ticked over past midnight.
        agent._request_date = datetime(2026, 8, 5).date()
        agent._last_submitted = 0.0
        agent._rate_limit_blocked = False
        agent._token_usage_log = []
        agent._token_limit_logged_at = 0.0
        agent._work = queue_module.Queue(maxsize=1)
        agent.log = logging.getLogger("test-agent")

        with unittest.mock.patch(
            "webull_bot.market_agent.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2026, 8, 6, 2, 30, tzinfo=timezone.utc
            )
            agent.submit_strategy_review({"a": 1})
        # Before market open - still yesterday's session, budget untouched.
        self.assertEqual(agent._requests_today, 200)

        with unittest.mock.patch(
            "webull_bot.market_agent.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2026, 8, 6, 5, 0, tzinfo=timezone.utc
            )
            agent.submit_strategy_review({"a": 1})
        # Past market open - new session, budget resets to 0
        # (submit_strategy_review() only resets/enqueues; _review_strategy()
        # is what later advances the count, on the worker thread this test
        # doesn't run).
        self.assertEqual(agent._requests_today, 0)
        self.assertEqual(agent._request_date, datetime(2026, 8, 6).date())

    def test_submit_strategy_review_force_bypasses_the_interval_throttle(self):
        """force=True must actually skip the interval wait - it was
        previously accepted as a parameter but never read anywhere in
        submit_strategy_review(), so a forced post-circuit-breaker
        reevaluation (submit_strategy_review(..., force=True)) silently
        behaved identically to a routine submit and could sit rate-
        limited for minutes instead of firing immediately.
        """
        import queue as queue_module
        import time as time_module

        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            agent_daily_token_budget=90000,
            market_open_time="00:00",
            session_time=lambda value: datetime_time(0, 0),
            strategy_review_interval_seconds=120,
        )
        agent._request_date = datetime.now(timezone.utc).date()
        agent._requests_today = 0
        agent._limit_logged_date = None
        agent._last_submitted = time_module.monotonic()  # "just submitted"
        agent._timezone = timezone.utc
        agent._work = queue_module.Queue(maxsize=1)
        agent._rate_limit_blocked = False
        agent._token_usage_log = []
        agent._token_limit_logged_at = 0.0
        agent.log = logging.getLogger("test-agent")

        agent.submit_strategy_review({"a": 1}, force=False)
        self.assertTrue(agent._work.empty())  # still within the interval

        agent.submit_strategy_review({"a": 1}, force=True)
        self.assertFalse(agent._work.empty())  # force bypassed the wait

    def test_submit_strategy_review_respects_rolling_token_budget_and_rate_limit_block(self):
        """The interval throttle alone doesn't protect against Groq's real
        tokens-per-day cap - a request can be perfectly on-schedule and
        still 429 if the account's rolling 24h usage is near its limit.
        submit_strategy_review() must refuse to queue work in either case:
        usage already near budget, or a prior 429 having blocked the rest
        of the session.
        """
        import queue as queue_module

        # A fixed monotonic clock, not the real one -
        # submit_strategy_review()'s interval check compares elapsed-
        # since-_last_submitted against this, and the real clock's
        # absolute value depends on how long the host has been up, which
        # is not something a test should depend on.
        now = 100_000.0

        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            agent_daily_token_budget=1000,
            market_open_time="00:00",
            session_time=lambda value: datetime_time(0, 0),
            strategy_review_interval_seconds=120,
        )
        agent._request_date = datetime.now(timezone.utc).date()
        agent._requests_today = 0
        agent._limit_logged_date = None
        agent._last_submitted = 0.0
        agent._timezone = timezone.utc
        agent._rate_limit_blocked = False
        agent._token_limit_logged_at = 0.0
        agent.log = logging.getLogger("test-agent")

        # Over the token budget, even though the interval has long elapsed.
        agent._work = queue_module.Queue(maxsize=1)
        agent._token_usage_log = [(now, 1500)]
        with unittest.mock.patch("time.monotonic", return_value=now):
            agent.submit_strategy_review({"a": 1})
        self.assertTrue(agent._work.empty())

        # Under budget and past the interval - goes through normally.
        agent._token_usage_log = [(now, 100)]
        with unittest.mock.patch("time.monotonic", return_value=now):
            agent.submit_strategy_review({"a": 1})
        self.assertFalse(agent._work.empty())

        # A prior 429 blocks submission for the rest of the session, even
        # with budget free and the interval elapsed - no backoff timer to
        # wait out, it just stays blocked until the next _session_date.
        agent._work = queue_module.Queue(maxsize=1)
        agent._last_submitted = 0.0
        agent._token_usage_log = []
        agent._rate_limit_blocked = True
        with unittest.mock.patch("time.monotonic", return_value=now):
            agent.submit_strategy_review({"a": 1})
        self.assertTrue(agent._work.empty())

    def test_rolling_tokens_used_prunes_entries_older_than_24h(self):
        import time as time_module

        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        now = time_module.monotonic()
        agent._token_usage_log = [
            (now - 86500, 5000),  # just over 24h old - dropped
            (now - 3600, 200),    # 1h old - kept
            (now, 100),           # fresh - kept
        ]
        self.assertEqual(agent._rolling_tokens_used(), 300)
        self.assertEqual(len(agent._token_usage_log), 2)

    def test_parse_retry_after_reads_groqs_minutes_seconds_hint(self):
        message = (
            "Error code: 429 - {'error': {'message': 'Rate limit reached "
            "... Please try again in 33m57.312s. Need more tokens?', "
            "'type': 'compound', 'code': 'rate_limit_exceeded'}}"
        )
        seconds = MarketResearchAgent._parse_retry_after(message)
        # 33*60 + 57.312 + 30s safety margin
        self.assertAlmostEqual(seconds, 2067.312, places=2)

    def test_parse_retry_after_falls_back_to_a_safe_default_when_unparseable(self):
        seconds = MarketResearchAgent._parse_retry_after("rate_limit_exceeded")
        self.assertEqual(seconds, 1800.0)

    def test_rate_limit_error_blocks_review_for_the_rest_of_the_session(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.log = logging.getLogger("test-agent")
        agent._rate_limit_blocked = False

        error = RuntimeError(
            "Error code: 429 - {'error': {'message': 'Rate limit reached "
            "... Please try again in 16m38.784s.', 'type': 'compound', "
            "'code': 'rate_limit_exceeded'}}"
        )
        with self.assertLogs("test-agent", level="WARNING") as logs:
            agent._handle_review_error(error)

        self.assertTrue(agent._rate_limit_blocked)
        self.assertIn("until the next session", logs.output[0])

    def test_rate_limit_block_clears_only_at_the_next_session(self):
        """A prior day's 429 must not silently linger and block review
        forever - it clears specifically when submit_strategy_review()
        rolls over to a new _session_date (start of the next extended
        trading day), same as the request-count budget.
        """
        import queue as queue_module

        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            agent_daily_token_budget=90000,
            market_open_time="04:00",
            strategy_review_interval_seconds=0,
        )
        agent.config.session_time = (
            lambda value: datetime_time(*(int(p) for p in value.split(":")))
        )
        agent._timezone = timezone.utc
        agent._requests_today = 10
        agent._limit_logged_date = None
        agent._request_date = datetime(2026, 8, 5).date()
        agent._rate_limit_blocked = True
        agent._last_submitted = 0.0
        agent._token_usage_log = []
        agent._token_limit_logged_at = 0.0
        agent._work = queue_module.Queue(maxsize=1)
        agent.log = logging.getLogger("test-agent")

        with unittest.mock.patch(
            "webull_bot.market_agent.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2026, 8, 5, 22, 0, tzinfo=timezone.utc
            )
            agent.submit_strategy_review({"a": 1})
        # Still the same session - the block must still be in effect.
        self.assertTrue(agent._rate_limit_blocked)
        self.assertTrue(agent._work.empty())

        with unittest.mock.patch(
            "webull_bot.market_agent.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2026, 8, 6, 5, 0, tzinfo=timezone.utc
            )
            agent.submit_strategy_review({"a": 1})
        # Past market open on a new day - the block clears and this submit
        # goes through.
        self.assertFalse(agent._rate_limit_blocked)
        self.assertFalse(agent._work.empty())

    def test_review_strategy_makes_exactly_one_call_per_cycle(self):
        """Regression test: a retry-with-different-params here used to
        count a second time against AGENT_DAILY_REQUEST_LIMIT and the
        rolling token budget, spending the day's budget faster than
        STRATEGY_REVIEW_INTERVAL_SECONDS' pacing intends -
        _review_strategy must place exactly one Groq call per invocation,
        no matter what comes back.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            groq_model="groq/compound-mini",
        )
        agent.log = logging.getLogger("test-agent")
        agent._requests_today = 0
        agent._latest_strategy_review = None
        agent._lock = threading.Lock()

        calls = []

        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChoice:
            def __init__(self, content):
                self.message = FakeMessage(content)

        class FakeResponse:
            def __init__(self, content):
                self.choices = [FakeChoice(content)]

        def fake_create(**kwargs):
            calls.append(kwargs["messages"][1]["content"])
            return FakeResponse("{}")

        agent.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=fake_create)
            )
        )

        agent._review_strategy(
            {"holdings": [], "pnl_today": {}, "recent_trades": []}
        )

        # Exactly one call - an empty response falls back to conservative
        # defaults for this cycle rather than retrying with different
        # params, and the request budget only ever advances by one.
        self.assertEqual(len(calls), 1)
        self.assertEqual(agent._requests_today, 1)
        self.assertEqual(agent._latest_strategy_review["severity"], "none")
        self.assertEqual(agent._latest_strategy_review["confidence"], 0)
        self.assertEqual(agent._latest_strategy_review["suggested_changes"], [])

    def test_review_strategy_disables_every_built_in_tool(self):
        """Regression test: raising max_completion_tokens and then telling
        the model to keep JSON compact both failed to reliably stop
        truncated/malformed responses in production - Groq's own tool-
        orchestration overhead before writing the JSON isn't something a
        prompt instruction can bound. This assessment never needs search
        (it's computed purely from STATE's numeric performance data), so
        every built-in tool must be disabled outright via compound_custom,
        not just discouraged in the prompt.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            groq_model="groq/compound-mini",
        )
        agent.log = logging.getLogger("test-agent")
        agent._requests_today = 0
        agent._latest_strategy_review = None
        agent._lock = threading.Lock()

        captured = {}

        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChoice:
            def __init__(self, content):
                self.message = FakeMessage(content)

        class FakeResponse:
            def __init__(self, content):
                self.choices = [FakeChoice(content)]

        def fake_create(**kwargs):
            captured.update(kwargs)
            return FakeResponse("{}")

        agent.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=fake_create)
            )
        )

        agent._review_strategy(
            {"holdings": [], "pnl_today": {}, "recent_trades": []}
        )

        self.assertEqual(
            captured["compound_custom"], {"tools": {"enabled_tools": []}}
        )
        self.assertNotIn("search_settings", captured)

    def test_review_strategy_omits_compound_custom_for_a_plain_model(self):
        """A plain (non-Compound) model doesn't understand compound_custom -
        it must only be sent when groq_model is actually a Compound system,
        not unconditionally.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            groq_model="llama-3.3-70b-versatile",
        )
        agent.log = logging.getLogger("test-agent")
        agent._requests_today = 0
        agent._latest_strategy_review = None
        agent._lock = threading.Lock()

        captured = {}

        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChoice:
            def __init__(self, content):
                self.message = FakeMessage(content)

        class FakeResponse:
            def __init__(self, content):
                self.choices = [FakeChoice(content)]

        def fake_create(**kwargs):
            captured.update(kwargs)
            return FakeResponse("{}")

        agent.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=fake_create)
            )
        )

        agent._review_strategy(
            {"holdings": [], "pnl_today": {}, "recent_trades": []}
        )

        self.assertNotIn("compound_custom", captured)

    def test_review_strategy_includes_reasoning_effort_for_a_gpt_oss_model(self):
        """gpt-oss is a reasoning model - its hidden "thinking" tokens are
        counted against max_completion_tokens, so reasoning_effort must be
        sent to keep that overhead small and bounded instead of unbounded.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            groq_model="openai/gpt-oss-120b",
            groq_reasoning_effort="low",
        )
        agent.log = logging.getLogger("test-agent")
        agent._requests_today = 0
        agent._latest_strategy_review = None
        agent._lock = threading.Lock()

        captured = {}

        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChoice:
            def __init__(self, content):
                self.message = FakeMessage(content)

        class FakeResponse:
            def __init__(self, content):
                self.choices = [FakeChoice(content)]

        def fake_create(**kwargs):
            captured.update(kwargs)
            return FakeResponse("{}")

        agent.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=fake_create)
            )
        )

        agent._review_strategy(
            {"holdings": [], "pnl_today": {}, "recent_trades": []}
        )

        self.assertEqual(captured["reasoning_effort"], "low")
        self.assertNotIn("compound_custom", captured)

    def test_review_strategy_omits_reasoning_effort_for_a_non_gpt_oss_model(self):
        """reasoning_effort is a gpt-oss-specific parameter - Groq rejects
        it outright for Compound, and other model families use a different
        enum, so it must only be sent when groq_model is actually gpt-oss.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            groq_model="groq/compound-mini",
        )
        agent.log = logging.getLogger("test-agent")
        agent._requests_today = 0
        agent._latest_strategy_review = None
        agent._lock = threading.Lock()

        captured = {}

        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChoice:
            def __init__(self, content):
                self.message = FakeMessage(content)

        class FakeResponse:
            def __init__(self, content):
                self.choices = [FakeChoice(content)]

        def fake_create(**kwargs):
            captured.update(kwargs)
            return FakeResponse("{}")

        agent.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=fake_create)
            )
        )

        agent._review_strategy(
            {"holdings": [], "pnl_today": {}, "recent_trades": []}
        )

        self.assertNotIn("reasoning_effort", captured)

    def test_review_strategy_requests_a_tpm_safe_completion_budget(self):
        """This account's on-demand tier caps prompt_tokens +
        max_completion_tokens at 8000 per request, enforced before the
        model runs - requesting the old 8000 ceiling here would always be
        rejected outright once any real STATE prompt is added on top of it.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            groq_model="openai/gpt-oss-120b",
            groq_reasoning_effort="low",
        )
        agent.log = logging.getLogger("test-agent")
        agent._requests_today = 0
        agent._latest_strategy_review = None
        agent._lock = threading.Lock()

        captured = {}

        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChoice:
            def __init__(self, content):
                self.message = FakeMessage(content)

        class FakeResponse:
            def __init__(self, content):
                self.choices = [FakeChoice(content)]

        def fake_create(**kwargs):
            captured.update(kwargs)
            return FakeResponse("{}")

        agent.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=fake_create)
            )
        )

        agent._review_strategy(
            {"holdings": [], "pnl_today": {}, "recent_trades": []}
        )

        self.assertLessEqual(captured["max_completion_tokens"], 4000)
