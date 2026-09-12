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
from koval.engine.backtest_engine import (
    BacktestResult,
    EngineRunSpec,
    ExecutionCapabilities,
    check_protocol_version,
    negotiate_execution_capabilities,
)
from koval.engine.engine_events import EngineEvent
from koval.engine.run_identity import CandleStreamIdentity
from koval.engine.timeframe_utils import ordered_timeframes, timeframe_to_minutes
from koval.engine.trade_metrics import build_closed_trade_metrics
from koval.strategy.block_assembler import assemble_from_graph

from koval_backtrader.bt_adapter import make_bt_strategy_class
from koval_backtrader.bt_analyzers import EquityCurveAnalyzer, TradeListAnalyzer
from koval_backtrader.execution_audit import build_execution_audit, execution_metadata
from koval_backtrader.execution_broker import ExecutionCostBroker
from koval_backtrader.execution_config import (
    COSTED_VERSIONS,
    REALISTIC_VERSION,
    resolve_execution_model,
)
from koval_backtrader.execution_evidence import validate_evidence_coverage
from koval_backtrader.oco_patch import apply_oco_guard
from koval_backtrader.realistic_broker import RealisticBroker
from koval_backtrader.research_metrics import build_research_metrics
from koval_backtrader.run_identity import build_run_identity
from koval_backtrader.runtime_boundaries import prepare_runtime

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


def _optional_timeframe_ms(timeframe: str) -> int | None:
    """Duration when the label resolves, otherwise None.

    A single-feed run has always accepted any label the engine can order, and
    that stays true. The result records the unresolved duration rather than
    inventing one, and grades itself accordingly.
    """
    try:
        return _timeframe_ms(timeframe)
    except ValueError:
        return None


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


def _open_position_view(position, last_close: float) -> dict | None:
    """A position still open at the end of the data, valued but not realised.

    Marking it at the last close is not the same as flattening it: no exit was
    simulated, no exit cost was charged, and the number would move if the data
    ran one bar longer. Keeping the two apart stops an unrealised mark from
    being read as a completed result.
    """
    size = float(position.size)
    if not size:
        return None
    return {
        "direction": "long" if size > 0 else "short",
        "quantity": abs(size),
        "entry_price": float(position.price),
        "last_close": float(last_close),
        "unrealized_pnl": size * (float(last_close) - float(position.price)),
        "realized": False,
        "valuation": "marked_to_last_close",
    }


class BacktraderBacktestEngine:
    """Runs a backtest with Backtrader. Implements ``BacktestEngineProtocol``."""

    def run(
        self,
        spec: EngineRunSpec,
        on_event: Callable[[dict], None] | None = None,
    ) -> BacktestResult:
        check_protocol_version(spec)
        model = resolve_execution_model(spec.execution_config)
        if len(spec.feeds) > 2:
            raise ValueError("this plugin supports at most two timeframes for one instrument")
        if model.version in COSTED_VERSIONS:
            _validate_fixed_inputs(spec)
        spec, boundaries = prepare_runtime(spec)
        if boundaries is not None and model.version not in COSTED_VERSIONS:
            raise ValueError("runtime boundaries require a costed execution profile")
        if model.version == REALISTIC_VERSION:
            for timeframe, candles in spec.feeds.items():
                identity = CandleStreamIdentity(timeframe)
                for row in candles:
                    identity.append(row)
        features = ["run_identity"]
        if boundaries is not None:
            features += ["runtime_boundaries_v1"]
        if model.version == REALISTIC_VERSION:
            features += ["same_bar_protection"]
            features += list(model.execution_evidence.as_config())
        negotiated = negotiate_execution_capabilities(
            spec,
            ExecutionCapabilities(
                execution_contract_versions=(1, 2) if model.version == REALISTIC_VERSION else (1,),
                features=tuple(features),
            ),
        )
        if model.version == REALISTIC_VERSION:
            primary = spec.feeds[ordered_timeframes(list(spec.feeds))[0]]
            if boundaries is not None:
                primary = primary[primary[:, 0] >= boundaries.evaluation_start_ms]
            validate_evidence_coverage(model.execution_evidence, primary[:, 0])
        metadata = execution_metadata(model)
        if boundaries is not None:
            metadata["runtime_contract"] = boundaries.as_dict()
            metadata["assumptions"]["end_of_data"] = boundaries.end_of_data_policy
        metadata["negotiated_capabilities"] = {
            "execution_contract_version": negotiated.execution_contract_version,
            "features": list(negotiated.features),
        }
        timeframes = ordered_timeframes(list(spec.feeds.keys()))
        # Only higher-timeframe availability needs durations, so a single-feed
        # run keeps accepting any label the engine's ordering accepts. A
        # multi-timeframe run cannot: every label there must resolve, or the
        # availability arithmetic is guesswork.
        primary_ms = htf_ms = None
        if len(timeframes) > 1:
            durations = {tf: _timeframe_ms(tf) for tf in timeframes}
            primary_ms, htf_ms = (durations[tf] for tf in timeframes[:2])
            if htf_ms % primary_ms != 0:
                raise ValueError(
                    "higher timeframe must be a whole multiple of the primary timeframe"
                )
        else:
            durations = {tf: _optional_timeframe_ms(tf) for tf in timeframes}
        primary_ms = durations[timeframes[0]]
        strategy = assemble_from_graph(spec.graph)
        bt_cls = make_bt_strategy_class(
            type(strategy),
            event_sink=_make_sink(on_event),
            execution_metadata=metadata,
            primary_timeframe_ms=primary_ms,
            htf_timeframe_ms=htf_ms,
            market_identity=model.market,
            runtime_boundaries=boundaries,
        )

        cerebro = bt.Cerebro()
        if model.version in COSTED_VERSIONS:
            broker_cls = (
                RealisticBroker if model.version == REALISTIC_VERSION else ExecutionCostBroker
            )
            cerebro.setbroker(
                broker_cls(
                    execution_model=model,
                    evaluation_start_ms=None
                    if boundaries is None
                    else boundaries.evaluation_start_ms,
                )
            )
        for tf in timeframes:
            candles = spec.feeds[tf]
            if tf != timeframes[0]:
                # A secondary feed cannot advance execution beyond the primary
                # dataset or replay its last candle as fresh liquidity.
                candles = candles[candles[:, 0] <= spec.feeds[timeframes[0]][-1, 0]]
                if not len(candles):
                    raise ValueError("higher timeframe has no bars within the primary interval")
            cerebro.adddata(_feed_from_ndarray(candles))
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
        metrics["run_identity"] = build_run_identity(
            graph=spec.graph,
            feeds=spec.feeds,
            durations=durations,
            ordered_timeframes=timeframes,
            history_bars=strat.params.history_bars,
            model=model,
            implementation_sha256=metadata["implementation_sha256"],
            initial_capital=spec.initial_capital,
            consumed_bars=len(equity_curve),
            boundaries=boundaries,
        )
        position = cerebro.broker.getposition(strat.data)
        if model.version in COSTED_VERSIONS:
            metrics["execution_audit"] = strat._trace.export(strat)
        last_close = float(strat.data.close[0])
        if model.version in COSTED_VERSIONS:
            metrics["execution_costs"] = build_execution_audit(
                fills=cerebro.broker.execution_fills,
                trades=trades,
                initial_capital=spec.initial_capital,
                final_capital=cerebro.broker.getvalue(),
                position_size=position.size,
                position_price=position.price,
                last_close=last_close,
                funding_entries=getattr(
                    getattr(cerebro.broker, "execution", None), "funding_entries", ()
                ),
                funding_status=getattr(cerebro.broker, "funding_status", "unavailable"),
                liquidation_fee=sum(
                    fill.get("liquidation_fee", 0.0) for fill in cerebro.broker.execution_fills
                ),
            )
        if model.version == REALISTIC_VERSION:
            metrics["execution_costs"]["ambiguities"] = list(cerebro.broker.ambiguities)
        metrics["research"] = build_research_metrics(
            trades=trades,
            equity_curve=equity_curve,
            total_bars=len(equity_curve),
            bars_in_position=strat.exposure_bars(),
            max_drawdown_pct=metrics["max_drawdown"],
            execution_costs=metrics.get("execution_costs"),
            open_position=_open_position_view(position, last_close),
        )
        return BacktestResult(metrics=dict(metrics), trades=trades, equity_curve=equity_curve)


def create_engine() -> BacktraderBacktestEngine:
    """Plugin entry point — returns a ready engine instance."""
    return BacktraderBacktestEngine()
