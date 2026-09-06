# SPDX-License-Identifier: GPL-3.0-or-later
"""The MIT engine ships public golden fixtures; this plugin must reproduce them."""

from __future__ import annotations

import numpy as np
import pytest
from koval.engine.backtest_engine import EngineRunSpec, load_backtest_engine
from koval.examples import parity_fixtures


@pytest.mark.parametrize("fixture", parity_fixtures(), ids=lambda f: f["fixture_id"])
def test_plugin_reproduces_engine_parity_fixture(fixture):
    if fixture.get("requires_direct_setup"):
        pytest.skip("direct-setup fixtures run in test_execution_realism.py")
    candles = np.array(fixture["candles"], dtype=float)
    spec = EngineRunSpec(
        graph=fixture["graph"],
        feeds={fixture["timeframe"]: candles},
        initial_capital=fixture["capital"],
        execution_config={
            "exchange": fixture["exchange"],
            "exchange_type": fixture["exchange_type"],
            "execution_model": {"version": "ohlcv_fixed_v1", **fixture["costs"]},
        },
    )
    result = load_backtest_engine().run(spec)
    expected = fixture["expected"]
    assert result.metrics["total_trades"] == len(expected["trades"])
    for trade, want in zip(result.trades, expected["trades"], strict=True):
        assert trade["entry_price"] == pytest.approx(want["entry_price"], abs=1e-9)
        assert trade["exit_price"] == pytest.approx(want["exit_price"], abs=1e-9)
        assert trade["commission"] == pytest.approx(want["commission"], abs=1e-9)
    assert result.metrics["final_capital"] == pytest.approx(expected["final_equity"], abs=1e-6)
