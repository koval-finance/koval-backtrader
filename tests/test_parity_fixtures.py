# SPDX-License-Identifier: GPL-3.0-or-later
"""The MIT engine ships public golden fixtures; this plugin must reproduce them.

Reproducing a fixture means reproducing the whole result, not three numbers
that happen to line up. Direction, exit reason, quantity, the cost split, the
lifecycle events, the reconciled ledger and the final equity are all compared,
because a plugin can agree on an entry price while disagreeing about what the
account did.

Broad agreement with the paper broker beyond these six cases is proven by
``test_paper_parity.py``.
"""

from __future__ import annotations

import numpy as np
import pytest
from koval.engine.backtest_engine import EngineRunSpec, load_backtest_engine
from koval.examples import parity_fixtures
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup

from koval_backtrader import backtest_runner

# The fixtures name an exit with the venue's shorthand; a trade record spells
# it out. Mapping them here keeps the fixture format the engine's business.
PROFILES = {
    "paper_ohlcv_fixed_v1": ("koval_execution_contract_v1", "ohlcv_fixed_v1"),
    "paper_ohlcv_realistic_v2": ("koval_execution_contract_v2", "ohlcv_realistic_v2"),
}

_EXIT_REASONS = {"sl": "Stop Loss", "tp": "Take Profit"}


def _spec(fixture: dict) -> EngineRunSpec:
    return EngineRunSpec(
        graph=fixture["graph"],
        feeds={fixture["timeframe"]: np.array(fixture["candles"], dtype=float)},
        initial_capital=fixture["capital"],
        execution_config={
            "exchange": fixture["exchange"],
            "exchange_type": fixture["exchange_type"],
            "market": {
                "exchange": fixture["exchange"],
                "market": fixture["exchange_type"],
                "canonical_symbol": "BTCUSDT",
                "contract_type": "spot" if fixture["exchange_type"] == "spot" else "perpetual",
            },
            "execution_model": {
                "version": PROFILES[fixture.get("profile_version", "paper_ohlcv_fixed_v1")][1],
                **fixture["costs"],
            },
        },
    )


def _assert_matches(result, fixture, events):
    expected = fixture["expected"]
    assert result.metrics["total_trades"] == len(expected["trades"])
    assert len(result.trades) == len(expected["trades"])

    for trade, want in zip(result.trades, expected["trades"], strict=True):
        assert trade["direction"] == want["direction"].upper()
        assert trade["entry_price"] == pytest.approx(want["entry_price"], abs=1e-9)
        assert trade["exit_price"] == pytest.approx(want["exit_price"], abs=1e-9)
        assert trade["commission"] == pytest.approx(want["commission"], abs=1e-9)
        assert trade["exit_reason"] == _EXIT_REASONS[want["exit_reason"]]
        # Attribution has to add up inside the trade as well as across the run.
        costs = trade["execution_costs"]
        assert costs["spread_cost"] + costs["slippage_cost"] == pytest.approx(
            costs["price_adjustment_cost"], abs=1e-9
        )
        assert trade["net_pnl_before_funding"] == pytest.approx(
            trade["gross_price_pnl"] - trade["commission"], abs=1e-9
        )
        assert costs["funding_status"] == "unavailable"
        entry_fill, exit_fill = costs["fills"]
        assert entry_fill["koval_role"] == "entry"
        assert exit_fill["koval_role"] == (
            "stop_loss" if want["exit_reason"] == "sl" else "take_profit"
        )
        assert entry_fill["size"] == pytest.approx(exit_fill["size"], abs=1e-12)
        assert entry_fill["fill_price"] == pytest.approx(trade["entry_price"], abs=1e-9)
        assert exit_fill["fill_price"] == pytest.approx(trade["exit_price"], abs=1e-9)

    assert result.metrics["final_capital"] == pytest.approx(expected["final_equity"], abs=1e-6)
    audit = result.metrics["execution_costs"]
    assert audit["reconciliation_error"] == pytest.approx(0.0, abs=1e-9)
    assert result.equity_curve[-1]["equity"] == pytest.approx(
        result.metrics["final_capital"], abs=1e-9
    )

    if "ambiguity_reason" in expected:
        assert expected["ambiguity_reason"] in {
            item["reason_code"] for item in audit["ambiguities"]
        }
    if "rejection_reason" in expected:
        assert [e["payload"]["reason"] for e in events if e["event_type"] == "ORDER_REJECTED"] == [
            expected["rejection_reason"]
        ]
    emitted = [event["event_type"] for event in events]
    for required in expected.get("events", []):
        assert required in emitted, f"{fixture['fixture_id']} did not emit {required}"
    if expected["trades"]:
        assert emitted.count("TRADE_OPENED") == len(expected["trades"])
        assert emitted.count("TRADE_CLOSED") == len(expected["trades"])


@pytest.mark.parametrize("fixture", parity_fixtures(), ids=lambda f: f["fixture_id"])
def test_plugin_reproduces_engine_parity_fixture(fixture, monkeypatch):
    if fixture.get("requires_direct_setup"):
        setup = fixture["setup"]

        class Direct(DeclarativeStrategy):
            def should_long(self):
                return setup["direction"] == "long" and self.bar_index == 1

            def should_short(self):
                return setup["direction"] == "short" and self.bar_index == 1

            def go_long(self):
                return TradeSetup(
                    direction=setup["direction"],
                    entry_type=setup.get("entry_type", "market"),
                    entry_price=setup.get("entry_price", self.close),
                    stop_loss=setup["stop_loss"],
                    take_profit=setup["take_profit"],
                    size=setup["size"],
                )

            go_short = go_long

            def on_tp_update(self, trade_id):
                return next(
                    (
                        action["value"]
                        for action in fixture.get("strategy_hooks", {}).get("on_tp_update", [])
                        if action["after_bar_index"] == self.bar_index - 1
                    ),
                    None,
                )

            def on_sl_update(self, trade_id):
                return next(
                    (
                        action["value"]
                        for action in fixture.get("strategy_hooks", {}).get("on_sl_update", [])
                        if action["after_bar_index"] == self.bar_index - 1
                    ),
                    None,
                )

        monkeypatch.setattr(backtest_runner, "assemble_from_graph", lambda graph: Direct())

    events: list[dict] = []
    result = load_backtest_engine().run(_spec(fixture), on_event=events.append)
    _assert_matches(result, fixture, events)


def test_every_shipped_fixture_is_exercised():
    """A fixture nobody runs is a fixture nobody is checked against."""
    fixtures = list(parity_fixtures())
    assert fixtures, "the engine shipped no parity fixtures"
    for fixture in fixtures:
        assert fixture.get("profile_version", "paper_ohlcv_fixed_v1") in PROFILES, (
            f"unsupported engine profile: {fixture['profile_version']}"
        )
        assert (
            fixture["contract"]
            == PROFILES[fixture.get("profile_version", "paper_ohlcv_fixed_v1")][0]
        )
        if fixture.get("requires_direct_setup"):
            assert set(fixture["setup"]) >= {"direction", "size", "stop_loss", "take_profit"}
            assert set(fixture.get("strategy_hooks", {})) <= {"on_sl_update", "on_tp_update"}
