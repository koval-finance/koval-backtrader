# SPDX-License-Identifier: GPL-3.0-or-later
"""Differential parity against the MIT engine's paper broker.

The static fixtures in ``test_parity_fixtures.py`` prove six scenarios. They
cannot prove that the two implementations agree in general, and a plugin that
matches six cases is not a plugin anyone should trust with their own candles.

This module drives the *same* scenario through two independent implementations
— this plugin's Backtrader broker and ``koval.engine.paper_broker.PaperBroker``
under ``paper_ohlcv_fixed_v1`` — and compares the whole ledger: reference
price, actual fill, quantity, cost components, trade lifecycle, refusals and
final equity.

Both runtimes are stepped in the order the engine's live runner uses: resting
orders are matched against the bar first, and only then does the strategy see
it. That ordering is why an order placed on bar N cannot fill on bar N, and it
has to hold on both sides or the comparison proves nothing.

Differences that are real and understood are declared per field in
``KNOWN_DIVERGENCES`` against a stable reason code. Every other field is still
asserted, and an exemption that stops firing fails the suite — a divergence
fixed upstream must not sit here unnoticed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np
import pytest
from koval.engine.account_state import PlatformAccountState
from koval.engine.backtest_engine import EngineRunSpec
from koval.engine.execution_conformance import compare_execution_results
from koval.engine.paper_broker import PaperBroker, PaperOrderRejected
from koval.engine.paper_profile import resolve_paper_profile
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup

from koval_backtrader import backtest_runner

START_MS = 1_704_067_200_000
BAR_MS = 3_600_000

# A quiet bar the strategy signals on. Nothing ever fills against it.
SIGNAL = (100.0, 101.0, 99.0, 100.0, 1000.0)

# Cancelling a working entry is not a refusal to accept one. The plugin reports
# both through ORDER_REJECTED, so the comparison separates them by reason.
_CANCELLATION_REASONS = frozenset({"canceled"})


@dataclass(frozen=True)
class Scenario:
    """One deterministic run, expressed identically for both runtimes."""

    name: str
    rows: tuple[tuple[float, float, float, float, float], ...]
    direction: str = "long"
    entry_type: str = "market"
    entry: float = 100.0
    stop: float = 90.0
    target: float = 120.0
    size: float = 2.0
    profile: str = "ohlcv_fixed_v1"
    market: str = "future"
    capital: float = 10_000.0
    leverage: float = 1.0
    commission_bps: float = 4.0
    spread_bps: float = 20.0
    slippage_bps: float = 10.0
    # Bar index (1-based, as the strategy sees it) from which a still-working
    # entry should be cancelled.
    cancel_from: int | None = None
    # {bar_index: price} protection replacements the strategy asks for.
    stop_updates: dict[int, float] = field(default_factory=dict)
    target_updates: dict[int, float] = field(default_factory=dict)

    @property
    def side(self) -> str:
        return "buy" if self.direction == "long" else "sell"


class _Script:
    """The strategy decisions both runtimes replay, with no runtime coupling."""

    def __init__(self, scenario: Scenario):
        self.scenario = scenario

    def should_enter(self, bar_index: int) -> bool:
        return bar_index == 1

    def should_cancel_entry(self, bar_index: int) -> bool:
        cancel_from = self.scenario.cancel_from
        return cancel_from is not None and bar_index >= cancel_from

    def new_stop(self, bar_index: int) -> float | None:
        return self.scenario.stop_updates.get(bar_index)

    def new_target(self, bar_index: int) -> float | None:
        return self.scenario.target_updates.get(bar_index)


def _paper_profile(scenario: Scenario):
    return resolve_paper_profile(
        {
            "version": "paper_" + scenario.profile,
            "commission_bps": scenario.commission_bps,
            "spread_bps": scenario.spread_bps,
            "slippage_bps": scenario.slippage_bps,
            "leverage": scenario.leverage,
        }
    )


def plugin_config(scenario: Scenario) -> dict:
    return {
        "exchange": "binance",
        "exchange_type": scenario.market,
        "market": {
            "exchange": "binance",
            "market": scenario.market,
            "canonical_symbol": "BTCUSDT",
            "contract_type": "spot" if scenario.market == "spot" else "perpetual",
        },
        "execution_model": {
            "version": scenario.profile,
            "commission_bps": scenario.commission_bps,
            "spread_bps": scenario.spread_bps,
            "slippage_bps": scenario.slippage_bps,
            "leverage": scenario.leverage,
        },
    }


def candles_of(scenario: Scenario) -> np.ndarray:
    return np.array(
        [[START_MS + i * BAR_MS, *row] for i, row in enumerate(scenario.rows)], dtype=float
    )


def probe_class(scenario: Scenario, seen: list | None = None) -> type[DeclarativeStrategy]:
    """A strategy that replays the scenario script and nothing else."""
    script = _Script(scenario)

    class Probe(DeclarativeStrategy):
        def on_bar(self):
            if seen is not None:
                seen.append(self.account)

        def should_long(self):
            return scenario.direction == "long" and script.should_enter(self.bar_index)

        def should_short(self):
            return scenario.direction == "short" and script.should_enter(self.bar_index)

        def go_long(self):
            return TradeSetup(
                direction=scenario.direction,
                entry_type=scenario.entry_type,
                entry_price=scenario.entry,
                stop_loss=scenario.stop,
                take_profit=scenario.target,
                size=scenario.size,
            )

        go_short = go_long

        def should_cancel_entry(self):
            return script.should_cancel_entry(self.bar_index)

        def on_sl_update(self, trade_id):
            return script.new_stop(self.bar_index)

        def on_tp_update(self, trade_id):
            return script.new_target(self.bar_index)

    return Probe


def run_plugin(monkeypatch, scenario: Scenario) -> dict:
    """Run the scenario through the real runner, adapter, broker and analyzers."""
    snapshots: list = []
    probe = probe_class(scenario, snapshots)
    monkeypatch.setattr(backtest_runner, "assemble_from_graph", lambda graph: probe())
    events: list[dict] = []
    result = backtest_runner.create_engine().run(
        EngineRunSpec(
            graph={},
            feeds={"1h": candles_of(scenario)},
            initial_capital=scenario.capital,
            execution_config=plugin_config(scenario),
        ),
        on_event=events.append,
    )
    refusals = [
        event["payload"]["reason"] for event in events if event["event_type"] == "ORDER_REJECTED"
    ]
    return {
        "fills": [
            {
                "role": fill["koval_role"],
                "timestamp_ms": fill["timestamp_ms"],
                "quantity": fill["size"],
                "reference_price": fill["reference_price"],
                "fill_price": fill["fill_price"],
                "commission": fill["commission"],
                "spread_cost": fill["spread_cost"],
                "slippage_cost": fill["slippage_cost"],
            }
            for fill in result.metrics["execution_costs"]["fills"]
        ],
        "final_equity": result.metrics["final_capital"],
        "closed_trades": len(result.trades),
        "rejections": [r for r in refusals if r not in _CANCELLATION_REASONS],
        "cancellations": [r for r in refusals if r in _CANCELLATION_REASONS],
        "account": _account_view(snapshots[-1]) if snapshots else None,
    }


def run_paper(scenario: Scenario) -> dict:
    """Replay the same decisions through the MIT paper broker.

    Mirrors ``LiveEngine.process_bar``: fills first, strategy second. The paper
    runtime honours ``on_sl_update`` only, so target replacement is applied on
    the plugin side alone and shows up as a declared divergence.
    """
    script = _Script(scenario)
    broker = PaperBroker(scenario.capital, profile=_paper_profile(scenario), market=scenario.market)
    account = PlatformAccountState(starting_balance=scenario.capital, ledger=broker.ledger)
    fills: list[dict] = []
    rejections: list[str] = []
    cancellations: list[str] = []
    closed_trades = 0

    for index, row in enumerate(scenario.rows):
        bar_index = index + 1
        ts = START_MS + index * BAR_MS
        open_, high, low, close, _volume = row
        for fill in broker.process_bar(ts_ms=ts, open=open_, high=high, low=low, close=close):
            fills.append(
                {
                    "role": fill.kind,
                    "timestamp_ms": fill.timestamp_ms,
                    "quantity": fill.quantity,
                    "reference_price": fill.reference_price,
                    "fill_price": fill.price,
                    "commission": fill.commission,
                    "spread_cost": fill.spread_cost,
                    "slippage_cost": fill.slippage_cost,
                }
            )
            if fill.kind == "entry":
                account.on_open(
                    side=fill.side,
                    entry_price=fill.price,
                    quantity=fill.quantity,
                    current_stop=scenario.stop,
                    margin=fill.margin,
                )
            else:
                closed_trades += 1
                account.on_close(
                    realized_pnl=fill.realized_pnl + fill.commission, record_ledger=False
                )
        rejection = broker.consume_entry_rejection()
        if rejection is not None:
            rejections.append(rejection.split(":", 1)[0])
        account.on_bar(equity=broker.equity, timestamp_ms=ts)

        if broker.position is None and broker.pending and script.should_cancel_entry(bar_index):
            broker.cancel_all("PARITY", "parity-session")
            cancellations.append("canceled")
            continue
        if broker.position is None and not broker.pending and script.should_enter(bar_index):
            try:
                broker.submit_bracket(
                    side=scenario.side,
                    entry_price=scenario.entry,
                    stop_price=scenario.stop,
                    target_price=scenario.target,
                    quantity=scenario.size,
                    order_type=scenario.entry_type,
                    risk_budget=abs(scenario.entry - scenario.stop) * scenario.size
                    if scenario.profile == "ohlcv_realistic_v2"
                    else None,
                )
            except PaperOrderRejected as exc:
                rejections.append(exc.reason)
        elif broker.position is not None:
            new_stop = script.new_stop(bar_index)
            new_target = script.new_target(bar_index)
            if new_stop is not None or new_target is not None:
                broker.modify_protection(stop_price=new_stop, target_price=new_target)
                if new_stop is not None:
                    account.on_stop_update(new_stop)

    return {
        "fills": fills,
        "final_equity": broker.equity,
        "closed_trades": closed_trades,
        "rejections": rejections,
        "cancellations": cancellations,
        "account": _account_view(account.snapshot()),
    }


_ACCOUNT_FIELDS = (
    "balance",
    "equity",
    "realized_pnl",
    "unrealized_pnl",
    "daily_pnl",
    "peak_equity",
    "drawdown_pct",
    "daily_loss_pct",
    "trade_realized_pnl",
    "fees",
    "funding",
    "margin_used",
    "free_margin",
)


def _account_view(snapshot) -> dict:
    """The risk figures a gate decides on, from either runtime's snapshot."""
    return {field: float(getattr(snapshot, field)) for field in _ACCOUNT_FIELDS}


_FILL_FIELDS = (
    "role",
    "timestamp_ms",
    "quantity",
    "reference_price",
    "fill_price",
    "commission",
    "spread_cost",
    "slippage_cost",
)


def compare(plugin: dict, paper: dict, *, tolerance: float = 1e-9) -> dict[str, str]:
    """Map every disagreeing field to a readable description of the difference.

    Keys are stable so an exemption can name one field of one scenario without
    silencing the rest of that scenario.
    """
    differences: dict[str, str] = {}
    if len(plugin["fills"]) != len(paper["fills"]):
        differences["fill_count"] = (
            f"plugin {[f['role'] for f in plugin['fills']]} "
            f"vs paper {[f['role'] for f in paper['fills']]}"
        )
    for index, (left, right) in enumerate(zip(plugin["fills"], paper["fills"], strict=False)):
        for key in _FILL_FIELDS:
            mine, theirs = left[key], right[key]
            if isinstance(mine, str) or isinstance(theirs, str):
                unequal = mine != theirs
            else:
                unequal = abs(float(mine) - float(theirs)) > tolerance
            if unequal:
                differences[f"fill[{index}].{key}"] = f"plugin {mine!r} vs paper {theirs!r}"
    for key in ("final_equity", "closed_trades"):
        if abs(float(plugin[key]) - float(paper[key])) > 1e-6:
            differences[key] = f"plugin {plugin[key]!r} vs paper {paper[key]!r}"
    for key in ("rejections", "cancellations"):
        if plugin[key] != paper[key]:
            differences[key] = f"plugin {plugin[key]!r} vs paper {paper[key]!r}"
    mine, theirs = plugin.get("account"), paper.get("account")
    if mine is not None and theirs is not None:
        for field in _ACCOUNT_FIELDS:
            if abs(mine[field] - theirs[field]) > 1e-6:
                differences[f"account.{field}"] = (
                    f"plugin {mine[field]!r} vs paper {theirs[field]!r}"
                )
    return differences


# Scenarios the roadmap requires: favorable and adverse gaps in both directions
# for every entry type, ambiguous protection bars, cancellation, margin refusal,
# dynamic replacement and an open position at end of data.
SCENARIOS: tuple[Scenario, ...] = (
    Scenario("market_long_clean", (SIGNAL, SIGNAL, (100, 125, 75, 100, 1000))),
    Scenario(
        "market_short_clean",
        (SIGNAL, SIGNAL, (100, 125, 75, 100, 1000)),
        direction="short",
        stop=110.0,
        target=80.0,
    ),
    Scenario("market_long_entry_gap_up", (SIGNAL, (108, 110, 107, 109, 1000), SIGNAL)),
    Scenario(
        "market_short_entry_gap_down",
        (SIGNAL, (92, 93, 90, 91, 1000), SIGNAL),
        direction="short",
        stop=110.0,
        target=80.0,
    ),
    Scenario(
        "stop_long_gap_through_trigger",
        (SIGNAL, (103, 105, 95, 100, 1000)),
        entry_type="stop",
        entry=101.0,
    ),
    Scenario(
        "stop_short_gap_through_trigger",
        (SIGNAL, (97, 105, 95, 100, 1000)),
        direction="short",
        entry_type="stop",
        entry=99.0,
        stop=110.0,
        target=80.0,
    ),
    Scenario(
        "stop_long_intrabar_touch",
        (SIGNAL, (100, 105, 95, 100, 1000)),
        entry_type="stop",
        entry=101.0,
    ),
    Scenario(
        "stop_short_intrabar_touch",
        (SIGNAL, (100, 105, 95, 100, 1000)),
        direction="short",
        entry_type="stop",
        entry=99.0,
        stop=110.0,
        target=80.0,
    ),
    Scenario(
        "limit_long_exact_touch",
        (SIGNAL, (100, 105, 95, 100, 1000)),
        entry_type="limit",
        entry=99.0,
    ),
    Scenario(
        "limit_short_exact_touch",
        (SIGNAL, (100, 105, 95, 100, 1000)),
        direction="short",
        entry_type="limit",
        entry=101.0,
        stop=110.0,
        target=80.0,
    ),
    Scenario(
        "limit_long_favorable_open_range_covers_limit",
        (SIGNAL, (98, 105, 95, 100, 1000)),
        entry_type="limit",
        entry=100.0,
    ),
    Scenario(
        "limit_short_favorable_open_range_covers_limit",
        (SIGNAL, (102, 105, 95, 100, 1000)),
        direction="short",
        entry_type="limit",
        entry=100.0,
        stop=110.0,
        target=80.0,
    ),
    Scenario(
        "limit_long_favorable_open_range_misses_limit",
        (SIGNAL, (96, 97.5, 95, 96.5, 1000)),
        entry_type="limit",
        entry=100.0,
    ),
    Scenario(
        "limit_short_favorable_open_range_misses_limit",
        (SIGNAL, (104, 105, 102.5, 103, 1000)),
        direction="short",
        entry_type="limit",
        entry=100.0,
        stop=110.0,
        target=80.0,
    ),
    Scenario("stop_loss_gap_below_trigger", (SIGNAL, SIGNAL, (85, 89, 80, 86, 1000))),
    Scenario(
        "stop_loss_gap_above_trigger",
        (SIGNAL, SIGNAL, (115, 120, 112, 116, 1000)),
        direction="short",
        stop=110.0,
        target=80.0,
    ),
    Scenario("stop_loss_intrabar_touch", (SIGNAL, SIGNAL, (95, 96, 89, 94, 1000))),
    Scenario("take_profit_exact_touch", (SIGNAL, SIGNAL, (119, 122, 118, 121, 1000))),
    Scenario("take_profit_favorable_open", (SIGNAL, SIGNAL, (125, 130, 121, 124, 1000))),
    Scenario(
        "take_profit_favorable_open_short",
        (SIGNAL, SIGNAL, (75, 79, 70, 76, 1000)),
        direction="short",
        stop=110.0,
        target=80.0,
    ),
    Scenario("ambiguous_bar_resolves_stop_first", (SIGNAL, SIGNAL, (100, 125, 75, 100, 1000))),
    Scenario(
        "ambiguous_bar_resolves_stop_first_short",
        (SIGNAL, SIGNAL, (100, 125, 75, 100, 1000)),
        direction="short",
        stop=110.0,
        target=80.0,
    ),
    Scenario("open_position_at_end_of_data", (SIGNAL, (100, 102, 99, 101, 1000))),
    Scenario(
        "open_position_at_end_of_data_short",
        (SIGNAL, (100, 102, 99, 101, 1000)),
        direction="short",
        stop=110.0,
        target=80.0,
    ),
    Scenario(
        "entry_cancelled_before_fill",
        (SIGNAL, SIGNAL, SIGNAL),
        entry_type="limit",
        entry=95.0,
        cancel_from=2,
    ),
    Scenario("insufficient_margin_refuses_entry", (SIGNAL, SIGNAL), size=2.0, capital=100.0),
    # Minimized from a randomized differential sweep. The requested notional
    # (8 x 101 = 808) fits inside 1000 of capital, so both runtimes accept the
    # order. The bar then gaps far above the trigger and the entry actually
    # fills near 137, for a notional of ~1096 the account cannot cover.
    Scenario(
        "stop_entry_gap_makes_the_accepted_order_unaffordable",
        (SIGNAL, (137, 140, 136, 138, 1000), SIGNAL),
        entry_type="stop",
        entry=101.0,
        size=8.0,
        capital=1000.0,
        leverage=1.0,
    ),
    Scenario(
        "leverage_five_market_entry",
        (SIGNAL, SIGNAL, (85, 89, 80, 86, 1000)),
        leverage=5.0,
        size=20.0,
    ),
    Scenario(
        "dynamic_stop_tightened_after_entry",
        (SIGNAL, SIGNAL, (105, 106, 104, 105, 1000), (100, 101, 94, 95, 1000)),
        stop_updates={3: 95.0},
    ),
    Scenario(
        "dynamic_stop_requested_on_entry_fill_bar",
        (SIGNAL, SIGNAL, (100, 101, 94, 95, 1000)),
        stop_updates={2: 95.0},
    ),
    Scenario(
        "dynamic_target_replacement",
        (SIGNAL, SIGNAL, (105, 106, 104, 105, 1000), (106, 112, 105, 111, 1000)),
        target_updates={3: 110.0},
    ),
    Scenario(
        "zero_cost_model_matches_exactly",
        (SIGNAL, SIGNAL, (85, 89, 80, 86, 1000)),
        commission_bps=0.0,
        spread_bps=0.0,
        slippage_bps=0.0,
    ),
)

# Engine 0.11 closes the old divergences. The shared comparator rejects any
# future unexplained difference; no waiver remains for either supported pair.
KNOWN_DIVERGENCES = {}
REASON_CODES = frozenset()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_plugin_matches_the_paper_broker_ledger(monkeypatch, scenario):
    compare_execution_results(run_paper(scenario), run_plugin(monkeypatch, scenario))


def _generated_scenarios(count: int = 120) -> tuple[Scenario, ...]:
    """Seeded differential cases, so coverage is not limited to what I thought of.

    Limit entries are excluded: their divergence is already known, declared
    above, and would otherwise drown every other signal in this sweep. The seed
    is fixed, so a failure here names one reproducible scenario.
    """
    rng = np.random.default_rng(20260910)
    scenarios = []
    for index in range(count):
        direction = "long" if index % 2 == 0 else "short"
        entry_type = ("market", "limit", "stop")[index % 3]
        rows = [SIGNAL]
        for _ in range(int(rng.integers(2, 6))):
            open_ = float(rng.uniform(70, 130))
            close = float(rng.uniform(70, 130))
            high = float(max(open_, close) + rng.uniform(0, 8))
            low = float(min(open_, close) - rng.uniform(0, 8))
            rows.append((open_, high, max(low, 1.0), close, 1000.0))
        entry = 100.0
        stop, target = (90.0, 120.0) if direction == "long" else (110.0, 80.0)
        if entry_type == "stop":
            entry = 101.0 if direction == "long" else 99.0
        scenarios.append(
            Scenario(
                f"generated_{index:03d}_{direction}_{entry_type}",
                tuple(rows),
                direction=direction,
                entry_type=entry_type,
                entry=entry,
                stop=stop,
                target=target,
                size=float(rng.uniform(0.5, 5.0)),
                leverage=float(rng.choice([1.0, 2.0, 5.0])),
                commission_bps=float(rng.choice([0.0, 4.0, 10.0])),
                spread_bps=float(rng.choice([0.0, 6.0, 20.0])),
                slippage_bps=float(rng.choice([0.0, 3.0, 10.0])),
            )
        )
    return tuple(scenarios)


GENERATED = _generated_scenarios()


@pytest.mark.parametrize("scenario", GENERATED, ids=lambda s: s.name)
def test_generated_scenarios_match_the_paper_broker(monkeypatch, scenario):
    """No exemptions here: a generated case must agree exactly."""
    differences = compare(run_plugin(monkeypatch, scenario), run_paper(scenario))
    assert differences == {}, f"{scenario.name} diverged from paper:\n" + "\n".join(
        f"  {key}: {detail}" for key, detail in sorted(differences.items())
    )


def test_the_generated_sweep_actually_reaches_fills():
    """A sweep whose scenarios never fill would pass while proving nothing."""
    filled = 0
    for scenario in GENERATED:
        paper = run_paper(scenario)
        if paper["fills"]:
            filled += 1
    assert filled >= len(GENERATED) // 2, f"only {filled}/{len(GENERATED)} generated cases filled"


def test_every_declared_reason_code_is_recognised():
    used = {code for fields in KNOWN_DIVERGENCES.values() for code in fields.values()}
    assert used == REASON_CODES


def test_known_divergences_name_real_scenarios():
    names = {scenario.name for scenario in SCENARIOS}
    assert set(KNOWN_DIVERGENCES) <= names


def test_scenario_names_are_unique():
    names = [scenario.name for scenario in SCENARIOS]
    assert sorted(names) == sorted(set(names))


def test_scenario_protection_is_coherent():
    """A long whose target sits below its entry would silently test nothing."""
    for scenario in SCENARIOS:
        if scenario.direction == "long":
            assert scenario.stop < scenario.entry < scenario.target, scenario.name
        else:
            assert scenario.target < scenario.entry < scenario.stop, scenario.name


def test_the_comparison_would_notice_a_difference():
    """Guards the guard: a comparator returning nothing would pass everything."""
    ledger = {
        "fills": [
            {
                "role": "entry",
                "timestamp_ms": START_MS,
                "quantity": 2.0,
                "reference_price": 100.0,
                "fill_price": 100.0,
                "commission": 0.0,
                "spread_cost": 0.0,
                "slippage_cost": 0.0,
            }
        ],
        "final_equity": 10_000.0,
        "closed_trades": 0,
        "rejections": [],
        "cancellations": [],
    }
    assert compare(ledger, ledger) == {}
    for key, value in (
        ("fill_price", 101.0),
        ("commission", 1.0),
        ("role", "stop_loss"),
        ("timestamp_ms", START_MS + BAR_MS),
        ("quantity", 3.0),
        ("reference_price", 99.0),
        ("spread_cost", 0.5),
        ("slippage_cost", 0.5),
    ):
        other = {**ledger, "fills": [{**ledger["fills"][0], key: value}]}
        assert list(compare(ledger, other)) == [f"fill[0].{key}"]
    assert list(compare(ledger, {**ledger, "final_equity": 9_999.0})) == ["final_equity"]
    assert list(compare(ledger, {**ledger, "closed_trades": 1})) == ["closed_trades"]
    assert list(compare(ledger, {**ledger, "rejections": ["x"]})) == ["rejections"]
    assert list(compare(ledger, {**ledger, "cancellations": ["x"]})) == ["cancellations"]
    assert list(compare(ledger, {**ledger, "fills": []})) == ["fill_count"]


@pytest.mark.parametrize("scenario", (*SCENARIOS, *GENERATED), ids=lambda s: s.name)
def test_realistic_profile_matches_the_paper_broker(monkeypatch, scenario):
    scenario = replace(scenario, profile="ohlcv_realistic_v2")
    compare_execution_results(run_paper(scenario), run_plugin(monkeypatch, scenario))


@pytest.mark.parametrize("profile", ["ohlcv_fixed_v1", "ohlcv_realistic_v2"])
def test_spot_short_refusal_matches_paper(monkeypatch, profile):
    scenario = Scenario(
        "spot_short",
        (SIGNAL, SIGNAL),
        direction="short",
        stop=110,
        target=80,
        market="spot",
        profile=profile,
    )
    compare_execution_results(run_paper(scenario), run_plugin(monkeypatch, scenario))


@pytest.mark.parametrize("stop,target", [(85, None), (95, 94), (None, float("nan")), (None, 0)])
def test_invalid_protection_update_fails_in_both_runtimes(monkeypatch, stop, target):
    scenario = Scenario(
        "invalid_protection",
        (SIGNAL, SIGNAL, SIGNAL),
        stop_updates={2: stop},
        target_updates={2: target},
    )
    for run in (lambda: run_paper(scenario), lambda: run_plugin(monkeypatch, scenario)):
        with pytest.raises(ValueError, match="protection|stop update"):
            run()


def test_short_gap_rechecks_actual_margin_in_fixed_v1(monkeypatch):
    scenario = Scenario(
        "short_gap_margin",
        (SIGNAL, (140, 145, 135, 140, 1000)),
        direction="short",
        entry=100.0,
        stop=150.0,
        target=90.0,
        size=8.0,
        capital=1000.0,
    )
    compare_execution_results(run_paper(scenario), run_plugin(monkeypatch, scenario))
