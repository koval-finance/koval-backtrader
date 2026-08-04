# koval-backtrader

[![CI](https://github.com/koval-finance/koval-backtrader/actions/workflows/ci.yml/badge.svg)](https://github.com/koval-finance/koval-backtrader/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/koval-backtrader.svg)](https://pypi.org/project/koval-backtrader/)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](https://github.com/koval-finance/koval-backtrader/blob/main/LICENSE)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/koval-finance/koval-backtrader/badge)](https://scorecard.dev/viewer/?uri=github.com/koval-finance/koval-backtrader)

The [Backtrader](https://github.com/mementum/backtrader) backtest engine for
[koval-engine](https://github.com/koval-finance/koval-engine).

**Status:** 0.9.x. The public API may change before 1.0.

## Why this is a separate package

koval-engine is MIT-licensed and defines backtesting as a plugin contract —
`BacktestEngineProtocol` — without shipping an implementation. Backtrader is
GPL-3.0, and linking it would make the combined work GPL. Keeping the
Backtrader-specific code in its own distribution is what lets the engine stay
MIT while still giving you a working backtester.

The consequence is worth stating plainly: **koval-engine on its own cannot run
a backtest.** It needs an engine plugin, and this is one.

```
koval-engine (MIT)  ──entry-point group "koval.backtest_engines"──▶  koval-backtrader (GPL-3.0)
```

## Install

```bash
pip install koval-engine koval-backtrader
```

That is the entire setup. The engine discovers this package through its entry
point — there is no environment variable to export, no module to import, and
no configuration file.

## Use

Through the engine's CLI, against the example data that ships inside
koval-engine:

```bash
koval examples --copy .
koval backtest koval-examples/graphs/ema_cross_trend.json --data koval-examples/data/sample-1h.csv --timeframe 1h
```

Or through the Python API, where this package never appears by name:

```python
from koval.engine.backtest_engine import EngineRunSpec, load_backtest_engine

spec = EngineRunSpec(graph=graph, feeds={"1h": candles}, initial_capital=10_000.0)
result = load_backtest_engine().run(spec)

print(result.metrics)
```

`load_backtest_engine()` returns this adapter because it is installed. Install
a different plugin and the same code runs on that instead.

## What it does

Assembles a strategy from a typed graph, runs it bar by bar through
Backtrader's `Cerebro`, and returns plain data: metrics, closed trades, and an
equity curve. No Backtrader object crosses the boundary back into your code.

Bracket orders are placed as stop-loss and take-profit pairs, and a bundled
patch fixes a Backtrader bug where both legs of a bracket can fill within the
same bar and produce a trade that never happened.

## What it does not do

- **Execution modelling is fees-only.** Commission is applied from the venue
  configuration. There is no slippage model, no funding, no partial fills, and
  no order-book depth. Results are an upper bound on what the same strategy
  would have achieved, not an estimate of it.
- **No real-money trading.** This package replays historical candles through a
  simulated broker. It holds no credentials and opens no venue connection.
- **No live execution.** Paper and exchange-sandbox trading live in
  koval-engine and do not use this package.

## Compatibility

Requires Python 3.11+ and `koval-engine>=0.9.0,<0.10.0`. The upper bound
tracks the engine's backtest protocol version; when the engine raises it, this
package needs a release rather than a looser pin.

## Contributing

See [CONTRIBUTING.md](https://github.com/koval-finance/koval-backtrader/blob/main/CONTRIBUTING.md).
Contributions require a DCO sign-off (`git commit -s`). Tests come first here:
this package decides where simulated orders fill.

## Licence

GPL-3.0-or-later. See [LICENSE](https://github.com/koval-finance/koval-backtrader/blob/main/LICENSE).

Note the asymmetry: this package is GPL because it links Backtrader, while
koval-engine remains MIT. Using koval-engine alone does not subject your code
to the GPL; distributing something that links this package does.

This repository starts at v0.9.0 with a clean history; prior development
happened in a private monorepo. [koval.finance](https://koval.finance) is a
separate commercial hosted product built on these components.
