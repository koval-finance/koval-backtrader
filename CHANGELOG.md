# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.10.0] - 2026-09-06

### Fixed

- Bracket legs are submitted without Backtrader's submit-time cash pseudo-check.
  Previously a short whose notional approached the cash balance could have its
  take-profit margin-rejected and its stop cancelled through the OCO link,
  leaving an unprotected position to the end of data. Affects every model,
  including `legacy_v1` results for such runs.
- Higher-timeframe bars are injected only once they have closed before the
  primary decision. Multi-timeframe results for both models can change.
- `ohlcv_fixed_v1` uses one nearest-millisecond UTC conversion for events,
  fills and injected state.
- A margin-rejected, cancelled or rejected entry emits `ORDER_REJECTED` instead
  of being dropped silently. A cancellation is only notified on the following
  bar, so the adapter now keeps the cancelled order's reference and still
  recognises the notification as its own.
- Higher-timeframe injection no longer rescans the whole higher-timeframe
  history on every primary bar, which made a two-feed run cost
  O(primary bars x HTF bars). Only trailing bars can still be forming, so the
  scan stops at the first closed bar and fetches only the rows it injects.
  On 8000 hourly bars with 2000 four-hour bars the injection overhead drops
  from 18.2s to 2.0s, and a two-feed run is now within roughly a quarter of a
  single-feed run rather than 3.3 times its cost.
- Timeframe durations are resolved through `koval.engine.timeframe_utils`
  instead of the exchange kline table, which recognised only
  `1m/5m/15m/1h/4h/1d`. Labels such as `30m`, `2h` and `1w` ran on 0.9.1 and
  run again; an unresolvable label is still refused rather than silently
  treated as a sentinel duration. Durations are now required only when a
  second feed is present, since only availability filtering needs them.
- Resolving execution settings no longer imports `koval.exchanges`, which
  pulled the exchange HTTP clients into a package whose invariant is that no
  code path reaches a live venue. Importing the runner is correspondingly
  cheaper.
- An out-of-range `leverage` that cannot be converted to a float raises
  `ValueError` like every other configuration error, instead of escaping as
  `OverflowError`.

### Added

- `ohlcv_fixed_v1.leverage` (optional, default 1) with leverage-aware linear
  cash accounting and a symmetric margin rule for longs and shorts;
  `ORDER_REJECTED` events for insufficient margin;
  take-profit modelled as market-on-touch with full adverse cost; parity
  fixture test against the MIT engine's shipped fixtures.
- `tests/test_engine_signal_parity.py` — decision parity against the MIT
  `LiveEngine` now runs in this gate, where Backtrader is installed.
- Fill records carry `koval_role` (`entry` / `stop_loss` / `take_profit`).

### Changed

- `history_bars` defaults to `koval.engine.history_window.DEFAULT_HISTORY_BARS`
  (1000, previously 300), so a backtest and a paper session warm up identically.
- Higher-timeframe arrays stay `None` until at least one higher-timeframe bar
  has closed, which is the same "unavailable" state a single-feed run injects.
  They previously became empty arrays during warm-up — a third state that a
  strategy guarding with `is None` would fall straight through. The declared
  type is unchanged (`np.ndarray | None`).
- Requires `koval-engine>=0.10.0,<0.11.0`.

## [0.9.1] - 2026-08-14

### Added

- A `docs/` tree for users and contributors: getting started, the execution
  model and its limitations, a field-by-field results reference, a guide to
  writing strategies, an architecture walkthrough, how to write a competing
  backtest engine, and troubleshooting. Shipped in the source distribution.
- Rewrote the README around what the package produces and what it does not
  model, with links into the new documentation.
- `tests/test_docs.py` pins the new tree: every relative link, every section
  anchor, and every page's presence in the index and the README.
  `tests/test_examples.py` executes the documentation's runnable examples, so
  a page whose output has drifted fails the build.

### Changed

- Results can differ from 0.9.0 for some graphs. Calling `on_bar()` every bar
  also advances koval-engine's `PlatformAccountState` every bar, so
  `peak_equity`, `drawdown_pct` and `daily_pnl` now track what happened while
  a position was open rather than only on flat bars. A graph whose risk gate
  reads `account.drawdown_pct`, or whose `state.*` blocks track `since_bar`,
  can therefore reach a different decision than it did before. No block
  listed by `koval blocks` reads either today and the bundled
  `ema_cross_trend` example is unchanged, but a result stored from 0.9.0 is
  no longer guaranteed to reproduce.
- `tests/test_bt_adapter.py` scanned a directory that does not exist in this
  repository and could never fail. Replaced with a real licence-boundary
  guard in `tests/test_package_metadata.py`: the installed koval-engine must
  not import Backtrader.

### Fixed

- `TRADE_CLOSED` events reported `exit_reason: "unknown"` for every close and
  an `exit_price` taken from the bar's close: both were read after the
  adapter had already cleared them. The event now carries the real exit
  reason and the bracket's fill price.
- `TRADE_CLOSED`, `on_close_position()`, `on_sl_update()` and
  `on_tp_update()` received a `trade_id` one lower than the `TRADE_OPENED`
  for the same trade. Every hook and every event now uses the same id,
  counting from 1.
- `DeclarativeStrategy.on_bar()` was never called. The adapter now calls it
  once per bar, in a position or not, as koval-engine's live runner does — a
  strategy that keeps per-bar state no longer behaves differently in a
  backtest than in paper.
- Closed trades carried Backtrader's process-wide trade reference as `id`, so
  a second run in the same interpreter numbered its trades from where the
  first stopped. Ids are now sequential within each run.

## [0.9.0] - 2026-08-03

First public release. Extracted from a private monorepo with a clean history.

### Added

- `BacktraderBacktestEngine`, an implementation of koval-engine's
  `BacktestEngineProtocol` that runs typed strategy graphs through Backtrader's
  `Cerebro` and returns plain metrics, closed trades, and an equity curve.
- Registration under the `koval.backtest_engines` entry-point group as
  `backtrader`, so installing the package is the entire configuration —
  `load_backtest_engine()` finds it with no environment variable.
- `BTStrategyAdapter` and the `make_bt_strategy_class` factory, bridging
  `DeclarativeStrategy` to `bt.Strategy`: bar-window array injection, higher
  timeframe feeds, bracket placement, trailing and breakeven exit updates, and
  an engine-event stream that makes a run auditable after the fact.
- `TradeListAnalyzer` and `EquityCurveAnalyzer` for result extraction.
- `apply_oco_guard()`, fixing a Backtrader bug where a take-profit and a
  stop-loss both eligible within one bar could both fill, producing a trade
  that never happened.
- Guard tests covering the entry-point seam, GPL SPDX headers, the published
  package surface, and the release pipeline.

### Security

- No real-money code path. This package replays historical candles through a
  simulated broker; it holds no credentials and opens no venue connection.
- Releases publish to PyPI through Trusted Publishing (OIDC) with PEP 740
  attestations. No long-lived PyPI credential is used.

[0.9.1]: https://github.com/koval-finance/koval-backtrader/releases/tag/v0.9.1
[0.9.0]: https://github.com/koval-finance/koval-backtrader/releases/tag/v0.9.0
