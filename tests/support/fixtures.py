"""Shared test fixtures, split out of the former
test_strategy_and_logging.py monolith so every focused test
module can reuse the curated config without duplicating it."""
from decimal import Decimal
from types import SimpleNamespace



class StrategyConfigMixin:
    def config(self):
        return SimpleNamespace(
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
            tick_direction_enabled=True,
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
            resistance_exit_band_percent=Decimal("0.01"),
            entry_extension_jump_momentum_percent=Decimal("0.03"),
            stock_core_session_position_fraction=Decimal("0.10"),
            sma_trend_filter_enabled=False,
            rsi_filter_enabled=False,
            multi_day_momentum_filter_enabled=False,
            short_selling_enabled=False,
            opening_grace_spread_multiplier=Decimal("2"),
            opening_grace_extension_multiplier=Decimal("2"),
            extended_hours_spread_multiplier=Decimal("3"),
            option_take_profit_percent=Decimal("0.75"),
            option_stop_loss_percent=Decimal("0.50"),
            # Focus-mode additions: the profit-lock trail and the net
            # buy/sell pressure read. Defaults mirror
            # FocusModeSettings so existing expectations are
            # unaffected - the trail cannot arm without a peak_price
            # argument, which legacy callers don't pass.
            profit_lock_enabled=True,
            profit_lock_arm_percent=Decimal("0.10"),
            profit_lock_giveback_fraction=Decimal("0.50"),
            profit_lock_giveback_fraction_after_throttle=Decimal("0.25"),
            pressure_enabled=True,
            pressure_min_for_entry=Decimal("0.15"),
            pressure_flip_exit_enabled=True,
            pressure_flip_exit_threshold=Decimal("0.25"),
            pressure_history_seconds=300,
            option_min_hold_dte=2,
            option_capital_fraction=Decimal("0.05"),
            option_quantity=1,
            max_order_notional=Decimal("1000"),
            agent_exit_influence_enabled=True,
            agent_exit_min_confidence=Decimal("0.60"),
            agent_runner_bias_threshold=Decimal("0.50"),
            agent_runner_profit_percent=Decimal("0.01"),
            agent_derisk_bias_threshold=Decimal("-0.50"),
            time_aware_stop_enabled=False,
            time_aware_stop_widen_seconds=60,
            time_aware_stop_widen_multiplier=Decimal("1.5"),
            volatility_scalp_enabled=True,
            volatility_scalp_lookback_samples=20,
            volatility_scalp_min_stdev_percent=Decimal("0.015"),
            # Default 0 (no-op) so existing tests that feed a small,
            # arbitrary volume via _feed's "volume": "1000" aren't
            # affected - dedicated tests below override this to
            # exercise the floor itself.
            volatility_scalp_min_dollar_volume=Decimal("0"),
            trend_efficiency_trending_threshold=Decimal("0.5"),
            trend_efficiency_lookback_samples=10,
            volatility_scalp_dip_entry_percent=Decimal("0.005"),
            volatility_scalp_averaging_step_multiplier=Decimal("0.5"),
            volatility_scalp_vwap_band_percent=Decimal("0.05"),
            volatility_scalp_target_percent=Decimal("0.005"),
            volatility_scalp_momentum_stall_min_profit_fraction=Decimal("0.6"),
            volatility_scalp_hard_stop_percent=Decimal("0.05"),
            volatility_scalp_max_price=Decimal("5"),
            volatility_scalp_target_notional=Decimal("400"),
            volatility_scalp_target_notional_buying_power_fraction=Decimal("0.15"),
            volatility_scalp_breakout_k=Decimal("0.5"),
            heikin_ashi_bar_samples=3,
            heikin_ashi_bar_count=6,
            parabolic_sar_af_step=Decimal("0.02"),
            parabolic_sar_af_max=Decimal("0.2"),
        )
