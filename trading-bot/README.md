# Webull Trading Bot

Python 3.12+ trading platform scaffold using a broker abstraction. It starts in safe paper mode and never submits a live order as distributed.

## Quick start

```bash
cp .env.example .env
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
uvicorn app.dashboard.api:app --reload
```

To make a paper trade, first set a quote using `POST /paper/quote/AAPL/210.33`, then send `POST /orders/buy/AAPL/10`. Inspect state at `GET /account` and `GET /health`.

## Configuration

Keep `MODE=PAPER` during development. Copy `.env.example` to `.env`, enter `WEBULL_APP_KEY`, `WEBULL_APP_SECRET`, and `ACCOUNT_ID`, then install `webull-openapi-python-sdk`. The official SDK signs each call and handles 2FA token flow—do not set or commit a secret header. Begin with `WEBULL_API_ENDPOINT=https://api.sandbox.webull.com`; production is `https://api.webull.com`.

The official SDK broker uses `account_v2`, `order_v3`, and `data_client` for stock trading. `MODE=LIVE` is the required deliberate gate before it can submit or cancel an order. Options use the same `order_v3.place_order` endpoint with `instrument_type=OPTION`; keep `OPTIONS_ENABLED=false` until the account has options approval and you have tested its payloads in sandbox.

`EOD_FLATTEN_TIME` is the New York-time cutoff for the `EndOfDayFlattener` job. Deploy it through an external, reliable scheduler and add exchange-calendar checks, retry handling, cancellation of open orders, and an operator alert before enabling live trading. Options are controlled by `OPTIONS_ENABLED=false`; the broker must validate contract symbols, expiry, multiplier, and permissions before supporting options.

## Design

- `app/broker`: interchangeable paper and Webull adapters.
- `app/strategies`: common signal interface plus EMA-cross strategy.
- `app/risk`: mandatory buying-power, position-limit, and stop-loss risk checks.
- `app/execution`: broker-neutral `buy`, `sell`, and `cancel_order` interface.
- `app/execution/eod.py`: broker-neutral position-flattening job for end-of-day liquidation.
- `app/dashboard`: FastAPI health, account, quote, and order endpoints.

Run tests with `pytest -q`; run the service in Docker with `docker compose up --build`.

## Live-trading checklist

Read [docs/LIVE_TRADING.md](docs/LIVE_TRADING.md). Implement and integration-test confirmed official Webull order/cancel calls, authenticated dashboard access, database audit logs, notification credentials, and backtest validation before allowing live execution. Automated trading has material financial risk.
