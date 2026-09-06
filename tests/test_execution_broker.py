"""Broker order paths that are not emitted by the current bracket adapter."""

import backtrader as bt
import pandas as pd
import pytest

from koval_backtrader.execution_broker import ExecutionCostBroker
from koval_backtrader.execution_config import resolve_execution_model
from koval_backtrader.oco_patch import apply_oco_guard


@pytest.mark.parametrize("short,entry,exit_price", [(False, 100.2, 104.79), (True, 99.8, 105.21)])
def test_market_close_uses_adverse_fill_in_broker_accounting(short, entry, exit_price):
    class RoundTrip(bt.Strategy):
        def next(self):
            if len(self) == 1:
                (self.sell if short else self.buy)(size=2)
            elif len(self) == 2:
                self.close()

    apply_oco_guard()
    model = resolve_execution_model(
        {
            "exchange": "binance",
            "exchange_type": "future",
            "execution_model": {
                "version": "ohlcv_fixed_v1",
                "commission_bps": 4,
                "spread_bps": 20,
                "slippage_bps": 10,
            },
        }
    )
    cerebro = bt.Cerebro()
    broker = ExecutionCostBroker(execution_model=model)
    broker.setcash(10_000)
    broker.setcommission(commission=model.commission_bps / 10_000)
    cerebro.setbroker(broker)
    df = pd.DataFrame(
        {
            "open": [100, 100, 105],
            "high": [101, 101, 106],
            "low": [99, 99, 104],
            "close": [100, 100, 105],
            "volume": [1000] * 3,
        },
        index=pd.date_range("2024-01-01", periods=3, freq="h"),
    )
    cerebro.adddata(bt.feeds.PandasData(dataname=df))
    cerebro.addstrategy(RoundTrip)
    strategy = cerebro.run()[0]
    assert not strategy.position
    fills = broker.execution_fills
    assert len(fills) == 2
    assert fills[0]["fill_price"] == pytest.approx(entry)
    assert fills[1]["fill_price"] == pytest.approx(exit_price)
    sign = -1 if short else 1
    assert broker.getvalue() == pytest.approx(
        10_000 + sign * 2 * (exit_price - entry) - 2 * (entry + exit_price) * 0.0004
    )


def test_out_of_range_leverage_raises_value_error_not_overflow():
    """A huge integer must fail validation, not escape as OverflowError."""
    config = {
        "exchange": "binance",
        "exchange_type": "future",
        "execution_model": {
            "version": "ohlcv_fixed_v1",
            "commission_bps": 4.0,
            "spread_bps": 20.0,
            "slippage_bps": 10.0,
            "leverage": 10**400,
        },
    }
    with pytest.raises(ValueError, match="leverage"):
        resolve_execution_model(config)
