# SPDX-License-Identifier: GPL-3.0-or-later
# Koval Backtrader Adapter — backtest engine plugin
"""Backtrader implementation of ``BacktestEngineProtocol``.

Loaded at runtime by ``koval.engine.backtest_engine.load_backtest_engine()``.
GPL-3.0: imports Backtrader. May import MIT code; MIT never imports this.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime

import backtrader as bt
import numpy as np
import pandas as pd
from koval.engine.backtest_engine import BacktestResult, EngineRunSpec, check_protocol_version
from koval.engine.engine_events import EngineEvent
from koval.engine.timeframe_utils import ordered_timeframes, timeframe_to_minutes
from koval.engine.trade_metrics import build_closed_trade_metrics
from koval.strategy.block_assembler import assemble_from_graph

from koval_backtrader.bt_adapter import make_bt_strategy_class
from koval_backtrader.bt_analyzers import EquityCurveAnalyzer, TradeListAnalyzer
from koval_backtrader.execution_audit import build_execution_audit, execution_metadata
from koval_backtrader.execution_broker import ExecutionCostBroker
from koval_backtrader.execution_config import FIXED_VERSION, resolve_execution_model
from koval_backtrader.oco_patch import apply_oco_guard

apply_oco_guard()

_OHLCV_COLS = ["open", "high", "low", "close", "volume"]

# ``timeframe_to_minutes`` returns this sentinel instead of raising on an
# unrecognised label. It must never reach the HTF availability arithmetic as if
# it were a real duration.
_UNKNOWN_TIMEFRAME_MINUTES = 10**9


def _timeframe_ms(timeframe: str) -> int:
    """Duration of one candle in milliseconds, refusing unknown labels."""
    minutes = timeframe_to_minutes(timeframe)
    if minutes >= _UNKNOWN_TIMEFRAME_MINUTES:
        raise ValueError(f"unknown timeframe: {timeframe!r}")
    return minutes * 60_000


def _validate_fixed_inputs(spec: EngineRunSpec) -> None:
    capital = spec.initial_capital
    if (
        isinstance(capital, bool)
        or not isinstance(capital, (int, float))
        or not np.isfinite(capital)
        or capital <= 0
    ):
        raise ValueError("initial_capital must be positive and finite")
    if not spec.feeds:
        raise ValueError("OHLCV feeds must not be empty")
    for candles in spec.feeds.values():
        if candles.ndim != 2 or candles.shape[1] != 6 or not len(candles):
            raise ValueError("OHLCV feeds must have non-empty shape (N, 6)")
        if not np.isfinite(candles).all():
            raise ValueError("OHLCV values must be finite")
        ts, opens, highs, lows, closes, volumes = candles.T
        if np.any(ts != np.floor(ts)) or np.any(np.diff(ts) <= 0):
            raise ValueError("OHLCV timestamps must be unique increasing integer milliseconds")
        if np.any(candles[:, 1:5] <= 0) or np.any(volumes < 0):
            raise ValueError("OHLCV prices must be positive and volumes non-negative")
        if np.any(lows > np.minimum(opens, closes)) or np.any(highs < np.maximum(opens, closes)):
            raise ValueError("OHLCV low/high must contain open and close")


def _feed_from_ndarray(candles: np.ndarray) -> bt.feeds.PandasData:
    """Convert an [N, 6] OHLCV ndarray (col 0 = timestamp_ms) to a BT feed."""
    if candles.shape[0] == 0:
        raise ValueError("empty OHLCV feed")
    index = pd.to_datetime(candles[:, 0].astype("int64"), unit="ms", utc=True)
    df = pd.DataFrame(candles[:, 1:6], columns=_OHLCV_COLS, index=index)
    return bt.feeds.PandasData(dataname=df)


def _make_sink(on_event: Callable[[dict], None] | None) -> Callable[[EngineEvent], None] | None:
    if on_event is None:
        return None

    def sink(event: EngineEvent) -> None:
        on_event(
            {
                "event_type": event.event_type.value,
                "bar_index": event.bar_index,
                "timestamp_ms": event.timestamp_ms,
                "payload": deepcopy(event.payload),
            }
        )

    return sink


def _iso(ts: object) -> str:
    return ts.isoformat() if isinstance(ts, datetime) else str(ts)


def _max_drawdown(equity_curve: list[dict]) -> float:
    """Largest peak-to-trough decline of the equity curve, in percent."""
    peak = 0.0
    max_dd = 0.0
    for point in equity_curve:
        value = float(point["equity"])
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, 100.0 * (peak - value) / peak)
    return max_dd


class BacktraderBacktestEngine:
    """Runs a backtest with Backtrader. Implements ``BacktestEngineProtocol``."""

    def run(
        self,
        spec: EngineRunSpec,
        on_event: Callable[[dict], None] | None = None,
    ) -> BacktestResult:
        check_protocol_version(spec)
        model = resolve_execution_model(spec.execution_config)
        if model.version == FIXED_VERSION:
            _validate_fixed_inputs(spec)
        metadata = execution_metadata(model)
        timeframes = ordered_timeframes(list(spec.feeds.keys()))
        # Only higher-timeframe availability needs durations, so a single-feed
        # run keeps accepting any label the engine's ordering accepts.
        primary_ms = htf_ms = None
        if len(timeframes) > 1:
            primary_ms, htf_ms = (_timeframe_ms(tf) for tf in timeframes[:2])
            if htf_ms % primary_ms != 0:
                raise ValueError(
                    "higher timeframe must be a whole multiple of the primary timeframe"
                )
        strategy = assemble_from_graph(spec.graph)
        bt_cls = make_bt_strategy_class(
            type(strategy),
            event_sink=_make_sink(on_event),
            execution_metadata=metadata,
            primary_timeframe_ms=primary_ms,
            htf_timeframe_ms=htf_ms,
        )

        cerebro = bt.Cerebro()
        if model.version == FIXED_VERSION:
            cerebro.setbroker(ExecutionCostBroker(execution_model=model))
        for tf in timeframes:
            cerebro.adddata(_feed_from_ndarray(spec.feeds[tf]))
        cerebro.broker.setcash(spec.initial_capital)
        # Stock-like linear cash with Backtrader's leverage: a long entry debits
        # notional / leverage of cash and equity stays cash + position value.
        # Do not switch to stocklike=False/automargin: it marks the margin at the
        # current price and overstates open profit in the equity curve.
        cerebro.broker.setcommission(
            commission=model.commission_bps / 10_000, leverage=model.leverage
        )
        cerebro.addstrategy(bt_cls)
        cerebro.addanalyzer(TradeListAnalyzer, _name="trades")
        cerebro.addanalyzer(EquityCurveAnalyzer, _name="equity")
        strat = cerebro.run()[0]

        trades = list(strat.analyzers.trades.get_analysis())
        equity_curve = [
            {"timestamp": _iso(p["timestamp"]), "equity": float(p["equity"])}
            for p in strat.analyzers.equity.get_analysis()
        ]
        metrics = build_closed_trade_metrics(
            initial_capital=spec.initial_capital,
            final_capital=float(cerebro.broker.getvalue()),
            closed_trades=trades,
        )
        metrics["max_drawdown"] = _max_drawdown(equity_curve)
        metrics["execution_model"] = metadata
        if model.version == FIXED_VERSION:
            position = cerebro.broker.getposition(strat.data)
            metrics["execution_costs"] = build_execution_audit(
                fills=cerebro.broker.execution_fills,
                trades=trades,
                initial_capital=spec.initial_capital,
                final_capital=cerebro.broker.getvalue(),
                position_size=position.size,
                position_price=position.price,
                last_close=float(strat.data.close[0]),
            )
        return BacktestResult(metrics=dict(metrics), trades=trades, equity_curve=equity_curve)


def create_engine() -> BacktraderBacktestEngine:
    """Plugin entry point — returns a ready engine instance."""
    return BacktraderBacktestEngine()
