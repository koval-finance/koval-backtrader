# SPDX-License-Identifier: GPL-3.0-or-later
"""Signal intents use the next open and the broker's actual inventory ledger."""

from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pytest
from koval.engine.backtest_engine import EngineRunSpec, ProtocolVersionError
from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup

from koval_backtrader import backtest_runner
from tests.test_realistic_evidence import START, STEP, instrument

QUIET = (100, 101, 99, 100, 100)
MARKET = dict(exchange="binance", market="spot", canonical_symbol="BTCUSDT", contract_type="spot")


def configuration(*, version="ohlcv_realistic_v2", costs=None, evidence=None, market=None):
    result = dict(
        exchange="binance",
        exchange_type="spot",
        market=market or MARKET,
        execution_model=dict(
            version=version, commission_bps=0, spread_bps=0, slippage_bps=0, leverage=1
        )
        | (costs or {}),
        **(evidence or {}),
    )
    if version == "legacy_v1":
        result["execution_model"] = dict(version=version, commission_bps=0)
    return result


def exit_graph(mode="disabled", *, exit=True):
    graph = {
        "blocks": [
            {"id": "sig", "type": "fact.every_bar", "params": {"direction": "bullish"}},
            {"id": "agreement", "type": "interp.confluence_and", "params": {"min_signals": 1}},
            {
                "id": "gate",
                "type": "interp.direction_gate",
                "params": {"allow_long": True, "allow_short": False},
            },
            {
                "id": "order",
                "type": "exec.order_constructor",
                "params": {
                    "sl_pct": 20,
                    "risk_pct": 0.2,
                    "risk_reward": 10,
                    "entry_type": "market",
                    **({"take_profit_mode": mode} if mode else {}),
                },
            },
        ],
        "connections": [
            {"from": "sig", "from_port": "event", "to": "agreement", "to_port": "events"},
            {"from": "agreement", "from_port": "agreement", "to": "gate", "to_port": "agreement"},
            {"from": "gate", "from_port": "intent", "to": "order", "to_port": "intent"},
        ],
    }
    if exit:
        graph["blocks"] += [
            {
                "id": "ema",
                "type": "policy.ema_trend",
                "params": {"period": 2, "direction": "bearish"},
            },
            {"id": "close", "type": "exec.position_exit", "params": {}},
        ]
        graph["connections"].append(
            {"from": "ema", "from_port": "policy", "to": "close", "to_port": "condition"}
        )
    return graph


def unsupported_position_features():
    """Older supported engines must refuse the new graph before session effects."""
    from koval_backtrader.position_exit import engine_contract_available

    if engine_contract_available():
        return False
    spec = EngineRunSpec(
        graph=exit_graph(),
        feeds={"1m": np.array([[START, *QUIET]])},
        initial_capital=10000,
        execution_config=configuration(),
    )
    events = []
    with pytest.raises(ProtocolVersionError, match="capabilities"):
        backtest_runner.create_engine().run(spec, events.append)
    assert events == []
    return True


def run_exit_probe(
    monkeypatch,
    rows,
    *,
    mode="bracket",
    target=120,
    stop=80,
    size=1,
    signal_bars=(2,),
    entry_bars=(1,),
    request_position=None,
    costs=None,
    evidence=None,
    stop_updates=None,
    target_updates=None,
    duplicate_notify=False,
    contract=None,
):
    calls, seen = [], []
    entry_context = {
        "version": "koval_trade_decision_context_v1",
        "status": "partial",
        "nodes": [{"node_id": "entry", "values": {"close": 100}}],
    }

    class Probe(DeclarativeStrategy):
        def __init__(self):
            super().__init__()
            self.identity = None
            self.request = None

        def on_bar(self):
            calls.append(self.bar_index)
            seen.append(
                (self.bar_index, self.position_size, self.position_direction, self.identity)
            )
            self.request = None
            if self.bar_index in signal_bars and self.identity is not None:
                self.request = SimpleNamespace(
                    symbol="BTCUSDT",
                    position_id=self.identity if request_position is None else request_position,
                    position_side="long",
                    quantity_fraction=1.0,
                    order_type="market",
                    reason="signal",
                    bar_index=self.bar_index,
                    timestamp_ms=self.timestamp_ms,
                    source_node_id="close",
                    decision_context={
                        "version": "koval_trade_decision_context_v1",
                        "status": "recorded",
                        "nodes": [{"node_id": "ema", "values": {"close": self.close, "ema": 105}}],
                    },
                )

        def get_position_exit_request(self):
            return deepcopy(self.request)

        def should_long(self):
            return self.bar_index in entry_bars

        def go_long(self):
            # Test fixtures also exercise legacy engines with no new dataclass field.
            kwargs = (
                {"take_profit_mode": mode}
                if "take_profit_mode" in TradeSetup.__dataclass_fields__
                else {}
            )
            setup = TradeSetup(
                "long",
                100,
                stop,
                None if mode == "disabled" else target,
                size,
                "market",
                decision_context=deepcopy(entry_context),
                **kwargs,
            )
            if not kwargs:
                setup.take_profit_mode = mode
            return setup

        def on_open_position(self, trade_id, setup):
            self.identity = trade_id

        def on_close_position(self, trade_id, result):
            self.identity = None

        def on_sl_update(self, trade_id):
            return (stop_updates or {}).get(self.bar_index)

        def on_tp_update(self, trade_id):
            return (target_updates or {}).get(self.bar_index)

    monkeypatch.setattr(backtest_runner, "assemble_from_graph", lambda _: Probe())
    if duplicate_notify:
        from koval_backtrader.bt_adapter import BTStrategyAdapter

        original = BTStrategyAdapter.notify_order

        def notify_twice(self, order):
            original(self, order)
            original(self, order)

        monkeypatch.setattr(BTStrategyAdapter, "notify_order", notify_twice)
    spec = EngineRunSpec(
        graph={},
        feeds={"1m": np.array([[START + i * STEP, *r] for i, r in enumerate(rows)], dtype=float)},
        initial_capital=10000,
        execution_config=configuration(costs=costs, evidence=evidence),
    )
    if contract is not None:
        spec.runtime_contract = contract
    events = []
    result = backtest_runner.create_engine().run(spec, events.append)
    return result, events, calls, seen


def exits(result):
    return [f for f in result.metrics["execution_audit"]["fills"] if f["role"] == "exit"]


def test_next_open_reference_and_exact_one_coin_costs(monkeypatch):
    # A cheaper entry gap provides enough stop-risk budget to fill exactly one coin.
    if unsupported_position_features():
        return
    result, _, calls, seen = run_exit_probe(
        monkeypatch,
        [QUIET, (99, 101, 98, 100, 100), (90, 91, 89, 90, 100)],
        costs=dict(commission_bps=10, spread_bps=20, slippage_bps=10),
    )
    fill = exits(result)[0]
    assert fill["reference_price"] == 90
    assert fill["fill_price"] == pytest.approx(89.82)
    assert fill["size"] == 1
    assert fill["commission"] == pytest.approx(0.08982)
    assert fill["timestamp_ms"] == START + 2 * STEP
    assert result.trades[0]["reason"] == "signal"
    assert calls == [1, 2, 3]
    assert seen[1] == (2, 1, "long", 1)
    assert abs(result.metrics["execution_costs"]["reconciliation_error"]) < 1e-8


@pytest.mark.parametrize(
    "row,stop,target,reason,reference",
    [
        ((100, 105, 90, 100, 100), 95, 120, "signal", 100),
        ((90, 105, 85, 100, 100), 95, 120, "stop_loss", 90),
        ((125, 130, 90, 100, 100), 95, 120, "take_profit", 125),
        ((125, 130, 90, 100, 100), 95, None, "signal", 125),
    ],
)
def test_open_priority_does_not_consult_future_low(
    monkeypatch, row, stop, target, reason, reference
):
    if unsupported_position_features():
        return
    result, _, _, _ = run_exit_probe(
        monkeypatch,
        [QUIET, QUIET, row],
        stop=stop,
        target=target,
        mode="disabled" if target is None else "bracket",
    )
    fills = exits(result)
    assert len(fills) == 1
    assert fills[0]["koval_role"] == reason
    assert fills[0]["reference_price"] == reference
    assert fills[0]["size"] == 1


def test_partial_signal_then_stop_preserves_reasons_and_shared_budget(monkeypatch):
    if unsupported_position_features():
        return
    result, _, _, seen = run_exit_probe(
        monkeypatch,
        [QUIET, (99, 101, 98, 100, 100), (100, 101, 99, 100, 4), (85, 101, 80, 95, 6)],
        stop=90,
        mode="disabled",
        signal_bars=(2, 3, 4),
        duplicate_notify=True,
        evidence={
            "execution_proxy": ExecutionProxyConfig(Decimal(".1"), "carry", ExecutionLatency())
        },
        costs={"commission_bps": 10},
    )
    fills = exits(result)
    assert [f["koval_role"] for f in fills] == ["signal", "stop_loss"]
    assert fills[0]["size"] == pytest.approx(0.4)
    assert sum(f["size"] for f in fills) == pytest.approx(result.trades[0]["size"])
    assert fills[1]["size"] == pytest.approx(0.6)
    assert result.trades[0]["reason"] == "sl"
    assert result.trades[0]["fully_signal_closed"] is False
    assert result.metrics["research"]["fully_signal_closed_trades"] == 0
    orders = [o for o in result.metrics["execution_audit"]["orders"] if o["role"] == "signal"]
    assert len(orders) == 1
    assert seen[2][1] == pytest.approx(result.trades[0]["size"] - 0.4)
    assert result.trades[0]["commission"] == pytest.approx(
        sum(f["commission"] for f in result.metrics["execution_audit"]["fills"])
    )


def test_accepting_close_cancels_pending_entry_and_resizes_stop(monkeypatch):
    if unsupported_position_features():
        return
    result, _, _, _ = run_exit_probe(
        monkeypatch,
        [QUIET, (100, 101, 99, 100, 4), (100, 101, 99, 100, 2), QUIET],
        mode="disabled",
        size=1,
        signal_bars=(2, 3),
        evidence={
            "execution_proxy": ExecutionProxyConfig(Decimal(".1"), "carry", ExecutionLatency())
        },
    )
    fills = result.metrics["execution_audit"]["fills"]
    assert [(f["role"], f["size"]) for f in fills] == [("entry", 0.4), ("exit", 0.2), ("exit", 0.2)]
    assert result.trades[0]["size"] == 0.4
    assert result.trades[0]["fully_signal_closed"] is True
    assert result.metrics["research"]["fully_signal_closed_trades"] == 1
    entry = next(o for o in result.metrics["execution_audit"]["orders"] if o["role"] == "entry")
    assert entry["status"] == "canceled"


@pytest.mark.parametrize("mode", ["bracket", "disabled"])
def test_pending_close_cooldown_includes_completion_by_stop(monkeypatch, mode):
    if unsupported_position_features():
        return
    result, events, _, _ = run_exit_probe(
        monkeypatch,
        [QUIET, QUIET, (100, 101, 99, 100, 4), (85, 88, 80, 85, 100), QUIET, QUIET],
        mode=mode,
        stop=90,
        entry_bars=(1, 4, 5),
        signal_bars=(2, 3),
        evidence={
            "execution_proxy": ExecutionProxyConfig(Decimal(".1"), "carry", ExecutionLatency())
        },
    )
    placed = [
        e["bar_index"]
        for e in events
        if e["event_type"] == "ORDER_PLACED" and e["payload"].get("entry_type") == "market"
    ]
    assert placed == [1, 5]
    assert result.trades[0]["reason"] == "sl"


@pytest.mark.parametrize("mode", ["bracket", "disabled"])
def test_ordinary_stop_keeps_same_bar_reentry(monkeypatch, mode):
    if unsupported_position_features():
        return
    _, events, _, _ = run_exit_probe(
        monkeypatch,
        [QUIET, QUIET, (85, 88, 80, 85, 100), QUIET],
        mode=mode,
        stop=90,
        signal_bars=(),
        entry_bars=(1, 3),
    )
    assert [e["bar_index"] for e in events if e["event_type"] == "ORDER_PLACED"] == [1, 3]


def test_stale_position_request_cannot_close_replacement(monkeypatch):
    if unsupported_position_features():
        return
    result, _, _, _ = run_exit_probe(
        monkeypatch,
        [QUIET, QUIET, (85, 88, 80, 85, 100), QUIET, QUIET, QUIET],
        signal_bars=(4, 5),
        entry_bars=(1, 3),
        request_position=1,
        stop=90,
    )
    assert len(result.trades) == 1
    assert not any(f["koval_role"] == "signal" for f in exits(result))
    assert result.metrics["research"]["final_open_position"]["quantity"] > 0


def test_disabled_target_survives_rounding_stop_update_and_terminal(monkeypatch):
    if unsupported_position_features():
        return
    result, _, _, _ = run_exit_probe(
        monkeypatch,
        [QUIET, QUIET, (100, 300, 99, 120, 100)],
        mode="disabled",
        stop_updates={2: 90.04},
        target_updates={2: 110},
        signal_bars=(),
        evidence={"instrument_specs": (instrument(market="spot"),)},
    )
    assert not exits(result)
    orders = result.metrics["execution_audit"]["orders"]
    assert not any(o["role"] == "take_profit" for o in orders)
    assert result.metrics["research"]["final_open_position"]["quantity"] == 1
    assert (
        next(o for o in orders if o["role"] == "stop_loss" and o["status"] == "accepted")[
            "requested_price"
        ]
        == 90
    )


def test_pending_terminal_intent_is_marked_and_replays_without_fill(monkeypatch):
    if unsupported_position_features():
        return
    kwargs = dict(mode="disabled", signal_bars=(2,))
    first, _, _, _ = run_exit_probe(monkeypatch, [QUIET, QUIET], **kwargs)
    second, _, _, _ = run_exit_probe(monkeypatch, [QUIET, QUIET], **kwargs)
    assert not first.trades and not exits(first)
    audit = first.metrics["execution_audit"]
    assert audit["pending_position_exit"]["request"]["position_id"] == 1
    assert audit["pending_position_exit"]["status"] in {"accepted", "submitted", "requested"}
    assert first.metrics["research"]["final_open_position"]["quantity"] == 1
    assert first.metrics == second.metrics
    assert first.equity_curve == second.equity_curve


def test_exit_evidence_links_decision_order_fills_without_replacing_entry(monkeypatch):
    if unsupported_position_features():
        return
    result, _, _, _ = run_exit_probe(monkeypatch, [QUIET, QUIET, (90, 91, 89, 90, 100)])
    trade = result.trades[0]
    context = trade["exit_decision_context"]
    assert context["nodes"][0]["values"] == {"close": 100, "ema": 105}
    assert context["signal_bar_open_ms"] == START + STEP
    assert context["decision_timestamp_ms"] == START + 2 * STEP
    assert context["position_id"] == 1
    assert context["source_node_id"] == "close"
    assert trade["decision_context"]["nodes"][0]["node_id"] == "entry"
    audit = result.metrics["execution_audit"]
    order = next(o for o in audit["orders"] if o["role"] == "signal")
    assert context["exit_order_id"] == order["order_id"] == trade["exit_order_id"]
    fill = exits(result)[0]
    assert fill["order_id"] == order["order_id"]
    assert fill["decision_id"] == context["decision_id"] != trade["decision_id"]
    assert trade["exit_fill_reasons"] == [
        {
            "fill_id": fill["fill_id"],
            "order_id": fill["order_id"],
            "reason": "signal",
            "quantity": 1,
        }
    ]


@pytest.mark.parametrize("version", ["legacy_v1", "ohlcv_fixed_v1"])
@pytest.mark.parametrize("mode,signal", [("disabled", False), (None, True)])
def test_unsupported_profiles_reject_new_graph_before_session(version, mode, signal):
    spec = EngineRunSpec(
        graph=exit_graph(mode, exit=signal),
        feeds={"1m": np.array([[START, *QUIET], [START + STEP, *QUIET]])},
        initial_capital=10000,
        execution_config=configuration(version=version),
    )
    events = []
    with pytest.raises(ProtocolVersionError, match="capabilit|position_exit|optional_take_profit"):
        backtest_runner.create_engine().run(spec, events.append)
    assert events == []


def test_actual_graph_ema_exit_is_cached_once_and_exported():
    # This graph is independent from probes: exercises the actual MIT terminal/hook.
    if unsupported_position_features():
        return
    from koval.strategy.graph.entities import PositionExitRequest

    assert PositionExitRequest
    spec = EngineRunSpec(
        graph=exit_graph(),
        feeds={
            "1m": np.array(
                [
                    [START + i * STEP, *r]
                    for i, r in enumerate(
                        [QUIET, QUIET, (100, 101, 94, 95, 100), (90, 91, 89, 90, 100)]
                    )
                ]
            )
        },
        initial_capital=10000,
        execution_config=configuration(),
    )
    result = backtest_runner.create_engine().run(spec)
    assert result.trades[0]["reason"] == "signal"
    assert result.trades[0]["take_profit"] is None
    context = result.trades[0]["exit_decision_context"]
    assert context["source_node_id"] == "close"
    assert context["position_id"] == 1
    assert exits(result)[0]["reference_price"] == 90
    assert {"position_exit_v1", "optional_take_profit_v1"} <= set(
        result.metrics["execution_model"]["negotiated_capabilities"]["features"]
    )


@pytest.mark.parametrize("stop,message", [(79, "widen"), (float("nan"), "finite"), (0, "finite")])
def test_stop_only_updates_reject_widening_and_nonfinite_prices(monkeypatch, stop, message):
    if unsupported_position_features():
        return
    with pytest.raises(ValueError, match=message):
        run_exit_probe(
            monkeypatch,
            [QUIET, QUIET, QUIET],
            mode="disabled",
            signal_bars=(),
            stop_updates={2: stop},
        )


def test_partial_signal_resizes_real_stop_and_pending_intent(monkeypatch):
    if unsupported_position_features():
        return
    result, _, _, _ = run_exit_probe(
        monkeypatch,
        [QUIET, QUIET, (100, 101, 99, 100, 4)],
        mode="disabled",
        evidence={
            "execution_proxy": ExecutionProxyConfig(Decimal(".1"), "carry", ExecutionLatency())
        },
    )
    audit = result.metrics["execution_audit"]
    stop = next(o for o in audit["orders"] if o["role"] == "stop_loss")
    signal = next(o for o in audit["orders"] if o["role"] == "signal")
    assert stop["remaining_quantity"] == pytest.approx(0.6)
    assert signal["remaining_quantity"] == pytest.approx(0.6)
    assert audit["pending_position_exit"]["status"] == "partial"
    assert audit["open_position"]["quantity"] == pytest.approx(0.6)
    terminal = result.metrics["research"]["final_open_position"]
    assert terminal["take_profit_mode"] == "disabled"
    assert terminal["take_profit"] is None
    assert exits(result)[0]["take_profit_mode"] == "disabled"
    assert abs(result.metrics["execution_costs"]["reconciliation_error"]) < 1e-8


@pytest.mark.parametrize("gap", [70, 130])
def test_stop_only_entry_gap_and_intrabar_paths_have_no_target(monkeypatch, gap):
    if unsupported_position_features():
        return
    result, _, _, _ = run_exit_probe(
        monkeypatch, [QUIET, (gap, gap + 1, gap - 1, gap, 100)], mode="disabled", signal_bars=()
    )
    assert not any(o["role"] == "take_profit" for o in result.metrics["execution_audit"]["orders"])
    if gap == 70:
        assert exits(result)[0]["reference_price"] == 70
    else:
        assert not exits(result)


def test_old_none_target_keeps_rr_fallback(monkeypatch):
    if unsupported_position_features():
        return
    result, _, _, _ = run_exit_probe(
        monkeypatch, [QUIET, QUIET, (100, 150, 99, 145, 100)], target=None, signal_bars=()
    )
    assert exits(result)[0]["reference_price"] == 140
    assert exits(result)[0]["koval_role"] == "take_profit"


def test_next_liquid_open_and_configured_latency_are_honored(monkeypatch):
    if unsupported_position_features():
        return
    for proxy, rows, expected_time, expected_price in [
        (
            ExecutionProxyConfig(Decimal(".1"), "carry", ExecutionLatency()),
            [QUIET, QUIET, (90, 91, 89, 90, 0), (95, 96, 94, 95, 100)],
            START + 3 * STEP,
            95,
        ),
        (
            ExecutionProxyConfig(
                Decimal("1"), "carry", ExecutionLatency(decision_to_submission_ms=STEP)
            ),
            [QUIET, QUIET, QUIET, (90, 91, 89, 90, 100), (85, 86, 84, 85, 100)],
            START + 4 * STEP,
            85,
        ),
    ]:
        result, _, _, _ = run_exit_probe(
            monkeypatch,
            rows,
            stop=60,
            signal_bars=(3,) if proxy.latency.decision_to_submission_ms else (2,),
            evidence={"execution_proxy": proxy},
        )
        assert exits(result)[0]["timestamp_ms"] == expected_time
        assert exits(result)[0]["reference_price"] == expected_price


def test_a0_a1_without_target_hits_preserve_trades_equity_and_reentry():
    if unsupported_position_features():
        return
    rows = [
        QUIET,
        QUIET,
        (75, 79, 70, 75, 100),
        (90, 91, 89, 90, 100),
        QUIET,
        (60, 65, 55, 60, 100),
    ]
    results = []
    for mode in (None, "disabled"):
        spec = EngineRunSpec(
            graph=exit_graph(mode, exit=False),
            feeds={"1m": np.array([[START + i * STEP, *r] for i, r in enumerate(rows)])},
            initial_capital=10000,
            execution_config=configuration(),
        )
        results.append(backtest_runner.create_engine().run(spec))
    a0, a1 = results
    assert len(a0.trades) == len(a1.trades) == 2
    assert a0.equity_curve == a1.equity_curve
    for original, disabled in zip(a0.trades, a1.trades, strict=True):
        for key in (
            "entry_price",
            "exit_price",
            "entry_time",
            "exit_time",
            "size",
            "realized_pnl",
            "commission",
            "exit_reason",
        ):
            assert original[key] == disabled[key]
    assert [
        o["submitted_timestamp_ms"]
        for o in a0.metrics["execution_audit"]["orders"]
        if o["role"] == "entry"
    ] == [
        o["submitted_timestamp_ms"]
        for o in a1.metrics["execution_audit"]["orders"]
        if o["role"] == "entry"
    ]


@pytest.mark.parametrize("market", [None, "future", "whitebit"])
def test_new_graph_rejects_unsupported_market_hosts_before_events(market):
    config = configuration()
    if market is None:
        config.pop("market")
    elif market == "future":
        config.update(
            exchange_type="future",
            market=MARKET | {"market": "future", "contract_type": "perpetual"},
        )
    else:
        config.update(exchange="whitebit", market=MARKET | {"exchange": "whitebit"})
    spec = EngineRunSpec(
        graph=exit_graph(),
        feeds={"1m": np.array([[START, *QUIET]])},
        initial_capital=10000,
        execution_config=config,
    )
    events = []
    with pytest.raises(ProtocolVersionError, match="capabilities"):
        backtest_runner.create_engine().run(spec, events.append)
    assert events == []


@pytest.mark.parametrize("policy", ["mark_at_last_close", "flatten_at_last_close"])
def test_stop_only_terminal_policy_preserves_signal_intent_without_signal_fill(monkeypatch, policy):
    if unsupported_position_features():
        return
    contract = dict(
        version="koval_runtime_boundaries_v1",
        warmup_start_ms=START,
        evaluation_start_ms=START,
        evaluation_end_ms=START + 2 * STEP,
        decision_clock="bar_close",
        initial_balance=10000,
        daily_baseline_equity=10000,
        peak_equity=10000,
        end_of_data_policy=policy,
    )
    result, _, _, _ = run_exit_probe(
        monkeypatch, [QUIET, QUIET], mode="disabled", contract=contract
    )
    audit = result.metrics["execution_audit"]
    assert not any(f["koval_role"] == "signal" for f in audit["fills"])
    if policy == "mark_at_last_close":
        assert audit["pending_position_exit"] is not None
        assert result.metrics["research"]["final_open_position"]["take_profit"] is None
        assert result.trades == []
    else:
        assert result.trades[0]["reason"] == "eod"
        assert not result.trades[0]["fully_signal_closed"]
        assert result.trades[0]["take_profit"] is None
        assert audit["pending_position_exit"] is None


def test_active_gap_target_precedes_intrabar_stop_while_signal_latency_pending(monkeypatch):
    if unsupported_position_features():
        return
    result, _, _, _ = run_exit_probe(
        monkeypatch,
        [QUIET, QUIET, QUIET, (125, 130, 90, 100, 100)],
        signal_bars=(3,),
        stop=95,
        target=120,
        evidence={
            "execution_proxy": ExecutionProxyConfig(
                Decimal("1"), "carry", ExecutionLatency(decision_to_submission_ms=STEP)
            )
        },
    )
    fills = exits(result)
    assert len(fills) == 1
    assert fills[0]["koval_role"] == "take_profit"
    assert fills[0]["reference_price"] == 125
    assert result.trades[0]["reason"] == "tp"
    assert not result.trades[0]["fully_signal_closed"]


def test_signal_partial_fill_events_report_delta_price_and_new_fill_ids(monkeypatch):
    if unsupported_position_features():
        return
    result, events, _, _ = run_exit_probe(
        monkeypatch,
        [QUIET, (99, 101, 98, 100, 100), (100, 101, 99, 100, 4), (90, 91, 89, 90, 6)],
        duplicate_notify=True,
        costs={"commission_bps": 10},
        evidence={
            "execution_proxy": ExecutionProxyConfig(Decimal(".1"), "carry", ExecutionLatency())
        },
    )
    fills = exits(result)
    notifications = [
        e["payload"]
        for e in events
        if e["event_type"] == "ORDER_FILLED" and e["payload"].get("reason") == "signal"
    ]
    assert len(notifications) == 2
    assert [n["fill_price"] for n in notifications] == [100, 90]
    assert [n["fill_quantity"] for n in notifications] == [0.4, 0.6]
    assert [n["fill_ids"] for n in notifications] == [[fills[0]["fill_id"]], [fills[1]["fill_id"]]]
    assert notifications[-1]["cumulative_quantity"] == 1
    assert notifications[-1]["cumulative_fill_price"] == 94
    assert [n["commission"] for n in notifications] == pytest.approx([0.04, 0.054])
    assert sum(n["commission"] for n in notifications) == pytest.approx(
        sum(f["commission"] for f in fills)
    )


def test_stop_only_terminal_flatten_order_and_fill_retain_mode(monkeypatch):
    if unsupported_position_features():
        return
    contract = dict(
        version="koval_runtime_boundaries_v1",
        warmup_start_ms=START,
        evaluation_start_ms=START,
        evaluation_end_ms=START + 2 * STEP,
        decision_clock="bar_close",
        initial_balance=10000,
        daily_baseline_equity=10000,
        peak_equity=10000,
        end_of_data_policy="flatten_at_last_close",
    )
    result, _, _, _ = run_exit_probe(
        monkeypatch, [QUIET, QUIET], mode="disabled", signal_bars=(), contract=contract
    )
    audit = result.metrics["execution_audit"]
    order = next(o for o in audit["orders"] if o["role"] == "end_of_data")
    fill = exits(result)[0]
    assert order["take_profit_mode"] == fill["take_profit_mode"] == "disabled"


@pytest.mark.parametrize("mode", ["bracket", "disabled"])
def test_mixed_partial_exit_finishes_at_liquidity_roundoff_boundary(monkeypatch, mode):
    if unsupported_position_features():
        return
    result, _, _, _ = run_exit_probe(
        monkeypatch,
        [QUIET, QUIET, QUIET, (90, 91, 89, 90, 2), (100, 101, 99, 100, 2), (100, 101, 99, 100, 6)],
        mode=mode,
        stop=90,
        target=120,
        signal_bars=(3,),
        evidence={
            "execution_proxy": ExecutionProxyConfig(Decimal(".1"), "carry", ExecutionLatency())
        },
    )
    audit = result.metrics["execution_audit"]
    fills = exits(result)
    assert [(fill["koval_role"], fill["size"]) for fill in fills] == [
        ("stop_loss", pytest.approx(0.2)),
        ("signal", pytest.approx(0.2)),
        ("signal", pytest.approx(0.6)),
    ]
    assert sum(fill["size"] for fill in fills) == pytest.approx(1)
    assert len(result.trades) == 1
    assert result.trades[0]["fully_signal_closed"] is False
    assert result.trades[0]["commission"] == 0
    assert result.trades[0]["reason"] == "signal"
    assert [fill["reason"] for fill in result.trades[0]["exit_fill_reasons"]] == [
        "sl",
        "signal",
        "signal",
    ]
    assert result.metrics["research"]["fully_signal_closed_trades"] == 0
    assert result.metrics["final_capital"] == 9998
    assert result.metrics["execution_costs"]["reconciliation_error"] == 0
    assert audit["open_position"] is None
    assert audit["pending_position_exit"] is None
    assert audit["terminal_account"]["open_positions"] == 0
    signal = next(order for order in audit["orders"] if order["role"] == "signal")
    assert signal["status"] == "completed"
    assert signal["remaining_quantity"] == 0
    assert all(
        order["status"] == "canceled"
        for order in audit["orders"]
        if order["role"] in {"stop_loss", "take_profit"}
    )


@pytest.mark.parametrize("size,last_volume", [(1, 5.999), (1, 0), (0.000001, 0.000005999)])
def test_mixed_partial_exit_keeps_real_residual_beyond_roundoff(monkeypatch, size, last_volume):
    if unsupported_position_features():
        return
    result, _, _, _ = run_exit_probe(
        monkeypatch,
        [
            QUIET,
            QUIET,
            QUIET,
            (90, 91, 89, 90, 2 * size),
            (100, 101, 99, 100, 2 * size),
            (100, 101, 99, 100, last_volume),
        ],
        mode="disabled",
        size=size,
        stop=90,
        signal_bars=(3,),
        evidence={
            "execution_proxy": ExecutionProxyConfig(Decimal(".1"), "carry", ExecutionLatency())
        },
    )
    audit = result.metrics["execution_audit"]
    assert result.trades == []
    expected = 0.6 * size - 0.1 * last_volume
    assert audit["open_position"]["quantity"] > 0
    assert audit["open_position"]["quantity"] == pytest.approx(expected, rel=1e-8, abs=0)
    assert audit["pending_position_exit"]["status"] == "partial"
    assert audit["terminal_account"]["open_positions"] == 1
    signal = next(order for order in audit["orders"] if order["role"] == "signal")
    assert signal["status"] == "partial"
    assert signal["remaining_quantity"] == pytest.approx(expected, rel=1e-8, abs=0)


def test_mixed_partial_exit_roundoff_with_costs_and_signal_latency(monkeypatch):
    if unsupported_position_features():
        return
    result, _, _, _ = run_exit_probe(
        monkeypatch,
        [
            QUIET,
            (99, 101, 98, 100, 100),
            (135, 135, 80, 88, 0),
            (90, 150, 90, 149, 2),
            (110, 150, 50, 62, 2),
            (100, 101, 80, 89, 6),
        ],
        stop=90,
        target=120,
        signal_bars=(2, 3, 4, 5, 6),
        costs={"commission_bps": 10, "spread_bps": 20, "slippage_bps": 10},
        evidence={
            "execution_proxy": ExecutionProxyConfig(
                Decimal(".1"), "carry", ExecutionLatency(decision_to_submission_ms=STEP)
            )
        },
    )
    fills = exits(result)
    assert [fill["size"] for fill in fills] == pytest.approx([0.2, 0.2, 0.6])
    assert [fill["reference_price"] for fill in fills] == [90, 110, 100]
    assert [fill["fill_price"] for fill in fills] == pytest.approx([89.82, 109.78, 99.8])
    assert len(result.trades) == 1
    assert result.trades[0]["commission"] == pytest.approx(0.198998)
    assert result.metrics["final_capital"] == pytest.approx(10000.403002)
    assert result.metrics["execution_costs"]["reconciliation_error"] == pytest.approx(0, abs=1e-8)
    assert result.metrics["execution_audit"]["open_position"] is None
    assert result.metrics["execution_audit"]["pending_position_exit"] is None
