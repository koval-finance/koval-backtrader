# Execution-cost milestone decision record

Research accessed **2026-09-05**. This is a deterministic historical simulation,
not an order-book reconstruction or a performance bound. Implementation and
verification evidence is recorded in [execution-validation.md](execution-validation.md).

## Baseline and source audit

The starting working tree was clean at `2978a5f`; the full gate passed **106
tests**. Installed versions: Backtrader **1.9.78.123**, koval-engine **0.9.0**.
The installed `backtrader/brokers/bbroker.py` SHA-256 was
`0fd397a72814f0de31fcd318b4a1da2030412ae9bea426c0d5dcdd001a52965e`.
The installed source, rather than a moving upstream branch, governed the audit.

- Backtrader's broker selects open/trigger/limit reference prices, uses the
  remaining quantity without a filler, and charges percentage commission on
  executed notional. Market execution waits for a later bar. Its `_execute`
  also performs a submission cash simulation (`ago=None`), which must never
  enter an execution-cost ledger. See the [broker source](https://raw.githubusercontent.com/mementum/backtrader/master/backtrader/brokers/bbroker.py)
  and [order execution documentation](https://www.backtrader.com/docu/order-creation-execution/order-creation-execution/).
- [Slippage controls](https://www.backtrader.com/docu/slippage/slippage/) include
  open-price adjustment, range caps, limit caps and fills outside the range.
  We retain upstream matching, but use one local adjustment at `_execute`:
  the built-in knobs do not provide separate spread/slippage attribution and
  range-capping can erase costs on flat candles. No new dependency is needed.
- [Commission schemes](https://www.backtrader.com/docu/commission-schemes/commission-schemes/)
  distinguish stock-like percentage cash accounting from futures margin
  schemes. This plugin currently uses stock-like linear accounting even when
  `exchange_type="future"`; a fee label does not turn it into a futures
  margin or liquidation simulator.
- [Fillers](https://www.backtrader.com/docu/filler/) can cap quantity using bar
  volume. Installed `Order.execute` marks an incomplete quantity `Partial`;
  [order notifications](https://www.backtrader.com/docu/order/) carry cumulative
  size/price and execution bits. The adapter handles only `Completed` entries,
  and the patched OCO callback cancels siblings after any nonzero execution.
  A filler alone is therefore unsafe for bracket accounting.
- Binance [Spot commissions](https://developers.binance.com/docs/binance-spot-api-docs/faqs/commission_faq)
  depend on maker/taker and account/symbol conditions, discounts and other fee
  components. Its example rates are explicitly fictional. The USD-M
  [user commission endpoint](https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/User-Commission-Rate)
  provides account rates; the example 2/4 bps is not evidence of a historical
  universal rate. We preserve legacy constants only for compatibility.
- Binance [funding rules](https://www.binance.com/en/support/faq/detail/360033525031)
  use position notional at mark price. Positive rates debit longs and credit
  shorts; negative rates reverse that. Eight-hour intervals are a default,
  not an invariant, and settlement timing has a documented tolerance. The
  [historical endpoint](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History)
  returns timestamps, signed rates and associated mark prices in ascending
  order. [Funding info](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-Info)
  exposes interval adjustments. Neither a current rate nor a hardcoded
  eight-hour clock is sufficient for historical accrual.
- Binance [Spot filters](https://developers.binance.com/docs/binance-spot-api-docs/filters)
  constrain price ticks, lot steps, market quantities and notionals. USD-M
  [exchange information](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information)
  carries analogous rules and trigger protection; price/quantity precision
  fields must not be substituted for tick/step sizes. Historical snapshots
  are needed to replay past constraints.
- Binance's [clearing procedures](https://bin.bnbstatic.com/static/cms/cg08ou2ak0tn7mcplvfg/file/53197b612332da02c20b5b7d19b81ff53ee5f4938c6330c72a30a1ca4f91049f.pdf)
  describe mark-price liquidation, maintenance-margin rates/amounts tied to
  position brackets, and contract-specific collateral rules. This supports
  deferring liquidation until the correct historical account and mark inputs
  exist; it does not justify using trade-price candles as mark prices.
- QuantConnect's primary [model documentation](https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/slippage/supported-models),
  [volume-share implementation](https://raw.githubusercontent.com/QuantConnect/Lean/master/Common/Orders/Slippage/VolumeShareSlippageModel.cs)
  and [market-impact implementation](https://raw.githubusercontent.com/QuantConnect/Lean/master/Common/Orders/Slippage/MarketImpactSlippageModel.cs)
  illustrate constant percentage, participation-squared and volatility/volume
  models. These are assumptions requiring calibration, not observed depth.
  Using a filling candle's completed volume/volatility to price its open
  would use information unavailable at that open.

The adjacent engine's current `PaperBroker` already handles carried stop gaps
and checks same-bar protection for limit/stop entries. `LiveEngine` still fills
market entries at signal close, and its trade mapper uses setup entry prices.
The current engine Binance data adapter uses `/fapi/v1/klines` and its factory
selects by exchange name only. A caller that adds a spot request field without
also changing data selection and cache identity would still replay futures
candles under a spot label.
The existing protocol has dictionary seams in both directions; this milestone
requires no shared dataclass change or protocol version bump.

## Decision matrix

All proposed simulations below are deterministic with frozen inputs. Repository
ownership refers to implementation; acquisition and persistence belong to the
host application, never to a network call inside a backtest.

| Effect | Required data | Proposed model | Owner | Determinism and limitation | This milestone |
|---|---|---|---|---|---|
| Spread | OHLCV plus explicit full-spread bps | Adverse half spread per fill, limited by order price | Plugin; engine for paper parity | Fixed arithmetic; no observed bid/ask | Implement |
| Slippage | OHLCV plus explicit bps | Adverse fixed fraction of matched reference | Plugin; engine for paper parity | No randomness, volatility or depth claim | Implement |
| Volatility slippage | Prior completed bars, window, coefficient, warmup policy | Lagged ATR/price or return volatility times coefficient | Plugin / engine | Deterministic; needs calibration and frozen warmup | Defer F4 |
| Market impact | Historical volume units, size, calibrated coefficient | Lagged participation-squared sensitivity model | Plugin / engine | Proxy; bar volume is not depth or queue priority | Defer F4 |
| Maker/taker | Historical fee schedule and execution liquidity evidence/quotes | Per-fill liquidity classification and account fee rules | Plugin / engine; app sources data | Order type alone cannot establish maker status | Defer F2; explicit uniform fee now |
| Funding | Symbol, time-aligned signed rates and settlement mark prices, coverage | Broker cashflow only for exposure at each settlement | Plugin / engine; app sources data | No current-rate substitution; bar boundary needs explicit rule | Unavailable; defer F1 |
| Participation / partial fills | Base-unit volume, participation cap, queue policy | Shared bar budget and cumulative protected exposure | Plugin / engine | Deterministic proxy; cannot infer intrabar liquidity | Defer F3 |
| Stop gaps / ambiguity | Current OHLC | Gap uses open before cost; existing stop-first bracket queue | Plugin | Preserved tie-break; no intrabar price path | Preserve and test |
| Extra latency | Bar timestamps, explicit integer delay | Delay eligibility by primary-feed bars | Plugin / engine | Coarse delay; cancellation/protection timing must be defined | Defer F5; zero extra delay |
| Margin / liquidation | Historical mark prices, collateral, tiers, rules, contract type | Separate account/margin state machine | Plugin / engine; app sources data | Trade-price OHLC and leverage cap are insufficient | Defer F6 |
| Attribution | Actual broker fills, reference prices, commission | Embedded cost ledger and account reconciliation | Plugin; app persists/displays | Costs explain these fills, not an alternate strategy run | Implement |
| Assumption disclosure | Resolved config, package versions, data provenance | Versioned metadata in result metrics | Plugin; app persists/gates | Reproduction also needs unchanged graph, feeds and software | Implement |

## Chosen contract

`execution_config.execution_model` is an opt-in dictionary. `legacy_v1`
freezes a resolved uniform `commission_bps`; `ohlcv_fixed_v1` additionally
requires explicit `spread_bps` and `slippage_bps`. Top-level `exchange` and
`exchange_type` identify the market. Versioned dictionaries reject unknown,
missing, non-finite, negative and contradictory settings. Valid legacy
unversioned requests keep their existing fee-resolution precedence; malformed
fields, numeric strings, unknown values and fees at least 100% now fail instead
of silently falling back. Legacy configs cannot silently opt into costs.
Metadata returns a strict resolved configuration suitable for replay, not just
the name of a defaults table. See [execution-model.md](execution-model.md).

The new price is the Backtrader-matched reference plus adverse
`reference * (spread_bps / 2 + slippage_bps) / 10000`. Both components use the
same reference, without compounding. A buy limit never pays above its limit;
a sell limit never receives below it. A capped adjustment is allocated between
the components in their configured proportions. Trigger-touch limit fills
have zero adjustment. Market/stop synthetic prices are not capped by the
completed candle's high/low. Gaps change the reference first and are not
mislabelled as configured spread/slippage.

Funding, true maker/taker classification, latency, partial fills, impact,
exchange filters and liquidation are explicitly unavailable. Uniform fees
are an assumption, not a claim that all limit fills are takers. A missing
funding series is not a measured zero funding rate. No model in this milestone
qualifies a strategy for sandbox promotion by itself.

## Staged follow-up and acceptance criteria

The [execution readiness record](execution-plan.md) holds the closed timing
fixes and the compatibility decisions around them. The MIT engine's
independent work is specified in its
[execution contract plan](https://github.com/koval-finance/koval-engine/blob/main/agents_docs/execution_contract_plan.md).
The numbered items below retain the input requirements and model decisions;
none is completed merely because it is written down.

1. **Engine parity and contracts.** In `koval-engine`, extend
   `execution_settings.py`, `paper_broker.py`, `live_engine.py` and their tests
   with an independently written MIT implementation of the accepted price
   formula, limit caps, uniform fee accounting and metadata vocabulary. Use
   this plugin's documented numeric fixtures, never GPL imports or copied GPL
   implementation. Resolve signal-close versus next-open timing and immediate
   versus delayed brackets explicitly under a new paper model version. Record
   actual entry fills in live trade results. Debit fees in the paper broker
   and account state exactly once. The current dictionary seams need no
   protocol bump; a future required typed result field or nonoptional data
   feed contract requires `ENGINE_PROTOCOL_VERSION` negotiation and staged
   engine/plugin compatibility tests.
   Before application spot support, extend the engine's exchange-adapter
   factory and OHLCV cache contract to select and key by market type. Preserve
   the existing futures default and identify existing futures cache entries;
   spot and futures data for the same symbol must never share a cache key.
2. **F1: historical funding.** App acquisition must supply immutable symbol,
   settlement timestamp, signed decimal rate, mark price, source and full
   coverage boundaries (including an explicitly empty/zero series). Plugin
   and engine must agree on settlement-before-orders at equal timestamps,
   carried exposure, gaps and events between bars. Reject duplicates,
   unsorted/out-of-coverage observations and incomplete coverage; do not
   forward-fill unknown rates. Add a broker cashflow ledger before adapting
   trade summaries. Tests: positive/negative rates for long/short, entry and
   exit boundary timestamps, no flat-position accrual, no duplicate processing,
   no future accrual, missing versus zero, open-at-end and equity reconciliation.
3. **F2: executed fee roles.** App supplies historical account/symbol rates,
   fee currency, discounts/rebates and quote/liquidity evidence. Engine broker
   fill metadata must carry liquidity role and actual fee amount/currency.
   Plugin may first offer a separately versioned, explicitly labelled quote
   crossing/resting proxy, never `limit == maker`. Tests must distinguish
   marketable limits, resting limits, gaps, stops converted to market, and
   currency conversion without double charging. Rates cannot come from today's
   signed endpoint for an old run.
4. **F3: partial fills.** Plugin first models cumulative entry quantity and
   average price on `Partial`, assigns one stable trade ID at first exposure,
   protects every acquired unit, and resizes both exits as entry grows.
   Define cancel/remainder policy when an exit starts before entry completion.
   A partial exit must reduce the sibling, not cancel all protection; no
   oversell or ghost reversal is allowed. Handle terminal partial entries,
   cumulative notifications using execution deltas, commissions per execution,
   and deterministic event order. Use one base-volume budget per primary bar,
   shared across all orders. Decide lagged-volume eligibility versus a
   completed-bar allocation model before coding. Acceptance: multi-bar entry,
   simultaneous stop/target eligibility, trailing replacement, cancellations,
   partial exit, missing/zero volume, stable IDs and final/open reconciliation.
   Mirror accepted behavior in the engine before enabling paper parity.
5. **F4/F5: adaptive costs and latency.** Plugin/engine require fixed window,
   warmup, lag, calibration source and coefficients before volatility/impact
   models; never inspect the fill bar's future volume at its open. Extra
   latency needs entry, cancellation, bracket activation and replacement rules
   counted on the primary feed, with multi-timeframe tests. Zero-volume,
   warmup and insufficient-history cases must fail or follow a stored policy.
6. **F6: margin, filters and liquidation.** App must archive historical symbol
   filters, mark-price history, contract multiplier/type, margin tiers,
   collateral rules and liquidation fees. Plugin/engine need an independent
   account state machine for maintenance margin and liquidation ordering,
   including funding's effect on collateral. Tests must cover tier transitions,
   mark/trade-price divergence, gaps and insufficient collateral. Existing
   Backtrader cash rejection and sizing leverage are not this model.
7. **Consumer integration after a released version.** A consuming
   application must carry an explicit market type and versioned cost inputs
   through its own request, queue and storage layers; installing this plugin
   activates nothing on its own. Feed acquisition must use that same market
   type, so spot and futures candles cannot be interchanged under one cache
   identity. Preserve the original request together with the returned
   `metrics.execution_model`, `metrics.execution_costs`, fill records and
   per-trade cost fields; a trade whitelist that copies only the legacy fields
   drops them. Historical unversioned runs must stay labelled legacy and must
   never be rewritten as v1. Funding, quote and filter snapshots are acquired
   outside this plugin. A user interface should distinguish observed candles,
   assumptions, derived costs and unavailable effects, and should show
   open-position reconciliation. Promotion to paper or sandbox trading must
   fail when required fee, funding, latency, ambiguity, data-quality or warmup
   assumptions are missing or unsupported; the presence of a model is not a
   pass. Add intended/simulated/observed price-drift logging with tested
   thresholds before any sandbox promotion. Update engine and plugin pins only
   after compatible packages are released; no GPL imports into MIT code.
