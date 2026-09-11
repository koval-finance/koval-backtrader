# Execution model

Version 0.11 runs against published `koval-engine>=0.11.0,<0.12.0`.
It simulates one instrument from closed OHLCV bars. Matching historical and
paper results demonstrates implementation conformance, not equivalent exchange
fills. See the [verification and review record](execution-validation.md).

## Versions and resolved configuration

| Plugin profile | Paired paper profile | Protection | Optional execution evidence |
|---|---|---|---|
| `legacy_v1` | No execution-parity claim | Bar after entry fill | None |
| `ohlcv_fixed_v1` | `paper_ohlcv_fixed_v1` | Bar after entry fill | None |
| `ohlcv_realistic_v2` | `paper_ohlcv_realistic_v2` | Entry-fill bar, subject to activation latency | Funding, fees, instruments, marks, execution proxy |

A bare `EngineRunSpec` remains fee-free legacy. CLI defaults can supply legacy
fees. A versioned request states costs explicitly:

```python
execution_config = {
    "exchange": "binance",
    "exchange_type": "future",
    "market": {
        "exchange": "binance",
        "market": "future",
        "canonical_symbol": "BTCUSDT",
        "contract_type": "perpetual",
    },
    "execution_model": {
        "version": "ohlcv_realistic_v2",
        "commission_bps": 4.0,
        "spread_bps": 20.0,
        "slippage_bps": 10.0,
        "leverage": 1.0,
    },
}
```

These rates are assumptions, not an exchange fee quote. `leverage` defaults
to 1 and accepts [1, 125]. Costs must be finite, non-negative and below 10,000
bps; half spread plus slippage must also be below 10,000. Unknown fields,
versions and conflicting legacy fee overrides fail. `legacy_v1` accepts only
`version` and `commission_bps` inside its model.

Store `metrics.execution_model.resolved_config`; it is JSON-replayable,
including normalized evidence. Typed engine evidence dataclasses and equivalent
JSON mappings are accepted in the five optional top-level fields below.
Decimal values serialize as strings. Raw venue responses are not embedded:
the caller owns their archive and provenance.

`EngineRunSpec.execution_contract_version=2` can require v2. Required capability
names are `run_identity`, `same_bar_protection`, `funding`, `fee_schedule`,
`instrument_specs`, `mark_prices`, and `execution_proxy`. Evidence capabilities
are offered only when their inputs are supplied. Unsupported requests raise
`ProtocolVersionError` before simulation. A default version-1 request still
accepts an explicitly selected v2 profile; record the profile as well as the
negotiated request.

## Declaring the market

The engine's canonical identity is `{exchange, market, canonical_symbol,
contract_type}`. Supported exchanges are `binance` and `whitebit`, markets
`spot` and `future`, contracts `spot` and `perpetual`. Delivery and inverse
contracts are unsupported. Old six-field market blocks with `venue`, `symbol`,
`base_currency`, and `quote_currency` remain accepted but resolve to the engine
identity. `futures` resolves to `future`; WhiteBIT `BTC_PERP` becomes `BTCPERP`,
not `BTCUSDT`.

A declared spot market refuses leverage above one and softly rejects shorts
with `spot_short_unsupported`, allowing later long signals. Labels and market
identity must agree. Without a market block, historical label-only behavior
remains unconstrained and the run is not comparable by canonical market.
Every optional execution-evidence block requires an explicit market identity.

## Binding a result to its inputs

Every run emits `koval_run_identity_v1`: primary candle stream, empty preseed
warmup, graph, paired execution profile, evidence hashes, market, run
parameters, engine and plugin versions, and plugin source fingerprint.
The optional legacy `evidence` provenance block requires `dataset_id`, `source`,
`retrieved_at_ms` and a lowercase `content_sha256`; a caller's assertion does
not certify historical completeness.

Shared stream hashes use the engine encoding. Old plugin feed hashes remain
as extensions and include all supplied feeds. An irregular v1 stream keeps
its old encoding and is `not_comparable`. `identified_simulation` identifies
inputs; it never means venue fidelity or full reproducibility. The deprecated
plugin `reproducibility.level` is always `partial`.

## The bar clock

A primary candle is timestamped at its opening time. The broker handles that
bar, notifications update the account, then the strategy sees the closed bar
and decides. New strategy entries become eligible on a later primary bar.
Equity and decisions are recorded once per primary bar. A higher-timeframe
feed cannot extend execution past the primary dataset.

V2 equal-timestamp order follows the engine contract: funding settlement,
mark-price liquidation, entry fill, bracket activation, stop/target fill, OCO
sibling cancellation, dynamic replacement. Existing protection is evaluated
before an outstanding entry remainder. A protective fill cancels that remainder.

## Entry timing

V1 signals on N, fills on N+1, protects from N+2. V2 can open and close on
N+1. An entry already beyond its requested bracket is contained at the matched
market reference. For an ordinary entry-bar stop touch, a pre-entry opening
gap is not reused as the stop fill. Existing stops retain gap-through behavior.

### Where this differs from the paper broker

All published 0.11 fixtures run, selected by contract and profile version.
Baseline v1/v2 differential scenarios have no waivers. The old favorable-limit,
target-update and spot-refusal exceptions are removed. Actual `LiveEngine`
replay also covers a refused spot short followed by a long and a moved target.

Advanced combinations still have engine-side gaps: partial-exit residual
valuation, quantity re-quantization after risk sizing, and volume reserved
before risk clipping. Graph account binding also needs a public runtime hook.
The exact examples, reason codes and affected comparisons are in
[execution-validation.md](execution-validation.md#remaining-engine-011-integration-gaps).
Do not claim general graph or advanced-evidence parity until these are resolved.

## Fill prices

Market entries match the next eligible open. Resting limits match a favorable
open, otherwise their touched limit. Stops match a gap-through open, otherwise
the touched trigger. A stop is not a guarantee of maximum loss.

### Fixed spread and slippage

For reference `p` and buy/sell sign `s=+1/-1`, costed fills use
`p * (1 + s * (spread_bps / 2 + slippage_bps) / 10000)`.
Entry limits remain bounded by their limit. Take-profits are market-on-touch
and pay the full adjustment. Synthetic cost prices can exceed candle ranges.
V2 may then round entry prices to the supplied instrument tick. Signed price
adjustment records a favorable rounding as a benefit rather than a debit.

## Brackets, and the OCO patch

The global Backtrader patch prevents sibling double fills and cancels submitted
as well as pending siblings. V2 retains and resizes both legs after a partial
exit; a complete exit cancels its sibling. It does not delegate historical
matching to `PaperBroker`. Treat a Backtrader upgrade as a source-review event.

If both protection levels are touched, v2 selects the stop. Its ambiguity
record includes local stop/target reference PnL for the full current position,
before fees, funding and liquidity limits. These alternatives describe one bar;
they are not optimistic/pessimistic bounds on the strategy's total return.

## Moving a stop or a target

Both hooks run after broker processing, including on the entry-fill bar.
The final pair must be finite and positive, ordered stop < target for a long
and target < stop for a short. A stop cannot widen risk. Profit locks are
allowed. Invalid replacements fail before cancellation; valid replacements
rebuild the OCO pair and update a carried entry remainder. With instrument
evidence, replacement prices are normalized before final validation.

## Position sizing

Explicit strategy size is the requested quantity. Missing size uses the engine
risk sizer and margin cap. V2 revalidates the stop-risk budget at the actual
entry price, including entry/exit fee assumptions and adverse stop costs.
Instrument quantity steps apply after both risk and volume caps. A resulting
entry below minimum quantity/notional is rejected as `instrument_constraint`.
V2 invalid prices, sizes, entry types and brackets fail loudly.

`risk_at_entry`, `actual_risk` and the legacy account `position.risk_amount`
are diagnostics based on the configured fixed costs; they are estimates when
historical fees or calibrated impact override those costs. Actual charged fees
and fills are authoritative in the execution ledger. Profit locks report zero
capital at risk when their estimated net stop outcome is positive.

## Fees

V1 uses a uniform fee; v2 can take `fee_schedule: FeeScheduleEvidence` with
maker/taker rates, interval, currency, tier, discount treatment and provenance.
A resting entry limit is *assumed* maker, other fills taker. OHLCV cannot prove
actual liquidity provision. Historical evidence must cover each charged fill;
current snapshots and configured rates remain approximations as labelled by
the engine. Fee evidence overrides configured fees in affordability checks too.
Only quote currency or the selected symbol's quote asset is accepted. Discount
rates must already incorporate the supplied treatment. Cross-asset conversion
is unsupported and is never hidden in slippage.

## Accounting, leverage and affordability

A shared append-only broker ledger is the account's source of cash, gross trade
PnL, fees and funding. Cached totals keep snapshots O(1) in trade count.
Margin is entry notional / leverage; free margin is equity minus used margin.
Both costed profiles check both directions at submission and at actual fill.
Legacy retains Backtrader's historical cash behavior.

`funding: FundingSeries` requires complete perpetual coverage. Signed payments
use archived settlement mark prices, before orders at that timestamp, against
the position held before those orders. Missing coverage fails even when flat.
Funding remains separate from trade price PnL and commission. Empty-series
coverage is a caller assertion under the engine's normalized contract.

`instrument_specs` selects time-valid tick/step/minimum/price-band evidence.
`mark_prices: MarkPriceSeries` additionally enables the engine's single-position
cross-margin maintenance calculation and liquidation fee. Marks must cover
every primary bar. Funding precedes liquidation; liquidation precedes protection
and bypasses the OHLCV volume cap. Only linear base-quantity contracts with
contract size 1 and quote-asset collateral are supported. This is a normalized subset of venue rules,
not a claim that all historical exchange filters are represented.

`execution_proxy: ExecutionProxyConfig` enables partial fills, a shared
`volume * maximum_volume_participation` budget, carry/cancel entry remainder
policy, and timeline delays. Only actual quantities consume the budget.
Protection is resized to residual exposure. Calibrated impact uses only
calibration strictly before the decision; without calibration, fixed slippage
remains. Decision/submission/acknowledgement/fill/protection delays are checked
at primary-bar boundaries. Nonzero cancellation and replacement delays are
refused: they require a further lifecycle contract.

## Drawdown cut-off

The optional adapter `max_drawdown` gate is checked after a closed trade. It is
not a continuous intrabar risk control. Daily PnL uses the previous bar's equity
as the next UTC day's baseline, following engine account semantics.

## What is not modelled

Depth, queue priority, observed bid/ask, exchange downtime, borrow interest,
portfolio/isolated margin, inverse/delivery contracts, currency conversion,
full venue filter sets and actual intrabar price paths remain unsupported.
A volume/impact model is an OHLCV proxy even with archived calibration.
The plugin has no credentials, network order route or live trading loop.

## Determinism and look-ahead

Costed input validation checks positive finite capital, shape `(N,6)`, coherent
positive prices, finite values, non-negative volume and increasing integer
timestamps. V2 additionally requires the engine's aligned contiguous streams.
At most two timeframes for one instrument are supported. Higher-timeframe
candles are visible only when
`htf_open + htf_duration <= primary_open + primary_duration`.
History defaults to the engine's `DEFAULT_HISTORY_BARS` (1000).

V1/v2 events and fill timestamps use nearest-millisecond UTC conversion.
Legacy event epochs retain the old host-timezone-sensitive conversion for
replay. Identical inputs and software give deterministic results and run-local
IDs. Caller archives, warmup adequacy and strategy selection bias remain
outside these structural checks.

## Reading a result honestly

Closed-trade statistics exclude an unfinished position; final equity marks it
at the last close without an exit fee. Paper session finalization flattens it,
so terminal policies must match before comparing totals. Bar-high/low excursion
diagnostics may include prices before entry or after exit and are not lower
bounds on actual experienced MAE/MFE. Read raw fills, sample size and out-of-sample
results alongside aggregate statistics.

## See also

- [results.md](results.md) — result fields and reconciliation.
- [execution-validation.md](execution-validation.md) — tests, review and known gaps.
- [execution-research.md](execution-research.md) — primary sources and decisions.
