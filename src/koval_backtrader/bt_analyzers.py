# SPDX-License-Identifier: GPL-3.0-or-later

from datetime import UTC

import backtrader as bt


class TradeListAnalyzer(bt.Analyzer):
    def __init__(self):
        self.trades = []

    def notify_trade(self, trade):
        if trade.isclosed:
            pnl = trade.pnl
            pnlcomm = trade.pnlcomm
            entry_date = bt.num2date(trade.dtopen)
            exit_date = bt.num2date(trade.dtclose)
            if entry_date.tzinfo is None:
                entry_date = entry_date.replace(tzinfo=UTC)
            else:
                entry_date = entry_date.astimezone(UTC)
            if exit_date.tzinfo is None:
                exit_date = exit_date.replace(tzinfo=UTC)
            else:
                exit_date = exit_date.astimezone(UTC)

            size = abs(trade.size) if trade.size else 0
            trade_record = {
                "id": trade.ref,
                "direction": "LONG" if trade.long else "SHORT",
                "entry_price": trade.price,
                "entry_time": entry_date.isoformat().replace("+00:00", "Z"),
                "exit_time": exit_date.isoformat().replace("+00:00", "Z"),
                "duration": str(exit_date - entry_date),
                "realized_pnl": pnlcomm,
                "stop_loss": 0,
                "take_profit": 0,
                "reason": "Signal",
                "exit_reason": "Unknown",
                "commission": pnl - pnlcomm,
            }

            info = (
                self.strategy.get_trade_info(trade.ref)
                if hasattr(self.strategy, "get_trade_info")
                else {}
            )
            if info:
                canonical_direction = trade_record["direction"]
                trade_record.update(info)
                trade_record["direction"] = canonical_direction
                if "direction" in info:
                    trade_record["signal_direction"] = info["direction"]
                funding_adjustment = info.get("funding_adjustment", 0.0)
                try:
                    funding_adjustment = float(funding_adjustment)
                except (TypeError, ValueError):
                    funding_adjustment = 0.0
                trade_record["funding_adjustment"] = funding_adjustment
                trade_record["gross_realized_pnl"] = pnlcomm
                trade_record["realized_pnl"] = pnlcomm + funding_adjustment
                if size == 0 and "size" in info:
                    size = info["size"]
                if "exit_reason" in info:
                    trade_record["exit_reason"] = info["exit_reason"]
                elif trade_record["realized_pnl"] > 0:
                    trade_record["exit_reason"] = "Take Profit (Approx)"
                else:
                    trade_record["exit_reason"] = "Stop Loss (Approx)"
                if "sl_calculation" in info:
                    trade_record["sl_calculation"] = info["sl_calculation"]
                if "tp_calculation" in info:
                    trade_record["tp_calculation"] = info["tp_calculation"]

            if size != 0:
                pnl_per_unit = pnl / size
                if trade.long:
                    trade_record["exit_price"] = trade.price + pnl_per_unit
                else:
                    trade_record["exit_price"] = trade.price - pnl_per_unit
                trade_record["size"] = size
            else:
                exit_price = trade.price
                if trade.history:
                    last_event = trade.history[-1]
                    event_price = getattr(getattr(last_event, "event", None), "price", None)
                    if event_price is not None:
                        exit_price = event_price
                trade_record["exit_price"] = exit_price
                trade_record["size"] = 0

            self.trades.append(trade_record)

    def get_analysis(self):
        return self.trades


class EquityCurveAnalyzer(bt.Analyzer):
    def __init__(self):
        self.equity_curve = []

    def next(self):
        self.equity_curve.append(
            {
                "timestamp": self.datas[0].datetime.datetime(0),
                "equity": self.strategy.broker.getvalue(),
            }
        )

    def get_analysis(self):
        return self.equity_curve
