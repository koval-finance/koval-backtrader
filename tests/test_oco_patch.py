import backtrader as bt
import pandas as pd

from koval_backtrader.oco_patch import apply_oco_guard


class OCOMinimalStrategy(bt.Strategy):
    def __init__(self):
        self.order = None
        self.stop_order = None
        self.tp_order = None
        self.exit_fills = []
        self.entry_fill_price = None

    def next(self):
        if self.order or self.position:
            return
        self.order = self.buy(size=1.0, exectype=bt.Order.Market)

    def notify_order(self, order):
        if order.status in (order.Submitted, order.Accepted):
            return

        if order.status == order.Completed:
            if order == self.order:
                self.order = None
                self.entry_fill_price = order.executed.price
                self.stop_order = self.sell(
                    price=self.entry_fill_price - 10.0,
                    exectype=bt.Order.Stop,
                    size=abs(order.executed.size),
                )
                self.tp_order = self.sell(
                    price=self.entry_fill_price + 20.0,
                    exectype=bt.Order.Limit,
                    size=abs(order.executed.size),
                    oco=self.stop_order,
                )
                return

            self.exit_fills.append(
                {
                    "price": order.executed.price,
                    "ref": order.ref,
                    "size": order.executed.size,
                }
            )

        if order.status in (order.Completed, order.Canceled, order.Margin, order.Rejected):
            if order == self.stop_order:
                self.stop_order = None
            elif order == self.tp_order:
                self.tp_order = None
            elif order == self.order:
                self.order = None


def _ambiguous_oco_df():
    return pd.DataFrame(
        {
            "open": [100.0, 102.0, 102.0],
            "high": [105.0, 125.0, 125.0],
            "low": [95.0, 90.0, 90.0],
            "close": [102.0, 102.0, 102.0],
            "volume": [1000] * 3,
        },
        index=pd.date_range("2024-01-01", periods=3, freq="h"),
    )


def test_oco_patch_applies_without_error_and_is_idempotent():
    apply_oco_guard()
    apply_oco_guard()

    assert bt.Cerebro() is not None


def test_oco_same_bar_only_one_exit_fills_and_position_is_flat():
    apply_oco_guard()
    cerebro = bt.Cerebro()
    cerebro.addstrategy(OCOMinimalStrategy)
    data = bt.feeds.PandasData(dataname=_ambiguous_oco_df())
    cerebro.adddata(data)
    cerebro.broker.setcash(100000.0)
    cerebro.broker.setcommission(commission=0.0)

    results = cerebro.run()
    strategy = results[0]
    position = cerebro.broker.getposition(data)

    assert position.size == 0
    assert len(strategy.exit_fills) == 1
    assert abs(strategy.exit_fills[0]["size"]) == 1.0
    assert strategy.entry_fill_price is not None
    assert strategy.exit_fills[0]["price"] in (
        strategy.entry_fill_price - 10.0,
        strategy.entry_fill_price + 20.0,
    )
