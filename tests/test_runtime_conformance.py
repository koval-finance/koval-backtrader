# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare real graph contexts and final state across both replay runtimes."""

from dataclasses import asdict, fields
from decimal import Decimal as D

import numpy as np
import pytest
from koval.engine.account_state import AccountSnapshot
from koval.engine.backtest_engine import EngineRunSpec
from koval.engine.execution_conformance import compare_execution_results
from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig
from koval.engine.funding import FundingRecord, build_funding_series
from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.live_feed import ReplayFeed, StopSignal
from koval.strategy.base.trade_setup import TradeSetup
from koval.strategy.graph.strategy import build_graph_strategy

from koval_backtrader import backtest_runner
from tests.test_realistic_evidence import QUIET, START, STEP, instrument

GRAPH = {"blocks": [{"id": "clock", "type": "fact.every_bar", "params": {}}], "connections": []}
MODEL = dict(
    version="ohlcv_realistic_v2",
    commission_bps=4.0,
    spread_bps=2.0,
    slippage_bps=10.0,
    leverage=1.0,
)


def _snapshot(account):
    values = asdict(account)
    return {field.name: values[field.name] for field in fields(AccountSnapshot)}


@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize(
    "scenario",
    ["partial_funding", "instrument", "shared_volume", "gap_stop", "gap_target", "open_end"],
)
def test_graph_accounts_and_terminal_results_match(monkeypatch, direction, scenario):
    rows, evidence = [QUIET] * 3, {}
    long = direction == "long"
    if scenario in {"partial_funding", "instrument"}:
        rows = [QUIET] * 3 + [(100, 126, 74, 125 if long else 75, 10)] * 2
        evidence["execution_proxy"] = ExecutionProxyConfig(D("0.137"), "carry", ExecutionLatency())
        if scenario == "instrument":
            evidence["instrument_specs"] = (instrument(),)
        else:
            evidence["funding"] = build_funding_series(
                [
                    FundingRecord("BTCUSDT", D("0.001"), START + i * STEP, D("100"), STEP, "test")
                    for i in range(len(rows))
                ],
                exchange="binance",
                market="future",
                symbol="BTCUSDT",
                requested_start_ms=START,
                requested_end_ms=START + (len(rows) - 1) * STEP,
            )
    elif scenario == "shared_volume":
        rows = [QUIET, (100, 111, 89, 100, 10), QUIET]
        evidence["execution_proxy"] = ExecutionProxyConfig(D("0.2"), "carry", ExecutionLatency())
    elif scenario.startswith("gap"):
        gap = (85 if long else 115) if scenario == "gap_stop" else (125 if long else 75)
        rows = [QUIET, (gap, gap + 1, gap - 1, gap, 10), QUIET]
    candles = np.array([[START + i * STEP, *row] for i, row in enumerate(rows)])

    def strategy(captured):
        class Probe(build_graph_strategy(GRAPH)):
            def on_bar(self):
                super().on_bar()
                captured.append(_snapshot(self._ctx().account))

            def should_long(self):
                return long and self.bar_index == 1 and self._ctx().account.free_margin > 200

            def should_short(self):
                return not long and self.bar_index == 1 and self._ctx().account.free_margin > 200

            def go_long(self):
                return TradeSetup(
                    direction, 100, 90 if long else 110, 120 if long else 80, 2, "market"
                )

            go_short = go_long

        return Probe()

    live_snapshots, plugin_snapshots, statuses, live_trades = [], [], [], []
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda _: strategy(live_snapshots)
    )
    monkeypatch.setattr(
        backtest_runner, "assemble_from_graph", lambda _: strategy(plugin_snapshots)
    )
    runtime = LiveEngine(
        GRAPH,
        LiveEngineConfig(
            "BTCUSDT",
            "1m",
            10_000,
            exchange="binance",
            execution=MODEL | {"version": "paper_ohlcv_realistic_v2"},
            end_of_data_policy="mark_at_last_close",
            **evidence,
        ),
        on_status=statuses.append,
        on_trade=live_trades.append,
    )
    runtime.run(ReplayFeed(candles), StopSignal())
    result = backtest_runner.create_engine().run(
        EngineRunSpec(
            graph=GRAPH,
            feeds={"1m": candles},
            initial_capital=10_000,
            execution_config={
                "exchange": "binance",
                "exchange_type": "future",
                "execution_model": MODEL,
                "market": {
                    "exchange": "binance",
                    "market": "future",
                    "canonical_symbol": "BTCUSDT",
                    "contract_type": "perpetual",
                },
                **evidence,
            },
        )
    )
    assert len(live_snapshots) == len(plugin_snapshots) == len(rows)
    compare_execution_results(live_snapshots, plugin_snapshots)
    assert result.metrics["final_capital"] == pytest.approx(statuses[-1]["metrics"]["equity"])
    assert len(result.trades) == len(live_trades)
    for live, plugin in zip(live_trades, result.trades, strict=True):
        for key in ("entry_price", "exit_price", "size", "commission", "gross_price_pnl"):
            assert live[key] == pytest.approx(plugin[key])
        assert live["pnl"] == pytest.approx(plugin["net_pnl_before_funding"])
    live_identity, plugin_identity = runtime._run_identity(), result.metrics["run_identity"]
    for key in (
        "market_identity",
        "dataset_identity",
        "execution_identity",
        "strategy_sha256",
        "run_parameters",
    ):
        compare_execution_results(live_identity[key], plugin_identity[key])
