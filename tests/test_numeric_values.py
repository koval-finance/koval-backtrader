# SPDX-License-Identifier: GPL-3.0-or-later
"""Numeric historical conformance through the public plugin, without venue I/O."""

from copy import deepcopy
from importlib.util import find_spec
from types import SimpleNamespace

import backtrader as bt
import numpy as np
import pandas as pd
import pytest
from koval.engine.backtest_engine import EngineRunSpec, ProtocolVersionError, load_backtest_engine

from koval_backtrader import backtest_runner
from koval_backtrader.bt_adapter import BTStrategyAdapter, make_bt_strategy_class
from tests.test_position_exit import configuration

CAPABILITY = "numeric_values_v1"
HAS_VALUES = find_spec("koval.strategy.graph.values") is not None


def graph_for(family="channel", *, exits=True):
    level = {"component": "channel.upper", "period": 2}
    if family == "bb":
        level = {"component": "bb.lower", "period": 3, "std_dev": 1.0}
    blocks = [
        {"id": "price", "type": "source.value", "params": {"component": "close"}},
        {"id": "level", "type": "source.value", "params": level},
        {"id": "cross", "type": "fact.cross", "params": {"direction": "up"}},
        {"id": "agreement", "type": "interp.confluence_and", "params": {}},
        {"id": "gate", "type": "interp.direction_gate", "params": {"allow_short": False}},
        {
            "id": "order",
            "type": "exec.order_constructor",
            "params": {
                "sl_pct": 90,
                "risk_pct": 0.01,
                "risk_reward": 20,
                "entry_type": "market",
            },
        },
    ]
    edges = [
        ("price", "value", "cross", "left"),
        ("level", "value", "cross", "right"),
        ("cross", "event", "agreement", "events"),
        ("agreement", "agreement", "gate", "agreement"),
        ("gate", "intent", "order", "intent"),
    ]
    if exits:
        blocks += [
            {"id": "ma", "type": "source.value", "params": {"component": "ema", "period": 2}},
            {"id": "below", "type": "fact.compare", "params": {"operator": "lt"}},
            {"id": "exit_policy", "type": "policy.from_condition", "params": {}},
            {"id": "exit", "type": "exec.position_exit", "params": {}},
        ]
        edges += [
            ("price", "value", "below", "left"),
            ("ma", "value", "below", "right"),
            ("below", "condition", "exit_policy", "condition"),
            ("exit_policy", "policy", "exit", "condition"),
        ]
    return {
        "blocks": blocks,
        "connections": [
            dict(zip(("from", "from_port", "to", "to_port"), edge, strict=True)) for edge in edges
        ],
    }


def rows_for(closes, *, step=60_000):
    close = np.asarray(closes, dtype=float)
    return np.column_stack(
        (
            np.arange(len(close)) * step,
            close,
            close + 0.5,
            np.maximum(0.1, close - 0.5),
            close,
            np.full(len(close), 100.0),
        )
    )


def spec_for(closes=(10, 10, 10, 12, 8, 8), *, graph=None, explicit=True):
    return EngineRunSpec(
        graph=graph_for() if graph is None else graph,
        feeds={"1m": rows_for(closes)},
        initial_capital=10000,
        execution_config=configuration(costs={"commission_bps": 10}),
        required_execution_capabilities=(CAPABILITY,) if explicit else (),
    )


@pytest.mark.parametrize(
    "family,closes", [("channel", (10, 10, 10, 12, 8, 8)), ("bb", (10, 10, 1, 7, 3, 3))]
)
@pytest.mark.parametrize("explicit", [True, False])
def test_public_numeric_entry_exit_fills_fees_evidence_and_replay(family, closes, explicit):
    spec = spec_for(closes, graph=graph_for(family), explicit=explicit)
    engine = load_backtest_engine()
    if not HAS_VALUES:
        with pytest.raises(ProtocolVersionError, match=CAPABILITY):
            engine.run(spec)
        return
    first = engine.run(spec)
    assert first == engine.run(spec)
    assert len(first.trades) == 1
    trade = first.trades[0]
    assert trade["reason"] == "signal"
    assert trade["fully_signal_closed"] is True
    audit = first.metrics["execution_audit"]
    fills = audit["fills"]
    assert [f["role"] for f in fills] == ["entry", "exit"]
    assert [f["timestamp_ms"] for f in fills] == [240_000, 300_000]
    assert [f["fill_price"] for f in fills] == list(closes[-2:])
    assert fills[0]["size"] == fills[1]["size"]
    assert trade["commission"] == pytest.approx(sum(f["commission"] for f in fills))
    assert all(f["commission"] == pytest.approx(f["size"] * f["fill_price"] * 0.001) for f in fills)
    assert first.metrics["research"]["final_open_position"] is None
    assert abs(first.metrics["execution_costs"]["reconciliation_error"]) < 1e-8
    for name in ("decision_context", "exit_decision_context"):
        context = trade[name]
        assert context["version"] == "koval_trade_decision_context_v1"
        assert context["history_start_ms"] == 0
        assert context["decision_timestamp_ms"] == context["signal_bar_open_ms"] + 60_000
        assert any(n["node_type"] == "source.value" for n in context["nodes"])
    assert CAPABILITY in first.metrics["execution_model"]["negotiated_capabilities"]["features"]


def test_numeric_only_does_not_require_position_exit():
    spec = spec_for(graph=graph_for(exits=False))
    if not HAS_VALUES:
        return
    result = load_backtest_engine().run(spec)
    assert result.metrics["execution_audit"]["fills"]


def test_read_only_discovery_matches_default_budget_and_contract():
    report = load_backtest_engine().numeric_values_capabilities()
    assert report["capability"] == CAPABILITY
    assert report["available"] is HAS_VALUES
    assert report["reason"] == (None if HAS_VALUES else "missing_engine_numeric_contract")
    assert report["history_bars"] == 1000
    assert report["availability_model"] == "historical_ideal_close_v1"
    assert report["supported_modes"] == ["historical"]
    assert report["supported_profiles"] == ["ohlcv_realistic_v2"]
    assert report["supported_timeframes"] == ["1m", "5m", "15m", "1h", "4h", "1d"]
    report["supported_modes"].append("paper")
    assert load_backtest_engine().numeric_values_capabilities()["supported_modes"] == ["historical"]


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize(
    "bad", ["gap", "interior", "alignment", "fraction", "duplicate", "reverse"]
)
def test_numeric_rejects_every_bad_timestamp_before_runtime(monkeypatch, bad, explicit):
    spec = spec_for(explicit=explicit)
    ts = spec.feeds["1m"][:, 0]
    if bad == "gap":
        ts[3:] += 60_000
    elif bad == "interior":
        ts[2] += 1
    elif bad == "alignment":
        ts[:] += 1
    elif bad == "fraction":
        ts[2] += 0.5
    elif bad == "duplicate":
        ts[2] = ts[1]
    else:
        ts[2], ts[3] = ts[3], ts[2]

    def forbidden(*args, **kwargs):
        pytest.fail("refusal must precede runtime construction")

    monkeypatch.setattr(backtest_runner, "assemble_from_graph", forbidden)
    monkeypatch.setattr(backtest_runner, "_feed_from_ndarray", forbidden)
    monkeypatch.setattr(backtest_runner.bt, "Cerebro", forbidden)
    events = []
    with pytest.raises((ValueError, ProtocolVersionError), match="timestamp|contiguous|capabilit"):
        load_backtest_engine().run(spec, events.append)
    assert events == []


@pytest.mark.parametrize("bad", ["legacy", "fixed", "htf", "timeframe", "market", "identity"])
@pytest.mark.parametrize("explicit", [True, False])
def test_unsupported_numeric_hosts_refuse_before_construction(monkeypatch, bad, explicit):
    spec = spec_for(explicit=explicit)
    if bad in {"legacy", "fixed"}:
        spec.execution_config = configuration(
            version="legacy_v1" if bad == "legacy" else "ohlcv_fixed_v1"
        )
    elif bad == "htf":
        spec.feeds["5m"] = spec.feeds["1m"][::5]
    elif bad == "timeframe":
        spec.feeds = {"3m": spec.feeds["1m"]}
    elif bad == "identity":
        spec.execution_config.pop("market")
    else:
        spec.execution_config = configuration(
            market=dict(
                exchange="whitebit", market="spot", canonical_symbol="BTCUSDT", contract_type="spot"
            )
        )
        spec.execution_config["exchange"] = "whitebit"
    monkeypatch.setattr(
        backtest_runner, "assemble_from_graph", lambda _: pytest.fail("constructed")
    )
    with pytest.raises(ProtocolVersionError, match=CAPABILITY):
        load_backtest_engine().run(spec)


def probe_contexts(closes, *, history=1000, volume=True):
    from koval.strategy.base.declarative import DeclarativeStrategy

    seen = []

    class Probe(DeclarativeStrategy):
        def on_bar(self):
            seen.append(deepcopy(vars(self)))

    rows = rows_for(closes)
    frame = pd.DataFrame(
        rows[:, 1:],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.to_datetime(rows[:, 0], unit="ms", utc=True),
    )
    if not volume:
        frame = frame.drop(columns="volume")
    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=frame))
    cerebro.addstrategy(
        make_bt_strategy_class(
            Probe,
            history_bars=history,
            primary_timeframe_ms=60_000,
            execution_features=(CAPABILITY,),
            strategy_config={"symbol": "BTCUSDT", "timeframe": "1m"},
        )
    )
    cerebro.run()
    return seen


def test_retained_history_binding_tracks_small_and_sliding_default_windows():
    for count, budget in [(4, 3), (1003, 1000)]:
        seen = probe_contexts([10] * count, history=budget)
        for i, ctx in enumerate(seen):
            start = max(0, i + 1 - budget)
            assert ctx["history_start_ms"] == start * 60_000
            assert len(ctx["closes"]) == i + 1 - start
            assert ctx["timestamp_ms"] == i * 60_000
            assert ctx["decision_timestamp_ms"] == (i + 1) * 60_000


def test_absent_volume_line_is_missing_and_measured_zero_stays_zero():
    if not HAS_VALUES:
        return
    from koval.strategy.graph.node import BarContext
    from koval.strategy.graph.values import ValueSpecV1, value_at

    ctx = probe_contexts([10] * 4, volume=False)[-1]
    value = value_at(
        BarContext(
            **{k: v for k, v in ctx.items() if k in BarContext.__dataclass_fields__},
            symbol="BTCUSDT",
            timeframe="1m",
        ),
        ValueSpecV1(component="volume"),
    )
    assert value.value is None
    assert value.readiness != "ready"

    # A custom feed can omit the line entirely; public ndarrays cannot.
    class Line:
        def __init__(self, value):
            self.value = value

        def __getitem__(self, index):
            return self.value

        def get(self, **kwargs):
            return [self.value] * kwargs["size"]

    class MissingVolume:
        close = high = low = open = Line(10.0)
        datetime = Line(bt.date2num(pd.Timestamp(0, unit="ms").to_pydatetime()))

        def __len__(self):
            return 4

    brain = SimpleNamespace()
    host = SimpleNamespace(
        _strategy=brain,
        data=MissingVolume(),
        datas=[None],
        params=SimpleNamespace(
            history_bars=3, primary_timeframe_ms=60_000, execution_features=(CAPABILITY,)
        ),
        broker=SimpleNamespace(getvalue=lambda: 10000),
        _account=SimpleNamespace(snapshot=lambda: None),
        position=SimpleNamespace(size=0),
        _safe_timestamp_ms=lambda: 180_000,
    )
    BTStrategyAdapter._inject_state(host)
    assert brain.volumes is None
    assert np.isnan(brain.volume)
    original = probe_contexts([10] * 4)[-1]
    assert original["volumes"].tolist() == [100] * 4
    spec = spec_for(graph=graph_for(exits=False))
    spec.feeds["1m"][:, 5] = 0
    assert load_backtest_engine().run(spec).metrics["execution_audit"]["decisions"]


@pytest.mark.parametrize("malformed", ["absent", "nan"])
def test_public_malformed_volume_refuses(malformed):
    spec = spec_for()
    if malformed == "absent":
        spec.feeds["1m"] = spec.feeds["1m"][:, :5]
    else:
        spec.feeds["1m"][2, 5] = np.nan
    with pytest.raises(ValueError, match="shape|finite"):
        load_backtest_engine().run(spec)


def test_requirement_421_fits_and_over_budget_refuses_before_assembly(monkeypatch):
    if not HAS_VALUES:
        return
    graph = {
        "blocks": [
            {
                "id": "v",
                "type": "source.value",
                "params": {"component": "channel.upper", "period": 400, "offset_bars": 20},
            }
        ],
        "connections": [],
    }
    result = load_backtest_engine().run(spec_for([10] * 422, graph=graph))
    assert result.metrics["run_identity"]["warmup"]["history_bars"] == 1000
    graph["blocks"][0]["params"]["offset_bars"] = 600
    monkeypatch.setattr(
        backtest_runner, "assemble_from_graph", lambda _: pytest.fail("constructed")
    )
    with pytest.raises(ValueError, match="1001.*1000"):
        load_backtest_engine().run(spec_for(graph=graph))


@pytest.mark.parametrize(
    "timeframe,step",
    [
        ("1m", 60_000),
        ("5m", 300_000),
        ("15m", 900_000),
        ("1h", 3_600_000),
        ("4h", 14_400_000),
        ("1d", 86_400_000),
    ],
)
def test_all_advertised_timeframes_bind_declared_symbol_and_clock(timeframe, step):
    if not HAS_VALUES:
        return
    spec = spec_for()
    spec.feeds = {timeframe: rows_for([10, 10, 10, 12, 8, 8], step=step)}
    result = load_backtest_engine().run(spec)
    context = result.trades[0]["decision_context"]
    value = next(
        n["outputs"]["value"] for n in context["nodes"] if n["node_type"] == "source.value"
    )
    assert value["symbol"] == "BTCUSDT" and value["timeframe"] == timeframe
    assert context["decision_timestamp_ms"] == 4 * step


def test_direct_prepared_recorded_derived_equality_and_future_suffix(monkeypatch):
    if not HAS_VALUES:
        return
    from koval.strategy.graph.node import BarContext
    from koval.strategy.graph.series import strategy_indicator_series
    from koval.strategy.graph.values import ValueSpecV1, value_at

    graph = graph_for()
    # Each default-budget prefix includes offsets, warmup, and a sliding recursive seed.
    graph["blocks"].append(
        {
            "id": "lag",
            "type": "source.value",
            "params": {"component": "ema", "period": 2, "offset_bars": 1},
        }
    )
    closes = [10.0] * 1000 + [12.0, 8.0, 8.0]
    spec = spec_for(closes, graph=graph)
    fast = load_backtest_engine().run(spec)
    assemble = backtest_runner.assemble_from_graph

    def scalar(graph):
        strategy = assemble(graph)
        monkeypatch.setattr(type(strategy), "prepare_backtest", None)
        return strategy

    monkeypatch.setattr(backtest_runner, "assemble_from_graph", scalar)
    assert load_backtest_engine().run(spec) == fast
    series = {
        s["node_id"]: s
        for s in strategy_indicator_series(
            graph, spec.feeds["1m"], timeframe="1m", symbol="BTCUSDT", history_bars=1000
        )
    }
    context = fast.trades[0]["decision_context"]
    for node in (n for n in context["nodes"] if n["node_type"] == "source.value"):
        index = node["timestamp_ms"] // 60_000
        rows = spec.feeds["1m"][max(0, index - 999) : index + 1]
        last = rows[-1]
        ctx = BarContext(
            close=last[4],
            high=last[2],
            low=last[3],
            open=last[1],
            volume=last[5],
            closes=rows[:, 4],
            highs=rows[:, 2],
            lows=rows[:, 3],
            opens=rows[:, 1],
            volumes=rows[:, 5],
            timestamp_ms=int(last[0]),
            decision_timestamp_ms=int(last[0]) + 60_000,
            history_start_ms=int(rows[0, 0]),
            bar_index=index + 1,
            symbol="BTCUSDT",
            timeframe="1m",
        )
        actual = node["outputs"]["value"]
        direct = value_at(ctx, ValueSpecV1(**node["params"])).model_dump()
        assert actual == {**direct, "source_node_id": node["runtime_node_id"]}
        point = series[node["runtime_node_id"]]["points"][index]
        assert {k: actual[k] for k in point if k != "t"} == {
            k: v for k, v in point.items() if k != "t"
        }
    changed = deepcopy(spec)
    changed.feeds["1m"] = np.vstack(
        (spec.feeds["1m"], rows_for([15, 20]) + [1003 * 60_000, 0, 0, 0, 0, 0])
    )
    later = load_backtest_engine().run(changed)
    assert (
        later.metrics["execution_audit"]["decisions"][:1003]
        == fast.metrics["execution_audit"]["decisions"]
    )
    assert later.metrics["execution_audit"]["fills"][:2] == fast.metrics["execution_audit"]["fills"]


def test_factory_small_budget_has_matching_arrays_preparation_and_state_hooks():
    if not HAS_VALUES:
        return
    from koval.strategy.block_assembler import assemble_from_graph
    from koval.strategy.graph.values import ValueSpecV1, value_at

    cls = type(assemble_from_graph(graph_for()))
    observed = []

    class Capture(cls):
        def should_long(self):
            # Capture graph intents without submitting broker orders in this state-hook probe.
            return False

        def on_bar(self):
            super().on_bar()
            saved = self.export_graph_state()
            restored = cls()
            restored.config = dict(self.config)
            for name in (
                "close",
                "open",
                "high",
                "low",
                "volume",
                "closes",
                "opens",
                "highs",
                "lows",
                "volumes",
                "timestamp_ms",
                "decision_timestamp_ms",
                "history_start_ms",
                "bar_index",
                "account_value",
                "position_size",
                "position_direction",
            ):
                setattr(restored, name, deepcopy(getattr(self, name)))
            restored.restore_graph_state(saved)
            restored.on_bar()
            assert restored.should_long() is False
            assert restored.get_position_exit_request() is None
            observed.append((self._ctx(), self._result, saved, restored))

    rows = rows_for([10, 10, 10, 12, 8])
    frame = pd.DataFrame(
        rows[:, 1:],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.to_datetime(rows[:, 0], unit="ms", utc=True),
    )
    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=frame))
    cerebro.addstrategy(
        make_bt_strategy_class(
            Capture,
            history_bars=3,
            preparation_candles=rows,
            execution_features=(CAPABILITY,),
            primary_timeframe_ms=60_000,
            strategy_config={"symbol": "BTCUSDT", "timeframe": "1m"},
        )
    )
    cerebro.run()
    ctx, result, saved, restored = observed[3]
    assert saved["version"] == 1
    assert ctx.history_start_ms == 60_000 and len(ctx.closes) == 3
    assert result.orders
    value = value_at(ctx, ValueSpecV1(component="ema", period=2))
    assert value.readiness == "ready"
    next_ctx, next_result, next_state, _ = observed[4]
    # Restore only graph state: no pending broker orders are implied by this hook.
    restored.close, restored.open, restored.high, restored.low = (
        next_ctx.close,
        next_ctx.open,
        next_ctx.high,
        next_ctx.low,
    )
    for name in (
        "closes",
        "opens",
        "highs",
        "lows",
        "volumes",
        "timestamp_ms",
        "history_start_ms",
        "decision_timestamp_ms",
        "bar_index",
    ):
        setattr(restored, name, deepcopy(getattr(next_ctx, name)))
    restored.on_bar()
    assert restored.export_graph_state() == next_state
    assert restored._result.entities_by_node == next_result.entities_by_node


@pytest.mark.parametrize("budget", [True, 1, 100001, 3.5])
def test_factory_numeric_budget_uses_existing_host_resolver(budget):
    from koval.strategy.base.declarative import DeclarativeStrategy

    with pytest.raises(ValueError, match="history_bars"):
        make_bt_strategy_class(
            DeclarativeStrategy, history_bars=budget, execution_features=(CAPABILITY,)
        )


@pytest.mark.parametrize(
    "clock,reason", [(179999, "future_availability"), (240000, "stale_primary_bar")]
)
def test_early_and_stale_value_clocks_remain_null(clock, reason):
    if not HAS_VALUES:
        return
    from koval.strategy.graph.node import BarContext
    from koval.strategy.graph.values import ValueSpecV1, value_at

    value = value_at(
        BarContext(
            close=10,
            high=11,
            low=9,
            open=10,
            volume=0,
            bar_index=2,
            timestamp_ms=120000,
            decision_timestamp_ms=clock,
            history_start_ms=0,
            closes=np.array([10.0, 10.0, 10.0]),
            symbol="BTCUSDT",
            timeframe="1m",
        ),
        ValueSpecV1(component="close"),
    )
    assert value.reason == reason and value.value is None


def test_numeric_partial_signal_then_stop_preserves_inventory_and_remainder():
    if not HAS_VALUES:
        return
    from decimal import Decimal

    from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig

    spec = spec_for([10, 10, 10, 12, 8, 7, 0.5, 10])
    spec.execution_config["execution_proxy"] = ExecutionProxyConfig(
        Decimal(".1"), "carry", ExecutionLatency()
    )
    spec.feeds["1m"][4, 5] = 0.4
    spec.feeds["1m"][5, 5] = 0.2
    result = load_backtest_engine().run(spec)
    fills = result.metrics["execution_audit"]["fills"]
    assert [f["koval_role"] for f in fills] == ["entry", "signal", "stop_loss"]
    assert [f["size"] for f in fills] == pytest.approx([0.04, 0.02, 0.02])
    assert result.trades[0]["reason"] == "sl"
    assert result.trades[0]["fully_signal_closed"] is False
    assert result.metrics["research"]["final_open_position"] is None
    assert abs(result.metrics["execution_costs"]["reconciliation_error"]) < 1e-8


def test_numeric_run_identity_includes_actual_contract_and_source():
    if not HAS_VALUES:
        return
    result = load_backtest_engine().run(spec_for())
    report = result.metrics["execution_model"]["numeric_values"]
    assert report["capability"] == CAPABILITY
    assert report["history_bars"] == 1000
    assert report["formula_version"] == "numeric_value_v1"
    assert len(report["engine_source_sha256"]) == 64
    assert result.metrics["run_identity"]["numeric_values"] == report


@pytest.mark.parametrize("explicit", [False, True])
def test_missing_engine_contract_report_and_admission_agree(monkeypatch, explicit):
    from koval_backtrader import numeric_values

    def missing(name):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(numeric_values, "import_module", missing)
    engine = load_backtest_engine()
    assert engine.numeric_values_capabilities()["available"] is False
    assert engine.numeric_values_capabilities()["reason"] == "missing_engine_numeric_contract"
    monkeypatch.setattr(
        backtest_runner, "assemble_from_graph", lambda _: pytest.fail("constructed")
    )
    events = []
    with pytest.raises(ProtocolVersionError, match="missing_engine_numeric_contract"):
        engine.run(spec_for(explicit=explicit), events.append)
    assert events == []


@pytest.mark.parametrize("bad", ["unit", "cardinality", "source_symbol", "source_timeframe"])
def test_public_plugin_preserves_numeric_graph_validation(bad):
    if not HAS_VALUES:
        return
    from koval.strategy.block_assembler import GraphValidationError

    graph = graph_for()
    if bad == "unit":
        graph["blocks"][0]["params"]["component"] = "volume"
    elif bad == "cardinality":
        graph["connections"].append(
            {"from": "ma", "from_port": "value", "to": "cross", "to_port": "left"}
        )
    else:
        graph["blocks"][0]["params"]["symbol" if bad == "source_symbol" else "timeframe"] = "other"
    with pytest.raises(
        (ValueError, GraphValidationError),
        match="identity|scalar|cardinal|input|extra|validation|connect",
    ):
        load_backtest_engine().run(spec_for(graph=graph))


@pytest.mark.parametrize(
    "field,value", [("symbol", "ETHUSDT"), ("timeframe", "5m"), ("unit", "base_volume")]
)
def test_scalar_identity_is_not_rebound_to_primary(field, value):
    if not HAS_VALUES:
        return
    from koval.strategy.graph.node import BarContext
    from koval.strategy.graph.registry import get_node
    from koval.strategy.graph.values import ValueSpecV1, value_at

    ctx = BarContext(
        close=10,
        high=11,
        low=9,
        open=10,
        volume=0,
        bar_index=2,
        timestamp_ms=120000,
        decision_timestamp_ms=180000,
        history_start_ms=0,
        closes=np.array([10.0, 10.0, 10.0]),
        symbol="BTCUSDT",
        timeframe="1m",
    )
    left = value_at(ctx, ValueSpecV1(component="close"))
    node = get_node("fact.compare")
    evaluate = node.factory(node.params_schema(operator="gt"))
    with pytest.raises(ValueError, match="identity"):
        evaluate(ctx, {"left": [left], "right": [left.model_copy(update={field: value})]}, {})


def test_measured_zero_is_ready_and_missing_timing_is_not():
    if not HAS_VALUES:
        return
    from koval.strategy.graph.node import BarContext
    from koval.strategy.graph.values import ValueSpecV1, value_at

    ctx = BarContext(
        close=12,
        open=12,
        high=13,
        low=11,
        volume=0,
        bar_index=3,
        timestamp_ms=180000,
        decision_timestamp_ms=240000,
        history_start_ms=None,
        closes=np.array([10.0, 10.0, 10.0, 12.0]),
        opens=np.array([10.0, 10.0, 10.0, 12.0]),
        highs=np.array([11.0, 11.0, 11.0, 13.0]),
        lows=np.array([9.0, 9.0, 9.0, 11.0]),
        volumes=np.zeros(4),
        symbol="BTCUSDT",
        timeframe="1m",
    )
    ema = ValueSpecV1(component="ema", period=2)
    assert value_at(ctx, ema).reason == "missing_timing"
    ctx.history_start_ms = 0
    assert value_at(ctx, ema).readiness == "ready"
    zero = value_at(ctx, ValueSpecV1(component="volume"))
    assert zero.readiness == "ready" and zero.value == 0
    ctx.volumes = None
    missing = value_at(ctx, ValueSpecV1(component="volume"))
    assert missing.readiness == "missing" and missing.value is None


def test_factory_offset_sources_and_requirement_421_match_recorded_and_derived_prefixes():
    if not HAS_VALUES:
        return
    from koval.strategy.block_assembler import assemble_from_graph
    from koval.strategy.graph.series import strategy_indicator_series
    from koval.strategy.graph.values import ValueSpecV1, value_at

    graph = {
        "blocks": [
            {
                "id": "channel",
                "type": "source.value",
                "params": {"component": "channel.upper", "period": 400, "offset_bars": 20},
            },
            {
                "id": "ema",
                "type": "source.value",
                "params": {"component": "ema", "period": 2, "offset_bars": 1},
            },
            {
                "id": "atr",
                "type": "source.value",
                "params": {"component": "atr", "period": 2, "offset_bars": 1},
            },
            {
                "id": "bb",
                "type": "source.value",
                "params": {"component": "bb.lower", "period": 3, "offset_bars": 2},
            },
        ],
        "connections": [],
    }
    cls = type(assemble_from_graph(graph))
    seen = []

    class Capture(cls):
        def on_bar(self):
            super().on_bar()
            ctx = self._ctx()
            for block in graph["blocks"]:
                actual = self._result.entities_by_node[block["id"]]["value"].model_dump()
                direct = value_at(ctx, ValueSpecV1(**block["params"])).model_dump()
                assert actual == {**direct, "source_node_id": block["id"]}
            seen.append(deepcopy(self._result.entities_by_node))

    rows = rows_for(10 + np.sin(np.arange(422)))
    frame = pd.DataFrame(
        rows[:, 1:],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.to_datetime(rows[:, 0], unit="ms", utc=True),
    )
    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=frame))
    cerebro.addstrategy(
        make_bt_strategy_class(
            Capture,
            preparation_candles=rows,
            execution_features=(CAPABILITY,),
            primary_timeframe_ms=60_000,
            strategy_config={"symbol": "BTCUSDT", "timeframe": "1m"},
        )
    )
    cerebro.run()
    assert seen[419]["channel"]["value"].readiness == "warming_up"
    assert seen[419]["channel"]["value"].value is None
    assert seen[420]["channel"]["value"].readiness == "ready"
    for series in strategy_indicator_series(graph, rows, timeframe="1m", symbol="BTCUSDT"):
        for i, point in enumerate(series["points"]):
            actual = seen[i][series["node_id"]]["value"].model_dump()
            assert {k: actual[k] for k in point if k != "t"} == {
                k: v for k, v in point.items() if k != "t"
            }
