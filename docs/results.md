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
`float`. The one field that can be `None` is `profit_factor`.

The examples on this page come from one real run — the bundled
`ema_cross_trend` graph over `sample-1h.csv`, 1200 hourly candles, 10 000
starting capital, Binance futures fees.

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

Every metric except `max_drawdown` comes from the engine's shared
`build_closed_trade_metrics()`, which is MIT and is also what a paper run
uses. That is deliberate: a backtest and a live paper session report the same
numbers because they run the same code, not because two implementations were
kept in step by hand.

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
| `realized_pnl` | float | Net PnL: after commission, plus `funding_adjustment`. This is what every metric is computed from. |
| `gross_realized_pnl` | float | PnL after commission but **before** funding. Equal to `realized_pnl` here, since funding is always zero. |
| `funding_adjustment` | float | Always `0.0` from this package. The field exists for hosts that compute funding themselves and pass it in. |
| `commission` | float | Total commission for the round trip, both sides. `0` when the run had no `execution_config`. |
| `stop_loss` | float | The stop price from the strategy's `TradeSetup`, as at entry. Not updated if the stop was trailed. |
| `take_profit` | float or null | The target from the `TradeSetup`. **`null` when the strategy set none** and the adapter derived one from `risk_reward_ratio` — the derived level is not written back here. |
| `reason` | str | Always `"Signal"`. A placeholder from the analyzer's field set. |
| `exit_reason` | str | `"Take Profit"`, `"Stop Loss"`, or `"Manual"` for anything else. |
| `sl_calculation` | str or null | The strategy's explanation of how the stop was derived, if it set `sl_calc_expr`. |
| `tp_calculation` | str or null | Same for the target. |
| `why_entry` | list[str] | The strategy's stated reasons for entering. Graph strategies fill this only when a block produced a reasoning chain. |
| `indicators_at_entry` | dict | Indicator values captured at entry, if the strategy provided them. |

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
| `SESSION_START` | Adapter construction, before any bar | `strategy` |
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
