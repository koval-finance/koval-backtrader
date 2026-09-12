# SPDX-License-Identifier: GPL-3.0-or-later
"""Saved results must explain every executed cost without an event callback."""

import json
import math
from dataclasses import asdict

import pytest
from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig

from tests.test_realistic_evidence import QUIET, START, STEP, D, execute


@pytest.mark.parametrize("version", ["ohlcv_fixed_v1", "ohlcv_realistic_v2"])
@pytest.mark.parametrize("closed", [False, True])
def test_saved_audit_links_decisions_orders_fills_and_cashflows(monkeypatch, version, closed):
    rows = [QUIET] * 3
    if closed:
        rows.append((100, 121, 99, 120, 10))
    result, events, snapshots = execute(
        monkeypatch,
        rows,
        costs={"version": version, "commission_bps": 4, "spread_bps": 2, "slippage_bps": 3},
    )
    audit = json.loads(json.dumps(result.metrics.get("execution_audit")))
    assert audit is not None, "the persisted result must include the authoritative execution audit"
    assert audit["version"] == "koval_backtrader_audit_v1"
    assert audit["completeness"] == "complete"
    decisions = {d["decision_id"]: d for d in audit["decisions"]}
    orders = {o["order_id"]: o for o in audit["orders"]}
    fills = {f["fill_id"]: f for f in audit["fills"]}
    ledger = {e["sequence"]: e for e in audit["ledger"]}
    assert len(decisions) == len(rows)
    assert audit["fills"] == result.metrics["execution_costs"]["fills"]
    for fill in fills.values():
        order = orders[fill["order_id"]]
        decision = decisions[fill["decision_id"]]
        assert order["decision_id"] == fill["decision_id"]
        assert decision["decision_timestamp_ms"] <= fill["timestamp_ms"]
        assert fill["execution_rule"]
        assert fill["cashflow_sequences"]
        for sequence in fill["cashflow_sequences"]:
            entry = ledger[sequence]
            assert entry["reference_id"] == str(fill["fill_id"])
    for decision in decisions.values():
        assert decision["indicators"] is None
        assert decision["history_end_ms"] == decision["decision_timestamp_ms"]
        assert decision["bar_timestamp_ms"] + STEP == decision["decision_timestamp_ms"]
    assert len(audit["account_snapshots"]) == len(rows)
    assert audit["terminal_account"] == asdict(snapshots[-1])
    # Independent reference arithmetic: no runtime accounting or matching helper.
    cash = 10_000 + math.fsum(e["amount"] for e in ledger.values())
    account = audit["terminal_account"]
    assert account["balance"] == pytest.approx(cash)
    assert result.metrics["final_capital"] == pytest.approx(cash + account["unrealized_pnl"])
    assert audit["open_position"] == account["open_position"]
    assert audit["events"] == events
    assert len({e["payload"]["event_id"] for e in events}) == len(events)
    if closed:
        assert result.trades[0]["fill_ids"] == list(fills)
        assert result.trades[0]["decision_id"] == audit["intents"][0]["decision_id"]
    else:
        assert not result.trades
        assert result.metrics["execution_costs"]["open_commission"] > 0


def test_partial_residual_cashflows_have_delta_ids_and_reconcile(monkeypatch):
    result, _, _ = execute(
        monkeypatch,
        [QUIET] * 3 + [(100, 121, 99, 120, 10)],
        evidence={
            "execution_proxy": ExecutionProxyConfig(D("0.1"), "carry", ExecutionLatency()),
        },
        costs={"commission_bps": 4},
    )
    audit = result.metrics.get("execution_audit")
    assert audit is not None
    assert audit["open_position"]["quantity"] > 0
    protective = [o for o in audit["orders"] if o["role"] in {"stop_loss", "take_profit"}]
    assert len(protective) == 2
    assert all(
        o["remaining_quantity"] == pytest.approx(audit["open_position"]["quantity"])
        for o in protective
    )
    assert not result.trades
    assert len(audit["fills"]) == 3
    sequences = [s for f in audit["fills"] for s in f["cashflow_sequences"]]
    assert len(sequences) == len(set(sequences)) == len(audit["ledger"])
    cash = 10_000 + sum(e["amount"] for e in audit["ledger"])
    assert result.metrics["final_capital"] == pytest.approx(
        cash + audit["terminal_account"]["unrealized_pnl"]
    )


def test_funding_cashflow_identifies_applied_record_and_rule(monkeypatch):
    from koval.engine.funding import FundingRecord, build_funding_series

    funding = build_funding_series(
        [
            FundingRecord("BTCUSDT", D("0.001"), START + i * STEP, D("100"), STEP, "archive")
            for i in range(3)
        ],
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=START,
        requested_end_ms=START + 2 * STEP,
    )
    result, _, _ = execute(monkeypatch, [QUIET] * 3, evidence={"funding": funding})
    entry = result.metrics["execution_costs"]["funding_entries"][0]
    assert entry["metadata"].get("evidence_sha256")
    assert entry["metadata"]["rule"] == "signed_position_at_settlement_before_orders"
    assert entry["metadata"]["trade_id"] == 1


def test_rejected_intent_remains_explainable_without_a_fill(monkeypatch):
    result, _, _ = execute(monkeypatch, [QUIET] * 3, size=1000)
    audit = result.metrics.get("execution_audit")
    assert audit is not None
    assert not audit["fills"]
    assert audit["intents"][0]["status"] == "rejected"
    assert audit["intents"][0]["reason"] == "insufficient_margin"
    assert audit["intents"][0]["decision_id"] == audit["decisions"][0]["decision_id"]


def test_delayed_order_rejection_keeps_its_original_intent(monkeypatch):
    result, events, _ = execute(
        monkeypatch,
        [QUIET, QUIET, (150, 151, 149, 150, 10)],
        entry=110,
        stop=100,
        target=130,
        size=90,
        entry_type="stop",
        costs={"version": "ohlcv_fixed_v1"},
    )
    audit = result.metrics["execution_audit"]
    assert audit["intents"][0]["status"] == "rejected"
    event = next(e for e in events if e["event_type"] == "ORDER_REJECTED")
    assert event["payload"]["intent_id"] == audit["intents"][0]["intent_id"]
    assert event["payload"]["decision_id"] == audit["intents"][0]["decision_id"]


@pytest.mark.parametrize("version", ["ohlcv_fixed_v1", "ohlcv_realistic_v2"])
def test_delayed_entry_protection_links_the_decision_that_requested_it(monkeypatch, version):
    result, _, _ = execute(
        monkeypatch,
        [QUIET, QUIET, (110, 111, 109, 110, 10)],
        entry=110,
        stop=100,
        target=130,
        entry_type="stop",
        costs={"version": version},
    )
    audit = result.metrics["execution_audit"]
    origin = audit["intents"][0]["decision_id"]
    protective = [o for o in audit["orders"] if o["role"] in {"stop_loss", "take_profit"}]
    assert len(protective) == 2
    assert all(o["decision_id"] == origin for o in protective)


def test_unresolved_decision_clock_cannot_claim_a_complete_audit():
    from koval_backtrader.backtest_runner import create_engine
    from tests.test_runtime_boundaries import MODEL, spec_for

    spec = spec_for(model=MODEL | {"version": "ohlcv_fixed_v1"})
    spec.runtime_contract = None
    spec.feeds = {"unresolved": spec.feeds["1m"]}
    audit = create_engine().run(spec).metrics["execution_audit"]
    assert audit["completeness"] == "partial"
    assert "decision_clock_unavailable" in audit["incomplete_reasons"]
