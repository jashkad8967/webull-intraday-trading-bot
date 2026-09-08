import logging
from decimal import ROUND_DOWN, Decimal

from webull_bot.pairs import PAIRS, PAIRS_CAPITAL_FRACTION, PAIRS_MAX_CONCURRENT
from webull_bot.webull_api import QuoteUnavailableError

log = logging.getLogger("webull-bot")


def trade_pairs(self, positions: list[dict], buying_power: Decimal) -> Decimal:
    """Correlated-pairs mean reversion: long the relatively cheap leg,
    short the relatively expensive one, when the spread between two
    historically-correlated stocks stretches to a statistical extreme,
    and unwind when it reverts. See src/webull_bot/pairs.py. Its own
    capital slice (PAIRS_CAPITAL_FRACTION), carved out up front, so it
    never competes with the main scan's budget for the rest of the
    cycle.
    """
    if not PAIRS:
        return buying_power
    capital_budget = buying_power * PAIRS_CAPITAL_FRACTION
    per_pair_budget = (
        capital_budget / PAIRS_MAX_CONCURRENT if PAIRS_MAX_CONCURRENT else Decimal("0")
    )
    for pair in PAIRS:
        symbol_a, symbol_b = pair
        try:
            quote_a = self.api.stock_quote(
                symbol_a, self.stock_categories.get(symbol_a, "US_STOCK")
            )
            quote_b = self.api.stock_quote(
                symbol_b, self.stock_categories.get(symbol_b, "US_STOCK")
            )
            price_a = self.api.quote_price(quote_a)
            price_b = self.api.quote_price(quote_b)
        except Exception as exc:
            if isinstance(exc, QuoteUnavailableError):
                continue
            log.error("PAIRS  | %s/%s | quote failed | %s", symbol_a, symbol_b, exc)
            continue
        self.pairs.update(pair, price_a, price_b)
        quote_by_symbol = {symbol_a: quote_a, symbol_b: quote_b}
        held = self.pairs_positions.get(pair)
        decision = self.pairs.decision(pair, is_open=held is not None)

        if held is None:
            if decision.action not in (
                "ENTER_LONG_A_SHORT_B",
                "ENTER_LONG_B_SHORT_A",
            ):
                continue
            if not self.short_selling_supported:
                continue
            if len(self.pairs_positions) >= PAIRS_MAX_CONCURRENT:
                continue
            key_a, key_b = f"STOCK:{symbol_a}", f"STOCK:{symbol_b}"
            if not (
                self.cooldown_ready(key_a)
                and self.cooldown_ready(key_b)
                and self.reentry_cooldown_ready(key_a)
                and self.reentry_cooldown_ready(key_b)
                and not self.rate_capped(key_a)
                and not self.rate_capped(key_b)
                and symbol_a not in self.broker_conflict_symbols
                and symbol_b not in self.broker_conflict_symbols
                and not self.wash_sales.blocked_until(symbol_a)
                and not self.wash_sales.blocked_until(symbol_b)
            ):
                continue
            existing_a, _ = self.api.stock_position(symbol_a, positions)
            existing_b, _ = self.api.stock_position(symbol_b, positions)
            if existing_a != 0 or existing_b != 0:
                continue
            leg_budget = min(per_pair_budget, buying_power) / 2
            qty_a = int((leg_budget / price_a).to_integral_value(rounding=ROUND_DOWN))
            qty_b = int((leg_budget / price_b).to_integral_value(rounding=ROUND_DOWN))
            if qty_a <= 0 or qty_b <= 0:
                continue
            if decision.action == "ENTER_LONG_A_SHORT_B":
                long_symbol, long_qty = symbol_a, qty_a
                short_symbol, short_qty = symbol_b, qty_b
            else:
                long_symbol, long_qty = symbol_b, qty_b
                short_symbol, short_qty = symbol_a, qty_a
            try:
                long_order = self.place_stock_scaled(
                    long_symbol,
                    "BUY",
                    long_qty,
                    f"STOCK:{long_symbol}",
                    quote_by_symbol[long_symbol],
                )
            except Exception as exc:
                log.error(
                    "PAIRS  | %s/%s | long leg entry failed | %s",
                    symbol_a,
                    symbol_b,
                    exc,
                )
                continue
            if long_order is None:
                continue
            try:
                short_order = self.place_stock_scaled(
                    short_symbol,
                    "SHORT",
                    short_qty,
                    f"STOCK:{short_symbol}",
                    quote_by_symbol[short_symbol],
                )
            except Exception as exc:
                # The long leg is already working/filled with no short
                # hedge behind it - unwind it immediately rather than
                # leave a naked, unintended long. This is not a rare
                # path on a sub-$2,000 account: Webull rejects every
                # short with CAN_NOT_SELL_SHORT_FOR_LT_2K there, so
                # this fires on every pairs entry attempt until equity
                # clears that minimum.
                if self.is_short_selling_unsupported(exc):
                    self.handle_short_selling_unsupported(exc)
                elif self.is_fractional_ticker_unsupported(exc):
                    self.handle_fractional_ticker_unsupported(short_symbol, exc)
                else:
                    log.error(
                        "PAIRS  | %s/%s | short leg entry failed | %s",
                        symbol_a,
                        symbol_b,
                        exc,
                    )
                try:
                    self.api.place_stock(
                        long_symbol,
                        "SELL",
                        long_qty,
                        limit_price=self.api.stock_limit_price(
                            quote_by_symbol[long_symbol], "SELL"
                        ),
                    )
                except Exception as unwind_exc:
                    log.error(
                        "PAIRS  | %s | failed to unwind orphaned long "
                        "leg after short leg rejection - check the "
                        "Webull app for a stuck naked position | %s",
                        long_symbol,
                        unwind_exc,
                    )
                continue
            if short_order is None:
                # The long leg is already working/filled with no
                # short hedge behind it - unwind it immediately
                # rather than leave a naked, unintended long.
                self.api.place_stock(
                    long_symbol,
                    "SELL",
                    long_qty,
                    limit_price=self.api.stock_limit_price(
                        quote_by_symbol[long_symbol], "SELL"
                    ),
                )
                continue
            self.record_trade(
                f"STOCK:{long_symbol}",
                long_order,
                "BUY",
                entry_price=self.api.stock_limit_price(
                    quote_by_symbol[long_symbol], "BUY"
                ),
                quantity=long_qty,
            )
            self.record_trade(
                f"STOCK:{short_symbol}",
                short_order,
                "BUY",
                entry_price=self.api.stock_limit_price(
                    quote_by_symbol[short_symbol], "SHORT"
                ),
                quantity=short_qty,
            )
            self.position_buckets[long_symbol] = "PAIRS_LONG"
            self.position_buckets[short_symbol] = "PAIRS_SHORT"
            self.pairs_positions[pair] = {"long": long_symbol, "short": short_symbol}
            self.pairs.mark_entered(pair)
            buying_power = max(Decimal("0"), buying_power - leg_budget * 2)
            log.info(
                "PAIRS  | %s/%s | entered | long=%s(%s) short=%s(%s) | z=%.2f",
                symbol_a,
                symbol_b,
                long_symbol,
                long_qty,
                short_symbol,
                short_qty,
                decision.z_score,
            )
            continue

        if decision.action not in ("UNWIND", "STOP"):
            continue
        long_symbol, short_symbol = held["long"], held["short"]
        long_qty, long_cost = self.api.stock_position(long_symbol, positions)
        # A short position's quantity is reported negative (same
        # convention close_all_positions already relies on) - normalize
        # to a positive magnitude for order sizing/pnl below, but the
        # sign itself is what tells us whether the short is still open.
        short_position_qty, short_cost = self.api.stock_position(
            short_symbol, positions
        )
        short_qty = -short_position_qty if short_position_qty < 0 else Decimal("0")
        if long_qty <= 0 and short_qty <= 0:
            self.pairs_positions.pop(pair, None)
            self.pairs.mark_exited(pair)
            continue
        try:
            if long_qty > 0:
                sell_price = self.api.stock_limit_price(
                    quote_by_symbol[long_symbol], "SELL"
                )
                order_id = self.api.place_stock(
                    long_symbol, "SELL", long_qty, limit_price=sell_price
                )
                pnl = self.record_realized_exit(long_cost, sell_price, long_qty)
                self.record_trade(
                    f"STOCK:{long_symbol}",
                    order_id,
                    "PROFIT" if decision.action == "UNWIND" else "STOP",
                    sell_price,
                    pnl=pnl,
                    entry_price=long_cost,
                    quantity=long_qty,
                )
            if short_qty > 0:
                # Covering submits as a plain "BUY" order (Webull has
                # no fourth order side), priced via the "COVER" pricing
                # branch (crosses above the ask) so it fills with the
                # same urgency any other forced exit gets.
                cover_price = self.api.stock_limit_price(
                    quote_by_symbol[short_symbol], "COVER"
                )
                order_id = self.api.place_stock(
                    short_symbol, "BUY", short_qty, limit_price=cover_price
                )
                pnl = self.record_realized_exit(short_cost, cover_price, short_qty, multiplier=-1)
                self.record_trade(
                    f"STOCK:{short_symbol}",
                    order_id,
                    "PROFIT" if decision.action == "UNWIND" else "STOP",
                    cover_price,
                    pnl=pnl,
                    entry_price=short_cost,
                    quantity=short_qty,
                )
        except Exception as exc:
            log.error(
                "PAIRS  | %s/%s | unwind failed | %s", symbol_a, symbol_b, exc
            )
            continue
        self.pairs_positions.pop(pair, None)
        self.pairs.mark_exited(pair)
        log.info(
            "PAIRS  | %s/%s | unwound (%s) | z=%.2f",
            symbol_a,
            symbol_b,
            decision.reason,
            decision.z_score,
        )
    return buying_power
