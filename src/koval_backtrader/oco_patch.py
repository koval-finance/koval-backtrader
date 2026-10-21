# SPDX-License-Identifier: GPL-3.0-or-later
# Koval Backtrader Adapter - OCO guard patch
# Fixes Backtrader ghost-trade bug: same-bar TP+SL double-fill.

"""
OCO (One-Cancels-Other) guard patch for Backtrader BackBroker.

Fixes the ghost-trade bug: when both TP and SL are eligible in the same bar,
Backtrader's _ococheck runs after _try_exec, so both orders can fill before
cancellations propagate. This patch:

1. Tracks OCO groups that have already completed this bar (_oco_done).
2. Before _try_exec: if the order's OCO group is in _oco_done, cancel immediately.
3. In _ococheck: add ocoref to _oco_done as soon as one order completes.
4. cancel(): also remove from submitted (not just pending) - fixes "hanging" orders
   when trailing/breakeven runs same-bar as entry fill while orders are still Submitted.
"""

from collections import deque

from backtrader.brokers import bbroker
from backtrader.order import Order


def _get_ocoref(broker, order):
    """Get OCO group reference for an order. Returns None if not in OCO."""
    oref = order.ref
    if oref not in broker._ocos:
        return None
    parentref = broker._ocos[oref]
    return broker._ocos.get(parentref, parentref)


def _patched_ococheck(self, order):
    """Original _ococheck with ocoref added to _oco_done before cancelling siblings."""
    parentref = self._ocos.get(order.ref)
    if parentref is None:
        return
    ocoref = self._ocos.get(parentref, parentref)
    ocol = self._ocol.pop(ocoref, None)

    # Mark this OCO group as done BEFORE processing - prevents same-bar double fill
    oco_done = getattr(self, "_oco_done", None)
    if oco_done is not None:
        oco_done.add(ocoref)

    if ocol:
        for i in range(len(self.pending) - 1, -1, -1):
            o = self.pending[i]
            if o is not None and o.ref in ocol:
                del self.pending[i]
                o.cancel()
                self.notify(o)


def _opening_protection_priority(order):
    """Known opening executions precede ambiguous touches inside one OCO pair."""
    opening = getattr(order.data, "tick_open", None)
    if opening is None:
        opening = order.data.open[0]
    trigger = order.created.price
    if order.exectype == Order.Stop:
        crossed = opening >= trigger if order.isbuy() else opening <= trigger
        return 0 if crossed else 2
    if order.exectype == Order.Limit:
        crossed = opening <= trigger if order.isbuy() else opening >= trigger
        return 1 if crossed else 2
    return 2


def _prioritize_opening_protection(broker):
    """Reorder only existing active protection within its own queued OCO slots."""
    pending = list(broker.pending)
    groups = {}
    for index, order in enumerate(pending):
        if (
            not order.alive()
            or not order.active()
            or order.created.dt >= order.data.datetime[0]
            or order.info.get("koval_role") not in {"stop_loss", "take_profit"}
        ):
            continue
        group = _get_ocoref(broker, order)
        if group is not None:
            groups.setdefault(group, []).append(index)
    for indices in groups.values():
        orders = sorted((pending[i] for i in indices), key=_opening_protection_priority)
        for index, order in zip(indices, orders, strict=True):
            pending[index] = order
    broker.pending = deque(pending)


def _patched_next(self):
    """Original BackBroker.next with OCO guard before _try_exec."""
    # Reset OCO-done set each bar
    self._oco_done = set()

    while self._toactivate:
        self._toactivate.popleft().activate()

    if self.p.checksubmit:
        self.check_submitted()

    # Discount any cash for positions hold
    credit = 0.0
    for data, pos in self.positions.items():
        if pos:
            comminfo = self.getcommissioninfo(data)
            dt0 = data.datetime.datetime()
            dcredit = comminfo.get_credit_interest(data, pos, dt0)
            self.d_credit[data] += dcredit
            credit += dcredit
            pos.datetime = dt0
    self.cash -= credit

    self._process_order_history()
    _prioritize_opening_protection(self)

    # Iterate once over all elements of the pending queue
    self.pending.append(None)
    while True:
        order = self.pending.popleft()
        if order is None:
            break

        if order.expire():
            self.notify(order)
            self._ococheck(order)
            self._bracketize(order, cancel=True)

        elif not order.active():
            self.pending.append(order)

        else:
            # OCO guard: if this order's group already completed this bar, cancel and skip
            ocoref = _get_ocoref(self, order)
            if ocoref is not None and ocoref in self._oco_done:
                order.cancel()
                self.notify(order)
                self._ococheck(order)
                continue

            self._try_exec(order)
            if order.alive():
                self.pending.append(order)

        if order.status == Order.Completed:
            self._bracketize(order)

    # Operations have been executed ... adjust cash end of bar
    for data, pos in self.positions.items():
        if pos:
            comminfo = self.getcommissioninfo(data)
            self.cash += comminfo.cashadjust(pos.size, pos.adjbase, data.close[0])
            pos.adjbase = data.close[0]

    self._get_value()


def _patched_cancel(self, order, bracket=False):
    """Cancel order from pending or submitted (original only removes from pending)."""
    removed = False
    try:
        self.pending.remove(order)
        removed = True
    except ValueError:
        pass
    if not removed:
        try:
            self.submitted.remove(order)
            removed = True
        except ValueError:
            pass
    if not removed:
        return False
    order.cancel()
    self.notify(order)
    self._ococheck(order)
    if not bracket:
        self._bracketize(order, cancel=True)
    return True


def apply_oco_guard():
    """Apply OCO guard patch to BackBroker. Call before creating Cerebro."""
    bbroker.BackBroker._ococheck = _patched_ococheck
    bbroker.BackBroker.next = _patched_next
    bbroker.BackBroker.cancel = _patched_cancel
