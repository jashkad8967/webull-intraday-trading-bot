# Webull production stock and options auto trader

This application uses Webull's official Python SDK against the US production
API, adds a small dashboard with manual override controls, and ships as two
containers you can run locally or deploy to a free-tier cloud VM. It can:

- Scan a capped cross-exchange US universe while always including popular stocks.
- Trade exact OCC option contracts.
- Progressively discover current ATM calls and puts for every optionable stock.
- Scan every second, every few minutes, or up to once per hour.
- Buy on EMA crossovers confirmed by session VWAP, or a re-forming uptrend that
  has held for a configurable number of polls, target percentage-based profits
  scaled to a volatility-adaptive stop, and re-enter while the uptrend persists.
- Cap trades per symbol per hour so a persistent trend doesn't cause overtrading.
- Stop opening positions at the configured end-of-day time.
- Cancel working orders and repeatedly close stock positions before 8:00 PM
  New York time.
- Serve a live dashboard (buying power, positions with buy price and P&L,
  recent trades with realized P&L, watchlist, research-agent state) from a
  second container, with Close All / manual Sell / add-to-watchlist actions.

Webull supports stock market orders. Webull options do not support market
orders, so option entries and exits use refreshed aggressive limit prices.

## Strategy overview

The bot runs two independent entry strategies side by side, on top of a
shared universe-selection and risk-management layer. Nothing below replaced
anything - each piece was added alongside what already existed, and every
layer stays fully configurable/optional.

**1. Universe selection.** `STOCK_SYMBOLS=ALL` builds the tradeable pool one
of two ways: the classic mode (a capped cross-exchange directory scan) or
`MARKET_CAP_ALLOCATION_ENABLED` (Webull's screener, tiered into LARGE_CAP/
SMALL_CAP buckets). Either way, the pool is topped up with today's actual
top gainers (`TOP_GAINERS_LIMIT`) so real current movers are included, not
just high-volume names, and any symbol you've explicitly listed in
`POPULAR_STOCK_SYMBOLS` is reinstated even if the historical-volatility
filter would have dropped it - your trusted names never silently disappear.

**2. Trend-following core.** The main scan buys on an EMA(fast,slow)
crossover, gated by session VWAP support and an extension check (don't chase
a name already sitting at today's high), with `REENTER_CONFIRMATION_POLLS`
consecutive confirming polls required before re-entering after an exit, to
cut whipsaw. Batch priority - which symbols get scanned in a given cycle -
favors two things beyond raw activity: research-agent-boosted candidates,
and symbols that have repeatedly flipped direction today
(`STOCK_OSCILLATION_WEIGHT`), since a choppy, frequently-reversing mover
keeps producing fresh scalp setups while a name that made one move and
stalled doesn't.

**3. Micro-scalp layer.** A handful of extremely liquid single stocks
(`MICRO_SCALP_SYMBOLS`, e.g. TSLA/NVDA) tick by a few cents constantly
without ever forming a clean trend - the EMA core structurally can't act on
that. `MICRO_SCALP_ENABLED` runs a second, fully independent decision path
for exactly those symbols: buy a small dip below a rolling reference price,
sell a small fixed-cents bounce. It has its own capital slice
(`MICRO_SCALP_CAPITAL_FRACTION`) and position cap, and those symbols are
excluded from the trend-following scan so the two paths never fight over
managing the same position.

**4. Sizing.** Entries size to whole shares by default, capped by
`MAX_ORDER_NOTIONAL` and the active bucket's capital allocation. When a
bucket's remaining budget can't afford even one whole share,
`FRACTIONAL_SHARES_ENABLED` falls back to a fractional MARKET order (Webull
requires quantity in (0, 1] and a $5 minimum for these) instead of skipping
the entry.

**5. Research agent (optional, advisory only).** A Groq-based web-research
pass biases priority ranking and exit timing - it never gates an entry that
the technical signals already support, and a missing/degraded agent just
means less-informed prioritization, not a stopped bot. It's resilient to the
underlying model's own quirks: oversized-request retries, malformed/
truncated JSON handling, and a retry-without-discovery fallback for when an
agentic model spends its whole completion budget on web search and returns
an empty assessment.

**6. Safety rails**, all independent of which entry strategy opened a
position: wash-sale blocking (stocks and options, including from a manual
dashboard sell at a loss), a simultaneous-unrealized-loss circuit breaker, a
daily realized-loss circuit breaker, exit escalation for a stuck stop-loss
*or* profit-take order (a profit target the market never actually reaches
would otherwise cancel and resubmit at the identical unreachable price
forever; escalation re-quotes at the current aggressive crossing price
instead, and backs off normally if the escalated resubmission itself
fails, instead of retrying every single poll forever), a stall breaker that
unsticks
capital that hasn't filled in a while, crash-safe universe loading (a
screener hiccup logs a warning and falls back to the prior universe instead
of taking the whole process down), automatic order sizing up to Webull's
100-share minimum lot for stocks priced $0.10-$0.999 (an order under that
is rejected outright, not just slower to fill), and broker-conflict
handling: if Webull rejects an order because its account state disagrees
with the bot's local view of a position (e.g. `..._REVERSE_OPTION`), that
symbol is blacklisted from further automated action for the rest of the
day (logged under `CONFLICT`) instead of retrying an order that can't
succeed - check the Webull app for a stuck order or unexpected position on
that symbol.

**7. Manual overrides.** The dashboard's Close All, per-position Sell, and
watchlist-add all route through a narrow shared command file rather than
giving the dashboard direct Webull access - see [Dashboard](#dashboard).

**8. Observability.** Every 30 seconds the log emits a `SCAN` summary
(universe size, positions, buying power, today's P&L, watchlist size) and,
when entries aren't firing, a `GATES` line showing the top reasons why (e.g.
`price below session VWAP=40 | EMA entry not ready=112`) - so a quiet period
reads as "here's what's blocking entries," not silence.

## Repository layout

```text
src/webull_bot/     trading bot package (config, strategy, execution, agent)
ui/                 dashboard: FastAPI server + static HTML/JS, own Dockerfile
deploy/             compose.yaml (bot + dashboard) and deploy/gcp/*.sh
tests/              unit tests
Dockerfile          bot image (built from repo root, COPYs src/)
.env.example        copyable settings template
setup.ps1           local Windows environment setup
```

## Recreate the application from code

Copy this repository (or the files above) to the destination computer. Do not
copy `.venv`, `.webull-skill-venv`, or create `.env` manually. The setup
script recreates both local environments, installs every dependency, and copies
`.env.example` exactly to `.env`. You then enter your private production Webull
credentials in `.env`.

## 1. Install Python and packages

Open PowerShell in the copied folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

The script:

1. Prefers Python 3.12 and accepts a newer installed Python.
2. Installs Python 3.12 through `winget` if no compatible Python is available.
3. Creates `.venv` for the autonomous trader.
4. Installs the current official Webull SDK and required packages.
5. Creates `.webull-skill-venv` for Webull Agent Skills.
6. Installs the official Webull Agent Skills 1.1.2 package.
7. Copies `.env.example` exactly to `.env` if `.env` does not exist.

The two files are identical immediately after setup. `.env.example` remains the
copyable template, while `.env` is ignored by Git and holds your real secrets.

## 2. Enter Webull credentials

Open `.env`:

```powershell
notepad .env
```

Enter the production credentials issued through Webull OpenAPI Management:

```dotenv
MODE=LIVE
WEBULL_APP_KEY=your_app_key
WEBULL_APP_SECRET=your_app_secret
ACCOUNT_ID=
WEBULL_API_ENDPOINT=api.webull.com
WEBULL_ENVIRONMENT=prod
WEBULL_REGION_ID=us
LIVE_TRADING_ENABLED=true
```

If 2FA is enabled, authenticate once and approve the request in the Webull
mobile app:

```powershell
.\.webull-skill-venv\Scripts\webull-skill.exe auth
```

The token is stored under `conf` and reused by Agent Skills. The Webull SDK
also handles the production token flow when required.

Get the production Account ID:

```powershell
$env:PYTHONPATH="src"; .\.venv\Scripts\python.exe -m webull_bot.connect
```

Copy the returned `account_id` into `.env`, then run the command again:

```powershell
$env:PYTHONPATH="src"; .\.venv\Scripts\python.exe -m webull_bot.connect
```

It will display buying power, positions, and the first configured stock quote.

## 3. Select stocks

The default takes a capped sample from Webull's broad US stock directory and
then guarantees that the configured popular stocks and ETFs are included:

```dotenv
STOCK_SYMBOLS=ALL
MAX_SYMBOLS=500
STOCK_UNIVERSE_PAGE_SIZE=200
STOCK_BATCH_SIZE=100
STOCK_PRIORITY_FRACTION=0.70
STOCK_PENNY_FRACTION=0.10
PENNY_STOCK_MAX_PRICE=5.00
POPULAR_STOCK_SYMBOLS=SPY,QQQ,NVDA,TSLA,AMD,AAPL,AMZN,META,MSFT,GOOGL,NFLX,AVGO,COIN,PLTR,MSTR,HOOD,SOFI,RIVN,GME,AMC,NIO,BABA,F,SNAP,UBER
POPULAR_STOCK_MIN_VOLUME=1000000
POPULAR_STOCK_MAX_SPREAD_PERCENT=0.50
STOCK_POPULAR_CAPITAL_FRACTION=0.70
STOCK_PENNY_CAPITAL_FRACTION=0.10
STOCK_DISCOVERY_CAPITAL_FRACTION=0.20
TOP_GAINERS_LIMIT=200
```

The bot downloads at most `MAX_SYMBOLS` directory entries from Webull in
bounded pages at the start of each trading day and keeps each instrument's
required `US_STOCK` or `US_ETF` category. It separately resolves and adds every
valid configured popular symbol, so those names cannot be lost because of the
directory cap. It also merges in `TOP_GAINERS_LIMIT` symbols from Webull's
today's-top-gainers screener (ranked by actual price change, not just volume),
so stocks that are genuinely trending up right now are part of the universe
too - not only whatever happens to be most heavily traded. Research
discoveries are accepted only when they exist in this combined universe. For
compatibility with existing deployments, `MAX_SYMBOLS=0` now also uses the
safe 500-symbol cap.

`HISTORICAL_VOLATILITY_FILTER_ENABLED` can still drop a configured popular
symbol if its recent amplitude sits under `MIN_HISTORICAL_VOLATILITY_PERCENT`
(common for calmer large-caps) - the bot reinstates any such symbol that was
actually present in the downloaded universe, logging
`LOAD | reinstated N popular symbols the volatility filter would have
dropped`, so a name you explicitly configured never silently disappears from
trading just because of a volatility score.

Each batch always includes held stocks, then targets 70% popular/liquid names,
10% stocks below `PENNY_STOCK_MAX_PRICE`, and 20% rotating discovery. The
popular bucket starts with the configurable seed symbols and adds current
popular volatile symbols found by research. Non-seed popular candidates need
at least the configured volume and no more than the configured spread.

Buying power is reserved using the same 70/10/20 split, so penny or discovery
orders cannot consume the popular-stock allocation first. With the default five
open positions, the slot limits are three popular, one penny, and one discovery
position. The activity score combines current volume, absolute price change,
and intraday or extended-hours range.

If Webull's instrument directory contains a stale or delisted symbol, the bot
uses the rejected-symbol list returned by Webull, skips those symbols for the
rest of that day, and immediately retries only the valid part of the batch.

Webull accepts up to 100 stock symbols in one snapshot request. The bot rotates
through the universe in batches of 100 instead of making one request per stock.
If Webull reports that a directory or snapshot payload is too large, the bot
automatically reduces that request and retries it without dropping symbols.

To restrict trading to a smaller list instead:

```dotenv
STOCK_SYMBOLS=AAPL,MSFT,NVDA,SPY
MAX_SYMBOLS=0
```

With `STOCK_SYMBOLS=ALL`, `MAX_SYMBOLS=500` avoids downloading the entire
15,000+ instrument directory. Webull's listed-equity directory can contain both
stocks and ETFs. The bot checks rejected snapshot symbols against the other category,
corrects stock/ETF mismatches, and skips only symbols invalid in both
categories. Startup prints `LOAD` progress while symbols are downloaded.

### Market-cap-tiered universe (large-cap + small-cap volatility split)

Instead of the popular/penny/discovery split above, you can load a stock-only
universe ranked by market cap straight from Webull's most-active screener and
allocate capital by cap-size tier:

```dotenv
STOCK_SYMBOLS=ALL
MARKET_CAP_ALLOCATION_ENABLED=true
STOCK_LARGE_CAP_MIN_MARKET_VALUE=100000000000
STOCK_LARGE_CAP_CAPITAL_FRACTION=0.80
STOCK_SMALL_CAP_CAPITAL_FRACTION=0.20
MAX_SYMBOLS=750
STOCK_UNIVERSE_PAGE_SIZE=500
EXCLUDE_ETFS=true
HISTORICAL_VOLATILITY_FILTER_ENABLED=true
MIN_HISTORICAL_VOLATILITY_PERCENT=3
TOP_GAINERS_LIMIT=200
```

With this enabled, `STOCK_SYMBOLS=ALL` downloads the universe from Webull's
most-active screener (stocks only, no ETFs) in pages of
`STOCK_UNIVERSE_PAGE_SIZE` up to `MAX_SYMBOLS` total, ranked by market value,
and keeps paging through subsequent pages until either the page limit or
`MAX_SYMBOLS` is reached. It then merges in `TOP_GAINERS_LIMIT` symbols from
Webull's today's-top-gainers screener (ranked by actual price change) that
weren't already in the most-active list, so stocks that are genuinely up big
today are part of the selectable universe even if they aren't among the
highest-volume names. Raise `MAX_SYMBOLS` above 500 (e.g. 750-1000) and set
`STOCK_UNIVERSE_PAGE_SIZE=500` to load more than 500 stocks, 500 per request.

Every symbol still has to pass the existing
`HISTORICAL_VOLATILITY_FILTER_ENABLED` volatility screen, so only volatile
names are kept regardless of cap size - except any symbol you've explicitly
listed in `POPULAR_STOCK_SYMBOLS` that was present in the downloaded universe
but got cut by that filter (common for well-known large-caps with calmer
historical amplitude): the bot reinstates it, logging `LOAD | reinstated N
popular symbols the volatility filter would have dropped`. This is also why
you should always see your explicitly configured names trading somewhere in
the universe - if one is missing entirely, check the startup log for `LOAD |
popular symbols not found in screener universe (skipped)`, which means Webull's
screener response didn't include that symbol at all that day (rare for a
liquid name, but not impossible).

Symbols at or above `STOCK_LARGE_CAP_MIN_MARKET_VALUE` are tagged `LARGE_CAP`;
everything else is `SMALL_CAP`. Buying power is reserved 80/20 between the two
tiers (configurable via `STOCK_LARGE_CAP_CAPITAL_FRACTION` /
`STOCK_SMALL_CAP_CAPITAL_FRACTION`, which must sum to 1), and each batch always
includes held positions first, then the top-ranked large-cap and small-cap
names by the same activity/research priority score used elsewhere, plus a
rotating exploration slice. This mode replaces (not adds to) the
popular/penny/discovery bucketing above — leave
`MARKET_CAP_ALLOCATION_ENABLED=false` (the default) to keep the original
behavior.

Webull's screener response field names aren't published in the SDK's type
stubs, so market value/volume/change/amplitude are parsed defensively; verify
the `LOAD | market-cap universe ready | large_cap=... | small_cap=...` startup
log line against your account once deployed to confirm the split looks right.

### Micro-scalp mode (fixed-cents mean reversion on always-ticking stocks)

Everything above is trend-following: it waits for an EMA crossover, which
requires the price to actually be trending, not just bouncing around. A
handful of extremely liquid single stocks (TSLA, NVDA, etc.) tick almost
every second by a few cents without ever forming a clean trend - the
opportunity there is buying a small dip and selling the bounce back, not
catching a move. Micro-scalp mode is a second, independent decision path
built for exactly that:

```dotenv
MICRO_SCALP_ENABLED=true
MICRO_SCALP_SYMBOLS=TSLA,NVDA,AMD,COIN,PLTR,MSTR
MICRO_SCALP_CAPITAL_FRACTION=0.20
MICRO_SCALP_MAX_POSITIONS=3
MICRO_SCALP_DIP_CENTS=0.05
MICRO_SCALP_TARGET_CENTS=0.06
MICRO_SCALP_STOP_CENTS=0.10
MICRO_SCALP_REFERENCE_WINDOW=20
```

For each symbol in `MICRO_SCALP_SYMBOLS`, the bot keeps a rolling average of
the last `MICRO_SCALP_REFERENCE_WINDOW` quote prices - the level it's "just
been trading around," not a trend direction. It buys when price dips at
least `MICRO_SCALP_DIP_CENTS` below that average (and the spread still
passes `STOCK_ENTRY_MAX_SPREAD_PERCENT`), then exits at
`+MICRO_SCALP_TARGET_CENTS` on a bounce or `-MICRO_SCALP_STOP_CENTS` if the
dip keeps falling - fixed cents, not percentages, since "a few cents" on a
$400 stock is a much smaller move than the bot's normal percentage-based
stop/target bands are tuned for.

This runs as a fully separate path from the main universe scan: it has its
own capital reservation (`MICRO_SCALP_CAPITAL_FRACTION`, taken as a slice of
buying power before the normal bucket split runs) and its own position cap
(`MICRO_SCALP_MAX_POSITIONS`, still bounded by the overall
`MAX_OPEN_POSITIONS`). Symbols in `MICRO_SCALP_SYMBOLS` are automatically
excluded from the main EMA-based scan (logged as `LOAD | excluded N
micro-scalp symbols from the main universe scan`), so the two decision paths
never end up managing the same position with conflicting rules. It still
goes through the same order placement, wash-sale blocking, stop escalation,
and cooldown/rate-cap machinery as every other stock trade - only the
entry/exit decision itself is different.

Keep the symbol list short and genuinely liquid - this is a high-turnover
mode by design (small, frequent wins), so slippage and spread on a thin
name will eat the edge faster than the target captures it.

### Fractional shares

A capital bucket (or micro-scalp's dedicated slice) can easily be too small
to afford one whole share of an expensive stock - `STOCK_QUANTITY` shares at
that price simply won't fit the budget, and the entry is skipped. Enabling
this lets the bot fall back to a fractional-share order instead of skipping:

```dotenv
FRACTIONAL_SHARES_ENABLED=true
FRACTIONAL_SHARES_MIN_NOTIONAL=5
```

Webull only supports fractional share trading as a **MARKET** order during
**core** trading hours - never `LIMIT`, never extended hours - so whenever a
fractional order is placed, the bot forces `order_type=MARKET` and
`support_trading_session=CORE` regardless of the usual limit-price logic,
and the order value must clear `FRACTIONAL_SHARES_MIN_NOTIONAL` (Webull's
own minimum is $5). The fallback only engages when the normal whole-share
sizing comes up empty - if you can afford a whole share, that's still what
gets bought.

Because it's a market order, the fill price isn't guaranteed the way a
limit order's is - on the main stock strategy this is a minor, occasional
edge case, but if you also enable fractional shares under `MICRO_SCALP_*`,
be aware that a market-order fill a cent or two off from the intended entry
eats directly into a strategy that's already targeting only a few cents of
edge per trade.

Fractional positions are tracked and closed out exactly like whole-share
ones (including by the EOD closeout and stall breaker) - Webull's account
position quantity is read as a decimal everywhere it matters, not truncated
to a whole number.

**Dollar-sized core-session entries.** `STOCK_CORE_SESSION_POSITION_FRACTION`
(default `0.10`) changes how the main EMA-scalp strategy sizes a brand new
stock entry during core trading hours (9:30-4:00 ET): instead of buying a
fixed `STOCK_QUANTITY` number of whole shares (falling back to a fractional
order above only when that doesn't fit), it sizes the entry as this fraction
of *total account buying power* and places it as a fractional **MARKET**
order for however many decimal shares that dollar amount buys - not capped
at one share the way the fallback above is. This is the primary
core-session sizing method, not a last resort:

```dotenv
STOCK_CORE_SESSION_POSITION_FRACTION=0.10
```

Outside core hours (pre-market/after-hours), entries always fall back to the
fixed `STOCK_QUANTITY` whole-share sizing, since Webull doesn't allow
fractional orders outside core hours. Set
`STOCK_CORE_SESSION_POSITION_FRACTION=0` to disable this and use the fixed
whole-share sizing all day, matching the bot's pre-existing behavior. This
setting only affects the main stock strategy's `trade_stocks` entries -
micro-scalp mode keeps its own fixed-cents sizing untouched.

Fractional orders (both the sizing above and the `FRACTIONAL_SHARES_ENABLED`
fallback) require the Webull account itself to have agreed to fractional
trading - a one-time click-through, not a code setting. If it hasn't, every
fractional order is rejected with
`OAUTH_OPENAPI_OPENAPI_FRACT_VERSION2_ACCOUNT_NOT_TRADE` and a link to that
agreement page. The bot detects this once, logs the link, and falls back to
whole-share sizing for the rest of that run instead of repeating the same
rejection on every symbol every cycle; open the link in the Webull app or
website and restart the bot to pick fractional sizing back up.

## 4. Select options

### All optionable stocks

The default checks the complete downloaded stock universe:

```dotenv
OPTION_CONTRACTS=
OPTION_UNDERLYINGS=ALL
OPTION_TYPE=BOTH
OPTION_MIN_DTE=7
OPTION_MAX_DTE=45
OPTION_BATCH_SIZE=20
OPTION_DISCOVERY_PER_CYCLE=1
OPTION_DISCOVERY_SECONDS=15
```

Discovery is progressive because requesting every listed strike and expiration
simultaneously would exceed the instrument limits. On each cycle the bot checks
another underlying and retains the nearest-expiration ATM call and put within
the configured DTE range. Every exact listed contract remains usable through
`OPTION_CONTRACTS`.

Webull accepts up to 20 option symbols in a snapshot request, so discovered
contracts are scanned in rotating batches of 20.

### Exact contracts

Enter one or more current OCC option symbols:

```dotenv
OPTION_CONTRACTS=AAPL260918C00200000,SPY260918P00550000
OPTION_UNDERLYINGS=
```

The bot queries Webull for each contract's strike, expiration, type, underlying,
and trading status.

### Automatic ATM calls or puts

Let the bot choose the nearest strike within a DTE range:

```dotenv
OPTION_CONTRACTS=
OPTION_UNDERLYINGS=AAPL,SPY,NVDA
OPTION_TYPE=BOTH
OPTION_MIN_DTE=7
OPTION_MAX_DTE=45
```

Use only one contract type if preferred:

```dotenv
OPTION_TYPE=CALL
```

or:

```dotenv
OPTION_TYPE=PUT
```

You can use exact contracts and automatic selection together.

## 5. Set the trading speed

EMA periods count price samples. With `REENTER_ON_TREND=true`, the bot may buy
again once an uptrend has held for `REENTER_CONFIRMATION_POLLS` consecutive
polls after a take-profit exit, provided price is also within
`VWAP_ENTRY_BAND_PERCENT` of session VWAP. Actual fills depend on price
movement, liquidity, spreads, and the number of configured instruments.

Fast scanning, allowing multiple instruments to trade in a minute:

```dotenv
POLL_SECONDS=0.25
ACCOUNT_REFRESH_SECONDS=5
TRADE_COOLDOWN_SECONDS=15
STOCK_MAX_TRADES_PER_HOUR=12
EMA_FAST_PERIOD=3
EMA_SLOW_PERIOD=8
REENTER_ON_TREND=true
REENTER_CONFIRMATION_POLLS=2
VWAP_ENTRY_BAND_PERCENT=0.001
STOCK_MIN_NET_PROFIT_PERCENT=0.0015
STOCK_ESTIMATED_ROUND_TRIP_COST_PERCENT=0.002
STOCK_STOP_LOSS_MIN_PERCENT=0.0012
STOCK_STOP_LOSS_MAX_PERCENT=0.006
STOCK_STOP_LOSS_RANGE_MULTIPLIER=0.28
STOCK_TARGET_STOP_MULTIPLE=1.2
STOCK_ENTRY_MAX_EXTENSION_PERCENT=0.01
STOCK_OSCILLATION_WEIGHT=0.5
OPTION_TAKE_PROFIT_PRICE=0.01
OPTION_STOP_LOSS_PERCENT=0.50
```

`STOCK_ENTRY_MAX_EXTENSION_PERCENT` blocks a fresh entry when price is
already within that percent of today's high — an EMA/VWAP crossover only
confirms after part of a move has already happened, so without this, the
bot can end up buying right as a fast spike exhausts and reverses, hitting
the stop shortly after. Raise it to allow chasing further-extended moves;
set it to `0` to disable the check entirely.

**Entry gates.** The spread/VWAP/extension/EMA-signal checks run in that
order, each only evaluating candidates that already passed everything
before it - so a `GATES` log dominated by "spread too wide" means most
candidates are being filtered out before VWAP, extension, or the actual
entry signal are ever checked. `STOCK_ENTRY_MAX_SPREAD_PERCENT` default is
`0.50` - deliberately wider than the very first ~0.15% this project
shipped with, which was blocking the large majority of scan candidates
immediately. This is a real tradeoff, not a free win: a wider tolerated
spread means some entries pay more of the strategy's already-thin targeted
margin (`STOCK_MIN_NET_PROFIT_PERCENT` + `STOCK_ESTIMATED_ROUND_TRIP_COST_PERCENT`)
just crossing the spread. Lower it back down for fewer, higher-quality
fills; raise it further for more fire rate at the cost of average edge per
trade.

**Opening grace window.** `STOCK_ENTRY_MAX_SPREAD_PERCENT` and
`STOCK_ENTRY_MAX_EXTENSION_PERCENT` are tuned for profitable mid-day
scalping, but the first few minutes after the 9:30 bell naturally have wider
spreads and an intraday high that hasn't had time to separate from price
yet — so those same gates can reject nearly every entry right at the open
(visible as a `GATES` log dominated by "spread too wide"/"already extended"
right after 9:30). `OPENING_GRACE_MINUTES` (default 10) widens both gates by
`OPENING_GRACE_SPREAD_MULTIPLIER`/`OPENING_GRACE_EXTENSION_MULTIPLIER`
(default 2x each) for that opening stretch only, then snaps back to the
tighter full-day thresholds above. Set `OPENING_GRACE_MINUTES=0` to disable
and use the plain thresholds all day.

### Trade cadence and favoring recurring movers

Every stock's profit target scales with its own adaptive stop
(`STOCK_TARGET_STOP_MULTIPLE` × the stop distance from
`STOCK_STOP_LOSS_MIN/MAX_PERCENT`), so exits are already sized to be small
and quick rather than holding out for a big move. Two settings control how
often the bot is willing to re-enter the same symbol:
`TRADE_COOLDOWN_SECONDS` (minimum gap between orders on one symbol) and
`STOCK_MAX_TRADES_PER_HOUR` (a per-symbol ceiling on top of the cooldown).
Lowering the cooldown and raising the hourly cap makes each individual
symbol tradeable more often; the VWAP gate, extension gate, and
`REENTER_CONFIRMATION_POLLS` still have to agree before any of those
re-entries actually fire, so cadence goes up without giving up the
whipsaw protection those gates were added for.

Some stocks genuinely move back and forth many times in a session instead
of trending once and going flat — those are the ones that keep producing
fresh small scalps all day. The strategy tracks, per symbol, how many times
today its EMA(fast)/EMA(slow) spread has flipped sign (an
`old_spread`/`new_spread` crossing either direction), and adds
`STOCK_OSCILLATION_WEIGHT` per flip (capped at 20) to that symbol's ranking
score used for batch selection and research-candidate ordering. A choppy,
frequently-reversing mover therefore keeps climbing toward the front of the
queue as the day goes on, while a stock that made one move and stalled
falls back toward the rotating-exploration slice. Raise
`STOCK_OSCILLATION_WEIGHT` to lean harder into repeat movers, or set it to
`0` to rank purely on the existing volume/price-move/range activity score.

Medium cadence:

```dotenv
POLL_SECONDS=60
TRADE_COOLDOWN_SECONDS=300
EMA_FAST_PERIOD=5
EMA_SLOW_PERIOD=15
```

Slower, up to hourly:

```dotenv
POLL_SECONDS=900
TRADE_COOLDOWN_SECONDS=3600
EMA_FAST_PERIOD=3
EMA_SLOW_PERIOD=8
```

`POLL_SECONDS` accepts values from 0.25 through 3600. Going below 1s mainly
tightens the outer loop's own sleep — actual request cadence to Webull is
still separately capped by `MARKET_REQUESTS_PER_MINUTE` and the other
per-group throttles in [7. API request pacing](#7-api-request-pacing), so a
value below roughly `60 / MARKET_REQUESTS_PER_MINUTE` seconds mostly means
the loop spends more time waiting on those throttles instead of sleeping.

The loop uses a start-to-start cadence: time spent processing is subtracted
from `POLL_SECONDS` instead of adding another full sleep afterward. Account
balance and positions are cached briefly with `ACCOUNT_REFRESH_SECONDS=5`, and
option discovery runs independently at `OPTION_DISCOVERY_SECONDS=15`. Webull's
request throttles remain enforced.

## Optional web-research agent

The strategy and execution loop do not wait for the agent. A background worker
uses Groq Compound Mini's built-in web search to research current news and catalysts for
held positions and EMA entry candidates. Its output changes which symbols are
scanned most often; it cannot approve, reject, or submit an order.

Create a free Groq developer key at
[console.groq.com/keys](https://console.groq.com/keys), then add it to `.env`:

```dotenv
AGENT_ENABLED=true
GROQ_API_KEY=your_groq_api_key
GROQ_MODEL=groq/compound-mini
AGENT_CORE_RESEARCH_SECONDS=120
AGENT_EXTENDED_RESEARCH_SECONDS=622
AGENT_DAILY_REQUEST_LIMIT=250
AGENT_MAX_SYMBOLS=5
AGENT_DISCOVERY_MAX_SYMBOLS=5
AGENT_TIMEOUT_SECONDS=60
LOSS_CIRCUIT_BREAKER_ENABLED=true
LOSS_SPREE_POSITION_COUNT=3
LOSS_SPREE_TOTAL_DOLLARS=1.00
LOSS_REEVALUATION_SECONDS=120
```

`groq/compound-mini` is the low-latency Compound system and can perform one
built-in web search per research request. The free tier currently allows 250
Compound Mini requests per day. During the 9:30 AM–4:00 PM core session, the
bot allows one call every 120 seconds, allocating up to 195 calls. During the
4:00–9:30 AM and 4:00–8:00 PM extended sessions, it allows one call every 622
seconds and uses up to the remaining 55 calls. A hard 250-call daily limit
applies across both windows. A request can still perform broad popular/volatile
discovery when there are no supplied symbols to assess. Cached results are used
between calls.

Every research response contains `market_direction` and `market_volatility`.
Every researched symbol contains `priority`, `quick_trade_score`,
`symbol_volatility`, `spread_opportunity`, `confidence`, `catalyst_strength`,
`expected_move_percent`, `horizon_minutes`, `downside_risk`, `liquidity_risk`,
and `exit_bias`. Missing symbol output is replaced with conservative values
rather than silently accepted.

Groq searches for current popular, widely traded volatile stocks and ETFs in
addition to assessing supplied candidates. Newly discovered symbols are
accepted only when they also exist in Webull's current tradable universe.
Discovery output is symbol-only - no other field downstream ever reads a
discovery's popularity/volatility/confidence, so the model isn't asked to
spend completion tokens producing them. Assessments never require a web
search (they're computed from the numeric price/change/volume/spread already
sent for each symbol), so an assessment-only retry pass explicitly tells the
model not to search - this keeps both the request payload and the response
small and makes it far less likely the model runs out of completion budget
mid-response and falls back to conservative defaults instead of a real,
computed assessment.
Research confidence, volatility, catalysts, and quick-trade scores boost the
symbol's scan priority. A high-confidence, liquid, bullish setup with a
30-minute-or-shorter horizon can add an entry path. Research never vetoes an
EMA entry, and missing or negative research does not block technical trading.
It also evaluates the supplied live bid/ask gap; a wide spread is treated as an
opportunity only when volume, liquidity, catalysts, and price movement suggest
it is executable.

### Loss-spree circuit breaker

The optional portfolio circuit breaker liquidates all positions when at least
`LOSS_SPREE_POSITION_COUNT` positions are losing *at the same time* and their
combined unrealized loss reaches `LOSS_SPREE_TOTAL_DOLLARS`.

After liquidation, entries remain paused for at least
`LOSS_REEVALUATION_SECONDS`. This rule is deterministic and does not wait for
Groq.

### Daily loss circuit breaker

The loss-spree breaker above only looks at positions losing *simultaneously*
right now — a string of small stop-losses taken one at a time, each below
that threshold, never trips it. `DAILY_LOSS_CIRCUIT_BREAKER_ENABLED` (default
`false`) adds a second, independent check: once the running total of
realized stop-loss exits for the day reaches `DAILY_MAX_LOSS_DOLLARS`
(estimated from each exit's submitted price, not the eventual fill), the bot
liquidates everything and halts new entries for the rest of the trading day —
it does not auto-resume like the loss-spree breaker does. Set
`DAILY_MAX_LOSS_DOLLARS` to a figure that makes sense for your account size;
it has no safe universal default.

### Stop-loss escalation

Stock sell exits — both profit-take and stop-loss — are submitted at the
current ask (the top of the bid/ask spread, the best price a resting sell
limit order could hope to get), and that resting order is continuously
re-quoted (cancelled and replaced) to track the ask as it moves for as long
as it stays unfilled, for the best available price. If the quote has no
valid ask, profit-take falls back to its fixed target price and stop-loss
falls back to the bid/ask midpoint, the same defensive pattern used
elsewhere in the order-placement code.

If that order still hasn't filled within `STOP_LOSS_ESCALATE_SECONDS` (15s
default), it's cancelled and resubmitted at an aggressive, spread-crossing
price instead — a stop that never fills while price keeps falling defeats
the entire point of having one. Continuous re-quoting to track the ask does
not reset this clock: it's measured from the exit's original submission, so
`STOP_LOSS_ESCALATE_SECONDS` remains a hard backstop that forces a
guaranteed-fill price regardless of how many times the resting order gets
re-quoted in between.

### Stall breaker

When `STALL_BREAKER_ENABLED` is true and no order fills for
`STALL_BREAKER_SECONDS` (120 by default), the bot places marketable sell orders
for any held position that can still lock in at least `STALL_BREAKER_MIN_PROFIT`
per share (1 cent by default) above its average cost. This keeps activity moving
during quiet stretches. It never sells at a loss, skips positions that already
have a pending exit, and resets its timer whenever a real fill occurs.

Repository responsibilities are intentionally separated:

- `src/webull_bot/strategy.py`: activity scoring, penny/popular allocation,
  Groq priority weighting, EMA + VWAP entry rules, adaptive targets/stops,
  sizing, and portfolio policy.
- `src/webull_bot/bot.py`: session scheduling, account caching, wash-block
  coordination, per-symbol trade-rate capping, and order workflow.
- `src/webull_bot/webull_api.py`: Webull authentication, throttling, market
  data, and order transport.
- `src/webull_bot/market_agent.py`: paced, non-blocking Groq research.
- `src/webull_bot/wash_sale.py`: persistent repurchase blocks.
- `src/webull_bot/status.py`: writes the JSON snapshot the dashboard reads.
- `ui/`: the dashboard's FastAPI server and static page.

**Entries** require an EMA(fast, slow) crossover (or, with
`REENTER_ON_TREND=true`, a re-forming uptrend that has held for
`REENTER_CONFIRMATION_POLLS` consecutive polls) *and* the price at or within
`VWAP_ENTRY_BAND_PERCENT` of the session VWAP — the crossover alone reacts to
quote noise more than genuine momentum, so VWAP acts as a second, independent
confirmation.

**Stops** scale with each symbol's own realized range instead of one flat
percentage: `STOCK_STOP_LOSS_RANGE_MULTIPLIER` times the symbol's
today's-high/low-vs-price ratio, clamped between
`STOCK_STOP_LOSS_MIN_PERCENT` and `STOCK_STOP_LOSS_MAX_PERCENT`. A calm
large-cap gets a tight stop; a wild small-cap gets a wider one.

**Targets** are the larger of (a) `STOCK_MIN_NET_PROFIT_PERCENT` +
`STOCK_ESTIMATED_ROUND_TRIP_COST_PERCENT`, or (b) that same adaptive stop
percent times `STOCK_TARGET_STOP_MULTIPLE` (default 1.2×) — so reward:risk
scales with volatility instead of staying fixed while the stop moves. For
options, `OPTION_TAKE_PROFIT_PRICE=0.01` sets the minimum sell limit one
premium cent above average cost, normally $1 per standard 100-share contract
before fees. Options also stop out at `OPTION_STOP_LOSS_PERCENT` (50% of
premium by default) — unlike stocks, there was previously no automatic loss
cut on an option position at all, so a losing contract could only be closed
by the end-of-day sweep. A profit or stop order remains subject to its limit
being filled.

When a stock *or option* loss exit is submitted (options block by their
underlying symbol), it's persisted in `conf/wash_sale_blocks.json` and
blocked from new purchases — of the stock or a new option on it — for
`WASH_SALE_BLOCK_DAYS` (31 by default), just past the 30-day IRS wash-sale
window. The tracker stores the date each block was triggered rather than a
precomputed end date, so changing `WASH_SALE_BLOCK_DAYS` retroactively
re-shortens or re-lengthens every existing block the next time the bot
starts, not just new ones going forward. This is a conservative
same-underlying control, not a tax determination: it has no visibility into
manual trades, other accounts, or IRAs, and it doesn't attempt the
"substantially identical security" analysis for deep-ITM options that real
tax software would. It only guards against the bot's own trading creating a
wash sale. Lowering `WASH_SALE_BLOCK_DAYS` closer to the 30-day floor
shrinks that safety margin - a re-entry landing exactly on day 31 is fine
against a loss 31 days prior, but leaves less room for error than the
previous 60-day default did.

The end-of-day closeout is the exception: it cancels working profit orders and
closes remaining positions before market close, which can realize a loss.

`STOCK_MAX_TRADES_PER_HOUR` (default 12) caps new entries per symbol per
rolling hour independent of `TRADE_COOLDOWN_SECONDS` (default 15s), so a
persistent trend can't turn into unbounded churn on one name. It does not
promise a fixed trade count: EMA/VWAP confirmation, target price movement,
fills, API response time, open-position limits, and Webull rate limits
determine the actual count.

> **Regulatory note:** FINRA's amended Rule 4210 (effective June 2026)
> replaced the old $25k/3-trades-per-5-business-days Pattern Day Trader
> threshold with a margin-based framework — verify your account's current
> treatment with Webull directly, since brokers had up to 18 months to roll
> out the change. Also re-verify the request-rate numbers in
> [7. API request pacing](#7-api-request-pacing) against your own app's limits
> in Webull's OpenAPI Management dashboard before relying on the defaults
> below.

One cent multiplied by 1,000 completed one-share trades is $10 gross. With 100
shares, a one-cent favorable move is $1 per completed trade. Net results also
include losing trades, bid/ask spread, slippage, regulatory fees, commissions
that may apply, and orders that do not fill.

## 6. Set sizes and limits

```dotenv
STOCK_QUANTITY=1
OPTION_QUANTITY=1
MAX_OPEN_POSITIONS=5
MAX_ORDER_NOTIONAL=1000
```

Option notional is calculated as premium x 100 x contracts.

Before every trading cycle, the bot refreshes Webull buying power. Stock market
buys reserve a 3% price cushion, which exceeds Webull's required 2% volatility
buffer. If the configured quantity is too large, the bot reduces it to the
largest affordable whole-share quantity. Option quantities are similarly
reduced using the submitted limit price. Remaining buying power is reserved
locally as each order is submitted so later orders in the same cycle cannot
reuse it.

## 7. API request pacing

Keep these defaults unless Webull changes the limits assigned to your
application:

```dotenv
MARKET_REQUESTS_PER_MINUTE=240
OPTION_INSTRUMENT_REQUESTS_PER_MINUTE=45
STOCK_INSTRUMENT_REQUESTS_PER_30_SECONDS=9
ACCOUNT_REQUESTS_PER_SECOND=0.8
ORDER_REQUESTS_PER_MINUTE=480
ORDER_TIMEOUT_SECONDS=120
ORDER_MONITOR_SECONDS=5
STOP_LOSS_ESCALATE_SECONDS=15
STALL_BREAKER_ENABLED=true
STALL_BREAKER_SECONDS=120
STALL_BREAKER_MIN_PROFIT=0.01
```

The bot maintains an independent timer for each API group, sends stock quotes
in groups of 100 and option quotes in groups of 20, and retries throttling or
temporary server errors with backoff. Order submissions are not automatically
retried because a timed-out order may already have reached the broker. It checks
working orders every five seconds and requests cancellation of any order
remainder that is still open 120 seconds after submission. Orders already open
when the bot starts are adopted and timed from when the bot first observes them.

## 8. Configure daily closeout

```dotenv
TRADING_TIMEZONE=America/New_York
MARKET_OPEN_TIME=04:00
EOD_CLOSE_TIME=19:50
MARKET_CLOSE_TIME=20:00
OPTION_MARKET_OPEN_TIME=09:30
OPTION_EOD_CLOSE_TIME=15:50
OPTION_MARKET_CLOSE_TIME=16:00
STOCK_LIMIT_OFFSET=0.005
EOD_RETRY_SECONDS=10
```

Stocks run in Webull's `ALL` session from 4:00 AM through 8:00 PM New York
time. All stock entries and exits use limit orders because market orders are
not eligible throughout the extended session. Buy limits use the bid/ask
midpoint rounded down to the nearest cent, never an amount above the displayed
ask. `STOCK_LIMIT_OFFSET=0.005` only lets stock closeout sells cross below the
current bid by 0.5%.

Options remain restricted to their supported core session. At 3:50 PM the bot
cancels working orders and repeatedly sends aggressive option close limits.
Stock trading continues afterward. At 7:50 PM the bot cancels working orders
and repeatedly sends stock close limits until positions are gone or the stock
extended session closes at 8:00 PM.

The closeout operates on positions in the configured Webull account, including
positions that existed before the bot started.

Add full-day exchange holidays as comma-separated dates:

```dotenv
MARKET_HOLIDAYS=2026-01-01,2026-12-25
```

## 9. Run the bot

```powershell
$env:PYTHONPATH="src"; .\.venv\Scripts\python.exe -m webull_bot
```

Stop it with:

```text
Ctrl+C
```

Force account-wide closeout at any time:

```powershell
$env:PYTHONPATH="src"; .\.venv\Scripts\python.exe -m webull_bot --close-all
```

Or run both the bot and the dashboard together with Docker Compose (see
[Dashboard](#dashboard) and [Run continuously on a free Google Cloud VM](#12-run-continuously-on-a-free-google-cloud-vm)
below):

```bash
cp .env.example .env   # then fill in your real credentials
BOT_ENV_FILE="$(pwd)/.env" docker compose -f deploy/compose.yaml up --build
```

(`BOT_ENV_FILE` must be an absolute path here — Compose resolves a relative
one against `deploy/`, where the compose file lives, not the repo root.)

This bypasses signals, cancels all working orders, and submits closes for every
position in `ACCOUNT_ID`, including positions opened outside this bot. It
cannot override a closed market, trading halt, rejected order, or unfilled
option limit.

The terminal reports selected targets, signals that become orders, API errors,
and end-of-day closeout progress in a compact colored format:

```text
04:00:00 INFO     START  | mode=LIVE | poll=1s | cooldown=5s
04:00:00 INFO     LOAD   | downloading stocks and ETFs | limit=500
04:00:04 INFO     READY  | stocks=500 | options=0 | option scan=ON
04:00:19 INFO     SCAN   | stocks=100/500 | options=0/0 | positions=0
04:00:21 INFO     ORDER  | STOCK       | BUY    | AAPL     | id=...
19:50:02 INFO     CLOSE  | submitted=3 | remaining=0
```

Verbose Webull SDK request objects and authentication headers are suppressed.

### Required production quote subscriptions

Production trading requires market prices, and Webull licenses those prices
separately for OpenAPI. A quote package purchased in the Webull mobile or
desktop application does not grant OpenAPI access.

1. Sign in to the Webull Technology website.
2. Select your avatar, then **Advanced Quotes**.
3. Open **OpenAPI Advanced Quotes**.
4. Enable **Nasdaq Basic Non-Display** for US stocks and ETFs.
5. Enable **OPRA Real-Time Non-Display** to trade options.
6. Restart the bot after Webull activates the subscriptions.

If the stock subscription is missing, the bot prints one `STOP` message and
exits instead of repeating permanent 401 errors. If stocks work but the OPRA
option subscription is missing, options are disabled for that run and stock
trading continues.

## 10. Use Webull Agent Skills

```powershell
.\.webull-skill-venv\Scripts\webull-skill.exe trading --action account-list
.\.webull-skill-venv\Scripts\webull-skill.exe trading --action balance --account-id YOUR_ACCOUNT_ID
.\.webull-skill-venv\Scripts\webull-skill.exe market-data --action stock-snapshot --symbols AAPL
```

Agent Skills is the companion interface for manual or AI-assistant commands.
The autonomous loop uses its own `.venv` and calls the official SDK directly,
which avoids starting a new CLI process for every market-data batch.

## 11. Daily context logs

The bot writes important `INFO`, warning, error, research, order, and status
messages live to:

```text
logs/YYYY/MM/YYYY-MM-DD.log
```

It switches files automatically at midnight in `TRADING_TIMEZONE` and adds one
`DAYEND` summary after the stock session closes. Files are flushed after each
message, so a process restart does not lose the day's accumulated context.
Change `LOG_DIRECTORY` to place logs on durable storage.

## Dashboard

A second container (`ui/`) serves a small live dashboard: buying power,
today's P&L (both the combined total and the realized/unrealized
breakdown), open positions (with buy price and live unrealized P&L),
pending orders still working at the broker, recent trades (with price and
realized P&L on each exit), the current watchlist, and the research
agent's latest state, polling a JSON snapshot the bot writes each cycle
(`STATUS_FILE`, default `status.json`). It has no access to your Webull
credentials or the trade API, and its mount of that status/log data is
read-only — a display bug in the dashboard can't corrupt the bot's own
state.

`DEFAULT_WATCHLIST_SYMBOLS` is seeded into the watchlist automatically every
time the bot starts - unlike `POPULAR_STOCK_SYMBOLS` (which only weights
priority within the scanned universe), these are always present, a restart
never loses them, and you never have to re-add them from the dashboard.
Anything added from the dashboard on top of that stays only in memory for
the current run.

The dashboard can also request four actions: **Close All** (cancels every
working order and closes every open position, stocks and options alike),
**Sell** on any individual position, **Cancel** on any individual pending
order (in the Pending Orders panel - useful for backing out of a BUY
that's still resting unfilled, or a STOP/PROFIT exit you'd rather reprice
or handle manually), and adding a symbol to the watchlist. These don't
give the dashboard trading access directly - clicking a button writes a
small request to a separate, dedicated shared file (`COMMAND_FILE`, a
distinct Docker volume the dashboard can only read/write that one file
in) that the trader process reads once per cycle and executes through its
own already-safe order-placement, wash-sale, and position-tracking code,
the same as every automatic exit. A request is picked up on the bot's next
cycle, not instantly - `POLL_SECONDS` is the worst-case delay. Close All,
Sell, and Cancel all ask for a JavaScript confirmation before sending the
request, since they act on real orders on your account. Cancelling a
STOP/PROFIT exit only cancels the *order* - the position itself stays
open, and the bot will submit a fresh exit order for it again next cycle
unless you also close the position (e.g. via Sell) or the market moves it
out of an exit condition.

A manual **Sell** is an urgent "get me out now" click, so it's priced
differently from a patient automatic exit: it sells at the current ask (the
top of the spread) instead of crossing further below the bid, and during
core trading hours (with a whole-share position, and once the account has
agreed to fractional/MARKET trading - see "Fractional shares" above) it
places a genuine MARKET order instead of a resting LIMIT order, so it
doesn't sit unfilled either. The old below-bid-crossing price shaved an
extra `STOCK_LIMIT_OFFSET`/`OPTION_LIMIT_OFFSET` off the sale for no real
reason, which could turn an otherwise flat or barely-profitable manual exit
into a recorded loss.

The trader and dashboard run as different non-root container users sharing
that one volume, so both Dockerfiles pre-create `/var/commands` world-writable
before either user is dropped into place. If you already ran
`docker compose up` once *before* pulling this fix, Docker will have
initialized that volume with the old, broken permissions - a plain restart
won't pick up the corrected ones (Docker only applies an image's directory
permissions to a named volume the first time it's used). If you see
`CMD | queue read failed | [Errno 13] Permission denied` in the trader's
logs, remove and let it recreate:

```bash
docker compose -f deploy/compose.yaml -p webull-bot down
docker volume rm webull-trading-commands
docker compose -f deploy/compose.yaml -p webull-bot up -d
```

Running via `deploy/compose.yaml`, the dashboard is bound to
`127.0.0.1:8080` on whatever host it runs on (not exposed to the network by
default). View it by tunneling over SSH:

```bash
ssh -L 8080:localhost:8080 <user>@<host>
```

then open <http://localhost:8080> in your browser. To expose it directly
instead (e.g. for local-only use), change the `ports:` mapping in
`deploy/compose.yaml` from `127.0.0.1:8080:8080` to `0.0.0.0:8080:8080` and
open port 8080 in your firewall/security rules.

## 12. Run continuously on a free Google Cloud VM

The repository includes a multi-architecture Docker image for the bot, a
Dockerfile for the dashboard, a Compose definition covering both, a Compute
Engine VM bootstrap script, and GitHub Actions continuous deployment. Use one
VM: simultaneous live bot instances could submit duplicate orders.

### Create the VM

1. Create a Google Cloud account/project if you don't have one (requires a
   card on file for identity verification; Compute Engine's Always Free tier
   itself has no recurring charge as long as you stay within it).
2. In Compute Engine, create an instance in an Always Free-eligible region
   (`us-west1`, `us-central1`, or `us-east1`) using the `e2-micro` machine
   type and an **Ubuntu 24.04 LTS** boot disk (not the default Debian image,
   so the bootstrap script's `apt` packages match).
3. Under the instance's SSH keys, add the public half of a dedicated
   deployment keypair (not your personal key) — generate one with
   `ssh-keygen -t ed25519 -C "webull-bot-deploy" -f webull-bot-deploy`.
4. In the VPC firewall rules, restrict inbound access to SSH (port 22) from
   your own IP only. The bot does not require an inbound HTTP port; the
   dashboard is reached over an SSH tunnel (see [Dashboard](#dashboard)).

### Bootstrap the VM once

Connect to the VM (`ssh -i webull-bot-deploy <user>@<external-ip>`), clone
this repository, and run:

```bash
git clone https://github.com/jashkad8967/webull-intraday-trading-bot.git
cd webull-intraday-trading-bot
sudo bash deploy/gcp/bootstrap.sh
sudo nano /opt/webull-bot/shared/.env
```

Enter `WEBULL_APP_KEY`, `WEBULL_APP_SECRET`, `ACCOUNT_ID`, and `GROQ_API_KEY`
in that VM-only `.env` file. Review every live-trading setting, save it, then
log out and reconnect so your user receives Docker group membership. Never
commit this file.

The bootstrap creates a persistent Docker volume named `webull-trading-data`.
Daily logs, wash-sale state, and the dashboard's status feed survive container
replacements and VM reboots. They remain on the VM boot disk, but not after
deleting the VM and its volume. Copy important logs off the VM periodically if
you need separate recovery.

### Connect GitHub deployment

Add these GitHub Actions repository secrets:

```text
GCP_HOST                 VM external IP or hostname
GCP_USER                 your GCE login user
GCP_SSH_PORT             22
GCP_SSH_PRIVATE_KEY      the deployment keypair's private half
GCP_KNOWN_HOSTS          verified VM SSH known_hosts entry
```

Before storing `GCP_KNOWN_HOSTS`, verify the VM's SSH host-key fingerprint
shown in the Compute Engine console (or over a console-based connection)
against what `ssh-keyscan` returns. Do not blindly trust an unverified
`ssh-keyscan` result.

Finally, add the repository variable:

```text
GCP_DEPLOY_ENABLED=true
```

Run the `Validate trading bot` workflow manually for the first deployment.
Afterward, each push to `main` runs tests, uploads an exact release archive,
builds the replacement while the current bot remains active, and then replaces
both containers. If the new bot container exits during its startup check, the
deployment script attempts to restore the previous release.

Useful VM commands:

```bash
docker logs --tail 200 -f webull-trading-bot
docker logs --tail 200 -f webull-trading-dashboard
docker exec webull-trading-bot find /var/data/logs -type f
docker exec webull-trading-bot tail -n 200 /var/data/logs/YYYY/MM/YYYY-MM-DD.log
docker restart webull-trading-bot
```

## Complete configuration example

```dotenv
MODE=LIVE
WEBULL_APP_KEY=
WEBULL_APP_SECRET=
ACCOUNT_ID=
WEBULL_API_ENDPOINT=api.webull.com
WEBULL_ENVIRONMENT=prod
WEBULL_REGION_ID=us
LIVE_TRADING_ENABLED=true
WEBULL_MAX_ORDER_NOTIONAL_USD=1000
WEBULL_MAX_ORDER_QUANTITY=1000
WEBULL_SYMBOL_WHITELIST=
WEBULL_TOKEN_DIR=conf
WEBULL_LOG_LEVEL=WARNING

STOCK_SYMBOLS=ALL
OPTION_CONTRACTS=
OPTION_UNDERLYINGS=
OPTION_TYPE=BOTH
OPTION_MIN_DTE=7
OPTION_MAX_DTE=45
MAX_SYMBOLS=0
STOCK_UNIVERSE_PAGE_SIZE=200
STOCK_BATCH_SIZE=100
STOCK_PRIORITY_FRACTION=0.70
STOCK_PENNY_FRACTION=0.10
PENNY_STOCK_MAX_PRICE=5.00
POPULAR_STOCK_SYMBOLS=SPY,QQQ,NVDA,TSLA,AMD,AAPL,AMZN,META,MSFT,COIN,PLTR,MSTR,MARA,IONQ,RGTI,QBTS,QUBT,FCX
POPULAR_STOCK_MIN_VOLUME=1000000
POPULAR_STOCK_MAX_SPREAD_PERCENT=0.50
STOCK_POPULAR_CAPITAL_FRACTION=0.70
STOCK_PENNY_CAPITAL_FRACTION=0.10
STOCK_DISCOVERY_CAPITAL_FRACTION=0.20
TOP_GAINERS_LIMIT=200
MARKET_CAP_ALLOCATION_ENABLED=false
STOCK_LARGE_CAP_MIN_MARKET_VALUE=100000000000
STOCK_LARGE_CAP_CAPITAL_FRACTION=0.80
STOCK_SMALL_CAP_CAPITAL_FRACTION=0.20
MICRO_SCALP_ENABLED=false
MICRO_SCALP_SYMBOLS=TSLA,NVDA,AMD,COIN,PLTR,MSTR
MICRO_SCALP_CAPITAL_FRACTION=0.20
MICRO_SCALP_MAX_POSITIONS=3
MICRO_SCALP_DIP_CENTS=0.05
MICRO_SCALP_TARGET_CENTS=0.06
MICRO_SCALP_STOP_CENTS=0.10
MICRO_SCALP_REFERENCE_WINDOW=20
FRACTIONAL_SHARES_ENABLED=false
FRACTIONAL_SHARES_MIN_NOTIONAL=5
OPTION_BATCH_SIZE=20
OPTION_DISCOVERY_PER_CYCLE=1
OPTION_DISCOVERY_SECONDS=15

STOCK_QUANTITY=1
OPTION_QUANTITY=1
MAX_OPEN_POSITIONS=5
MAX_ORDER_NOTIONAL=1000

POLL_SECONDS=0.25
ACCOUNT_REFRESH_SECONDS=5
TRADE_COOLDOWN_SECONDS=15
STOCK_MAX_TRADES_PER_HOUR=12
EMA_FAST_PERIOD=3
EMA_SLOW_PERIOD=8
REENTER_ON_TREND=true
REENTER_CONFIRMATION_POLLS=2
VWAP_ENTRY_BAND_PERCENT=0.001
STOCK_MIN_NET_PROFIT_PERCENT=0.0015
STOCK_ESTIMATED_ROUND_TRIP_COST_PERCENT=0.002
STOCK_STOP_LOSS_MIN_PERCENT=0.0012
STOCK_STOP_LOSS_MAX_PERCENT=0.006
STOCK_STOP_LOSS_RANGE_MULTIPLIER=0.28
STOCK_TARGET_STOP_MULTIPLE=1.2
STOCK_ENTRY_MAX_EXTENSION_PERCENT=0.01
STOCK_OSCILLATION_WEIGHT=0.5
OPTION_TAKE_PROFIT_PRICE=0.01
OPTION_STOP_LOSS_PERCENT=0.50
MARKET_REQUESTS_PER_MINUTE=240
OPTION_INSTRUMENT_REQUESTS_PER_MINUTE=45
STOCK_INSTRUMENT_REQUESTS_PER_30_SECONDS=9
ACCOUNT_REQUESTS_PER_SECOND=0.8
ORDER_REQUESTS_PER_MINUTE=480
ORDER_TIMEOUT_SECONDS=120
ORDER_MONITOR_SECONDS=5
STOP_LOSS_ESCALATE_SECONDS=15
STALL_BREAKER_ENABLED=true
STALL_BREAKER_SECONDS=120
STALL_BREAKER_MIN_PROFIT=0.01

AGENT_ENABLED=false
GROQ_API_KEY=
GROQ_MODEL=groq/compound-mini
AGENT_CORE_RESEARCH_SECONDS=120
AGENT_EXTENDED_RESEARCH_SECONDS=622
AGENT_DAILY_REQUEST_LIMIT=250
AGENT_MAX_SYMBOLS=5
AGENT_DISCOVERY_MAX_SYMBOLS=5
AGENT_TIMEOUT_SECONDS=60
LOSS_CIRCUIT_BREAKER_ENABLED=false
LOSS_SPREE_POSITION_COUNT=3
LOSS_SPREE_TOTAL_DOLLARS=1.00
LOSS_REEVALUATION_SECONDS=120
DAILY_LOSS_CIRCUIT_BREAKER_ENABLED=false
DAILY_MAX_LOSS_DOLLARS=50

TRADING_TIMEZONE=America/New_York
MARKET_OPEN_TIME=04:00
EOD_CLOSE_TIME=19:50
MARKET_CLOSE_TIME=20:00
OPTION_MARKET_OPEN_TIME=09:30
OPTION_EOD_CLOSE_TIME=15:50
OPTION_MARKET_CLOSE_TIME=16:00
EOD_RETRY_SECONDS=10
MARKET_HOLIDAYS=
WASH_SALE_BLOCK_DAYS=31
WASH_SALE_STATE_FILE=conf/wash_sale_blocks.json
STOCK_LIMIT_OFFSET=0.005
OPTION_LIMIT_OFFSET=0.03
LOG_DIRECTORY=logs
STATUS_FILE=status.json
```
