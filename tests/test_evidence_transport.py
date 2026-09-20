# SPDX-License-Identifier: GPL-3.0-or-later
"""Public EngineRunSpec through JSON request and worker pickle transport."""

import builtins
import json
import pickle
import runpy
from pathlib import Path

import numpy as np
import pytest
from koval.engine.backtest_engine import EngineRunSpec, ProtocolVersionError
from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig
from koval.engine.fee_evidence import FeeScheduleEvidence
from koval.engine.funding import FundingRecord, build_funding_series
from koval.engine.instrument_risk import MarkPriceRecord, build_mark_price_series

from koval_backtrader.backtest_runner import create_engine
from koval_backtrader.execution_evidence import evidence_json
from tests.test_realistic_evidence import START, STEP, D, instrument
from tests.test_runtime_boundaries import MARKET, MODEL, candles, graph

ROOT = Path(__file__).resolve().parents[1]


def evidence_set():
    return {
        "fee_schedule": FeeScheduleEvidence(
            "fees", 1, 4, "USDT", "historical", "archive", START, START + 6 * STEP
        ),
        "funding": build_funding_series(
            [
                FundingRecord("BTCUSDT", D("0.001"), START + i * STEP, D("100"), STEP, "archive")
                for i in range(7)
            ],
            exchange="binance",
            market="future",
            symbol="BTCUSDT",
            requested_start_ms=START,
            requested_end_ms=START + 6 * STEP,
        ),
        "instrument_specs": (instrument(),),
        "mark_prices": build_mark_price_series(
            [MarkPriceRecord(START + i * STEP, D("100"), "archive") for i in range(7)],
            exchange="binance",
            symbol="BTCUSDT",
            interval_ms=STEP,
            requested_start_ms=START,
            requested_end_ms=START + 6 * STEP,
        ),
        "execution_proxy": ExecutionProxyConfig(D("0.1"), "carry", ExecutionLatency()),
    }


def test_new_decoder_fallback_does_not_hide_an_internal_missing_dependency(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "koval.engine.execution_evidence":
            raise ModuleNotFoundError(
                "simulated missing internal dependency", name="internal_dependency"
            )
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(ModuleNotFoundError, match="missing internal dependency"):
        runpy.run_path(
            ROOT / "src" / "koval_backtrader" / "execution_evidence.py",
            run_name="_koval_backtrader_import_probe",
        )


def transport_spec(evidence, *, json_transport=True):
    request = {
        "graph": graph(),
        "feeds": {"1m": candles().tolist()},
        "initial_capital": 10_000,
        "execution_config": {
            "exchange": "binance",
            "exchange_type": "future",
            "market": MARKET,
            "execution_model": MODEL,
            **(evidence_json_mapping(evidence) if json_transport else evidence),
        },
        "execution_contract_version": 2,
        "required_execution_capabilities": tuple(evidence),
    }
    if json_transport:
        request = json.loads(json.dumps(request))
    request["feeds"] = {tf: np.array(rows) for tf, rows in request["feeds"].items()}
    request["required_execution_capabilities"] = tuple(request["required_execution_capabilities"])
    return pickle.loads(pickle.dumps(EngineRunSpec(**request)))


def evidence_json_mapping(evidence):
    return {key: evidence_json(value) for key, value in evidence.items()}


def test_every_evidence_path_round_trips_to_identical_fills_and_ledger():
    evidence = evidence_set()
    typed = create_engine().run(transport_spec(evidence, json_transport=False))
    replay = create_engine().run(transport_spec(evidence))
    assert typed.metrics == replay.metrics
    assert typed.trades == replay.trades
    assert set(replay.metrics["execution_model"]["negotiated_capabilities"]["features"]) == set(
        evidence
    )
    fill = replay.metrics["execution_costs"]["fills"][0]
    assert fill.get("evidence_refs"), "each fill must identify the evidence actually applied"
    assert fill["evidence_refs"]["fee_schedule"]["evidence_id"] == "fees"
    assert fill["evidence_refs"]["instrument_specs"]["evidence_id"] == "instrument-1"
    assert fill["evidence_refs"]["execution_proxy"]["sha256"]
    assert fill["cost_quality"] == {
        "commission": "approximated",
        "spread": "configured",
        "slippage": "configured",
    }


@pytest.mark.parametrize(
    "field,value", [("exchange", "whitebit"), ("market", "spot"), ("canonical_symbol", "ETHUSDT")]
)
def test_fee_market_context_is_not_lost_in_transport(field, value):
    evidence = evidence_json_mapping(evidence_set())
    evidence["fee_schedule"].update(exchange="binance", market="future", canonical_symbol="BTCUSDT")
    evidence["fee_schedule"][field] = value
    with pytest.raises(ValueError, match="fee|evidence"):
        create_engine().run(transport_spec(evidence))


@pytest.mark.parametrize("typed", [False, True])
def test_typed_and_json_fees_both_refuse_boolean_rates(typed):
    fee = FeeScheduleEvidence("bad-fee", True, 4, "USDT", "approximation", "configured")
    with pytest.raises(ValueError, match="number"):
        create_engine().run(transport_spec({"fee_schedule": fee}, json_transport=not typed))


@pytest.mark.parametrize("kind", ["fee_schedule", "funding", "instrument_specs", "mark_prices"])
def test_out_of_coverage_evidence_fails_before_any_decision_even_when_flat(kind):
    evidence = evidence_json_mapping(evidence_set())
    if kind == "fee_schedule":
        evidence[kind]["effective_to_ms"] = START
    elif kind == "instrument_specs":
        evidence[kind][0]["effective_to_ms"] = START
    else:
        evidence[kind]["requested_end_ms"] = START
        evidence[kind]["records"] = evidence[kind]["records"][:1]
    spec = transport_spec(evidence)
    spec.graph = {
        "blocks": [{"id": "clock", "type": "fact.every_bar", "params": {}}],
        "connections": [],
    }
    events = []
    with pytest.raises(ValueError, match="cover|evidence|effective"):
        create_engine().run(spec, events.append)
    assert events == []


def test_unsupported_capability_survives_worker_transport():
    spec = transport_spec(evidence_set())
    spec.required_execution_capabilities += ("order_book_queue",)
    with pytest.raises(ProtocolVersionError, match="order_book_queue"):
        create_engine().run(pickle.loads(pickle.dumps(spec)))


def test_decimal_transport_errors_are_value_errors():
    evidence = evidence_json_mapping(evidence_set())
    evidence["instrument_specs"][0]["step_size"] = "invalid-decimal"
    with pytest.raises(ValueError, match="decimal|Decimal"):
        create_engine().run(transport_spec(evidence))


def test_realistic_result_declares_unmeasured_accuracy_and_conditional_effects():
    result = create_engine().run(transport_spec(evidence_set()))
    execution_model = result.metrics["execution_model"]
    if "realism_report" not in execution_model:
        pytest.skip("realism report is unavailable on the older engine compatibility matrix")
    report = execution_model["realism_report"]
    assert report["accuracy"] == "unmeasured"
    assert report["maximum_error_pct"] is None
    assert report["effects"]["funding"] == "supplied_evidence"
    assert report["effects"]["liquidation"] == "sampled_mark_model"
    assert report["effects"]["partial_fills"] == "ohlcv_proxy"
