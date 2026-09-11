# SPDX-License-Identifier: GPL-3.0-or-later
"""A result must name the exact inputs that produced it.

Reproducing a backtest offline means knowing which graph, which candles, which
software and which market the numbers came from. Version strings alone do not
establish that: two runs of "the same" 1h BTCUSDT history can differ by a
revised candle and produce different equity with identical metadata.

These tests pin the fingerprints a result carries and, just as importantly,
pin that a run which *cannot* prove its inputs says so rather than looking
reproducible.
"""

from __future__ import annotations

import copy
import json

import numpy as np
import pytest
from koval.engine.backtest_engine import EngineRunSpec
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup

from koval_backtrader import backtest_runner
from koval_backtrader.run_identity import EvidenceIdentity, resolve_evidence_identity

START_MS = 1_704_067_200_000
SIGNAL = (100.0, 101.0, 99.0, 100.0, 1000.0)
GRAPH = {"blocks": [{"id": "a", "type": "signal.every_bar"}], "connections": []}


def evidence(**overrides) -> dict:
    block = {
        "dataset_id": "binance-btcusdt-1h-2024",
        "source": "binance_klines_archive",
        "retrieved_at_ms": 1_735_689_600_000,
        "content_sha256": "a" * 64,
    }
    block.update(overrides)
    return block


def market(**overrides) -> dict:
    block = {
        "venue": "binance",
        "market": "futures",
        "symbol": "BTCUSDT",
        "contract_type": "perpetual",
        "base_currency": "BTC",
        "quote_currency": "USDT",
    }
    block.update(overrides)
    return block


def fixed_config(*, market_block=None, evidence_block=None, version="ohlcv_fixed_v1") -> dict:
    model = {"version": version, "commission_bps": 4.0}
    if version == "ohlcv_fixed_v1":
        model.update(spread_bps=20.0, slippage_bps=10.0, leverage=1.0)
    config = {"exchange": "binance", "exchange_type": "future", "execution_model": model}
    if market_block is not None:
        config["market"] = market_block
    if evidence_block is not None:
        config["evidence"] = evidence_block
    return config


def run(monkeypatch, *, config, feeds=None, graph=GRAPH):
    class Probe(DeclarativeStrategy):
        def should_long(self):
            return self.bar_index == 1

        def go_long(self):
            return TradeSetup(
                direction="long",
                entry_type="market",
                entry_price=100.0,
                stop_loss=90.0,
                take_profit=120.0,
                size=2.0,
            )

    monkeypatch.setattr(backtest_runner, "assemble_from_graph", lambda g: Probe())
    if feeds is None:
        feeds = {
            "1h": np.array([[START_MS + i * 3_600_000, *SIGNAL] for i in range(3)], dtype=float)
        }
    return backtest_runner.create_engine().run(
        EngineRunSpec(graph=graph, feeds=feeds, initial_capital=10_000.0, execution_config=config)
    )


# --- Evidence block validation -------------------------------------------


def test_a_complete_evidence_block_resolves():
    identity = resolve_evidence_identity(evidence())
    assert identity == EvidenceIdentity(
        dataset_id="binance-btcusdt-1h-2024",
        source="binance_klines_archive",
        retrieved_at_ms=1_735_689_600_000,
        content_sha256="a" * 64,
    )
    assert identity.as_dict() == evidence()


def test_absent_evidence_block_stays_absent():
    assert resolve_evidence_identity(None) is None


@pytest.mark.parametrize("field", sorted(evidence()))
def test_evidence_is_all_or_nothing(field):
    """A half-filled provenance block is worse than none: it looks complete."""
    block = evidence()
    del block[field]
    with pytest.raises(ValueError, match=field):
        resolve_evidence_identity(block)


@pytest.mark.parametrize("value", ["", "zz" * 32, "a" * 63, "A" * 64, 1, None])
def test_content_hash_must_be_lowercase_hex_sha256(value):
    with pytest.raises(ValueError, match="content_sha256"):
        resolve_evidence_identity(evidence(content_sha256=value))


@pytest.mark.parametrize("value", [-1, 0, 1.5, "1700000000000", True, None])
def test_retrieved_at_must_be_a_positive_integer_millisecond(value):
    with pytest.raises(ValueError, match="retrieved_at_ms"):
        resolve_evidence_identity(evidence(retrieved_at_ms=value))


def test_unknown_evidence_fields_are_rejected():
    with pytest.raises(ValueError, match="unknown"):
        resolve_evidence_identity(evidence(bucket="s3://x"))


# --- What a result carries ------------------------------------------------


def test_result_fingerprints_graph_feeds_and_software(monkeypatch):
    result = run(monkeypatch, config=fixed_config(market_block=market(), evidence_block=evidence()))
    identity = result.metrics["run_identity"]
    assert len(identity["graph_sha256"]) == 64
    assert len(identity["evidence_set_sha256"]) == 64
    feed = identity["feeds"]["1h"]
    assert feed["bars"] == 3
    assert feed["first_timestamp_ms"] == START_MS
    assert feed["last_timestamp_ms"] == START_MS + 2 * 3_600_000
    assert feed["timeframe_ms"] == 3_600_000
    assert len(feed["sha256"]) == 64
    assert identity["execution_model_version"] == "ohlcv_fixed_v1"
    assert identity["market"] == {
        "exchange": "binance",
        "market": "future",
        "canonical_symbol": "BTCUSDT",
        "contract_type": "perpetual",
    }
    assert identity["evidence"] == evidence()
    assert identity["software"]["koval-engine"]
    assert len(identity["plugin_sha256"]) == 64
    assert identity["warmup"]["history_bars"] > 0
    assert identity["warmup"]["primary_timeframe"] == "1h"


def test_a_changed_candle_changes_the_fingerprint(monkeypatch):
    config = fixed_config(market_block=market(), evidence_block=evidence())
    base = run(monkeypatch, config=config)
    altered_feed = {
        "1h": np.array([[START_MS + i * 3_600_000, *SIGNAL] for i in range(3)], dtype=float)
    }
    altered_feed["1h"][2][4] = 100.5
    altered = run(monkeypatch, config=config, feeds=altered_feed)
    assert (
        altered.metrics["run_identity"]["feeds"]["1h"]["sha256"]
        != (base.metrics["run_identity"]["feeds"]["1h"]["sha256"])
    )
    assert (
        altered.metrics["run_identity"]["evidence_set_sha256"]
        != (base.metrics["run_identity"]["evidence_set_sha256"])
    )


def test_a_changed_graph_changes_the_graph_hash(monkeypatch):
    config = fixed_config(market_block=market(), evidence_block=evidence())
    base = run(monkeypatch, config=config)
    other = run(monkeypatch, config=config, graph={**GRAPH, "connections": [{"from": "a"}]})
    assert (
        other.metrics["run_identity"]["graph_sha256"]
        != (base.metrics["run_identity"]["graph_sha256"])
    )


def test_graph_hash_ignores_key_order_only(monkeypatch):
    """Reordering a dict is not a different strategy; renaming a key is."""
    config = fixed_config(market_block=market(), evidence_block=evidence())
    base = run(monkeypatch, config=config)
    reordered = run(
        monkeypatch,
        config=config,
        graph={"connections": [], "blocks": [{"type": "signal.every_bar", "id": "a"}]},
    )
    assert (
        reordered.metrics["run_identity"]["graph_sha256"]
        == (base.metrics["run_identity"]["graph_sha256"])
    )


# --- Honest grading -------------------------------------------------------


def test_a_fully_specified_run_identifies_inputs_without_certifying_the_archive(monkeypatch):
    result = run(monkeypatch, config=fixed_config(market_block=market(), evidence_block=evidence()))
    grade = result.metrics["run_identity"]["reproducibility"]
    assert grade["level"] == "partial"
    assert grade["reasons"] == []


@pytest.mark.parametrize(
    "config_kwargs,reason",
    [
        ({"market_block": market()}, "dataset_evidence_absent"),
        ({"evidence_block": evidence()}, "market_identity_absent"),
    ],
)
def test_a_run_missing_provenance_is_graded_partial(monkeypatch, config_kwargs, reason):
    result = run(monkeypatch, config=fixed_config(**config_kwargs))
    grade = result.metrics["run_identity"]["reproducibility"]
    assert grade["level"] == "partial"
    assert reason in grade["reasons"]


def test_a_legacy_run_is_never_graded_reproducible(monkeypatch):
    result = run(monkeypatch, config=None)
    grade = result.metrics["run_identity"]["reproducibility"]
    assert grade["level"] == "partial"
    assert "unversioned_execution_model" in grade["reasons"]
    # Legacy results stay readable: identity is added, nothing is removed.
    assert result.metrics["run_identity"]["execution_model_version"] == "legacy_v1"
    assert result.metrics["execution_model"]["version"] == "legacy_v1"


def test_identity_is_json_serialisable_and_does_not_mutate_the_request(monkeypatch):
    config = fixed_config(market_block=market(), evidence_block=evidence())
    original = copy.deepcopy(config)
    result = run(monkeypatch, config=config)
    assert config == original
    assert json.loads(json.dumps(result.metrics, allow_nan=False)) == result.metrics


def test_two_identical_runs_produce_identical_identity(monkeypatch):
    config = fixed_config(market_block=market(), evidence_block=evidence())
    first = run(monkeypatch, config=config)
    second = run(monkeypatch, config=config)
    assert first.metrics["run_identity"] == second.metrics["run_identity"]


# --- Multi-timeframe metadata --------------------------------------------


def _multi_feeds():
    return {
        "1h": np.array([[START_MS + i * 3_600_000, *SIGNAL] for i in range(6)], dtype=float),
        "4h": np.array([[START_MS + i * 14_400_000, *SIGNAL] for i in range(2)], dtype=float),
    }


def test_every_feed_is_fingerprinted_in_a_multi_timeframe_run(monkeypatch):
    result = run(
        monkeypatch,
        config=fixed_config(market_block=market(), evidence_block=evidence()),
        feeds=_multi_feeds(),
    )
    identity = result.metrics["run_identity"]
    assert set(identity["feeds"]) == {"1h", "4h"}
    assert identity["feeds"]["4h"]["timeframe_ms"] == 14_400_000
    assert identity["warmup"]["primary_timeframe"] == "1h"
    assert identity["warmup"]["timeframes"] == ["1h", "4h"]


def test_an_unresolvable_timeframe_is_refused_in_a_multi_timeframe_run(monkeypatch):
    feeds = _multi_feeds()
    feeds["moonphase"] = feeds.pop("4h")
    with pytest.raises(ValueError, match="timeframe"):
        run(monkeypatch, config=fixed_config(), feeds=feeds)


def test_identity_uses_the_engine_stream_and_execution_contract(monkeypatch):
    from koval.engine.paper_profile import resolve_paper_profile
    from koval.engine.run_identity import CandleStreamIdentity, content_sha256

    result = run(monkeypatch, config=fixed_config(market_block=market()))
    identity = result.metrics["run_identity"]
    primary = CandleStreamIdentity("1h")
    for index in range(3):
        primary.append([START_MS + index * 3_600_000, *SIGNAL])
    assert identity["schema_version"] == "koval_run_identity_v1"
    assert identity["dataset_identity"]["primary"] == primary.as_dict()
    assert identity["dataset_identity"]["warmup"] == CandleStreamIdentity("1h").as_dict()
    assert identity["strategy_sha256"] == content_sha256(GRAPH)
    assert identity["reproducibility_grade"] == "identified_simulation"
    profile = resolve_paper_profile(
        {
            "version": "paper_ohlcv_fixed_v1",
            "commission_bps": 4.0,
            "spread_bps": 20.0,
            "slippage_bps": 10.0,
        }
    )
    assert identity["execution_identity"]["profile"] == profile.as_config()
    assert identity["run_parameters"]["end_of_data_policy"] == "mark_at_last_close"


def test_asserted_archive_id_never_certifies_full_reproducibility(monkeypatch):
    result = run(monkeypatch, config=fixed_config(market_block=market(), evidence_block=evidence()))
    identity = result.metrics["run_identity"]
    assert identity["reproducibility_grade"] == "identified_simulation"
    assert identity["reproducibility"]["level"] != "full"


def test_noncanonical_v1_data_remains_readable_but_not_comparable(monkeypatch):
    feeds = {"1h": np.array([[START_MS + 17 + i * 3_600_000, *SIGNAL] for i in range(3)])}
    identity = run(monkeypatch, config=fixed_config(market_block=market()), feeds=feeds).metrics[
        "run_identity"
    ]
    assert identity["reproducibility_grade"] == "not_comparable"
    assert (
        identity["dataset_identity"]["primary"]["encoding_version"]
        != "koval_candle_stream_sha256_v1"
    )
