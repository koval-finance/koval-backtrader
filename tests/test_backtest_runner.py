"""Tests for the Backtrader backtest runner (GPL engine plugin)."""

from __future__ import annotations

import numpy as np
import pytest
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


def test_closed_trade_events_carry_the_real_exit_reason_and_fill_price():
    """The event stream is the audit trail; it has to agree with the trades.

    Both fields are captured during the bracket fill and were previously reset
    before the event was emitted, so every close reported ``unknown`` and the
    bar's close price instead of what actually happened.
    """
    engine = create_engine()
    received: list[dict] = []
    spec = EngineRunSpec(graph=_GRAPH, feeds={"1h": _trending_feed()}, initial_capital=10_000.0)

    result = engine.run(spec, on_event=received.append)

    closed = [event for event in received if event["event_type"] == "TRADE_CLOSED"]
    assert closed, "expected at least one closed trade"
    assert {event["payload"]["exit_reason"] for event in closed} <= {"stop_loss", "take_profit"}
    for event, trade in zip(closed, result.trades, strict=True):
        assert event["payload"]["exit_price"] == pytest.approx(trade["exit_price"])


def test_trade_ids_restart_at_one_for_every_run():
    """Backtrader's trade reference is per-process, so a second run in the same
    interpreter used to continue counting from the first."""
    engine = create_engine()
    spec = EngineRunSpec(graph=_GRAPH, feeds={"1h": _trending_feed()}, initial_capital=10_000.0)

    first = engine.run(spec)
    second = engine.run(spec)

    assert [trade["id"] for trade in first.trades] == list(range(1, len(first.trades) + 1))
    assert [trade["id"] for trade in second.trades] == [trade["id"] for trade in first.trades]


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


@pytest.mark.parametrize("higher_timeframe", [False, True])
def test_fixed_model_reconciles_graph_trades_and_repeats_exactly(higher_timeframe):
    feeds = {"1h": _trending_feed()}
    prices = 150 + 30 * np.sin(np.arange(400) / 12)
    feeds["1h"][:, 1:5] = np.column_stack([prices, prices + 1, prices - 1, prices])
    if higher_timeframe:
        feeds["4h"] = feeds["1h"][::4].copy()
    spec = EngineRunSpec(
        graph=_GRAPH,
        feeds=feeds,
        initial_capital=10_000.0,
        execution_config={
            "exchange": "binance",
            "exchange_type": "future",
            "execution_model": {
                "version": "ohlcv_fixed_v1",
                "commission_bps": 4,
                "spread_bps": 20,
                "slippage_bps": 10,
            },
        },
    )
    events = []
    first = create_engine().run(spec, on_event=events.append)
    second_events = []
    second = create_engine().run(spec, on_event=second_events.append)
    assert first == second
    assert events == second_events
    assert len(first.trades) > 2
    audit = first.metrics["execution_costs"]
    assert audit["reconciliation_error"] == pytest.approx(0, abs=1e-8)
    for trade in first.trades:
        costs = trade["execution_costs"]
        assert {fill["trade_id"] for fill in costs["fills"]} == {trade["id"]}
        assert trade["realized_pnl"] == pytest.approx(
            costs["reference_pnl"]
            - costs["spread_cost"]
            - costs["slippage_cost"]
            - costs["commission"]
        )
    closed = [e for e in events if e["event_type"] == "TRADE_CLOSED"]
    for event, trade in zip(closed, first.trades, strict=True):
        assert event["payload"]["exit_price"] == trade["exit_price"]


def test_zero_adjustment_fixed_model_preserves_legacy_graph_prices_and_equity():
    spec = EngineRunSpec(
        graph=_GRAPH,
        feeds={"1h": _trending_feed()},
        initial_capital=10_000.0,
        execution_config={"exchange": "binance", "exchange_type": "future"},
    )
    legacy = create_engine().run(spec)
    spec.execution_config["execution_model"] = {
        "version": "ohlcv_fixed_v1",
        "commission_bps": 4,
        "spread_bps": 0,
        "slippage_bps": 0,
    }
    fixed = create_engine().run(spec)
    assert fixed.equity_curve == legacy.equity_curve
    for key, value in legacy.metrics.items():
        if key != "execution_model":
            assert fixed.metrics[key] == value
    for before, after in zip(legacy.trades, fixed.trades, strict=True):
        for key in ("entry_price", "exit_price", "realized_pnl", "commission", "size"):
            assert after[key] == pytest.approx(before[key])


def _feed_at(n: int, step_ms: int) -> np.ndarray:
    """A trending [N, 6] feed whose bars are ``step_ms`` apart."""
    candles = _trending_feed(n)
    candles[:, 0] = np.arange(n, dtype=np.float64) * step_ms + 1_704_067_200_000
    return candles


def test_runner_accepts_timeframes_outside_the_exchange_kline_table():
    """30m/2h/1w ran on 0.9.1 and must keep running; only HTF needs durations."""
    engine = create_engine()
    for timeframe, step_ms in (("30m", 1_800_000), ("2h", 7_200_000), ("1w", 604_800_000)):
        spec = EngineRunSpec(
            graph=_GRAPH, feeds={timeframe: _feed_at(400, step_ms)}, initial_capital=10_000.0
        )
        assert isinstance(engine.run(spec), BacktestResult)


def test_runner_accepts_a_two_feed_pair_outside_that_table():
    engine = create_engine()
    spec = EngineRunSpec(
        graph=_GRAPH,
        feeds={"30m": _feed_at(400, 1_800_000), "2h": _feed_at(100, 7_200_000)},
        initial_capital=10_000.0,
    )
    assert isinstance(engine.run(spec), BacktestResult)
