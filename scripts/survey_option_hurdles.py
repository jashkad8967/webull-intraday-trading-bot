"""Rank candidate option contracts by ROUND-TRIP COST, not by premium.

The measurement that was missing on 2026-10-01. Four losing sessions
were spent tuning exits while the real defect was that every contract
entered opened ~8.1% underwater on the bid against a 10% stop, leaving
1.9% of usable room - 9 of 10 recorded positions never traded above
cost+fee at any point in their lives.

The hurdle is

    (half the quoted spread + the exit fee) / bid

and the dominant term is the SPREAD, not the premium. That distinction
matters because it inverts the conclusion: a $0.60 contract quoted 2
cents wide has a 3.3% hurdle and is tradeable on a small account, while
a $1.30 contract quoted 9 cents wide has an 11.5% hurdle and is a
guaranteed loss before direction is considered. Screening on premium
alone cannot tell those apart.

So this surveys real chains and ranks what is actually affordable by
hurdle, which is what option_max_entry_hurdle_fraction gates on.

Read-only: pulls chains and quotes, places no orders.

    python scripts/survey_option_hurdles.py SPY QQQ IWM ACN PLTR

Webull allows at most 20 option symbols per snapshot call, so this
samples a bounded number of strikes per underlying rather than quoting
whole chains - the API budget is shared with the live trading loop.
"""

import sys
from datetime import date
from decimal import Decimal

FEE = Decimal("0.02")  # option_sell_fee_per_contract / 100
BATCH = 20  # Webull's hard cap per option snapshot call


def survey(api, underlying, max_premium, stop, dte_lo=7, dte_hi=21,
           batches=2):
    """Return (hurdle, option_symbol, bid, ask) rows, cheapest hurdle
    first, for affordable two-sided quotes on this underlying.
    """
    today = date.today()
    try:
        chain = api.option_contracts(underlying=underlying)
    except Exception as exc:
        return [], f"chain error: {type(exc).__name__}"

    eligible = []
    for contract in chain:
        expiration = contract.get("expiration_date") or ""
        try:
            dte = (date.fromisoformat(expiration) - today).days
        except Exception:
            continue
        if dte_lo <= dte <= dte_hi and contract.get("symbol"):
            eligible.append(contract)
    if not eligible:
        return [], f"no contract in the {dte_lo}-{dte_hi} DTE window"

    # Nearest expiration first, then spread the sample across strikes
    # rather than taking 20 adjacent ones - adjacent strikes are all
    # equally far from the money and tell us nothing.
    eligible.sort(key=lambda c: (str(c.get("expiration_date")),
                                 str(c.get("strike_price"))))
    wanted = BATCH * max(1, batches)
    step = max(1, len(eligible) // wanted)
    sampled, seen = [], set()
    for contract in eligible[::step]:
        symbol = contract.get("symbol")
        if symbol and symbol not in seen:
            seen.add(symbol)
            sampled.append(symbol)
        if len(sampled) >= wanted:
            break

    rows, errors = [], []
    for start in range(0, len(sampled), BATCH):
        chunk = sampled[start:start + BATCH]
        try:
            quotes = api.option_quotes(chunk)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}")
            continue
        for quote in quotes:
            bid, ask = api.quote_bid(quote), api.quote_ask(quote)
            if not bid or not ask or ask < bid or bid <= 0:
                continue
            if bid * 100 > max_premium:
                continue
            hurdle = ((ask - bid) / 2 + FEE) / bid
            rows.append((hurdle, quote.get("symbol"), bid, ask))
    rows.sort(key=lambda r: r[0])
    note = f"quote errors: {','.join(errors)}" if errors and not rows else ""
    return rows, note


def main(argv):
    from webull_bot.config import Settings
    from webull_bot.webull_api import WebullAPI

    settings = Settings()
    api = WebullAPI(settings)
    stop = settings.option_stop_loss_percent
    fraction = settings.option_max_entry_hurdle_fraction
    ceiling = stop * fraction

    try:
        power = api.option_buying_power()
    except Exception:
        power = None
    max_premium = (
        power * settings.option_capital_fraction
        if power else Decimal("84")
    )

    print(f"stop {stop * 100:.0f}% x hurdle fraction {fraction} "
          f"-> a contract must sit under {ceiling * 100:.1f}% to be entered")
    print(f"max premium ${max_premium / 100:.2f}/share "
          f"(${max_premium:.0f}/contract)"
          f"{'' if power else '  [buying power unavailable, assumed]'}\n")

    print(f"{'underlying':<11} {'contract':<22} {'bid':>5} {'ask':>5} "
          f"{'spr':>5} {'hurdle':>7} {'room':>6}  verdict")
    print("-" * 78)
    tradeable = []
    for underlying in argv or ["SPY", "QQQ", "IWM"]:
        rows, note = survey(api, underlying, max_premium, stop)
        if not rows:
            print(f"{underlying:<11} {note or 'nothing affordable+two-sided'}")
            continue
        for hurdle, symbol, bid, ask in rows[:3]:
            passes = hurdle <= ceiling
            if passes:
                tradeable.append((hurdle, underlying, symbol, bid, ask))
            print(f"{underlying:<11} {str(symbol)[:22]:<22} {bid:>5} {ask:>5} "
                  f"{ask - bid:>5} {hurdle * 100:>6.1f}% "
                  f"{(stop - hurdle) * 100:>5.1f}%  "
                  f"{'TRADEABLE' if passes else 'refused'}")

    print("-" * 78)
    if tradeable:
        tradeable.sort(key=lambda r: r[0])
        print(f"\n{len(tradeable)} contract(s) clear the gate. Best:")
        for hurdle, underlying, symbol, bid, ask in tradeable[:5]:
            print(f"  {underlying:<6} {symbol:<22} {bid}/{ask} "
                  f"hurdle {hurdle * 100:.1f}%  ${bid * 100:.0f}/contract")
    else:
        print("\nNothing clears the gate. That is the gate working, not a "
              "fault: every affordable contract costs more in round-trip "
              "friction than half its own stop budget.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
