# Live trading safety gate

The repository ships in `MODE=PAPER`. Do not switch to `LIVE` until you have confirmed the official Webull OpenAPI trade endpoint, request schema, supported order types, account permissions, and a tested sandbox/paper account. `WebullBroker.submit` and `cancel` intentionally reject requests until that work is implemented. This is a safety feature.
