# Writing your own backtest engine

This package is one implementation of a contract, not the contract itself.
If Backtrader is not what you want — you would rather use a vectorised
engine, a Rust core, or your firm's internal simulator — you can publish a
plugin that koval-engine will find in exactly the same way, and every
strategy graph keeps working.

That is not a hypothetical: the seam exists precisely because Backtrader's
GPL licence had to be kept out of an MIT engine. The generality was a
by-product, and you may as well use it.

## The contract

koval-engine defines it in `koval/engine/backtest_engine.py`:

```python
class BacktestEngineProtocol(Protocol):
    def run(
        self,
        spec: EngineRunSpec,
        on_event: Callable[[dict], None] | None = None,
    ) -> BacktestResult: ...
```

Inputs:

| `EngineRunSpec` field | Type | Notes |
|---|---|---|
| `graph` | `dict` | A validated block graph. Call `assemble_from_graph()` to get a `DeclarativeStrategy`, or interpret it yourself. |
| `feeds` | `dict[str, np.ndarray]` | Timeframe label to an `(N, 6)` float array: `timestamp_ms, open, high, low, close, volume`. |
| `initial_capital` | `float` | |
| `execution_config` | `dict \| None` | Execution assumptions and normalized evidence. Legacy venue settings resolve through `resolve_execution_settings()`; versioned profiles and native Spot use their own explicit contracts. `None` keeps the fee-free legacy path. |
| `protocol_version` | `int` | Defaults to `2`; protocol `1` remains accepted without runtime boundaries. Call `check_protocol_version(spec)` first and let it raise. |
| `execution_contract_version` | `int` | Defaults to `1`. Negotiate the requested execution contract before running; native Spot requires `2`. |
| `required_execution_capabilities` | `tuple[str, ...]` | Defaults to `()`. Caller requirements combine with graph-inferred capabilities; refuse every requirement the plugin cannot implement. |
| `runtime_contract` | `dict \| None` | Optional protocol-2 warmup/evaluation interval, account baselines, bar-close clock and terminal policy. `None` preserves legacy boundaries. |

Protocol validation alone does not establish execution support. Call
`negotiate_execution_capabilities(spec, offered)` with an `ExecutionCapabilities`
record before constructing a strategy, feed or broker. Implement every negotiated
feature, including boundary coverage and terminal behavior when offering
`runtime_boundaries_v1`. The native [Spot lane](spot-multi-order.md) requires its
own capability set and binding evaluation interval; it refuses `runtime_contract`.

Output is a `BacktestResult` with `metrics`, `trades`, and `equity_curve`.
Nothing enforces the shape of those dicts, so match
[results.md](results.md) if you want existing consumers — dashboards, the
CLI's `--json`, promotion gates — to keep working. Using the engine's
`build_closed_trade_metrics()` for metrics is the cheap way to guarantee it.

`on_event` is optional. Call it with `{"event_type", "bar_index",
"timestamp_ms", "payload"}` dicts if your engine can explain itself; skip it
if it cannot.

## A working example

This loader example values a buy-and-hold price series. It ignores graph decisions
and venue fee labels, and does not model execution costs or produce a trade ledger.
It refuses requested execution capabilities and contract v2, so it cannot silently
stand in for the supported historical execution lanes.

```python
# my_engine.py
from koval.engine.backtest_engine import (
    BacktestResult,
    EngineRunSpec,
    ExecutionCapabilities,
    check_protocol_version,
    negotiate_execution_capabilities,
)
from koval.engine.timeframe_utils import ordered_timeframes


class BuyAndHoldEngine:
    """Ignores the graph entirely and holds the primary feed from end to end."""

    def run(self, spec: EngineRunSpec, on_event=None) -> BacktestResult:
        check_protocol_version(spec)
        negotiate_execution_capabilities(
            spec, ExecutionCapabilities(execution_contract_versions=(1,), features=())
        )
        if spec.execution_config is not None and set(spec.execution_config) - {
            "exchange",
            "exchange_type",
        }:
            raise ValueError("this example does not support execution assumptions or evidence")
        candles = spec.feeds[ordered_timeframes(list(spec.feeds))[0]]
        first, last = float(candles[0][4]), float(candles[-1][4])
        final = spec.initial_capital * last / first
        return BacktestResult(
            metrics={
                "initial_capital": spec.initial_capital,
                "final_capital": final,
                "total_pnl": final - spec.initial_capital,
                "total_trades": 1,
            },
            trades=[],
            equity_curve=[
                {"timestamp": int(row[0]), "equity": spec.initial_capital * float(row[4]) / first}
                for row in candles
            ],
        )


def create_engine() -> BuyAndHoldEngine:
    return BuyAndHoldEngine()
```

Point the engine at it without packaging anything:

```bash
PYTHONPATH=. KOVAL_BACKTEST_ENGINE=my_engine \
  koval backtest koval-examples/graphs/ema_cross_trend.json \
  --data koval-examples/data/sample-1h.csv --timeframe 1h
```

```
final_capital            20108.118679660132
initial_capital          10000.0
total_pnl                10108.118679660132
total_trades             1
```

`KOVAL_BACKTEST_ENGINE` takes a **module path**, not an entry-point name, and
the module must expose `create_engine()`. It is a development override — it
wins over entry-point discovery, and it is read on every call to
`load_backtest_engine()`, so a stale value in your shell is a plausible
explanation for "the wrong engine ran".

## Publishing it

Declare the entry point and installation becomes the whole configuration
story:

```toml
[project.entry-points."koval.backtest_engines"]
my-engine = "my_engine:create_engine"
```

`load_backtest_engine()` with no argument takes the member named
`backtrader`; `load_backtest_engine("my-engine")` takes yours by name. If you
intend to replace this package rather than sit alongside it, register under
the name `backtrader` and do not install both — two distributions claiming
one entry-point name resolve unpredictably.

Entry points are read from **installed distribution metadata**, not from
`pyproject.toml`. Adding one to a project you already installed in editable
mode does nothing until you reinstall. This costs everyone an hour exactly
once.

## Guards worth copying

The tests in this repository that have earned their keep, and that any engine
plugin benefits from:

| Test | What it protects |
|---|---|
| `tests/test_entry_point.py` | The package installs and is discovered with no environment variable, and the declared entry point matches the installed one. Without this, your plugin can install cleanly and do nothing. |
| `tests/test_package_metadata.py` | The engine dependency stays a version *range*, and the distribution never ships a `koval` package. |
| `tests/test_examples.py` | The documented quickstart is executed, not eyeballed. |

The namespace rule deserves emphasis if you are forking this repository:
koval-engine ships `koval/__init__.py`, which makes `koval` a regular
package. A second distribution that also installs into `koval/` does not
merge with it — one shadows the other depending on `sys.path` order, and
which one wins varies by machine. Name your top-level package something else,
however natural `koval.adapters.yours` looks.

## Licensing

The plugin boundary is also a licence boundary. This package is GPL-3.0
because Backtrader is; an engine built on a permissively licensed simulator
can be MIT, BSD, or proprietary. The engine stays MIT either way, because it
never imports an implementation — it only reads entry points.

If you fork *this* package, the GPL travels with it: derivative works are
GPL-3.0-or-later, and distributing a binary or service built on it carries
the source-availability obligation. Starting from the protocol rather than
from this code is the way to avoid that.

## See also

- [architecture.md](architecture.md) — how this implementation is put together.
- [../CONTRIBUTING.md](../CONTRIBUTING.md) — if the change belongs here instead.
