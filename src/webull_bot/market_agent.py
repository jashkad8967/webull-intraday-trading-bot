import json
import queue
import re
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

class MarketResearchAgent:
    """Periodic account-performance review, review-gated: this only ever
    produces a structured suggestion (assessment/severity/suggested
    changes from a fixed, named lever list) for a human or a later
    Claude session to review and implement as a normal tested code
    change - it never submits broker orders, and nothing here mutates
    trading behavior at runtime.
    """

    _VALID_LEVERS = {
        "stop-loss tightness",
        "profit-target distance",
        "position size",
        "entry selectivity",
        "symbol-quarantine aggressiveness",
        "time-aware-stop widen window",
        "fractional-vs-whole-share balance",
    }
    _VALID_SEVERITIES = {"none", "minor", "moderate", "severe"}
    _VALID_DIRECTIONS = {"increase", "decrease", "disable", "enable"}

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
        self._latest_strategy_review: dict | None = None
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
        # review for the rest of the current session, cleared only when
        # submit_strategy_review() rolls over to a new _session_date
        # (start of the next extended trading day), not after a short
        # backoff.
        self._rate_limit_blocked = False
        self._token_limit_logged_at = 0.0
        threading.Thread(target=self._worker, daemon=True).start()
        self.log.info(
            "AGENT  | enabled | model=%s | interval=%ss | budget=%s/day | "
            "tokens=%s/day",
            config.groq_model,
            config.strategy_review_interval_seconds,
            config.agent_daily_request_limit,
            config.agent_daily_token_budget,
        )

    def _session_date(self, moment: datetime):
        """The AGENT_DAILY_REQUEST_LIMIT budget resets at the start of the
        extended trading day (MARKET_OPEN_TIME), not calendar midnight - a
        moment before that boundary still belongs to the previous session
        (the tail end of the prior day's after-hours idle stretch), not a
        fresh one.
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

    def submit_strategy_review(self, state: dict, force: bool = False) -> None:
        """force=True (e.g. a post-circuit-breaker reevaluation) bypasses
        only the interval throttle below, not the daily request budget,
        the rolling token budget, or a rate-limit block above - a forced
        call still counts against and can still be blocked by all three,
        since those are hard cost/availability caps, not pacing
        mechanisms.
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
            if elapsed < self.config.strategy_review_interval_seconds:
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

    def strategy_review(self) -> dict | None:
        with self._lock:
            result = self._latest_strategy_review
            if not result:
                return None
            if time.monotonic() - result["updated_at"] > (
                self.config.strategy_review_interval_seconds * 3
            ):
                return None
            return dict(result)

    def _worker(self) -> None:
        while True:
            state = self._work.get()
            try:
                self._review_strategy(state)
            except Exception as exc:
                self._handle_review_error(exc)

    def _handle_review_error(self, exc: Exception) -> None:
        message = str(exc)
        if "rate_limit_exceeded" in message or (
            "429" in message and "rate_limit" in message.lower()
        ):
            # A real 429 means the account is exhausted for the day - our
            # own rolling budget check in submit_strategy_review() should
            # normally catch this first, but the server-side web search
            # on an agentic model can consume tokens we can't see ahead
            # of time, so this is the reactive backstop. Block review for
            # the rest of the session rather than retrying after Groq's
            # own "try again in Nm" hint - that hint is when the *next*
            # token would free up on the rolling window, not when the
            # account is safely clear of the cap again, so retrying at it
            # tends to just hit the same 429 a second time.
            self._rate_limit_blocked = True
            self.log.warning(
                "AGENT  | Groq daily token limit hit | pausing "
                "strategy review until the next session (Groq's own "
                "estimate was %.0fs, but that's just when the next "
                "token frees up on the rolling window, not when "
                "the account is clear of the cap) | %s",
                self._parse_retry_after(message),
                message,
            )
        elif "request_too_large" in message or "413" in message:
            # STATE itself is small and fixed-size (see _review_strategy) -
            # this is the model's own reasoning/search overhead growing
            # the effective prompt, not our payload. Skipped for this
            # cycle only; the next scheduled cycle tries again fresh.
            self.log.warning(
                "AGENT  | strategy review skipped | Groq request too "
                "large (not our fixed-size payload) | %s",
                exc,
            )
        elif isinstance(exc, json.JSONDecodeError):
            self.log.warning(
                "AGENT  | strategy review skipped | Groq returned "
                "invalid JSON (truncated or malformed) | %s",
                exc,
            )
        else:
            self.log.warning("AGENT  | strategy review failed | %s", exc)

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
    def _salvage_json_objects(text: str, required_key: str) -> list[dict]:
        """Last-resort recovery for a genuinely truncated response (cut
        off mid-string with no balanced top-level object anywhere in it,
        so _extract_json_object can't find anything): scan for every
        individually-balanced {...} object at any nesting depth, parse
        each standalone, and keep the ones carrying required_key (e.g.
        "lever" for a suggested_changes[] entry). A well-formed entry is
        self-contained - it doesn't reference anything outside itself -
        so whatever the model finished writing before the cutoff is
        still valid, parseable data; only the incomplete tail object
        (which never gets a closing brace) is correctly left out.
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
                if isinstance(parsed, dict) and required_key in parsed:
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
                salvaged = self._salvage_json_objects(text, "lever")
                if not salvaged:
                    raise exc
                self.log.warning(
                    "AGENT  | response was truncated/malformed - salvaged "
                    "%s complete suggested_changes entr(y/ies) instead of "
                    "discarding the whole cycle",
                    len(salvaged),
                )
                return {"suggested_changes": salvaged}
        return parsed if isinstance(parsed, dict) else {}

    def _normalize_review(self, payload) -> dict:
        if not isinstance(payload, dict):
            payload = {}
        severity = str(payload.get("severity", "none") or "none").strip().lower()
        if severity not in self._VALID_SEVERITIES:
            severity = "none"
        raw_changes = payload.get("suggested_changes", [])
        changes = []
        if isinstance(raw_changes, list):
            for raw in raw_changes:
                if not isinstance(raw, dict):
                    continue
                lever = str(raw.get("lever", "") or "").strip().lower()
                if lever not in self._VALID_LEVERS:
                    continue
                direction = str(raw.get("direction", "") or "").strip().lower()
                if direction not in self._VALID_DIRECTIONS:
                    continue
                changes.append(
                    {
                        "lever": lever,
                        "direction": direction,
                        "reasoning": str(raw.get("reasoning", "") or "").strip()[
                            :300
                        ],
                    }
                )
        return {
            "assessment": str(payload.get("assessment", "") or "").strip()[:400],
            "severity": severity,
            "confidence": self._number(payload.get("confidence"), 0, 1, 0),
            "suggested_changes": changes[:10],
        }

    def _review_strategy(self, state: dict) -> None:
        if self._requests_today >= self.config.agent_daily_request_limit:
            return
        self._requests_today += 1
        # Compact, literal field:range spec instead of prose sentences - a
        # model reproduces a short explicit schema more reliably than a
        # description of one, which cuts both input tokens (helps the
        # daily token budget) and output-format drift.
        #
        # No search tool (see request_kwargs' compound_custom below) -
        # this assessment is computed entirely from STATE's own numeric
        # performance data (holdings/pnl/trades), so nothing here ever
        # needed search to begin with, and letting a tool run risks
        # unpredictable overhead eating the output budget the same way
        # it did for the predecessor of this capability.
        lever_list = ", ".join(sorted(self._VALID_LEVERS))
        prompt = (
            "Output compact, single-line JSON only - no pretty-printing, "
            "no indentation, no newlines or spaces around punctuation. "
            "This account has a strict daily token budget shared across "
            "every request today; every unnecessary token (formatting "
            "whitespace especially) is budget a later cycle won't have.\n"
            "You review the live performance of an automated US "
            "intraday scalping bot. Assess whether its CURRENT strategy "
            "is actually working from the real holdings/today's pnl/"
            "recent trades in STATE - not any single trade or stock "
            "pick, the pattern across STATE as a whole. Never invent "
            "data beyond what STATE provides. This is a single request "
            "with no retry.\n"
            "Return exactly: assessment (string, 1-2 sentences), "
            "severity:\"none\"|\"minor\"|\"moderate\"|\"severe\", "
            "confidence:0-1, suggested_changes[].\n"
            "suggested_changes[] (0 or more - omit entirely when "
            "severity is \"none\"): {lever, direction:\"increase\"|"
            "\"decrease\"|\"disable\"|\"enable\", reasoning}. lever MUST "
            "be exactly one of: " + lever_list + ". Never invent a lever "
            "name outside this list, never reference code, config keys, "
            "or other internals directly - these are read by a human "
            "who will decide how to actually implement a change, not "
            "applied automatically.\n"
            "severity reflects how confident you are the CURRENT "
            "strategy (not normal variance) needs a change - \"none\" if "
            "today's results look like ordinary noise, \"severe\" only "
            "for a clear, repeated pattern across multiple trades (e.g. "
            "every stop-loss losing more than every profit-take gains, "
            "or fractional trades consistently losing to flat fees)."
            "\nSTATE:"
            + json.dumps(state, separators=(",", ":"), default=str)
        )
        self.log.info(
            "AGENT  | requesting strategy review | trades=%s | bytes=%s | "
            "daily=%s/%s",
            len(state.get("recent_trades", [])),
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
            # This account's on-demand tier caps combined prompt+completion
            # tokens at 8000 per request (Groq's admission control rejects
            # the request outright, before the model even runs, once
            # prompt_tokens + max_completion_tokens exceeds that). 4000
            # leaves comfortable room for STATE to grow while still being
            # far more than a real response needs for this fixed, small
            # schema.
            "max_completion_tokens": 4000,
            # Low, not zero: keeps the severity/confidence assessment
            # consistent/reproducible run to run instead of drifting,
            # while still leaving enough room to weigh conflicting
            # signals rather than collapsing to one canned answer.
            "temperature": 0.2,
        }
        if "compound" in self.config.groq_model.lower():
            # Only meaningful for a Compound system, and this account
            # deliberately isn't on one anymore (see groq_model's default
            # in config.py) - a plain model doesn't understand this param,
            # so it's only sent when it would actually do something.
            request_kwargs["compound_custom"] = {"tools": {"enabled_tools": []}}
        if "gpt-oss" in self.config.groq_model.lower():
            # gpt-oss is a reasoning model - it spends hidden "reasoning"
            # tokens (counted against max_completion_tokens) before writing
            # the actual JSON answer. Left uncapped this risks unpredictable
            # non-answer overhead crowding out the real response; "low"
            # keeps it small. Only sent for gpt-oss - Groq rejects this
            # param outright for Compound, and other model families use a
            # different enum.
            request_kwargs["reasoning_effort"] = self.config.groq_reasoning_effort
        # Exactly one Groq call per review cycle, deliberately no retry -
        # a retry-with-different-params here would count a second time
        # against both AGENT_DAILY_REQUEST_LIMIT and the rolling token
        # budget, silently spending the day's budget faster than
        # STRATEGY_REVIEW_INTERVAL_SECONDS' pacing intends. Any failure
        # here - oversized request, empty/malformed response - is handled
        # by skipping this cycle only; the next scheduled cycle (15
        # minutes later) tries again fresh. The outer worker loop's
        # exception handler already logs a specific, clear message for
        # both failure modes below.
        response = self.client.chat.completions.create(**request_kwargs)
        usage = getattr(response, "usage", None)
        tokens_used = int(getattr(usage, "total_tokens", 0) or 0) if usage else 0
        if tokens_used:
            self._token_usage_log.append((time.monotonic(), tokens_used))
        content = response.choices[0].message.content
        parsed = self._parse_response(content)
        review = self._normalize_review(parsed)
        review["updated_at"] = time.monotonic()
        with self._lock:
            self._latest_strategy_review = review
        self.log.info(
            "AGENT  | strategy review | severity=%s | confidence=%.2f | %s",
            review["severity"],
            review["confidence"],
            review["assessment"],
        )
        for change in review["suggested_changes"]:
            self.log.warning(
                "AGENT  | suggested change | lever=%s | direction=%s | %s",
                change["lever"],
                change["direction"],
                change["reasoning"],
            )
