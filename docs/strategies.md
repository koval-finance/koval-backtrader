# Writing strategies for this adapter

Two kinds of strategy run here, and they use the same machinery.

**Graph strategies** are JSON: a list of blocks and the connections between
them. koval-engine assembles them into a `DeclarativeStrategy` for you. Most
users never write Python at all — `koval blocks` lists what is available, and
the engine owns every block's semantics.

**Hand-written strategies** subclass `DeclarativeStrategy` directly. Use one
when a block does not exist for what you want, or when you are prototyping a
block before proposing it upstream.

Either way, the strategy expresses *intent* and never touches Backtrader.
There is no `self.buy()`; you return a `TradeSetup` and the adapter turns it
into orders. That separation is what lets the same strategy run on a
different backtest engine, or on the engine's paper broker, without changes.

## The hooks the adapter calls

Per bar, in this order:

| Hook | When | Returns |
|---|---|---|
| `on_bar()` | Every bar, once state has been injected | — |
| `should_long()` | Every bar with no position and no live entry order | `bool` |
| `should_short()` | Same bar, only if `should_long()` was `False` | `bool` |
| `filters()` | After a direction is chosen | list of zero-argument callables; all must return `True` |
| `go_long()` / `go_short()` | Once the filters pass | `TradeSetup` |
| `should_cancel_entry()` | Every bar an entry order is still unfilled | `bool` |
| `on_open_position(trade_id, setup)` | The bar the position opens | — |
| `on_sl_update(trade_id)` | Every bar with a position and a live stop, the entry-fill bar included | new stop price or `None` |
| `on_tp_update(trade_id)` | Same | new target price or `None` |
| `on_close_position(trade_id, result)` | The bar the position closes | — |

Things worth knowing before you rely on them:

- **`on_bar()` is the only hook that runs unconditionally.** It fires on
  every bar, in a position or not, which is also what koval-engine's live
  runner does — so per-bar bookkeeping belongs there and behaves the same in
  a backtest as it does in paper. `should_long()` is not a substitute: it is
  skipped while a position is open. (0.9.0 never called `on_bar()` at all.)
- **`trade_id` is the same number in every hook** and in the events and trade
  record for that trade, counting from 1. (0.9.0 passed `N - 1` to
  `on_sl_update()`, `on_tp_update()` and `on_close_position()`.)
- **Long wins ties.** `should_short()` is only consulted when `should_long()`
  returned `False`.
- **One position at a time.** No entry is attempted while a position is
  open, so there is no pyramiding or hedging.

## What is injected before each bar

`_inject_state()` writes these attributes onto your instance before any hook
runs. Read them; do not set them.

| Attribute | Type | Meaning |
|---|---|---|
| `close`, `high`, `low`, `open`, `volume` | float | The current (closed) bar. |
| `bar_index` | int | 1-based count of bars processed. |
| `timestamp_ms` | int | Bar timestamp, epoch milliseconds UTC. |
| `closes`, `highs`, `lows`, `opens`, `volumes` | ndarray or `None` | Chronological history, `arr[-1]` is the current bar. Length is `min(history_bars, bars so far)`, capped at 300 by default. |
| `htf_closes`, `htf_highs`, `htf_lows`, `htf_opens`, `htf_volumes` | ndarray or `None` | The same for the second feed. `None` when only one timeframe was supplied. |
| `account_value` | float | Broker equity, marked to market. Equal to `account.equity`. |
| `account` | `AccountInputs` | The full account state — see below. |
| `position_size` | float | Absolute size, `0.0` when flat. |
| `position_direction` | str or `None` | `"long"`, `"short"`, or `None`. |
| `config` | dict | Strategy configuration. Always `{}` on the `run()` path — see the limitation below. |

### `account`

`account_value` is one number, and one number cannot tell you whether the
entry you got was the entry you asked for, what the fees have already cost,
or how far below its peak the account is. Those decide whether a risk gate
should fire, so they are injected too.

The balance, equity, realized/unrealized split, daily PnL, peak equity and
drawdown come from `koval.engine.account_state.PlatformAccountState` — the
same MIT account contract as paper. In this plugin the graph context is bound
to the broker ledger through `DeclarativeStrategy.bind_account(provider)`.
`account_snapshot()` is the public engine reader; the plugin's existing
`account` attribute and its extra risk diagnostics remain supported. See the
[paired acceptance scope](execution-validation.md#0111-paired-acceptance).

| Field | Meaning |
|---|---|
| `balance` | Starting capital plus realized results and fees. Excludes open PnL. |
| `equity` | `balance` plus the open position marked to the last close. |
| `realized_pnl`, `unrealized_pnl` | The two halves of the above, kept apart. |
| `daily_pnl` | Equity minus the previous bar's equity at the UTC day boundary (initial capital on the first day). |
| `peak_equity`, `drawdown_pct` | Running peak and the distance below it. |
| `margin_used`, `free_margin` | Notional over leverage for the open position, and what remains. |
| `fees`, `fees_paid` | Cumulative commission and liquidation fees; the latter is a compatibility alias. |
| `daily_loss_pct`, `trade_realized_pnl` | Engine daily-loss percentage and gross ledger trade PnL. |
| `open_position` | Canonical engine position: buy/sell side, actual average price, quantity, current stop and margin. |
| `funding_paid`, `funding_status` | Signed funding and `modelled` with v2 funding evidence; otherwise zero and `unavailable`. `funding` is the canonical field. |
| `open_positions` | `0` or `1`. This adapter never pyramids. |
| `position` | `None` when flat, otherwise the record below. |

`account.position` describes what actually happened, not what was requested:

| Field | Meaning |
|---|---|
| `side` | `"long"` or `"short"`. |
| `entry_price` | The **actual** fill, gap and costs included. |
| `quantity` | The **actual** filled size. |
| `current_stop` | The stop in force now, including any move the strategy made. |
| `entry_commission` | Commission charged on the entry leg. |
| `risk_amount` | Quote-currency loss if the position closes at its stop, counting the adverse adjustment on the exit and the commission on both legs. A bar that *gaps* through the stop costs more than this. |

Sizing from `setup.entry_price` after a gap is sizing from a price the
account never paid. `ORDER_FILLED` carries `planned_risk`, `actual_risk` and `risk_drift`.
These use configured fixed cost assumptions and are estimates when fee or
impact evidence overrides those costs; inspect the actual execution ledger.
Stop widening and invalid final stop/target pairs fail before cancelling live
simulation protection. Valid profit locks and target changes are supported.

The arrays are freshly allocated each bar; slicing them is cheap, and
mutating them affects nothing. If your indicator needs more than 1000 bars of
history, raise `history_bars` (see below) — until you do, `closes` is
silently truncated and a 200-period average computed from it is wrong.

Nothing here reaches into the future. Scalars come from index `[0]` and
arrays end at the current bar.

## Returning a `TradeSetup`

```python
TradeSetup(
    direction="long",  # required: "long" | "short"
    entry_price=self.close,  # required
    stop_loss=self.close * 0.98,  # required
    take_profit=None,  # optional; derived from risk_reward_ratio if None
    size=None,  # optional; the adapter sizes the trade if None
    entry_type="market",  # "market" | "limit" | "stop"; the dataclass default is "limit"
    why_entry=["20-bar breakout"],
    indicators_at_entry={"rsi": 61.2},
    sl_calc_expr="20-bar low",
    tp_calc_expr=None,
)
```

`entry_price` is used as the price for limit and stop entries. For a market
entry it is only recorded — the actual fill is the next bar's open.

`why_entry`, `indicators_at_entry`, `sl_calc_expr` and `tp_calc_expr` are
carried straight through to the closed-trade record. They cost nothing and
turn a trade list into something you can audit six months later; fill them in.

## A complete hand-written strategy

This runs as written. It builds its own `Cerebro` rather than going through
`EngineRunSpec`, which is the way to embed the adapter when you are not
driving it from a graph.

```python
import backtrader as bt
import numpy as np
import pandas as pd
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup

from koval_backtrader.bt_adapter import make_bt_strategy_class
from koval_backtrader.bt_analyzers import EquityCurveAnalyzer, TradeListAnalyzer
from koval_backtrader.oco_patch import apply_oco_guard

apply_oco_guard()  # required: without it, both bracket legs can fill on one bar


class BreakoutStrategy(DeclarativeStrategy):
    """Long when the close makes a new 20-bar high; stop at the 20-bar low."""

    LOOKBACK = 20

    def should_long(self) -> bool:
        if self.closes is None or len(self.closes) <= self.LOOKBACK:
            return False
        return self.close >= self.closes[-self.LOOKBACK :].max()

    def go_long(self) -> TradeSetup:
        return TradeSetup(
            direction="long",
            entry_price=self.close,
            stop_loss=float(self.lows[-self.LOOKBACK :].min()),
            entry_type="market",
            why_entry=[f"close {self.close:.2f} broke the {self.LOOKBACK}-bar high"],
            indicators_at_entry={"lookback_high": float(self.closes[-self.LOOKBACK :].max())},
        )


def demo_feed(n=300):
    rng = np.random.default_rng(7)
    price = 100 + np.cumsum(rng.standard_normal(n))
    index = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    frame = pd.DataFrame(
        {
            "open": price,
            "high": price * 1.004,
            "low": price * 0.996,
            "close": price,
            "volume": 1000.0,
        },
        index=index,
    )
    return bt.feeds.PandasData(dataname=frame)


cerebro = bt.Cerebro()
cerebro.adddata(demo_feed())
cerebro.broker.setcash(10_000.0)
cerebro.broker.setcommission(commission=0.0004)  # 0.04%, both sides
cerebro.addstrategy(
    make_bt_strategy_class(BreakoutStrategy, risk_per_trade=1.0, risk_reward_ratio=2.0)
)
cerebro.addanalyzer(TradeListAnalyzer, _name="trades")
cerebro.addanalyzer(EquityCurveAnalyzer, _name="equity")

strategy = cerebro.run()[0]
trades = strategy.analyzers.trades.get_analysis()
print(f"{len(trades)} trades, final equity {cerebro.broker.getvalue():.2f}")
```

Output on this seed: `2 trades, final equity 9853.15`. Both stopped out,
which is what a breakout strategy does to random data — a reminder that a
working example is not a working strategy.

`strategy._events` holds the full `EngineEvent` list afterwards, or pass
`event_sink=callable` to `make_bt_strategy_class` to receive them live.

## Adapter parameters

`make_bt_strategy_class()` accepts these; unknown keyword arguments are
passed through as extra Backtrader params.

| Parameter | Default | Effect |
|---|---|---|
| `risk_per_trade` | `1.0` | Percent of equity risked, used only when `TradeSetup.size` is `None`. |
| `risk_reward_ratio` | `2.0` | Multiple of stop distance used to derive a target when `take_profit` is `None`. |
| `leverage` | `1.0` | Raises the sizing cap only. Does not change what a stop-out costs. |
| `max_drawdown` | `None` | Percent. When breached after a close, trading stops for the rest of the run. |
| `history_bars` | `300` | Length of the injected history arrays. |
| `strategy_config` | `{}` | Copied onto `strategy.config`. |
| `event_sink` | `None` | Callable receiving each `EngineEvent` as it is emitted. |

**Limitation worth stating plainly:** none of these are reachable through
`EngineRunSpec`. `BacktraderBacktestEngine.run()` sets only `event_sink`, so
a run driven by `koval backtest` or `load_backtest_engine()` uses the
defaults above. In practice graph strategies do not need them — the
`risk.pct_risk` block supplies `size` and `exit.fixed_sl_tp` supplies
`take_profit`, so sizing and targets come from the graph. But if you need 500
bars of history or a drawdown cut-off, you have to build the Cerebro
yourself, as above.

## Multiple timeframes

Pass more than one feed and the engine sorts them ascending, so the lowest
timeframe is the clock and the next one up is injected as `htf_*`:

```python
spec = EngineRunSpec(
    graph=graph,
    feeds={"1h": hourly, "4h": four_hourly},
    initial_capital=10_000.0,
)
```

Only two are consumed. A third feed is loaded into Cerebro and then ignored,
because the adapter reads `self.datas[1]` and stops there.

The higher-timeframe arrays contain only bars that have closed at the current
point in time, so no future leaks in through them. Their length is capped by
`history_bars` like everything else.

## Testing a strategy

The suite in this repository is the model to copy: build a `pd.DataFrame`
whose prices make the case unambiguous — a clean trend for entries, a single
engineered gap for fills — and assert on emitted events or closed trades
rather than on adapter internals. `tests/test_bt_adapter.py` has the
established shape, including a probe strategy that records what it saw.

Call `apply_oco_guard()` at module import in any test that places bracket
orders. The runner applies it, so a test that skips it is testing unpatched
Backtrader and will eventually disagree with production for reasons that take
a day to find.

## See also

- [execution-model.md](execution-model.md) — what happens to your `TradeSetup`.
- [results.md](results.md) — what your strategy's annotations look like on the way out.
- [custom-engines.md](custom-engines.md) — running the same strategy on something other than Backtrader.
