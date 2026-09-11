# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline settlement and fee selection for the v2 Backtrader broker."""

from dataclasses import asdict
from decimal import Decimal

from koval.engine.fee_evidence import FeeScheduleEvidence, resolve_fee_application
from koval.engine.funding import funding_cashflow

from koval_backtrader.execution_account import IncrementalAccountLedger
from koval_backtrader.time_conversion import num2utc_ms


class EvidenceExecution:
    """Mutable run state over immutable normalized evidence."""

    def __init__(self, model, starting_balance):
        self.model = model
        self.evidence = model.execution_evidence
        self.ledger = IncrementalAccountLedger(starting_balance)
        self.funding_cursor = 0
        self.funding_entries = []
        self.fees = self.evidence.fee_schedule or FeeScheduleEvidence(
            "configured_uniform_fee",
            model.commission_bps,
            model.commission_bps,
            "quote",
            "approximation",
            "execution_model",
        )

    def fee(self, role, timestamp):
        return resolve_fee_application(self.fees, role=role, timestamp_ms=timestamp)

    def settle_funding(self, broker, data):
        series = self.evidence.funding
        if series is None:
            return
        timestamp = num2utc_ms(data.datetime[0])
        if not series.requested_start_ms <= timestamp <= series.requested_end_ms:
            raise ValueError("funding evidence does not cover the bar timestamp")
        while self.funding_cursor < len(series.records):
            record = series.records[self.funding_cursor]
            if record.settlement_timestamp_ms > timestamp:
                break
            self.funding_cursor += 1
            position = broker.positions[data]
            if not position:
                continue
            amount = float(
                funding_cashflow(
                    side="buy" if position.size > 0 else "sell",
                    quantity=Decimal(str(abs(position.size))),
                    rate=record.rate,
                    mark_price=record.settlement_mark_price,
                )
            )
            entry = self.ledger.record(
                timestamp_ms=record.settlement_timestamp_ms,
                kind="funding",
                amount=amount,
                reference_id=f"funding-{record.settlement_timestamp_ms}",
                metadata={
                    "rate": str(record.rate),
                    "mark_price": str(record.settlement_mark_price),
                    "source": record.source,
                },
            )
            self.funding_entries.append(asdict(entry))
            broker.cash += amount

    def record_fill(self, fill, *, entry_price):
        if fill["role"] == "exit":
            sign = 1 if fill["side"] == "sell" else -1
            self.ledger.record(
                timestamp_ms=fill["timestamp_ms"],
                kind="trade_pnl",
                amount=sign * fill["size"] * (fill["fill_price"] - entry_price),
                reference_id=str(fill["fill_id"]),
            )
        self.ledger.record(
            timestamp_ms=fill["timestamp_ms"],
            kind="commission",
            amount=-fill["commission"],
            reference_id=str(fill["fill_id"]),
            currency=fill["fee_currency"],
            metadata={"fee_evidence_id": fill["fee_evidence_id"]},
        )
