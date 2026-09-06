# Execution-cost validation record

This record accompanies the [model specification](execution-model.md) and
[research/decision record](execution-research.md). It covers the working-tree
milestone prepared on 2026-09-05, without a commit or release.

## Scope and compatibility

Implemented `ohlcv_fixed_v1`: deterministic adverse half-spread and slippage,
uniform explicit fees on actual fill notional, limit caps, cost attribution,
resolved replay metadata, UTC v1 timestamps and account reconciliation.
Legacy valid fill/fee configurations preserve numeric behavior and legacy
trade fields; metadata is additive. Invalid/unknown old settings now fail
instead of silently defaulting. Legacy analyzer funding injection remains a
compatibility hook, not an account-level funding implementation.

Funding, maker/taker classification, partial fills, liquidity limits, adaptive
impact, extra latency, exchange filters, margin and liquidation are deferred
with input requirements and acceptance criteria in the decision record. No
funding debit/credit or partial-fill implementation is claimed. Tests prove
these unsupported requests are rejected and missing funding is disclosed,
not that unavailable features have been simulated.

## Failing-to-passing evidence

The initial `./scripts/verify.sh` passed **106 tests**. Fourteen new baseline
characterization cases then passed against the unchanged implementation:
next open, delayed bracket protection, stop-first ambiguity, long/short limit
and stop gaps/touches, unlimited volume and old fee/PnL semantics.

Subsequent observed red runs, before their corresponding implementation:

| Run | Observed result | Cause / resolution |
|---|---|---|
| Initial accepted behavior | 79 failed, 14 passed | Unadjusted prices, absent cost/metadata fields, ignored invalid configs; added parser, broker and audit |
| Input and audit robustness | 25 failed, 93 passed | Bad OHLCV/capital/order arithmetic, mutable callback metadata, silently ignored old cost fields; added validation and payload copying |
| Source identity and funding audit | 8 failed, 127 passed | Missing source fingerprint, unsupported old setting values and cosmetic funding accepted in v1; added fingerprint and rejection |
| UTC/replay review | 5 failed, 130 deselected | Naive fill time shifted by host timezone; unsupported or oversized legacy snapshots could not replay; corrected UTC and parser bounds |

The first green pass also exposed a floating-point accounting issue:
subtracting closed PnL-derived commission from total commission left a tiny
nonzero "open commission" while flat. Open commission now comes directly from
fills belonging to the open trade; v1 commission uses executed fee amounts.

The new real-graph multi-trade fixture initially generated just one crossover;
it was replaced with a deterministic oscillating series. This was a fixture
correction, not a production failure or weakened assertion. It now proves
multiple trades, stable IDs, repeated results/events and multi-timeframe runs.

## Coverage

- [`test_execution_realism.py`](../tests/test_execution_realism.py): public
  runner probes, long/short entry/exit costs, limit touches/improvement/caps,
  stop gaps, OCO ambiguity, component allocation, zero-cost mode, unavailable
  funding (positive/negative/zero synthetic records rejected), open-position
  costs, cash rejections, metadata replay, offline execution, input validation,
  source identity and UTC timestamps.
- [`test_execution_broker.py`](../tests/test_execution_broker.py): direct market
  exits for both position directions, with broker-value checks.
- [`test_backtest_runner.py`](../tests/test_backtest_runner.py): real graphs,
  multiple trades/timeframes, deterministic event/result equality and zero-cost
  compatibility. [`test_bt_analyzers.py`](../tests/test_bt_analyzers.py): both
  signs of analyzer-only funding rejected under v1; old hook preserved.
- Existing entry-point, SPDX/license boundary, OCO, adapter, packaging and
  documentation guards are retained unchanged. Documentation guard failures
  are resolved in the documents, not by editing guard expectations.

## Worked reconciliation

Input: capital 10,000; two-unit long; reference entry 100; stop 90; next exit
bar opens at 85. Costs: full spread 20 bps, slippage 10 bps, commission 4 bps.

| Component | Entry | Exit | Total |
|---|---|---|---|
| Matched reference price | 100 | 85 | — |
| Actual fill price | 100.20 | 84.83 | — |
| Spread cost | 0.20 | 0.17 | 0.37 |
| Slippage cost | 0.20 | 0.17 | 0.37 |
| Commission | 0.080160 | 0.067864 | 0.148024 |
| Funding booked | 0 | 0 | 0, unavailable |

Reference PnL is `2 * (85 - 100) = -30`. Actual-price gross PnL is
`2 * (84.83 - 100.20) = -30.74`. Net PnL is `-30.888024`.
Broker capital and the final equity point both equal **9969.111976**:

```
10000 - 30 - 0.37 - 0.37 - 0.148024 = 9969.111976
10000 + (-30.888024)                = 9969.111976
```

The gap loss is already in reference PnL; it is not counted again as slippage.
The embedded 0.74 cost must not be subtracted from actual-price PnL a second
time. Open-position tests separately reconcile unrealized PnL and entry fees.

## Verification and review

`./scripts/verify.sh` completed with exit **0**: lint passed, **60 files**
formatted, and **280 tests passed** with one skip. `git diff --check` also
passed. No guard test was modified or weakened.

The review covered tracked diffs and every new source, test and documentation
file, followed by the broader
[readiness review](#platform-readiness-review) recorded below. Both the
look-ahead and the timestamp defect that review found are now closed, each
with the regression test named in its section. Earlier corrections included
UTC v1 conversion, replayable legacy bounds, exact open-commission
attribution, payload isolation, explicit unavailable funding, and stale
performance-bound claims.

Known constraints remain explicit: synthetic prices may exceed candle ranges;
limit caps can reduce assumed costs to zero; filled quantity has unlimited
liquidity; uniform fees cannot identify maker behavior; historical funding and
venue margin/filters are unavailable; brackets wait one bar after entry;
intrabar ordering is a queue assumption; old timezone-sensitive legacy epoch
conversion remains for reproduction. This is not a sandbox-promotion approval.

No sibling repository was edited, no runtime dependency was added, and no Git
write or publication was performed. Human-managed releases must precede
application pin updates. Suggested commit message:
`Add versioned deterministic OHLCV execution costs`.

## Platform readiness review

Reviewed on 2026-09-05 against the complete working tree, then re-reviewed
before the 0.10.0 release. The original verdict was **changes required before
claiming trustworthy multi-timeframe execution**. Both defects it raised are
now closed, and each section below records what closed it and which test now
guards it.

Engine-side work is tracked in koval-engine's
[public plan](https://github.com/koval-finance/koval-engine/blob/main/agents_docs/execution_contract_plan.md).
How an application adopts the model is outside this repository: the plugin's
contract is `EngineRunSpec` in and `BacktestResult` out, and anything a
consumer must do to carry a version through its own request and storage layers
belongs in that consumer's own documentation.

The plugin seam itself is verified by `tests/test_entry_point.py`:
`load_backtest_engine()` resolves
`koval_backtrader.backtest_runner.BacktraderBacktestEngine` from the entry-point
group, so installing the distribution is the whole configuration story.

### P1: higher-timeframe candles expose future information

Owner: koval-backtrader. Relevant code:
[`backtest_runner.py`](../src/koval_backtrader/backtest_runner.py), feed creation;
[`bt_adapter.py`](../src/koval_backtrader/bt_adapter.py), `_inject_state()`.

The feed converter uses the supplied timestamps unchanged, and HTF injection
includes `htf.close.get(ago=0, size=m)`. Binance identifies futures klines by
their opening time, as documented in its
[official connector source](https://github.com/binance/binance-futures-connector-python/blob/main/binance/um_futures/market.py)
(accessed 2026-09-05). The engine's `BinanceAdapter._rows_to_ndarray()` retains
that time. A higher-timeframe candle can therefore be current in Backtrader
before its final OHLCV would have been available to the strategy.

An offline public-runner probe reproduced this with eight hourly bars
beginning at `1704067200000`, and four-hour bars aggregated from those same
rows. The first three hourly candles were identical, around price 100. Only
the fourth candle's future close and corresponding high/low changed, which
also changed the unfinished HTF aggregate:

| First four-hour final close | HTF closes seen in the first hourly callback | HTF closes seen in the second hourly callback |
|---|---|---|
| 50 | `[50]` | `[50]` |
| 500 | `[500]` | `[500]` |

The first hourly decision has access to a four-hour result that will not be
available until hour four. A probe entering when the HTF close exceeded 200
produced no early entry in the 50 case, but filled an entry on hourly bar two
in the 500 case, before the changed hourly candle was observable. This is a
look-ahead defect in both legacy and fixed execution, not an execution-cost
approximation. Existing HTF tests verify presence of arrays, and the existing
future-bar test covers a single feed; neither checks HTF availability.

Required correction: define candle opening time separately from information
availability, and inject only HTF bars closed by the primary decision time.
Add a test that changes unfinished HTF OHLCV and proves all earlier strategy
inputs and decisions remain unchanged. Cover the exact close boundary,
history windows, missing bars and warmup. State how corrected timing is
versioned so historical legacy outputs remain identifiable.

**Closed in 0.10.0.** `_inject_state()` injects an HTF bar only when
`htf_open + htf_duration <= primary_open + primary_duration`, and a second feed
without declared timeframe durations now fails loudly rather than guessing.
The rule applies to both execution models, because the previous behavior was a
defect rather than a documented semantic; the changelog says so and the
metadata carries `htf_availability_legacy_defect_fixed: true`, so a
recomputed legacy result stays identifiable. Regression coverage:
`tests/test_bt_adapter_htf.py::test_htf_bar_is_visible_only_after_it_closes`
(exact close boundary),
`::test_mutating_an_unfinished_htf_bar_does_not_change_earlier_inputs`
(the reproducer above, inverted into an assertion) and
`::test_a_second_feed_without_declared_timeframes_fails_loudly`.

### Consumer integration is out of scope for this repository

The original review also raised two findings against the application that
consumes this plugin: its request layer did not carry the new
`execution_model` through to `EngineRunSpec`, and its trade records dropped
the per-trade execution attribution this package returns. Both are consumer
concerns, not plugin defects, and they are tracked wherever that consumer is
developed.

They are recorded here only as a warning that applies to any consumer:
**installing this plugin does not by itself activate `ohlcv_fixed_v1`.** A
caller that does not pass an `execution_config` gets `legacy_v1`, and a caller
that builds its own configuration dictionary must forward `exchange`,
`exchange_type` and `execution_model` verbatim. If per-trade attribution
matters, persist `execution_costs` from each trade and
`metrics["execution_model"]` from the run; a whitelist that copies only the
legacy trade fields will silently drop them.

### P2: event and ledger timestamps can disagree by one millisecond

Owner: koval-backtrader. Relevant code:
[`bt_adapter.py`](../src/koval_backtrader/bt_adapter.py), `_timestamp_ms()`, and
[`execution_broker.py`](../src/koval_backtrader/execution_broker.py), `_record_fill()`.

The adapter truncates Backtrader's floating-point datetime to milliseconds;
the ledger rounds it. The input validator accepts integer millisecond times,
including those that do not lie exactly on a second. A public-runner probe
with hourly bars starting at `1704067200002` yielded:

| Representation of the same entry fill | Timestamp |
|---|---|
| Input candle | `1704070800002` |
| Fill ledger | `1704070800002` |
| `ORDER_FILLED` event | `1704070800001` |

Required correction: use one consistent v1 millisecond conversion for
strategy state, events and ledger records. Add failing tests with nonzero
millisecond offsets, including a close near a time boundary. Keep any legacy
compatibility behavior explicit. UTC attachment alone does not fix rounding.

**Closed in 0.10.0.** `time_conversion.utc_ms` / `num2utc_ms` is the one
conversion; the adapter, the execution broker and the trade-list analyzer all
use it under `ohlcv_fixed_v1`, and legacy keeps the truncating conversion
explicitly, in a commented branch. Regression coverage:
`tests/test_execution_realism.py::test_v1_event_ledger_and_strategy_timestamps_agree_at_millisecond_offsets`,
which runs the `1704067200002` probe above and asserts the injected state, the
`ORDER_FILLED` event and the fill ledger all report `1704070800002`.

### Release and consumer acceptance

The gate passed **280 tests**, lint and formatting, with both defects closed
under test-first changes and no guard weakened.

A caller that wants to explain an old run later must keep the inputs, not just
the outputs. Candles should be stored as immutable bytes, or as a durable
dataset reference with a verified content hash, alongside the graph and the
resolved `metrics["execution_model"]`. Reloading candles by exchange, symbol,
timeframe and date range does not bind a stored result to the bytes that
produced it: a cache refill or an upstream source correction can then change
the evidence behind a result that has already been reported.

Funding, maker/taker classification, partial fills, market impact and
liquidation remain the explicit limitations listed earlier. Their absence
does not invalidate the arithmetic of an honestly disclosed fixed-cost
model.
Paper/sandbox promotion still needs the separate parity and drift checks
specified in the [research record](execution-research.md#staged-follow-up-and-acceptance-criteria).

## Files changed

| Area | Files |
|---|---|
| New model code | `src/koval_backtrader/execution_config.py`, `execution_broker.py`, `execution_audit.py`, `time_conversion.py` |
| Wiring and results | `src/koval_backtrader/backtest_runner.py`, `bt_adapter.py`, `bt_analyzers.py` |
| New tests | `tests/test_execution_realism.py`, `tests/test_execution_broker.py`, `tests/test_engine_signal_parity.py`, `tests/test_parity_fixtures.py` |
| Extended tests | `tests/test_backtest_runner.py`, `tests/test_bt_analyzers.py`, `tests/test_bt_adapter.py`, `tests/test_bt_adapter_htf.py`, `tests/test_state_injection_arrays.py`, `tests/test_public_surface.py` |
| New records | `docs/execution-research.md`, `docs/execution-validation.md`, `docs/execution-plan.md` |
| User documentation | `README.md`, `docs/README.md`, `docs/architecture.md`, `docs/execution-model.md`, `docs/results.md`, `docs/troubleshooting.md`, `docs/getting-started.md` |
| Agent documentation | `AGENTS.md`, `agents_docs/README.md`, `agents_docs/architecture.md`, `agents_docs/invariants.md`, `agents_docs/testing.md`, `agents_docs/release_process.md` |
