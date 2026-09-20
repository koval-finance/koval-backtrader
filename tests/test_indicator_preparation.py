# SPDX-License-Identifier: GPL-3.0-or-later
"""Preparation is optional and never skips sequential simulator decisions."""

import numpy as np
import pytest
from koval.engine.backtest_engine import EngineRunSpec

from koval_backtrader import backtest_runner
from tests.test_engine_signal_parity import _ema_cross_graph
from tests.test_runtime_boundaries import candles, spec_for


def preparation_spec(rows=None):
    spec = spec_for(rows)
    if "runtime_contract" not in EngineRunSpec.__dataclass_fields__:
        spec.runtime_contract = None
    return spec


def test_runner_prepares_each_strategy_instance_with_its_primary_history(monkeypatch):
    assemble = backtest_runner.assemble_from_graph
    calls = []

    def assembled(graph):
        strategy = assemble(graph)

        def prepare(self, rows, *, history_bars):
            calls.append((self, rows.copy(), history_bars))

        monkeypatch.setattr(type(strategy), "prepare_backtest", prepare, raising=False)
        return strategy

    monkeypatch.setattr(backtest_runner, "assemble_from_graph", assembled)
    original = candles()
    first = backtest_runner.create_engine().run(preparation_spec(original))
    second = backtest_runner.create_engine().run(preparation_spec(original.copy()))
    assert len(calls) == 2
    assert calls[0][0] is not calls[1][0]
    expected = (
        original[:6] if "runtime_contract" in EngineRunSpec.__dataclass_fields__ else original
    )
    # The runner prepares the same feed it gives the simulator. Runtime boundaries
    # trim it on engine 0.12+; the supported 0.11 fallback has no such contract.
    np.testing.assert_array_equal(calls[0][1], expected)
    assert calls[0][2] == 1000
    assert first.trades == second.trades
    assert first.equity_curve == second.equity_curve
    assert first.metrics["execution_audit"] == second.metrics["execution_audit"]


def test_strategy_without_preparation_hook_still_executes(monkeypatch):
    assemble = backtest_runner.assemble_from_graph

    def assembled(graph):
        strategy = assemble(graph)
        monkeypatch.setattr(type(strategy), "prepare_backtest", None, raising=False)
        return strategy

    monkeypatch.setattr(backtest_runner, "assemble_from_graph", assembled)
    result = backtest_runner.create_engine().run(preparation_spec())
    assert result.metrics["execution_audit"]["decisions"]


@pytest.mark.parametrize("warmup", [False, True])
def test_complete_result_and_event_parity_across_rolling_boundary(monkeypatch, warmup):
    graph = _ema_cross_graph()
    if not callable(getattr(backtest_runner.assemble_from_graph(graph), "prepare_backtest", None)):
        pytest.skip("optional preparation is unavailable on the older engine compatibility matrix")
    index = np.arange(1200)
    close = 100 + 10 * np.sin(index / 10)
    rows = np.column_stack((index * 60_000, close, close + 2, close - 2, close, np.ones(1200)))
    spec = EngineRunSpec(
        graph=graph,
        feeds={"1m": rows, "5m": rows[::5]},
        initial_capital=10000,
        execution_config={
            "exchange": "binance",
            "exchange_type": "future",
            "execution_model": {
                "version": "ohlcv_fixed_v1",
                "commission_bps": 4,
                "spread_bps": 2,
                "slippage_bps": 1,
            },
        },
    )
    if warmup:
        spec.runtime_contract = {
            "version": "koval_runtime_boundaries_v1",
            "warmup_start_ms": 0,
            "evaluation_start_ms": 990 * 60_000,
            "evaluation_end_ms": 1200 * 60_000,
            "decision_clock": "bar_close",
            "initial_balance": 10000,
            "daily_baseline_equity": 10000,
            "peak_equity": 10000,
            "end_of_data_policy": "flatten_at_last_close",
        }
    fast_events, scalar_events = [], []
    fast = backtest_runner.create_engine().run(spec, on_event=fast_events.append)
    assemble = backtest_runner.assemble_from_graph

    def scalar_graph(graph):
        strategy = assemble(graph)
        monkeypatch.setattr(type(strategy), "prepare_backtest", None)
        return strategy

    monkeypatch.setattr(backtest_runner, "assemble_from_graph", scalar_graph)
    scalar = backtest_runner.create_engine().run(spec, on_event=scalar_events.append)
    assert fast.trades
    assert fast == scalar
    assert fast_events == scalar_events
