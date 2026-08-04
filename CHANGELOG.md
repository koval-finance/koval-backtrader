# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.9.0]: https://github.com/koval-finance/koval-backtrader/releases/tag/v0.9.0
