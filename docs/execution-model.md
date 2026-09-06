# Execution model

What the simulated broker actually does. Read this before you believe a
number this package produces.

A backtest is a claim about what would have happened. The claim is only as
good as the mechanics behind it, so this page spells out every rule: when
orders are placed, at what price they fill, what is charged, and — most
importantly — what is not modelled at all.

## Versions and resolved configuration

No config, or `{}`, retains fee-free legacy execution. An unversioned venue
config retains fees-only execution with the engine's existing fee precedence.
Valid legacy prices, PnL and equity are preserved. Malformed numeric values,
unknown keys and unsupported settings now raise `ValueError` instead of being
silently ignored or defaulted. Numeric strings and booleans are not numbers
in this contract.

Opt into the fixed cost model explicitly through the existing engine seam:

```python
execution_config = {
    "exchange": "binance",
    "exchange_type": "future",
    "execution_model": {
        "version": "ohlcv_fixed_v1",
        "commission_bps": 4.0,
        "spread_bps": 20.0,
        "slippage_bps": 10.0,
    },
}
```

These numbers are illustrative assumptions, not observed Binance rates or
spreads. Pass this dictionary as `EngineRunSpec.execution_config`.
`spread_bps` is the **full spread**; each execution incurs half. Slippage and
commission are per execution. One basis point is `0.0001` as a fraction.
All four model fields and the top-level market identifiers are required.
`exchange_type` must be `spot` or `future`; the latter means linear accounting,
not inverse contracts. Both labels are recorded in the resolved metadata, but
neither changes what the simulation permits: `spot` does **not** currently
forbid short positions or force `leverage` to 1, so a spot-labelled run can
produce a position no spot venue would let you open. Treat the label as
provenance for the fee assumption, not as a venue constraint. Unknown fields, duplicate fee settings outside the
model, unsupported versions, negative/non-finite costs, or costs at least
10,000 bps are rejected. Half spread plus slippage must also be below 10,000
bps, so an adverse sell fill remains positive.

`legacy_v1` accepts only `version` and explicit `commission_bps` inside
`execution_model`; spread and slippage are fixed at zero by that version.
The top-level exchange labels also remain required. A fee-free legacy snapshot
uses `"unspecified"` for both. Every result returns
`metrics.execution_model.resolved_config`, which can be passed back unchanged.
It freezes the effective fee rather than consulting a changing defaults table.
Replay also requires the same graph, candles and software; package versions
and a plugin source fingerprint accompany the configuration. An already stored
unversioned historical run needs its original software/defaults to reconstruct
that first snapshot.

In v1, fill-ledger, event and injected strategy timestamps are explicitly UTC
and independent of the host timezone. Legacy event/strategy epoch conversion
retains its old naive-datetime behavior for compatibility; reproduce those
with the original host timezone as well as the original software. Trade
timestamps remain UTC and equity timestamps remain naive UTC in both models.

Legacy requests recognize the engine fee fields `commission` (fraction),
`taker_fee` (percent), `maker_fee_bps`, `taker_fee_bps`,
`broker_commission_bps`, `fee_source`, `paper_commission_side`, and venue/mode
fields. Engine fee precedence remains unchanged; `broker_commission_bps` is
a resolved output field, not an override. Prefer the strict versioned form.
This plugin never routes an order to a venue regardless of those labels.

## The bar clock

Everything is driven by closed bars. There is no intra-bar simulation and no
tick data; a bar is four prices and a volume, and the broker can only reason
about those.

One bar, in order:

1. The broker processes the pending order queue against this bar's prices.
   Anything that fills, fills now.
2. Fill notifications reach the strategy adapter (`notify_order`,
   `notify_trade`). New orders created here are queued, not executed.
3. `next()` runs: the adapter injects state into the strategy, calls its
   `on_bar()`, the strategy decides, and any resulting order is queued.
4. The equity analyzer records account value once.

Step 1 happens before step 3, which is the whole reason an order placed on
bar N cannot fill on bar N.

## Entry timing

| Bar | What happens |
|---|---|
| N | The strategy sees bar N closed, signals, and the adapter submits the entry order. |
| N+1 | The entry fills. The adapter places the stop-loss and take-profit bracket. |
| N+2 | The bracket is live and can fill from this bar on. |

So the earliest possible round trip is three bars, and a position can never
be opened and closed within one bar. This is the Koval execution contract v1
timing, implemented independently here and by the MIT paper broker; the shared
[parity fixtures](https://pypi.org/project/koval-engine/) (`koval.examples.parity_fixtures`)
assert that both produce the same fills and the same final equity. If you are comparing against a
vectorised backtester that assumes same-bar entry at the signal price, this
package will look worse, and it is the more honest of the two.

`SIGNAL_DETECTED` carries `entry_price` — the price the strategy asked for.
`ORDER_FILLED` carries `fill_price` — the price it got. They differ whenever
the market gapped, and the difference is real, not an artefact.

## Fill prices

Reference-price matching is Backtrader's. The rules below describe legacy
fills and the reference prices to which `ohlcv_fixed_v1` applies costs.

**Market** (`entry_type: "market"`) fills at the **open of the next bar**.
Not at the signal bar's close. A gap between the two is your gap.

**Limit** (`entry_type: "limit"`, and every take-profit) fills when the bar
touches the price:

- If the bar opens through the limit, it fills at the open — you get the
  better price.
- Otherwise, if the bar's range reaches the limit, it fills exactly at the
  limit price.
- If the bar never reaches it, the order stays live.

**Stop** (`entry_type: "stop"`, and every stop-loss) fills when the bar
trades through the trigger:

- If the bar opens beyond the trigger, it fills at the **open**. A gap
  through a stop-loss is filled at the gap, so a stop does not cap the loss
  at its own price. This is the single most common way a backtested loss
  turns out larger than "risk per trade".
- Otherwise, if the bar's range reaches the trigger, it fills exactly at the
  trigger price.

There is no partial filling. An order fills completely or not at all, and
the size available is never questioned.

### Fixed spread and slippage

For a reference price `p`, buy/sell sign `s = +1/-1`:

```
adjustment = p * (spread_bps / 2 + slippage_bps) / 10000
fill       = p + s * adjustment
```

The implementation uses the equivalent `p * (1 + s * fraction)`. Both cost
components use the reference; they do not compound. A gap changes `p` first:
a long's stop at 90 with the next open at 85 uses 85 before costs, not 90.
The ledger's reference is the matched price, not the earlier signal price.
Configured costs therefore exclude the signal-to-open move and the stop gap.

| Order behavior | Cost application, for entries and exits |
|---|---|
| Market | Full adverse adjustment at next open |
| Stop, gap through trigger | Full adverse adjustment at open |
| Stop, intrabar trigger touch | Full adverse adjustment at trigger |
| Take-profit, any touch | Full adverse adjustment; never capped at the target |
| Entry limit, favorable open | Adverse adjustment capped at the limit |
| Entry limit, exact touch | Zero adjustment; fill remains at limit |

A take-profit is modelled as a **market-on-touch** order, matching the venue
order type a Koval sandbox or live session actually places
(`TAKE_PROFIT_MARKET`). It pays the full adverse adjustment and is never
capped at the target. Treating it as a free limit touch was optimistic: it
assumed a resting order that filled at its own price at no cost.

Buy **entry** limits never fill above the limit; sell entry limits never fill
below it. When the limit caps an adjustment, allocate the **actual** cost
between spread and slippage in their configured proportions. This includes
zero cost on a limit touch; it is not a claim of observed liquidity or maker
status. Each fill record carries `koval_role` (`entry`, `stop_loss`,
`take_profit`) so the two rules are distinguishable in the ledger.

Market and stop adjusted prices can fall outside the candle's high/low. They
are synthetic cost prices, explicitly disclosed in metadata. Capping at the
range can erase assumed costs on flat bars and would use a completed bar's
extremes to price its open. The model does not infer quotes from that range.
No random number, future bar, volatility measure or volume is used to compute
the adjustment. Matching still uses the current OHLC to detect triggers.

The adjusted price goes into the broker before commission, cash, position,
equity and trade notifications are calculated. Costs are not subtracted again
by an analyzer. Submission cash checks are hypothetical and produce no cost
records; an actual fill can still be rejected if its adjusted notional and
commission exceed available cash. This is Backtrader cash accounting, not a
venue margin model.

## Brackets, and the OCO patch

When an entry fills, the adapter places two exit orders sized to the filled
position: a stop-loss (stop order) and a take-profit (limit order) joined as
an OCO pair, with the stop as the group leader. Filling or cancelling either
leg cancels the other.

If the strategy supplied no `take_profit`, one is derived from the fill
price and the stop distance using the adapter's `risk_reward_ratio`
parameter (default 2.0).

Stock Backtrader has a bug here. It evaluates OCO cancellation *after*
execution, so on a bar whose range covers both the stop and the target, both
legs can fill — closing the position twice and inventing a trade that never
happened. `oco_patch.py` fixes it by tracking which OCO groups have already
completed within the current bar and cancelling the sibling before it can
execute. The patch is applied at import of `backtest_runner`, globally, to
`backtrader.brokers.bbroker.BackBroker`.

The consequence you should know about: **on an ambiguous bar, whichever leg
the broker reaches first wins.** The queue order decides, not the price
path, because a bar carries no information about the order in which its high
and low were reached. Treat any strategy whose results depend on ambiguous
bars as unproven. Widening the stop or the target until the two cannot be
reachable in the same bar is a cheap way to test whether that is happening.

## Moving a stop or a target

Every bar with an open position — except the bar the entry filled on, and
except a bar on which the stop order is no longer live — the adapter asks the
strategy for `on_sl_update(trade_id)` and `on_tp_update(trade_id)`. Returning
`None` keeps the current level; returning a price moves it.

A move rebuilds the whole bracket: the adapter snapshots both target prices,
cancels the stop once, and re-places both legs as a fresh OCO pair. It has
to work this way because cancelling the group leader cascades to the
sibling, so a naive "cancel the stop, then re-place it" silently drops the
take-profit. `tests/test_bt_adapter.py::test_take_profit_survives_trailing_stop_update`
is the regression test, and it exists because that bug shipped once.

Nothing validates the direction of a move. A strategy that trails a long's
stop *downwards* will be obeyed.

## Position sizing

If the strategy's `TradeSetup` carries an explicit `size`, that size is used
verbatim. Graph strategies always do — the `risk.pct_risk` block computes it.

Otherwise the adapter falls back to the engine's risk sizer:

```
risk_amount = account_value * risk_per_trade / 100
size        = risk_amount / |entry_price - stop_loss|
size        = min(size, account_value * leverage / entry_price)
```

The second line is a margin cap, and it bites more often than people expect:
a tight stop produces a large risk-based size, and the cap silently reduces
it, so the trade risks less than the configured percentage. Sizes of zero or
less are dropped without an order.

Leverage changes the cap only. It does not change what a stop-out costs,
because that is a function of price distance.

## Fees

Legacy commission is charged only when `EngineRunSpec.execution_config` is non-empty.
With no execution config the run is fee-free, deliberately, so unit tests can
assert on raw price action.

Fees resolve through the engine's `resolve_execution_settings()`, which maps
a venue to a taker rate and hands the broker a single percentage commission
applied to both sides of the trade. The historical compatibility defaults:

| Venue | Maker | Taker | Broker rate used |
|---|---|---|---|
| `binance` / `spot` | 10 bps | 10 bps | 0.10% |
| `binance` / `future` | 2 bps | 4 bps | 0.04% |
| anything else | 0 | 0 | 0% |

Worked example, from the sample data: a long of 40.504 units filled at
123.4434 is a notional of exactly 5000, so entry costs 2.00. It exits at
128.381136, a notional of 5200, costing 2.08. The trade record shows
`commission: 4.08`, and `realized_pnl` is already net of it.

This matters when comparing runs: `koval backtest` always passes an
execution config (`--exchange binance`, futures), so the CLI charges fees.
`load_backtest_engine().run(spec)` with a bare `EngineRunSpec` does not. The
same graph and the same candles will produce different numbers through the
two paths, and neither is wrong.

These constants are not a current or historical account-specific fee schedule.
The new model instead requires `commission_bps` and charges
`abs(filled_size) * actual_fill_price * commission_bps / 10000` on every fill,
including limits. This **uniform fee assumption** does not classify makers
and takers. OHLCV cannot establish whether an order actually added liquidity;
maker/taker fees and discounts remain unavailable. Exact treatment requires
historical fee and execution evidence; see the
[research and follow-up plan](execution-research.md).

## Accounting, leverage and affordability

`ohlcv_fixed_v1` accepts an optional `leverage` (default 1, range [1, 125]).
Cash accounting is **linear**: an entry debits `notional / leverage` of cash,
and equity is cash plus the position marked at the current close. The
alternative futures-style mode (`stocklike=False` with `automargin`) reaches
the same final value but marks the margin at the current price, which
overstates open profit in the equity curve and corrupts drawdown; it is
deliberately not used.

Before an entry is submitted the adapter applies one symmetric rule:

```
notional / leverage + commission <= equity - margin_already_used
```

If it does not hold, no order is sent and `ORDER_REJECTED` is emitted with
reason `insufficient_margin`. The strategy is not halted — it may size a
smaller entry on a later bar. The rule exists in the adapter because
Backtrader's own cash check never applies to a short (`shortcash` credits the
proceeds), so without it a short could exceed leverage silently. Backtrader's
long-only check remains as a backstop and, when it rejects an order, that
arrives as `ORDER_REJECTED` too.

Exit legs are submitted with `_checksubmit=False`. A closing order never needs
cash, and Backtrader's submit-time pseudo-execution runs both OCO legs against
one running cash figure — which used to margin-reject the take-profit of a
short whose notional approached the balance and, through the OCO link, cancel
its stop, leaving an unprotected position to the end of the data. This applies
to **every** model, `legacy_v1` included: it corrects an artefact, not a
documented semantic.

## Drawdown cut-off

The `max_drawdown` adapter parameter (percent, off by default) is checked
after every closed trade. Breaching it emits `DRAWDOWN_LIMIT_HIT`, calls
`cerebro.runstop()`, and stops the strategy from acting on any further bar.

Note that it is evaluated on realised equity after a close, not continuously,
so an open position can be far deeper underwater than the limit without
tripping it.

## What is not modelled

Say this out loud before quoting a result to anyone:

- **No observed spread or order-book slippage.** Legacy has no price
  adjustment. `ohlcv_fixed_v1` adds only the specified fixed costs, with no
  volatility, size-dependent impact, depth or queue model.
- **No funding or borrow costs.** There is no funding producer. The runner
  reports `funding_adjustment: 0.0` and discloses funding as `unavailable`;
  zero booked cashflow does not mean a historical rate was zero. Supplying
  any funding series, even empty or zero, is unsupported and rejected in the
  versioned config. The future implementation needs aligned historical rates
  and settlement mark prices, never present-day rates.
- **No partial fills and no liquidity limits.** Size is whatever the sizer
  says, and the market always absorbs it.
- **No liquidation, no margin calls, no maintenance margin.** Leverage
  affects the sizing cap and nothing else.
- **No extra latency, exchange tick/lot/notional filters, venue rejections or downtime.**
  The explicit extra delay is zero; next-bar matching and delayed protection
  still apply. Backtrader's own insufficient-cash rejection remains active.
- **One position at a time.** The adapter enters only when flat, so there is
  no pyramiding, no hedging, and no portfolio of concurrent positions.
- **One instrument.** Multiple feeds are timeframes of the same instrument,
  not different symbols.
- **No maker/taker classification.** Legacy uses the resolved taker rate;
  v1 uses the explicit uniform rate. Neither knows whether a fill added liquidity.

These omissions can bias results in different directions. Neither model is
a guaranteed upper or lower performance bound. Ambiguous bars, funding credits
and changed order eligibility alone defeat such a claim. Every deferred effect
has required inputs, an owner and acceptance criteria in the
[decision record](execution-research.md#staged-follow-up-and-acceptance-criteria).

## Determinism and look-ahead

Two runs of the same spec in the same process return identical results, trade
ids included: the adapter holds no global mutable state between runs and adds
no randomness.

`ohlcv_fixed_v1` validates positive finite initial capital, non-empty `(N, 6)`
feeds, finite OHLCV, positive coherent prices, non-negative volume, and unique
increasing integer millisecond timestamps. Prices, quantities and notionals
must remain finite during execution. These structural checks do not prove
historical completeness, correct symbol/market selection, absence of data
gaps, or sufficient strategy warmup. The caller owns that provenance.

Scalar state comes from index `[0]`, and history arrays end at the current
feed bar. The single-feed test
`tests/test_bt_adapter.py::test_strategy_never_sees_future_bar` checks that
later rows do not enter that window. It does not establish that a current
higher-timeframe bar has finished forming.

**Higher-timeframe availability.** A higher-timeframe bar is injected only
once it has closed before the primary bar's decision:

```
htf_open + htf_duration <= primary_open + primary_duration
```

Candles are timestamped at their opening time, so without this rule the
adapter exposed a forming HTF bar's final OHLCV — a bar revealing its own
future. The rule applies to **both** execution models; the previous behavior
was a defect, not a semantic. A second feed therefore requires
`primary_timeframe_ms` and `htf_timeframe_ms`, and the runner derives them
from the feed timeframes; the higher timeframe must be a whole multiple of the
primary.

Until the first higher-timeframe bar has closed, `htf_closes` and its siblings
stay `None` — the same "unavailable" value a single-feed run injects, not an
empty array. A strategy that guards with `if self.htf_closes is None` therefore
behaves identically during warm-up and with no higher-timeframe feed at all.

Only trailing rows can still be forming, so the scan stops at the first closed
bar and reads only the rows it injects. Injection cost is bounded by
`history_bars`, not by the length of the higher-timeframe feed.

Timeframe durations are resolved with `koval.engine.timeframe_utils`, which
understands minute, hour, day and week labels — `30m`, `2h` and `1w` included.
A label it cannot resolve is refused rather than treated as a sentinel
duration. Durations are required only when a second feed is present, so a
single-feed run accepts any label the engine will order.

Regression coverage:
`tests/test_bt_adapter_htf.py::test_htf_bar_is_visible_only_after_it_closes`,
`::test_mutating_an_unfinished_htf_bar_does_not_change_earlier_inputs`,
`::test_htf_arrays_stay_none_until_the_first_bar_has_closed` and
`::test_htf_injection_never_reads_the_whole_higher_timeframe_history`.

**One history window.** `history_bars` defaults to
`koval.engine.history_window.DEFAULT_HISTORY_BARS` (1000), the same window a
paper session injects, so a warmup-sensitive indicator cannot disagree between
the two runtimes.

**One timestamp conversion.** Under `ohlcv_fixed_v1`, events, the execution
ledger and the state injected into the strategy all go through
`time_conversion.utc_ms`, which treats a naive datetime as UTC and rounds to
the nearest millisecond. Legacy keeps the old truncating conversion for
reproduction. Regression coverage:
`tests/test_execution_realism.py::test_v1_event_ledger_and_strategy_timestamps_agree_at_millisecond_offsets`.

The look-ahead that no test can prevent is the one in your own head —
choosing a strategy, a symbol, or a date range because you already know how
it turned out. That one is on you.

## Reading a result honestly

- Compare against a benchmark you would actually have held, not against zero.
- Check `total_trades` before anything else. Under a few dozen closed trades,
  `win_rate` and `profit_factor` are noise.
- `max_drawdown` is computed from bar-close equity, so intra-bar pain is
  invisible and the real figure is worse.
- Run the same graph on a period you did not use while developing it. If it
  falls apart, you fitted the period, not the market.

## See also

- [results.md](results.md) — every field this package returns.
- [architecture.md](architecture.md) — where in the code each rule lives.
- [troubleshooting.md](troubleshooting.md) — when the numbers look wrong.
