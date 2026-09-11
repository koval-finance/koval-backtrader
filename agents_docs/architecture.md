# Architecture

One entry point, four core modules and three execution modules. This document covers what
each module owns and how a run flows through them.

For the function-by-function walkthrough, and for the fill rules a change
here has to preserve, read [../docs/architecture.md](../docs/architecture.md)
and [../docs/execution-model.md](../docs/execution-model.md).

## The seam

`koval-engine` declares the contract and never implements it:

```
koval-engine (MIT)                        koval-backtrader (GPL-3.0)
  BacktestEngineProtocol      ◀──────────   BacktraderBacktestEngine
  EngineRunSpec, BacktestResult             create_engine()
  load_backtest_engine()      ──finds──▶    entry point "backtrader"
```

`load_backtest_engine()` looks up the `koval.backtest_engines` entry-point
group, takes the member named `backtrader`, imports it, and calls it. The
factory returns a ready instance. The engine never imports this package by
name, which is what keeps its MIT licence intact.

`EngineRunSpec` is deliberately plain and picklable — a graph dict, a mapping
of timeframe to OHLCV array, initial capital, and an optional execution
config. `BacktestResult` is equally plain: metrics, trades, equity curve. No
Backtrader object crosses the boundary in either direction.

## Modules

### `backtest_runner.py` — the plugin

`BacktraderBacktestEngine.run()` is the only public surface. It checks the
protocol version, orders the timeframes, assembles a `DeclarativeStrategy`
from the graph, wraps it in a Backtrader class, builds a `Cerebro`, adds one
data feed per timeframe, applies the fee model, runs, and reduces the result
to plain dictionaries.

Feeds arrive as `(N, 6)` float arrays with column 0 holding epoch
milliseconds; they become `bt.feeds.PandasData` with a UTC index.

Fees resolve through `execution_config`; a run without one remains fee-free.
Valid legacy configs preserve prices, PnL and equity. Versioned configs opt into
explicit costs, and malformed/unknown settings now fail clearly. Every result
includes a resolved model snapshot and software identity in `metrics`.

### Execution assumptions and costs

`execution_config.py` owns the pure parser and frozen `ExecutionModel`;
`legacy_v1` freezes fees-only execution and `ohlcv_fixed_v1` requires explicit
commission, full-spread and slippage bps. No protocol change is required.

`execution_broker.py` adjusts Backtrader-matched prices before `_execute`
updates cash, commission, positions and notifications. Market/stop costs are
synthetic and not capped to bar extremes; limits preserve their price bound.
Submission checks never enter the ledger. Only actual fills receive run-local
IDs. Existing OCO matching and delayed brackets remain unchanged.

`execution_audit.py` emits metadata and attributes embedded costs without
debiting anything. Both reference cashflows and closed/open trade PnL must
reconcile with final broker value. Open entries and their fees are included.
The analyzer retains the legacy net-of-commission `gross_realized_pnl` alias;
v1 adds precise gross-price/net-before-funding fields and rejects cosmetic
funding adjustments. See [../docs/results.md](../docs/results.md).

Those v1 limits remain explicit. V2 adds normalized execution evidence and
partial lifecycle support through the modules below. Never enable unsupported
evidence by merely relaxing a parser; verify matching, cashflow and OCO semantics
and update [the model contract](../docs/execution-model.md).

### `bt_adapter.py` — the strategy bridge

`BTStrategyAdapter` is a `bt.Strategy` that owns no trading logic. Every
decision is delegated to a `DeclarativeStrategy` instance; the adapter's job
is translation in both directions.

Inbound, `_inject_state()` hands the strategy chronological numpy arrays for
the current bar window — closes, highs, lows, volumes, and any higher
timeframe feeds — plus position and account state. Outbound, `_submit_entry()`
and `_place_bracket()` turn a `TradeSetup` into a Backtrader order and its
stop/take-profit bracket, and `_update_exits()` moves those brackets when the
strategy asks for a trailing or breakeven change.

HTF injection previously treated a candle's opening timestamp as the time
its final OHLCV became available, which leaked unfinished HTF bars into
earlier primary decisions in both models. Since 0.10.0 a row is injected only
once `htf_open + htf_duration <= primary_open + primary_duration`, and the
arrays stay `None` until the first bar has closed. Only trailing bars can be
forming, so the scan stops at the first closed bar rather than re-reading the
whole HTF history every primary bar.

The [readiness record](../docs/execution-plan.md) carries the compatibility
decision and the tests; read it before changing multi-timeframe behavior.

Every meaningful decision emits an `EngineEvent` through an optional sink,
which is what makes a run explainable after the fact rather than a black box
that produced a number.

`make_bt_strategy_class(strategy_cls, event_sink=...)` is the factory. Nothing
outside this module subclasses `BTStrategyAdapter` directly.

### `bt_analyzers.py` — result extraction

`TradeListAnalyzer` records each closed trade with entry and exit prices,
timestamps, size, PnL, and the exit reason. `EquityCurveAnalyzer` samples
broker value once per bar. Both are ordinary Backtrader analyzers, read once
after `cerebro.run()` returns.

### `oco_patch.py` — a Backtrader bug fix

Backtrader runs `_ococheck` after `_try_exec`, so when a take-profit and a
stop-loss are both eligible within one bar, both can fill before the
cancellation propagates — producing a trade that never happened. The patch
tracks completed OCO groups per bar and cancels the sibling before execution,
and fixes `cancel()` to drop orders still in the submitted queue.

`apply_oco_guard()` is idempotent and called once at import of
`backtest_runner`. It monkey-patches broker internals, which is the fragile
coupling described in [invariants.md](invariants.md).

## Run flow

```
EngineRunSpec
  → assemble_from_graph(graph)          engine, MIT
  → make_bt_strategy_class(...)         this package
  → Cerebro: feeds, cash, commission
  → cerebro.run()
      per bar:  _inject_state → strategy decision → orders → events
  → analyzers: trades, equity curve
  → build_closed_trade_metrics(...)     engine, MIT
  → BacktestResult
```

Drawdown and execution auditing are computed here; scalar trade metrics use
the engine's shared calculations. Paper still has different fill timing and
protection behavior; shared metric formulas do not establish execution parity.

## Version 0.11 execution modules

`realistic_broker.py` independently matches v2 orders and resizes partial OCO
protection. `evidence_execution.py` mutates an incremental account ledger from
funding, fills and fees. `execution_evidence.py` validates public engine evidence
and produces JSON replay configuration. `strategy_account.py` binds the graph's
public 0.11.1 account reader to broker-authoritative snapshots, with the old
fallback retained for 0.11.0. No strategy or analyzer may book cashflows twice.

The runner negotiates capabilities, enforces v2 aligned contiguous data, refuses
more than two timeframes and prevents trailing HTF bars from replaying a primary
bar. Account snapshots cost O(1) in ledger length; the full ledger is exported
at session end. Paired acceptance and model limits are in
[../docs/execution-validation.md](../docs/execution-validation.md).

## Update this file when

A module gains or loses a responsibility, the run flow changes shape, or the
data crossing the engine boundary changes type.
