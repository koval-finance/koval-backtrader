# Troubleshooting

Symptoms first, because that is what you have when you arrive here.

## `NoBacktestEngineError`, but the package is installed

```
NoBacktestEngineError: No backtest engine named 'backtrader' is registered (available: none)
```

Entry points are read from **installed distribution metadata**, not from
`pyproject.toml`. Two things produce this:

- The package is installed into a different interpreter than the one you are
  running. Check with `python -c "import koval_backtrader, sys; print(sys.executable)"`.
- An editable install predates the entry-point declaration, leaving stale
  metadata behind. Reinstall: `pip install -e ".[dev]"`, or
  `pip install --force-reinstall koval-backtrader`.

Confirm the fix:

```bash
python -c "from importlib import metadata; print([e.name for e in metadata.entry_points(group='koval.backtest_engines')])"
```

```
['backtrader']
```

If `available:` lists an engine you did not expect, check
`KOVAL_BACKTEST_ENGINE` in your shell — it overrides discovery entirely.

## `ModuleNotFoundError: koval.adapters.backtrader`

That path never existed in the published package. The import is
`koval_backtrader`, top-level, because koval-engine owns the `koval` import
namespace as a regular package and a second distribution adding to it would
shadow rather than merge. Code or notes using the old path predate the split.

## `ProtocolVersionError`

```
ProtocolVersionError: spec protocol_version=2 unsupported; engine speaks 1
```

The installed koval-engine is newer than this adapter and has changed the
shape of `EngineRunSpec` or `BacktestResult`. Upgrade `koval-backtrader`, or
pin the engine back inside the declared range (`>=0.11.0,<0.12.0` for 0.11.x).
The bound is a real statement about protocol compatibility, so widening it
locally trades a clear error for a silent misinterpretation.

## `GraphValidationError`

The graph failed the engine's schema or structural validation, before
Backtrader was involved. `koval validate my-graph.json` gives the same answer
faster, and `koval blocks` lists valid block types with their parameters.
Block semantics belong to koval-engine — report those
[there](https://github.com/koval-finance/koval-engine/issues).

## `ValueError: empty OHLCV feed`

A feed array with zero rows reached the runner. Usually a date range with no
candles, or a CSV filtered down to nothing. Check
`candles.shape` before building the spec.

## The strategy never trades

Work down the event stream; it exists for exactly this.

```python
events = []
load_backtest_engine().run(spec, on_event=events.append)

from collections import Counter

print(Counter(event["event_type"] for event in events))
```

| What you see | What it means |
|---|---|
| Only `SESSION_START` and `SESSION_END` | No signal ever fired. The graph's conditions were never met, or the feed is shorter than the indicator warm-up. |
| `SIGNAL_DETECTED` but no `ORDER_PLACED` | The computed size was zero or negative. Usually a stop-loss equal to the entry price, or a stop on the wrong side of it. |
| `ORDER_PLACED` but no `ORDER_FILLED` | The order was cancelled, rejected, or refused for margin — no event is emitted for any of those. A limit or stop entry that price never reached does the same. |
| `TRADE_OPENED` and no matching `TRADE_CLOSED` | The position was still open when the candles ran out. It counts in `final_capital` but not in `total_trades`. |

To prove the plumbing works, swap the signal block for `signal.every_bar`,
which fires on every closed bar. If that produces trades, the problem is your
conditions, not the adapter.

Also check the arithmetic of your warm-up: history arrays are capped at 300
bars by default (`history_bars`), and a 200-period average over a 100-bar
feed is not a signal, it is noise.

## A trade appears that the strategy never opened

A position that closes twice, on a bar where both the stop and the target
were reachable. That is the Backtrader OCO bug, and it means
`apply_oco_guard()` did not run.

`backtest_runner` calls it at import, so any run through
`load_backtest_engine()` is covered. If you build `Cerebro` yourself, call it
at module import:

```python
from koval_backtrader.oco_patch import apply_oco_guard

apply_oco_guard()
```

It is idempotent, so calling it twice is fine.

## `TRADE_CLOSED` says `unknown`, or trade ids do not start at 1

Both were bugs in 0.9.0: the closed-trade event reported
`exit_reason: "unknown"` and the bar's close price whatever actually
happened, its `trade_id` was one lower than the matching `TRADE_OPENED`, and
`trades[*]["id"]` was Backtrader's process-wide reference, so a second run in
one interpreter carried on numbering from the first. All four are fixed —
upgrade. The trade records themselves were correct in 0.9.0, so stored
results are still usable apart from those ids. See
[results.md](results.md#reading-trade_closed).

## The CLI and the Python API disagree

`koval backtest` always passes an execution config, so it charges venue fees.
A bare `EngineRunSpec` does not, and runs fee-free. Add
`execution_config={"exchange": "binance", "exchange_type": "future"}` to
match. See [execution-model.md](execution-model.md#fees).

## Results look too good

They probably are. Before anything else, check the assumptions listed in
[execution-model.md](execution-model.md#what-is-not-modelled). V2 supports
optional evidence for funding, liquidity and liquidation, with explicit limits. Check
`metrics.execution_model.version`: legacy is fees-only; `ohlcv_fixed_v1`
includes only configured fixed spread/slippage and uniform fees. Then:

- Is `total_trades` large enough for the win rate to mean anything?
- Does the strategy depend on bars where the stop and the target were both
  reachable? On those, the queue order decides the outcome, not the market.
- Did you choose the period after seeing the result?

## Execution configuration is rejected

Versioned requests require explicit commission/spread/slippage values in the
[documented shape](execution-model.md#versions-and-resolved-configuration).
Do not mix old top-level fee overrides with `execution_model`. Cost numbers
must be finite, non-negative numeric values, not strings or booleans. Typos,
unknown modes and unsupported fields fail rather than being ignored. Funding,
fees, instruments, marks and execution proxies require the v2 documented
evidence shape; nonzero cancellation/replacement latency is refused. Valid legacy configs
retain their previous behavior; malformed configs formerly relying on silent
fallback must be corrected explicitly.

## A spot run refuses a short or refuses to start

Both come from the optional `market` block. If the run raises before any bar
(`a spot market cannot use leverage ...`), the block declares a spot product
while `execution_model.leverage` is above 1 — spot has no leverage, so the run
does not start. If instead entries are refused mid-run with `ORDER_REJECTED`
reason `spot_short_unsupported`, the strategy tried to open a short on a spot
market; longs continue normally.

Neither happens without a `market` block. If you want the old unconstrained
behaviour back, remove the block — but then the result no longer claims the
venue could have produced it, and it is graded `partial` for reproducibility.
See [declaring the market](execution-model.md#declaring-the-market).

## A result is graded `partial`

`metrics.run_identity.reproducibility.reasons` names every cause:

- `unversioned_execution_model` — the run used `legacy_v1`. Move to
  `ohlcv_fixed_v1`.
- `market_identity_absent` / `dataset_evidence_absent` — supply the optional
  `market` and `evidence` blocks.
- `timeframe_duration_unresolved` — a single-feed run used a timeframe label
  the engine cannot convert to a duration. The run is still valid; its warm-up
  bounds simply cannot be stated in milliseconds.

`partial` is the retained legacy grade and never becomes `full`, even with
all labels present. Use canonical `comparability`, stream/profile identities
and the release review together. `identified_simulation` identifies inputs;
it is not a certification of archive quality or exchange fidelity.

## A synthetic fill lies outside the candle

This is permitted by `ohlcv_fixed_v1` for market/stop costs. Inspect the fill's
`reference_price`, `fill_price` and cost components: the reference follows
Backtrader's matching rules; the difference is a configured assumption. Limit
prices are still respected. A fill can also fail Backtrader's cash check after
costs, even if its hypothetical submission price was affordable.

## Funding is zero or account PnL differs from the trade sum

`funding_status: "unavailable"` means no funding evidence was applied. V2
can settle a normalized FundingSeries with complete coverage and archived
settlement marks. Account funding is separate from closed-trade statistics.
See [the evidence contract](execution-model.md#accounting-leverage-and-affordability).
For an open position, account PnL includes its unrealized price PnL and entry
commission. Use the two [reconciliation identities](results.md#reconciliation).
Do not subtract spread/slippage a second time from actual-fill PnL.

## Backtrader was upgraded and fills changed

`oco_patch.py` reproduces `BackBroker` internals, so an upstream change to
that code path can alter behaviour without raising anything. Run the full
suite (`./scripts/verify.sh`), and pay particular attention to
`tests/test_oco_patch.py`, which pins the same-bar case. Then read the patch
against the new upstream source before trusting the numbers.

## Nothing here matches

Open an issue with the graph, the candles, and the exact command:
[koval-backtrader issues](https://github.com/koval-finance/koval-backtrader/issues).
A reproduction against koval-engine's bundled example data is worth more than
a paragraph of description. Block behaviour, graph semantics, and metric
definitions live in [koval-engine](https://github.com/koval-finance/koval-engine/issues).

Security problems go through
[private reporting](https://github.com/koval-finance/koval-backtrader/security/advisories/new),
never a public issue.
