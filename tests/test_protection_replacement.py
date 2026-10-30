# SPDX-License-Identifier: GPL-3.0-or-later
"""A refused replacement leaves actual residual inventory protected."""

from decimal import Decimal

import pytest
from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig

from tests.test_position_exit import QUIET, exits, run_exit_probe
from tests.test_realistic_evidence import instrument


@pytest.mark.parametrize("mode", ["bracket", "disabled"])
@pytest.mark.parametrize(
    "constraints,reason",
    [
        ({"minimum_quantity": Decimal(".5")}, "minimum quantity"),
        ({"minimum_notional": Decimal("50")}, "minimum notional"),
    ],
)
def test_residual_filter_refusal_retains_working_protection(monkeypatch, mode, constraints, reason):
    result, events, _, _ = run_exit_probe(
        monkeypatch,
        [QUIET, QUIET, (100, 101, 75, 100, 6), (85, 86, 75, 85, 100)],
        mode=mode,
        signal_bars=(),
        stop_updates={3: 90},
        target_updates={3: 110},
        evidence={
            "instrument_specs": (instrument(market="spot", **constraints),),
            "execution_proxy": ExecutionProxyConfig(Decimal(".1"), "carry", ExecutionLatency()),
        },
    )

    # The original stop closes .6 then the remaining .4 at 80. An accepted
    # replacement would close the remainder at the next open, 85.
    assert [(fill["size"], fill["reference_price"]) for fill in exits(result)] == [
        (0.6, 80),
        (0.4, 80),
    ]
    audit = result.metrics["execution_audit"]
    assert audit["account_snapshots"][2]["account"]["open_position"]["current_stop"] == 80
    assert audit["terminal_account"]["open_position"] is None
    assert audit["terminal_account"]["balance"] == 9980
    assert result.trades[0]["sl_history"] == []
    assert len(audit["orders"]) == (3 if mode == "bracket" else 2)
    assert audit["intents"][0]["status"] == "filled"
    rejections = [event["payload"] for event in events if event["event_type"] == "ORDER_REJECTED"]
    assert len(rejections) == 1
    assert rejections[0]["operation"] == "protection_replace"
    assert rejections[0]["reason"].startswith("instrument_constraint:")
    assert reason in rejections[0]["reason"]
    assert rejections[0]["stop_loss"] == 90
    assert rejections[0]["take_profit"] == (110 if mode == "bracket" else None)
    assert "intent_id" not in rejections[0]
    assert rejections[0]["protection_order_id"] == next(
        order["order_id"] for order in audit["orders"] if order["role"] == "stop_loss"
    )
    assert audit["events"] == events


def test_refused_replacement_does_not_change_carried_entry_protection(monkeypatch):
    result, events, _, _ = run_exit_probe(
        monkeypatch,
        [QUIET, (100, 101, 99, 100, 6), QUIET, (100, 101, 75, 100, 100)],
        size=2,
        signal_bars=(),
        stop_updates={2: 90},
        target_updates={2: 110},
        evidence={
            "instrument_specs": (instrument(market="spot", minimum_notional=Decimal("56")),),
            "execution_proxy": ExecutionProxyConfig(Decimal(".1"), "carry", ExecutionLatency()),
        },
    )
    audit = result.metrics["execution_audit"]
    assert [(fill["role"], fill["size"], fill["reference_price"]) for fill in audit["fills"]] == [
        ("entry", 0.6, 100),
        ("entry", 1.4, 100),
        ("exit", 2, 80),
    ]
    assert audit["intents"][0]["status"] == "filled"
    assert result.trades[0]["sl_history"] == []
    assert len([event for event in events if event["event_type"] == "ORDER_REJECTED"]) == 1


@pytest.mark.parametrize("stop,message", [(79, "risk"), (float("nan"), "finite"), (0, "finite")])
def test_invalid_strategy_update_still_raises_with_instrument_evidence(monkeypatch, stop, message):
    with pytest.raises(ValueError, match=message):
        run_exit_probe(
            monkeypatch,
            [QUIET, QUIET, QUIET],
            signal_bars=(),
            stop_updates={2: stop},
            evidence={"instrument_specs": (instrument(market="spot"),)},
        )
