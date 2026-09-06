import backtrader as bt
import pandas as pd
import pytest

from koval_backtrader.bt_analyzers import EquityCurveAnalyzer, TradeListAnalyzer
from koval_backtrader.execution_broker import ExecutionCostBroker
from koval_backtrader.execution_config import resolve_execution_model


def _mock_df(rows=50):
    return pd.DataFrame(
        {
            "open": [100.0] * rows,
            "high": [105.0] * rows,
            "low": [95.0] * rows,
            "close": [101.0] * rows,
            "volume": [1000] * rows,
        },
        index=pd.date_range("2024-01-01", periods=rows, freq="h"),
    )


class _OneTradeStrategy(bt.Strategy):
    def __init__(self):
        self.order = None

    def notify_order(self, order):
        if order.status in (order.Completed, order.Canceled, order.Margin, order.Rejected):
            self.order = None

    def next(self):
        if self.order:
            return
        if len(self) == 3:
            self.order = self.buy(size=10)
        elif len(self) == 10 and self.position:
            self.order = self.close()


class _OneTradeStrategyWithFundingInfo(_OneTradeStrategy):
    def get_trade_info(self, trade_ref):
        return {
            "direction": "long",
            "funding_adjustment": -12.5,
            "reason": "Funding test",
            "exit_reason": "Take Profit",
            "size": 10,
        }


def test_trade_list_analyzer_produces_required_closed_trade_fields():
    cerebro = bt.Cerebro()
    cerebro.addstrategy(_OneTradeStrategy)
    cerebro.addanalyzer(TradeListAnalyzer, _name="tradelist")
    cerebro.adddata(bt.feeds.PandasData(dataname=_mock_df()))

    results = cerebro.run()
    trades = results[0].analyzers.tradelist.get_analysis()

    assert len(trades) == 1
    trade = trades[0]
    assert {
        "id",
        "direction",
        "entry_price",
        "exit_price",
        "entry_time",
        "exit_time",
        "realized_pnl",
        "exit_reason",
        "size",
        "commission",
    }.issubset(trade)
    assert trade["direction"] == "LONG"


def test_trade_list_analyzer_applies_funding_adjustment_without_losing_gross_pnl():
    cerebro = bt.Cerebro()
    cerebro.addstrategy(_OneTradeStrategyWithFundingInfo)
    cerebro.addanalyzer(TradeListAnalyzer, _name="tradelist")
    cerebro.adddata(bt.feeds.PandasData(dataname=_mock_df()))

    results = cerebro.run()
    trade = results[0].analyzers.tradelist.get_analysis()[0]

    assert trade["direction"] == "LONG"
    assert trade["signal_direction"] == "long"
    assert trade["funding_adjustment"] == -12.5
    assert trade["realized_pnl"] == trade["gross_realized_pnl"] + trade["funding_adjustment"]


def test_equity_curve_analyzer_records_one_point_per_bar():
    df = _mock_df(rows=30)
    cerebro = bt.Cerebro()
    cerebro.addstrategy(_OneTradeStrategy)
    cerebro.addanalyzer(EquityCurveAnalyzer, _name="equity")
    cerebro.adddata(bt.feeds.PandasData(dataname=df))

    results = cerebro.run()
    curve = results[0].analyzers.equity.get_analysis()

    assert len(curve) == len(df)
    assert {"timestamp", "equity"}.issubset(curve[0])
    assert all(point["equity"] >= 0 for point in curve)


@pytest.mark.parametrize("funding", [-12.5, 12.5])
def test_fixed_model_rejects_analyzer_only_funding(funding):
    class WithFunding(_OneTradeStrategyWithFundingInfo):
        def get_trade_info(self, trade_ref):
            return {**super().get_trade_info(trade_ref), "funding_adjustment": funding}

    model = resolve_execution_model(
        {
            "exchange": "binance",
            "exchange_type": "future",
            "execution_model": {
                "version": "ohlcv_fixed_v1",
                "commission_bps": 0,
                "spread_bps": 0,
                "slippage_bps": 0,
            },
        }
    )
    cerebro = bt.Cerebro()
    cerebro.setbroker(ExecutionCostBroker(execution_model=model))
    cerebro.addstrategy(WithFunding)
    cerebro.addanalyzer(TradeListAnalyzer)
    cerebro.adddata(bt.feeds.PandasData(dataname=_mock_df()))
    with pytest.raises(ValueError, match="funding.*unavailable"):
        cerebro.run()
