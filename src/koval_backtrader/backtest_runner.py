# SPDX-License-Identifier: GPL-3.0-or-later
# Koval Backtrader Adapter — backtest engine plugin
"""Backtrader implementation of ``BacktestEngineProtocol``.

Loaded at runtime by ``koval.engine.backtest_engine.load_backtest_engine()``.
GPL-3.0: imports Backtrader. May import MIT code; MIT never imports this.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import backtrader as bt
import numpy as np
import pandas as pd
from koval.engine.backtest_engine import BacktestResult, EngineRunSpec, check_protocol_version
from koval.engine.engine_events import EngineEvent
from koval.engine.execution_settings import resolve_execution_settings
from koval.engine.timeframe_utils import ordered_timeframes
from koval.engine.trade_metrics import build_closed_trade_metrics
from koval.strategy.block_assembler import assemble_from_graph

from koval_backtrader.bt_adapter import make_bt_strategy_class
from koval_backtrader.bt_analyzers import EquityCurveAnalyzer, TradeListAnalyzer
from koval_backtrader.oco_patch import apply_oco_guard

apply_oco_guard()

_OHLCV_COLS = ["open", "high", "low", "close", "volume"]


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
                "payload": event.payload,
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
        timeframes = ordered_timeframes(list(spec.feeds.keys()))
        strategy = assemble_from_graph(spec.graph)
        bt_cls = make_bt_strategy_class(type(strategy), event_sink=_make_sink(on_event))

        cerebro = bt.Cerebro()
        for tf in timeframes:
            cerebro.adddata(_feed_from_ndarray(spec.feeds[tf]))
        cerebro.broker.setcash(spec.initial_capital)
        if spec.execution_config:
            settings = resolve_execution_settings(spec.execution_config)
            cerebro.broker.setcommission(commission=settings.commission_rate)
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
        return BacktestResult(metrics=dict(metrics), trades=trades, equity_curve=equity_curve)


def create_engine() -> BacktraderBacktestEngine:
    """Plugin entry point — returns a ready engine instance."""
    return BacktraderBacktestEngine()
