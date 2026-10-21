# SPDX-License-Identifier: GPL-3.0-or-later
"""An available opening print precedes unknown intrabar protective touches."""

from decimal import Decimal

import pytest
from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig

from tests.test_paper_parity import Scenario, compare, run_paper, run_plugin
from tests.test_position_exit import run_exit_probe, unsupported_position_features
from tests.test_realistic_evidence import QUIET, STEP, assert_paper_parity, execute

PROFILES = ["ohlcv_fixed_v1", "ohlcv_realistic_v2"]


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize(
    "direction,opening", [("long", 120), ("long", 125), ("short", 80), ("short", 75)]
)
@pytest.mark.parametrize("costs", [False, True])
def test_working_target_at_open_wins_over_later_stop(
    monkeypatch, profile, direction, opening, costs
):
    result, _, seen = execute(
        monkeypatch,
        [QUIET, QUIET, (opening, 130, 70, 100, 100), QUIET],
        direction=direction,
        size=1,
        costs=dict(
            version=profile,
            commission_bps=4 if costs else 0,
            spread_bps=20 if costs else 0,
            slippage_bps=10 if costs else 0,
        ),
    )
    entry, exit_fill = result.metrics["execution_costs"]["fills"]
    sign = 1 if direction == "long" else -1
    expected_price = opening * (1 - sign * 0.002) if costs else opening
    assert exit_fill["koval_role"] == "take_profit"
    assert exit_fill["reference_price"] == opening
    assert exit_fill["fill_price"] == pytest.approx(expected_price)
    assert exit_fill["size"] == entry["size"]
    assert exit_fill["commission"] == pytest.approx(
        expected_price * entry["size"] * (0.0004 if costs else 0)
    )
    capital = (
        10000
        + sign * entry["size"] * (expected_price - entry["fill_price"])
        - entry["commission"]
        - exit_fill["commission"]
    )
    assert result.metrics["final_capital"] == pytest.approx(capital)
    assert result.trades[0]["exit_reason"] == "Take Profit"
    assert seen[-1].position is None
    assert seen[-1].free_margin == pytest.approx(capital)
    assert result.metrics["execution_costs"]["reconciliation_error"] == pytest.approx(0, abs=1e-8)
    orders = result.metrics["execution_audit"]["orders"]
    assert next(o for o in orders if o["role"] == "stop_loss")["status"] == "canceled"
    if profile == "ohlcv_realistic_v2":
        assert result.metrics["execution_costs"]["ambiguities"] == []
        assert exit_fill["protection_context"]["rule"] == "target_gap_at_open"


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize(
    "direction,opening,reference",
    [
        ("long", 100, 90),
        ("short", 100, 110),
        ("long", 90, 90),
        ("long", 85, 85),
        ("short", 110, 110),
        ("short", 115, 115),
    ],
)
def test_stop_retains_opening_and_ambiguous_intrabar_priority(
    monkeypatch, profile, direction, opening, reference
):
    result, _, _ = execute(
        monkeypatch,
        [QUIET, QUIET, (opening, 130, 70, 100, 100)],
        direction=direction,
        size=1,
        costs=dict(version=profile),
    )
    entry, exit_fill = result.metrics["execution_costs"]["fills"]
    assert exit_fill["koval_role"] == "stop_loss"
    assert exit_fill["reference_price"] == reference
    assert exit_fill["size"] == entry["size"]


@pytest.mark.parametrize(
    "direction,row,target",
    [
        ("long", (125, 130, 99, 110, 100), 120),
        ("short", (75, 101, 70, 90, 100), 80),
    ],
)
@pytest.mark.parametrize("partial", [False, True])
def test_intrabar_limit_entry_cannot_reuse_earlier_open(
    monkeypatch, direction, row, target, partial
):
    # Limit entries keep their price cap with costs. A 1.5-unit bar budget
    # permits the one-unit entry and only half of its target exit.
    evidence = (
        {"execution_proxy": ExecutionProxyConfig(Decimal(".015"), "carry", ExecutionLatency())}
        if partial
        else {}
    )
    result, _, seen = execute(
        monkeypatch,
        [QUIET, row],
        direction=direction,
        entry_type="limit",
        size=1,
        evidence=evidence,
    )
    entry, exit_fill = result.metrics["execution_costs"]["fills"]
    assert entry["reference_price"] == 100
    assert exit_fill["koval_role"] == "take_profit"
    assert exit_fill["reference_price"] == target
    assert exit_fill["fill_price"] == target
    assert exit_fill["protection_context"]["rule"] == "target_touched"
    assert exit_fill["size"] == (0.5 if partial else 1)
    if partial:
        assert seen[-1].position.quantity == 0.5
        assert entry["size"] + exit_fill["size"] == 1.5
    else:
        assert seen[-1].position is None
        assert result.metrics["final_capital"] == 10020


@pytest.mark.parametrize("direction,opening", [("long", 125), ("short", 75)])
def test_partial_opening_target_resizes_oco_and_consumes_shared_budget(
    monkeypatch, direction, opening
):
    result, _, seen = assert_paper_parity(
        monkeypatch,
        [QUIET, QUIET, (opening, 130, 70, 100, 4), (100, 115, 85, 100, 6), QUIET],
        direction=direction,
        size=1,
        evidence={
            "execution_proxy": ExecutionProxyConfig(Decimal(".1"), "carry", ExecutionLatency())
        },
    )
    entry, target, stop = result.metrics["execution_costs"]["fills"]
    assert (target["koval_role"], target["reference_price"], target["size"]) == (
        "take_profit",
        opening,
        0.4,
    )
    assert stop["koval_role"] == "stop_loss"
    assert stop["size"] == pytest.approx(0.6)
    assert seen[2].position.quantity == pytest.approx(0.6)
    assert seen[-1].position is None
    orders = result.metrics["execution_audit"]["orders"]
    assert next(o for o in orders if o["role"] == "take_profit")["status"] == "canceled"
    assert entry["size"] == pytest.approx(target["size"] + stop["size"])


@pytest.mark.parametrize("partial", [False, True])
def test_signal_request_does_not_change_working_opening_target(monkeypatch, partial):
    if unsupported_position_features():
        return
    evidence = (
        {"execution_proxy": ExecutionProxyConfig(Decimal(".1"), "carry", ExecutionLatency())}
        if partial
        else {}
    )
    rows = [QUIET, QUIET, (125, 130, 80, 100, 4), (125, 130, 80, 100, 6), QUIET]
    observed = []
    for signal_bars in [(), (2,)]:
        result, _, _, _ = run_exit_probe(
            monkeypatch, rows, stop=90, signal_bars=signal_bars, evidence=evidence
        )
        fills = result.metrics["execution_costs"]["fills"]
        observed.append(
            [(f["koval_role"], f["reference_price"], f["size"], f["commission"]) for f in fills]
        )
        assert all(f["koval_role"] == "take_profit" for f in fills[1:])
        assert result.metrics["final_capital"] == 10025
        assert result.metrics["execution_audit"]["terminal_account"]["open_positions"] == 0
        if signal_bars:
            signal = next(
                o for o in result.metrics["execution_audit"]["orders"] if o["role"] == "signal"
            )
            assert signal["status"] == "canceled"
    assert observed[0] == observed[1]


@pytest.mark.parametrize(
    "profile,expected_count", [("ohlcv_fixed_v1", 1), ("ohlcv_realistic_v2", 2)]
)
def test_entry_bar_protection_keeps_profile_activation_timing(monkeypatch, profile, expected_count):
    result, _, _ = execute(
        monkeypatch, [QUIET, (100, 130, 80, 100, 100)], size=1, costs=dict(version=profile)
    )
    fills = result.metrics["execution_costs"]["fills"]
    assert len(fills) == expected_count
    if expected_count == 2:
        assert fills[-1]["koval_role"] == "stop_loss"


def test_protection_activation_delay_survives_opening_priority(monkeypatch):
    result, _, _ = execute(
        monkeypatch,
        [QUIET, QUIET, (125, 130, 70, 100, 100), QUIET],
        size=1,
        evidence={
            "execution_proxy": ExecutionProxyConfig(
                Decimal("1"), "carry", ExecutionLatency(protection_activation_ms=2 * STEP)
            )
        },
    )
    assert len(result.metrics["execution_costs"]["fills"]) == 1


@pytest.mark.parametrize("profile", PROFILES)
def test_spot_opening_target_matches_paper_cash_and_inventory(monkeypatch, profile):
    scenario = Scenario(
        "spot_opening_target",
        (QUIET, QUIET, (125, 130, 80, 100, 100)),
        market="spot",
        profile=profile,
        size=1,
        commission_bps=0,
        spread_bps=0,
        slippage_bps=0,
    )
    plugin = run_plugin(monkeypatch, scenario)
    assert compare(plugin, run_paper(scenario)) == {}
    assert plugin["fills"][1]["role"] == "take_profit"
    assert plugin["fills"][1]["fill_price"] == 125
    assert plugin["final_equity"] == 10025
    assert plugin["account"]["free_margin"] == 10025
    assert plugin["account"]["margin_used"] == 0


@pytest.mark.parametrize(
    "direction,row,opening",
    [
        ("long", (125, 130, 99, 110, 4), 125),
        ("short", (75, 101, 70, 90, 4), 75),
    ],
)
@pytest.mark.parametrize("empty_open", [False, True])
def test_partial_intrabar_entry_cannot_spend_exhausted_exit_liquidity(
    monkeypatch, direction, row, opening, empty_open
):
    rows = [QUIET, row]
    if empty_open:
        rows.append((opening, 130, 70, 100, 0))
    rows.extend([(opening, 130, 70, 100, 4), QUIET])
    result, _, seen = assert_paper_parity(
        monkeypatch,
        rows,
        direction=direction,
        entry_type="limit",
        size=1,
        evidence={
            "execution_proxy": ExecutionProxyConfig(Decimal(".1"), "carry", ExecutionLatency())
        },
    )
    entry, exit_fill = result.metrics["execution_costs"]["fills"]
    assert entry["size"] == 0.4
    assert seen[1].position.quantity == 0.4
    if empty_open:
        assert seen[2].position.quantity == 0.4
    assert exit_fill["timestamp_ms"] > entry["timestamp_ms"]
    assert (exit_fill["koval_role"], exit_fill["reference_price"], exit_fill["size"]) == (
        "take_profit",
        opening,
        0.4,
    )
    assert seen[-1].position is None
    orders = result.metrics["execution_audit"]["orders"]
    assert next(o for o in orders if o["role"] == "entry")["status"] == "canceled"
    assert next(o for o in orders if o["role"] == "stop_loss")["status"] == "canceled"
