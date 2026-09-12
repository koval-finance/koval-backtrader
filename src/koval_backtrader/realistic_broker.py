# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract-v2 matching around Backtrader's cash and order accounting.

Only this opt-in broker activates protection during the entry-fill bar. It
retains an independent matching loop; the MIT paper broker is a test oracle,
never the implementation of a historical run.
"""

from collections import deque
from dataclasses import replace
from decimal import ROUND_DOWN, Decimal
from math import isclose, isfinite

import backtrader as bt
from koval.engine.execution_proxy import (
    BarLiquidityBudget,
    execution_timeline,
    resolve_slippage_bps,
)
from koval.engine.instrument_risk import (
    evaluate_liquidation,
    normalize_order,
    select_instrument_spec,
)
from koval.engine.paper_fills import max_quantity_for_stop_risk
from koval.engine.run_identity import content_sha256

from koval_backtrader.evidence_execution import EvidenceExecution
from koval_backtrader.execution_broker import ExecutionCostBroker
from koval_backtrader.time_conversion import num2utc_ms


class RealisticBroker(ExecutionCostBroker):
    def __init__(self):
        super().__init__()
        self.ambiguities = []
        self._data = None
        self._liquidity = None
        self._exit_bar = None
        self._protection_active_ms = None
        self._order_ids = {}
        self._last_primary_timestamp = None
        self.p.filler = lambda order, price, ago: order.info["koval_fill_quantity"]

    def start(self):
        super().start()
        self.execution = EvidenceExecution(self.p.execution_model, self.startingcash)
        self.account_ledger = self.execution.ledger
        self.funding_status = (
            "modelled" if self.execution.evidence.funding is not None else "unavailable"
        )

    def bind_data(self, data):
        self._data = data

    def next(self):
        if self._data is not None and len(self._data):
            timestamp = num2utc_ms(self._data.datetime[0])
            if timestamp == self._last_primary_timestamp:
                return
            self._last_primary_timestamp = timestamp
            if self.p.evaluation_start_ms is not None and timestamp < self.p.evaluation_start_ms:
                return
            proxy = self.execution.evidence.execution_proxy
            self._liquidity = (
                None
                if proxy is None
                else BarLiquidityBudget(
                    timestamp_ms=num2utc_ms(self._data.datetime[0]),
                    observed_volume=Decimal(str(self._data.volume[0])),
                    maximum_participation=proxy.maximum_volume_participation,
                )
            )
            self.execution.settle_funding(self, self._data)
            self._liquidate()
        priority = {"stop_loss": 0, "take_profit": 1}
        self.pending = deque(
            sorted(self.pending, key=lambda order: priority.get(order.info.get("koval_role"), 2))
        )
        super().next()

    def _ococheck(self, order):
        if order.status == bt.Order.Partial:
            if order.info.get("koval_role") in {"stop_loss", "take_profit"}:
                self._resize_protection(order.owner)
            return
        super()._ococheck(order)

    def _resize_protection(self, owner):
        quantity = abs(self.positions[owner.data].size)
        for leg in (owner._stop_order, owner._tp_order):
            if leg is not None and leg.alive():
                sign = 1 if leg.isbuy() else -1
                leg.executed.remsize = sign * quantity
                leg.created.size = leg.executed.size + sign * quantity

    def _liquidate(self):
        data = self._data
        position = self.positions[data]
        evidence = self.execution.evidence
        if not position or evidence.mark_prices is None:
            return
        timestamp = num2utc_ms(data.datetime[0])
        mark = evidence.mark_prices.at(timestamp)
        if mark is None:
            raise ValueError(f"no mark price evidence for open position at {timestamp}")
        spec = select_instrument_spec(evidence.instrument_specs, timestamp_ms=timestamp)
        state = evaluate_liquidation(
            spec,
            side="buy" if position.size > 0 else "sell",
            quantity=Decimal(str(abs(position.size))),
            entry_price=Decimal(str(position.price)),
            cash_balance=Decimal(str(self.account_ledger.balance)),
            mark_price=mark.price,
        )
        if not state.liquidated:
            return
        owner = next(
            order.owner for order in self.pending if order is not None and order.data is data
        )
        for pending in list(self.pending):
            if pending is not None and pending.data is data:
                self.cancel(pending)
        fee = float(state.notional * Decimal(str(spec.liquidation_fee_bps)) / 10000)
        submit = owner.sell if position.size > 0 else owner.buy
        order = submit(
            size=abs(position.size),
            price=float(mark.price),
            _checksubmit=False,
            koval_role="liquidation",
            koval_liquidation_fee=fee,
        )
        self.pending.remove(order)
        self._execute(order, ago=0, price=float(mark.price))

    def _adjust_price(self, order, reference):
        if order.info.get("koval_role") == "liquidation":
            return reference
        model = self.p.execution_model
        slippage = order.info.get("koval_slippage_bps", model.slippage_bps)
        adjusted = reference * (
            1 + (1 if order.isbuy() else -1) * (model.spread_bps / 2 + slippage) / 10000
        )
        if order.exectype == bt.Order.Limit and order.info.get("koval_setup") is not None:
            adjusted = (
                min(adjusted, order.created.price)
                if order.isbuy()
                else max(adjusted, order.created.price)
            )
        spec = order.info.get("koval_instrument")
        if spec is not None and order.info.get("koval_setup") is not None:
            return float(
                normalize_order(
                    spec,
                    side="buy" if order.isbuy() else "sell",
                    order_type=order.getordername().lower(),
                    quantity=Decimal(str(abs(order.executed.remsize))),
                    price=Decimal(str(adjusted)),
                ).price
            )
        return adjusted

    def _record_fill(self, order, reference, adjusted, signed_size, commission, role):
        role = "entry" if order.info.get("koval_setup") is not None else "exit"
        super()._record_fill(order, reference, adjusted, signed_size, commission, role)
        record = self.execution_fills[-1]
        # Tick rounding can improve a limit fill. Preserve the signed cash
        # effect instead of turning that benefit into a fictitious cost.
        record["price_adjustment_cost"] = signed_size * (adjusted - reference)
        fee = order.info["koval_fee"]
        record.update(
            liquidity_role=fee.liquidity_role,
            fee_rate_bps=fee.rate_bps,
            fee_currency=fee.currency,
            fee_evidence_id=fee.evidence_id,
            fee_evidence_status=fee.evidence_status,
            discount_treatment=fee.discount_treatment,
            fee_tier_id=fee.tier_id,
            commission_policy="evidence_schedule",
        )
        self.execution.record_fill(record, entry_price=order.info["koval_prior_entry"])
        if (
            self._liquidity is not None
            and order.info.get("koval_role") != "liquidation"
            and not order.info.get("koval_terminal")
        ):
            self._liquidity.allocate(
                order_id=str(order.ref), requested=Decimal(str(record["size"]))
            )
        spec = order.info.get("koval_instrument")
        if spec is None and self.execution.evidence.instrument_specs:
            spec = select_instrument_spec(
                self.execution.evidence.instrument_specs, timestamp_ms=record["timestamp_ms"]
            )
        record["instrument_evidence_id"] = None if spec is None else spec.evidence_id
        record["instrument_evidence_status"] = None if spec is None else spec.evidence_status
        impact = order.info.get("koval_impact")
        record["impact_evidence_id"] = None if impact is None else impact.evidence_id
        record["impact_model"] = None if impact is None else impact.model
        record["cost_quality"]["commission"] = (
            "approximated" if self.execution.evidence.fee_schedule else "configured"
        )
        if impact is not None and impact.evidence_id is not None:
            record["cost_quality"]["slippage"] = "approximated"
        record["evidence_refs"]["fee_schedule"] = {
            "evidence_id": fee.evidence_id,
            "sha256": content_sha256(self.execution.fees),
            "source": fee.source,
            "status": fee.evidence_status,
            "rule": "resting_entry_limit_assumed_maker_other_fills_taker",
        }
        if spec is not None:
            record["evidence_refs"]["instrument_specs"] = {
                "evidence_id": spec.evidence_id,
                "sha256": content_sha256(spec),
                "source": spec.source,
                "status": spec.evidence_status,
                "rule": "time_valid_instrument_normalization",
            }
        proxy = self.execution.evidence.execution_proxy
        if proxy is not None and not order.info.get("koval_terminal"):
            duration = order.owner.params.primary_timeframe_ms
            record["evidence_refs"]["execution_proxy"] = {
                "sha256": content_sha256(proxy),
                "status": "approximated",
                "rule": "offline_shared_completed_bar_volume_budget",
                "volume_observed_at_ms": None
                if duration is None
                else record["timestamp_ms"] + duration,
            }
        if order.info.get("koval_role") == "liquidation":
            mark = self.execution.evidence.mark_prices.at(record["timestamp_ms"])
            record["evidence_refs"]["mark_prices"] = {
                "sha256": content_sha256(mark),
                "source": mark.source,
                "rule": "single_position_cross_margin_at_mark",
            }
        liquidation_fee = order.info.get("koval_liquidation_fee", 0.0)
        record["liquidation_fee"] = liquidation_fee
        slip = order.info.get("koval_slippage_bps", self.p.execution_model.slippage_bps)
        weight = self.p.execution_model.spread_bps / 2 + slip
        record["spread_cost"] = (
            record["price_adjustment_cost"] * (self.p.execution_model.spread_bps / 2 / weight)
            if weight
            else 0.0
        )
        record["slippage_cost"] = record["price_adjustment_cost"] - record["spread_cost"]
        order_id = self._order_ids.setdefault(order.ref, len(self._order_ids) + 1)
        record.update(
            status="partial" if order.status == bt.Order.Partial else "filled",
            cumulative_quantity=abs(order.executed.size),
            order_id=f"order-{order_id}",
        )
        timeline = order.info.get("koval_timeline")
        if timeline is not None:
            record.update(
                decision_timestamp_ms=timeline.decision_timestamp_ms,
                submission_timestamp_ms=timeline.submission_timestamp_ms,
                acknowledgement_timestamp_ms=timeline.acknowledgement_timestamp_ms,
                protection_active_timestamp_ms=timeline.protection_active_timestamp_ms,
            )
        if role == "exit":
            self._exit_bar = record["timestamp_ms"]
            pending_entry = order.owner._entry_order
            if pending_entry is not None and pending_entry.alive():
                self.cancel(pending_entry)
        if liquidation_fee:
            self.cash -= liquidation_fee
            entry = self.account_ledger.record(
                timestamp_ms=record["timestamp_ms"],
                kind="liquidation_fee",
                amount=-liquidation_fee,
                reference_id=str(record["fill_id"]),
            )
            record["cashflow_sequences"].append(entry.sequence)

    def validate_entry(self, setup, size):
        values = (setup.entry_price, setup.stop_loss, setup.take_profit, size)
        if setup.entry_type not in {"market", "limit", "stop"} or setup.direction not in {
            "long",
            "short",
        }:
            raise ValueError("invalid order: unsupported entry type or direction")
        if any(value is None or not isfinite(value) or value <= 0 for value in values):
            raise ValueError("invalid order: prices and quantity must be positive and finite")
        valid = (
            (setup.stop_loss < setup.entry_price < setup.take_profit)
            if setup.direction == "long"
            else (setup.take_profit < setup.entry_price < setup.stop_loss)
        )
        if not valid:
            raise ValueError("invalid order: protection must bracket the entry")

    def submission_requirement(self, setup, size, timestamp_ms):
        rate = self.execution.fee(
            "maker" if setup.entry_type == "limit" else "taker", timestamp_ms
        ).rate_bps
        return size * setup.entry_price * (1 / self.p.execution_model.leverage + rate / 10000)

    def normalize_protection(self, owner, stop, target):
        if self.execution.evidence.instrument_specs:
            spec = select_instrument_spec(
                self.execution.evidence.instrument_specs,
                timestamp_ms=num2utc_ms(owner.data.datetime[0]),
            )
            stop, target = (
                float(
                    normalize_order(
                        spec,
                        side="sell" if owner.position.size > 0 else "buy",
                        order_type="stop",
                        quantity=Decimal(str(abs(owner.position.size))),
                        price=Decimal(str(price)),
                        reference_price=Decimal(str(owner.data.close[0])),
                    ).price
                )
                for price in (stop, target)
            )
        return stop, target

    def protection_updated(self, owner, stop, target):
        entry = owner._entry_order
        if entry is not None and entry.alive():
            entry.addinfo(
                koval_setup=replace(entry.info["koval_setup"], stop_loss=stop, take_profit=target)
            )

    def _try_exec(self, order):
        if order.info.get("koval_role") in {"stop_loss", "take_profit"}:
            self._match_protection(order.owner, allow_open_gap=True)
            return
        if order.info.get("koval_setup") is not None:
            timestamp = num2utc_ms(order.data.datetime[0])
            if self._exit_bar == timestamp:
                return
            proxy = self.execution.evidence.execution_proxy
            if proxy is not None:
                if "koval_timeline" not in order.info:
                    order.addinfo(
                        koval_timeline=execution_timeline(
                            order.info.get(
                                "koval_decision_timestamp_ms", num2utc_ms(order.created.dt)
                            ),
                            proxy.latency,
                        )
                    )
                if timestamp < order.info["koval_timeline"].fill_eligible_timestamp_ms:
                    return
            if "koval_risk_budget" not in order.info:
                setup = order.info["koval_setup"]
                order.addinfo(
                    koval_risk_budget=abs(setup.entry_price - setup.stop_loss)
                    * abs(order.created.size)
                )
            if self.execution.evidence.instrument_specs:
                try:
                    self._normalize_entry(order)
                except ValueError as exc:
                    order.addinfo(koval_rejection=f"instrument_constraint: {exc}")
                    order.reject()
                    self.notify(order)
                    return
        super()._try_exec(order)

    def _normalize_entry(self, order):
        spec = select_instrument_spec(
            self.execution.evidence.instrument_specs,
            timestamp_ms=num2utc_ms(order.data.datetime[0]),
        )
        setup = order.info["koval_setup"]
        side = "buy" if order.isbuy() else "sell"
        kwargs = dict(
            spec=spec,
            quantity=Decimal(str(abs(order.executed.remsize))),
            reference_price=Decimal(str(order.data.open[0])),
        )
        entry = normalize_order(
            **kwargs, side=side, order_type=setup.entry_type, price=Decimal(str(setup.entry_price))
        )
        kwargs["quantity"] = entry.quantity
        closing_side = "sell" if order.isbuy() else "buy"
        stop, target = (
            normalize_order(
                **kwargs, side=closing_side, order_type="stop", price=Decimal(str(price))
            )
            for price in (setup.stop_loss, setup.take_profit)
        )
        normalized = replace(
            setup,
            entry_price=float(entry.price),
            stop_loss=float(stop.price),
            take_profit=float(target.price),
            size=float(entry.quantity),
        )
        order.addinfo(koval_setup=normalized, koval_instrument=spec)
        order.created.price = float(entry.price)
        order.executed.remsize = float(entry.quantity) * (1 if order.isbuy() else -1)
        if order.owner._pending_setup is not None:
            order.owner._pending_setup = normalized

    def _execute(self, order, ago=None, price=None, cash=None, position=None, dtcoc=None):
        setup = order.info.get("koval_setup")
        actual_entry = ago is not None and price is not None and setup is not None
        actual = ago is not None and price is not None
        if actual:
            requested = abs(order.executed.remsize)
            allocated = requested
            if (
                self._liquidity is not None
                and order.info.get("koval_role") != "liquidation"
                and not order.info.get("koval_terminal")
            ):
                allocated = min(requested, float(self._liquidity.remaining))
            if (
                not actual_entry
                and self.execution.evidence.instrument_specs
                and order.info.get("koval_role") != "liquidation"
            ):
                spec = select_instrument_spec(
                    self.execution.evidence.instrument_specs,
                    timestamp_ms=num2utc_ms(order.data.datetime[0]),
                )
                step = Decimal(str(spec.step_size))
                units = Decimal(str(allocated)) / step
                nearest = units.to_integral_value()
                # Backtrader stores quantities as binary floats. Recover a
                # mathematically integral lot count within rounding noise;
                # always cap by the actual residual to avoid a reverse dust trade.
                lots = (
                    nearest
                    if isclose(float(units), float(nearest), rel_tol=0.0, abs_tol=1e-9)
                    else units.to_integral_value(rounding=ROUND_DOWN)
                )
                allocated = min(requested, float(lots * step))
            if allocated <= 0:
                return
            proxy = self.execution.evidence.execution_proxy
            timeline = order.info.get("koval_timeline")
            decision = (
                timeline.decision_timestamp_ms
                if timeline is not None and actual_entry
                else num2utc_ms(order.data.datetime[0])
            )
            volume = 0.0 if order.info.get("koval_terminal") else float(order.data.volume[0])
            slippage = resolve_slippage_bps(
                fixed_slippage_bps=Decimal(str(self.p.execution_model.slippage_bps)),
                participation=Decimal(str(min(1.0, allocated / volume)))
                if proxy is not None and volume
                else Decimal("0"),
                decision_timestamp_ms=decision,
                calibration=proxy.calibration if proxy is not None and volume else None,
            )
            order.addinfo(
                koval_slippage_bps=float(slippage.slippage_bps),
                koval_fill_quantity=allocated,
                koval_impact=slippage,
            )
            role = "maker" if actual_entry and order.exectype == bt.Order.Limit else "taker"
            fee = self.execution.fee(role, num2utc_ms(order.data.datetime[0]))
            if order.info.get("koval_role") == "liquidation":
                fee = replace(fee, rate_bps=0.0)
            order.addinfo(koval_fee=fee, koval_prior_entry=self.positions[order.data].price)
        if actual_entry:
            model = self.p.execution_model
            adjusted = self._adjust_price(order, price)
            requested = abs(order.executed.remsize)
            fraction = (model.spread_bps / 2 + float(slippage.slippage_bps)) / 10000
            risk_budget = order.info.get(
                "koval_risk_budget", abs(setup.entry_price - setup.stop_loss) * requested
            )
            current = self.positions[order.data]
            if current:
                stop_fill = setup.stop_loss * (1 + (-1 if order.isbuy() else 1) * fraction)
                entries = self.trade_fills.get(self._execution_trade_id, [])
                risk_budget -= abs(current.price - stop_fill) * abs(current.size) + sum(
                    f["commission"] for f in entries if f["role"] == "entry"
                )
                risk_budget -= (
                    abs(current.size)
                    * stop_fill
                    * self.execution.fee("taker", num2utc_ms(order.data.datetime[0])).rate_bps
                    / 10000
                )
            quantity = min(
                requested,
                max_quantity_for_stop_risk(
                    risk_budget=max(0, risk_budget),
                    entry_fill=adjusted,
                    stop_reference=setup.stop_loss,
                    position_side="buy" if order.isbuy() else "sell",
                    adjustment_fraction=fraction,
                    entry_commission_bps=fee.rate_bps,
                    exit_commission_bps=self.execution.fee(
                        "taker", num2utc_ms(order.data.datetime[0])
                    ).rate_bps,
                ),
            )
            spec = order.info.get("koval_instrument")
            if spec is not None:
                # Risk sizing and participation are caps. Quantize AFTER both,
                # otherwise a valid order can become an invalid fractional fill.
                try:
                    quantity = float(
                        normalize_order(
                            spec,
                            side="buy" if order.isbuy() else "sell",
                            order_type=setup.entry_type,
                            quantity=Decimal(str(quantity)),
                            price=Decimal(str(adjusted)),
                        ).quantity
                    )
                    allocated = float(
                        normalize_order(
                            spec,
                            side="buy" if order.isbuy() else "sell",
                            order_type=setup.entry_type,
                            quantity=Decimal(str(min(allocated, quantity))),
                            price=Decimal(str(adjusted)),
                        ).quantity
                    )
                except ValueError as exc:
                    order.addinfo(koval_rejection=f"instrument_constraint: {exc}")
                    order.reject()
                    self.notify(order)
                    return
            sign = 1 if order.isbuy() else -1
            order.executed.remsize = sign * quantity
            order.created.size = order.executed.size + sign * quantity
            order.size = order.created.size
            fill_quantity = min(allocated, quantity)
            if fill_quantity <= 1e-12:
                order.addinfo(koval_rejection="risk_budget_exhausted")
                order.reject()
                self.notify(order)
                return
            order.addinfo(koval_fill_quantity=fill_quantity)
            available = (
                self.account_ledger.balance
                + current.size * (float(order.data.close[0]) - current.price)
                - abs(current.size) * current.price / model.leverage
            )
            required = (
                fill_quantity * adjusted / model.leverage
                + fill_quantity * adjusted * fee.rate_bps / 10000
            )
            if required > available:
                order.addinfo(koval_rejection="insufficient_margin")
                order.margin()
                self.notify(order)
                return
        comminfo = self.getcommissioninfo(order.data)
        previous_rate = comminfo.p.commission
        try:
            if actual:
                comminfo.p.commission = fee.rate_bps / 10000
            result = super()._execute(order, ago, price, cash, position, dtcoc)
        finally:
            comminfo.p.commission = previous_rate
        if actual_entry and order.status in (bt.Order.Completed, bt.Order.Partial):
            owner = order.owner
            if owner._stop_order is None or not owner._stop_order.alive():
                owner._place_bracket(
                    order.executed.price,
                    abs(self.positions[order.data].size),
                    setup,
                    origin_order=order,
                )
                self._protection_active_ms = (
                    None if timeline is None else timeline.protection_active_timestamp_ms
                )
            else:
                self._resize_protection(owner)
            order.addinfo(koval_protection_placed=True)
            # A gap may already have crossed the entire requested bracket.
            # Contain at the available market reference, not a stale trigger.
            fill = order.executed.price
            is_long = order.isbuy()
            stop, target = owner._stop_order.price, owner._tp_order.price
            breach = (
                "stop_loss"
                if (fill <= stop if is_long else fill >= stop)
                else "take_profit"
                if (fill >= target if is_long else fill <= target)
                else None
            )
            self._match_protection(owner, allow_open_gap=False, breach=breach, reference=price)
            if order.alive() and proxy is not None and proxy.entry_remainder_policy == "cancel":
                order.cancel()
                self.notify(order)
        return result

    def _match_protection(self, owner, *, allow_open_gap, breach=None, reference=None):
        data = owner.data
        position = self.positions[data]
        timestamp = num2utc_ms(data.datetime[0])
        if self._exit_bar == timestamp:
            return
        if (
            breach is None
            and self._protection_active_ms is not None
            and timestamp < self._protection_active_ms
        ):
            return
        stop_order, target_order = owner._stop_order, owner._tp_order
        if not position or stop_order is None or not stop_order.alive():
            return
        is_long = position.size > 0
        open_, high, low = float(data.open[0]), float(data.high[0]), float(data.low[0])
        stop, target = stop_order.price, target_order.price
        stop_gap = open_ <= stop if is_long else open_ >= stop
        hit_stop = low <= stop if is_long else high >= stop
        hit_target = high >= target if is_long else low <= target
        selected = None
        if breach is not None:
            selected = stop_order if breach == "stop_loss" else target_order
        elif allow_open_gap and stop_gap:
            selected, reference = stop_order, open_
        elif hit_stop:
            selected, reference = stop_order, stop
            if hit_target:
                self.ambiguities.append(
                    {
                        "timestamp_ms": num2utc_ms(data.datetime[0]),
                        "reason_code": "both_protective_levels_touched",
                        "selected": "stop_loss",
                        "sensitivity": {
                            "scope": "local_full_position_before_costs",
                            "quantity": abs(position.size),
                            "stop_reference_pnl": position.size * (stop - position.price),
                            "target_reference_pnl": position.size * (target - position.price),
                        },
                    }
                )
        elif hit_target:
            target_gap = open_ >= target if is_long else open_ <= target
            selected, reference = target_order, open_ if target_gap else target
        if selected is None:
            return
        # A directly matched child may still be in the pending queue. Remove
        # it before Backtrader's OCO callback, which cancels queued siblings.
        queued = False
        try:
            self.pending.remove(selected)
            queued = True
        except ValueError:
            pass
        self._execute(selected, ago=0, price=reference)
        if queued and selected.alive():
            self.pending.append(selected)
