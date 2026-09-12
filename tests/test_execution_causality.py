# SPDX-License-Identifier: GPL-3.0-or-later
"""Future mutations and independent arithmetic over recorded execution deltas."""

import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from importlib.resources import files

import pytest
from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig
from koval.engine.funding import FundingRecord, build_funding_series

from koval_backtrader.backtest_runner import create_engine
from tests.test_evidence_transport import evidence_set, transport_spec
from tests.test_realistic_evidence import QUIET, START, STEP, execute
from tests.test_runtime_boundaries import spec_for, unsupported_runtime


@pytest.mark.parametrize("changed", ["candles", "funding", "mark_prices"])
def test_future_inputs_do_not_change_past_decisions_fills_or_cashflows(changed):
    spec = transport_spec(evidence_set(), json_transport=False)
    before = create_engine().run(spec)
    other = deepcopy(spec)
    if changed == "candles":
        other.feeds["1m"][-1, 1:5] = [100, 130, 70, 120]
        other.feeds["1m"][-1, 5] = 1000
    else:
        series = other.execution_config[changed]
        update = {"rate": Decimal("0.3")} if changed == "funding" else {"price": Decimal("50")}
        other.execution_config[changed] = replace(
            series, records=(*series.records[:-1], replace(series.records[-1], **update))
        )
    after = create_engine().run(other)
    cutoff = START + 6 * STEP
    for key, timestamp in (
        ("decisions", "bar_timestamp_ms"),
        ("fills", "timestamp_ms"),
        ("ledger", "timestamp_ms"),
        ("account_snapshots", "timestamp_ms"),
    ):
        a = [r for r in before.metrics["execution_audit"][key] if r[timestamp] < cutoff]
        b = [r for r in after.metrics["execution_audit"][key] if r[timestamp] < cutoff]
        assert a == b


def test_completed_bar_volume_is_disclosed_as_an_offline_model_input():
    result = create_engine().run(transport_spec(evidence_set()))
    for fill in result.metrics["execution_costs"]["fills"]:
        proxy = fill["evidence_refs"]["execution_proxy"]
        assert proxy["status"] == "approximated"
        assert proxy["volume_observed_at_ms"] == fill["timestamp_ms"] + STEP


@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize("rate", [Decimal("0.001"), Decimal("-0.001")])
def test_independent_decimal_oracle_for_partial_positions_and_funding(monkeypatch, direction, rate):
    rows = [QUIET] * 3 + [(100, 121, 79, 120, 10)]
    funding = build_funding_series(
        [
            FundingRecord("BTCUSDT", rate, START + i * STEP, Decimal("100"), STEP, "archive")
            for i in range(len(rows))
        ],
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=START,
        requested_end_ms=START + 3 * STEP,
    )
    result, _, snapshots = execute(
        monkeypatch,
        rows,
        direction=direction,
        evidence={
            "funding": funding,
            "execution_proxy": ExecutionProxyConfig(Decimal("0.1"), "carry", ExecutionLatency()),
        },
        costs={"commission_bps": 4, "spread_bps": 2, "slippage_bps": 3},
    )
    cash, quantity, average = Decimal("10000"), Decimal("0"), Decimal("0")
    sign = Decimal("1") if direction == "long" else Decimal("-1")
    fills = result.metrics["execution_costs"]["fills"]
    for i, row in enumerate(rows):
        timestamp = START + i * STEP
        cash -= sign * quantity * Decimal("100") * rate
        for fill in (f for f in fills if f["timestamp_ms"] == timestamp):
            size, price = Decimal(str(fill["size"])), Decimal(str(fill["fill_price"]))
            fee = size * price * Decimal("0.0004")
            assert float(fee) == pytest.approx(fill["commission"])
            cash -= fee
            if fill["role"] == "entry":
                average = (average * quantity + size * price) / (quantity + size)
                quantity += size
            else:
                cash += sign * size * (price - average)
                quantity -= size
        equity = cash + sign * quantity * (Decimal(str(row[3])) - average)
        assert snapshots[i].balance == pytest.approx(float(cash))
        assert snapshots[i].equity == pytest.approx(float(equity))
    assert result.metrics["final_capital"] == pytest.approx(float(equity))


def test_installed_engine_public_runtime_fixtures_have_no_waivers():
    spec = spec_for()
    if unsupported_runtime(spec):
        return
    directory = files("koval").joinpath("examples/runtime")
    fixtures = sorted(p for p in directory.iterdir() if p.name.endswith(".json"))
    assert fixtures
    for path in fixtures:
        fixture = json.loads(path.read_text())
        import numpy as np

        spec = spec_for(
            np.array(fixture["candles"], dtype=float),
            contract=fixture["runtime_contract"],
            model=fixture["execution"]
            | {"version": fixture["execution"]["version"].removeprefix("paper_")},
        )
        spec.graph = fixture["graph"]
        result = create_engine().run(spec)
        account = result.metrics["execution_audit"]["terminal_account"]
        for key, value in fixture["expected_metrics"].items():
            assert account[key] == pytest.approx(value), (path.name, key)


def test_session_start_does_not_take_its_timestamp_from_preloaded_future_data():
    spec = transport_spec(evidence_set())
    events = []
    create_engine().run(spec, events.append)
    assert events[0]["timestamp_ms"] <= spec.feeds["1m"][0, 0]


def test_fill_events_include_the_persisted_order_and_fill_ids(monkeypatch):
    result, events, _ = execute(monkeypatch, [QUIET] * 3)
    event = next(e for e in events if e["event_type"] == "ORDER_FILLED")
    fill = result.metrics["execution_costs"]["fills"][0]
    assert event["payload"].get("order_id") == fill["order_id"]
    assert event["payload"]["fill_ids"] == [fill["fill_id"]]


def test_primary_decisions_continue_before_the_first_secondary_bar(monkeypatch):
    import numpy as np
    from koval.strategy.base.declarative import DeclarativeStrategy

    from koval_backtrader import backtest_runner

    seen = []

    class Probe(DeclarativeStrategy):
        def on_bar(self):
            seen.append((self.timestamp_ms, self.htf_closes))

        def should_long(self):
            return False

    monkeypatch.setattr(backtest_runner, "assemble_from_graph", lambda _: Probe())
    spec = transport_spec({})
    spec.feeds["5m"] = np.array([[START + 5 * STEP, *QUIET]])
    result = create_engine().run(spec)
    assert [timestamp for timestamp, _ in seen] == spec.feeds["1m"][:, 0].tolist()
    assert all(htf is None for _, htf in seen)
    assert len(result.metrics["execution_audit"]["decisions"]) == len(seen)
