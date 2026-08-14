# koval-backtrader

[![CI](https://github.com/koval-finance/koval-backtrader/actions/workflows/ci.yml/badge.svg)](https://github.com/koval-finance/koval-backtrader/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/koval-backtrader.svg)](https://pypi.org/project/koval-backtrader/)
[![Python](https://img.shields.io/pypi/pyversions/koval-backtrader.svg)](https://pypi.org/project/koval-backtrader/)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](https://github.com/koval-finance/koval-backtrader/blob/main/LICENSE)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/koval-finance/koval-backtrader/badge)](https://scorecard.dev/viewer/?uri=github.com/koval-finance/koval-backtrader)

The [Backtrader](https://github.com/mementum/backtrader) backtest engine for
[koval-engine](https://github.com/koval-finance/koval-engine).

Give it a strategy graph and a series of candles; it replays them bar by bar
through Backtrader's `Cerebro` and hands back metrics, closed trades, an
equity curve, and — if you ask for it — the full stream of decisions that
produced them. No Backtrader object crosses back into your code.

**Status:** 0.9.x. The public API may change before 1.0.

## Why this is a separate package

koval-engine is MIT-licensed and defines backtesting as a plugin contract —
`BacktestEngineProtocol` — without shipping an implementation. Backtrader is
GPL-3.0, and linking it would make the combined work GPL. Keeping the
Backtrader-specific code in its own distribution is what lets the engine stay
MIT while still giving you a working backtester.

The consequence is worth stating plainly: **koval-engine on its own cannot
run a backtest.** It needs an engine plugin, and this is one.

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

## Quickstart

Both commands run offline, against the example graph and synthetic candles
that ship inside koval-engine:

```bash
koval examples --copy .
koval backtest koval-examples/graphs/ema_cross_trend.json --data koval-examples/data/sample-1h.csv --timeframe 1h
```

```
avg_loss                 -111.30727867988314
avg_win                  204.6464062326714
final_capital            10969.32757778207
initial_capital          10000.0
loss_count               6
max_drawdown             2.8042923980132675
profit_factor            2.451428857241573
total_pnl                969.3275777820709
total_trades             14
win_count                8
win_rate                 57.14285714285714
```

Those two commands are executed verbatim by
[`tests/test_examples.py`](https://github.com/koval-finance/koval-backtrader/blob/main/tests/test_examples.py)
on every CI run, so a quickstart that has rotted fails the build.

## From Python

Note what this does not contain: any mention of `koval_backtrader`. Consumer
code depends on the MIT engine API alone, and the adapter arrives through the
entry point.

```python
from koval.engine.backtest_engine import EngineRunSpec, load_backtest_engine

spec = EngineRunSpec(graph=graph, feeds={"1h": candles}, initial_capital=10_000.0)
result = load_backtest_engine().run(spec)

print(result.metrics["total_trades"], result.metrics["final_capital"])
```

`load_backtest_engine()` returns this adapter because it is installed.
Install a different plugin and the same code runs on that instead.

Pass `on_event=` to receive every decision as it happens — signals, filters,
orders, fills, trades — which is what makes a run auditable rather than a
number that appeared from nowhere.

## What you get back

`BacktestResult` is three pieces of plain data:

| | |
|---|---|
| `metrics` | `initial_capital`, `final_capital`, `total_pnl`, `total_trades`, `win_rate`, `profit_factor`, `win_count`, `loss_count`, `avg_win`, `avg_loss`, `max_drawdown` |
| `trades` | one dict per closed trade: prices, times, size, PnL, commission, exit reason, and whatever the strategy recorded about why it entered |
| `equity_curve` | account value at every bar |

Every metric except `max_drawdown` is computed by koval-engine's shared code,
so a backtest and a paper run report the same numbers by construction rather
than by coincidence. Field-by-field definitions are in
[docs/results.md](https://github.com/koval-finance/koval-backtrader/blob/main/docs/results.md).

## What it does not model

A backtest is a claim about what would have happened. Here is what this one
leaves out:

- **Execution modelling is fees-only.** Commission comes from the venue
  configuration. There is no slippage, no spread, no funding, no partial
  fills, and no order-book depth. Market entries fill at the next bar's open;
  a stop that gaps fills at the gap.
- **One position at a time, one instrument.** No pyramiding, no hedging, no
  portfolio.
- **No real-money trading.** This package replays historical candles through
  a simulated broker. It holds no credentials and opens no venue connection.
- **No live execution.** Paper and exchange-sandbox trading live in
  koval-engine and do not use this package.

Every simplification points the same way: results here are an upper bound on
what the same strategy would have achieved, not an estimate of it. The full
accounting is in
[docs/execution-model.md](https://github.com/koval-finance/koval-backtrader/blob/main/docs/execution-model.md),
and it is the page to read before trusting a number.

## How it fits together

```
EngineRunSpec ─▶ assemble_from_graph()   koval-engine, MIT
              ─▶ BTStrategyAdapter       this package: state in, orders out
              ─▶ Cerebro                 backtrader, GPL-3.0
              ─▶ analyzers               trades + equity curve
              ─▶ BacktestResult          plain dicts, no backtrader objects
```

```
src/koval_backtrader/
├── backtest_runner.py   BacktraderBacktestEngine and the create_engine factory
├── bt_adapter.py        DeclarativeStrategy → bt.Strategy bridge, HTF injection
├── bt_analyzers.py      equity-curve and closed-trade analyzers
└── oco_patch.py         guard against a Backtrader OCO ghost-trade bug
```

That last file is worth a sentence. Stock Backtrader evaluates OCO
cancellation after execution, so on a bar where both a stop-loss and a
take-profit are reachable, both can fill — closing a position twice and
inventing a trade that never happened. The bundled patch cancels the sibling
before it can execute.

## Documentation

| | |
|---|---|
| [Getting started](https://github.com/koval-finance/koval-backtrader/blob/main/docs/getting-started.md) | Install, first backtest, using your own candles |
| [Execution model](https://github.com/koval-finance/koval-backtrader/blob/main/docs/execution-model.md) | Fill rules, fees, sizing, and everything not modelled |
| [Results reference](https://github.com/koval-finance/koval-backtrader/blob/main/docs/results.md) | Every metric, trade field, and event |
| [Writing strategies](https://github.com/koval-finance/koval-backtrader/blob/main/docs/strategies.md) | Hooks, injected state, a complete working example |
| [Architecture](https://github.com/koval-finance/koval-backtrader/blob/main/docs/architecture.md) | Code walkthrough, and where to change what |
| [Writing your own engine](https://github.com/koval-finance/koval-backtrader/blob/main/docs/custom-engines.md) | Replacing Backtrader behind the same contract |
| [Troubleshooting](https://github.com/koval-finance/koval-backtrader/blob/main/docs/troubleshooting.md) | Symptoms, causes, fixes |

## Compatibility

Requires Python 3.11+ and `koval-engine>=0.9.0,<0.10.0`. The upper bound
tracks the engine's backtest protocol version; when the engine raises it,
this package needs a release rather than a looser pin.

Backtrader is pinned loosely (`>=1.9.78`) but coupled tightly: `oco_patch.py`
reproduces broker internals, so treat any Backtrader upgrade as a
behavioural change and run the full suite.

## Contributing

See [CONTRIBUTING.md](https://github.com/koval-finance/koval-backtrader/blob/main/CONTRIBUTING.md).
Contributions require a DCO sign-off (`git commit -s`). Tests come first
here: this package decides where simulated orders fill, and those numbers are
what people use to judge whether a strategy is worth money.

Good first contributions are the gaps named above — slippage, funding,
partial fills — and anything that makes a result easier to distrust.
Strategy blocks and metric definitions belong to
[koval-engine](https://github.com/koval-finance/koval-engine), so file those
there and every backtest engine benefits.

`./scripts/verify.sh` is the definition of done: lint, formatting, and the
full suite. Exit code 0, nothing else.

## Licence

GPL-3.0-or-later. See [LICENSE](https://github.com/koval-finance/koval-backtrader/blob/main/LICENSE).

Note the asymmetry: this package is GPL because it links Backtrader, while
koval-engine remains MIT. Using koval-engine alone does not subject your code
to the GPL; distributing something that links this package does. If that is a
problem for you, the plugin seam is the way out — write an engine against the
same protocol on a permissively licensed simulator, and the engine will load
it instead.

This repository starts at v0.9.0 with a clean history; prior development
happened in a private monorepo. [koval.finance](https://koval.finance) is a
separate commercial hosted product built on these components.
