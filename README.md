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

**Status:** 0.13.x. This source candidate requires `koval-engine>=0.13.0,<0.14.0`. The public API may
change before 1.0.

Execution costs are an explicit, versioned model rather than a hidden default.
What it does and does not simulate is stated in full in the
[execution model](https://github.com/koval-finance/koval-backtrader/blob/main/docs/execution-model.md),
and the evidence behind it is in the
[validation record](https://github.com/koval-finance/koval-backtrader/blob/main/docs/execution-validation.md).
Read both before treating a number here as a forecast.

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

This excerpt shows the numeric metrics; the runner also returns execution
metadata identifying the legacy model and its resolved fee assumption.

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

For explicit fixed execution costs, pass:

```python
execution_config = {
    "exchange": "binance",
    "exchange_type": "future",
    "execution_model": {
        "version": "ohlcv_fixed_v1",
        "commission_bps": 4.0,
        "spread_bps": 20.0,
        "slippage_bps": 10.0,
        "leverage": 1.0,
    },
}
spec = EngineRunSpec(
    graph=graph,
    feeds={"1h": candles},
    initial_capital=10_000.0,
    execution_config=execution_config,
)
result = load_backtest_engine().run(spec)
```

These rates are example assumptions. The model worsens matched prices by half
the full spread plus slippage; an **entry** limit is still never filled worse
than its limit, while a take-profit is a market-on-touch order and pays the
full adjustment. It debits commission on actual fills. `leverage` is optional
(default 1, range [1, 125]): an entry debits `notional / leverage` of cash and
is rejected with `ORDER_REJECTED` when `notional / leverage + commission`
exceeds available equity. Store `result.metrics["execution_model"]` with the
graph and candles to preserve resolved assumptions and software identity.

For entry-bar protection, select `ohlcv_realistic_v2` and pair it with
engine `paper_ohlcv_realistic_v2`. Optional normalized engine evidence adds
funding, maker/taker fee assumptions, instrument constraints, liquidation and
an OHLCV partial-fill/latency/impact proxy. The
[configuration contract](https://github.com/koval-finance/koval-backtrader/blob/main/docs/execution-model.md)
includes canonical markets, replayable evidence and capability negotiation.

Every published engine fixture runs. Baseline v1/v2 scenarios agree without
waivers against engine 0.11.1; advanced and full-runtime acceptance is recorded in
[the 0.11 review](https://github.com/koval-finance/koval-backtrader/blob/main/docs/execution-validation.md).
No backtest is certified to reproduce real venue outcomes.

## What you get back

`BacktestResult` is three pieces of plain data:

| | |
|---|---|
| `metrics` | Performance metrics, `execution_model` metadata, `run_identity` (input fingerprints and a reproducibility grade), `research` (expectancy, exposure, MAE/MFE, cost share), and `execution_costs` for both costed profiles |
| `trades` | one dict per closed trade: prices, times, size, PnL, commission, exit reason, and whatever the strategy recorded about why it entered |
| `equity_curve` | account value at every bar |

Performance metrics use koval-engine's shared calculations, with drawdown and
execution auditing added here. Paper and backtest **fills still differ**, so
shared metric definitions do not guarantee equal results. Field definitions are in
[docs/results.md](https://github.com/koval-finance/koval-backtrader/blob/main/docs/results.md).

## What it does not model

- Historical execution evidence is optional. Missing funding, fees, instrument
  constraints or liquidity inputs remain explicitly unavailable or assumed.
- Depth, queue priority, actual bid/ask, venue downtime, cross-asset fee
  conversion, portfolio margin and inverse/delivery contracts are unsupported.
- One instrument, one position, at most two timeframes. No hedging or portfolio.
- Market identity is optional for replay compatibility; provide it to enforce
  spot constraints and make a run identifiable across runtimes.
- This plugin is offline. Paper and sandbox execution belong to koval-engine.
  No credentials or venue order connection are introduced here.

The [execution model](https://github.com/koval-finance/koval-backtrader/blob/main/docs/execution-model.md)
states each approximation and the evidence needed to enable it.

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
├── execution_config.py  strict versioned assumptions and legacy resolution
├── execution_broker.py  synthetic cost prices and actual-fill ledger
├── execution_audit.py   metadata, attribution and reconciliation
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
| [Execution research](https://github.com/koval-finance/koval-backtrader/blob/main/docs/execution-research.md) | Primary sources, decision matrix and staged follow-ups |
| [Execution validation](https://github.com/koval-finance/koval-backtrader/blob/main/docs/execution-validation.md) | Test evidence, reconciliation example and review findings |
| [Execution readiness](https://github.com/koval-finance/koval-backtrader/blob/main/docs/execution-plan.md) | Timing fixes, compatibility decisions and what stays deferred |
| [Results reference](https://github.com/koval-finance/koval-backtrader/blob/main/docs/results.md) | Every metric, trade field, and event |
| [Writing strategies](https://github.com/koval-finance/koval-backtrader/blob/main/docs/strategies.md) | Hooks, injected state, a complete working example |
| [Architecture](https://github.com/koval-finance/koval-backtrader/blob/main/docs/architecture.md) | Code walkthrough, and where to change what |
| [Writing your own engine](https://github.com/koval-finance/koval-backtrader/blob/main/docs/custom-engines.md) | Replacing Backtrader behind the same contract |
| [Troubleshooting](https://github.com/koval-finance/koval-backtrader/blob/main/docs/troubleshooting.md) | Symptoms, causes, fixes |

## Compatibility

The [runtime assurance guide](docs/runtime-assurance.md) covers explicit
warmup/evaluation windows, terminal policies, persisted audit links and
verification of exact installed wheel pairs.

Requires Python 3.11+ and `koval-engine>=0.13.0,<0.14.0`. The upper bound
protects the tested public API and backtest protocol contract; a future engine
series needs installed-pair verification before this range is widened.

Backtrader is pinned loosely (`>=1.9.78`) but coupled tightly: `oco_patch.py`
reproduces broker internals, so treat any Backtrader upgrade as a
behavioural change and run the full suite.

## Contributing

See [CONTRIBUTING.md](https://github.com/koval-finance/koval-backtrader/blob/main/CONTRIBUTING.md).
Contributions require a DCO sign-off (`git commit -s`). Tests come first
here: this package decides where simulated orders fill, and those numbers are
what people use to judge whether a strategy is worth money.

Execution follow-ups include historical funding and protected partial fills;
their required data and acceptance criteria are documented in the research record.
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
