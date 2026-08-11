import json
import queue
import re
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

class MarketResearchAgent:
    """Non-blocking web/news research. It never submits broker orders."""

    def __init__(self, config, log):
        try:
            from groq import Groq
        except ImportError as exc:
            raise RuntimeError("Run setup.ps1 to install the Groq SDK") from exc

        self.config = config
        self.log = log
        self.client = Groq(
            api_key=config.groq_api_key,
            timeout=float(config.agent_timeout_seconds),
        )
        self._work: queue.Queue[dict] = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._assessments: dict[str, dict] = {}
        self._last_submitted = 0.0
        self._request_date = None
        self._requests_today = 0
        self._timezone = ZoneInfo(config.trading_timezone)
        self._limit_logged_date = None
        # Groq's TPD limit is a rolling 24h window, not a midnight reset
        # (its own 429 gives a "try again in Nm" hint, not "try tomorrow") -
        # tracked here as (monotonic_timestamp, tokens_used) pairs so usage
        # ages out continuously instead of a lump reset that either holds
        # a stale block too long or clears a real block too early.
        self._token_usage_log: list[tuple[float, int]] = []
        # A real 429 from Groq's daily token cap means the account is
        # exhausted for the day - retrying mid-day (even after Groq's own
        # "try again in Nm" hint elapses) just spends more of an already-
        # exhausted budget and risks hitting it again. So this blocks all
        # research for the rest of the current session, cleared only when
        # submit() rolls over to a new _session_date (start of the next
        # extended trading day), not after a short backoff.
        self._rate_limit_blocked = False
        self._token_limit_logged_at = 0.0
        threading.Thread(target=self._worker, daemon=True).start()
        self.log.info(
            "AGENT  | enabled | model=%s | core=%ss | extended=%ss | budget=%s/day | "
            "tokens=%s/day | symbols=%s",
            config.groq_model,
            config.agent_core_research_seconds,
            config.agent_extended_research_seconds,
            config.agent_daily_request_limit,
            config.agent_daily_token_budget,
            min(config.agent_max_symbols, 10),
        )

    def _interval_seconds(self) -> int:
        current = datetime.now(self._timezone).time()
        core_open = self.config.session_time(
            self.config.option_market_open_time
        )
        core_close = self.config.session_time(
            self.config.option_market_close_time
        )
        if core_open <= current < core_close:
            return self.config.agent_core_research_seconds
        return self.config.agent_extended_research_seconds

    def _session_date(self, moment: datetime):
        """The AGENT_DAILY_REQUEST_LIMIT budget resets at the start of the
        extended trading day (MARKET_OPEN_TIME), not calendar midnight - a
        moment before that boundary still belongs to the previous session
        (the tail end of the prior day's after-hours idle stretch), not a
        fresh one. AGENT_CORE_RESEARCH_SECONDS/AGENT_EXTENDED_RESEARCH_SECONDS
        are tuned so the budget is spent across exactly this window (roughly
        195 core-hour requests + 54 extended-hour requests ~= 250), weighted
        toward core hours where research matters most.
        """
        session_open = self.config.session_time(self.config.market_open_time)
        if moment.time() >= session_open:
            return moment.date()
        return moment.date() - timedelta(days=1)

    def _rolling_tokens_used(self) -> int:
        cutoff = time.monotonic() - 86400
        self._token_usage_log = [
            entry for entry in self._token_usage_log if entry[0] >= cutoff
        ]
        return sum(tokens for _, tokens in self._token_usage_log)

    _RETRY_AFTER_RE = re.compile(
        r"try again in (?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>[\d.]+)s)?",
        re.IGNORECASE,
    )

    @classmethod
    def _parse_retry_after(cls, message: str, default: float = 1800.0) -> float:
        match = cls._RETRY_AFTER_RE.search(message)
        if not match or not any(match.groups()):
            return default
        hours = float(match.group("h") or 0)
        minutes = float(match.group("m") or 0)
        seconds = float(match.group("s") or 0)
        total = hours * 3600 + minutes * 60 + seconds
        # +30s margin - Groq's own estimate can undershoot slightly.
        return total + 30 if total > 0 else default

    def submit(self, state: dict, force: bool = False) -> None:
        """force=True (e.g. a post-liquidation reevaluation) bypasses only
        the interval throttle below, not the daily request budget, the
        rolling token budget, or a rate-limit block above - a forced call
        still counts against and can still be blocked by all three, since
        those are hard cost/availability caps, not pacing mechanisms.
        """
        today = self._session_date(datetime.now(self._timezone))
        if self._request_date != today:
            self._request_date = today
            self._requests_today = 0
            self._limit_logged_date = None
            self._rate_limit_blocked = False
        if self._rate_limit_blocked:
            return
        if self._requests_today >= self.config.agent_daily_request_limit:
            if self._limit_logged_date != today:
                self.log.warning(
                    "AGENT  | daily request budget reached | used=%s/%s",
                    self._requests_today,
                    self.config.agent_daily_request_limit,
                )
                self._limit_logged_date = today
            return
        now = time.monotonic()
        tokens_used = self._rolling_tokens_used()
        if tokens_used >= self.config.agent_daily_token_budget:
            if now - self._token_limit_logged_at > 300:
                self.log.warning(
                    "AGENT  | rolling 24h token budget reached | used=%s/%s",
                    tokens_used,
                    self.config.agent_daily_token_budget,
                )
                self._token_limit_logged_at = now
            return
        if not force:
            elapsed = now - self._last_submitted
            interval = self._interval_seconds()
            if elapsed < interval:
                return
        self._last_submitted = now
        try:
            self._work.put_nowait(state)
        except queue.Full:
            try:
                self._work.get_nowait()
            except queue.Empty:
                pass
            self._work.put_nowait(state)

    def assessment(self, symbol: str) -> dict | None:
        with self._lock:
            result = self._assessments.get(symbol)
            if not result:
                return None
            if time.monotonic() - result["updated_at"] > (
                self._interval_seconds() * 3
            ):
                return None
            return dict(result)

    def _worker(self) -> None:
        while True:
            state = self._work.get()
            try:
                self._research(state)
            except Exception as exc:
                self._handle_research_error(exc)

    def _handle_research_error(self, exc: Exception) -> None:
        message = str(exc)
        if "rate_limit_exceeded" in message or (
            "429" in message and "rate_limit" in message.lower()
        ):
            # A real 429 means the account is exhausted for the day - our
            # own rolling budget check in submit() should normally catch
            # this first, but the server-side web search on an agentic
            # model can consume tokens we can't see ahead of time, so this
            # is the reactive backstop. Block research for the rest of the
            # session rather than retrying after Groq's own "try again in
            # Nm" hint - that hint is when the *next* token would free up
            # on the rolling window, not when the account is safely clear
            # of the cap again, so retrying at it tends to just hit the
            # same 429 a second time.
            self._rate_limit_blocked = True
            self.log.warning(
                "AGENT  | Groq daily token limit hit | pausing "
                "research until the next session (Groq's own "
                "estimate was %.0fs, but that's just when the next "
                "token frees up on the rolling window, not when "
                "the account is clear of the cap) | %s",
                self._parse_retry_after(message),
                message,
            )
        elif "request_too_large" in message or "413" in message:
            # STATE itself is small and fixed-size (see _research) - this is
            # compound-mini's own server-side web search/retrieval growing
            # the effective prompt, not our payload. Skipped for this cycle
            # only; the next scheduled cycle tries again fresh.
            self.log.warning(
                "AGENT  | research skipped | Groq request too large "
                "(compound-mini's own web search results, not our fixed-"
                "size payload) | %s",
                exc,
            )
        elif isinstance(exc, json.JSONDecodeError):
            self.log.warning(
                "AGENT  | research skipped | Groq returned invalid "
                "JSON (truncated or malformed) | %s",
                exc,
            )
        else:
            self.log.warning("AGENT  | research failed | %s", exc)

    @staticmethod
    def _number(value, minimum: float, maximum: float, default: float) -> float:
        try:
            return min(max(float(value), minimum), maximum)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _extract_json_object(content: str) -> str | None:
        depth = 0
        start = None
        in_string = False
        escape = False
        for index, character in enumerate(content):
            if in_string:
                if escape:
                    escape = False
                elif character == "\\":
                    escape = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character == "{":
                if depth == 0:
                    start = index
                depth += 1
            elif character == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start is not None:
                        return content[start : index + 1]
        return None

    @staticmethod
    def _salvage_assessments(text: str) -> list[dict]:
        """Last-resort recovery for a genuinely truncated response (cut off
        mid-string with no balanced top-level object anywhere in it, so
        _extract_json_object can't find anything): scan for every
        individually-balanced {...} object at any nesting depth, parse each
        standalone, and keep the ones shaped like an assessment (a dict
        with a "symbol" key). An assessment object is self-contained - it
        doesn't reference anything outside itself - so whatever the model
        finished writing before the cutoff is still valid, parseable data;
        only the incomplete tail object (which never gets a closing brace)
        is correctly left out. Better to keep N-1 real assessments than
        discard the whole cycle over the Nth one being cut off.
        """
        found: list[dict] = []
        stack: list[int] = []
        in_string = False
        escape = False
        for index, character in enumerate(text):
            if in_string:
                if escape:
                    escape = False
                elif character == "\\":
                    escape = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character == "{":
                stack.append(index)
            elif character == "}":
                if not stack:
                    continue
                start = stack.pop()
                try:
                    parsed = json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict) and "symbol" in parsed:
                    found.append(parsed)
        return found

    def _parse_response(self, content: str) -> dict:
        text = str(content or "").strip()
        if not text:
            return {}
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            candidate = self._extract_json_object(text)
            if candidate is not None:
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    candidate = None
            if candidate is None:
                # Either no balanced top-level object exists at all (a
                # genuine truncation), or one does but is internally
                # malformed for some other reason (a missing comma, a
                # stray token) - the balanced-brace candidate's own
                # json.loads can raise too, and that used to propagate
                # uncaught here, skipping the salvage path entirely for
                # exactly the "balanced braces, broken inside" case.
                salvaged = self._salvage_assessments(text)
                if not salvaged:
                    raise exc
                self.log.warning(
                    "AGENT  | response was truncated/malformed - salvaged "
                    "%s complete assessment(s) instead of discarding the "
                    "whole cycle",
                    len(salvaged),
                )
                return {"assessments": salvaged}
        return parsed if isinstance(parsed, dict) else {}

    def _normalize(self, payload, expected_symbols: set[str]) -> dict:
        if not isinstance(payload, dict):
            payload = {}
        by_symbol = {}
        assessments = payload.get("assessments", [])
        if isinstance(assessments, list):
            for raw in assessments:
                if not isinstance(raw, dict):
                    continue
                symbol = str(raw.get("symbol", "")).strip().upper()
                if symbol not in expected_symbols:
                    continue
                by_symbol[symbol] = {
                    "symbol": symbol,
                    "priority": self._number(
                        raw.get("priority"), 0, 1, 0
                    ),
                    "spread_opportunity": self._number(
                        raw.get("spread_opportunity"), 0, 1, 0
                    ),
                    "quick_trade_score": self._number(
                        raw.get("quick_trade_score"), 0, 1, 0
                    ),
                    "symbol_volatility": self._number(
                        raw.get("symbol_volatility"), 0, 1, 0
                    ),
                    "confidence": self._number(
                        raw.get("confidence"), 0, 1, 0
                    ),
                    "catalyst_strength": self._number(
                        raw.get("catalyst_strength"), -1, 1, 0
                    ),
                    "expected_move_percent": self._number(
                        raw.get("expected_move_percent"), -100, 100, 0
                    ),
                    "horizon_minutes": int(
                        self._number(raw.get("horizon_minutes"), 1, 390, 60)
                    ),
                    "downside_risk": self._number(
                        raw.get("downside_risk"), 0, 1, 1
                    ),
                    "liquidity_risk": self._number(
                        raw.get("liquidity_risk"), 0, 1, 1
                    ),
                    "exit_bias": self._number(
                        raw.get("exit_bias"), -1, 1, 0
                    ),
                }

        for symbol in expected_symbols - by_symbol.keys():
            by_symbol[symbol] = {
                "symbol": symbol,
                "priority": 0,
                "spread_opportunity": 0,
                "quick_trade_score": 0,
                "symbol_volatility": 0,
                "confidence": 0,
                "catalyst_strength": 0,
                "expected_move_percent": 0,
                "horizon_minutes": 60,
                "downside_risk": 1,
                "liquidity_risk": 1,
                "exit_bias": 0,
            }

        return {
            "market_direction": self._number(
                payload.get("market_direction"), -1, 1, 0
            ),
            "market_volatility": self._number(
                payload.get("market_volatility"), 0, 1, 1
            ),
            "assessments": [by_symbol[symbol] for symbol in sorted(by_symbol)],
        }

    def _research(self, state: dict) -> None:
        if self._requests_today >= self.config.agent_daily_request_limit:
            return
        self._requests_today += 1
        expected_symbols = {
            str(item.get("symbol", "")).upper()
            for group in ("positions", "candidates")
            for item in state.get(group, [])
            if item.get("symbol")
        }
        # Compact, literal field:range spec instead of prose sentences - a
        # model reproduces a short explicit schema more reliably than a
        # description of one, which cuts both input tokens (helps the
        # daily token budget) and output-format drift (fewer malformed/
        # truncated responses across a full day of up to
        # AGENT_DAILY_REQUEST_LIMIT calls). Every field below is still
        # consumed downstream (research_supports_entry/_exit_bias in
        # strategy.py) - only the wording shrank, not the schema.
        #
        # No search tool at all now (see request_kwargs' compound_custom
        # below) - two rounds of "budget the search/reasoning better"
        # (raising max_completion_tokens, then telling the model to keep
        # JSON compact) still weren't reliable: Groq's own tool
        # orchestration overhead before the JSON is written isn't
        # something a prompt instruction can bound. Removing the tool
        # entirely is the only way to actually guarantee it can't eat the
        # output budget - TASK B was always computed purely from STATE's
        # numeric data anyway (STATE.market_pulse already carries real,
        # current top gainers/losers/most-active from Webull's own
        # screeners - see AutoTrader.refresh_market_pulse), so nothing
        # here ever needed search to begin with.
        prompt = (
            "Output compact, single-line JSON only - no pretty-printing, "
            "no indentation, no newlines or spaces around punctuation. "
            "This account has a strict daily token budget shared across "
            "every request today; every unnecessary token (formatting "
            "whitespace especially) is budget a later cycle won't have.\n"
            "You research setups for a fast US intraday scalping bot - "
            "2-30min holds, not a long-term thesis. Assess every STATE "
            "symbol from its price/chg(change ratio)/vol(volume)/spread, "
            "using STATE.market_pulse (today's actual top gainers/losers/"
            "most-active) as extra market context. Score for a scalp "
            "happening in the next few minutes, not a multi-day move: "
            "reward a real, currently-unfolding catalyst with repeatable "
            "liquid movement; penalize wide spread, thin volume, stale "
            "quotes, or a move that already happened and is fading. Never "
            "invent data beyond what STATE provides; rank attention/setup "
            "quality only, no buy/sell/hold calls. JSON only, numeric "
            "fields only. This is a single request with no retry.\n"
            "Return: market_direction:-1..1, market_volatility:0-1, "
            "assessments[].\n"
            "assessments[] (one per STATE symbol, no exceptions): {symbol, "
            "priority:0-1, spread_opportunity:0-1, confidence:0-1, "
            "quick_trade_score:0-1, symbol_volatility:0-1, "
            "catalyst_strength:-1..1, expected_move_percent:signed, "
            "horizon_minutes:1-390, downside_risk:0-1, liquidity_risk:0-1, "
            "exit_bias:-1..1}.\n"
            "priority/quick_trade_score: how good this symbol is for a "
            "scalp entry right now - low if the move already happened, "
            "the catalyst is stale, or there's nothing actionable in the "
            "next few minutes.\n"
            "exit_bias: negative=de-risk/exit now (fading catalyst, rising "
            "halt/dilution/reversal risk), positive=fresh strong catalyst "
            "supports holding for a larger move, 0=neutral. Every field is "
            "required for every STATE symbol - use neutral values with low "
            "confidence when evidence is thin, never omit a symbol or a "
            "field.\nSTATE:"
            + json.dumps(state, separators=(",", ":"), default=str)
        )
        self.log.info(
            "AGENT  | requesting research | symbols=%s | bytes=%s | daily=%s/%s",
            len(expected_symbols),
            len(prompt.encode("utf-8")),
            self._requests_today,
            self.config.agent_daily_request_limit,
        )
        request_kwargs = {
            "model": self.config.groq_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Assessment is computed entirely from the numeric "
                        "STATE data already in the user message. Return "
                        "JSON only, compact single-line with no whitespace "
                        "or pretty-printing - this account has a strict "
                        "shared daily token budget."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            # 8000 leaves a small margin under Groq's typical 8192-token
            # output ceiling rather than requesting the max itself.
            "max_completion_tokens": 8000,
            # Low, not zero: keeps numeric fields consistent/reproducible run
            # to run instead of drifting, while still leaving enough room for
            # the model to weigh conflicting signals rather than collapsing
            # to one canned answer.
            "temperature": 0.2,
        }
        if "compound" in self.config.groq_model.lower():
            # Only meaningful for a Compound system, and this account
            # deliberately isn't on one anymore (see groq_model's default
            # in config.py) - a plain model doesn't understand this param,
            # so it's only sent when it would actually do something.
            # Disables every built-in tool (web_search, visit_website,
            # code_interpreter, wolfram_alpha) - TASK B never needed
            # search to begin with (it's computed purely from STATE's
            # numeric data), and Compound's own tool-orchestration
            # overhead before the JSON is written was the actual source of
            # the truncated/malformed/empty responses _parse_response's
            # fallback kept having to catch, not the output schema itself.
            request_kwargs["compound_custom"] = {"tools": {"enabled_tools": []}}
        # Exactly one Groq call per research cycle, deliberately no retry -
        # a retry-with-different-params here would count a second time
        # against both AGENT_DAILY_REQUEST_LIMIT and the rolling token
        # budget, silently spending the day's budget faster than the
        # core/extended interval pacing intends (this was a real cause of
        # hitting the daily token ceiling hours before end of day). Any
        # failure here - oversized request, empty/malformed response - is
        # handled by falling back to conservative defaults for this cycle
        # only; the next scheduled cycle (2-10 minutes later) tries again
        # fresh. The outer worker loop's exception handler already logs a
        # specific, clear message for both failure modes below.
        response = self.client.chat.completions.create(**request_kwargs)
        usage = getattr(response, "usage", None)
        tokens_used = int(getattr(usage, "total_tokens", 0) or 0) if usage else 0
        if tokens_used:
            self._token_usage_log.append((time.monotonic(), tokens_used))
        content = response.choices[0].message.content
        parsed = self._parse_response(content)
        raw_assessments = parsed.get("assessments") if isinstance(parsed, dict) else None
        if expected_symbols and not raw_assessments:
            self.log.warning(
                "AGENT  | model returned no real assessments for %s "
                "requested symbols this cycle; falling back to "
                "conservative defaults | raw=%s",
                len(expected_symbols),
                json.dumps(parsed, separators=(",", ":"))[:300],
            )
        payload = self._normalize(parsed, expected_symbols)
        now = time.monotonic()
        with self._lock:
            for item in payload["assessments"]:
                symbol = item["symbol"].upper()
                item["market_direction"] = payload["market_direction"]
                item["market_volatility"] = payload["market_volatility"]
                item["updated_at"] = now
                self._assessments[symbol] = item
        self.log.info(
            "AGENT  | priority research | direction=%+.2f | volatility=%.2f | researched=%s",
            payload["market_direction"],
            payload["market_volatility"],
            len(payload["assessments"]),
        )
        for item in payload["assessments"]:
            self.log.info(
                "AI     | %-8s | priority=%.2f | quick=%.2f | volatility=%.2f | spread=%.2f | conf=%.2f | catalyst=%+.2f | move=%+.2f%%/%sm | downside=%.2f | liquidity=%.2f | exit=%+.2f",
                item["symbol"],
                item["priority"],
                item["quick_trade_score"],
                item["symbol_volatility"],
                item["spread_opportunity"],
                item["confidence"],
                item["catalyst_strength"],
                item["expected_move_percent"],
                item["horizon_minutes"],
                item["downside_risk"],
                item["liquidity_risk"],
                item["exit_bias"],
            )
