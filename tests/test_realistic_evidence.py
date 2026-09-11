# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent paper/Backtrader runs over the same normalized evidence."""

from dataclasses import replace
from decimal import Decimal as D

import numpy as np
import pytest
from koval.engine.backtest_engine import EngineRunSpec
from koval.engine.execution_conformance import compare_execution_results
from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig
from koval.engine.fee_evidence import FeeScheduleEvidence
from koval.engine.funding import FundingRecord, build_funding_series
from koval.engine.instrument_risk import (
    InstrumentSpecEvidence,
    MaintenanceMarginTier,
    MarkPriceRecord,
    build_mark_price_series,
)
from koval.engine.paper_broker import PaperBroker
from koval.engine.paper_profile import resolve_paper_profile
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup

from koval_backtrader import backtest_runner

START = 1_704_067_200_000
STEP = 60_000
QUIET = (100.0, 101.0, 99.0, 100.0, 10.0)


def instrument(**overrides):
    return InstrumentSpecEvidence(
        **(
            dict(
                evidence_id="instrument-1",
                exchange="binance",
                market="future",
                canonical_symbol="BTCUSDT",
                effective_from_ms=START,
                effective_to_ms=START + 20 * STEP,
                tick_size=D("0.1"),
                step_size=D("0.1"),
                minimum_quantity=D("0.1"),
                minimum_notional=D("1"),
                minimum_price=D("1"),
                maximum_price=D("10000"),
                contract_size=D("1"),
                collateral_currency="USDT",
                margin_tiers=(MaintenanceMarginTier(D("0"), None, D("0.05")),),
                liquidation_fee_bps=D("10"),
                source="archived-test",
                evidence_status="historical",
            )
            | overrides
        )
    )


def execute(
    monkeypatch,
    rows,
    *,
    evidence=None,
    direction="long",
    size=2.0,
    entry=100.0,
    stop=None,
    target=None,
    entry_type="market",
    costs=None,
    capital=10000.0,
    stop_updates=None,
    target_updates=None,
):
    evidence = evidence or {}
    stop = stop if stop is not None else (90.0 if direction == "long" else 110.0)
    target = target if target is not None else (120.0 if direction == "long" else 80.0)
    model = dict(
        version="ohlcv_realistic_v2",
        commission_bps=0.0,
        spread_bps=0.0,
        slippage_bps=0.0,
        leverage=1.0,
    ) | (costs or {})
    snapshots = []

    class Probe(DeclarativeStrategy):
        def on_bar(self):
            snapshots.append(self.account)

        def should_long(self):
            return direction == "long" and self.bar_index == 1

        def should_short(self):
            return direction == "short" and self.bar_index == 1

        def go_long(self):
            return TradeSetup(
                direction=direction,
                entry_type=entry_type,
                entry_price=entry,
                stop_loss=stop,
                take_profit=target,
                size=size,
            )

        go_short = go_long

        def on_sl_update(self, trade_id):
            return (stop_updates or {}).get(self.bar_index)

        def on_tp_update(self, trade_id):
            return (target_updates or {}).get(self.bar_index)

    monkeypatch.setattr(backtest_runner, "assemble_from_graph", lambda _: Probe())
    config = dict(
        exchange="binance",
        exchange_type="future",
        execution_model=model,
        market=dict(
            exchange="binance",
            market="future",
            canonical_symbol="BTCUSDT",
            contract_type="perpetual",
        ),
        **evidence,
    )
    events = []
    candles = np.array([[START + index * STEP, *row] for index, row in enumerate(rows)])
    result = backtest_runner.create_engine().run(
        EngineRunSpec(
            graph={},
            feeds={"1m": candles},
            initial_capital=capital,
            execution_config=config,
        ),
        on_event=events.append,
    )
    return result, events, snapshots


def assert_paper_parity(monkeypatch, rows, **kwargs):
    result, events, snapshots = execute(monkeypatch, rows, **kwargs)
    model = result.metrics["execution_model"]["resolved_config"]["execution_model"]
    broker = PaperBroker(
        kwargs.get("capital", 10000.0),
        profile=resolve_paper_profile(model | {"version": "paper_ohlcv_realistic_v2"}),
        **kwargs.get("evidence", {}),
    )
    side = "buy" if kwargs.get("direction", "long") == "long" else "sell"
    stop = kwargs.get("stop", 90.0 if side == "buy" else 110.0)
    target = kwargs.get("target", 120.0 if side == "buy" else 80.0)
    entry, size = kwargs.get("entry", 100.0), kwargs.get("size", 2.0)
    paper_fills = []
    for index, (open_, high, low, close, volume) in enumerate(rows):
        fills = broker.process_bar(
            ts_ms=START + index * STEP, open=open_, high=high, low=low, close=close, volume=volume
        )
        paper_fills.extend(fills)
        assert snapshots[index].equity == pytest.approx(broker.equity)
        assert snapshots[index].balance == pytest.approx(broker.balance)
        if index == 0:
            broker.submit_bracket(
                side=side,
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                quantity=size,
                order_type=kwargs.get("entry_type", "market"),
                symbol="BTCUSDT",
                risk_budget=abs(entry - stop) * size,
                decision_timestamp_ms=START,
            )
        elif broker.position is not None:
            sl = kwargs.get("stop_updates", {}).get(index + 1)
            tp = kwargs.get("target_updates", {}).get(index + 1)
            if sl is not None or tp is not None:
                broker.modify_protection(stop_price=sl, target_price=tp)
    expected = [
        dict(
            role=f.kind,
            price=f.price,
            quantity=f.quantity,
            commission=f.commission,
            reference=f.reference_price,
            spread=f.spread_cost,
            slippage=f.slippage_cost,
        )
        for f in paper_fills
    ]
    observed = [
        dict(
            role=f["koval_role"],
            price=f["fill_price"],
            quantity=f["size"],
            commission=f["commission"],
            reference=f["reference_price"],
            spread=f["spread_cost"],
            slippage=f["slippage_cost"],
        )
        for f in result.metrics["execution_costs"]["fills"]
    ]
    compare_execution_results(expected, observed)
    assert result.metrics["final_capital"] == pytest.approx(broker.equity)
    return result, events, snapshots


@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize("rate", [D("0.001"), D("-0.001")])
def test_funding_is_a_separate_signed_cashflow_before_orders(monkeypatch, direction, rate):
    records = [
        FundingRecord("BTCUSDT", rate, START + index * STEP, D("100"), STEP, "archive")
        for index in range(4)
    ]
    funding = build_funding_series(
        records,
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=START,
        requested_end_ms=START + 3 * STEP,
    )
    result, events, snapshots = assert_paper_parity(
        monkeypatch, [QUIET] * 4, direction=direction, evidence={"funding": funding}
    )
    expected = (-1 if direction == "long" else 1) * 2 * 100 * float(rate) * 2
    assert result.metrics["execution_costs"]["funding_cashflow"] == pytest.approx(expected)
    assert snapshots[-1].funding == pytest.approx(expected)
    assert len(result.metrics["execution_costs"]["funding_entries"]) == 2


def test_funding_coverage_exhaustion_fails_even_when_flat(monkeypatch):
    funding = build_funding_series(
        [],
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=START,
        requested_end_ms=START,
        interval_ms=8 * 3600000,
    )
    with pytest.raises(ValueError, match="funding.*cover"):
        execute(
            monkeypatch,
            [QUIET] * 2,
            evidence={"funding": funding},
            entry_type="limit",
            entry=50,
            stop=40,
        )


@pytest.mark.parametrize("entry_type", ["market", "limit", "stop"])
def test_fee_evidence_is_used_on_each_fill(monkeypatch, entry_type):
    fee = FeeScheduleEvidence(
        "fee-1", 1.0, 6.0, "USDT", "historical", "archive", START, START + 4 * STEP
    )
    result, _, _ = assert_paper_parity(
        monkeypatch,
        [QUIET, QUIET, (100, 121, 99, 120, 10)],
        evidence={"fee_schedule": fee},
        entry_type=entry_type,
    )
    fills = result.metrics["execution_costs"]["fills"]
    assert all(fill["fee_evidence_id"] == "fee-1" for fill in fills)
    assert fills[0]["liquidity_role"] == ("maker" if entry_type == "limit" else "taker")
    assert fills[-1]["liquidity_role"] == "taker"


def test_instrument_normalizes_quantity_and_protective_prices(monkeypatch):
    result, _, _ = assert_paper_parity(
        monkeypatch,
        [QUIET, QUIET, (100, 121, 99, 120, 10)],
        evidence={"instrument_specs": (instrument(),)},
        size=2.07,
        stop=90.07,
        target=120.07,
    )
    assert result.metrics["execution_costs"]["fills"][0]["size"] == 2.0
    assert result.trades[0]["exit_price"] == 120.0


def test_mark_price_liquidation_precedes_protection(monkeypatch):
    marks = build_mark_price_series(
        [
            MarkPriceRecord(START + i * STEP, D(str(price)), "archive")
            for i, price in enumerate([100, 100, 10])
        ],
        exchange="binance",
        symbol="BTCUSDT",
        interval_ms=STEP,
        requested_start_ms=START,
        requested_end_ms=START + 2 * STEP,
    )
    result, _, _ = assert_paper_parity(
        monkeypatch,
        [QUIET, QUIET, (100, 101, 99, 100, 10)],
        evidence={"instrument_specs": (instrument(),), "mark_prices": marks},
        capital=100.0,
        size=5.0,
        costs={"leverage": 10.0},
    )
    assert result.trades[0]["exit_reason"] == "Liquidation"
    assert result.metrics["execution_costs"]["liquidation_fee"] > 0


@pytest.mark.parametrize("policy", ["carry", "cancel"])
def test_partial_entries_and_exits_share_the_bar_budget(monkeypatch, policy):
    proxy = ExecutionProxyConfig(D("0.1"), policy, ExecutionLatency())
    rows = [QUIET, QUIET, QUIET, (100, 121, 99, 120, 10), (100, 121, 99, 120, 10)]
    result, _, _ = assert_paper_parity(monkeypatch, rows, evidence={"execution_proxy": proxy})
    fills = result.metrics["execution_costs"]["fills"]
    assert all(f["size"] <= 1 for f in fills)
    assert len(result.trades) == 1
    assert result.trades[0]["size"] == (2.0 if policy == "carry" else 1.0)


def test_partial_entry_is_protected_before_its_remainder(monkeypatch):
    proxy = ExecutionProxyConfig(D("0.1"), "carry", ExecutionLatency())
    result, _, _ = assert_paper_parity(
        monkeypatch,
        [QUIET, QUIET, (100, 121, 89, 100, 10), QUIET],
        evidence={"execution_proxy": proxy},
    )
    assert [f["role"] for f in result.metrics["execution_costs"]["fills"]] == ["entry", "exit"]


def test_submission_latency_delays_fill_until_eligible_bar(monkeypatch):
    proxy = ExecutionProxyConfig(
        D("1"), "carry", ExecutionLatency(decision_to_submission_ms=2 * STEP)
    )
    result, _, _ = assert_paper_parity(
        monkeypatch, [QUIET] * 4, evidence={"execution_proxy": proxy}
    )
    assert result.metrics["execution_costs"]["fills"][0]["timestamp_ms"] == START + 2 * STEP


@pytest.mark.parametrize(
    "evidence,reason",
    [
        ({"instrument_specs": (instrument(contract_size=D("10")),)}, "contract_size"),
        (
            {"fee_schedule": FeeScheduleEvidence("fee", 1, 2, "BNB", "current_snapshot", "api")},
            "currency",
        ),
        (
            {
                "execution_proxy": ExecutionProxyConfig(
                    D("1"), "carry", ExecutionLatency(cancellation_ms=1)
                )
            },
            "cancellation.*replacement",
        ),
        ({"instrument_specs": (replace(instrument(), exchange="whitebit"),)}, "exchange"),
    ],
)
def test_unsupported_or_mismatched_evidence_is_refused(monkeypatch, evidence, reason):
    with pytest.raises(ValueError, match=reason):
        execute(monkeypatch, [QUIET] * 2, evidence=evidence)


def test_partial_fill_order_ids_are_unique_and_repeatable(monkeypatch):
    proxy = ExecutionProxyConfig(D("0.1"), "carry", ExecutionLatency())
    rows = [QUIET] * 3 + [(100, 121, 99, 120, 10)] * 2
    first = execute(monkeypatch, rows, evidence={"execution_proxy": proxy})[0]
    second = execute(monkeypatch, rows, evidence={"execution_proxy": proxy})[0]
    ids = [f["order_id"] for f in first.metrics["execution_costs"]["fills"]]
    assert ids[0] == ids[1]
    assert ids[2] == ids[3]
    assert ids[0] != ids[2]
    assert ids == [f["order_id"] for f in second.metrics["execution_costs"]["fills"]]


def test_replacement_normalizes_instrument_ticks(monkeypatch):
    result, _, _ = assert_paper_parity(
        monkeypatch,
        [QUIET, QUIET, (100, 106, 99, 105, 10)],
        evidence={"instrument_specs": (instrument(),)},
        stop_updates={2: 95.07},
        target_updates={2: 105.07},
    )
    assert result.trades[0]["exit_price"] == 105.0
    assert all(
        f["instrument_evidence_id"] == "instrument-1"
        for f in result.metrics["execution_costs"]["fills"]
    )


def test_risk_capped_entries_remain_on_the_venue_quantity_step(monkeypatch):
    result, _, _ = assert_paper_parity(
        monkeypatch,
        [QUIET] * 3,
        evidence={"instrument_specs": (instrument(),)},
        costs={"commission_bps": 4, "slippage_bps": 10},
    )
    fill = result.metrics["execution_costs"]["fills"][0]
    assert D(str(fill["size"])) % D("0.1") == 0
    assert fill["size"] <= 2


def test_lagged_impact_is_disclosed_per_fill(monkeypatch):
    from koval.engine.execution_proxy import ImpactCalibrationEvidence

    calibration = ImpactCalibrationEvidence("impact-1", START - 1, D("20"), D("0.5"), "archive")
    proxy = ExecutionProxyConfig(D("0.1"), "carry", ExecutionLatency(), calibration)
    result, _, _ = assert_paper_parity(
        monkeypatch,
        [QUIET] * 3 + [(100, 121, 99, 120, 10)] * 3,
        evidence={"execution_proxy": proxy},
    )
    assert all(
        f["impact_evidence_id"] == "impact-1" for f in result.metrics["execution_costs"]["fills"]
    )


def test_non_lagged_impact_is_refused(monkeypatch):
    from koval.engine.execution_proxy import ImpactCalibrationEvidence

    proxy = ExecutionProxyConfig(
        D("1"),
        "carry",
        ExecutionLatency(),
        ImpactCalibrationEvidence("future", START, D("20"), D("1"), "archive"),
    )
    with pytest.raises(ValueError, match="strictly before"):
        execute(monkeypatch, [QUIET] * 2, evidence={"execution_proxy": proxy})


def test_resolved_evidence_can_be_replayed_as_json(monkeypatch):
    import json

    evidence = {
        "instrument_specs": (instrument(),),
        "execution_proxy": ExecutionProxyConfig(D("0.1"), "carry", ExecutionLatency()),
    }
    first = execute(monkeypatch, [QUIET] * 3, evidence=evidence)[0]
    config = json.loads(json.dumps(first.metrics["execution_model"]["resolved_config"]))
    replay = execute(monkeypatch, [QUIET] * 3, evidence={key: config[key] for key in evidence})[0]
    assert first.metrics["execution_costs"] == replay.metrics["execution_costs"]
    assert first.metrics["run_identity"] == replay.metrics["run_identity"]


def test_ambiguous_bar_reports_local_outcome_sensitivity(monkeypatch):
    result, _, _ = execute(monkeypatch, [QUIET, (100, 121, 89, 100, 10)])
    ambiguity = result.metrics["execution_costs"]["ambiguities"][0]
    assert ambiguity["sensitivity"]["stop_reference_pnl"] == -20
    assert ambiguity["sensitivity"]["target_reference_pnl"] == 40
    assert ambiguity["sensitivity"]["scope"] == "local_full_position_before_costs"


def test_favorable_tick_rounding_reconciles_as_price_improvement(monkeypatch):
    rows = [QUIET, (99.97, 100.1, 99.8, 100.0, 10.0)]
    result, _, _ = execute(
        monkeypatch, rows, entry_type="limit", evidence={"instrument_specs": (instrument(),)}
    )
    fill = result.metrics["execution_costs"]["fills"][0]
    assert fill["fill_price"] == 99.9
    assert fill["price_adjustment_cost"] < 0
    assert result.metrics["execution_costs"]["reconciliation_error"] == pytest.approx(0, abs=1e-8)


def test_evidence_metadata_and_research_report_modelled_funding(monkeypatch):
    records = [
        FundingRecord("BTCUSDT", D("0.001"), START + i * STEP, D("100"), STEP, "archive")
        for i in range(3)
    ]
    funding = build_funding_series(
        records,
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=START,
        requested_end_ms=START + 2 * STEP,
    )
    result, _, _ = execute(monkeypatch, [QUIET] * 3, evidence={"funding": funding})
    assert (
        result.metrics["execution_model"]["assumptions"]["funding"]
        == "archived_settlement_cashflows"
    )
    assert "funding" not in result.metrics["execution_model"]["unmodelled_effects"]
    assert result.metrics["research"]["costs"]["funding"] == pytest.approx(-0.2)
    assert result.metrics["research"]["costs"]["funding_status"] == "modelled"


def test_missing_mark_coverage_is_refused_even_when_flat(monkeypatch):
    marks = build_mark_price_series(
        [MarkPriceRecord(START, D("100"), "archive")],
        exchange="binance",
        symbol="BTCUSDT",
        interval_ms=STEP,
        requested_start_ms=START,
        requested_end_ms=START,
    )
    with pytest.raises(ValueError, match="mark.*cover"):
        execute(
            monkeypatch,
            [QUIET] * 3,
            evidence={"mark_prices": marks, "instrument_specs": (instrument(),)},
            entry_type="limit",
            entry=50,
            stop=40,
        )


def test_evidence_fee_overrides_configured_fee_for_affordability(monkeypatch):
    fee = FeeScheduleEvidence(
        "free", 0.0, 0.0, "quote", "historical", "archive", START, START + 3 * STEP
    )
    result, _, _ = assert_paper_parity(
        monkeypatch,
        [QUIET] * 2,
        size=2.0,
        capital=200.0,
        costs={"commission_bps": 4.0},
        evidence={"fee_schedule": fee},
    )
    assert len(result.metrics["execution_costs"]["fills"]) == 1


def test_risk_clipping_consumes_only_actual_liquidity(monkeypatch):
    proxy = ExecutionProxyConfig(D("0.2"), "carry", ExecutionLatency())
    result, _, _ = assert_paper_parity(
        monkeypatch,
        [QUIET, (100, 101, 89, 100, 10), (100, 101, 89, 100, 10)],
        evidence={"execution_proxy": proxy},
        costs={"commission_bps": 4.0},
    )
    fills = result.metrics["execution_costs"]["fills"]
    first_bar = [fill for fill in fills if fill["timestamp_ms"] == START + STEP]
    assert [fill["role"] for fill in first_bar] == ["entry", "exit"]
    assert sum(fill["size"] for fill in first_bar) == pytest.approx(2.0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"entry_type": "invalid"},
        {"size": -1.0},
        {"stop": 110.0},
        {"target": float("nan")},
    ],
)
def test_invalid_v2_setups_are_refused(monkeypatch, overrides):
    with pytest.raises(ValueError, match="invalid.*order"):
        execute(monkeypatch, [QUIET] * 2, **overrides)


def test_liquidation_event_and_session_ledger_reconcile(monkeypatch):
    marks = build_mark_price_series(
        [
            MarkPriceRecord(START + i * STEP, D(str(p)), "archive")
            for i, p in enumerate([100, 100, 10])
        ],
        exchange="binance",
        symbol="BTCUSDT",
        interval_ms=STEP,
        requested_start_ms=START,
        requested_end_ms=START + 2 * STEP,
    )
    result, events, _ = execute(
        monkeypatch,
        [QUIET] * 3,
        evidence={"instrument_specs": (instrument(),), "mark_prices": marks},
        capital=100.0,
        size=5.0,
        costs={"leverage": 10.0},
    )
    closed = next(e["payload"] for e in events if e["event_type"] == "TRADE_CLOSED")
    assert closed["pnl_comm"] == pytest.approx(result.trades[0]["realized_pnl"])
    assert closed["liquidation_fee"] == pytest.approx(
        result.metrics["execution_costs"]["liquidation_fee"]
    )
    end = events[-1]["payload"]
    assert end["account"]["balance"] == pytest.approx(result.metrics["final_capital"])
    assert 100.0 + sum(entry["amount"] for entry in end["account_ledger"]) == pytest.approx(
        end["account"]["balance"]
    )


def test_fractional_partial_exits_do_not_leave_rounding_dust(monkeypatch):
    proxy = ExecutionProxyConfig(D("0.07"), "carry", ExecutionLatency())
    result, _, _ = execute(
        monkeypatch,
        [QUIET] * 4 + [(100, 121, 99, 120, 10)] * 5,
        evidence={"instrument_specs": (instrument(),), "execution_proxy": proxy},
        size=2.1,
    )
    assert len(result.trades) == 1
    assert result.metrics["research"]["final_open_position"] is None
    assert sum(
        f["size"] for f in result.metrics["execution_costs"]["fills"] if f["role"] == "exit"
    ) == pytest.approx(2.1)


def test_non_quote_collateral_is_refused(monkeypatch):
    with pytest.raises(ValueError, match="collateral"):
        execute(
            monkeypatch,
            [QUIET] * 2,
            evidence={"instrument_specs": (instrument(collateral_currency="BTC"),)},
        )
