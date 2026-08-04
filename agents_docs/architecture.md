# Architecture

Four modules, one entry point, no configuration. This document covers what
each module owns and how a run flows through them.

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

Fees are applied only when `execution_config` is present. A run without one is
fee-free on purpose, so unit tests can assert on raw price action.

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

Drawdown is computed here from the equity curve; every other metric comes from
the engine's shared calculation, so a backtest and a paper run report the same
numbers by construction rather than by coincidence.

## Update this file when

A module gains or loses a responsibility, the run flow changes shape, or the
data crossing the engine boundary changes type.
