# SPDX-License-Identifier: GPL-3.0-or-later
"""Run-local links and recorded decision inputs; never calculate money here."""

from copy import deepcopy
from dataclasses import asdict

from koval_backtrader.time_conversion import num2utc_ms


class ExecutionTrace:
    def __init__(self):
        self.decisions = []
        self.intents = []
        self._intents_by_id = {}
        self.orders = {}
        self.account_snapshots = []
        self.decision_id = None
        self.intent = None
        self._legacy_order_ids = {}

    def begin_decision(self, strategy):
        brain = strategy._strategy
        timestamp = strategy._safe_timestamp_ms()
        count = len(brain.closes)
        self.decision_id = f"decision-{len(self.decisions) + 1}"
        self.intent = None
        self.decisions.append(
            {
                "decision_id": self.decision_id,
                "bar_index": len(strategy.data),
                "bar_timestamp_ms": timestamp,
                "decision_timestamp_ms": brain.decision_timestamp_ms,
                "history_start_ms": num2utc_ms(strategy.data.datetime[-(count - 1)]),
                "history_end_ms": brain.decision_timestamp_ms,
                "history_bars": count,
                "account": asdict(strategy._account.snapshot()),
                "indicators": None,
            }
        )
        self.account_snapshots.append(
            {
                "timestamp_ms": timestamp,
                "decision_id": self.decision_id,
                "account": asdict(strategy._account.snapshot()),
            }
        )

    def begin_intent(self, setup):
        self.intent = {
            "intent_id": f"intent-{len(self.intents) + 1}",
            "decision_id": self.decision_id,
            "status": "requested",
            "setup": asdict(setup),
        }
        self.intents.append(self.intent)
        self._intents_by_id[self.intent["intent_id"]] = self.intent

    def reject(self, payload):
        order = self.orders.get(payload.get("order_id"))
        intent = self.intent if order is None else self._intents_by_id.get(order["intent_id"])
        if intent is not None:
            intent.update(status="rejected", reason=payload.get("reason"))
            payload.update(intent_id=intent["intent_id"], decision_id=intent["decision_id"])

    def record_order(self, strategy, order):
        if order is None:
            return
        order_id = (
            strategy.broker.order_id(order)
            if hasattr(strategy.broker, "order_id")
            else f"order-{self._legacy_order_ids.setdefault(order.ref, len(self._legacy_order_ids) + 1)}"
        )
        if order_id not in self.orders:
            origin = order.info.get("koval_decision_id", self.decision_id)
            intent_id = order.info.get(
                "koval_intent_id", None if self.intent is None else self.intent["intent_id"]
            )
            order.addinfo(koval_decision_id=origin, koval_intent_id=intent_id)
            self.orders[order_id] = {
                "order_id": order_id,
                "decision_id": origin,
                "intent_id": intent_id,
                "side": "buy" if order.isbuy() else "sell",
                "order_type": order.getordername().lower(),
                "role": order.info.get("koval_role", "entry"),
                "requested_quantity": abs(float(order.created.size)),
                "requested_price": float(order.created.price),
            }
        self.orders[order_id].update(
            status=order.getstatusname().lower(),
            cumulative_quantity=abs(float(order.executed.size)),
            remaining_quantity=abs(float(order.executed.remsize)),
        )
        intent = self._intents_by_id.get(self.orders[order_id]["intent_id"])
        if order.info.get("koval_setup") is not None and intent is not None:
            status = order.getstatusname().lower()
            status = {"completed": "filled", "margin": "rejected"}.get(status, status)
            intent.update(order_id=order_id, status=status)

    def export(self, strategy):
        # OCO resizing changes a sibling's remaining quantity without emitting
        # another notification. Persist the broker's actual final order state.
        for order in strategy.broker.get_orders_open():
            self.record_order(strategy, order)
        fills = getattr(strategy.broker, "execution_fills", [])
        account = asdict(strategy._account.snapshot())
        reasons = []
        if not hasattr(strategy.broker, "account_ledger"):
            reasons.append("authoritative_fill_ledger_unavailable")
        if strategy.params.primary_timeframe_ms is None:
            reasons.append("decision_clock_unavailable")
        return deepcopy(
            {
                "version": "koval_backtrader_audit_v1",
                "completeness": "partial" if reasons else "complete",
                "incomplete_reasons": reasons,
                "decisions": self.decisions,
                "intents": self.intents,
                "orders": list(self.orders.values()),
                "fills": fills,
                "ledger": strategy._account.ledger_entries(),
                "account_snapshots": self.account_snapshots,
                "terminal_account": account,
                "open_position": account["open_position"],
                "events": [
                    {
                        "event_type": e.event_type.value,
                        "bar_index": e.bar_index,
                        "timestamp_ms": e.timestamp_ms,
                        "payload": e.payload,
                    }
                    for e in strategy._events
                ],
            }
        )
