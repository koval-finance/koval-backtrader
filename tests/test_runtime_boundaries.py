# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual graph decisions, account baselines and terminal policies across runtimes."""

import json
from dataclasses import asdict, fields

import numpy as np
import pytest
from koval.engine.account_state import AccountSnapshot
from koval.engine.backtest_engine import EngineRunSpec, ProtocolVersionError
from koval.engine.execution_conformance import compare_execution_results
from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.live_feed import ReplayFeed, StopSignal

from koval_backtrader import backtest_runner
from tests.test_realistic_evidence import QUIET, START, STEP

CONTRACT = {
    "version": "koval_runtime_boundaries_v1",
    "warmup_start_ms": START,
    "evaluation_start_ms": START + 2 * STEP,
    "evaluation_end_ms": START + 6 * STEP,
    "decision_clock": "bar_close",
    "initial_balance": 10_000.0,
    "daily_baseline_equity": 11_000.0,
    "peak_equity": 12_000.0,
    "end_of_data_policy": "mark_at_last_close",
}
MODEL = dict(version="ohlcv_realistic_v2", commission_bps=4, spread_bps=2, slippage_bps=3)
MARKET = dict(
    exchange="binance", market="future", canonical_symbol="BTCUSDT", contract_type="perpetual"
)


def graph(direction="long"):
    return {
        "blocks": [
            {
                "id": "sig",
                "type": "signal.every_bar",
                "params": {"direction": "bullish" if direction == "long" else "bearish"},
            },
            {"id": "ent", "type": "entry.both", "params": {"entry_type": "market"}},
            {
                "id": "ex",
                "type": "exit.fixed_sl_tp",
                "params": {"sl_pct": 10.0, "risk_reward": 2.0},
            },
            {"id": "risk", "type": "risk.pct_risk", "params": {"risk_pct": 0.2, "leverage": 1.0}},
        ],
        "connections": [
            {"from": a, "to": b} for a, b in (("sig", "ent"), ("ent", "ex"), ("ex", "risk"))
        ],
    }


def candles():
    return np.array(
        [
            [START + i * STEP, *row]
            for i, row in enumerate([QUIET] * 5 + [(100, 106, 99, 105, 10), QUIET])
        ]
    )


def spec_for(rows=None, *, contract=None, model=None, **evidence):
    spec = EngineRunSpec(
        graph=graph(),
        feeds={"1m": candles() if rows is None else rows},
        initial_capital=10_000,
        execution_config={
            "exchange": "binance",
            "exchange_type": "future",
            "market": MARKET,
            "execution_model": model or MODEL,
            **evidence,
        },
    )
    # On older engines an explicit request must fail, never silently trade preroll.
    spec.runtime_contract = json.loads(json.dumps(CONTRACT if contract is None else contract))
    return spec


def unsupported_runtime(spec):
    if "runtime_contract" in EngineRunSpec.__dataclass_fields__:
        return False
    with pytest.raises(ProtocolVersionError, match="runtime"):
        backtest_runner.create_engine().run(spec)
    return True


@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize(
    "version,partial,delay",
    [
        ("ohlcv_fixed_v1", False, 0),
        ("ohlcv_realistic_v2", False, 0),
        ("ohlcv_realistic_v2", True, 0),
        ("ohlcv_realistic_v2", True, STEP),
    ],
)
@pytest.mark.parametrize("policy", ["mark_at_last_close", "flatten_at_last_close"])
@pytest.mark.parametrize("preload", [False, True])
def test_real_graph_preroll_accounts_and_terminal_policy(
    monkeypatch, direction, version, partial, delay, policy, preload
):
    contract = CONTRACT | {"end_of_data_policy": policy}
    model = MODEL | {"version": version}
    from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig

    from tests.test_realistic_evidence import D, instrument

    evidence = (
        {
            "execution_proxy": ExecutionProxyConfig(
                D("0.1"), "carry", ExecutionLatency(decision_to_submission_ms=delay)
            ),
            "instrument_specs": (instrument(),),
        }
        if partial
        else {}
    )
    spec = spec_for(contract=contract, model=model, **evidence)
    if unsupported_runtime(spec):
        return
    spec.graph = graph(direction)
    captured = {"plugin": [], "paper": []}
    graph_class = type(backtest_runner.assemble_from_graph(spec.graph))

    def strategy(name):
        class Probe(graph_class):
            def on_bar(self):
                super().on_bar()
                snap = asdict(self._ctx().account)
                captured[name].append(
                    {
                        "bar": self.bar_index,
                        "timestamp_ms": self.timestamp_ms,
                        "decision_timestamp_ms": self.decision_timestamp_ms,
                        "history": self.closes.tolist(),
                        "account": {f.name: snap[f.name] for f in fields(AccountSnapshot)},
                    }
                )

        return Probe()

    monkeypatch.setattr(backtest_runner, "assemble_from_graph", lambda _: strategy("plugin"))
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda _: strategy("paper"))
    statuses, live_trades = [], []
    runtime = LiveEngine(
        spec.graph,
        LiveEngineConfig(
            "BTCUSDT",
            "1m",
            10_000,
            exchange="binance",
            execution=model | {"version": "paper_" + version},
            history=spec.feeds["1m"][:2] if preload else None,
            runtime_contract=contract,
            **evidence,
        ),
        on_status=statuses.append,
        on_trade=live_trades.append,
    )
    runtime.run(ReplayFeed(spec.feeds["1m"][2:] if preload else spec.feeds["1m"]), StopSignal())
    events = []
    result = backtest_runner.create_engine().run(spec, events.append)
    compare_execution_results(captured["paper"], captured["plugin"])
    assert [s["bar"] for s in captured["plugin"]] == [3, 4, 5, 6]
    assert len(result.equity_curve) == 4
    assert result.metrics["final_capital"] == pytest.approx(statuses[-1]["metrics"]["equity"])
    terminal = next(e["payload"] for e in events if e["event_type"] == "SESSION_END")
    for field in fields(AccountSnapshot):
        compare_execution_results(
            asdict(runtime._account.snapshot())[field.name], terminal["account"][field.name]
        )
    assert all(
        e["timestamp_ms"] >= contract["evaluation_start_ms"] for e in terminal["account_ledger"]
    )
    assert len(result.trades) == len(live_trades) == (1 if policy == "flatten_at_last_close" else 0)
    for name in ("dataset_identity", "execution_identity", "run_parameters"):
        compare_execution_results(
            runtime._run_identity()[name], result.metrics["run_identity"][name]
        )
    assert result.metrics["execution_model"]["runtime_contract"] == contract


@pytest.mark.parametrize("rows", [candles()[1:], candles()[:5], np.delete(candles(), 3, axis=0)])
def test_incomplete_window_is_refused_before_any_event(rows):
    spec = spec_for(rows)
    if unsupported_runtime(spec):
        return
    events = []
    with pytest.raises(ValueError, match="coverage"):
        backtest_runner.create_engine().run(spec, events.append)
    assert not events


def test_future_outside_evaluation_does_not_change_execution():
    spec = spec_for()
    if unsupported_runtime(spec):
        return
    before = backtest_runner.create_engine().run(spec)
    spec.feeds["1m"][-1, 1:5] = 1_000_000
    after = backtest_runner.create_engine().run(spec)
    assert before.trades == after.trades
    assert before.equity_curve == after.equity_curve
    assert before.metrics["execution_costs"] == after.metrics["execution_costs"]


def test_funding_coverage_only_needs_evaluation_not_warmup():
    from decimal import Decimal

    from koval.engine.funding import FundingRecord, build_funding_series

    funding = build_funding_series(
        [
            FundingRecord(
                "BTCUSDT", Decimal("0.001"), START + i * STEP, Decimal("100"), STEP, "test"
            )
            for i in range(2, 6)
        ],
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=START + 2 * STEP,
        requested_end_ms=START + 5 * STEP,
    )
    spec = spec_for(funding=funding)
    if unsupported_runtime(spec):
        return
    result = backtest_runner.create_engine().run(spec)
    entries = result.metrics["execution_costs"]["funding_entries"]
    assert [e["timestamp_ms"] for e in entries] == [START + 4 * STEP, START + 5 * STEP]


def test_daily_baseline_rollover_matches_the_real_paper_runtime():
    offset = 24 * 60 * STEP - 3 * STEP
    contract = {
        key: value + offset
        if key in {"warmup_start_ms", "evaluation_start_ms", "evaluation_end_ms"}
        else value
        for key, value in CONTRACT.items()
    }
    rows = candles()
    rows[:, 0] += offset
    spec = spec_for(rows, contract=contract)
    if unsupported_runtime(spec):
        return
    runtime = LiveEngine(
        spec.graph,
        LiveEngineConfig(
            "BTCUSDT",
            "1m",
            10_000,
            exchange="binance",
            execution=MODEL | {"version": "paper_ohlcv_realistic_v2"},
            runtime_contract=contract,
        ),
    )
    runtime.run(ReplayFeed(rows), StopSignal())
    result = backtest_runner.create_engine().run(spec)
    audit = result.metrics["execution_audit"]
    assert audit["account_snapshots"][0]["account"]["daily_pnl"] == -1000
    assert -10 < audit["account_snapshots"][1]["account"]["daily_pnl"] < 0
    assert audit["terminal_account"]["peak_equity"] == 12_000
    for field in fields(AccountSnapshot):
        compare_execution_results(
            asdict(runtime._account.snapshot())[field.name], audit["terminal_account"][field.name]
        )


@pytest.mark.parametrize("bar_count", [15, 1010])
def test_explicit_htf_decisions_use_the_same_completed_history_window(monkeypatch, bar_count):
    rows = np.array([[START + i * STEP, *QUIET] for i in range(bar_count)])
    contract = CONTRACT | {"evaluation_end_ms": START + bar_count * STEP}
    spec = spec_for(rows, contract=contract)
    if unsupported_runtime(spec):
        return
    spec.feeds["5m"] = np.array(
        [[START + i * STEP, 100, 101, 99, 100, 50] for i in range(0, bar_count, 5)]
    )
    graph_class = type(backtest_runner.assemble_from_graph(spec.graph))
    captured = {"plugin": [], "paper": []}

    def strategy(name):
        class Probe(graph_class):
            def on_bar(self):
                super().on_bar()
                captured[name].append(
                    {
                        "bar": self.bar_index,
                        "htf": None if self.htf_closes is None else self.htf_closes.tolist(),
                        "account": {
                            f.name: asdict(self._ctx().account)[f.name]
                            for f in fields(AccountSnapshot)
                        },
                    }
                )

        return Probe()

    monkeypatch.setattr(backtest_runner, "assemble_from_graph", lambda _: strategy("plugin"))
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda _: strategy("paper"))
    runtime = LiveEngine(
        spec.graph,
        LiveEngineConfig(
            "BTCUSDT",
            "1m",
            10_000,
            exchange="binance",
            higher_timeframe="5m",
            requires_higher_timeframe=True,
            execution=MODEL | {"version": "paper_ohlcv_realistic_v2"},
            runtime_contract=contract,
        ),
    )
    runtime.run(ReplayFeed(rows), StopSignal())
    result = backtest_runner.create_engine().run(spec)
    compare_execution_results(captured["paper"], captured["plugin"])
    first_fill = result.metrics["execution_costs"]["fills"][0]
    assert first_fill["timestamp_ms"] == START + 5 * STEP
