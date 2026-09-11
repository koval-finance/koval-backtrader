# SPDX-License-Identifier: GPL-3.0-or-later
"""Bind a result to the exact inputs that produced it.

A version string does not identify a run. Two backtests of "the same" 1h
history can differ by one revised candle and produce different equity while
carrying identical metadata, and the reader has no way to tell. So a result
records fingerprints of the graph and of every candle it saw, the warm-up
bounds it used, the software that ran it, and whatever provenance the caller
was able to assert.

Just as important is the negative case: a run that *cannot* prove its inputs
is graded ``partial`` and lists why, so an old unversioned result is never
presented as reproducible.

Hashes here identify bytes. They are not evidence that the bytes are correct —
the caller still owns whether those candles are the real history.
"""

from __future__ import annotations

import json
import platform
from collections.abc import Mapping
from dataclasses import dataclass, fields
from hashlib import sha256
from importlib.metadata import version

import numpy as np
from koval.engine.paper_profile import resolve_paper_profile
from koval.engine.run_identity import (
    CandleStreamIdentity,
    content_sha256,
)
from koval.engine.run_identity import (
    build_run_identity as engine_run_identity,
)

_HEX = frozenset("0123456789abcdef")
_SOFTWARE = ("koval-backtrader", "koval-engine", "backtrader", "numpy", "pandas")


@dataclass(frozen=True)
class EvidenceIdentity:
    """The archived dataset a caller asserts this run replayed."""

    dataset_id: str
    source: str
    retrieved_at_ms: int
    content_sha256: str

    def as_dict(self) -> dict:
        return {field.name: getattr(self, field.name) for field in fields(self)}


def _text(block: Mapping, name: str) -> str:
    value = block[name]
    if isinstance(value, bool) or not isinstance(value, str) or not value.strip():
        raise ValueError(f"evidence.{name} must be a non-empty string")
    return value.strip()


def _digest(block: Mapping, name: str) -> str:
    value = block[name]
    if (
        isinstance(value, bool)
        or not isinstance(value, str)
        or len(value) != 64
        or not set(value) <= _HEX
    ):
        raise ValueError(f"evidence.{name} must be a lowercase hex sha256 digest")
    return value


def _epoch_ms(block: Mapping, name: str) -> int:
    value = block[name]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"evidence.{name} must be a positive integer millisecond timestamp")
    return value


def resolve_evidence_identity(evidence: Mapping | None) -> EvidenceIdentity | None:
    """Validate a dataset identity, or return None when the caller omitted it.

    All-or-nothing on purpose: a block with three of four fields filled in reads
    as provenance while proving less than an empty one.
    """
    if evidence is None:
        return None
    if not isinstance(evidence, Mapping):
        raise ValueError("evidence must be a mapping")
    expected = {field.name for field in fields(EvidenceIdentity)}
    missing = sorted(expected - set(evidence))
    if missing:
        raise ValueError(f"evidence requires {', '.join(missing)}")
    unknown = sorted(set(evidence) - expected)
    if unknown:
        raise ValueError(f"evidence contains unknown fields: {', '.join(unknown)}")
    return EvidenceIdentity(
        dataset_id=_text(evidence, "dataset_id"),
        source=_text(evidence, "source"),
        retrieved_at_ms=_epoch_ms(evidence, "retrieved_at_ms"),
        content_sha256=_digest(evidence, "content_sha256"),
    )


def graph_sha256(graph: Mapping | None) -> str:
    """Hash a graph by its content, not by how a serializer ordered its keys."""
    return content_sha256(graph or {})


def _feed_sha256(candles: np.ndarray) -> str:
    # float64 big-endian, C-order: the same candles hash the same regardless of
    # the caller's platform or how the array was built.
    return sha256(np.ascontiguousarray(candles, dtype=">f8").tobytes()).hexdigest()


def feed_fingerprints(feeds: Mapping[str, np.ndarray], durations: Mapping[str, int | None]) -> dict:
    """One record per timeframe: content hash, bar count and observed bounds."""
    fingerprints = {}
    for timeframe, candles in feeds.items():
        array = np.asarray(candles, dtype=float)
        fingerprints[timeframe] = {
            "sha256": _feed_sha256(array),
            "bars": int(array.shape[0]),
            "first_timestamp_ms": int(array[0][0]) if array.shape[0] else None,
            "last_timestamp_ms": int(array[-1][0]) if array.shape[0] else None,
            "timeframe_ms": durations.get(timeframe),
        }
    return fingerprints


def evidence_set_sha256(fingerprints: Mapping[str, dict]) -> str:
    """One hash over every feed, so a single value identifies the whole input."""
    canonical = json.dumps(
        {timeframe: record["sha256"] for timeframe, record in fingerprints.items()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode()).hexdigest()


def _grade(
    *,
    versioned: bool,
    market_present: bool,
    evidence_present: bool,
    unresolved_timeframes: list[str],
) -> dict:
    reasons = []
    if not versioned:
        reasons.append("unversioned_execution_model")
    if not market_present:
        reasons.append("market_identity_absent")
    if not evidence_present:
        reasons.append("dataset_evidence_absent")
    if unresolved_timeframes:
        reasons.append("timeframe_duration_unresolved")
    return {"level": "partial", "reasons": reasons}


def build_run_identity(
    *,
    graph,
    feeds,
    durations,
    ordered_timeframes,
    history_bars,
    model,
    implementation_sha256,
    initial_capital,
    consumed_bars,
) -> dict:
    """Everything needed to prove which inputs produced this result."""
    fingerprints = feed_fingerprints(feeds, durations)
    unresolved = sorted(tf for tf, record in fingerprints.items() if record["timeframe_ms"] is None)
    identity = {
        "graph_sha256": graph_sha256(graph),
        "feeds": fingerprints,
        "evidence_set_sha256": evidence_set_sha256(fingerprints),
        "execution_model_version": model.version,
        "market": None if model.market is None else model.market.as_dict(),
        "evidence": None if model.evidence is None else model.evidence.as_dict(),
        "warmup": {
            "history_bars": int(history_bars),
            "primary_timeframe": ordered_timeframes[0] if ordered_timeframes else None,
            "timeframes": list(ordered_timeframes),
        },
        "plugin_sha256": implementation_sha256,
        "software": {
            **{name: version(name) for name in _SOFTWARE},
            "python": platform.python_version(),
        },
    }
    timeframe = ordered_timeframes[0]
    consumed = feeds[timeframe][:consumed_bars]
    canonical = True
    try:
        primary = CandleStreamIdentity(timeframe)
        warmup = CandleStreamIdentity(timeframe)
        for row in consumed:
            primary.append(row)
        primary_record, warmup_record = primary.as_dict(), warmup.as_dict()
    except ValueError:
        # v1 accepted irregular labels and timestamps. Preserve replay while
        # preventing an incompatible encoding from being compared with paper.
        canonical = False
        primary_record = {
            "encoding_version": "koval_plugin_f64be_v1",
            "sha256": _feed_sha256(consumed),
            "row_count": len(consumed),
            "timeframe": timeframe,
        }
        warmup_record = {"encoding_version": "unavailable", "row_count": 0}
    if model.version == "legacy_v1":
        profile = model.as_config()["execution_model"]
    else:
        profile = resolve_paper_profile(
            {
                **model.as_config()["execution_model"],
                "version": "paper_" + model.version,
            }
        ).as_config()
    shared = engine_run_identity(
        market_identity=model.market if canonical and model.version != "legacy_v1" else None,
        primary=primary_record,
        warmup=warmup_record,
        execution_profile=profile,
        execution_evidence=model.execution_evidence.manifest(),
        strategy_sha256=graph_sha256(graph),
        run_parameters={
            "initial_capital": float(initial_capital),
            "max_window": int(history_bars),
            "higher_timeframe": ordered_timeframes[1] if len(ordered_timeframes) > 1 else None,
            "requires_higher_timeframe": len(ordered_timeframes) > 1,
            "daily_baseline_equity": None,
            "peak_equity": None,
            "end_of_data_policy": "mark_at_last_close",
        },
        engine_version=version("koval-engine"),
        execution_mode="paper",
    )
    # `paper` names the simulated execution mode. This producer is identified
    # separately, along with the paired plugin profile and source fingerprint.
    shared["producer"] = {"name": "koval-backtrader", "version": version("koval-backtrader")}
    identity.update(shared)
    identity["reproducibility"] = _grade(
        versioned=model.version != "legacy_v1",
        market_present=model.market is not None,
        evidence_present=model.evidence is not None,
        unresolved_timeframes=unresolved,
    )
    return identity
