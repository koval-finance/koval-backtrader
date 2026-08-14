import backtrader as bt
import numpy as np
import pandas as pd
import pytest
from koval.engine.engine_events import EventType
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup

from koval_backtrader.bt_adapter import make_bt_strategy_class
from koval_backtrader.oco_patch import apply_oco_guard

apply_oco_guard()


class _AlwaysLong(DeclarativeStrategy):
    """Test stub: enters long on every bar when flat."""

    def should_long(self) -> bool:
        return self.position_size == 0.0

    def go_long(self) -> TradeSetup:
        return TradeSetup(
            direction="long",
            entry_price=self.close,
            stop_loss=self.close * 0.90,
            entry_type="market",
            why_entry=["always long stub"],
            indicators_at_entry={"close": self.close},
        )


class _NeverTrades(DeclarativeStrategy):
    """Test stub: never signals."""

    pass


def _synthetic_feed(n: int = 100, start_price: float = 100.0) -> bt.feeds.PandasData:
    dates = pd.date_range("2024-01-01", periods=n, freq="1h")
    # Use std=3.0 so price swings are large enough to hit SL (10% away) / TP (20% away)
    price = start_price + np.cumsum(np.random.default_rng(42).standard_normal(n) * 3.0)
    price = np.maximum(price, 1.0)
    df = pd.DataFrame(
        {
            "open": price,
            "high": price * 1.05,
            "low": price * 0.95,
            "close": price,
            "volume": np.ones(n) * 1000.0,
        },
        index=dates,
    )
    return bt.feeds.PandasData(dataname=df)


# --- Strategy-agnostic contract ---


def test_make_bt_strategy_class_accepts_declarative_subclass():
    cls = make_bt_strategy_class(_AlwaysLong)
    assert issubclass(cls, bt.Strategy)


def test_make_bt_strategy_class_sets_adapter_name():
    cls = make_bt_strategy_class(_AlwaysLong)
    assert "AlwaysLong" in cls.__name__


def test_make_bt_strategy_class_default_params():
    cls = make_bt_strategy_class(_AlwaysLong)
    param_dict = dict(cls.params._getitems())
    assert "risk_reward_ratio" in param_dict
    assert "risk_per_trade" in param_dict
    assert "leverage" in param_dict
    assert "strategy_config" in param_dict


def test_make_bt_strategy_class_custom_params_override_defaults():
    cls = make_bt_strategy_class(_AlwaysLong, risk_per_trade=2.5)
    param_dict = dict(cls.params._getitems())
    assert param_dict["risk_per_trade"] == pytest.approx(2.5)


# --- Integration: adapter runs through Cerebro ---


def _run_cerebro(declarative_cls, n_bars: int = 100, cash: float = 10_000.0, **kwargs):
    AdaptedClass = make_bt_strategy_class(declarative_cls, **kwargs)
    cerebro = bt.Cerebro()
    cerebro.adddata(_synthetic_feed(n_bars))
    cerebro.addstrategy(AdaptedClass)
    cerebro.broker.setcash(cash)
    cerebro.broker.set_coo(True)
    results = cerebro.run()
    return results[0]


def test_adapter_runs_without_error_on_always_long():
    strat = _run_cerebro(_AlwaysLong, risk_per_trade=1.0, risk_reward_ratio=2.0)
    assert strat is not None


def test_adapter_produces_at_least_one_trade():
    strat = _run_cerebro(_AlwaysLong, risk_per_trade=1.0, risk_reward_ratio=2.0)
    assert strat._next_trade_id > 1


def test_adapter_never_trades_strategy_produces_no_trades():
    strat = _run_cerebro(_NeverTrades)
    assert strat._next_trade_id == 1


def test_strategy_config_injected_into_declarative():
    AdaptedClass = make_bt_strategy_class(
        _AlwaysLong,
        strategy_config={"my_param": 42},
    )
    cerebro = bt.Cerebro()
    cerebro.adddata(_synthetic_feed(10))
    cerebro.addstrategy(AdaptedClass)
    cerebro.broker.setcash(10_000)
    cerebro.broker.set_coo(True)
    results = cerebro.run()
    strat = results[0]
    assert strat._strategy.config.get("my_param") == 42


# --- Event-driven: events are emitted ---


def test_session_start_event_emitted():
    strat = _run_cerebro(_AlwaysLong, risk_per_trade=1.0, risk_reward_ratio=2.0)
    types = [e.event_type for e in strat._events]
    assert EventType.SESSION_START in types


def test_session_end_event_emitted():
    strat = _run_cerebro(_AlwaysLong, risk_per_trade=1.0, risk_reward_ratio=2.0)
    types = [e.event_type for e in strat._events]
    assert EventType.SESSION_END in types


def test_signal_detected_event_emitted_when_trading():
    strat = _run_cerebro(_AlwaysLong, risk_per_trade=1.0, risk_reward_ratio=2.0)
    types = [e.event_type for e in strat._events]
    assert EventType.SIGNAL_DETECTED in types


def test_signal_event_has_direction_and_why():
    strat = _run_cerebro(_AlwaysLong, risk_per_trade=1.0, risk_reward_ratio=2.0)
    signal_events = [e for e in strat._events if e.event_type == EventType.SIGNAL_DETECTED]
    assert len(signal_events) > 0
    first = signal_events[0]
    assert first.payload.get("direction") in ("long", "short")
    assert isinstance(first.payload.get("why_entry"), list)


def test_trade_opened_event_emitted():
    strat = _run_cerebro(_AlwaysLong, risk_per_trade=1.0, risk_reward_ratio=2.0)
    types = [e.event_type for e in strat._events]
    assert EventType.TRADE_OPENED in types


def test_trade_closed_event_emitted():
    strat = _run_cerebro(_AlwaysLong, risk_per_trade=1.0, risk_reward_ratio=2.0)
    types = [e.event_type for e in strat._events]
    assert EventType.TRADE_CLOSED in types


def test_trade_closed_event_has_exit_reason():
    strat = _run_cerebro(_AlwaysLong, risk_per_trade=1.0, risk_reward_ratio=2.0)
    closed_events = [e for e in strat._events if e.event_type == EventType.TRADE_CLOSED]
    assert len(closed_events) > 0
    assert "exit_reason" in closed_events[0].payload


def test_no_never_trades_signal_events():
    strat = _run_cerebro(_NeverTrades)
    signal_events = [e for e in strat._events if e.event_type == EventType.SIGNAL_DETECTED]
    assert signal_events == []


def test_drawdown_limit_hit_stops_trading():
    # max_drawdown=0.1 (0.1%) is extremely tight — any single losing trade will breach it.
    # risk_per_trade=1.0 keeps position sizes within broker margin so orders actually fill.
    # The synthetic feed (seed=42, std=3.0) guarantees at least one SL hit in 200 bars.
    strat = _run_cerebro(
        _AlwaysLong,
        n_bars=200,
        risk_per_trade=1.0,
        risk_reward_ratio=2.0,
        max_drawdown=0.1,
    )
    dd_events = [e for e in strat._events if e.event_type == EventType.DRAWDOWN_LIMIT_HIT]
    assert len(dd_events) > 0
    assert strat._dd_limit_hit is True


# --- No look-ahead bias ---


def test_strategy_never_sees_future_bar():
    """
    Strictly increasing price feed. Strategy records every close it sees.
    If it ever saw a future bar, it would see a price higher than the current bar's close.
    """
    seen_closes: list[float] = []

    class _RecordingStrategy(DeclarativeStrategy):
        def should_long(self) -> bool:
            seen_closes.append(self.close)
            return False

    AdaptedClass = make_bt_strategy_class(_RecordingStrategy)
    dates = pd.date_range("2024-01-01", periods=50, freq="1h")
    prices = [float(100 + i) for i in range(50)]
    df = pd.DataFrame(
        {
            "open": prices,
            "high": [p * 1.001 for p in prices],
            "low": [p * 0.999 for p in prices],
            "close": prices,
            "volume": [1000.0] * 50,
        },
        index=dates,
    )
    feed = bt.feeds.PandasData(dataname=df)
    cerebro = bt.Cerebro()
    cerebro.adddata(feed)
    cerebro.addstrategy(AdaptedClass)
    cerebro.broker.setcash(10_000)
    cerebro.run()

    assert len(seen_closes) > 0
    for i in range(1, len(seen_closes)):
        assert seen_closes[i] >= seen_closes[i - 1], (
            f"Look-ahead bias: bar {i} saw close {seen_closes[i]} < previous {seen_closes[i - 1]}"
        )
    assert max(seen_closes) == pytest.approx(prices[-1], rel=1e-6)


# --- Dynamic exits: trailing/breakeven SL update must not drop the TP ---


class _TrailLongKeepTP(DeclarativeStrategy):
    """Enters long once, sets a far SL+TP, then trails the stop up every bar.

    The trail keeps the stop below price (never hit) and the TP far above
    (never hit), so the position stays open while ``on_sl_update`` fires
    repeatedly — exercising the adapter's bracket-rebuild path.
    """

    def should_long(self) -> bool:
        return self.position_size == 0.0 and self.bar_index <= 3

    def go_long(self) -> TradeSetup:
        return TradeSetup(
            direction="long",
            entry_price=self.close,
            stop_loss=self.close * 0.80,
            take_profit=self.close * 1.50,
            entry_type="market",
            why_entry=["trail+tp"],
        )

    def on_sl_update(self, trade_id: int) -> float:
        # Trail the stop upward each bar; always below price so it never fills.
        return self.close * 0.85


def _rising_feed(n: int = 30, start: float = 100.0, step: float = 0.5):
    prices = [start + i * step for i in range(n)]
    dates = pd.date_range("2024-01-01", periods=n, freq="1h")
    df = pd.DataFrame(
        {
            "open": prices,
            "high": [p * 1.01 for p in prices],
            "low": [p * 0.99 for p in prices],
            "close": prices,
            "volume": [1000.0] * n,
        },
        index=dates,
    )
    return bt.feeds.PandasData(dataname=df)


def test_take_profit_survives_trailing_stop_update():
    """Regression: updating the stop must not silently drop the take-profit.

    Cancelling the stop (the OCO group leader) cascades to its TP sibling, so
    the adapter must re-place the TP when it rebuilds the bracket.
    """
    Adapted = make_bt_strategy_class(_TrailLongKeepTP, risk_per_trade=1.0)
    observations: list[tuple[int, bool, bool]] = []

    class _Probe(Adapted):  # type: ignore[valid-type, misc]
        def next(self):
            super().next()
            if self.position:
                stop_alive = self._stop_order is not None and self._stop_order.alive()
                tp_alive = self._tp_order is not None and self._tp_order.alive()
                observations.append((len(self.data), stop_alive, tp_alive))

    cerebro = bt.Cerebro()
    cerebro.adddata(_rising_feed())
    cerebro.addstrategy(_Probe)
    cerebro.broker.setcash(100_000)
    cerebro.run()

    # Position stayed open across many bars (so on_sl_update fired repeatedly).
    assert len(observations) > 3
    # The trailing stop is always live...
    assert all(stop_alive for _, stop_alive, _ in observations)
    # ...and the take-profit must remain live on every open bar.
    dropped = [bar for bar, _, tp_alive in observations if not tp_alive]
    assert dropped == [], f"take-profit dropped on open bars: {dropped}"


# --- Determinism ---


def test_same_seed_same_trade_count():
    def _run():
        AdaptedClass = make_bt_strategy_class(
            _AlwaysLong, risk_per_trade=1.0, risk_reward_ratio=2.0
        )
        cerebro = bt.Cerebro()
        cerebro.adddata(_synthetic_feed(50))
        cerebro.addstrategy(AdaptedClass)
        cerebro.broker.setcash(10_000)
        cerebro.broker.set_coo(True)
        return cerebro.run()[0]._next_trade_id

    assert _run() == _run()


# --- Strategy hooks: the adapter drives every one of them ---


class _HookRecorder(DeclarativeStrategy):
    """Enters long whenever flat and records the trade id each hook received."""

    def __init__(self):
        super().__init__()
        self.bars = 0
        self.bars_while_holding = 0
        self.opened: list[int] = []
        self.sl_updated: list[int] = []
        self.tp_updated: list[int] = []
        self.closed: list[int] = []

    def on_bar(self) -> None:
        self.bars += 1
        if self.position_size > 0.0:
            self.bars_while_holding += 1

    def should_long(self) -> bool:
        return self.position_size == 0.0

    def go_long(self) -> TradeSetup:
        return TradeSetup(
            direction="long",
            entry_price=self.close,
            stop_loss=self.close * 0.90,
            entry_type="market",
            why_entry=["hook recorder"],
        )

    def on_open_position(self, trade_id: int, setup: TradeSetup) -> None:
        self.opened.append(trade_id)

    def on_sl_update(self, trade_id: int) -> float | None:
        self.sl_updated.append(trade_id)
        return None

    def on_tp_update(self, trade_id: int) -> float | None:
        self.tp_updated.append(trade_id)
        return None

    def on_close_position(self, trade_id: int, result: dict) -> None:
        self.closed.append(trade_id)


def test_on_bar_runs_once_per_bar_including_while_a_position_is_open():
    """koval-engine's live runner calls `on_bar` every bar; so must this one,
    or a strategy behaves differently in a backtest than it does in paper."""
    strat = _run_cerebro(_HookRecorder, risk_per_trade=1.0)
    strategy = strat._strategy

    assert strategy.bars == len(strat.data)
    assert strategy.bars_while_holding > 0


def test_every_trade_hook_sees_the_same_trade_id():
    strat = _run_cerebro(_HookRecorder, risk_per_trade=1.0)
    strategy = strat._strategy

    assert strategy.opened == list(range(1, len(strategy.opened) + 1))
    assert strategy.closed == strategy.opened[: len(strategy.closed)]
    assert strategy.sl_updated, "expected at least one bar with a live stop"
    assert set(strategy.sl_updated) <= set(strategy.opened)
    assert set(strategy.tp_updated) <= set(strategy.opened)


def test_trade_opened_and_trade_closed_events_agree_on_the_trade_id():
    strat = _run_cerebro(_AlwaysLong, risk_per_trade=1.0, risk_reward_ratio=2.0)
    opened = [
        e.payload["trade_id"] for e in strat._events if e.event_type == EventType.TRADE_OPENED
    ]
    closed = [
        e.payload["trade_id"] for e in strat._events if e.event_type == EventType.TRADE_CLOSED
    ]

    assert opened == list(range(1, len(opened) + 1))
    assert closed == opened[: len(closed)]
