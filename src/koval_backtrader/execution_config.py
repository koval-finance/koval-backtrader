# SPDX-License-Identifier: GPL-3.0-or-later
"""Versioned execution assumptions, independent of Backtrader wiring."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from koval.engine.execution_settings import resolve_execution_settings

LEGACY_VERSION = "legacy_v1"
FIXED_VERSION = "ohlcv_fixed_v1"


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

    def as_config(self) -> dict:
        model = {"version": self.version, "commission_bps": self.commission_bps}
        if self.version == FIXED_VERSION:
            model.update(
                spread_bps=self.spread_bps,
                slippage_bps=self.slippage_bps,
                leverage=self.leverage,
            )
        return {
            "exchange": self.exchange,
            "exchange_type": self.exchange_type,
            "execution_model": model,
        }


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

    if set(config) != {"exchange", "exchange_type", "execution_model"}:
        raise ValueError("execution_config requires only exchange, exchange_type, execution_model")
    for name in ("exchange", "exchange_type"):
        if not isinstance(config[name], str) or not config[name].strip():
            raise ValueError(f"execution_config.{name} must be a non-empty string")
    model = config["execution_model"]
    if not isinstance(model, Mapping):
        raise ValueError("execution_model must be a mapping")
    version = model.get("version")
    if version not in (LEGACY_VERSION, FIXED_VERSION):
        raise ValueError("execution_model.version must be legacy_v1 or ohlcv_fixed_v1")
    if config["exchange_type"] not in ("spot", "future", "futures", "usdm", "usd_m", "unspecified"):
        raise ValueError("execution_config.exchange_type is unsupported")
    expected = {"version", "commission_bps"}
    optional: set[str] = set()
    if version == FIXED_VERSION:
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
    leverage = _leverage(model) if version == FIXED_VERSION else MIN_LEVERAGE
    return ExecutionModel(
        version, config["exchange"], config["exchange_type"], leverage=leverage, **costs
    )
