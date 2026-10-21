# SPDX-License-Identifier: GPL-3.0-or-later
"""The first UTC-day close must reach the strategy's daily loss filter."""

import numpy as np
import pytest
from koval.engine.backtest_engine import EngineRunSpec
from koval.engine.execution_conformance import compare_execution_results
from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.live_feed import ReplayFeed, StopSignal
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup

from koval_backtrader import backtest_runner
from tests.test_paper_parity import BAR_MS, START_MS
from tests.test_position_exit import configuration
from tests.test_runtime_conformance import _snapshot


def run_rollover(monkeypatch, profile, *, exit_price=90, commission=0, repeat=False):
    candles = np.array(
        [
            [START_MS + (22 + index) * BAR_MS, *row]
            for index, row in enumerate(
                [
                    (100, 101, 99, 100, 100),
                    (100, 101, 99, 100, 100),
                    (exit_price, exit_price + 1, exit_price - 1, exit_price, 100),
                    (100, 101, 99, 100, 100),
                ]
            )
        ],
        dtype=float,
    )
    plugin_snapshots, paper_snapshots = [], []

    def strategy(snapshots):
        class Probe(DeclarativeStrategy):
            def on_bar(self):
                snapshots.append(_snapshot(self.account_snapshot()))

            def should_long(self):
                return repeat or self.bar_index == 1

            def go_long(self):
                return TradeSetup("long", 100, 90, 120, 2, "market")

            def filters(self):
                return [lambda: self.account_snapshot().daily_loss_pct < 0.1]

            def was_blocked(self):
                return False

        return Probe()

    monkeypatch.setattr(
        backtest_runner, "assemble_from_graph", lambda _: strategy(plugin_snapshots)
    )
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda _: strategy(paper_snapshots)
    )
    config = configuration(version=profile, costs={"commission_bps": commission})
    events, paper_events = [], []
    result = backtest_runner.create_engine().run(
        EngineRunSpec(
            graph={}, feeds={"1h": candles}, initial_capital=10000, execution_config=config
        ),
        events.append,
    )
    live = LiveEngine(
        {},
        LiveEngineConfig(
            "BTCUSDT",
            "1h",
            10000,
            exchange="binance",
            market="spot",
            execution=config["execution_model"] | {"version": "paper_" + profile},
            end_of_data_policy="mark_at_last_close",
        ),
        on_event=paper_events.append,
    )
    live.run(ReplayFeed(candles), StopSignal())
    compare_execution_results(paper_snapshots, plugin_snapshots)
    return result, events, plugin_snapshots, paper_events


@pytest.mark.parametrize("profile", ["ohlcv_fixed_v1", "ohlcv_realistic_v2"])
@pytest.mark.parametrize("exit_price,commission", [(90, 0), (125, 0), (90, 10), (125, 10)])
def test_midnight_full_close_preserves_loss_gain_and_exit_fee(
    monkeypatch, profile, exit_price, commission
):
    result, _, seen, _ = run_rollover(
        monkeypatch, profile, exit_price=exit_price, commission=commission
    )
    entry, exit_fill = result.metrics["execution_costs"]["fills"]
    quantity = entry["size"]
    entry_fee = quantity * 100 * commission / 10000
    exit_fee = quantity * exit_price * commission / 10000
    pnl = quantity * (exit_price - 100)
    assert entry["fill_price"] == 100
    assert exit_fill["fill_price"] == exit_price
    assert entry["timestamp_ms"] == START_MS + 23 * BAR_MS
    assert exit_fill["timestamp_ms"] == START_MS + 24 * BAR_MS
    assert seen[1]["equity"] == pytest.approx(10000 - entry_fee)
    midnight = seen[2]
    assert midnight["balance"] == pytest.approx(10000 + pnl - entry_fee - exit_fee)
    assert midnight["daily_pnl"] == pytest.approx(pnl - exit_fee)
    assert midnight["daily_loss_pct"] == pytest.approx(
        max(0, -(pnl - exit_fee) / (10000 - entry_fee) * 100)
    )
    assert midnight["fees"] == pytest.approx(entry_fee + exit_fee)
    assert midnight["open_positions"] == 0
    assert seen[3]["daily_pnl"] == midnight["daily_pnl"]
    if commission == 0 and exit_price == 90:
        assert quantity == 2
        assert midnight["balance"] == 9980
        assert midnight["daily_pnl"] == -20
        assert midnight["daily_loss_pct"] == 0.2


@pytest.mark.parametrize("profile", ["ohlcv_fixed_v1", "ohlcv_realistic_v2"])
def test_midnight_daily_limit_blocks_reentry_in_both_runtimes(monkeypatch, profile):
    result, events, seen, paper_events = run_rollover(monkeypatch, profile, repeat=True)
    assert seen[2]["daily_loss_pct"] == 0.2
    assert seen[3]["daily_loss_pct"] == 0.2
    assert len(result.trades) == 1
    assert len(result.metrics["execution_costs"]["fills"]) == 2
    assert seen[-1]["open_positions"] == 0
    for stream in [events, paper_events]:
        assert any(event["event_type"] == "FILTER_REJECTED" for event in stream)
