# SPDX-License-Identifier: GPL-3.0-or-later
"""Strict transport of the engine's normalized, offline execution evidence."""

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal, InvalidOperation
from types import UnionType
from typing import get_args, get_origin, get_type_hints

from koval.engine.execution_proxy import ExecutionProxyConfig
from koval.engine.fee_evidence import FeeScheduleEvidence, resolve_fee_application
from koval.engine.funding import FundingSeries, build_funding_series
from koval.engine.instrument_risk import (
    InstrumentSpecEvidence,
    MarkPriceSeries,
    build_mark_price_series,
    select_instrument_spec,
)
from koval.engine.run_identity import execution_evidence_manifest

EVIDENCE_KEYS = frozenset(
    {"funding", "fee_schedule", "instrument_specs", "mark_prices", "execution_proxy"}
)


def evidence_json(value):
    """Replayable normalized JSON; raw responses are archived by the caller."""
    if is_dataclass(value):
        return {
            f.name: evidence_json(getattr(value, f.name))
            for f in fields(value)
            if f.init and f.name != "raw_responses"
        }
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (tuple, list)):
        return [evidence_json(item) for item in value]
    return value


def _decode(annotation, value):
    if value is None:
        if type(None) in get_args(annotation):
            return None
        raise ValueError("execution evidence has an unexpected null")
    if get_origin(annotation) is UnionType:
        annotation = next(t for t in get_args(annotation) if t is not type(None))
    if get_origin(annotation) is tuple:
        if not isinstance(value, (tuple, list)):
            raise ValueError("execution evidence collection must be a list or tuple")
        return tuple(_decode(get_args(annotation)[0], item) for item in value)
    if is_dataclass(annotation):
        if isinstance(value, annotation):
            value = {
                f.name: getattr(value, f.name)
                for f in fields(value)
                if f.init and f.name != "raw_responses"
            }
        if not isinstance(value, Mapping):
            raise ValueError(f"execution evidence requires {annotation.__name__} or a mapping")
        allowed = {f.name for f in fields(annotation) if f.init and f.name != "raw_responses"}
        if set(value) - allowed:
            raise ValueError("execution evidence contains unknown fields")
        hints = get_type_hints(annotation)
        try:
            return annotation(**{key: _decode(hints[key], item) for key, item in value.items()})
        except TypeError as exc:
            raise ValueError(f"invalid {annotation.__name__}: {exc}") from exc
    if annotation is Decimal:
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("execution evidence requires a valid Decimal") from exc
    if annotation in {bool, int, str} and type(value) is not annotation:
        raise ValueError(f"execution evidence requires {annotation.__name__}")
    if annotation is float and (isinstance(value, bool) or not isinstance(value, (float, int))):
        raise ValueError("execution evidence requires a number")
    return value


@dataclass(frozen=True)
class ExecutionEvidence:
    funding: FundingSeries | None = None
    fee_schedule: FeeScheduleEvidence | None = None
    instrument_specs: tuple[InstrumentSpecEvidence, ...] = ()
    mark_prices: MarkPriceSeries | None = None
    execution_proxy: ExecutionProxyConfig | None = None

    def as_kwargs(self):
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def as_config(self):
        return {name: evidence_json(value) for name, value in self.as_kwargs().items() if value}

    def manifest(self):
        return execution_evidence_manifest(**self.as_kwargs())


def resolve_execution_evidence(config, *, realistic, market) -> ExecutionEvidence:
    supplied = {key: config[key] for key in EVIDENCE_KEYS if key in config}
    if supplied and not realistic:
        raise ValueError("execution evidence requires ohlcv_realistic_v2")
    evidence = _decode(ExecutionEvidence, supplied)
    if not supplied:
        return evidence
    if market is None:
        raise ValueError("execution evidence requires an explicit market identity")
    funding, marks = evidence.funding, evidence.mark_prices
    fee = evidence.fee_schedule
    if fee is not None:
        for name, expected in (
            ("exchange", market.exchange),
            ("market", market.market),
            ("canonical_symbol", market.canonical_symbol),
        ):
            actual = getattr(fee, name, None)
            if actual is not None and actual != expected:
                raise ValueError(f"fee evidence {name} mismatch")
    for item in (funding, marks, *evidence.instrument_specs):
        if item is None:
            continue
        if item.exchange != market.exchange:
            raise ValueError("execution evidence exchange mismatch")
        if item.canonical_symbol != market.canonical_symbol:
            raise ValueError("execution evidence symbol mismatch")
        if hasattr(item, "market") and item.market != market.market:
            raise ValueError("execution evidence market mismatch")
    if funding is not None:
        if not funding.coverage_complete or market.market != "future":
            raise ValueError("funding requires complete perpetual coverage")
        if funding.records:
            checked = build_funding_series(
                funding.records,
                exchange=funding.exchange,
                market=funding.market,
                symbol=funding.canonical_symbol,
                requested_start_ms=funding.requested_start_ms,
                requested_end_ms=funding.requested_end_ms,
            )
            if checked.records != funding.records:
                raise ValueError("funding records must be chronological")
            for record in funding.records:
                if (
                    not funding.requested_start_ms
                    <= record.settlement_timestamp_ms
                    <= funding.requested_end_ms
                ):
                    raise ValueError("funding settlement is outside its coverage interval")
                if (
                    record.symbol.replace("/", "").replace("_", "").upper()
                    != market.canonical_symbol
                ):
                    raise ValueError("funding record symbol mismatch")
    if marks is not None:
        if (
            not marks.coverage_complete
            or not evidence.instrument_specs
            or market.market != "future"
        ):
            raise ValueError(
                "mark-price liquidation requires complete coverage and perpetual instrument specs"
            )
        build_mark_price_series(
            marks.records,
            exchange=marks.exchange,
            symbol=marks.canonical_symbol,
            interval_ms=marks.interval_ms,
            requested_start_ms=marks.requested_start_ms,
            requested_end_ms=marks.requested_end_ms,
        )
    for spec in evidence.instrument_specs:
        if Decimal(str(spec.contract_size)) != 1:
            raise ValueError("base-quantity accounting requires contract_size == 1")
    proxy = evidence.execution_proxy
    if proxy is not None and (proxy.latency.cancellation_ms or proxy.latency.replacement_ms):
        raise ValueError("nonzero cancellation and replacement latency is unsupported")
    quote = next(
        (
            c
            for c in ("FDUSD", "USDT", "USDC", "TUSD", "BUSD", "BTC", "ETH", "EUR", "USD")
            if market.canonical_symbol.endswith(c)
        ),
        None,
    )
    if market.exchange == "whitebit" and market.canonical_symbol.endswith("PERP"):
        quote = "USDT"
    for spec in evidence.instrument_specs:
        if spec.collateral_currency != quote:
            raise ValueError("linear accounting requires quote collateral")
    fee = evidence.fee_schedule
    if fee is not None and fee.currency not in {"quote", quote}:
        raise ValueError("fee currency must match the instrument quote currency")
    return evidence


def validate_evidence_coverage(evidence, timestamps):
    """Reject unusable declared coverage before a session emits any decisions."""
    funding = evidence.funding
    for timestamp in timestamps:
        timestamp = int(timestamp)
        if (
            funding is not None
            and not funding.requested_start_ms <= timestamp <= funding.requested_end_ms
        ):
            raise ValueError("funding evidence does not cover the evaluation interval")
        if evidence.fee_schedule is not None:
            resolve_fee_application(evidence.fee_schedule, role="taker", timestamp_ms=timestamp)
        if evidence.instrument_specs:
            select_instrument_spec(evidence.instrument_specs, timestamp_ms=timestamp)
        if evidence.mark_prices is not None and evidence.mark_prices.at(timestamp) is None:
            raise ValueError("mark price evidence must cover every primary bar")
