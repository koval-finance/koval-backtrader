# SPDX-License-Identifier: GPL-3.0-or-later
"""Metrics that let someone judge a strategy instead of admire it.

Win rate and profit factor over eleven trades tell you almost nothing, and a
`profit_factor: 0.0` on a run with no losing trades is worse than nothing —
it reads as a catastrophic result. These tests pin the additional evidence a
result carries, and pin that anything unavailable says so by name instead of
being rendered as a zero or an infinity.

Everything here is additive: the raw trades and the existing metrics are
untouched, because an aggregate that replaces its inputs cannot be checked.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest
from koval.engine.backtest_engine import EngineRunSpec
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup

from koval_backtrader import backtest_runner
from koval_backtrader.research_metrics import time_under_water

START_MS = 1_704_067_200_000
SIGNAL = (100.0, 101.0, 99.0, 100.0, 1000.0)


def fixed_config(**overrides) -> dict:
    model = {
        "version": "ohlcv_fixed_v1",
        "commission_bps": 4.0,
        "spread_bps": 20.0,
        "slippage_bps": 10.0,
    }
    model.update(overrides)
    return {"exchange": "binance", "exchange_type": "future", "execution_model": model}


def run(monkeypatch, rows, *, config=None, entries=(1,), stop=90.0, target=120.0, size=2.0):
    """A probe that can open several sequential trades."""

    class Probe(DeclarativeStrategy):
        def should_long(self):
            return self.bar_index in entries

        def go_long(self):
            return TradeSetup(
                direction="long",
                entry_type="market",
                entry_price=100.0,
                stop_loss=stop,
                take_profit=target,
                size=size,
            )

    monkeypatch.setattr(backtest_runner, "assemble_from_graph", lambda graph: Probe())
    candles = np.array(
        [[START_MS + i * 3_600_000, *row] for i, row in enumerate(rows)], dtype=float
    )
    return backtest_runner.create_engine().run(
        EngineRunSpec(
            graph={}, feeds={"1h": candles}, initial_capital=10_000.0, execution_config=config
        )
    )


# --- Time under water is pure arithmetic, so test it directly -------------


def test_time_under_water_counts_points_below_the_running_peak():
    curve = [100.0, 110.0, 105.0, 104.0, 120.0]
    share, longest = time_under_water(curve)
    assert share == pytest.approx(2 / 5 * 100.0)
    assert longest == 2


def test_a_monotonically_rising_curve_is_never_under_water():
    assert time_under_water([1.0, 2.0, 3.0]) == (0.0, 0)


def test_a_falling_curve_is_under_water_throughout_except_its_peak():
    share, longest = time_under_water([10.0, 9.0, 8.0, 7.0])
    assert share == pytest.approx(75.0)
    assert longest == 3


def test_an_empty_curve_has_no_time_under_water():
    assert time_under_water([]) == (0.0, 0)


# --- What a result carries ------------------------------------------------


def test_research_block_is_present_and_serialisable(monkeypatch):
    result = run(monkeypatch, [SIGNAL, SIGNAL, (85, 89, 80, 86, 1000)], config=fixed_config())
    research = result.metrics["research"]
    assert set(research) >= {
        "sample_size",
        "expectancy",
        "exposure",
        "drawdown",
        "excursion",
        "costs",
        "final_open_position",
        "unavailable",
    }
    assert json.loads(json.dumps(result.metrics, allow_nan=False)) == result.metrics


def test_the_raw_trades_are_not_replaced_by_the_aggregate(monkeypatch):
    result = run(monkeypatch, [SIGNAL, SIGNAL, (85, 89, 80, 86, 1000)], config=fixed_config())
    assert len(result.trades) == 1
    assert result.trades[0]["realized_pnl"] < 0
    assert result.metrics["total_trades"] == 1
    assert result.metrics["win_rate"] == 0.0


def test_sample_size_is_reported_alongside_the_ratios(monkeypatch):
    result = run(monkeypatch, [SIGNAL, SIGNAL, (85, 89, 80, 86, 1000)], config=fixed_config())
    sample = result.metrics["research"]["sample_size"]
    assert sample["closed_trades"] == 1
    assert sample["wins"] == 0
    assert sample["losses"] == 1
    assert sample["sufficient_for_ratios"] is False


def test_expectancy_is_the_mean_realised_result(monkeypatch):
    result = run(monkeypatch, [SIGNAL, SIGNAL, (85, 89, 80, 86, 1000)], config=fixed_config())
    expectancy = result.metrics["research"]["expectancy"]
    assert expectancy["per_trade"] == pytest.approx(result.trades[0]["realized_pnl"])
    # Risked ~20 to make or lose; the R multiple must be finite and negative.
    assert expectancy["per_trade_r"] is not None
    assert expectancy["per_trade_r"] < 0
    assert math.isfinite(expectancy["per_trade_r"])


# --- Nothing misleading when there is nothing to report -------------------


def test_a_run_with_no_closed_trades_reports_unavailable_not_zero(monkeypatch):
    result = run(monkeypatch, [SIGNAL, SIGNAL], config=fixed_config())
    research = result.metrics["research"]
    assert research["sample_size"]["closed_trades"] == 0
    assert research["expectancy"]["per_trade"] is None
    assert research["expectancy"]["per_trade_r"] is None
    assert research["unavailable"]["expectancy"] == "no_closed_trades"
    assert research["excursion"]["avg_mae"] is None
    assert research["unavailable"]["excursion"] == "no_closed_trades"


def test_no_metric_is_reported_as_infinity(monkeypatch):
    """A ratio with a zero denominator is unavailable, not infinite."""
    result = run(monkeypatch, [SIGNAL, SIGNAL, (119, 122, 118, 121, 1000)], config=fixed_config())
    assert result.trades[0]["realized_pnl"] > 0

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, float):
            assert math.isfinite(value), value

    walk(result.metrics["research"])


def test_funding_share_is_unavailable_rather_than_zero(monkeypatch):
    result = run(monkeypatch, [SIGNAL, SIGNAL, (85, 89, 80, 86, 1000)], config=fixed_config())
    costs = result.metrics["research"]["costs"]
    assert costs["funding"] is None
    assert costs["funding_status"] == "unavailable"
    assert costs["commission"] > 0
    assert costs["price_adjustment"] > 0


# --- Exposure, drawdown duration and excursion ----------------------------


def test_exposure_counts_only_bars_holding_a_position(monkeypatch):
    rows = [SIGNAL, SIGNAL, SIGNAL, (85, 89, 80, 86, 1000), SIGNAL]
    result = run(monkeypatch, rows, config=fixed_config())
    exposure = result.metrics["research"]["exposure"]
    assert exposure["total_bars"] == 5
    # Filled on bar 2, closed on bar 4: bars 2, 3 and 4 held the position.
    assert exposure["bars_in_position"] == 3
    assert exposure["exposure_pct"] == pytest.approx(60.0)


def test_a_run_that_never_enters_has_zero_exposure(monkeypatch):
    result = run(monkeypatch, [SIGNAL, SIGNAL], config=fixed_config(), entries=())
    exposure = result.metrics["research"]["exposure"]
    assert exposure["bars_in_position"] == 0
    assert exposure["exposure_pct"] == 0.0


def test_holding_time_is_reported_in_bars_and_seconds(monkeypatch):
    rows = [SIGNAL, SIGNAL, SIGNAL, (85, 89, 80, 86, 1000)]
    result = run(monkeypatch, rows, config=fixed_config())
    exposure = result.metrics["research"]["exposure"]
    assert exposure["avg_holding_bars"] == pytest.approx(3.0)
    assert exposure["avg_holding_seconds"] == pytest.approx(2 * 3600.0)


def test_time_under_water_is_reported_for_the_run(monkeypatch):
    rows = [SIGNAL, SIGNAL, (85, 89, 80, 86, 1000), SIGNAL]
    result = run(monkeypatch, rows, config=fixed_config())
    drawdown = result.metrics["research"]["drawdown"]
    assert drawdown["time_under_water_pct"] > 0
    assert drawdown["max_time_under_water_bars"] >= 1
    assert drawdown["max_drawdown_pct"] == pytest.approx(result.metrics["max_drawdown"])


def test_excursion_records_how_far_the_trade_went_both_ways(monkeypatch):
    """Entry at ~100.2, up to 130, then stopped out on a bar reaching 80.

    The target is pushed out to 200 so the take-profit does not close the trade
    on the favorable bar; otherwise this measures a two-bar hold, not the
    round trip it means to.
    """
    rows = [SIGNAL, SIGNAL, (100, 130, 99, 129, 1000), (95, 96, 80, 85, 1000)]
    result = run(monkeypatch, rows, config=fixed_config(), stop=82.0, target=200.0)
    trade = result.trades[0]
    assert trade["max_favorable_excursion"] == pytest.approx(2 * (130 - trade["entry_price"]))
    assert trade["max_adverse_excursion"] == pytest.approx(2 * (trade["entry_price"] - 80))
    excursion = result.metrics["research"]["excursion"]
    assert excursion["avg_mfe"] == pytest.approx(trade["max_favorable_excursion"])
    assert excursion["worst_mae"] == pytest.approx(trade["max_adverse_excursion"])
    assert excursion["resolution"] == "bar_high_low_not_intrabar_path"


def test_excursion_is_never_negative(monkeypatch):
    """A trade that only ever moved one way still has a zero-floored other side."""
    rows = [SIGNAL, SIGNAL, (119, 122, 118, 121, 1000)]
    result = run(monkeypatch, rows, config=fixed_config())
    trade = result.trades[0]
    assert trade["max_adverse_excursion"] >= 0
    assert trade["max_favorable_excursion"] >= 0


# --- The open position is not a realised result ---------------------------


def test_an_open_position_is_valued_separately_from_realised_results(monkeypatch):
    result = run(monkeypatch, [SIGNAL, (100, 102, 99, 101, 1000)], config=fixed_config())
    final = result.metrics["research"]["final_open_position"]
    assert final is not None
    assert final["realized"] is False
    assert final["valuation"] == "marked_to_last_close"
    assert final["direction"] == "long"
    assert final["quantity"] == 2.0
    assert final["unrealized_pnl"] == pytest.approx(2 * (101 - 100 * 1.002))
    assert result.metrics["research"]["sample_size"]["closed_trades"] == 0


def test_a_flat_run_has_no_open_position_block(monkeypatch):
    result = run(monkeypatch, [SIGNAL, SIGNAL, (85, 89, 80, 86, 1000)], config=fixed_config())
    assert result.metrics["research"]["final_open_position"] is None


# --- Segment inputs -------------------------------------------------------


def test_each_trade_carries_calendar_and_market_segment_inputs(monkeypatch):
    config = fixed_config()
    config["market"] = {
        "venue": "binance",
        "market": "futures",
        "symbol": "BTCUSDT",
        "contract_type": "perpetual",
        "base_currency": "BTC",
        "quote_currency": "USDT",
    }
    result = run(monkeypatch, [SIGNAL, SIGNAL, (85, 89, 80, 86, 1000)], config=config)
    segment = result.trades[0]["segment"]
    assert segment["venue"] == "binance"
    assert segment["symbol"] == "BTCUSDT"
    assert segment["market"] == "future"
    assert segment["year"] == 2024
    assert segment["month"] == 1
    assert segment["weekday"] == 0  # 2024-01-01 was a Monday
    assert segment["hour_utc"] == 1  # entry filled on the second bar
    assert segment["iso_week"] == 1


def test_segment_inputs_omit_the_market_when_none_was_declared(monkeypatch):
    result = run(monkeypatch, [SIGNAL, SIGNAL, (85, 89, 80, 86, 1000)], config=fixed_config())
    segment = result.trades[0]["segment"]
    assert segment["venue"] is None
    assert segment["symbol"] is None
    assert segment["year"] == 2024


def test_legacy_runs_also_get_research_metrics(monkeypatch):
    result = run(monkeypatch, [SIGNAL, SIGNAL, (85, 89, 80, 86, 1000)], config=None)
    research = result.metrics["research"]
    assert research["sample_size"]["closed_trades"] == 1
    assert research["costs"]["price_adjustment"] is None
    assert research["unavailable"]["price_adjustment"] == "not_modelled_by_legacy_v1"


def test_cost_totals_include_open_fills_but_ratio_uses_closed_fills(monkeypatch):
    rows = [SIGNAL, SIGNAL, (119, 122, 118, 121, 1000), SIGNAL, SIGNAL]
    result = run(monkeypatch, rows, config=fixed_config(), entries=(1, 4))
    audit = result.metrics["execution_costs"]
    costs = result.metrics["research"]["costs"]
    assert audit["open_commission"] > 0
    assert costs["commission"] == pytest.approx(audit["commission"])
    assert costs["total_modelled_cost"] == pytest.approx(
        audit["commission"] + audit["price_adjustment_cost"]
    )
    trade = result.trades[0]
    closed_cost = trade["commission"] + trade["execution_costs"]["price_adjustment_cost"]
    assert costs["cost_over_gross_pct"] == pytest.approx(
        100 * closed_cost / abs(trade["gross_price_pnl"])
    )
