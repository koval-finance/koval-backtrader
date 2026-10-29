# Historical bounded Spot Grid and DCA

The additive `koval_spot_historical_v1` lane executes the engine's native
`exec.spot_grid` and `exec.spot_dca` graphs. Grid uses cash-funded fixed bids and
inventory-backed adjacent asks. Price-drop DCA uses one basket, bounded additions
and one fill-based shared target. Scheduled DCA is unsupported.

## Compatibility and release status

Adapter 0.12.4 requires `koval-engine>=0.12.7,<0.13.0`, using the published
`koval.engine.spot_multi_order` contract. Normal and release CI verify the exact
engine 0.12.7 and 0.12.8 wheels with the built adapter. Missing APIs fail closed.
Local candidate acceptance is recorded in
[execution-validation.md](execution-validation.md#published-engine-compatibility);
it does not establish adapter publication or application adoption. Adapter
0.12.3 and engine 0.12.6 remain the earlier legacy-only package pair.

Protocol 2 and execution contract 2 are required. Static graph negotiation requires
`spot_multi_order_v1`, `spot_reservations_v1`, `spot_causal_ohlcv_v1`,
`spot_state_restore_v1`, and `spot_grid_v1` or `spot_dca_v1`. Legacy execution
terminals cannot be mixed with native recipe terminals. One Binance Spot symbol
and one timeframe are supported; legacy `runtime_contract` is refused. Use the
engine binding's evaluation interval instead. There is no venue transport.

## Calling the normal loader

The following offline example consumes a packaged engine fixture as explicit
synthetic evidence. It is not a proposed strategy or historical exchange evidence.

```python
from copy import deepcopy
import numpy as np
from koval.engine.backtest_engine import EngineRunSpec, load_backtest_engine
from koval.examples import spot_multi_order_fixtures

case = next(c for c in spot_multi_order_fixtures() if c["id"] == "grid_control")
binding = deepcopy(case["binding"])
binding["mode"] = "historical"
candles = np.array([s["candle"] for s in case["observations"]], dtype=float)
spec = EngineRunSpec(
    graph=case["graph"],
    feeds={binding["timeframe"]: candles},
    initial_capital=float(binding["initial_quote_cash"]),
    execution_contract_version=2,
    execution_config={
        "spot_multi_order": {
            "version": "koval_spot_historical_v1",
            "binding": binding,
            "instrument": case["instrument"],
            "policy": case["policy"],
        }
    },
)
result = load_backtest_engine().run(spec)
report = result.metrics["spot_execution_v1"]
```

`binding`, `instrument`, and `policy` use the public engine models
`SpotBindingV1`, `SpotInstrumentEvidenceV1`, and `SpotFillPolicyV1`. Graph, policy
and instrument content hashes must match their bindings. Costs, fee assets and
evidence provenance are mandatory. No hidden zero-cost or current-filter default
is added. Initial base is zero; initial quote cash must match the run spec.
The engine owns recipe schemas, budget checks, normalization and order admission.

The execution config contains only `spot_multi_order`. Its allowed keys are
`version`, `binding`, `instrument`, `policy`, `driver`, `observations`, `finish`,
`checkpoint`, and `journal_suffix`. Unknown fields are refused. The normal
`driver="candles"` uses the spec feed, with `finish=True` by default. Finish
applies the binding's explicit `stop_and_hold` or `stop_and_flatten` policy.
`finish=False` produces a continuation checkpoint with pending orders intact.

Optional candle observations schedule `{"kind": "command", "payload": ...}`
between `{"kind": "candle", "candle": [...]}` observations. Candle observations
must exactly match the feed. A command has `command_id`, `action`, and `reason`;
actions are `stop_and_hold` or `stop_and_flatten`. Optional observation `mark`
must equal the confirmed close. Chronology and instrument coverage are checked
before simulation. NumPy integer/float feeds are accepted; arbitrary precision
that was already lost in a floating-point feed cannot be recovered.

`driver="observations"` is an explicit synthetic-evidence conformance/replay lane,
not candle matching or venue reconciliation. It requires `synthetic_fixture`
instrument provenance. It accepts public observation kinds with `payload`,
`observation_id`, `timestamp_ms`, and an optional report `mark`, plus explicit
`submit_request`, `cancel_request`, `replace_request`, `restore`, and
`simulator_reconciliation` steps. It never auto-acknowledges or auto-cancels
unknown outcomes. Refused evidence raises; runtime does not consume fixture
expectations or swallow expected errors.

## Execution and terminal semantics

Cerebro advances the historical candle clock. A dedicated Decimal matcher emits
actual execution deltas into public `advance_spot`; the engine reducer implements
recipe decisions, accounting, reservations and terminal transitions. The ordinary
BackBroker account is not used as a second Spot ledger. Legacy bracket strategies
continue through their original adapter and brokers.

The matcher independently implements `koval_spot_ohlcv_v1`: open, low, high, close;
opening gaps use the favorable open for limits. A stop at the open precedes buys;
an existing executable opening target precedes a later stop. Otherwise the low
and stop precede the high and target. Equal-price priority is terminal/stop, SELL,
BUY, then acceptance sequence and client order ID. Multiple crossed limit levels
are recorded as ambiguous. This is an assumed path, not observed intrabar history.

Spread/2 plus slippage adjusts prices adversely, then rounds adversely to ticks
and respects limits. Opening/market matches use taker fees; resting crossings
use maker fees. This is a declared proxy, not queue evidence. All orders share
`volume * maximum_volume_participation`, including partial closes and protection.
New ordinary orders and replacements activate at a later candle; a newly bought
lot cannot sell or rearm using earlier extrema. Protective containment may use
one later unconsumed crossing after its cancellation barrier. Final flatten uses
only unused final-bar volume at the final close, without another candle.

The first positive DCA base fill is protected, even before the base completes.
Terminal commands freeze entries/additions/rearms and cancel/reconcile every
owned pending order before a replacement close. Unknown outcomes keep reserves
and halt writes. Raced fills update the same ledger before residual close size
is chosen. Partial closes preserve command/generation identities. Repeated
commands, finish and restore cannot duplicate a close. Hold remains stopped and
unprotected; flatten preserves unsellable dust as inventory. The public engine
reducer owns these rules, including source-sequence, conflicting-identity and
stale-generation refusal.

Base fees reduce actual inventory. Quote fees debit actual cash. Third-asset fees
retain valuation evidence; missing valuation makes net PnL/equity and root
performance metrics unavailable, and blocks new buys until reconciled. Current
instrument snapshots in historical mode are labeled approximations. No queue,
latency or historical filter evidence is fabricated.

## Results, journal and recovery

`metrics.spot_execution_v1` transports `koval_spot_execution_report_v1` additively:
orders and complete actual events in the trace, requested/admitted quantities,
Grid lots, DCA basket/cycle state, cash/base free and reserved, fee assets and
valuation status, realized PnL, remaining cost basis, marked holdings and total
net equity. It also provides ordered `intents`, `fills`, `order_states`, and
`phase` projections for conformance consumers. Use the trace's execution/trade
IDs for durable fill identity. Do not count each DCA addition as a trade.

Root `initial_capital`, `final_capital`, `total_pnl`, `total_return_pct` and
`max_drawdown` describe the marked account, including terminal close costs.
Unavailable net performance is `None`, never zero. Legacy `trades` remains empty
and `total_trades=0` for this lane; consumers must read lots/baskets rather than
interpret this as inactivity. Winning-pair profit never substitutes for total
equity, including losing residual inventory. The last root equity point includes
terminal costs at its existing timestamp; the engine report retains its original
pre-terminal candle equity curve for exact paper comparison.

`metrics.execution_model` records the negotiated features, policy and actual
engine/adapter source digests, separately from resolved distribution metadata
labels. With a source override, metadata can also resolve from the source tree;
this must not be interpreted as proof of an installed wheel.
`metrics.execution_audit` is `koval_spot_historical_audit_v1`: compact journal
headers and a final `koval_spot_historical_checkpoint_v1` checkpoint. The latter
wraps the checksummed engine state with actual software identity. Resume by
passing it as `checkpoint`; a changed source digest or binding fails closed.

`on_event` receives `SPOT_EXECUTION_RECORD` with integer candle milliseconds
(or `None` when explicit replay has no candle), bar index, and a full checksummed
`koval_spot_historical_journal_v1` record in `payload`. The callback must durably
append and acknowledge synchronously when recovery is required. Each observation
is journaled before reduction. Callback/transition failure latches the session;
recover a fresh instance with the checkpoint and complete committed
`journal_suffix`. Incomplete candles/pending inputs, changed software and broken
journal chains are refused. The compact result headers alone cannot recover an
interrupted candle; store streamed full records. Calls on a session are serial;
independent runs have no shared controller/account state.

## Acceptance and limits

All 16 checksummed `koval.examples.spot_multi_order_fixtures()` are exercised
through `load_backtest_engine()` against their independent expectations at every
observation. All 11 candle cases additionally compare full paper reports, including
end-of-data handling. The only full-report difference is the explicit historical
versus paper binding mode. Prices, quantities, identities and reserves are exact;
cash/cost/equity differences must be strictly below 1e-18 through the engine's
public comparator. No tolerance is relaxed by this adapter.

The controls are cash 200 -> 210 for the Grid sequence (190 reserved, 20 free,
zero inventory), and cash 1000 -> 1020 for the DCA basket (two buys, one shared
sell). Additional hand calculations exercise nonzero fees, different maker/taker
rates, adverse costs, tick rounding, limit clipping and fractional liquidity.
Historical acceptance proves this documented model, not profitability or venue
acceptance. Released-wheel pair verification and consuming application adoption
remain separate gates.

Current scaling is a material limitation: full public reducer state and journal
checkpoints grow with processed observations and fills. A quiet-session review
measured roughly 0.57/1.75/6.67 seconds cumulatively for 40/80/160 candles on one
development machine, even with a discarded journal sink. This is not a throughput
guarantee. Compact returned journal headers avoid retaining all full snapshots,
but state copies/checksums still make long histories expensive. Large datasets
need an upstream bounded-state/replay contract and measured acceptance; adapters
must not trim idempotence history or mutate engine internals to hide this cost.
