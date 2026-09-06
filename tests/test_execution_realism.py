"""Deterministic execution probes through the public runner, without cheat modes."""

from __future__ import annotations

import copy
import json

import numpy as np
import pytest
from koval.engine.backtest_engine import EngineRunSpec
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup

from koval_backtrader import backtest_runner
from koval_backtrader.execution_config import resolve_execution_model

START_MS = 1_704_067_200_000


def run_probe(
    monkeypatch,
    rows,
    *,
    config=None,
    direction="long",
    entry_type="market",
    entry=100.0,
    stop=None,
    target=None,
    size=2.0,
    capital=10_000.0,
    on_event=None,
    observed_timestamps=None,
    start_ms=None,
):
    """Replace graph assembly only; exercise the real adapter, broker and analyzers."""

    class Probe(DeclarativeStrategy):
        def on_bar(self):
            if observed_timestamps is not None:
                observed_timestamps.append(self.timestamp_ms)

        def should_long(self):
            return direction == "long" and self.bar_index == 1

        def should_short(self):
            return direction == "short" and self.bar_index == 1

        def go_long(self):
            return TradeSetup(
                direction=direction,
                entry_type=entry_type,
                entry_price=entry,
                stop_loss=stop if stop is not None else (90.0 if direction == "long" else 110.0),
                take_profit=target
                if target is not None
                else (120.0 if direction == "long" else 80.0),
                size=size,
            )

        go_short = go_long

    monkeypatch.setattr(backtest_runner, "assemble_from_graph", lambda graph: Probe())
    first_ms = START_MS if start_ms is None else start_ms
    candles = np.array(
        [[first_ms + i * 3_600_000, *row] for i, row in enumerate(rows)], dtype=float
    )
    events = []

    def receive(event):
        events.append(event)
        if on_event is not None:
            on_event(event)

    result = backtest_runner.create_engine().run(
        EngineRunSpec(
            graph={}, feeds={"1h": candles}, initial_capital=capital, execution_config=config
        ),
        on_event=receive,
    )
    return result, events


SIGNAL = [100, 101, 99, 100, 1000]


def fixed_config(**overrides):
    model = {
        "version": "ohlcv_fixed_v1",
        "commission_bps": 4.0,
        "spread_bps": 20.0,
        "slippage_bps": 10.0,
    }
    model.update(overrides)
    return {"exchange": "binance", "exchange_type": "future", "execution_model": model}


@pytest.mark.parametrize("direction,exit_price", [("long", 90), ("short", 110)])
def test_legacy_next_open_delayed_bracket_and_stop_first(monkeypatch, direction, exit_price):
    result, events = run_probe(
        monkeypatch,
        [SIGNAL, [102, 150, 50, 100, 1000], [100, 125, 75, 100, 1000]],
        direction=direction,
    )
    trade = result.trades[0]
    assert len(result.trades) == 1
    assert trade["entry_price"] == 102
    assert trade["exit_price"] == exit_price
    sign = 1 if direction == "long" else -1
    assert trade["realized_pnl"] == sign * 2 * (exit_price - 102)
    assert result.metrics["final_capital"] == 10_000 + trade["realized_pnl"]
    assert [e["bar_index"] for e in events if e["event_type"] == "TRADE_CLOSED"] == [3]


@pytest.mark.parametrize(
    "direction,entry_type,entry,row,expected",
    [
        ("long", "limit", 100, [98, 105, 95, 100, 1], 98),
        ("short", "limit", 100, [102, 105, 95, 100, 1], 102),
        ("long", "limit", 99, [100, 105, 95, 100, 1], 99),
        ("short", "limit", 101, [100, 105, 95, 100, 1], 101),
        ("long", "stop", 101, [103, 105, 95, 100, 1], 103),
        ("short", "stop", 99, [97, 105, 95, 100, 1], 97),
        ("long", "stop", 101, [100, 105, 95, 100, 1], 101),
        ("short", "stop", 99, [100, 105, 95, 100, 1], 99),
    ],
)
def test_legacy_limit_and_stop_reference_prices(
    monkeypatch, direction, entry_type, entry, row, expected
):
    result, events = run_probe(
        monkeypatch, [SIGNAL, row], direction=direction, entry_type=entry_type, entry=entry
    )
    assert result.trades == []
    fill = next(e for e in events if e["event_type"] == "ORDER_FILLED")
    assert fill["payload"]["fill_price"] == expected
    assert fill["payload"]["size"] == 2  # Even when bar volume is only one unit.


@pytest.mark.parametrize(
    "config,bps",
    [
        (None, 0),
        ({}, 0),
        ({"exchange": "binance", "exchange_type": "future"}, 4),
        ({"exchange": "binance", "exchange_type": "spot"}, 10),
    ],
)
def test_legacy_commission_and_accounting_baseline(monkeypatch, config, bps):
    result, _ = run_probe(monkeypatch, [SIGNAL, SIGNAL, [85, 89, 80, 86, 1000]], config=config)
    trade = result.trades[0]
    commission = 2 * (100 + 85) * bps / 10_000
    assert trade["entry_price"] == 100
    assert trade["exit_price"] == 85
    assert trade["commission"] == pytest.approx(commission)
    assert trade["gross_realized_pnl"] == pytest.approx(-30 - commission)
    assert trade["funding_adjustment"] == 0
    assert trade["realized_pnl"] == pytest.approx(-30 - commission)
    assert result.metrics["final_capital"] == pytest.approx(9970 - commission)


@pytest.mark.parametrize(
    "direction,entry_price,exit_price", [("long", 100.2, 89.82), ("short", 99.8, 110.22)]
)
def test_fixed_costs_are_adverse_at_entry_and_stop_exit(
    monkeypatch, direction, entry_price, exit_price
):
    result, events = run_probe(
        monkeypatch,
        [SIGNAL, SIGNAL, [100, 125, 75, 100, 1000]],
        config=fixed_config(),
        direction=direction,
    )
    trade = result.trades[0]
    assert trade["entry_price"] == pytest.approx(entry_price)
    assert trade["exit_price"] == pytest.approx(exit_price)
    assert len(result.trades) == 1  # Both bracket prices touched: no OCO ghost reversal.
    commission = 2 * (entry_price + exit_price) * 0.0004
    sign = 1 if direction == "long" else -1
    gross = sign * 2 * (exit_price - entry_price)
    assert trade["gross_price_pnl"] == pytest.approx(gross)
    assert trade["commission"] == pytest.approx(commission)
    assert trade["net_pnl_before_funding"] == pytest.approx(gross - commission)
    assert trade["realized_pnl"] == pytest.approx(gross - commission)
    assert result.metrics["final_capital"] == pytest.approx(10_000 + gross - commission)
    opened = next(e for e in events if e["event_type"] == "ORDER_FILLED")
    closed = next(e for e in events if e["event_type"] == "TRADE_CLOSED")
    assert opened["payload"]["fill_price"] == trade["entry_price"]
    assert closed["payload"]["exit_price"] == trade["exit_price"]
    assert closed["payload"]["pnl_comm"] == trade["realized_pnl"]


@pytest.mark.parametrize(
    "direction,row,reference,expected",
    [
        ("long", [85, 89, 80, 86, 1000], 85, 84.83),
        ("short", [115, 120, 112, 116, 1000], 115, 115.23),
    ],
)
def test_gap_stop_cost_is_applied_after_the_open_gap(
    monkeypatch, direction, row, reference, expected
):
    result, _ = run_probe(
        monkeypatch, [SIGNAL, SIGNAL, row], config=fixed_config(), direction=direction
    )
    trade = result.trades[0]
    assert trade["exit_price"] == pytest.approx(expected)
    fill = trade["execution_costs"]["fills"][1]
    assert fill["reference_price"] == reference
    assert fill["price_adjustment_cost"] == pytest.approx(2 * abs(expected - reference))


@pytest.mark.parametrize(
    "direction,entry_type,entry,row,expected",
    [
        ("long", "limit", 100, [98, 105, 95, 100, 1000], 98.196),
        ("short", "limit", 100, [102, 105, 95, 100, 1000], 101.796),
        ("long", "limit", 100.1, [100, 105, 95, 100, 1000], 100.1),
        ("short", "limit", 99.9, [100, 105, 95, 100, 1000], 99.9),
        ("long", "limit", 99, [100, 105, 95, 100, 1000], 99),
        ("short", "limit", 101, [100, 105, 95, 100, 1000], 101),
        ("long", "stop", 101, [103, 105, 95, 100, 1000], 103.206),
        ("short", "stop", 99, [97, 105, 95, 100, 1000], 96.806),
        ("long", "stop", 101, [100, 105, 95, 100, 1000], 101.202),
        ("short", "stop", 99, [100, 105, 95, 100, 1000], 98.802),
    ],
)
def test_fixed_limit_and_stop_entries(monkeypatch, direction, entry_type, entry, row, expected):
    result, events = run_probe(
        monkeypatch,
        [SIGNAL, row],
        config=fixed_config(),
        direction=direction,
        entry_type=entry_type,
        entry=entry,
    )
    fill = next(e for e in events if e["event_type"] == "ORDER_FILLED")
    assert fill["payload"]["fill_price"] == pytest.approx(expected)
    audit = result.metrics["execution_costs"]["fills"][0]
    assert audit["fill_price"] == fill["payload"]["fill_price"]
    assert audit["commission"] == pytest.approx(2 * expected * 0.0004)
    assert audit["commission_policy"] == "uniform"
    assert audit["liquidity_role"] == "unavailable"
    assert audit["spread_cost"] == pytest.approx(audit["slippage_cost"])
    if entry_type == "limit":
        assert expected <= entry if direction == "long" else expected >= entry


@pytest.mark.parametrize(
    "direction,row,expected",
    [
        # A favourable open is the reference; the adverse adjustment from it is
        # already worse than the target, so these two are unchanged by v1's
        # market-on-touch take-profit.
        ("long", [125, 130, 121, 124, 1000], 124.75),
        ("short", [75, 79, 70, 76, 1000], 75.15),
        # An exact touch used to fill at the target for free. A take-profit is a
        # market-on-touch order (venue TAKE_PROFIT_MARKET), so it now pays the
        # full adverse adjustment: 120 * 0.998 and 80 * 1.002.
        ("long", [119, 122, 118, 121, 1000], 119.76),
        ("short", [81, 82, 78, 79, 1000], 80.16),
    ],
)
def test_take_profit_targets_pay_full_adverse_cost_with_uniform_fees(
    monkeypatch, direction, row, expected
):
    result, _ = run_probe(
        monkeypatch, [SIGNAL, SIGNAL, row], config=fixed_config(), direction=direction
    )
    trade = result.trades[0]
    assert trade["exit_price"] == pytest.approx(expected)
    assert trade["exit_reason"] == "Take Profit"
    fill = trade["execution_costs"]["fills"][1]
    assert fill["commission"] == pytest.approx(2 * expected * 0.0004)
    assert fill["liquidity_role"] == "unavailable"


def test_costs_are_not_erased_by_flat_bars_or_changed_by_future_range(monkeypatch):
    flat, _ = run_probe(monkeypatch, [SIGNAL, [100, 100, 100, 100, 0]], config=fixed_config())
    wide, _ = run_probe(monkeypatch, [SIGNAL, [100, 200, 1, 100, 1e9]], config=fixed_config())
    assert flat.metrics["final_capital"] == pytest.approx(9999.51984)
    assert flat.metrics["execution_costs"] == wide.metrics["execution_costs"]
    fill = flat.metrics["execution_costs"]["fills"][0]
    assert fill["fill_price"] > 100  # Explicit synthetic cost price outside observed OHLC.


def test_worked_reconciliation_and_serializable_replay_metadata(monkeypatch):
    config = fixed_config()
    original = copy.deepcopy(config)
    rows = [SIGNAL, SIGNAL, [85, 89, 80, 86, 1000]]
    result, _ = run_probe(monkeypatch, rows, config=config)
    assert result.metrics["final_capital"] == pytest.approx(9969.111976)
    trade = result.trades[0]
    costs = trade["execution_costs"]
    assert costs["reference_pnl"] == -30
    assert costs["spread_cost"] == pytest.approx(0.37)
    assert costs["slippage_cost"] == pytest.approx(0.37)
    assert costs["commission"] == pytest.approx(0.148024)
    assert trade["realized_pnl"] == pytest.approx(-30.888024)
    assert trade["gross_price_pnl"] == pytest.approx(-30.74)
    audit = result.metrics["execution_costs"]
    assert audit["reconciliation_error"] == pytest.approx(0, abs=1e-9)
    assert audit["reference_pnl"] == -30
    assert audit["open_unrealized_pnl"] == 0
    assert audit["open_commission"] == 0
    assert audit["closed_net_pnl"] == trade["realized_pnl"]
    assert result.equity_curve[-1]["equity"] == result.metrics["final_capital"]
    metadata = result.metrics["execution_model"]
    # The resolved config freezes every effective assumption, including the
    # leverage the request left implicit.
    assert metadata["resolved_config"] == fixed_config(leverage=1.0)
    assert metadata["assumptions"]["funding"] == "unavailable"
    assert metadata["assumptions"]["liquidity"] == "unlimited_full_fills"
    assert metadata["assumptions"]["additional_latency_bars"] == 0
    assert metadata["software"]["backtrader"] == "1.9.78.123"
    assert config == original
    replay, replay_events = run_probe(
        monkeypatch, rows, config=json.loads(json.dumps(metadata["resolved_config"]))
    )
    assert replay == result
    assert json.loads(json.dumps(result.metrics, allow_nan=False)) == result.metrics
    assert replay_events[0]["payload"]["execution_model"] == metadata


@pytest.mark.parametrize(
    "direction,expected_gross,expected_comm", [("long", 1.6, 0.08016), ("short", -2.4, 0.07984)]
)
def test_open_position_reconciliation_includes_entry_costs(
    monkeypatch, direction, expected_gross, expected_comm
):
    result, _ = run_probe(
        monkeypatch, [SIGNAL, [100, 102, 99, 101, 1000]], config=fixed_config(), direction=direction
    )
    assert result.trades == []
    audit = result.metrics["execution_costs"]
    assert audit["open_unrealized_pnl"] == pytest.approx(expected_gross)
    assert audit["open_commission"] == pytest.approx(expected_comm)
    assert audit["closed_net_pnl"] == 0
    assert audit["funding_cashflow"] == 0
    assert audit["funding_status"] == "unavailable"
    assert result.metrics["final_capital"] == pytest.approx(10_000 + expected_gross - expected_comm)
    assert audit["reconciliation_error"] == pytest.approx(0, abs=1e-9)


@pytest.mark.parametrize(
    "rows,capital,entry_type,entry",
    [
        ([SIGNAL], 10_000, "market", 100),
        ([SIGNAL, SIGNAL], 100, "market", 100),
        ([SIGNAL, SIGNAL], 10_000, "limit", 80),
    ],
)
def test_pending_and_cash_rejected_orders_have_no_costs(
    monkeypatch, rows, capital, entry_type, entry
):
    result, _ = run_probe(
        monkeypatch,
        rows,
        config=fixed_config(),
        capital=capital,
        entry_type=entry_type,
        entry=entry,
    )
    assert result.metrics["execution_costs"]["fills"] == []
    assert result.metrics["execution_costs"]["commission"] == 0
    assert result.metrics["final_capital"] == capital


def test_explicit_legacy_model_freezes_fee_resolution(monkeypatch):
    rows = [SIGNAL, SIGNAL, [85, 89, 80, 86, 1000]]
    legacy = {"exchange": "binance", "exchange_type": "future", "taker_fee_bps": 13.0}
    first, _ = run_probe(monkeypatch, rows, config=legacy)
    frozen = {
        "exchange": "binance",
        "exchange_type": "future",
        "execution_model": {"version": "legacy_v1", "commission_bps": 13.0},
    }
    second, _ = run_probe(monkeypatch, rows, config=frozen)
    assert second.trades == first.trades
    assert second.equity_curve == first.equity_curve
    assert first.metrics["execution_model"]["resolved_config"] == frozen
    assert first.metrics == second.metrics


@pytest.mark.parametrize("name", ["commission_bps", "spread_bps", "slippage_bps"])
@pytest.mark.parametrize(
    "value", [-1, float("nan"), float("inf"), -float("inf"), True, "1", None, [], 10**400]
)
def test_invalid_cost_parameters_fail_clearly(monkeypatch, name, value):
    with pytest.raises(ValueError, match=name):
        run_probe(monkeypatch, [SIGNAL], config=fixed_config(**{name: value}))


@pytest.mark.parametrize(
    "patch",
    [
        {"version": "v2"},
        {"version": None},
        {"version": []},
        {"spread_bps": 10_000, "slippage_bps": 5000},
        {"commission_bps": 10_000},
        {"latency_bars": 1},
        {"funding": []},
        {"funding": [{"timestamp_ms": START_MS, "rate": 0.001, "mark_price": 100}]},
        {"funding": [{"timestamp_ms": START_MS, "rate": -0.001, "mark_price": 100}]},
        {"funding": [{"timestamp_ms": START_MS, "rate": 0, "mark_price": 100}]},
        {"participation": 0.1},
        {"maker_fee_bps": 2},
        {"fee_policy": "maker_taker"},
        {"version": "legacy_v1", "spread_bps": 1},
    ],
)
def test_unknown_unsupported_and_contradictory_models_are_rejected(monkeypatch, patch):
    with pytest.raises(ValueError, match="execution_model"):
        run_probe(monkeypatch, [SIGNAL], config=fixed_config(**patch))


@pytest.mark.parametrize("key", ["version", "spread_bps", "slippage_bps", "commission_bps"])
def test_versioned_model_has_no_implicit_numeric_defaults(monkeypatch, key):
    config = fixed_config()
    del config["execution_model"][key]
    with pytest.raises(ValueError, match="execution_model"):
        run_probe(monkeypatch, [SIGNAL], config=config)


@pytest.mark.parametrize(
    "patch",
    [
        {"commission": 0.1},
        {"taker_fee_bps": 4},
        {"unknown": 1},
        {"execution_mode": "live"},
        {"exchange_type": "inverse"},
        {"exchange": ""},
        {"execution_model": None},
        {"execution_model": []},
    ],
)
def test_versioned_envelope_rejects_ambiguous_values(monkeypatch, patch):
    config = fixed_config()
    config.update(patch)
    with pytest.raises(ValueError, match="execution"):
        run_probe(monkeypatch, [SIGNAL], config=config)


@pytest.mark.parametrize(
    "row",
    [
        [100, float("nan"), 99, 100, 1000],
        [100, 101, 99, float("inf"), 1000],
        [0, 101, 0, 100, 1000],
        [100, 98, 99, 100, 1000],
        [100, 101, 102, 100, 1000],
        [100, 101, 99, 100, -1],
    ],
)
def test_fixed_model_rejects_invalid_observed_candles(monkeypatch, row):
    with pytest.raises(ValueError, match="OHLCV"):
        run_probe(monkeypatch, [SIGNAL, row], config=fixed_config())


@pytest.mark.parametrize("capital", [0, -1, float("nan"), float("inf"), True])
def test_fixed_model_rejects_invalid_capital(monkeypatch, capital):
    with pytest.raises(ValueError, match="initial_capital"):
        run_probe(monkeypatch, [SIGNAL], config=fixed_config(), capital=capital)


@pytest.mark.parametrize(
    "timestamps", [[START_MS, START_MS], [START_MS + 1, START_MS], [START_MS, START_MS + 0.5]]
)
def test_fixed_model_rejects_duplicate_unsorted_or_fractional_timestamps(timestamps):
    candles = np.array([[ts, *SIGNAL] for ts in timestamps], dtype=float)
    with pytest.raises(ValueError, match="OHLCV"):
        backtest_runner.create_engine().run(
            EngineRunSpec(
                graph={},
                feeds={"1h": candles},
                initial_capital=10000,
                execution_config=fixed_config(),
            )
        )


def test_event_consumer_cannot_rewrite_resolved_assumptions(monkeypatch):
    def corrupt_event(event):
        if event["event_type"] == "SESSION_START":
            event["payload"]["execution_model"]["resolved_config"]["execution_model"][
                "slippage_bps"
            ] = 9000

    result, _ = run_probe(
        monkeypatch, [SIGNAL, SIGNAL], config=fixed_config(), on_event=corrupt_event
    )
    assert result.metrics["execution_model"]["resolved_config"] == fixed_config(leverage=1.0)


@pytest.mark.parametrize("size", [float("nan"), float("inf"), 1e308])
def test_fixed_model_rejects_nonfinite_order_arithmetic(monkeypatch, size):
    with pytest.raises(ValueError, match="execution.*finite"):
        run_probe(
            monkeypatch, [SIGNAL, SIGNAL], config=fixed_config(), size=size, direction="short"
        )


@pytest.mark.parametrize(
    "config",
    [
        {"slippage_bps": 10},
        {"execution_modle": {}},
        {"commission": -1},
        {"taker_fee_bps": float("nan")},
        {"maker_fee_bps": float("inf")},
        {"taker_fee": True},
        {"execution_mode": "live"},
    ],
)
def test_unversioned_malformed_costs_are_not_silently_ignored(monkeypatch, config):
    with pytest.raises(ValueError, match="execution"):
        run_probe(monkeypatch, [SIGNAL], config=config)


@pytest.mark.parametrize(
    "config",
    [
        {"fee_source": "typo"},
        {"paper_commission_side": "maker"},
        {"exchange": []},
        {"exchange": ""},
        {"exchange_type": "inverse"},
    ],
)
def test_unknown_legacy_setting_values_are_rejected(monkeypatch, config):
    with pytest.raises(ValueError, match="execution"):
        run_probe(monkeypatch, [SIGNAL], config=config)


def test_metadata_identifies_unreleased_source_code(monkeypatch):
    result, _ = run_probe(monkeypatch, [SIGNAL], config=fixed_config())
    fingerprint = result.metrics["execution_model"].get("implementation_sha256", "")
    assert len(fingerprint) == 64
    assert set(fingerprint) <= set("0123456789abcdef")


@pytest.mark.parametrize(
    "spread,slippage,expected_spread,expected_slippage",
    [
        (20, 0, 0.2, 0),
        (0, 10, 0, 0.2),
        (0, 0, 0, 0),
        (30, 5, 0.3, 0.1),
    ],
)
def test_cost_components_can_be_used_independently(
    monkeypatch, spread, slippage, expected_spread, expected_slippage
):
    result, _ = run_probe(
        monkeypatch,
        [SIGNAL, SIGNAL],
        config=fixed_config(spread_bps=spread, slippage_bps=slippage, commission_bps=13),
    )
    audit = result.metrics["execution_costs"]
    assert audit["spread_cost"] == pytest.approx(expected_spread)
    assert audit["slippage_cost"] == pytest.approx(expected_slippage)
    fill = audit["fills"][0]
    assert fill["commission"] == pytest.approx(2 * fill["fill_price"] * 0.0013)


def test_actual_fill_cash_rejection_does_not_accrue_hypothetical_costs(monkeypatch):
    result, _ = run_probe(monkeypatch, [SIGNAL, SIGNAL], config=fixed_config(), capital=200.09)
    assert result.metrics["final_capital"] == 200.09
    assert result.metrics["execution_costs"]["fills"] == []


def test_fixed_model_never_uses_network(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail("a historical backtest attempted network access")

    monkeypatch.setattr(socket, "socket", forbidden)
    result, _ = run_probe(
        monkeypatch, [SIGNAL, SIGNAL, [85, 89, 80, 86, 1000]], config=fixed_config()
    )
    assert result.metrics["final_capital"] == pytest.approx(9969.111976)


def test_fill_ledger_timestamps_are_utc_independent_of_host_timezone(monkeypatch):
    import time

    if not hasattr(time, "tzset"):
        pytest.skip("host does not expose tzset")
    try:
        with monkeypatch.context() as local:
            local.setenv("TZ", "Europe/Kyiv")
            time.tzset()
            timestamps = []
            result, events = run_probe(
                local, [SIGNAL, SIGNAL], config=fixed_config(), observed_timestamps=timestamps
            )
            assert (
                result.metrics["execution_costs"]["fills"][0]["timestamp_ms"]
                == START_MS + 3_600_000
            )
            assert (
                next(e for e in events if e["event_type"] == "ORDER_FILLED")["timestamp_ms"]
                == START_MS + 3_600_000
            )
            assert timestamps == [START_MS, START_MS + 3_600_000]
    finally:
        time.tzset()


@pytest.mark.parametrize(
    "config",
    [
        {"commission": 1e308},
        {"taker_fee": 100},
        {"taker_fee_bps": 10000},
        {
            "exchange": "binance",
            "exchange_type": "inverse",
            "execution_model": {"version": "legacy_v1", "commission_bps": 4},
        },
    ],
)
def test_every_accepted_legacy_snapshot_must_be_replayable(monkeypatch, config):
    with pytest.raises(ValueError, match="execution"):
        run_probe(monkeypatch, [SIGNAL], config=config)


def test_short_bracket_survives_full_cash_notional(monkeypatch):
    # Short 100 units at reference 100 on capital 10000 (100% notional), stop 101,
    # target 99. Before the fix Backtrader margin-rejects the take-profit during
    # check_submitted and the OCO cancels the stop, leaving a naked short.
    rows = [
        SIGNAL,
        [100, 100.2, 99.8, 100, 1000],
        [100, 100.2, 98.5, 99, 1000],
        [99, 99.5, 98.5, 99, 1000],
    ]
    result, events = run_probe(
        monkeypatch,
        rows,
        config={"exchange": "binance", "exchange_type": "future"},
        direction="short",
        entry=100.0,
        stop=101.0,
        target=99.0,
        size=100.0,
    )
    assert [trade["exit_reason"] for trade in result.trades] == ["Take Profit"]
    assert result.metrics["total_trades"] == 1
    assert [e["event_type"] for e in events].count("TRADE_CLOSED") == 1


LEVERAGE_ROWS = [
    SIGNAL,
    [100, 100.5, 99.5, 100, 1000],
    [100, 101, 99.5, 100.5, 1000],
    [104, 105, 103, 104, 1000],
    [108, 108.5, 107.5, 108, 1000],
]


def test_v1_accepts_optional_leverage_and_reports_it():
    model = resolve_execution_model(fixed_config(leverage=5.0))
    assert model.leverage == 5.0
    assert model.as_config()["execution_model"]["leverage"] == 5.0
    assert resolve_execution_model(fixed_config()).leverage == 1.0


@pytest.mark.parametrize("bad", [0.5, 0, -1, 126, True, "5", float("nan")])
def test_v1_rejects_invalid_leverage(bad):
    with pytest.raises(ValueError, match="leverage"):
        resolve_execution_model(fixed_config(leverage=bad))


def test_leverage_five_long_fills_two_times_cash_and_reconciles(monkeypatch):
    result, events = run_probe(
        monkeypatch,
        LEVERAGE_ROWS,
        config=fixed_config(spread_bps=0.0, slippage_bps=0.0, leverage=5.0),
        entry=100.0,
        stop=90.0,
        target=108.0,
        size=200.0,
    )
    assert result.metrics["total_trades"] == 1
    assert result.metrics["final_capital"] == pytest.approx(11583.36, abs=1e-6)
    assert result.metrics["execution_model"]["assumptions"]["leverage"] == 5.0
    assert (
        result.metrics["execution_model"]["assumptions"]["accounting"]
        == "linear_cash_leverage_margin"
    )
    # Equity is cash plus position value: bar after the fill at 100 -> 10000 - 8;
    # the bar closing at 104 -> 10000 - 8 + 200 * 4. A margin-marked mode would
    # report 10952 here and must not be used.
    assert result.equity_curve[1]["equity"] == pytest.approx(9992.0, abs=1e-6)
    assert result.equity_curve[3]["equity"] == pytest.approx(10792.0, abs=1e-6)


def test_leverage_five_short_reconciles(monkeypatch):
    result, _ = run_probe(
        monkeypatch,
        LEVERAGE_ROWS,
        config=fixed_config(spread_bps=0.0, slippage_bps=0.0, leverage=5.0),
        direction="short",
        entry=100.0,
        stop=108.0,
        target=92.0,
        size=200.0,
    )
    assert result.metrics["total_trades"] == 1
    assert result.metrics["final_capital"] == pytest.approx(8383.36, abs=1e-6)
    assert result.equity_curve[3]["equity"] == pytest.approx(9192.0, abs=1e-6)


def test_short_beyond_leverage_is_rejected_by_the_adapter(monkeypatch):
    # Backtrader never cash-checks a short (shortcash credits the proceeds), so
    # the symmetric rule must come from the adapter: 60000 / 5 > 10000.
    result, events = run_probe(
        monkeypatch,
        LEVERAGE_ROWS,
        config=fixed_config(spread_bps=0.0, slippage_bps=0.0, leverage=5.0),
        direction="short",
        entry=100.0,
        stop=108.0,
        target=92.0,
        size=600.0,
    )
    rejected = [e for e in events if e["event_type"] == "ORDER_REJECTED"]
    assert rejected and rejected[0]["payload"]["reason"] == "insufficient_margin"
    assert not any(e["event_type"] == "ORDER_PLACED" for e in events)
    assert result.metrics["total_trades"] == 0
    assert result.metrics["final_capital"] == pytest.approx(10_000.0)


def test_insufficient_margin_emits_order_rejected_and_continues(monkeypatch):
    result, events = run_probe(
        monkeypatch,
        LEVERAGE_ROWS,
        config=fixed_config(spread_bps=0.0, slippage_bps=0.0, leverage=1.0),
        entry=100.0,
        stop=90.0,
        target=108.0,
        size=200.0,
    )
    rejected = [e for e in events if e["event_type"] == "ORDER_REJECTED"]
    assert rejected and rejected[0]["payload"]["reason"] == "insufficient_margin"
    assert result.metrics["total_trades"] == 0
    assert result.metrics["final_capital"] == pytest.approx(10_000.0)
    assert not any(e["event_type"] == "TRADE_OPENED" for e in events)


def test_backtrader_margin_rejection_emits_order_rejected(monkeypatch):
    # The legacy model has no adapter-side affordability rule, so this reaches
    # Backtrader's own cash check and arrives as a Margin notification.
    result, events = run_probe(
        monkeypatch,
        LEVERAGE_ROWS,
        config={"exchange": "binance", "exchange_type": "future"},
        entry=100.0,
        stop=90.0,
        target=108.0,
        size=200.0,
    )
    rejected = [e for e in events if e["event_type"] == "ORDER_REJECTED"]
    assert rejected and rejected[0]["payload"]["reason"] == "insufficient_margin"
    assert rejected[0]["payload"]["direction"] == "long"
    assert rejected[0]["payload"]["size"] == pytest.approx(200.0)
    assert result.metrics["total_trades"] == 0


def test_take_profit_pays_full_adverse_cost_not_limit_cap(monkeypatch):
    rows = [SIGNAL, [100, 100.5, 99.5, 100, 1000], [110, 121, 109, 120, 1000]]
    result, _ = run_probe(
        monkeypatch,
        rows,
        config=fixed_config(),
        entry=100.0,
        stop=90.0,
        target=120.0,
        size=2.0,
    )
    trade = result.trades[0]
    assert trade["entry_price"] == pytest.approx(100.20)
    assert trade["exit_price"] == pytest.approx(119.76)
    assert trade["commission"] == pytest.approx(0.08016 + 0.095808)
    assert result.metrics["final_capital"] == pytest.approx(10038.944032, abs=1e-9)
    fills = result.metrics["execution_costs"]["fills"]
    assert fills[1]["koval_role"] == "take_profit"
    assert fills[1]["spread_cost"] == pytest.approx(0.24)
    assert fills[1]["slippage_cost"] == pytest.approx(0.24)


def test_v1_event_ledger_and_strategy_timestamps_agree_at_millisecond_offsets(monkeypatch):
    offset = 2
    seen = []
    rows = [SIGNAL, [100, 100.5, 99.5, 100, 1000], [85, 86, 84, 85, 1000]]
    events_ts = {}

    def capture(event):
        if event["event_type"] == "ORDER_FILLED":
            events_ts["fill"] = event["timestamp_ms"]

    result, _ = run_probe(
        monkeypatch,
        rows,
        config=fixed_config(),
        on_event=capture,
        observed_timestamps=seen,
        entry=100.0,
        stop=90.0,
        target=120.0,
        size=2.0,
        start_ms=START_MS + offset,
    )
    ledger = result.metrics["execution_costs"]["fills"][0]["timestamp_ms"]
    assert ledger == START_MS + offset + 3_600_000
    assert events_ts["fill"] == ledger
    assert seen[1] == ledger
