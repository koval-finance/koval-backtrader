# SPDX-License-Identifier: GPL-3.0-or-later
"""Versioned execution assumptions, independent of Backtrader wiring."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from koval.engine.execution_settings import resolve_execution_settings

from koval_backtrader.execution_evidence import (
    EVIDENCE_KEYS,
    ExecutionEvidence,
    resolve_execution_evidence,
)
from koval_backtrader.market_identity import (
    MarketIdentity,
    market_constraints,
    resolve_market_identity,
    validate_against_labels,
    validate_against_model,
)
from koval_backtrader.run_identity import EvidenceIdentity, resolve_evidence_identity

LEGACY_VERSION = "legacy_v1"
FIXED_VERSION = "ohlcv_fixed_v1"
REALISTIC_VERSION = "ohlcv_realistic_v2"
COSTED_VERSIONS = (FIXED_VERSION, REALISTIC_VERSION)


MIN_LEVERAGE = 1.0
MAX_LEVERAGE = 125.0


@dataclass(frozen=True)
class ExecutionModel:
    version: str
    exchange: str
    exchange_type: str
    commission_bps: float
    spread_bps: float = 0.0
    slippage_bps: float = 0.0
    leverage: float = 1.0
    market: MarketIdentity | None = None
    evidence: EvidenceIdentity | None = None
    execution_evidence: ExecutionEvidence = field(default_factory=ExecutionEvidence)

    @property
    def market_constraints(self) -> str:
        return market_constraints(self.market)

    def as_config(self) -> dict:
        model = {"version": self.version, "commission_bps": self.commission_bps}
        if self.version in COSTED_VERSIONS:
            model.update(
                spread_bps=self.spread_bps,
                slippage_bps=self.slippage_bps,
                leverage=self.leverage,
            )
        config = {
            "exchange": self.exchange,
            "exchange_type": self.exchange_type,
            "execution_model": model,
        }
        # Optional blocks are omitted rather than emitted as null, so a
        # resolved config can always be replayed verbatim.
        if self.market is not None:
            config["market"] = self.market.as_dict()
        if self.evidence is not None:
            config["evidence"] = self.evidence.as_dict()
        config.update(self.execution_evidence.as_config())
        return config


def _cost_bps(model: Mapping, name: str) -> float:
    value = model[name]
    message = f"execution_model.{name} must be a finite non-negative number below 10000"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)
    try:
        parsed = float(value)
    except OverflowError as exc:
        raise ValueError(message) from exc
    if not math.isfinite(parsed) or not 0 <= parsed < 10_000:
        raise ValueError(message)
    return parsed


def _leverage(model: Mapping) -> float:
    value = model.get("leverage", MIN_LEVERAGE)
    message = (
        f"execution_model.leverage must be a finite number in [{MIN_LEVERAGE}, {MAX_LEVERAGE}]"
    )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)
    try:
        parsed = float(value)
    except OverflowError as exc:
        raise ValueError(message) from exc
    if not math.isfinite(parsed) or not MIN_LEVERAGE <= parsed <= MAX_LEVERAGE:
        raise ValueError(message)
    return parsed


def _validate_legacy_config(config: Mapping) -> None:
    numeric = {"commission", "taker_fee", "maker_fee_bps", "taker_fee_bps", "broker_commission_bps"}
    known = numeric | {
        "exchange",
        "exchange_type",
        "execution_mode",
        "fee_source",
        "paper_commission_side",
    }
    if set(config) - known:
        raise ValueError("execution_config contains unknown fields; costs require execution_model")
    for name in numeric & config.keys():
        value = config[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"execution_config.{name} must be finite and non-negative")
        try:
            upper_bound = {"commission": 1, "taker_fee": 100}.get(name, 10_000)
            valid = math.isfinite(value) and 0 <= value < upper_bound
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError(f"execution_config.{name} must be finite, non-negative and below 100%")
    if "execution_mode" in config and config["execution_mode"] not in ("paper", "binance_sandbox"):
        raise ValueError("execution_config.execution_mode must be paper or binance_sandbox")
    for name in ("exchange", "exchange_type"):
        if name in config and (not isinstance(config[name], str) or not config[name].strip()):
            raise ValueError(f"execution_config.{name} must be a non-empty string")
    if "exchange_type" in config and config["exchange_type"].strip().lower() not in (
        "spot",
        "future",
        "futures",
        "usdm",
        "usd_m",
    ):
        raise ValueError("execution_config.exchange_type is unsupported")
    if "fee_source" in config and config["fee_source"] not in (
        "",
        "exchange_default",
        "config_override",
        "binance_api",
        "legacy_commission",
        "legacy_taker_fee",
    ):
        raise ValueError("execution_config.fee_source is unsupported")
    if "paper_commission_side" in config and config["paper_commission_side"] != "taker":
        raise ValueError("execution_config.paper_commission_side must be taker")


def resolve_execution_model(config: Mapping | None) -> ExecutionModel:
    """Old dictionaries retain engine resolution; explicit versions are strict.

    The returned config freezes the effective commission even when the original
    request depended on an engine defaults table. Versioned requests never
    consult that table.
    """
    if config is None or (isinstance(config, Mapping) and not config):
        return ExecutionModel(LEGACY_VERSION, "unspecified", "unspecified", 0.0)
    if not isinstance(config, Mapping):
        raise ValueError("execution_config must be a mapping")
    if "execution_model" not in config:
        _validate_legacy_config(config)
        settings = resolve_execution_settings(config)
        return ExecutionModel(
            LEGACY_VERSION,
            settings.exchange,
            settings.exchange_type,
            settings.broker_commission_bps,
        )

    # Market/provenance are optional for old requests. Execution evidence is
    # opt-in v2 input and must never silently alter a fixed-v1 replay.
    required = {"exchange", "exchange_type", "execution_model"}
    optional_blocks = {"market", "evidence"} | EVIDENCE_KEYS
    if not required <= set(config) <= required | optional_blocks:
        raise ValueError(
            "execution_config requires only exchange, exchange_type, execution_model "
            "and optionally market, evidence, funding, fee_schedule, "
            "instrument_specs, mark_prices, execution_proxy"
        )
    for name in ("exchange", "exchange_type"):
        if not isinstance(config[name], str) or not config[name].strip():
            raise ValueError(f"execution_config.{name} must be a non-empty string")
    model = config["execution_model"]
    if not isinstance(model, Mapping):
        raise ValueError("execution_model must be a mapping")
    version = model.get("version")
    if version not in (LEGACY_VERSION, *COSTED_VERSIONS):
        raise ValueError(
            "execution_model.version must be legacy_v1, ohlcv_fixed_v1 or ohlcv_realistic_v2"
        )
    if config["exchange_type"] not in ("spot", "future", "futures", "usdm", "usd_m", "unspecified"):
        raise ValueError("execution_config.exchange_type is unsupported")
    expected = {"version", "commission_bps"}
    optional: set[str] = set()
    if version in COSTED_VERSIONS:
        expected |= {"spread_bps", "slippage_bps"}
        optional = {"leverage"}
        if config["exchange_type"] not in ("spot", "future"):
            raise ValueError("execution_config.exchange_type must be spot or future")
    if not expected <= set(model) <= expected | optional:
        raise ValueError(
            f"execution_model requires exactly {', '.join(sorted(expected))}"
            + (f" and optionally {', '.join(sorted(optional))}" if optional else "")
        )
    costs = {key: _cost_bps(model, key) for key in expected - {"version"}}
    if costs.get("spread_bps", 0) / 2 + costs.get("slippage_bps", 0) >= 10_000:
        raise ValueError("execution_model half spread plus slippage must be below 10000 bps")
    leverage = _leverage(model) if version in COSTED_VERSIONS else MIN_LEVERAGE
    market = resolve_market_identity(config.get("market"))
    validate_against_labels(
        market, exchange=config["exchange"], exchange_type=config["exchange_type"]
    )
    validate_against_model(market, leverage=leverage)
    return ExecutionModel(
        version,
        config["exchange"],
        config["exchange_type"],
        leverage=leverage,
        market=market,
        evidence=resolve_evidence_identity(config.get("evidence")),
        execution_evidence=resolve_execution_evidence(
            config, realistic=version == REALISTIC_VERSION, market=market
        ),
        **costs,
    )
