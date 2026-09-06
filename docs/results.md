# Results reference

Everything `BacktraderBacktestEngine.run()` gives back, field by field, with
the definitions used to compute it.

`BacktestResult` is a plain dataclass owned by koval-engine:

```python
@dataclass
class BacktestResult:
    metrics: dict[str, Any]
    trades: list[dict[str, Any]]
    equity_curve: list[dict[str, Any]]
```

No Backtrader object crosses the boundary, and every value below serialises
with `json.dumps` — numpy scalars survive because `np.float64` subclasses
`float`. `profit_factor` and optional strategy fields can be `None`.

The examples on this page come from one real run — the bundled
`ema_cross_trend` graph over `sample-1h.csv`, 1200 hourly candles, 10 000
starting capital, legacy Binance futures fee assumptions. Numeric examples
below omit the newly added execution metadata for readability.

## Metrics

```json
{
  "initial_capital": 10000.0,
  "final_capital": 10969.32757778207,
  "total_pnl": 969.3275777820709,
  "total_trades": 14,
  "win_rate": 57.14285714285714,
  "profit_factor": 2.451428857241573,
  "win_count": 8,
  "loss_count": 6,
  "avg_win": 204.6464062326714,
  "avg_loss": -111.30727867988314,
  "max_drawdown": 2.8042923980132675
}
```

| Key | Type | Definition |
|---|---|---|
| `initial_capital` | float | Echoed from `EngineRunSpec.initial_capital`. |
| `final_capital` | float | Broker account value after the last bar, including any position still open. |
| `total_pnl` | float | `final_capital - initial_capital`. |
| `total_trades` | int | Number of **closed** trades. A position open at the end is not counted. |
| `win_rate` | float | Percent, `win_count / total_trades * 100`. `0.0` when there are no trades. |
| `profit_factor` | float or `None` | Gross wins ÷ gross losses. `0.0` with no trades at all; **`None` when there were wins but no losses**, because the ratio is undefined. |
| `win_count` | int | Closed trades with `realized_pnl > 0`. |
| `loss_count` | int | Closed trades with `realized_pnl < 0`. A break-even trade counts as neither. |
| `avg_win` | float | Mean `realized_pnl` of winners. `0.0` when there are none. |
| `avg_loss` | float | Mean `realized_pnl` of losers, so a negative number. |
| `max_drawdown` | float | Percent. Largest peak-to-trough decline of the equity curve. |

The scalar performance metrics except `max_drawdown` come from the engine's shared
`build_closed_trade_metrics()`, which is MIT and is also what a paper run
uses. That is deliberate: a backtest and a live paper session report the same
numbers because they run the same code, not because two implementations were
kept in step by hand.

`metrics.execution_model` is added by this plugin for every run. It contains
the version, replayable `resolved_config`, observed-input description, resolved
assumptions, `unmodelled_effects`, data-quality disclosure, software versions,
and `implementation_sha256`. The fingerprint covers the plugin's `.py` source
names and bytes in sorted order, including editable changes. Store the graph
and exact input datasets alongside it; a fingerprint identifies code but does
not archive it. The same metadata appears in `SESSION_START`.

Only `ohlcv_fixed_v1` adds `metrics.execution_costs`. These are quote-currency
amounts, with no implied USD conversion:

| Field | Meaning |
|---|---|
| `spread_cost`, `slippage_cost` | Actual adverse price adjustment allocated to each assumption, including open entries. |
| `price_adjustment_cost` | Sum of those embedded price costs; never deduct it again from actual-price PnL. |
| `commission` | Total broker commission on executed fills, including open entries. |
| `funding_cashflow`, `funding_status` | `0.0` booked and `"unavailable"`; not measured zero funding. |
| `reference_pnl` | Signed cashflows at matched reference prices plus ending exposure marked at the last close. |
| `closed_net_pnl` | Sum of closed trade `realized_pnl`. |
| `open_unrealized_pnl` | Remaining position PnL at actual entry price and last close, before its entry commission. |
| `open_commission` | Entry commission belonging to the still-open position. |
| `reconciliation_error` | Final broker capital minus independently reconstructed capital; must be zero within floating-point tolerance. |
| `fills` | Every actual fill, including entries still open when data ends. No hypothetical, pending, canceled or rejected execution. |

Each fill records `fill_id`, `trade_id` (both scoped to this run),
`timestamp_ms`, `bar_index`, `role` (`entry`/`exit`), `koval_role`
(`entry`/`stop_loss`/`take_profit`), `side` (`buy`/`sell`), `order_type`,
positive `size`, `reference_price`, actual `fill_price`, the three price-cost
amounts, and `commission`. `koval_role` distinguishes a take-profit — modelled
as market-on-touch and charged the full adverse adjustment — from an entry
limit, which is still never filled worse than its limit, even though Backtrader
names both `limit` in `order_type`. `commission_policy` is `uniform` and
`liquidity_role` is `unavailable`. An exit references the same trade ID as its
entry; no process-global Backtrader reference is persisted.

Reference PnL describes the **same fills and sizes** before their price
adjustment. It is not the PnL of a second, cost-free strategy run: changing fills
can also change brackets, future decisions, sizing and cash rejection.

### Reconciliation

For the new model, both identities must hold:

```
final = initial + reference_pnl - spread_cost - slippage_cost - commission
final = initial + closed_net_pnl + open_unrealized_pnl - open_commission
```

Funding contributes zero booked cashflow in this version. Actual-price trade
PnL already includes spread/slippage. The runtime compares the identities
against the broker using `math.isclose(rel_tol=1e-12, abs_tol=1e-8)` and raises
if accounting diverges. Open-position commissions explain part of the
difference between total PnL and the closed trade sum.

Worked synthetic example: capital 10,000; buy two units at reference 100;
stop at 90; exit bar opens at 85. Configure full spread 20 bps, slippage
10 bps and uniform commission 4 bps. The actual fills are 100.20 and 84.83.
Reference PnL is -30; spread cost is 0.37, slippage cost is 0.37, actual-price
gross PnL is -30.74, and commission is 0.148024. Net PnL is -30.888024 and
final broker equity is **9969.111976**. See the
[validation record](execution-validation.md) and the executable probe in
[`tests/test_execution_realism.py`](../tests/test_execution_realism.py).

Two things worth internalising:

**`total_pnl` and the sum of `realized_pnl` can disagree.** `total_pnl`
comes from the broker and includes an open position's unrealised PnL; the
trade list only has closed trades. If the last signal is still open when the
candles run out, the difference is that position.

**`max_drawdown` is sampled at bar closes**, so a spike that dipped and
recovered inside one bar never appears. The true figure is always at least
this bad, never better.

## Trades

One dictionary per closed trade, in the order they closed.

```json
{
  "id": 1,
  "direction": "LONG",
  "entry_price": 123.4434,
  "entry_time": "2023-11-25T13:13:20Z",
  "exit_price": 128.381136,
  "exit_time": "2023-11-25T16:13:20Z",
  "duration": "3:00:00",
  "size": 40.50439310647632,
  "realized_pnl": 195.92,
  "gross_realized_pnl": 195.92,
  "funding_adjustment": 0.0,
  "commission": 4.0800000000000125,
  "stop_loss": 120.974532,
  "take_profit": 128.381136,
  "reason": "Signal",
  "exit_reason": "Take Profit",
  "sl_calculation": null,
  "tp_calculation": null,
  "why_entry": [],
  "indicators_at_entry": {}
}
```

| Field | Type | Meaning |
|---|---|---|
| `id` | int | Sequential within the run, starting at 1 in close order. Matches the `trade_id` on the trade's events. |
| `direction` | str | `"LONG"` or `"SHORT"`, upper case, taken from the executed position. |
| `entry_price` | float | Average fill price of the entry. |
| `entry_time` | str | ISO 8601, UTC, `Z`-suffixed. The bar the entry filled on. |
| `exit_price` | float | Derived as `entry_price ± gross_pnl / size`, which is the true average exit fill. |
| `exit_time` | str | ISO 8601, UTC, `Z`-suffixed. |
| `duration` | str | `str(exit_time - entry_time)`, e.g. `"3:00:00"` or `"1 day, 4:00:00"`. |
| `size` | float | Filled quantity, always positive. Captured at entry, because Backtrader reports `0` on a closed trade. |
| `realized_pnl` | float | Net PnL: after commission, plus `funding_adjustment`. Used for closed-trade statistics; total account PnL and drawdown come from broker equity. |
| `gross_realized_pnl` | float | Historical compatibility name: **net of commission**, before funding. Not gross trading PnL. Preserved unchanged; prefer the explicit v1 fields below. |
| `funding_adjustment` | float | `0.0` from the runner because funding is unavailable. It must not be interpreted as observed zero funding. |
| `commission` | float | Total commission for the round trip, both sides. `0` when the run had no `execution_config`. |
| `stop_loss` | float | The stop price from the strategy's `TradeSetup`, as at entry. Not updated if the stop was trailed. |
| `take_profit` | float or null | The target from the `TradeSetup`. **`null` when the strategy set none** and the adapter derived one from `risk_reward_ratio` — the derived level is not written back here. |
| `reason` | str | Always `"Signal"`. A placeholder from the analyzer's field set. |
| `exit_reason` | str | `"Take Profit"`, `"Stop Loss"`, or `"Manual"` for anything else. |
| `sl_calculation` | str or null | The strategy's explanation of how the stop was derived, if it set `sl_calc_expr`. |
| `tp_calculation` | str or null | Same for the target. |
| `why_entry` | list[str] | The strategy's stated reasons for entering. Graph strategies fill this only when a block produced a reasoning chain. |
| `indicators_at_entry` | dict | Indicator values captured at entry, if the strategy provided them. |

The new model adds `gross_price_pnl` (actual-fill PnL **before commission**),
`net_pnl_before_funding` (gross price PnL minus actual commission), and
`execution_costs` (the cost totals, `reference_pnl`, funding status and the
two fills belonging to this trade). Entry and exit prices are copied from
the execution ledger, so they exactly match the event fill prices. The legacy
trade field set is retained for legacy runs.

For direct legacy analyzer users only, `get_trade_info()` can still inject
an analyzer-only funding adjustment for compatibility. That hook does not
change broker cash/equity and is **not a funding model**. The new model rejects
nonzero adjustments through that hook; implement funding in the broker before
reporting it as part of net results.

### Ids are per run

`id` counts from 1 within each result, so two backtests in the same
interpreter number their trades identically and two results can be diffed
directly. In 0.9.0 this field carried Backtrader's process-wide trade
reference instead, and a second run in one process started numbering where
the first had stopped; ids in results stored from that version are not
comparable across runs.

## Equity curve

One point per bar of the lowest timeframe, including bars with no position:

```json
[
  {"timestamp": "2023-11-14T22:13:20", "equity": 10000.0},
  {"timestamp": "2024-01-03T21:13:20", "equity": 10969.32757778207}
]
```

`equity` is the broker's account value: cash plus the mark-to-market value of
any open position. The list length equals the number of candles in the
primary feed.

Note the asymmetry with trade timestamps: **equity timestamps carry no `Z`
suffix**, because they come from Backtrader's naive datetimes. They are UTC.
If you merge the two series, normalise first.

## Events

Passing `on_event` to `run()` gives you a live decision trace — the reason a
run is auditable rather than a number that appeared from nowhere. Each call
receives a plain dict:

```python
{"event_type": "SIGNAL_DETECTED", "bar_index": 255, "timestamp_ms": 1700907200000, "payload": {...}}
```

`bar_index` is 1-based and counts bars processed on the primary feed.

| Event | When | Payload |
|---|---|---|
| `SESSION_START` | Adapter construction, before any bar | `strategy`, `execution_model` when called through the runner |
| `FILTER_REJECTED` | A signal fired but a filter blocked it | `direction` |
| `FILTER_PASSED` | Signal survived every filter | `direction` |
| `SIGNAL_DETECTED` | Setup built, before the order | `direction`, `entry_price`, `stop_loss`, `take_profit`, `why_entry`, `indicators_at_entry` |
| `ORDER_PLACED` | Entry order submitted | `direction`, `entry_type`, `size`, `price` |
| `ORDER_FILLED` | Entry filled; bracket placed | `direction`, `fill_price`, `size` |
| `TRADE_OPENED` | Position opened | `trade_id`, `direction`, `entry_price`, `stop_loss`, `take_profit`, `why_entry` |
| `TRADE_CLOSED` | Position closed | `trade_id`, `pnl`, `pnl_comm`, `exit_reason`, `entry_price`, `exit_price` |
| `DRAWDOWN_LIMIT_HIT` | `max_drawdown` breached after a close | `drawdown_pct`, `limit` |
| `SESSION_END` | Last bar | `total_trades`, `final_value` |

The sample run emits exactly 14 of each event from `FILTER_PASSED` through
`TRADE_CLOSED`, plus one `SESSION_START` and one `SESSION_END`.

The two `FILTER_*` events report `DeclarativeStrategy.filters()`, nothing
else. A graph strategy evaluates its `filter.*` blocks inside the graph, so a
blocked signal never reaches the adapter: the bar produces no events at all,
and `FILTER_REJECTED` never fires. Its `FILTER_PASSED` events are a vacuous
pass over an empty filter list. Real rejections only appear from a
hand-written strategy that overrides `filters()`.

### Reading `TRADE_CLOSED`

`trade_id` matches the `TRADE_OPENED` of the same trade and the `id` of the
corresponding trade record, so the three views of one trade join cleanly.

`exit_reason` on the event is the internal token — `"stop_loss"`,
`"take_profit"`, or `"unknown"` for a close that came through neither bracket
leg. The trade record carries the display label for the same thing
(`"Stop Loss"`, `"Take Profit"`, `"Manual"`). `exit_price` is the bracket's
fill price, falling back to the bar's close when there was no bracket fill to
report.

In 0.9.0 all three fields were wrong on this event — the reason was always
`"unknown"`, the price was the bar close, and the id was one lower than the
matching `TRADE_OPENED`. Only the event was affected; trade records from that
version are correct.

No event is emitted when an order is cancelled, rejected, or refused for
margin. An `ORDER_PLACED` with no following `ORDER_FILLED` is how you spot
one — usually a size the account cannot afford.

## See also

- [execution-model.md](execution-model.md) — how these numbers are produced.
- [strategies.md](strategies.md) — what a strategy can put into them.
