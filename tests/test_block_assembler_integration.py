"""End-to-end smoke test: an assembled JSON graph runs through Cerebro.

Proves the whole path — `assemble_from_graph` → `make_bt_strategy_class` →
`cerebro.run()` — emits engine events and reaches the signal-detection
branch. Every other test in this suite exercises one link in that chain in
isolation; this one is the only place the links are checked together.
"""

from __future__ import annotations

import backtrader as bt
import numpy as np
import pandas as pd
from koval.engine.engine_events import EventType
from koval.strategy.block_assembler import assemble_from_graph

from koval_backtrader.bt_adapter import make_bt_strategy_class
from koval_backtrader.oco_patch import apply_oco_guard

apply_oco_guard()


def _data_with_clear_trend(n: int = 400) -> bt.feeds.PandasData:
    """Strong uptrend then downturn — guarantees ema_cross signals fire."""
    rng = np.random.default_rng(42)
    half = n // 2
    base = np.concatenate(
        [
            np.linspace(100.0, 200.0, half),
            np.linspace(200.0, 130.0, n - half),
        ]
    )
    noise = rng.standard_normal(n) * 0.5
    price = base + noise
    df = pd.DataFrame(
        {
            "open": price,
            "high": price + 1,
            "low": price - 1,
            "close": price,
            "volume": 1000.0,
        },
        index=pd.date_range("2024-01-01", periods=n, freq="1h"),
    )
    return bt.feeds.PandasData(dataname=df)


def test_assembled_graph_runs_and_emits_signal_events():
    graph = {
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
    strategy = assemble_from_graph(graph)
    Adapted = make_bt_strategy_class(type(strategy), risk_per_trade=1.0, history_bars=300)

    cerebro = bt.Cerebro()
    cerebro.adddata(_data_with_clear_trend(400))
    cerebro.broker.setcash(10_000)
    cerebro.addstrategy(Adapted)
    bt_strat = cerebro.run()[0]

    event_types = {e.event_type for e in bt_strat._events}
    assert EventType.SESSION_START in event_types
    assert EventType.SESSION_END in event_types
    # Strong trend + zero filters → at least one signal must fire and place
    # an order. The plan's lenient `OR FILTER_REJECTED` branch is unreachable
    # without filter blocks, so we assert the reachable path directly.
    assert EventType.SIGNAL_DETECTED in event_types
    assert EventType.ORDER_PLACED in event_types
