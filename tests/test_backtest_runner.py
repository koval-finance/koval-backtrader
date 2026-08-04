"""Tests for the Backtrader backtest runner (GPL engine plugin)."""

from __future__ import annotations

import numpy as np
from koval.engine.backtest_engine import BacktestResult, EngineRunSpec

from koval_backtrader.backtest_runner import create_engine

_GRAPH = {
    "blocks": [
        {"id": "sig", "type": "signal.ema_cross", "params": {"fast": 5, "slow": 20}},
        {"id": "ent", "type": "entry.both", "params": {"entry_type": "market"}},
        {"id": "ex", "type": "exit.fixed_sl_tp", "params": {"sl_pct": 5.0, "risk_reward": 1.5}},
        {"id": "rsk", "type": "risk.pct_risk", "params": {"risk_pct": 1.0}},
    ],
    "connections": [
        {"from": "sig", "to": "ent"},
        {"from": "ent", "to": "ex"},
        {"from": "ex", "to": "rsk"},
    ],
}


def _trending_feed(n: int = 400) -> np.ndarray:
    """[N, 6] OHLCV — col0 = timestamp_ms; strong up-then-down trend."""
    half = n // 2
    base = np.concatenate([np.linspace(100.0, 200.0, half), np.linspace(200.0, 130.0, n - half)])
    ts = np.arange(n, dtype=np.float64) * 3_600_000 + 1_704_067_200_000
    out = np.empty((n, 6), dtype=np.float64)
    out[:, 0] = ts
    out[:, 1] = base
    out[:, 2] = base + 1.0
    out[:, 3] = base - 1.0
    out[:, 4] = base
    out[:, 5] = 1000.0
    return out


def test_runner_produces_a_backtest_result():
    engine = create_engine()
    spec = EngineRunSpec(graph=_GRAPH, feeds={"1h": _trending_feed()}, initial_capital=10_000.0)
    result = engine.run(spec)
    assert isinstance(result, BacktestResult)
    assert result.metrics["total_trades"] >= 1
    assert len(result.equity_curve) > 0
    assert result.equity_curve[0]["equity"] > 0


def test_runner_forwards_events_to_on_event():
    engine = create_engine()
    received: list[dict] = []
    spec = EngineRunSpec(graph=_GRAPH, feeds={"1h": _trending_feed()}, initial_capital=10_000.0)
    engine.run(spec, on_event=received.append)
    assert any(e["event_type"] == "SIGNAL_DETECTED" for e in received)


def test_runner_accepts_two_feeds():
    engine = create_engine()
    spec = EngineRunSpec(
        graph=_GRAPH,
        feeds={"1h": _trending_feed(400), "4h": _trending_feed(100)},
        initial_capital=10_000.0,
    )
    result = engine.run(spec)
    assert isinstance(result, BacktestResult)


def test_runner_trades_carry_real_size_exit_price_and_reason():
    """Closed bracket trades must expose a non-zero size, a real exit price
    (different from entry), and a tp/sl exit reason — not the zero-size
    fallback that leaks ``exit_price == entry_price`` and ``size == 0``."""
    engine = create_engine()
    spec = EngineRunSpec(graph=_GRAPH, feeds={"1h": _trending_feed()}, initial_capital=10_000.0)
    result = engine.run(spec)

    assert result.trades, "expected at least one closed trade"
    for trade in result.trades:
        assert trade["size"] > 0, f"size should be non-zero: {trade}"
        assert trade["exit_price"] != trade["entry_price"], (
            f"exit_price must differ from entry_price: {trade}"
        )
        assert trade["exit_reason"] in {"Take Profit", "Stop Loss"}, (
            f"bracket exit must be tp/sl: {trade}"
        )


def test_runner_applies_futures_commission_from_execution_config():
    """An execution_config marks fees on the broker, so an otherwise-identical
    run finishes with less capital and a non-zero per-trade commission."""
    engine = create_engine()
    feed = _trending_feed()

    free = engine.run(EngineRunSpec(graph=_GRAPH, feeds={"1h": feed}, initial_capital=10_000.0))
    charged = engine.run(
        EngineRunSpec(
            graph=_GRAPH,
            feeds={"1h": feed},
            initial_capital=10_000.0,
            execution_config={"exchange": "binance", "exchange_type": "future"},
        )
    )

    assert charged.metrics["final_capital"] < free.metrics["final_capital"]
    assert any(t["commission"] > 0 for t in charged.trades)
    assert all(t["commission"] == 0 for t in free.trades)
