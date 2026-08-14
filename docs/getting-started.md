# Getting started

From nothing to a backtest you can read, in about five minutes.

## Requirements

Python 3.11 or newer. Everything else — koval-engine, Backtrader, numpy,
pandas — is pulled in by the install.

## Install

```bash
pip install koval-engine koval-backtrader
```

Both packages are needed. koval-engine is the strategy engine and the CLI;
this package is the thing that can actually run a backtest. Installing the
engine alone gives you a `koval backtest` command that refuses to do
anything, which is by design and explained in the
[README](../README.md#why-this-is-a-separate-package).

From a checkout, for development:

```bash
git clone https://github.com/koval-finance/koval-backtrader.git
cd koval-backtrader
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

## Check that the plugin was found

```bash
python -c "from koval.engine.backtest_engine import load_backtest_engine; print(type(load_backtest_engine()).__name__)"
```

```
BacktraderBacktestEngine
```

If that raises `NoBacktestEngineError`, the package is either not installed
in the interpreter you just used or its metadata is stale — see
[troubleshooting.md](troubleshooting.md#nobacktestengineerror-but-the-package-is-installed).

There is nothing to configure. No environment variable, no import, no config
file: the engine discovers this package through a Python entry point, and
installing it is the whole of the setup.

## Your first backtest

koval-engine ships example graphs and a synthetic price file, so this runs
offline and produces the same numbers on every machine.

```bash
koval examples --copy .
koval backtest koval-examples/graphs/ema_cross_trend.json \
  --data koval-examples/data/sample-1h.csv --timeframe 1h
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

Fourteen trades, eight of them winners, ending about 9.7% up on 1200 hourly
candles. The data is synthetic and the prices never happened — it exists so
the quickstart is reproducible, not so the result means something.

Add `--json` for the machine-readable form, which includes every closed
trade:

```bash
koval backtest koval-examples/graphs/ema_cross_trend.json \
  --data koval-examples/data/sample-1h.csv --timeframe 1h --json
```

Every field in that output is defined in [results.md](results.md).

## The same thing from Python

Note what this code does not contain: any mention of `koval_backtrader`.
Consumer code talks to the MIT engine, and the adapter arrives through the
entry point.

```python
import json

from koval.cli.data import load_csv_candles
from koval.engine.backtest_engine import EngineRunSpec, load_backtest_engine
from koval.examples import example_path

graph = json.loads(example_path("graphs", "ema_cross_trend.json").read_text())["graph"]
candles = load_csv_candles(example_path("data", "sample-1h.csv"))

spec = EngineRunSpec(graph=graph, feeds={"1h": candles}, initial_capital=10_000.0)
result = load_backtest_engine().run(spec)

print(result.metrics["total_trades"], result.metrics["final_capital"])
```

That is [`examples/run_backtest.py`](../examples/run_backtest.py), which the
test suite executes on every run so it cannot rot.

You will notice the Python result differs slightly from the CLI's:
`11030.94` against `10969.33`. Neither is wrong. `koval backtest` always
passes an execution config, so fees are charged; a bare `EngineRunSpec` has
no `execution_config` and runs fee-free. To match the CLI:

```python
spec = EngineRunSpec(
    graph=graph,
    feeds={"1h": candles},
    initial_capital=10_000.0,
    execution_config={"exchange": "binance", "exchange_type": "future"},
)
```

## Watching the decisions

Pass `on_event` and you get the reasoning as it happens, not just the total:

```python
result = load_backtest_engine().run(spec, on_event=print)
```

```
{'event_type': 'FILTER_PASSED',    'bar_index': 255, 'timestamp_ms': 1700907200000, 'payload': {...}}
{'event_type': 'SIGNAL_DETECTED',  'bar_index': 255, 'timestamp_ms': 1700907200000, 'payload': {...}}
{'event_type': 'ORDER_PLACED',     'bar_index': 255, 'timestamp_ms': 1700907200000, 'payload': {...}}
{'event_type': 'ORDER_FILLED',     'bar_index': 256, 'timestamp_ms': 1700910800000, 'payload': {...}}
```

This is the first tool to reach for when a strategy does nothing: the events
show whether a signal fired at all, whether a filter blocked it, and whether
an order was placed but never filled. The full list is in
[results.md](results.md#events).

## Using your own candles

For a CSV, the loader wants a header row and these columns; extras are
ignored, missing ones are an error, and rows must be chronological:

```csv
timestamp_ms,open,high,low,close,volume
1700000000000,100.0000,101.5443,99.7000,101.2406,1000
1700003600000,101.2406,102.8052,100.9369,102.4977,1001
```

```bash
koval backtest my-graph.json --data my-candles.csv --timeframe 1h
```

To fetch real candles instead, the engine's exchange adapters handle it and
cache locally:

```bash
koval backtest my-graph.json --symbol BTCUSDT --timeframe 1h \
  --from 2024-01-01 --to 2024-06-01
```

From Python, `feeds` takes an `(N, 6)` float array per timeframe — column 0
is epoch milliseconds, then open, high, low, close, volume — so any source
you can get into numpy works without a CSV round trip.

## Writing your own strategy

`koval blocks` lists the signals, filters, entries, exits, and risk blocks
you can wire into a graph, and `koval validate my-graph.json` checks one
before you run it. Both belong to koval-engine — file block behaviour issues
[there](https://github.com/koval-finance/koval-engine/issues), so every
backtest engine benefits.

When blocks are not enough, [strategies.md](strategies.md) shows a complete
hand-written strategy running through this adapter.

## Before you trust a number

Read [execution-model.md](execution-model.md). It is short, and it is the
difference between a backtest you can reason about and a number you have
talked yourself into. The summary: fills are optimistic, there is no
slippage or funding, and every simplification points the same way.

## See also

- [results.md](results.md) — every metric, trade field, and event.
- [architecture.md](architecture.md) — how the pieces fit.
- [troubleshooting.md](troubleshooting.md) — when something does not work.
