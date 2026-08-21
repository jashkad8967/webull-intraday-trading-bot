"""Deterministic synthetic scenario suite for the paper-sim-gate CI check.

Every scenario below is fully synthetic: constructed prices, quotes, and
timestamps fed directly into TradingStrategy/AutoTrader's real decision
methods (the same methods trade_stocks/trade_options call in production) -
no network calls, no dependency on real-time market data, no broker
credentials. That is what makes this suite fast (well under a second) and
exactly repeatable run to run, which a live paper-trading session sitting
through real market hours could never be.

This suite is a system-level sanity net, not a replacement for
tests/test_strategy_and_logging.py's much finer-grained unit coverage of
these same functions - paper-sim-gate.yml runs both, and either failing
fails the gate. Each scenario function below takes no arguments, uses plain
`assert` to state its expectation, and either returns normally (pass) or
raises (fail, message captured by the runner).
"""

import time
from collections import defaultdict, deque
from decimal import Decimal
from types import SimpleNamespace

from webull_bot.bot import AutoTrader
from webull_bot.strategy import TradingStrategy


SCENARIOS: list = []


def scenario(func):
    SCENARIOS.append(func)
    return func


class ScenarioResult:
    def __init__(self, name: str, passed: bool, detail: str = ""):
        self.name = name
        self.passed = passed
        self.detail = detail


def run_all() -> list:
    results = []
    for func in SCENARIOS:
        try:
            func()
        except AssertionError as exc:
            results.append(ScenarioResult(func.__name__, False, str(exc)))
        except Exception as exc:  # a crash is as much a failure as a bad assertion
            results.append(
                ScenarioResult(func.__name__, False, f"{type(exc).__name__}: {exc}")
            )
        else:
            results.append(ScenarioResult(func.__name__, True))
    return results


def _strategy_config(**overrides) -> SimpleNamespace:
    """A full TradingStrategy config, matching every field
    tests/test_strategy_and_logging.py's StrategyConfigMixin uses plus the
    Part C feature flags - defaults mirror config.py's real production
    defaults so a scenario passing here means something about real
    behavior, not just about a permissive test fixture.
    """
    defaults = dict(
        ema_fast_period=3,
        ema_slow_period=8,
        stock_batch_size=5,
        stock_priority_fraction=0.6,
        stock_penny_fraction=0.2,
        stock_oscillation_weight=Decimal("0.5"),
        most_active_priority_bonus=Decimal("15"),
        penny_stock_max_price=Decimal("5"),
        popular_stock_min_volume=1_000_000,
        popular_stock_max_spread_percent=Decimal("0.50"),
        reenter_on_trend=True,
        reenter_confirmation_polls=2,
        tick_direction_enabled=False,
        tick_direction_window=10,
        tick_direction_veto_threshold=Decimal("0"),
        vwap_entry_band_percent=Decimal("0.001"),
        stock_min_net_profit_percent=Decimal("0.0001"),
        stock_estimated_round_trip_cost_percent=Decimal("0.002"),
        sell_fee_dollars=Decimal("0.02"),
        stock_stop_loss_min_percent=Decimal("0.0015"),
        stock_stop_loss_max_percent=Decimal("0.02"),
        stock_stop_loss_range_multiplier=Decimal("0.35"),
        stock_target_stop_multiple=Decimal("1.2"),
        stock_price_sanity_percent=Decimal("0.15"),
        stock_entry_max_spread_percent=Decimal("0.15"),
        stock_entry_max_extension_percent=Decimal("0.01"),
        stock_core_session_position_fraction=Decimal("0.10"),
        sma_trend_filter_enabled=False,
        short_selling_enabled=True,
        opening_grace_spread_multiplier=Decimal("2"),
        opening_grace_extension_multiplier=Decimal("2"),
        option_take_profit_percent=Decimal("0.75"),
        option_stop_loss_percent=Decimal("0.50"),
        option_min_hold_dte=2,
        option_capital_fraction=Decimal("0.05"),
        option_quantity=1,
        max_order_notional=Decimal("1000"),
        fractional_shares_min_notional=Decimal("5"),
        agent_exit_influence_enabled=True,
        agent_exit_min_confidence=Decimal("0.60"),
        agent_runner_bias_threshold=Decimal("0.50"),
        agent_runner_profit_percent=Decimal("0.01"),
        agent_derisk_bias_threshold=Decimal("-0.50"),
        time_aware_stop_enabled=False,
        time_aware_stop_widen_seconds=60,
        time_aware_stop_widen_multiplier=Decimal("1.5"),
        # Off by default here so existing scenarios keep testing only the
        # trend strategy they were written for - the volatility-scalp
        # path itself is covered directly in
        # tests/test_strategy_and_logging.py, not via a paper-sim
        # scenario.
        volatility_scalp_enabled=False,
        volatility_scalp_lookback_samples=20,
        volatility_scalp_min_stdev_percent=Decimal("0.015"),
        volatility_scalp_dip_entry_percent=Decimal("0.005"),
        volatility_scalp_target_percent=Decimal("0.005"),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _quote(
    symbol: str,
    price: str,
    bid: str | None = None,
    ask: str | None = None,
    high: str | None = None,
    low: str | None = None,
    volume: str = "5000000",
) -> dict:
    return {
        "symbol": symbol,
        "bid": bid if bid is not None else price,
        "ask": ask if ask is not None else price,
        "high": high if high is not None else price,
        "low": low if low is not None else price,
        "volume": volume,
    }


def _feed_uptrend(strategy: TradingStrategy, key: str, start: Decimal) -> Decimal:
    """Feeds a clean, monotonic downtrend-then-uptick through trend_signal
    to produce a fresh bullish EMA cross, then returns the price the very
    next stock_decision call should evaluate. Mirrors the exact recipe
    tests/test_strategy_and_logging.py already uses to drive a real BUY.
    """
    price = start
    for _ in range(9):
        strategy.trend_signal(key, price)
        price -= Decimal("0.1")
    strategy.trend_signal(key, price)
    return price + Decimal("0.1")


# --- Entry gates under adverse open/quote conditions -----------------------


@scenario
def gap_up_open_resolves_without_crashing():
    strategy = TradingStrategy(_strategy_config())
    key = "STOCK:GAPUP"
    strategy.update_stock_snapshot(_quote("GAPUP", "50", high="55", low="49"), Decimal("50"))
    entry_price = _feed_uptrend(strategy, key, Decimal("50"))
    decision = strategy.stock_decision(key, entry_price, 0, Decimal("0"))
    assert decision.action in ("BUY", "HOLD", "SHORT"), decision.action


@scenario
def gap_down_open_resolves_without_crashing():
    strategy = TradingStrategy(_strategy_config())
    key = "STOCK:GAPDN"
    strategy.update_stock_snapshot(_quote("GAPDN", "50", high="51", low="45"), Decimal("50"))
    price = Decimal("50")
    for _ in range(9):
        strategy.trend_signal(key, price)
        price += Decimal("0.1")
    strategy.trend_signal(key, price)
    decision = strategy.stock_decision(key, price + Decimal("0.1"), 0, Decimal("0"))
    assert decision.action in ("BUY", "HOLD", "SHORT"), decision.action


@scenario
def high_volatility_whipsaw_never_crashes():
    strategy = TradingStrategy(_strategy_config())
    key = "STOCK:WHIP"
    price = Decimal("100")
    swing = Decimal("3")
    for tick in range(40):
        price += swing if tick % 2 == 0 else -swing
        decision = strategy.stock_decision(key, price, 0, Decimal("0"))
        assert decision.action in ("BUY", "SHORT", "HOLD"), decision.action


@scenario
def thin_spread_quote_does_not_block_an_otherwise_good_entry():
    strategy = TradingStrategy(_strategy_config())
    key = "STOCK:THIN"
    strategy.update_stock_snapshot(
        _quote("THIN", "50", bid="49.99", ask="50.01"), Decimal("50")
    )
    entry_price = _feed_uptrend(strategy, key, Decimal("50"))
    decision = strategy.stock_decision(key, entry_price, 0, Decimal("0"))
    assert "spread" not in decision.reason, decision.reason


@scenario
def wide_spread_quote_blocks_entry():
    strategy = TradingStrategy(_strategy_config())
    key = "STOCK:WIDE"
    # A ~5% spread is far past STOCK_ENTRY_MAX_SPREAD_PERCENT's 0.15%
    # default - must reject regardless of how clean the EMA cross is.
    strategy.update_stock_snapshot(
        _quote("WIDE", "50", bid="48.75", ask="51.25"), Decimal("50")
    )
    entry_price = _feed_uptrend(strategy, key, Decimal("50"))
    decision = strategy.stock_decision(key, entry_price, 0, Decimal("0"))
    assert decision.action == "HOLD", decision.action
    assert "spread" in decision.reason, decision.reason


@scenario
def flat_no_signal_condition_holds_without_crashing():
    # short_selling_enabled=False here: a perfectly flat series' EMA spread
    # sits exactly at zero, which trend_signal's re-entry streak logic
    # (correctly, by its own existing rules) eventually reads as a
    # continuing bearish signal - this scenario is about the no-new-
    # information case resolving to HOLD, so it isolates that from the
    # short-selling path entirely rather than asserting on that edge case.
    strategy = TradingStrategy(_strategy_config(short_selling_enabled=False))
    key = "STOCK:FLAT"
    for _ in range(15):
        decision = strategy.stock_decision(key, Decimal("20"), 0, Decimal("0"))
    assert decision.action == "HOLD", decision.action


# --- Fractional lot-size boundary ($0.10-$0.999) ----------------------------


@scenario
def fractional_lot_boundary_never_sizes_an_order_webull_would_reject():
    """Regression net for the OPTT/XHG-class bug fixed earlier: Webull
    rejects any order under 100 shares for a stock priced $0.10-$0.999, so
    neither fractional sizing path may ever return a fractional (< 1,
    non-integer) quantity in that band.
    """
    strategy = TradingStrategy(_strategy_config())
    # The restricted band is exactly $0.10-$0.999 - stay within whole
    # cents 10..99 so every price in this loop is genuinely inside it.
    for cents in range(10, 100, 7):
        price = Decimal(cents) / Decimal("100")
        dollar_qty, _ = strategy.dollar_stock_quantity(price, Decimal("500"))
        fallback_qty = strategy.fractional_stock_quantity(price, Decimal("500"))
        assert dollar_qty == 0, f"${price} dollar_stock_quantity={dollar_qty}"
        assert fallback_qty == 0, f"${price} fractional_stock_quantity={fallback_qty}"


@scenario
def fractional_sizing_works_normally_just_outside_the_boundary():
    # fractional_stock_quantity caps total spend at one share's price, so
    # a tiny FRACTIONAL_SHARES_MIN_NOTIONAL here isolates the lot-size
    # boundary itself (the thing this scenario is about) from that
    # separate, unrelated min-notional economics gate.
    strategy = TradingStrategy(
        _strategy_config(fractional_shares_min_notional=Decimal("0.01"))
    )
    just_below_band = strategy.fractional_stock_quantity(Decimal("0.09"), Decimal("50"))
    just_above_band = strategy.fractional_stock_quantity(Decimal("1.00"), Decimal("50"))
    assert just_below_band > 0, just_below_band
    assert just_above_band > 0, just_above_band


@scenario
def fractional_exit_pricing_resolves_cleanly_across_the_boundary():
    strategy = TradingStrategy(_strategy_config())
    key = "STOCK:FRAC"
    for cents in range(10, 1000, 41):
        price = Decimal(cents) / Decimal("100")
        cost = price * Decimal("0.98")
        decision = strategy.stock_decision(key, price, Decimal("0.5"), cost)
        assert decision.action in ("HOLD", "PROFIT", "LOSS"), decision.action
        assert decision.target_price is None or decision.target_price.is_finite()


# --- Stop-loss guard: trips on a fast string of stops, then clears ---------


@scenario
def stop_loss_guard_trips_then_clears():
    now = time.monotonic()
    config = SimpleNamespace(
        stop_loss_guard_enabled=True,
        stop_loss_guard_trade_limit=4,
        stop_loss_guard_lookback_seconds=1,
        stop_loss_guard_cooldown_seconds=1,
    )
    fake_bot = SimpleNamespace(
        config=config,
        recent_stop_losses=deque([now - 0.9, now - 0.8, now - 0.7, now - 0.6]),
        stop_loss_guard_until=0.0,
    )
    guard = AutoTrader.stop_loss_guard_active.__get__(fake_bot)
    assert guard() is True

    fake_bot.stop_loss_guard_until = time.monotonic() - 10
    fake_bot.recent_stop_losses = deque(t - 20 for t in fake_bot.recent_stop_losses)
    assert guard() is False


# --- Part C features, exercised individually --------------------------------


@scenario
def symbol_quarantine_trips_on_one_symbol_and_leaves_others_alone():
    now = time.monotonic()
    config = SimpleNamespace(
        symbol_quarantine_enabled=True,
        symbol_quarantine_lookback_seconds=1800,
        symbol_quarantine_min_trades=3,
        symbol_quarantine_loss_dollars=Decimal("0.50"),
        symbol_quarantine_cooldown_seconds=900,
    )
    history = defaultdict(deque)
    history["STOCK:LOSER"] = deque(
        [(now - 10, Decimal("-1")), (now - 20, Decimal("-1")), (now - 30, Decimal("-1"))]
    )
    fake_bot = SimpleNamespace(
        config=config, symbol_pnl_history=history, symbol_quarantine_until={}
    )
    quarantined = AutoTrader.symbol_quarantined.__get__(fake_bot)
    assert quarantined("STOCK:LOSER") is True
    assert quarantined("STOCK:WINNER") is False


@scenario
def time_aware_stop_widens_then_tightens_on_schedule():
    config = _strategy_config(
        time_aware_stop_enabled=True,
        time_aware_stop_widen_seconds=60,
        time_aware_stop_widen_multiplier=Decimal("3"),
    )
    strategy = TradingStrategy(config)
    strategy.metrics["AAPL"] = {"range_ratio": Decimal("0")}
    cost = Decimal("100")
    normal_stop_percent = strategy.adaptive_stop_percent("AAPL")
    dip_price = cost * (Decimal("1") - normal_stop_percent)

    fresh = strategy.stock_decision(
        "STOCK:AAPL", dip_price, 10, cost, seconds_since_entry=5
    )
    aged = strategy.stock_decision(
        "STOCK:AAPL", dip_price, 10, cost, seconds_since_entry=120
    )
    assert fresh.action != "LOSS", "a fresh entry must survive a dip within its grace window"
    assert aged.action == "LOSS", "the same dip must stop out once the grace window elapses"


@scenario
def regime_gate_blocks_in_a_spiking_regime_and_allows_in_a_calm_one():
    calm_history = deque([Decimal(x) for x in range(1, 21)])
    spiking_current = calm_history[-1]  # 20 - top of its own 20-sample range
    calm_current = Decimal(5)  # mid-range
    percentile = Decimal("0.85")
    assert (
        TradingStrategy.stock_market_regime_ok(calm_history, spiking_current, percentile)
        is False
    )
    assert (
        TradingStrategy.stock_market_regime_ok(calm_history, calm_current, percentile)
        is True
    )


# --- Day-boundary sweep: fractional positions never survive past core close


@scenario
def fractional_pre_close_sweep_closes_only_profitable_fractional_positions():
    positions = [
        {"instrument_type": "EQUITY", "symbol": "MSFT", "quantity": "0.011", "cost_price": "493.23"},
        {"instrument_type": "EQUITY", "symbol": "BABA", "quantity": "0.109", "cost_price": "128.93"},
        {"instrument_type": "EQUITY", "symbol": "FPE", "quantity": "1", "cost_price": "17.81"},
    ]
    quotes = {"MSFT": "497.33", "BABA": "122.20"}
    fake_bot = SimpleNamespace(
        config=SimpleNamespace(eod_retry_seconds=Decimal("10")),
        last_fractional_sweep=time.monotonic() - 999999,
        pending_stock_exits={"MSFT", "BABA"},
        wash_sales=SimpleNamespace(block=lambda *a, **k: None),
        is_fractional_quantity=AutoTrader.is_fractional_quantity,
    )
    fake_bot.api = SimpleNamespace(
        positions=lambda: positions,
        stock_quote=lambda symbol: {"symbol": symbol, "price": str(quotes.get(symbol, "0"))},
        quote_price=lambda quote: Decimal(str(quote["price"])),
    )
    calls = {}

    def fake_close_all_positions(instrument_types, loss_callback=None, exclude_symbols=None):
        calls["exclude_symbols"] = exclude_symbols
        return ["order-1"]

    fake_bot.api.close_all_positions = fake_close_all_positions
    sweep = AutoTrader.close_fractional_positions_before_core_close.__get__(fake_bot)

    sweep()

    # MSFT (profitable fractional) must be swept; BABA (underwater
    # fractional) and FPE (whole-share) must both be excluded.
    assert calls["exclude_symbols"] == {"BABA", "FPE"}
    assert "MSFT" not in fake_bot.pending_stock_exits


# --- Idle-cash relaxation ramp: relaxes, then resets on the next fill ------


@scenario
def idle_cash_ramp_reaches_a_relaxed_entry_and_resets_on_a_fill():
    config = SimpleNamespace(
        idle_cash_relaxation_enabled=True,
        idle_cash_grace_seconds=300,
        idle_cash_ramp_seconds=1800,
    )
    fake_bot = SimpleNamespace(
        config=config,
        last_capital_deployed_at=time.monotonic() - 2100,  # grace + full ramp
    )
    progress = AutoTrader.idle_cash_ramp_progress.__get__(fake_bot)
    assert progress(Decimal("50")) == Decimal("1")

    fake_bot.last_trade = {}
    fake_bot.last_exit_at = {}
    fake_bot.trade_times = defaultdict(deque)
    fake_bot.working_orders = {}
    fake_bot.status = SimpleNamespace(record_trade=lambda *a, **k: None)
    fake_bot.recent_stop_losses = deque()
    fake_bot.position_opened_at = {}
    fake_bot.symbol_pnl_history = defaultdict(deque)
    fake_bot.consecutive_exit_failures = defaultdict(int)
    fake_bot.submitted_order_ids_today = set()
    record_trade = AutoTrader.record_trade.__get__(fake_bot)
    record_trade("STOCK:AAPL", "order-1", "BUY")

    assert progress(Decimal("50")) == Decimal("0")


if __name__ == "__main__":
    results = run_all()
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.name}" + (f" - {result.detail}" if result.detail else ""))
    failures = [r for r in results if not r.passed]
    print(f"\n{len(results) - len(failures)}/{len(results)} scenarios passed")
    raise SystemExit(1 if failures else 0)
