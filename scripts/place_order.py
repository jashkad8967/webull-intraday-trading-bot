"""Place one order through the bot's own queue, re-verifying first.

A single, explicit, auditable entry point for order placement, so that
granting it permission grants exactly this and nothing else. Everything
goes through the bot's CommandQueue rather than the broker API directly,
which means the trader's existing sizing, pricing, wash-sale checks,
position caps and trade recording all still apply - this adds a decision,
never a new order path.

    python scripts/place_order.py buy NVDA
    python scripts/place_order.py sell NVDA
    python scripts/place_order.py buy NVDA --force

WHY IT RE-VERIFIES. An automated or scheduled entry is decided at one
moment and placed at another. On 2026-10-01 a queued NVDA buy was lost in
a container restart and only found by checking positions afterwards; a
second one was refused for sizing ten seconds after being queued. A setup
can also simply evaporate: ACN that day went from +18% to 14% of its
daily range. So a BUY re-checks, against live quotes, the criteria that
actually produced the two profitable trades of that session:

    up on the day, and
    range position >= --min-range (default 70%), and
    spread <= --max-spread (default 0.10%)

and refuses otherwise. Range position is (last - low) / (high - low):
where price sits in today's range, NOT range_ratio, which is how wide
the range is. ACN (+18% gap, top-4 by the bot's own priority_score) and
GOOGL (29.6 score, second highest) were both near their lows and both
would have been bought on gap or score alone.

--force skips re-verification. It exists for closing or for a deliberate
override, and it says so in the log line, because an unexplained order is
the thing that wastes the most time later.

SELL never re-verifies: refusing to exit because a setup looks wrong is
backwards.
"""

import argparse
import logging
import sys
from decimal import Decimal

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("place-order")


def _range_position(quote, api):
    last = api.quote_price(quote)
    high = quote.get("high")
    low = quote.get("low")
    if last is None or high in (None, "") or low in (None, ""):
        return None, last
    high, low, last = Decimal(str(high)), Decimal(str(low)), Decimal(str(last))
    if high <= low:
        return None, last
    return (last - low) / (high - low), last


def main(argv) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # "stop" places a RESTING broker-side protective stop. It is grouped
    # with buy/sell here so the single permitted entry point covers it,
    # and it is worth being explicit that this widens that permission:
    # it can only ever submit a SELL stop against an existing position,
    # so it reduces exposure and cannot open any. It is the one order
    # type that keeps protecting a position while this process is not
    # running - which on 2026-10-02 was four hours forty.
    parser.add_argument("side", choices=["buy", "sell", "stop"])
    parser.add_argument("symbol")
    parser.add_argument("--stop-price", type=Decimal, default=None,
                        help="trigger price for 'stop'; defaults to the "
                             "configured stock stop below the last price")
    parser.add_argument("--instrument", default="EQUITY",
                        choices=["EQUITY", "OPTION"])
    parser.add_argument("--min-range", type=Decimal, default=Decimal("0.70"))
    parser.add_argument("--max-spread", type=Decimal, default=Decimal("0.0010"))
    parser.add_argument("--force", action="store_true",
                        help="skip re-verification (closing, or a "
                             "deliberate override)")
    args = parser.parse_args(argv)

    from webull_bot.commands import CommandQueue
    from webull_bot.config import Settings
    from webull_bot.operator_overrides import OperatorOverrides

    settings = Settings()
    symbol = args.symbol.upper()

    # The standing operator veto applies here too. A manual path that
    # ignored it would make the veto worthless: it is meant to be the
    # last word on "do not trade that name", not advice to the automatic
    # paths only.
    overrides = OperatorOverrides(settings.operator_overrides_state_file)
    if args.side == "buy":
        if overrides.entries_halted():
            log.error("REFUSED %s: operator halt is set on new entries",
                      symbol)
            return 2
        if overrides.symbol_blocked(symbol):
            log.error("REFUSED %s: standing operator veto on this symbol",
                      symbol)
            return 2

    if args.side == "buy" and not args.force:
        from webull_bot.webull_api import WebullAPI

        api = WebullAPI(settings)
        try:
            quotes, _ = api.stock_quotes_resilient([symbol], "US_STOCK")
        except Exception as exc:
            log.error("REFUSED %s: quote failed, not placing blind | %s",
                      symbol, exc)
            return 3
        if not quotes:
            log.error("REFUSED %s: no quote returned", symbol)
            return 3
        quote = quotes[0]
        position, last = _range_position(quote, api)
        change = Decimal(str(quote.get("change_ratio") or 0))
        bid = Decimal(str(quote.get("bid") or 0))
        ask = Decimal(str(quote.get("ask") or 0))
        spread = (
            (ask - bid) / ((ask + bid) / 2) if bid > 0 and ask > 0
            else Decimal("1")
        )

        why = []
        if change <= 0:
            why.append(f"down {change * 100:.2f}% on the day")
        if position is None:
            why.append("range position unavailable")
        elif position < args.min_range:
            why.append(f"range position {position * 100:.0f}% "
                       f"< {args.min_range * 100:.0f}%")
        if spread > args.max_spread:
            why.append(f"spread {spread * 100:.2f}% "
                       f"> {args.max_spread * 100:.2f}%")
        if why:
            log.error("REFUSED %s @ %s: %s", symbol, last, "; ".join(why))
            return 1
        log.info(
            "VERIFIED %s @ %s | %+.2f%% on the day | range %.0f%% | "
            "spread %.2f%%",
            symbol, last, change * 100, position * 100, spread * 100,
        )

    if args.side == "stop":
        # Placed DIRECTLY, not through the command queue. The queue is
        # drained by the trader once per cycle, and the whole value of a
        # resting stop is that it exists at the broker independently of
        # this bot's loop - routing it through a queue the bot has to be
        # alive to read would defeat the point.
        from webull_bot.webull_api import WebullAPI

        api = WebullAPI(settings)
        quantity, _cost = api.stock_position(symbol, api.positions())
        if quantity <= 0:
            log.error("REFUSED %s: no position to protect", symbol)
            return 4
        stop_price = args.stop_price
        if stop_price is None:
            quotes, _ = api.stock_quotes_resilient([symbol], "US_STOCK")
            if not quotes:
                log.error("REFUSED %s: no quote to derive a stop from", symbol)
                return 3
            last = api.quote_price(quotes[0])
            stop_price = last * (
                Decimal("1") - settings.stock_stop_loss_max_percent
            )
        try:
            order_id = api.place_stock_stop_loss(symbol, quantity, stop_price)
        except ValueError as exc:
            # The fractional case lands here, with its reason intact.
            log.error("REFUSED %s: %s", symbol, exc)
            return 5
        log.warning(
            "RESTING STOP placed | %s qty=%s stop=%s | id=%s | this survives "
            "a sleep, a crash, and this process not running",
            symbol, quantity, stop_price, order_id,
        )
        return 0

    queue = CommandQueue(settings.command_file)
    command_id = queue.enqueue(
        args.side, symbol=symbol, instrument_type=args.instrument
    )
    log.warning(
        "QUEUED %s %s (%s)%s | id=%s | the trader executes it on its next "
        "cycle - confirm with the CMD lines in the log",
        args.side.upper(), symbol, args.instrument,
        " [FORCED, re-verification skipped]" if args.force else "",
        command_id,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
