from __future__ import annotations

import backtrader as bt
import numpy as np
import pandas as pd
import pytest
from koval.engine.history_window import DEFAULT_HISTORY_BARS
from koval.strategy.base.declarative import DeclarativeStrategy

from koval_backtrader.bt_adapter import make_bt_strategy_class
from koval_backtrader.oco_patch import apply_oco_guard

apply_oco_guard()


class _Probe(DeclarativeStrategy):
    """Records the latest injected arrays so tests can inspect them."""

    def __init__(self):
        super().__init__()
        self.captured: dict = {}

    def should_long(self) -> bool:
        self.captured["closes"] = None if self.closes is None else self.closes.copy()
        self.captured["highs"] = None if self.highs is None else self.highs.copy()
        self.captured["lows"] = None if self.lows is None else self.lows.copy()
        self.captured["opens"] = None if self.opens is None else self.opens.copy()
        self.captured["volumes"] = None if self.volumes is None else self.volumes.copy()
        self.captured["close_scalar"] = self.close
        self.captured["high_scalar"] = self.high
        self.captured["low_scalar"] = self.low
        self.captured["open_scalar"] = self.open
        return False


def _synthetic(n: int = 50) -> bt.feeds.PandasData:
    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    price = np.linspace(100.0, 150.0, n)
    df = pd.DataFrame(
        {
            "open": price,
            "high": price + 1,
            "low": price - 1,
            "close": price,
            "volume": 1000.0,
        },
        index=idx,
    )
    return bt.feeds.PandasData(dataname=df)


def _run(probe_cls=_Probe, n: int = 50, history_bars: int | None = None):
    if history_bars is None:
        Adapted = make_bt_strategy_class(probe_cls)
    else:
        Adapted = make_bt_strategy_class(probe_cls, history_bars=history_bars)
    cerebro = bt.Cerebro()
    cerebro.adddata(_synthetic(n))
    cerebro.addstrategy(Adapted)
    return cerebro.run()[0]._strategy


def test_arrays_are_numpy_ndarrays():
    s = _run(n=50, history_bars=300)
    for key in ("closes", "highs", "lows", "opens", "volumes"):
        assert isinstance(s.captured[key], np.ndarray), f"{key} must be ndarray"


def test_arrays_are_chronological_last_equals_current_bar():
    s = _run(n=50, history_bars=300)
    assert s.captured["closes"][-1] == pytest.approx(s.captured["close_scalar"])
    assert s.captured["highs"][-1] == pytest.approx(s.captured["high_scalar"])
    assert s.captured["lows"][-1] == pytest.approx(s.captured["low_scalar"])
    assert s.captured["opens"][-1] == pytest.approx(s.captured["open_scalar"])


def test_arrays_capped_by_history_bars():
    s = _run(n=500, history_bars=200)
    assert len(s.captured["closes"]) == 200
    assert len(s.captured["highs"]) == 200
    assert len(s.captured["lows"]) == 200
    assert len(s.captured["opens"]) == 200
    assert len(s.captured["volumes"]) == 200


def test_arrays_grow_with_data_until_cap():
    # n=10 bars, cap=300 → arrays should have len(<=10) on the last bar
    s = _run(n=10, history_bars=300)
    assert len(s.captured["closes"]) == 10


def test_arrays_default_history_bars_is_the_shared_engine_window():
    # No history_bars override: the adapter defaults to the one window every
    # runtime injects, so a backtest and a paper session see the same warmup.
    s = _run(n=DEFAULT_HISTORY_BARS + 100)
    assert len(s.captured["closes"]) == DEFAULT_HISTORY_BARS
    assert DEFAULT_HISTORY_BARS == 1000


def test_volume_array_populated():
    s = _run(n=30, history_bars=300)
    # synthetic data sets volume=1000 on every bar
    assert np.allclose(s.captured["volumes"], 1000.0)
