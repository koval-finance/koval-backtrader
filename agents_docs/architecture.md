# Architecture

## Historical numeric values

`numeric_values.py` admits `numeric_values_v1` only with the actual optional MIT
numeric contract. Imports remain lazy so same-version engines without those
modules can still load the plugin and execute legacy graphs. Both explicit and
graph-inferred requests require one declared Binance Spot cash-funded long-only
1x market, `ohlcv_realistic_v2`, and one 1m/5m/15m/1h/4h/1d feed. This scope does
not imply the graph needs `position_exit_v1`; exit graphs negotiate it separately.

Admission validates every supplied primary timestamp before preparation or
strategy/feed/broker construction: finite integer, unique increasing, aligned
and contiguous candle opens. There is no gap filling or compressed timeline.
The MIT `graph_required_history_bars` requirement must fit the actual unchanged
1000-bar public runner budget. No automatic growth or clamping is permitted.
The existing `make_bt_strategy_class(..., history_bars=...)` factory override
uses the engine's 2..100000 resolver for numeric hosts; EngineRunSpec gains no
budget field. Preparation receives the same budget as chronological arrays.

On every numeric closed primary bar, the adapter binds `history_start_ms` to
the oldest retained candle open and `decision_timestamp_ms` to current open plus
the validated interval. Numeric direct factory hosts also use UTC conversion.
Absent custom volume lines produce scalar NaN and absent arrays; nonfinite
measurements remain nonfinite and measured zero stays zero. Costed public
ndarrays already reject absent/nonfinite volume before construction. Legacy
missing-line zero fallbacks and naive legacy clock behavior remain unchanged.

The optional loader-returned `numeric_values_capabilities(self) -> dict` is
read-only and does not alter the MIT protocol or run signature. Its exact
schema and scope are documented in [the execution model](../docs/execution-model.md#numeric-values).
Numeric runs bind the contract and loaded MIT source digest into execution
metadata and run parameters/identity; legacy identity fields are not extended.
The digest reuses `spot_identity.source_identity` and its documented byte
encoding, distinct from a wheel SHA or a Python-only canonical fingerprint.

Public graph state export/restore is a post-bar checkpoint: restoring the same
stepped bar yields no transient intent, and the next step matches uninterrupted
graph evaluation. It does not restore pending broker intents. Numeric adapter
resume/crash recovery remains unsupported; full deterministic rerun is tested.
Matcher, costs, protection and order lifecycle remain the existing contracts.

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

Specs also carry the requested `execution_contract_version` and
`required_execution_capabilities`; protocol 2 adds optional `runtime_contract`
boundaries. Protocol validation and execution negotiation are separate checks:
unsupported requested features must fail before construction. Native Spot
requires protocol 2, execution contract 2 and its binding interval, and refuses
legacy `runtime_contract` boundaries.
The complete input fields are listed in [the plugin contract](../docs/custom-engines.md#the-contract).

## Modules

Native Grid/DCA uses a separate [historical Spot lane](spot_multi_order.md).
Legacy adapter/broker guards remain unchanged.

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
and fixes `cancel()` to drop orders still in the submitted queue. Before matching,
it orders existing active plugin protection within each OCO group's queue slots:
opening stop, opening target, then intrabar touches in their existing order.
Unrelated orders and newly created protection keep their placement and timing.

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

The optional 0.12 path is separated into `runtime_boundaries.py` (validate and
slice the MIT contract) and `terminal_execution.py` (finish through actual
broker notifications without another candle). `execution_trace.py` records
decision inputs and run-local links and exports authoritative broker/account
state; it never computes cash. Both costed profiles export the audit in metrics.

For a canonical Spot identity, `ExecutionAccount` adapts the shared engine
snapshot's `free_margin` to `balance - margin_used`: unspent quote cash after
fees and inventory purchases. The shared `balance` is realized capital and
`margin_used` is held inventory's acquisition cost. Mark-to-market PnL affects
equity but cannot fund another Spot purchase. Futures keep the shared engine
margin calculation. `tests/test_execution_account.py` exercises both costed
profiles with rising and falling prices through the actual strategy bridge.
`RealisticBroker` uses the same cash boundary for partial-entry affordability;
only derivative markets include unrealized PnL as collateral.
See [../docs/runtime-assurance.md](../docs/runtime-assurance.md).

## Update this file when

A module gains or loses a responsibility, the run flow changes shape, or the
data crossing the engine boundary changes type.

## Optional indicator preparation

The runner passes the actual primary feed, after runtime-boundary trimming, as
`preparation_candles`. Each adapter instance calls the strategy's optional
`prepare_backtest(candles, history_bars=...)` hook once. Older engine versions and
custom strategies without a callable hook use normal scalar evaluation.

Compatible graph strategies batch pure EMA/RSI/ATR calculations with NumPy while
preserving finite-window SMA seeds and previous/current values. The normal
per-bar history, HTF availability, graph state, broker, audit and event paths
remain active. Preparation includes warmup candles but does not execute warmup
decisions. Future primary candles cannot affect earlier prepared values. No new
`EngineRunSpec` field or execution-profile setting is required.

`tests/test_indicator_preparation.py` checks per-instance preparation, older-host
fallback, and complete result/event equality across the rolling-window boundary,
including two feeds and explicit warmup/terminal policies.

## 0.12.1 realism corrections

Carried partial-entry affordability marks existing exposure at the matching
price, not the later candle close. Protection delay begins with actual first
execution, including delayed limits and unavailable early volume; subsequent
partials keep the existing activation. Funding settlements must align with the
execution grid or the run fails before any decisions. Use finer candles rather
than inventing intrabar exposure.

With the paired engine, evidence uses its strict MIT JSON codec and results
include `realism_report`: supplied evidence, modeled proxies, unavailable effects
and explicitly unmeasured exchange accuracy. The original decoder remains for
older supported engines. No numerical accuracy or error ceiling is inferred.
Local candidate wheels must be rebuilt after edits; production release is a
separate human action.

## Trade decision evidence

The optional MIT `TradeSetup.decision_context` is copied at accepted intent time
into the execution trace and raw trade. The analyzer joins entry orders through
actual fill/trade IDs. Stop replacements keep the original decision/intent IDs
and append observed stop changes. Entry fill sizing and protective fill context
are read from the same broker branch that executed the fill; no second evaluator
or ledger is allowed. Keep these snapshots detached from mutable setup state.
Regression coverage: `tests/test_trade_decision_evidence.py`.

## Historical position exits

`position_exit.py` derives graph requirements without eagerly importing new MIT
symbols, gates the two optional features to explicit Binance Spot v2 with
leverage 1, and validates stop-only updates independently of the MIT bracket-only
validator. The runner negotiates before creating execution resources. Older
engines still run old entry graphs; new graphs require the actual MIT mode and
exit hooks and fail explicitly when unavailable.

The adapter queries the cached exit hook after one `on_bar`, binds one intent to
the current run-local position and source decision, cancels entry remainder, and
submits one market close. `RealisticBroker.next` matches active gap protection
before that close at an eligible open, then processes intrabar protection for
remaining inventory. The same opening-protection priority applies without a
pending signal order; only an open inside the bracket retains ambiguous
stop-first matching. Intrabar entry protection must pass `allow_open_gap=False`
and cannot reuse a favorable open for its target. Signal
orders share sizing caps, volume, fees and the actual-fill ledger with protection.
Completed signal lifecycle sets a completion-bar cooldown even when protection
closed the residual. No accepted signal means no new cooldown.

Disabled take-profit is preserved in setup replacement/normalization, stops,
partial fills and terminal output. No RR fallback or target is added. The trace
holds exit intents separately from entry intents, and the trade joins its exit
context to a signal order whose actual fill IDs remain in the ledger. Mixed exits
use their final fill reason and do not count as fully signal-closed positions.
See [../docs/results.md](../docs/results.md) for the exported field contract.

Protection replacement validates the raw strategy instruction before instrument
normalization and validates the normalized bracket afterward. A time-valid
instrument-filter refusal emits `ORDER_REJECTED` with
`operation="protection_replace"` before any cancellation or account/history
mutation. The working pair and any carried entry retain their previous prices.
This refusal does not reject the original successful entry intent; malformed
or widening strategy instructions still raise explicitly.
