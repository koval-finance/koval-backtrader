# Architecture

A code walkthrough: four modules, roughly 800 lines, and where to look when
you need to change something.

If you only want the shape of the plugin seam, the
[README](../README.md#how-it-fits-together) covers it in a diagram. This page
is for people who are about to edit the code.

## Two packages, one seam

koval-engine declares the contract and ships no implementation:

```
koval-engine (MIT)                          koval-backtrader (GPL-3.0)
  BacktestEngineProtocol        ◀────────     BacktraderBacktestEngine
  EngineRunSpec, BacktestResult                create_engine()
  load_backtest_engine()        ──finds──▶     entry point "backtrader"
```

`load_backtest_engine()` reads the `koval.backtest_engines` entry-point
group, takes the member named `backtrader`, imports it, and calls it. The
factory returns a ready instance. The engine never names this package in its
own source, which is what keeps its MIT licence intact — see
[custom-engines.md](custom-engines.md) if you want to write your own plugin,
and [../agents_docs/invariants.md](../agents_docs/invariants.md) for why the
direction of that arrow is enforced by tests on both sides.

`EngineRunSpec` is deliberately plain and picklable: a graph dict, a mapping
of timeframe to OHLCV array, initial capital, an optional execution config.
`BacktestResult` is equally plain. No Backtrader object crosses the boundary
in either direction, so a caller never links Backtrader by accident.

## The modules

```
src/koval_backtrader/
├── backtest_runner.py   116 lines  the plugin: spec in, result out
├── bt_adapter.py        460 lines  DeclarativeStrategy → bt.Strategy bridge
├── bt_analyzers.py      115 lines  trade list and equity curve extraction
└── oco_patch.py         148 lines  the Backtrader OCO bug fix
```

`tests/` is flat and mirrors those names.

### `backtest_runner.py`

`BacktraderBacktestEngine.run()` is the only public surface, and it is short
enough to read in one sitting. In order:

1. `check_protocol_version(spec)` — refuse a spec from a newer engine rather
   than silently misinterpreting it.
2. `ordered_timeframes()` — sort the feed keys ascending, so `data0` is
   always the lowest timeframe and `data1` the higher one regardless of dict
   order.
3. `assemble_from_graph(spec.graph)` — the engine turns JSON into a
   `DeclarativeStrategy` instance. This is MIT code; the adapter never parses
   a graph itself.
4. `make_bt_strategy_class(type(strategy), event_sink=...)` — wrap the
   strategy's class in a Backtrader class.
5. Build `Cerebro`: one `PandasData` feed per timeframe, starting cash,
   commission if `execution_config` is present.
6. `cerebro.run()`, then read the two analyzers.
7. Reduce to plain data and hand the closed trades to the engine's
   `build_closed_trade_metrics()`. Only `max_drawdown` is computed here.

`_feed_from_ndarray()` converts an `(N, 6)` float array — column 0 is epoch
milliseconds, then OHLCV — into a Backtrader feed with a UTC index. An empty
array raises `ValueError("empty OHLCV feed")` rather than producing a run
with no bars.

The step-5 detail that surprises people: commission is applied **only** when
`execution_config` is present. A bare spec is fee-free on purpose, so tests
can assert on raw price action. See
[execution-model.md](execution-model.md#fees).

### `bt_adapter.py`

The largest module and the one that decides where orders fill.
`BTStrategyAdapter` is a `bt.Strategy` that owns no trading logic — every
decision belongs to the wrapped `DeclarativeStrategy`, and the adapter only
translates.

**Inbound**, `_inject_state()` runs at the top of every bar and writes onto
the strategy instance: the current bar's OHLCV scalars, bar index and
timestamp, account value, position size and direction, and chronological
numpy history arrays capped at `history_bars` (default 300). Only index `[0]`
and `get(ago=0, size=n)` are used, so a future bar is unreachable. The
strategy's `on_bar()` is called immediately afterwards, on every bar whether
or not a position is open, matching what the engine's live runner does.

If a second feed exists, the same arrays are injected with an `htf_` prefix.
A third feed is loaded into Cerebro but never injected — the adapter reads
`self.datas[1]` and nothing beyond it.

**Outbound**, the order path is four methods:

| Method | Runs | Does |
|---|---|---|
| `_try_enter()` | Bar with no position and no live entry | Ask `should_long` / `should_short`, run filters, build the `TradeSetup`, emit the signal event |
| `_submit_entry()` | Immediately after | Size the order if the setup did not, submit market / limit / stop |
| `_place_bracket()` | On the entry fill, from `notify_order` | Place the stop-loss and the OCO take-profit |
| `_update_exits()` | Every later bar with a position | Ask the strategy for new levels, rebuild the bracket atomically if they changed |

`_update_exits()` looks more defensive than it needs to be, and the comment
in it explains why: cancelling the stop cascades to the take-profit through
the OCO group, so both prices are snapshotted before the cancel and both legs
are re-placed afterwards. Removing that dance silently drops take-profits.

`notify_order()` and `notify_trade()` are where per-trade bookkeeping lives.
Backtrader reports `size == 0` on a closed trade, so the filled size, the
exit price, and the exit reason are captured as they happen and exposed
through `get_trade_info()`, which the analyzer reads back. That indirection
exists because an analyzer cannot see the adapter's internals any other way.

Every meaningful decision calls `_emit()`, which appends an `EngineEvent` to
`self._events` and forwards it to the optional sink. The event stream is what
makes a run explainable after the fact instead of a number with no
provenance.

`make_bt_strategy_class(cls, **params)` is the factory. It merges overrides
into the default parameter tuple, subclasses `BTStrategyAdapter`, and names
the result `<StrategyName>Adapter` so tracebacks stay readable. Nothing
outside this module should subclass `BTStrategyAdapter` directly.

### `bt_analyzers.py`

Two ordinary Backtrader analyzers, read once after `cerebro.run()` returns.

`TradeListAnalyzer` builds one record per closed trade, merging Backtrader's
view (prices, PnL, timestamps) with the adapter's `get_trade_info()` (size,
exit reason, the setup's stop and target, the strategy's stated reasons). It
derives the exit price from gross PnL over size rather than trusting a field
that Backtrader has already zeroed.

`EquityCurveAnalyzer` samples `broker.getvalue()` once per bar. That is all
it does, and it is why `max_drawdown` is a close-to-close figure.

### `oco_patch.py`

Backtrader runs `_ococheck` *after* `_try_exec`, so on a bar where both
bracket legs are reachable, both can fill before the cancellation propagates.
The result is a position closed twice and a trade in the output that never
happened.

The patch replaces three `BackBroker` methods:

- `next()` — resets a per-bar `_oco_done` set, and before executing any
  order checks whether its OCO group already completed this bar. If so the
  order is cancelled instead of executed.
- `_ococheck()` — records the group in `_oco_done` as soon as one leg
  completes, then cancels the siblings as upstream does.
- `cancel()` — also removes orders from the `submitted` queue, not just
  `pending`. Without this, an exit rebuilt on the same bar the entry filled
  leaves a hanging order.

`apply_oco_guard()` is idempotent and is called at import of
`backtest_runner`. It monkey-patches a global class, so it affects every
`Cerebro` in the process — including ones you build yourself. That is
deliberate (a run that skips it is running with a known fill bug) but it is
also the most fragile coupling in this repository: `_patched_next` is a copy
of upstream's `next()` with the guard spliced into the middle, so a
Backtrader upgrade can change behaviour underneath it without raising. Treat any version bump as a
behavioural change and run the full suite.

## A run, end to end

```
EngineRunSpec
  → check_protocol_version                     engine, MIT
  → assemble_from_graph(graph)                 engine, MIT
  → make_bt_strategy_class(...)                this package
  → Cerebro: feeds, cash, commission
  → cerebro.run()
       per bar:
         broker: execute pending orders        oco_patch guards this
         notify_order / notify_trade           bookkeeping, bracket placement
         next(): _inject_state → on_bar → decide → order
         analyzers: equity point
  → analyzers: trades, equity curve
  → build_closed_trade_metrics(...)            engine, MIT
  → BacktestResult
```

## Where to change what

| You want to change | Edit | Read first |
|---|---|---|
| When or at what price orders fill | `bt_adapter.py` | [execution-model.md](execution-model.md) |
| A field in the trade list or metrics | `bt_analyzers.py`, or the engine for shared metrics | [results.md](results.md) |
| What the strategy can see | `_inject_state()` in `bt_adapter.py` | [strategies.md](strategies.md) |
| Fee or venue handling | `backtest_runner.py` and the engine's execution settings | [execution-model.md](execution-model.md#fees) |
| Anything touching the entry point or licensing | `pyproject.toml` | [../agents_docs/invariants.md](../agents_docs/invariants.md) |

Two rules hold everywhere in this tree. Nothing here may import application
code, and this distribution must never ship a `koval` package — the engine
owns that import namespace, and a second distribution adding to it shadows
unpredictably. Both are pinned by `tests/test_package_metadata.py`.
