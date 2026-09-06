"""Tests for HTF injection and the event_sink hook in BTStrategyAdapter."""

from __future__ import annotations

import backtrader as bt
import numpy as np
import pandas as pd
import pytest
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
    Adapted = make_bt_strategy_class(
        _Probe, primary_timeframe_ms=3_600_000, htf_timeframe_ms=14_400_000
    )
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


def _hourly(n: int, closes: list[float]) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    o = np.array(closes, dtype=float)
    return pd.DataFrame(
        {"open": o, "high": o + 1, "low": o - 1, "close": o, "volume": 1.0}, index=index
    )


def _four_hour(hourly: pd.DataFrame) -> pd.DataFrame:
    return hourly.resample("4h", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )


class _HtfRecorder(DeclarativeStrategy):
    def __init__(self) -> None:
        self.seen: list[tuple[float, ...] | None] = []

    def on_bar(self) -> None:
        self.seen.append(tuple(self.htf_closes) if self.htf_closes is not None else None)

    def should_long(self) -> bool:
        return False

    def go_long(self) -> TradeSetup:  # pragma: no cover - never called
        raise AssertionError


def _run(hourly: pd.DataFrame) -> list:
    Adapted = make_bt_strategy_class(
        _HtfRecorder, primary_timeframe_ms=3_600_000, htf_timeframe_ms=14_400_000
    )
    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=hourly))
    cerebro.adddata(bt.feeds.PandasData(dataname=_four_hour(hourly)))
    cerebro.addstrategy(Adapted)
    return cerebro.run()[0]._strategy.seen


def test_htf_bar_is_visible_only_after_it_closes():
    seen = _run(_hourly(8, [100, 100, 100, 50, 100, 100, 100, 100]))
    assert seen[3] == (50.0,)  # primary open 03:00 + 1h == HTF open 00:00 + 4h
    assert seen[7] == (50.0, 100.0)


def test_mutating_an_unfinished_htf_bar_does_not_change_earlier_inputs():
    a = _run(_hourly(8, [100, 100, 100, 50, 100, 100, 100, 100]))
    b = _run(_hourly(8, [100, 100, 100, 500, 100, 100, 100, 100]))
    assert a[:3] == b[:3]
    assert a[3] != b[3]


def test_a_second_feed_without_declared_timeframes_fails_loudly():
    Adapted = make_bt_strategy_class(_Probe)
    cerebro = bt.Cerebro()
    cerebro.adddata(_feed(240, "1h"))
    cerebro.adddata(_feed(60, "4h"))
    cerebro.addstrategy(Adapted)
    with pytest.raises(ValueError, match="htf_timeframe_ms"):
        cerebro.run()


def test_htf_arrays_stay_none_until_the_first_bar_has_closed():
    """No completed HTF history is the same 'unavailable' state as no HTF feed.

    An empty array is a third state no consumer expects: the documented type is
    ``np.ndarray | None`` and ``None`` is what a single-feed run injects, so a
    strategy guarding with ``is None`` must not fall through to an empty array.
    """
    seen = _run(_hourly(8, [100, 100, 100, 50, 100, 100, 100, 100]))
    assert seen[0] is None and seen[1] is None and seen[2] is None


def test_htf_injection_never_reads_the_whole_higher_timeframe_history():
    """Per-bar work must stay bounded by history_bars, not by feed length.

    Rescanning every HTF bar on every primary bar is O(primary x htf): only the
    trailing bars can still be forming, so the scan stops at the first closed one.
    """
    sizes: list[int] = []
    original = bt.linebuffer.LineBuffer.get

    def recording_get(self, ago=0, size=1):
        sizes.append(size)
        return original(self, ago=ago, size=size)

    bt.linebuffer.LineBuffer.get = recording_get
    try:
        Adapted = make_bt_strategy_class(
            _Probe,
            primary_timeframe_ms=3_600_000,
            htf_timeframe_ms=14_400_000,
            history_bars=5,
        )
        cerebro = bt.Cerebro()
        cerebro.adddata(_feed(200, "1h"))
        cerebro.adddata(_feed(50, "4h"))
        cerebro.addstrategy(Adapted)
        cerebro.run()
    finally:
        bt.linebuffer.LineBuffer.get = original

    assert sizes, "expected the adapter to fetch line history"
    assert max(sizes) <= 5, f"fetched {max(sizes)} bars at once with history_bars=5"
