# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.12.1] - 2026-09-20

### Execution correctness

- Causal matching-price margin for partial entry remainders; protection delay
  anchored to actual first execution. Off-grid funding fails before simulation.
- Adopt the paired MIT strict evidence decoder and conditional realism report;
  retain compatibility with the original decoder on older supported engines.

### Performance

- Pass the boundary-trimmed primary feed to each strategy instance's optional
  `prepare_backtest` hook. Compatible engine builds precompute exact finite-window
  EMA/RSI/ATR values. All simulation decisions, broker matching, events and audit
  records still execute; fresh runs and verification replays both benefit.
- Older engines and custom strategies without the hook continue unchanged.
  No dependency floor, protocol, fill assumption or history-window change.

## [0.12.0] - 2026-09-12

### Added

- Optional MIT runtime boundaries: separate non-trading preroll and evaluation,
  initial account/risk baselines, close-time decisions and explicit retain/flatten
  terminal policies. Engine 0.12 protocol 2 is required for this opt-in contract.
- Persisted costed-run audit linking decisions, intents, orders, fills, events,
  ledger movements, per-decision account snapshots and the final open position.
- Applied evidence hashes/rules and separate model-quality labels on fills.
- Full-runtime comparisons including partial terminal exits; public runtime
  fixtures, future-input mutation tests and an independent Decimal cash oracle.
- Exact-wheel acceptance command and CI matrix across engine 0.11.1/0.12.0
  and Python 3.11/3.13. Manifests identify artifacts and the verification suite.

### Fixed

- Refuse mismatched fee market context, malformed typed evidence and uncovered
  evaluation periods before session start. Worker JSON/pickle transport keeps
  capability requests and normalized evidence intact.
- Continue primary strategy callbacks before a later secondary feed starts.
  Session-start timestamps no longer read preloaded future data.
- Start submission latency at the decision's close time when explicit runtime
  boundaries are supplied, matching actual GraphStrategy/LiveEngine decisions.
- Export actual remaining protective quantities after partial OCO exits,
  including sibling resizing that emits no separate order notification.
- Correct the HTF buffer offset after skipping a forming row: a positive offset
  could expose later preloaded HTF candles instead of the last closed history.
  Explicit runtime boundaries also align HTF history with the rolling primary
  window and defer entries until completed HTF data is available.

The engine dependency range is now `>=0.11.0,<0.13.0`; the older graph binding
fallback remains. Legacy result shape and default terminal behavior are preserved.
No exchange-execution certification is claimed.

## [0.11.1] - 2026-09-12

### Changed

- Prefer engine 0.11.1's public account snapshot hook. Preserve the declared
  `koval-engine>=0.11.0,<0.12.0` range and use the narrow graph fallback only
  with engine 0.11.0, where the public hook is unavailable.
- Remove the obsolete partial-exit equity waiver after observing the comparator
  reject it as unused. Risk-capped instrument quantities and shared-liquidity
  cases now compare independently executed paper and Backtrader results.
- Add full LiveEngine/Backtrader comparisons of graph account snapshots, trade
  prices, quantities, costs, terminal equity and input identities for both
  directions with funding, partial fills, instrument rules, gaps and open endings.
- Document the cross-package compatibility matrix and engine-first release
  order so release CI verifies the corrected pair.

Backtest terminal exposure remains marked at the final close. Select the same
explicit policy for paper replay. No new dependency, execution mode, or
exchange-fidelity claim is introduced.

## [0.11.0] - 2026-09-11

### Added

- Opt-in `ohlcv_realistic_v2`, paired with engine `paper_ohlcv_realistic_v2`:
  entry-bar protection, stop-first ambiguity records and local outcome sensitivity.
- Offline engine evidence for funding, fee schedules, instrument constraints,
  mark-price liquidation, partial fills, volume participation and lagged impact.
  Resolved evidence is JSON-replayable; unsupported currency conversion,
  contract multipliers and cancellation/replacement delays fail explicitly.
- Engine `koval_run_identity_v1` stream/profile/evidence identities, canonical
  Binance/WhiteBIT markets and execution-capability negotiation.
- Actual-fill account snapshots for graph strategies, incremental ledger totals,
  run-local fill/order IDs, partial OCO resizing and session ledger exports.
- Research metrics: expectancy, time under water, exposure, holding time,
  bar-resolution excursions, cost attribution and unfinished-position valuation.
- Distribution guard comparing wheel/sdist package bytes and metadata with
  the working tree, in the test suite and release workflow before upload.

### Fixed

- Removed stale engine divergence exceptions; every published fixture is now
  dispatched by contract/profile version, including v2 and spot refusal.
- Protective updates validate the final pair and refuse stop widening before
  cancellation. Target updates and replacements on the entry-fill bar work.
- Fees, gross trade PnL, funding and liquidation fees reconcile separately.
  Partial-fill quantities/commissions accumulate without losing remaining protection.
- Tick/step normalization survives risk and volume caps. Price improvement is
  signed correctly in reconciliation. Only actual fills consume liquidity.
- Both costed profiles recheck short affordability at a gapped actual fill.
  Evidence fees also govern affordability. Invalid v2 orders, gapped streams,
  missing mark coverage and silently ignored extra timeframes are refused.
- Trailing HTF data no longer repeats the final primary candle or extends trading.
- Open-position costs no longer contaminate closed-trade cost ratios; profit
  locks no longer report locked profit as capital at risk.

### Compatibility and limitations

- Requires published `koval-engine>=0.11.0,<0.12.0`; protocol version remains 1.
  Legacy and fixed-v1 fill timing remain available. Corrected graph account
  inputs, stop validation and HTF handling can change affected strategies.
- Input identification is not certification. The legacy reproducibility grade
  remains `partial`; no result is promoted to fully reproducible by a caller hash.
- Advanced paper parity still has known engine 0.11 gaps in residual marks,
  quantity steps, liquidity reservation and graph account binding. See
  [the release review](docs/execution-validation.md). OHLCV execution is not a
  guarantee of Binance/WhiteBIT outcomes. Application integration is separate.

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

[Unreleased]: https://github.com/koval-finance/koval-backtrader/compare/v0.12.1...HEAD
[0.12.1]: https://github.com/koval-finance/koval-backtrader/compare/v0.12.0...v0.12.1
[0.12.0]: https://github.com/koval-finance/koval-backtrader/compare/v0.11.1...v0.12.0
[0.11.1]: https://github.com/koval-finance/koval-backtrader/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/koval-finance/koval-backtrader/compare/v0.9.1...v0.11.0
[0.9.1]: https://github.com/koval-finance/koval-backtrader/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/koval-finance/koval-backtrader/releases/tag/v0.9.0
