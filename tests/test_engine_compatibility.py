# SPDX-License-Identifier: GPL-3.0-or-later
"""Published historical graph scenarios through the unchanged plugin seam."""

import json

import numpy as np
import pytest
from koval.engine.backtest_engine import EngineRunSpec, load_backtest_engine
from koval.examples import example_path

from tests.test_position_exit import configuration


@pytest.mark.parametrize("veto", [False, True])
def test_published_level_graph_preserves_next_open_fills_and_quote_cash(veto):
    fixture = json.loads(example_path("runtime", "historical_level_graph_v1.json").read_text())
    assert fixture["schema"] == "koval_historical_level_graph_fixture_v1"
    candles = np.array(fixture["candles"], dtype=float)
    if veto:
        for index, volume in fixture["veto_variant"]["volume_changes"].items():
            candles[int(index), 5] = volume
    result = load_backtest_engine().run(
        EngineRunSpec(
            graph=fixture["graph"],
            feeds={"1m": candles},
            initial_capital=1000,
            execution_config=configuration(costs={"commission_bps": 10}),
        )
    )
    fills = result.metrics["execution_audit"]["fills"]
    account = result.metrics["execution_audit"]["terminal_account"]
    if veto:
        assert fills == []
        assert result.trades == []
        assert result.metrics["final_capital"] == 1000
        return
    assert [fill["role"] for fill in fills] == ["entry", "exit"]
    assert [fill["timestamp_ms"] for fill in fills] == [420_000, 480_000]
    assert [fill["fill_price"] for fill in fills] == [113, 114]
    quantity = fills[0]["size"]
    assert quantity > 0
    assert fills[1]["size"] == quantity
    assert fills[0]["commission"] == pytest.approx(0.113 * quantity)
    assert fills[1]["commission"] == pytest.approx(0.114 * quantity)
    assert len(result.trades) == 1
    assert result.trades[0]["reason"] == "signal"
    assert result.trades[0]["fully_signal_closed"] is True
    assert account["balance"] == pytest.approx(1000 + 0.773 * quantity)
    assert result.metrics["final_capital"] == pytest.approx(1000 + 0.773 * quantity)
    assert result.metrics["research"]["final_open_position"] is None
    assert abs(result.metrics["execution_costs"]["reconciliation_error"]) < 1e-8
