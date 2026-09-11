# Execution readiness record

Status: **the two defects this record tracked are closed in 0.10.0**, each
under a test written and observed failing first. The
[readiness review](execution-validation.md#review-findings-corrected-in-this-repository) holds the
evidence; the [research record](execution-research.md) remains the source for
model decisions and for effects that are deliberately still unmodelled.

## Scope and ownership

This plugin owns OHLCV fill semantics, higher-timeframe availability,
timestamp consistency, cost attribution and broker reconciliation. The MIT
engine owns paper parity and market-aware data adapters and caching; see its
[execution contract plan](https://github.com/koval-finance/koval-engine/blob/main/agents_docs/execution_contract_plan.md).
Anything a consuming application must do to carry a version through its own
request and storage layers belongs to that application.

No live-money path, network I/O or new runtime dependency belongs in this
package. SPDX headers, entry points, OCO guards and the one-way import
boundary are preserved. Tests precede each logic change.

## 1. Close-aware higher-timeframe inputs

**Defect.** Candle opening timestamps made a higher-timeframe bar current in
Backtrader before its final OHLCV would have been available. Being at index
zero is not proof of causality.

**Fix.** A higher-timeframe row is injected only when it has closed by the
primary bar's decision time:

```text
primary_decision_ms = primary_open_ms + primary_timeframe_ms
htf_available_ms    = htf_open_ms + htf_timeframe_ms
include the HTF row only if htf_available_ms <= primary_decision_ms
```

Input opening timestamps are retained; execution matching still runs on the
primary feed, so broker scheduling, warm-up and entry timing are unchanged.
Open, high, low, close and volume all use the same eligible rows, and history
length is counted after filtering. A second feed without declared timeframe
durations fails loudly rather than guessing one.

No completed higher-timeframe history is the same "unavailable" state as no
higher-timeframe feed: the arrays stay `None` rather than becoming an empty
array, so a strategy guarding with `is None` behaves the same in both cases.
No completed bar is ever fabricated. `on_bar()` is still called once per
primary bar.

Because only trailing bars can still be forming, the scan stops at the first
closed bar instead of re-reading the whole higher-timeframe history on every
primary bar, which had made a two-feed run cost O(primary bars x HTF bars).

**Compatibility.** The rule applies to both execution models, because the old
behaviour was a defect rather than a documented semantic. Resolved metadata
carries `htf_availability_legacy_defect_fixed: true`, so a recomputed
`legacy_v1` result stays identifiable. A prior result requires its source
fingerprint and original inputs; the version name alone does not reproduce it.

**Tests.** `tests/test_bt_adapter_htf.py`:
`test_htf_bar_is_visible_only_after_it_closes` (exact close boundary),
`test_mutating_an_unfinished_htf_bar_does_not_change_earlier_inputs`
(the original reproducer, inverted into an assertion),
`test_htf_arrays_stay_none_until_the_first_bar_has_closed`,
`test_a_second_feed_without_declared_timeframes_fails_loudly` and
`test_htf_injection_never_reads_the_whole_higher_timeframe_history`.

## 2. One v1 millisecond conversion

**Defect.** `_timestamp_ms()` truncated while `_record_fill()` rounded
Backtrader's floating datetime, so an input at `1704070800002` could surface as
an event at `1704070800001` while the ledger kept the correct time.

**Fix.** `time_conversion.utc_ms` / `num2utc_ms` is the single conversion, used
by the adapter, the execution broker and the trade-list analyzer under
`ohlcv_fixed_v1`, with documented nearest-millisecond rounding. `legacy_v1`
keeps the truncating conversion in an explicitly commented branch so old
results stay reproducible.

**Test.** `tests/test_execution_realism.py::test_v1_event_ledger_and_strategy_timestamps_agree_at_millisecond_offsets`.

## 3. Audit and acceptance

Versioned configuration stays strict: exchange, market and explicit model
fields, with a documented schema change required for new fields. Availability
policy, timestamp convention and fixed-cost limitations are recorded in
resolved metadata as well as in the documentation, and observed funding is
never inferred from zero. Actual-fill accounting and both reconciliation
invariants are retained.

The public reference fixture — a long 2-unit position through a 100 to 85 gap —
reproduces actual fills of 100.20 and 84.83, spread and slippage of 0.37 each,
a fee of 0.148024 and final capital of 9969.111976. Zero-cost runs, both
directions, limits, stop gaps, OCO, cash rejection, open-at-end and repeated
runs are covered, as is corrected higher-timeframe behaviour, without cheat
modes.

There is no separate evaluation or warm-up window in the current spec: warm-up
candles are not prepended and then silently traded on. An explicit window, or
a required historical-series contract, needs engine and plugin agreement
first. Until then a caller records the history it actually supplied.

## 4. Deferred effects remain separate versions

The [research follow-up](execution-research.md#staged-follow-up-and-acceptance-criteria)
holds the complete pickup conditions.

| Work | Required before implementation |
|---|---|
| Funding | Historical signed rates, mark prices, settlement times and complete coverage; broker cashflow ledger; equal-time settlement ordering and debit/credit/boundary tests |
| Maker/taker fees | Historical account fees and executed liquidity evidence; never `limit == maker` or today's rates for an old run |
| Partial fills | Cumulative entry/exit accounting, protection resizing, entry-remainder policy, partial OCO behaviour, shared volume budget, stable ids and delta-fee tests |
| Impact/latency | Lagged inputs, calibrated coefficients, warm-up and explicit entry/cancel/replacement/protection delays |
| Filters/margin/liquidation | Historical contract/mark/filter/tier/collateral evidence and a tested account state machine |

Evidence for these is acquired and archived outside this plugin. MIT paper
implementations are independent and must pass the common fixtures before
claiming parity. Full fills, delayed brackets and uniform fees remain
disclosed approximations. Closing the timing defect implements none of the
deferred effects.

Update this file when implementation decisions, compatibility policy, upstream
dependencies or test evidence change.
