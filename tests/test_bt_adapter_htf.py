"""Tests for HTF injection and the event_sink hook in BTStrategyAdapter."""

from __future__ import annotations

import backtrader as bt
import numpy as np
import pandas as pd
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup

from koval_backtrader.bt_adapter import make_bt_strategy_class
from koval_backtrader.oco_patch import apply_oco_guard

apply_oco_guard()


class _Probe(DeclarativeStrategy):
    """Records whether HTF arrays were injected; never trades."""

    def __init__(self) -> None:
        self.htf_seen = False

    def should_long(self) -> bool:
        if self.htf_closes is not None and len(self.htf_closes) > 0:
            self.htf_seen = True
        return False

    def go_long(self) -> TradeSetup:  # pragma: no cover - never called
        raise AssertionError


def _feed(n: int, freq: str) -> bt.feeds.PandasData:
    price = np.linspace(100.0, 110.0, n)
    df = pd.DataFrame(
        {"open": price, "high": price + 1, "low": price - 1, "close": price, "volume": 1000.0},
        index=pd.date_range("2024-01-01", periods=n, freq=freq),
    )
    return bt.feeds.PandasData(dataname=df)


def test_event_sink_receives_events():
    received = []
    Adapted = make_bt_strategy_class(_Probe, event_sink=received.append)
    cerebro = bt.Cerebro()
    cerebro.adddata(_feed(60, "1h"))
    cerebro.addstrategy(Adapted)
    cerebro.run()
    assert len(received) >= 1  # at least SESSION_START + SESSION_END


def test_htf_arrays_injected_with_second_feed():
    Adapted = make_bt_strategy_class(_Probe)
    cerebro = bt.Cerebro()
    cerebro.adddata(_feed(240, "1h"))  # data0 = LTF
    cerebro.adddata(_feed(60, "4h"))  # data1 = HTF
    cerebro.addstrategy(Adapted)
    strat = cerebro.run()[0]
    assert strat._strategy.htf_seen is True


def test_htf_arrays_stay_none_with_single_feed():
    Adapted = make_bt_strategy_class(_Probe)
    cerebro = bt.Cerebro()
    cerebro.adddata(_feed(60, "1h"))
    cerebro.addstrategy(Adapted)
    strat = cerebro.run()[0]
    assert strat._strategy.htf_closes is None
