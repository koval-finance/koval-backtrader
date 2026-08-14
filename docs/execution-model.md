# Execution model

What the simulated broker actually does. Read this before you believe a
number this package produces.

A backtest is a claim about what would have happened. The claim is only as
good as the mechanics behind it, so this page spells out every rule: when
orders are placed, at what price they fill, what is charged, and — most
importantly — what is not modelled at all.

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
be opened and closed within one bar. If you are comparing against a
vectorised backtester that assumes same-bar entry at the signal price, this
package will look worse, and it is the more honest of the two.

`SIGNAL_DETECTED` carries `entry_price` — the price the strategy asked for.
`ORDER_FILLED` carries `fill_price` — the price it got. They differ whenever
the market gapped, and the difference is real, not an artefact.

## Fill prices

Fill logic is Backtrader's, unmodified. The rules below are the ones that
apply to the order types this adapter emits.

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

Commission is charged only when `EngineRunSpec.execution_config` is present.
With no execution config the run is fee-free, deliberately, so unit tests can
assert on raw price action.

Fees resolve through the engine's `resolve_execution_settings()`, which maps
a venue to a taker rate and hands the broker a single percentage commission
applied to both sides of the trade. The built-in defaults:

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

Fees are the only cost modelled. See below.

## Drawdown cut-off

The `max_drawdown` adapter parameter (percent, off by default) is checked
after every closed trade. Breaching it emits `DRAWDOWN_LIMIT_HIT`, calls
`cerebro.runstop()`, and stops the strategy from acting on any further bar.

Note that it is evaluated on realised equity after a close, not continuously,
so an open position can be far deeper underwater than the limit without
tripping it.

## What is not modelled

Say this out loud before quoting a result to anyone:

- **No slippage.** Fills land exactly on the prices above. Backtrader's
  slippage parameters exist but this package never sets them.
- **No spread.** One price series serves as both bid and ask.
- **No funding or borrow costs.** A perpetual-futures short is free to hold
  forever. `funding_adjustment` exists in the trade record for hosts that
  compute it elsewhere; this package always reports `0.0`.
- **No partial fills and no liquidity limits.** Size is whatever the sizer
  says, and the market always absorbs it.
- **No liquidation, no margin calls, no maintenance margin.** Leverage
  affects the sizing cap and nothing else.
- **No latency, no venue rejections, no downtime.**
- **One position at a time.** The adapter enters only when flat, so there is
  no pyramiding, no hedging, and no portfolio of concurrent positions.
- **One instrument.** Multiple feeds are timeframes of the same instrument,
  not different symbols.
- **Maker/taker distinction is collapsed** into a single taker-rate
  commission, so limit entries are charged as if they crossed the spread.

Every one of these makes results optimistic. A backtest here is an upper
bound on the same strategy's real performance, not an estimate of it.

## Determinism and look-ahead

Two runs of the same spec in the same process return identical results, trade
ids included: the adapter holds no global mutable state between runs and adds
no randomness.

The strategy is never handed a future bar. Scalar state comes from index
`[0]`, the current bar; history arrays come from `line.get(ago=0, size=n)`,
which ends at the current bar. `tests/test_bt_adapter.py::test_strategy_never_sees_future_bar`
pins this with a strictly increasing price series: any leak would show up as
a strategy observing a price it should not have seen yet.

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
