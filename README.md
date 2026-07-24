# Compact Webull production stock and options auto trader

This application contains fewer than 15 copyable files and uses Webull's
official Python SDK against the US production API. It can:

- Download and rotate through Webull's complete tradable US stock and ETF universe.
- Trade exact OCC option contracts.
- Progressively discover current ATM calls and puts for every optionable stock.
- Scan every second, every few minutes, or up to once per hour.
- Buy on EMA crossovers or an active uptrend, take configured penny-scale
  profits, and re-enter while the uptrend remains active.
- Stop opening positions at the configured end-of-day time.
- Cancel working orders and repeatedly close account positions before 4:00 PM
  New York time.

Webull supports stock market orders. Webull options do not support market
orders, so option entries and exits use refreshed aggressive limit prices.

## Recreate the application from code

Create one folder on the destination computer and copy the contents of these
10 files into files with the same names:

```text
.env.example
.gitignore
README.md
requirements.txt
setup.ps1
config.py
strategy.py
webull_api.py
connect.py
bot.py
```

Do not copy `.venv`, `.webull-skill-venv`, or create `.env` manually. The setup
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
.\.venv\Scripts\python.exe connect.py
```

Copy the returned `account_id` into `.env`, then run the command again:

```powershell
.\.venv\Scripts\python.exe connect.py
```

It will display buying power, positions, and the first configured stock quote.

## 3. Select stocks

The default includes every tradable US stock and ETF:

```dotenv
STOCK_SYMBOLS=ALL
MAX_SYMBOLS=0
STOCK_BATCH_SIZE=100
```

The bot downloads the current lists directly from Webull at the start of each
trading day and keeps each instrument's required `US_STOCK` or `US_ETF`
category. A static ticker list is intentionally not embedded because listings
and trading status change. `MAX_SYMBOLS=0` means no cap.

If Webull's instrument directory contains a stale or delisted symbol, the bot
uses the rejected-symbol list returned by Webull, skips those symbols for the
rest of that day, and immediately retries only the valid part of the batch.

Webull accepts up to 100 stock symbols in one snapshot request. The bot rotates
through the universe in batches of 100 instead of making one request per stock.

To restrict trading to a smaller list instead:

```dotenv
STOCK_SYMBOLS=AAPL,MSFT,NVDA,SPY
MAX_SYMBOLS=0
```

To keep `ALL` but limit it to the first 500 returned tradable symbols, set
`MAX_SYMBOLS=500`. Webull's listed-equity directory can contain both stocks and
ETFs. The bot checks rejected snapshot symbols against the other category,
corrects stock/ETF mismatches, and skips only symbols invalid in both
categories. Startup prints `LOAD` progress while symbols are downloaded.

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
again after a take-profit exit while the fast EMA remains above the slow EMA.
Actual fills depend on price movement, liquidity, spreads, and the number of
configured instruments.

Fast scanning, allowing multiple instruments to trade in a minute:

```dotenv
POLL_SECONDS=1
TRADE_COOLDOWN_SECONDS=5
EMA_FAST_PERIOD=3
EMA_SLOW_PERIOD=8
REENTER_ON_TREND=true
STOCK_TAKE_PROFIT_PER_SHARE=0.01
STOCK_STOP_LOSS_PER_SHARE=0.05
OPTION_TAKE_PROFIT_PRICE=0.01
OPTION_STOP_LOSS_PRICE=0.05
```

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

`POLL_SECONDS` accepts values from 1 through 3600.

For stocks, a `STOCK_TAKE_PROFIT_PER_SHARE` value of `0.01` requests an exit
after a one-cent favorable move from the reported average cost. For options,
`OPTION_TAKE_PROFIT_PRICE=0.01` is a $0.01 premium move, normally $1 per
standard 100-share contract before spread, fees, and execution differences.

With the default five-second cooldown, the code can attempt multiple orders per
minute across several instruments. With `ALL`, each batch is processed quickly,
but the complete universe takes multiple cycles. It does not promise a fixed
trade count: EMA direction, target price movement, fills, API response time,
open-position limits, and Webull rate limits determine the actual count.

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

## 7. API request pacing

Keep these defaults unless Webull changes the limits assigned to your
application:

```dotenv
MARKET_REQUESTS_PER_MINUTE=240
OPTION_INSTRUMENT_REQUESTS_PER_MINUTE=45
STOCK_INSTRUMENT_REQUESTS_PER_30_SECONDS=9
ACCOUNT_REQUESTS_PER_SECOND=0.8
ORDER_REQUESTS_PER_MINUTE=480
```

The bot maintains an independent timer for each API group, sends stock quotes
in groups of 100 and option quotes in groups of 20, and retries throttling or
temporary server errors with backoff. Order submissions are not automatically
retried because a timed-out order may already have reached the broker.

## 8. Configure daily closeout

```dotenv
TRADING_TIMEZONE=America/New_York
MARKET_OPEN_TIME=09:30
EOD_CLOSE_TIME=15:50
MARKET_CLOSE_TIME=16:00
EOD_RETRY_SECONDS=10
```

At 3:50 PM New York time the bot stops running the strategy. It then:

1. Cancels currently reported working orders.
2. Sends market orders to close stock positions.
3. Sends aggressive limit orders to close single-leg option positions.
4. Refreshes positions.
5. Repeats cancellation and closeout every `EOD_RETRY_SECONDS` until positions
   are gone or the market closes.

The closeout operates on positions in the configured Webull account, including
positions that existed before the bot started.

Add full-day exchange holidays as comma-separated dates:

```dotenv
MARKET_HOLIDAYS=2026-01-01,2026-12-25
```

## 9. Run the bot

```powershell
.\.venv\Scripts\python.exe bot.py
```

Stop it with:

```text
Ctrl+C
```

The terminal reports selected targets, signals that become orders, API errors,
and end-of-day closeout progress in a compact colored format:

```text
09:30:00 INFO     START  | mode=LIVE | poll=1s | cooldown=5s
09:30:00 INFO     LOAD   | downloading stocks and ETFs | limit=500
09:30:04 INFO     READY  | stocks=500 | options=0 | option scan=ON
09:30:19 INFO     SCAN   | stocks=100/500 | options=0/0 | positions=0
09:30:21 INFO     ORDER  | STOCK       | BUY    | AAPL     | id=...
15:50:02 INFO     CLOSE  | submitted=3 | remaining=0
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
6. Restart `bot.py` after Webull activates the subscriptions.

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
OPTION_UNDERLYINGS=ALL
OPTION_TYPE=BOTH
OPTION_MIN_DTE=7
OPTION_MAX_DTE=45
MAX_SYMBOLS=0
STOCK_BATCH_SIZE=100
OPTION_BATCH_SIZE=20
OPTION_DISCOVERY_PER_CYCLE=1

STOCK_QUANTITY=1
OPTION_QUANTITY=1
MAX_OPEN_POSITIONS=5
MAX_ORDER_NOTIONAL=1000

POLL_SECONDS=1
TRADE_COOLDOWN_SECONDS=5
EMA_FAST_PERIOD=3
EMA_SLOW_PERIOD=8
REENTER_ON_TREND=true
STOCK_TAKE_PROFIT_PER_SHARE=0.01
STOCK_STOP_LOSS_PER_SHARE=0.05
OPTION_TAKE_PROFIT_PRICE=0.01
OPTION_STOP_LOSS_PRICE=0.05
MARKET_REQUESTS_PER_MINUTE=240
OPTION_INSTRUMENT_REQUESTS_PER_MINUTE=45
STOCK_INSTRUMENT_REQUESTS_PER_30_SECONDS=9
ACCOUNT_REQUESTS_PER_SECOND=0.8
ORDER_REQUESTS_PER_MINUTE=480

TRADING_TIMEZONE=America/New_York
MARKET_OPEN_TIME=09:30
EOD_CLOSE_TIME=15:50
MARKET_CLOSE_TIME=16:00
EOD_RETRY_SECONDS=10
MARKET_HOLIDAYS=
OPTION_LIMIT_OFFSET=0.03
```
