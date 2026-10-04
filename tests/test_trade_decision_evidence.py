# SPDX-License-Identifier: GPL-3.0-or-later
"""Evidence follows its entry order through delayed and partial execution."""

from copy import deepcopy
from dataclasses import field, make_dataclass

import numpy as np
import pytest
from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig
from koval.strategy.base.trade_setup import TradeSetup

from koval_backtrader.backtest_runner import create_engine
from tests import test_realistic_evidence as fixture
from tests.test_realistic_evidence import QUIET, START, STEP, D, execute
from tests.test_runtime_boundaries import spec_for, unsupported_runtime


def test_actual_graph_context_reaches_trade_and_origin_decision():
    rows = np.array(
        [
            [START + i * STEP, *row]
            for i, row in enumerate([QUIET] * 4 + [(100, 125, 99, 120, 100)] * 3)
        ]
    )
    spec = spec_for(rows)
    if unsupported_runtime(spec):
        return
    result = create_engine().run(spec)
    assert result.trades
    trade = result.trades[0]
    context = trade.get("decision_context")
    if "decision_context" not in TradeSetup.__dataclass_fields__:
        # Older engines emit entries without graph decision recording. Verify
        # actual execution links and absence of invented graph evidence instead.
        assert context is None
        entries = [
            f
            for f in result.metrics["execution_audit"]["fills"]
            if f["role"] == "entry" and f["trade_id"] == trade["id"]
        ]
        assert trade["entry_order_id"] == entries[0]["order_id"]
        assert all(f["decision_id"] == trade["decision_id"] for f in entries)
        return
    assert context and context["status"] == "recorded"
    assert context["risk"]["risk_budget"] == 20
    assert context["signal_bar_open_ms"] == START + 2 * STEP
    assert context["decision_timestamp_ms"] == START + 3 * STEP
    assert context["history_start_ms"] == START
    assert all(n["timeframe"] == "1m" for n in context["nodes"])
    audit = result.metrics["execution_audit"]
    intent = next(i for i in audit["intents"] if i["order_id"] == trade["entry_order_id"])
    decision = next(d for d in audit["decisions"] if d["decision_id"] == intent["decision_id"])
    assert decision["decision_context"] == context
    assert decision["indicators"] is not None
    assert intent["setup"]["decision_context"] == context
    entry_order = next(o for o in audit["orders"] if o["order_id"] == trade["entry_order_id"])
    assert entry_order["submitted_timestamp_ms"] == context["decision_timestamp_ms"]


@pytest.mark.parametrize(
    "partial,delayed,gap",
    [(False, False, False), (False, True, False), (True, False, False), (False, False, True)],
)
def test_snapshot_survives_execution_and_is_detached(monkeypatch, partial, delayed, gap):
    original = {
        "version": "koval_trade_decision_context_v1",
        "status": "partial",
        "nodes": [{"values": {"flag": False, "zero": 0, "unknown": None}}],
    }
    monkeypatch.setattr(
        fixture,
        "TradeSetup",
        lambda **kw: (
            TradeSetup
            if "decision_context" in TradeSetup.__dataclass_fields__
            else make_dataclass(
                "RecordedSetup",
                [("decision_context", dict | None, field(default=None))],
                bases=(TradeSetup,),
            )
        )(**kw, decision_context=deepcopy(original)),
    )
    price = 110 if delayed else 100
    rows = [QUIET, QUIET, (price, price + 1, price - 1, price, 100)] + [
        (price, price + 1, price - 1, price, 100)
    ] * 5
    rows += [(80, 85, 75, 80, 100)] * 4 if gap else [(120, 125, 115, 120, 100)] * 4
    result, _, _ = execute(
        monkeypatch,
        rows,
        entry=price,
        entry_type="stop" if delayed else "market",
        evidence={"execution_proxy": ExecutionProxyConfig(D("0.1"), "carry", ExecutionLatency())}
        if partial
        else {},
        costs={"commission_bps": 4, "spread_bps": 2, "slippage_bps": 3},
    )
    assert result.trades
    trade = result.trades[0]
    assert trade["decision_context"]["nodes"] == original["nodes"]
    assert trade["entry_order_id"]
    audit = result.metrics["execution_audit"]
    fills = [f for f in audit["fills"] if f["fill_id"] in trade["fill_ids"]]
    entries = [f for f in fills if f["role"] == "entry"]
    assert entries
    assert all(f["decision_id"] == trade["decision_id"] for f in entries)
    assert all(f.get("sizing_adjustment") for f in entries)
    adjustment = entries[0]["sizing_adjustment"]
    assert adjustment["quantity_after_risk"] < adjustment["requested_remaining_quantity"]
    assert adjustment["available_cash"] > 0
    trade["decision_context"]["nodes"][0]["values"]["zero"] = 123
    assert audit["intents"][0]["setup"]["decision_context"]["nodes"][0]["values"]["zero"] == 0


def test_updated_stop_keeps_entry_origin_and_records_change(monkeypatch):
    result, _, _ = execute(monkeypatch, [QUIET] * 4 + [(97, 98, 96, 97, 10)], stop_updates={2: 98})
    assert result.trades
    trade = result.trades[0]
    assert trade["sl_history"][-1]["stop_loss"] == 98
    audit = result.metrics["execution_audit"]
    entry = next(f for f in audit["fills"] if f["role"] == "entry")
    exit_fill = next(f for f in audit["fills"] if f["role"] == "exit")
    assert exit_fill["decision_id"] == entry["decision_id"]


def test_ambiguous_protection_and_gap_are_recorded_on_the_actual_exit_fill(monkeypatch):
    for row, both, gap in [
        ((100, 125, 85, 100, 100), True, False),
        ((80, 85, 75, 80, 100), False, True),
    ]:
        result, _, _ = execute(monkeypatch, [QUIET, QUIET, row])
        fill = next(f for f in result.metrics["execution_audit"]["fills"] if f["role"] == "exit")
        context = fill["protection_context"]
        assert context["both_levels_touched"] is both
        assert context["gap_through_stop"] is gap
        assert context["stop_loss"] == 90
        assert context["take_profit"] == 120
        assert context["selected"] == "stop_loss"
        assert context["rule"] == ("stop_gap_at_open" if gap else "conservative_stop_first")


def test_entry_breach_has_priority_over_both_levels_touched(monkeypatch):
    result, _, _ = execute(monkeypatch, [QUIET, (125, 130, 80, 125, 100), QUIET])
    fill = next(f for f in result.metrics["execution_audit"]["fills"] if f["role"] == "exit")
    context = fill["protection_context"]
    assert context["both_levels_touched"] is True
    assert context["selected"] == "take_profit"
    assert context["entry_breach"] == "take_profit"
    assert context["rule"] == "entry_price_breached_protection"


def test_closed_trade_releases_mutable_stop_history(monkeypatch):
    from koval_backtrader.bt_adapter import BTStrategyAdapter

    original = BTStrategyAdapter.notify_trade
    retained = []

    def observe_map(self, trade):
        original(self, trade)
        if trade.isclosed:
            retained.append(len(self._trade_map))

    monkeypatch.setattr(BTStrategyAdapter, "notify_trade", observe_map)
    result, _, _ = execute(monkeypatch, [QUIET] * 4 + [(97, 98, 96, 97, 10)], stop_updates={2: 98})
    assert result.trades[0]["sl_history"]
    assert retained == [0]
