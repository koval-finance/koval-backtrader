# SPDX-License-Identifier: GPL-3.0-or-later
"""Strategies must size risk from what actually happened, not what was asked for.

A strategy that decides from `account_value` alone is deciding from one number
that hides the difference between a requested entry and the price it got, the
fees already paid, and how far the account is below its peak. In paper those
figures come from the engine's own ``PlatformAccountState``; a backtest that
computes them differently will gate differently, and the two runtimes stop
being comparable exactly when it matters.

So the adapter feeds the same MIT account state the paper runtime feeds, in the
same order, and injects the resulting snapshot. These tests pin that.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from koval.engine.backtest_engine import EngineRunSpec
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup

from koval_backtrader import backtest_runner

START_MS = 1_704_067_200_000
SIGNAL = (100.0, 101.0, 99.0, 100.0, 1000.0)


def fixed_config(**overrides) -> dict:
    model = {
        "version": "ohlcv_fixed_v1",
        "commission_bps": 4.0,
        "spread_bps": 20.0,
        "slippage_bps": 10.0,
    }
    model.update(overrides)
    return {"exchange": "binance", "exchange_type": "future", "execution_model": model}


def run(
    monkeypatch,
    rows,
    *,
    config=None,
    direction="long",
    size=2.0,
    entry=100.0,
    stop=None,
    target=None,
    capital=10_000.0,
    step_ms=3_600_000,
):
    """Capture the injected account snapshot on every bar."""
    seen: list = []

    class Probe(DeclarativeStrategy):
        def on_bar(self):
            seen.append(getattr(self, "account", None))

        def should_long(self):
            return direction == "long" and self.bar_index == 1

        def should_short(self):
            return direction == "short" and self.bar_index == 1

        def go_long(self):
            return TradeSetup(
                direction=direction,
                entry_type="market",
                entry_price=entry,
                stop_loss=stop if stop is not None else (90.0 if direction == "long" else 110.0),
                take_profit=target
                if target is not None
                else (120.0 if direction == "long" else 80.0),
                size=size,
            )

        go_short = go_long

    monkeypatch.setattr(backtest_runner, "assemble_from_graph", lambda graph: Probe())
    candles = np.array([[START_MS + i * step_ms, *row] for i, row in enumerate(rows)], dtype=float)
    events: list[dict] = []
    result = backtest_runner.create_engine().run(
        EngineRunSpec(
            graph={}, feeds={"1h": candles}, initial_capital=capital, execution_config=config
        ),
        on_event=events.append,
    )
    return result, events, seen


# --- The snapshot exists and is populated --------------------------------


def test_every_bar_sees_an_account_snapshot(monkeypatch):
    _, _, seen = run(monkeypatch, [SIGNAL, SIGNAL, SIGNAL], config=fixed_config())
    assert len(seen) == 3
    assert all(snapshot is not None for snapshot in seen)
    first = seen[0]
    assert first.balance == 10_000.0
    assert first.equity == 10_000.0
    assert first.realized_pnl == 0.0
    assert first.unrealized_pnl == 0.0
    assert first.drawdown_pct == 0.0
    assert first.position is None
    assert first.open_positions == 0
    assert first.funding_status == "unavailable"


def test_the_snapshot_is_available_to_a_legacy_run_too(monkeypatch):
    _, _, seen = run(monkeypatch, [SIGNAL, SIGNAL], config=None)
    assert seen[0].balance == 10_000.0
    assert seen[0].position is None


def test_account_value_still_agrees_with_the_snapshot_equity(monkeypatch):
    """The old scalar stays; it must not disagree with the new structure."""
    values: list[tuple[float, float]] = []

    class Probe(DeclarativeStrategy):
        def on_bar(self):
            values.append((self.account_value, self.account.equity))

    monkeypatch.setattr(backtest_runner, "assemble_from_graph", lambda graph: Probe())
    candles = np.array([[START_MS + i * 3_600_000, *SIGNAL] for i in range(3)], dtype=float)
    backtest_runner.create_engine().run(
        EngineRunSpec(
            graph={},
            feeds={"1h": candles},
            initial_capital=10_000.0,
            execution_config=fixed_config(),
        )
    )
    assert values and all(scalar == equity for scalar, equity in values)


# --- Actual fill, not requested setup ------------------------------------


def test_the_position_reports_the_actual_fill_and_quantity(monkeypatch):
    """The entry gapped to 108; the strategy must not be told it paid 100."""
    _, _, seen = run(
        monkeypatch,
        [SIGNAL, (108, 110, 107, 109, 1000), SIGNAL],
        config=fixed_config(),
        entry=100.0,
    )
    position = seen[2].position
    assert position is not None
    assert position.side == "long"
    assert position.quantity == 2.0
    # 108 open plus the adverse half-spread and slippage, not the requested 100.
    assert position.entry_price == pytest.approx(108 * 1.002)
    assert position.current_stop == 90.0
    assert position.entry_commission == pytest.approx(2 * 108 * 1.002 * 0.0004)


def test_fees_paid_accumulate_across_both_legs(monkeypatch):
    result, _, seen = run(
        monkeypatch, [SIGNAL, SIGNAL, (85, 89, 80, 86, 1000)], config=fixed_config()
    )
    trade = result.trades[0]
    assert seen[-1].fees_paid == pytest.approx(trade["commission"])
    assert seen[-1].position is None
    assert seen[-1].open_positions == 0


def test_realized_and_unrealized_split_matches_the_broker(monkeypatch):
    result, _, seen = run(monkeypatch, [SIGNAL, (100, 102, 99, 101, 1000)], config=fixed_config())
    last = seen[-1]
    assert last.balance + last.unrealized_pnl == pytest.approx(last.equity)
    assert last.equity == pytest.approx(result.metrics["final_capital"])
    assert last.position is not None


def test_margin_used_reflects_leverage(monkeypatch):
    _, _, seen = run(
        monkeypatch,
        [SIGNAL, SIGNAL, SIGNAL],
        config=fixed_config(leverage=5.0),
        size=20.0,
    )
    last = seen[-1]
    assert last.margin_used == pytest.approx(20 * 100 * 1.002 / 5)
    assert last.free_margin == pytest.approx(last.equity - last.margin_used)


def test_a_flat_account_uses_no_margin(monkeypatch):
    _, _, seen = run(monkeypatch, [SIGNAL, SIGNAL, (85, 89, 80, 86, 1000)], config=fixed_config())
    assert seen[-1].margin_used == 0.0
    assert seen[-1].free_margin == pytest.approx(seen[-1].equity)


# --- Risk revalidated after the gap and the costs ------------------------


def test_order_filled_reports_planned_and_actual_risk(monkeypatch):
    _, events, _ = run(
        monkeypatch,
        [SIGNAL, (108, 110, 107, 109, 1000), SIGNAL],
        config=fixed_config(),
        entry=100.0,
        stop=90.0,
    )
    payload = next(e for e in events if e["event_type"] == "ORDER_FILLED")["payload"]
    assert payload["requested_price"] == 100.0
    assert payload["fill_price"] == pytest.approx(108 * 1.002)
    assert payload["requested_size"] == 2.0
    assert payload["filled_size"] == 2.0
    # Planned: 2 x (100 - 90) = 20. Actual is larger because the entry gapped up
    # and both legs pay commission.
    assert payload["planned_risk"] == pytest.approx(20.0)
    assert payload["actual_risk"] > payload["planned_risk"]
    assert payload["risk_drift"] == pytest.approx(payload["actual_risk"] - payload["planned_risk"])


def test_actual_risk_includes_entry_and_exit_costs(monkeypatch):
    _, events, _ = run(
        monkeypatch, [SIGNAL, SIGNAL, SIGNAL], config=fixed_config(), entry=100.0, stop=90.0
    )
    payload = next(e for e in events if e["event_type"] == "ORDER_FILLED")["payload"]
    fill = 100 * 1.002
    stop_exit = 90 * 0.998
    expected = 2 * (fill - stop_exit) + 2 * fill * 0.0004 + 2 * stop_exit * 0.0004
    assert payload["actual_risk"] == pytest.approx(expected)


def test_risk_is_reported_without_costs_under_the_legacy_model(monkeypatch):
    _, events, _ = run(monkeypatch, [SIGNAL, SIGNAL, SIGNAL], config=None, entry=100.0, stop=90.0)
    payload = next(e for e in events if e["event_type"] == "ORDER_FILLED")["payload"]
    assert payload["planned_risk"] == pytest.approx(20.0)
    assert payload["actual_risk"] == pytest.approx(20.0)
    assert payload["risk_drift"] == pytest.approx(0.0)


def test_position_risk_is_visible_to_the_strategy(monkeypatch):
    _, _, seen = run(monkeypatch, [SIGNAL, SIGNAL, SIGNAL], config=fixed_config())
    position = seen[-1].position
    assert position.risk_amount > 0
    assert position.risk_amount == pytest.approx(
        2 * (100 * 1.002 - 90 * 0.998) + 2 * 100 * 1.002 * 0.0004 + 2 * 90 * 0.998 * 0.0004
    )


def test_moving_the_stop_updates_the_reported_risk(monkeypatch):
    seen: list = []

    class Probe(DeclarativeStrategy):
        def on_bar(self):
            seen.append(getattr(self, "account", None))

        def should_long(self):
            return self.bar_index == 1

        def go_long(self):
            return TradeSetup(
                direction="long",
                entry_type="market",
                entry_price=100.0,
                stop_loss=90.0,
                take_profit=120.0,
                size=2.0,
            )

        def on_sl_update(self, trade_id):
            return 95.0 if self.bar_index == 3 else None

    monkeypatch.setattr(backtest_runner, "assemble_from_graph", lambda graph: Probe())
    candles = np.array([[START_MS + i * 3_600_000, *SIGNAL] for i in range(4)], dtype=float)
    backtest_runner.create_engine().run(
        EngineRunSpec(
            graph={},
            feeds={"1h": candles},
            initial_capital=10_000.0,
            execution_config=fixed_config(),
        )
    )
    assert seen[2].position.current_stop == 90.0
    assert seen[3].position.current_stop == 95.0
    assert seen[3].position.risk_amount < seen[2].position.risk_amount


# --- Drawdown and daily PnL semantics ------------------------------------


def test_drawdown_tracks_the_peak_every_bar(monkeypatch):
    rows = [SIGNAL, SIGNAL, (100, 130, 99, 129, 1000), (95, 96, 80, 82, 1000)]
    _, _, seen = run(monkeypatch, rows, config=fixed_config(), target=200.0, stop=70.0)
    peak = max(snapshot.equity for snapshot in seen)
    assert seen[-1].peak_equity == pytest.approx(peak)
    assert seen[-1].drawdown_pct == pytest.approx(100.0 * (peak - seen[-1].equity) / peak)
    assert seen[-1].drawdown_pct > 0


def test_daily_pnl_includes_the_move_since_the_previous_utc_day(monkeypatch):
    """The previous close is the new day's baseline, including overnight moves."""
    rows = [SIGNAL, (100, 101, 99, 100, 1000), (105, 106, 104, 105, 1000)]
    _, _, seen = run(monkeypatch, rows, config=fixed_config(), step_ms=86_400_000)
    assert seen[0].daily_pnl == 0.0
    assert seen[-1].daily_pnl == pytest.approx(seen[-1].equity - seen[-2].equity)


@pytest.mark.parametrize(
    "direction,stop", [("long", 85.0), ("short", 115.0), ("long", float("nan")), ("long", 0.0)]
)
def test_account_refuses_invalid_stop_updates_without_mutation(direction, stop):
    from koval_backtrader.execution_account import ExecutionAccount

    account = ExecutionAccount(1000.0)
    account.on_open(
        direction=direction,
        fill_price=100.0,
        quantity=1.0,
        stop_price=90.0 if direction == "long" else 110.0,
        commission=0.1,
        margin=20.0,
    )
    before = account.snapshot()
    with pytest.raises(ValueError, match="stop update"):
        account.on_stop_moved(stop)
    assert account.snapshot() == before


def test_snapshot_has_the_complete_engine_account_contract(monkeypatch):
    from dataclasses import fields

    from koval.engine.account_state import AccountSnapshot

    result, _, seen = run(
        monkeypatch, [SIGNAL, SIGNAL, (85, 89, 80, 86, 1000)], config=fixed_config()
    )
    snapshot = seen[-1]
    assert all(hasattr(snapshot, field.name) for field in fields(AccountSnapshot))
    assert snapshot.fees == pytest.approx(result.trades[0]["commission"])
    assert snapshot.trade_realized_pnl == pytest.approx(result.trades[0]["gross_price_pnl"])
    assert snapshot.daily_loss_pct > 0


def test_snapshot_does_not_rescan_past_ledger_entries():
    from koval_backtrader.execution_account import ExecutionAccount

    account = ExecutionAccount(1000.0)
    for _ in range(1000):
        account.on_fee(0.001)

    class CountedEntries(list):
        scans = 0

        def __iter__(self):
            self.scans += 1
            return super().__iter__()

    entries = CountedEntries(account._state.ledger._entries)
    account._state.ledger._entries = entries
    for index in range(100):
        account.on_bar(equity=999.0, timestamp_ms=START_MS + index * 1000)
        assert account.snapshot().balance == pytest.approx(999.0)
    assert entries.scans == 0


def test_graph_nodes_read_the_actual_execution_account(monkeypatch):
    from koval.examples import parity_fixtures
    from koval.strategy.graph.executor import GraphExecutor

    fixture = next(f for f in parity_fixtures() if f["fixture_id"] == "long_entry_bar_ambiguity_v2")
    captured = []
    step = GraphExecutor.step

    def capture(self, context):
        captured.append(context.account)
        return step(self, context)

    monkeypatch.setattr(GraphExecutor, "step", capture)
    rows = np.array(fixture["candles"], dtype=float)
    result = backtest_runner.create_engine().run(
        EngineRunSpec(
            graph=fixture["graph"],
            feeds={fixture["timeframe"]: rows},
            initial_capital=fixture["capital"],
            execution_config=fixed_config(),
        )
    )
    fill = result.metrics["execution_costs"]["fills"][0]
    snapshot = captured[-1]
    assert snapshot.open_position.entry_price == pytest.approx(fill["fill_price"])
    assert snapshot.open_position.quantity == pytest.approx(fill["size"])
    assert snapshot.fees == pytest.approx(fill["commission"])
    assert snapshot.balance == pytest.approx(fixture["capital"] - fill["commission"])


def test_daily_pnl_accumulates_within_one_utc_day(monkeypatch):
    rows = [SIGNAL, SIGNAL, (105, 106, 104, 105, 1000)]
    _, _, seen = run(monkeypatch, rows, config=fixed_config())
    assert seen[-1].daily_pnl == pytest.approx(seen[-1].equity - seen[0].equity)


def test_the_drawdown_cut_off_trips_on_the_bar_the_loss_is_realised():
    """The gate shares the account's peak but must not lag a bar behind it.

    `_check_drawdown` runs during close notification, before the account has
    absorbed this bar's equity. Comparing against the account's own equity
    there would postpone every trip by one bar, so this pins the bar.

    Driven through Cerebro directly because `max_drawdown` is an adapter
    parameter that the runner does not take from the run spec.
    """
    import backtrader as bt
    import pandas as pd
    from koval.engine.engine_events import EventType

    from koval_backtrader.bt_adapter import make_bt_strategy_class
    from koval_backtrader.oco_patch import apply_oco_guard

    apply_oco_guard()

    class Once(DeclarativeStrategy):
        def should_long(self):
            return self.bar_index == 1

        def go_long(self):
            return TradeSetup(
                direction="long",
                entry_type="market",
                entry_price=100.0,
                stop_loss=70.0,
                take_profit=200.0,
                size=50.0,
            )

    rows = [SIGNAL, SIGNAL, (100, 130, 99, 129, 1000), (95, 96, 60, 62, 1000), SIGNAL]
    frame = pd.DataFrame(
        [row[:5] for row in rows],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.to_datetime(
            [START_MS + i * 3_600_000 for i in range(len(rows))], unit="ms", utc=True
        ),
    )
    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=frame))
    cerebro.broker.setcash(10_000.0)
    cerebro.addstrategy(make_bt_strategy_class(Once, max_drawdown=5.0))
    strat = cerebro.run()[0]

    hit = [e for e in strat._events if e.event_type == EventType.DRAWDOWN_LIMIT_HIT]
    assert len(hit) == 1
    # Bar 4 is where the stop fills and the loss is realised. A one-bar lag
    # would report this on bar 5.
    assert hit[0].bar_index == 4
    assert hit[0].payload["drawdown_pct"] > hit[0].payload["limit"]
    assert strat._dd_limit_hit is True


def test_balance_reconciles_to_the_trade_ledger_over_many_trades(monkeypatch):
    """Balance must be derivable from the trades, not from catching one event.

    `pnlcomm` already accounts for every commission Backtrader charged. Anything
    that reconstructs the same figure by intercepting the exit order's
    notification drifts silently the moment a close arrives another way.
    """
    seen: list = []

    class Probe(DeclarativeStrategy):
        def on_bar(self):
            seen.append(self.account)

        def should_long(self):
            return self.position_size == 0.0

        def go_long(self):
            return TradeSetup(
                direction="long",
                entry_type="market",
                entry_price=self.close,
                stop_loss=self.close * 0.97,
                take_profit=self.close * 1.03,
                size=1.0,
            )

    monkeypatch.setattr(backtest_runner, "assemble_from_graph", lambda graph: Probe())
    rng = np.random.default_rng(11)
    prices = np.maximum(100 + np.cumsum(rng.standard_normal(150) * 1.5), 5.0)
    candles = np.array(
        [
            [START_MS + i * 3_600_000, p, p * 1.02, p * 0.98, p, 1000.0]
            for i, p in enumerate(prices)
        ],
        dtype=float,
    )
    result = backtest_runner.create_engine().run(
        EngineRunSpec(
            graph={},
            feeds={"1h": candles},
            initial_capital=10_000.0,
            execution_config=fixed_config(),
        )
    )
    assert len(result.trades) > 10
    account = seen[-1]
    realized = math.fsum(trade["realized_pnl"] for trade in result.trades)
    closed_fees = math.fsum(trade["commission"] for trade in result.trades)
    # A position still open at the end has already paid its entry fee, so the
    # wallet is lower than the closed-trade sum alone by exactly that amount.
    # The audit names the same quantity, and the two must agree.
    open_entry_fee = result.metrics["execution_costs"]["open_commission"]
    assert account.balance == pytest.approx(10_000.0 + realized - open_entry_fee, abs=1e-9)
    assert account.realized_pnl == pytest.approx(realized - open_entry_fee, abs=1e-9)
    assert account.fees_paid == pytest.approx(closed_fees + open_entry_fee, abs=1e-9)
    # Equity is the authoritative figure and must match the broker exactly.
    assert account.equity == pytest.approx(result.metrics["final_capital"], abs=1e-9)
    assert account.balance + account.unrealized_pnl == pytest.approx(account.equity, abs=1e-9)


def test_balance_reconciles_when_the_run_ends_flat(monkeypatch):
    """The same invariant with no open entry fee in play, stated separately."""
    result, _, seen = run(
        monkeypatch, [SIGNAL, SIGNAL, (85, 89, 80, 86, 1000)], config=fixed_config()
    )
    assert result.metrics["execution_costs"]["open_commission"] == 0.0
    realized = math.fsum(trade["realized_pnl"] for trade in result.trades)
    assert seen[-1].balance == pytest.approx(10_000.0 + realized, abs=1e-9)
    assert seen[-1].fees_paid == pytest.approx(
        math.fsum(trade["commission"] for trade in result.trades), abs=1e-9
    )


@pytest.mark.parametrize("direction,stop", [("long", 110.0), ("short", 90.0)])
def test_profit_lock_reports_no_capital_at_risk(direction, stop):
    from koval_backtrader.execution_account import risk_to_stop

    assert (
        risk_to_stop(
            direction=direction,
            quantity=1.0,
            entry_price=100.0,
            stop_price=stop,
            commission_bps=4.0,
        )
        == 0.0
    )
