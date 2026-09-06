# SPDX-License-Identifier: GPL-3.0-or-later
"""One brain, two runtimes: the MIT LiveEngine and this plugin must emit the
same SIGNAL_DETECTED stream (decision-time, pre-fill) on identical bars.

The engine keeps its own copy of this assertion behind a ``backtrader`` marker
its gate deselects, so decision parity is only actually enforced here, where
Backtrader is installed. Fills legitimately differ (paper simulation versus the
Backtrader broker); only the decisions must match.
"""

from __future__ import annotations

import numpy as np
from koval.engine.backtest_engine import EngineRunSpec, load_backtest_engine
from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.live_feed import ReplayFeed, StopSignal


def _ema_cross_graph() -> dict:
    """An EMA-cross graph tuned so an up-then-down ramp yields one bearish cross."""
    return {
        "blocks": [
            {"id": "sig", "type": "signal.ema_cross", "params": {"fast": 9, "slow": 21}},
            {
                "id": "trend",
                "type": "filter.ema_trend",
                "params": {"period": 21, "direction": "bearish"},
            },
            {"id": "ent", "type": "entry.both", "params": {"entry_type": "market"}},
            {"id": "ex", "type": "exit.fixed_sl_tp", "params": {"sl_pct": 2.0, "risk_reward": 2.0}},
            {"id": "rsk", "type": "risk.pct_risk", "params": {"risk_pct": 1.0}},
        ],
        "connections": [
            {"from": "sig", "to": "trend"},
            {"from": "trend", "to": "ent"},
            {"from": "ent", "to": "ex"},
            {"from": "ex", "to": "rsk"},
        ],
    }


def _ramp_then_drop(n_up: int, n_down: int) -> np.ndarray:
    rows, price, ts = [], 100.0, 0
    for _ in range(n_up):
        price += 1.0
        rows.append([ts, price - 0.5, price + 0.5, price - 0.6, price, 10.0])
        ts += 60_000
    for _ in range(n_down):
        price -= 1.0
        rows.append([ts, price + 0.5, price + 0.6, price - 0.5, price, 10.0])
        ts += 60_000
    return np.array(rows, dtype=float)


def _signal_dirs_live(graph, candles):
    events = []
    LiveEngine(
        graph,
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        on_event=events.append,
    ).run(ReplayFeed(candles, delay_seconds=0.0), StopSignal())
    return [e["payload"]["direction"] for e in events if e["event_type"] == "SIGNAL_DETECTED"]


def _signal_dirs_backtest(graph, candles):
    events = []
    spec = EngineRunSpec(graph=graph, feeds={"1m": candles}, initial_capital=10_000.0)
    load_backtest_engine().run(spec, on_event=events.append)
    return [e["payload"]["direction"] for e in events if e.get("event_type") == "SIGNAL_DETECTED"]


def test_live_and_backtest_emit_same_signal_directions():
    candles = _ramp_then_drop(25, 25)
    graph = _ema_cross_graph()
    live = _signal_dirs_live(graph, candles)
    backtest = _signal_dirs_backtest(graph, candles)
    assert live, "expected at least one signal"
    assert live == backtest
