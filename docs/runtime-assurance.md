# Comparable, inspectable simulations

Version 0.12 adds optional engine runtime boundaries and a persisted execution
audit. These establish which rules, inputs and cashflows produced a simulation.
They do not certify exchange executions, data provenance or strategy profitability.

## Explicit warmup and evaluation

With an installed engine providing protocol 2, pass this dictionary as
`EngineRunSpec.runtime_contract`, alongside a costed execution profile:

```python
runtime_contract = {
    "version": "koval_runtime_boundaries_v1",
    "warmup_start_ms": 1704067200000,
    "evaluation_start_ms": 1704067320000,
    "evaluation_end_ms": 1704067560000,
    "decision_clock": "bar_close",
    "initial_balance": 10000.0,
    "daily_baseline_equity": 11000.0,
    "peak_equity": 12000.0,
    "end_of_data_policy": "mark_at_last_close",
}
```

Supply primary candles continuously from `warmup_start_ms` to the exclusive
`evaluation_end_ms`. Boundaries refer to candle opening timestamps, must align
to the primary timeframe, and initial balance must equal initial capital.
Missing preroll, a missing evaluation candle, unknown fields or incompatible
capabilities fail before a session starts. Candles at or beyond the evaluation
end are excluded. The input arrays are not mutated.

Warmup populates history only. It produces no strategy callbacks, orders,
cashflows, account observations or equity-curve points. The first evaluation
decision has the full preceding history, bounded by the existing 1000-bar
window; its bar index counts actual preroll. Account baselines are initialized
from the contract, including the prior equity peak and current day's baseline.
The two primary stream hashes distinguish actual warmup and actual evaluation.

`timestamp_ms` on the strategy remains the candle opening timestamp.
`decision_timestamp_ms` is its close time. Completed primary OHLCV values are
available to the strategy at that decision. With explicit boundaries, submission
latency starts at that close time, matching the paper runtime. Higher-timeframe arrays contain
only bars closed by that time. A secondary feed starting later does not suppress
primary callbacks. No primary decision runs when only the secondary clock advances.
Explicit boundaries also limit HTF history to complete buckets inside the
rolling primary history window and require available HTF data before entry
when a second feed is supplied. For paper comparisons, derive that second feed
from the same primary candles; independent vendor HTF values may differ.

The closed-row buffer offset is corrected for all profiles. Previous versions
could select a future preloaded HTF row after skipping a forming candle.
Reproducing such an older result requires its original artifact and inputs;
the profile name alone does not preserve that defect.

The engine automatically requires `runtime_boundaries_v1` for an explicit
contract. Engine 0.11 cannot represent it and this plugin refuses an explicit
request on that runtime. Without the contract, previous full-input execution
and mark-at-last-close behavior remain available. Explicit boundaries require
`ohlcv_fixed_v1` or `ohlcv_realistic_v2`; legacy replay retains its old format.

## Ending policy

- `mark_at_last_close` retains actual residual exposure and charges no exit fee.
  A pending entry remainder is canceled. The result distinguishes the open
  position, its paid fees, partial realized PnL and unrealized PnL.
- `flatten_at_last_close` cancels pending orders and executes the entire
  remaining position at the last close with the configured spread/slippage
  and applicable taker fee. Normal order/trade notifications and accounting
  handle the close. No extra candle, funding settlement or strategy decision
  is generated. The last equity point includes the terminal costs.

Terminal flattening is an explicit full-exit assumption, including when the
ordinary bar-volume proxy would allow only a partial exit. It uses fixed
terminal price costs without volume calibration, matching the paper runtime's
terminal policy. Select the same policy in both runtimes.

## Persisted audit

Both costed profiles export `metrics.execution_audit` with version
`koval_backtrader_audit_v1`, even without an event callback:

| Field | Meaning |
|---|---|
| `decisions` | Run-local decision ID, bar and decision times, actual history bounds/count, and the account visible before the decision |
| `intents` | Requested setup, originating decision and submission/refusal information |
| `orders` | Stable run-local order ID, origin, requested size/price and latest lifecycle state, including protective and unfilled orders |
| `fills` | Actual execution deltas, order/decision/trade IDs, model rule and cashflow sequence links |
| `ledger` | Authoritative signed quote-currency cash movements, with stable sequence numbers |
| `account_snapshots` | The authoritative account before every evaluation decision |
| `terminal_account`, `open_position` | Final account and any actual residual exposure |
| `events` | Persistable copies of the event stream, with run-local event IDs |

The same fills appear in `execution_costs.fills`. Closed trades include
`fill_ids`, `order_ids` and the entry `decision_id`. Entry fill events link
their order and cumulative fill IDs. Final order rows include protective
quantity changes after partial exits, even without a new order notification.
A cashflow's `reference_id` identifies
its fill; each fill lists its `cashflow_sequences`. Funding instead references
the settlement and records the applied record hash, source, trade ID and
settlement-before-orders rule.

IDs are local to one run. Store them together with the application's run ID.
The audit is assembled from runtime state and broker records, never by replaying
financial formulas in a result mapper. Legacy results do not acquire a complete
audit retroactively. Missing decision indicators remain `null`; recorded setup
indicators and reasons remain separate from the decision's account/history.

Do not confuse Backtrader's margin-reserved cash with ledger balance. For the
supported linear model:

```text
balance_end = initial_balance + sum(signed ledger amounts)
equity_end = balance_end + unrealized_pnl_at_last_close
```

Trade PnL, trading fees, signed funding and liquidation fees are separate ledger
kinds. Closed `net_pnl_before_funding` is price PnL less trading commission;
it excludes funding and liquidation fees. `realized_pnl` additionally accounts
for liquidation fees. Account equity includes open exposure and all booked
cashflows. Embedded spread/slippage must not be deducted from actual-price PnL
again.

## Evidence and model uncertainty

Typed evidence and normalized JSON receive the same validation. Pickled worker
transport preserves the public spec, capability requirements and evidence.
Coverage is checked before session start across the evaluation period, including
flat periods; preroll does not require execution evidence. Fee venue/market/symbol
context, when present in the installed MIT contract, must agree with the run.
Historical fee validity, unique time-valid instrument rules, funding bounds and
mark coverage cannot be silently discarded.

V2 fills contain `evidence_refs` for the actual fee schedule, selected instrument,
volume proxy and liquidation mark as applicable. These include content hashes
and named rules. `cost_quality` is independent of source provenance: spread and
fixed slippage are `configured`; a fee schedule combined with an assumed
maker/taker role is `approximated`; calibrated slippage is `approximated`.
Historical source metadata does not make those executions observed.

Bar-volume participation is an offline allocation over a completed candle.
`volume_observed_at_ms` records its closing time, later than an opening-price
fill timestamp. This is a declared approximation, not knowledge available at
the bar open. Stop-first ambiguity is a modeled choice, not an observed price
path or a global lower bound. Unavailable funding still means unavailable,
even when no funding cashflow was booked.

## Artifact acceptance

Build wheel and sdist, then retain the exact engine wheel and run:

```bash
python -m build
python scripts/check_dist.py --require-artifacts
python scripts/verify_pair.py \
  --engine /path/to/koval_engine-0.12.1-py3-none-any.whl \
  --plugin dist/koval_backtrader-0.12.1-py3-none-any.whl \
  --python /path/to/python3.11 \
  --output data_cache/pair-acceptance.json
```

The command creates a clean environment, installs both wheels together, runs
`pip check`, verifies discovery and every installed package byte, and invokes
the complete `scripts/verify.sh` gate through that interpreter. It rejects
editable/sibling package imports. The manifest records wheel hashes, versions,
Python/platform, dependencies, verifier/test source identity and gate outcome.
Retain its logs and artifacts in durable storage outside a disposable cache.

CI tests engine 0.11.1, 0.12.0 and 0.12.1 with the built plugin on Python 3.11
and 3.13. Publish engine 0.12.1 before expecting the new CI matrix to pass from
PyPI.
Local candidate validation and hosted CI are separate evidence.

The permitted dependency range is `>=0.11.0,<0.13.0`. The original 0.11.0
fallback remains for compatibility; its historical parity gaps are documented
in [execution-validation.md](execution-validation.md). The range is not a
claim that every possible pair passed this acceptance command.

Public installed-engine fixtures, real GraphStrategy/LiveEngine comparisons,
future-input mutations and an independent Decimal calculator cover simulation
conformance. Application request/UI/worker wiring, durable data archives and
sandbox operational acceptance remain responsibilities of their owning packages.
The current public spec transport tests do not claim that an application has
already exposed these new fields.
