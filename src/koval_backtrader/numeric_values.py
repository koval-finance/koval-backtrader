# SPDX-License-Identifier: GPL-3.0-or-later
"""Narrow historical numeric admission; all value mathematics belongs to MIT."""

from importlib import import_module

import numpy as np
from koval.engine.backtest_engine import ProtocolVersionError
from koval.engine.history_window import DEFAULT_HISTORY_BARS

from koval_backtrader.position_exit import supports_position_features

CAPABILITY = "numeric_values_v1"
NODE_TYPES = frozenset(
    {"source.value", "fact.compare", "fact.cross", "fact.distance", "policy.from_condition"}
)
TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")


def numeric_requested(spec):
    return CAPABILITY in spec.required_execution_capabilities or any(
        block.get("type") in NODE_TYPES for block in spec.graph.get("blocks", [])
    )


def _engine_contract():
    # Same-version public engines lack these modules. Never import them at plugin load.
    try:
        values = import_module("koval.strategy.graph.values")
        entities = import_module("koval.strategy.graph.entities")
        if (
            values.VALUE_CAPABILITY == CAPABILITY
            and values.VALUE_FORMULA_VERSION == "numeric_value_v1"
            and callable(values.value_at)
            and callable(values.graph_required_history_bars)
            and hasattr(entities, "NumericValueV1")
            and hasattr(entities, "ConditionV1")
        ):
            return values
    except (ImportError, AttributeError):
        pass
    return None


def capabilities():
    available = _engine_contract() is not None
    return {
        "schema_version": "koval_backtrader_numeric_capabilities_v1",
        "capability": CAPABILITY,
        "available": available,
        "reason": None if available else "missing_engine_numeric_contract",
        "availability_model": "historical_ideal_close_v1",
        "supported_modes": ["historical"],
        "supported_profiles": ["ohlcv_realistic_v2"],
        "supported_market": {
            "exchange": "binance",
            "market": "spot",
            "leverage": 1,
            "direction": "long",
            "funding": "cash",
        },
        "supported_timeframes": list(TIMEFRAMES),
        "history_bars": DEFAULT_HISTORY_BARS,
        "feed_count": 1,
        "timestamp_policy": "aligned_contiguous_integer_open_ms",
        "adapter_resume_supported": False,
    }


def run_contract():
    """Use the existing loaded-package byte identity, distinct from version labels."""
    from koval_backtrader.spot_identity import source_identity

    return {
        **capabilities(),
        "formula_version": "numeric_value_v1",
        "engine_source_sha256": source_identity()["koval-engine"]["source_sha256"],
        "source_encoding": "relative_path_nul_bytes_nul_py_json_checksums_v1",
    }


def admit_numeric(spec, model):
    """Validate before strategy/feed/broker construction, including inferred requests."""
    if not numeric_requested(spec):
        return False
    contract = _engine_contract()
    if contract is None:
        raise ProtocolVersionError(
            f"engine is missing required execution capabilities: {CAPABILITY} "
            "(missing_engine_numeric_contract)"
        )
    if (
        not supports_position_features(model)
        or len(spec.feeds) != 1
        or next(iter(spec.feeds), None) not in TIMEFRAMES
    ):
        raise ProtocolVersionError(
            f"{CAPABILITY} requires one Binance Spot cash long-only 1x feed, "
            "ohlcv_realistic_v2 and a supported primary timeframe"
        )
    required = contract.graph_required_history_bars(spec.graph)
    if required > DEFAULT_HISTORY_BARS:
        raise ValueError(
            f"numeric graph requires {required} history bars; host budget is {DEFAULT_HISTORY_BARS}"
        )
    # Fixed-input validation owns shape, finite prices/volume and increasing integer clocks.
    # Inspect every timestamp here as BarContext carries only the span endpoints.
    timeframe, rows = next(iter(spec.feeds.items()))
    from koval.engine.timeframe_utils import timeframe_to_minutes

    step = timeframe_to_minutes(timeframe) * 60_000
    timestamps = rows[:, 0]
    if np.any(timestamps % step != 0) or np.any(np.diff(timestamps) != step):
        raise ValueError("numeric timestamps must be aligned and contiguous primary candle opens")
    return True
