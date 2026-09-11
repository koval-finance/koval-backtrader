# SPDX-License-Identifier: GPL-3.0-or-later
"""A spot label must constrain the simulation, not just annotate it.

`ohlcv_fixed_v1` accepted `exchange_type: "spot"` and then happily opened a
short at five times leverage, because the label only ever chose a fee. A result
like that cannot be reproduced on the venue it names, which makes it worse than
no result. These tests pin the market identity contract and the two constraints
a true spot product imposes.

The block is optional. A run that does not supply it behaves exactly as it did
before, so every released result still replays.
"""

from __future__ import annotations

import numpy as np
import pytest
from koval.engine.backtest_engine import EngineRunSpec
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup

from koval_backtrader import backtest_runner
from koval_backtrader.market_identity import MarketIdentity, resolve_market_identity

START_MS = 1_704_067_200_000
SIGNAL = (100.0, 101.0, 99.0, 100.0, 1000.0)


def spot_market(**overrides) -> dict:
    market = {
        "venue": "binance",
        "market": "spot",
        "symbol": "BTCUSDT",
        "contract_type": "spot",
        "base_currency": "BTC",
        "quote_currency": "USDT",
    }
    market.update(overrides)
    return market


def futures_market(**overrides) -> dict:
    return spot_market(
        **{
            "market": "futures",
            "contract_type": "perpetual",
            **overrides,
        }
    )


def run(monkeypatch, *, config, direction="long", rows=(SIGNAL, SIGNAL, SIGNAL), size=2.0):
    class Probe(DeclarativeStrategy):
        def should_long(self):
            return direction == "long" and self.bar_index == 1

        def should_short(self):
            return direction == "short" and self.bar_index == 1

        def go_long(self):
            return TradeSetup(
                direction=direction,
                entry_type="market",
                entry_price=100.0,
                stop_loss=90.0 if direction == "long" else 110.0,
                take_profit=120.0 if direction == "long" else 80.0,
                size=size,
            )

        go_short = go_long

    monkeypatch.setattr(backtest_runner, "assemble_from_graph", lambda graph: Probe())
    candles = np.array(
        [[START_MS + i * 3_600_000, *row] for i, row in enumerate(rows)], dtype=float
    )
    events: list[dict] = []
    result = backtest_runner.create_engine().run(
        EngineRunSpec(
            graph={}, feeds={"1h": candles}, initial_capital=10_000.0, execution_config=config
        ),
        on_event=events.append,
    )
    return result, events


def config_with(market=None, *, exchange_type="spot", leverage=1.0, **model_overrides) -> dict:
    model = {
        "version": "ohlcv_fixed_v1",
        "commission_bps": 4.0,
        "spread_bps": 20.0,
        "slippage_bps": 10.0,
        "leverage": leverage,
    }
    model.update(model_overrides)
    config = {
        "exchange": "binance",
        "exchange_type": exchange_type,
        "execution_model": model,
    }
    if market is not None:
        config["market"] = market
    return config


# --- Identity validation -------------------------------------------------


def test_a_complete_market_block_normalises_every_identifier():
    identity = resolve_market_identity(
        {
            "venue": " Binance ",
            "market": "SPOT",
            "symbol": " btcusdt ",
            "contract_type": "Spot",
            "base_currency": "btc",
            "quote_currency": "usdt",
        }
    )
    assert identity == MarketIdentity(
        exchange="binance",
        market="spot",
        canonical_symbol="BTCUSDT",
        contract_type="spot",
    )
    assert identity.market == "spot"
    assert identity.as_dict()["canonical_symbol"] == "BTCUSDT"


def test_absent_market_block_stays_absent():
    assert resolve_market_identity(None) is None


def test_market_output_matches_the_engine_identity():
    from koval.engine.market_identity import resolve_market_identity as engine_identity

    assert (
        resolve_market_identity(futures_market()).as_dict()
        == engine_identity(exchange="binance", market="future", symbol="BTCUSDT").as_dict()
    )


def test_unsupported_venue_is_rejected():
    with pytest.raises(ValueError, match="unsupported venue/market"):
        resolve_market_identity(spot_market(venue="acme-exchange"))


def test_delivery_contract_is_rejected():
    with pytest.raises(ValueError, match="contract_type"):
        resolve_market_identity(futures_market(contract_type="delivery"))


def test_whitebit_native_perpetual_symbol_is_not_rewritten_as_a_spot_pair():
    identity = resolve_market_identity(
        {
            "exchange": "whitebit",
            "market": "future",
            "canonical_symbol": "BTC_PERP",
            "contract_type": "perpetual",
        }
    )
    assert identity.as_dict()["canonical_symbol"] == "BTCPERP"


@pytest.mark.parametrize("field", sorted(spot_market()))
def test_every_market_identifier_is_required(field):
    market = spot_market()
    del market[field]
    with pytest.raises(ValueError, match=field):
        resolve_market_identity(market)


@pytest.mark.parametrize("value", ["", "   ", 1, None, [], True])
def test_market_identifiers_must_be_non_empty_strings(value):
    with pytest.raises(ValueError, match="symbol"):
        resolve_market_identity(spot_market(symbol=value))


def test_unknown_market_fields_are_rejected():
    with pytest.raises(ValueError, match="unknown"):
        resolve_market_identity(spot_market(tier="vip"))


@pytest.mark.parametrize("value", ["margin", "options", "perp"])
def test_market_kind_must_be_canonical(value):
    with pytest.raises(ValueError, match="market"):
        resolve_market_identity(spot_market(market=value))


@pytest.mark.parametrize("value", ["swap", "inverse", "futures"])
def test_contract_type_must_be_canonical(value):
    with pytest.raises(ValueError, match="contract_type"):
        resolve_market_identity(spot_market(contract_type=value))


@pytest.mark.parametrize(
    "market,contract_type",
    [("spot", "perpetual"), ("spot", "delivery"), ("futures", "spot")],
)
def test_market_and_contract_type_must_agree(market, contract_type):
    with pytest.raises(ValueError, match="contract_type"):
        resolve_market_identity(spot_market(market=market, contract_type=contract_type))


def test_symbol_must_contain_both_currencies():
    with pytest.raises(ValueError, match="symbol"):
        resolve_market_identity(spot_market(symbol="ETHUSDT", base_currency="BTC"))


def test_base_and_quote_must_differ():
    with pytest.raises(ValueError, match="quote_currency"):
        resolve_market_identity(spot_market(symbol="BTCBTC", quote_currency="BTC"))


# --- Constraints the label now imposes -----------------------------------


def test_spot_market_rejects_leverage_above_one(monkeypatch):
    with pytest.raises(ValueError, match="spot"):
        run(monkeypatch, config=config_with(spot_market(), leverage=2.0))


def test_spot_market_accepts_leverage_of_one(monkeypatch):
    result, _ = run(monkeypatch, config=config_with(spot_market(), leverage=1.0))
    assert result.metrics["final_capital"] > 0


def test_spot_market_refuses_to_open_a_short(monkeypatch):
    result, events = run(monkeypatch, config=config_with(spot_market()), direction="short")
    assert result.trades == []
    assert result.metrics["execution_costs"]["fills"] == []
    rejected = [e for e in events if e["event_type"] == "ORDER_REJECTED"]
    assert [e["payload"]["reason"] for e in rejected] == ["spot_short_unsupported"]
    assert result.metrics["final_capital"] == 10_000.0


def test_futures_market_still_allows_shorts_and_leverage(monkeypatch):
    config = config_with(futures_market(), exchange_type="future", leverage=5.0)
    result, events = run(monkeypatch, config=config, direction="short")
    assert [e["payload"]["reason"] for e in events if e["event_type"] == "ORDER_REJECTED"] == []
    assert result.metrics["execution_costs"]["fills"] != []


def test_market_block_must_agree_with_the_top_level_labels(monkeypatch):
    with pytest.raises(ValueError, match="exchange"):
        run(monkeypatch, config=config_with(spot_market(venue="whitebit")))


def test_market_kind_must_agree_with_exchange_type(monkeypatch):
    with pytest.raises(ValueError, match="exchange_type"):
        run(monkeypatch, config=config_with(futures_market(), exchange_type="spot"))


def test_market_identity_is_recorded_in_resolved_metadata(monkeypatch):
    result, _ = run(monkeypatch, config=config_with(spot_market()))
    metadata = result.metrics["execution_model"]
    assert metadata["resolved_config"]["market"] == resolve_market_identity(spot_market()).as_dict()
    assert metadata["assumptions"]["market_constraints"] == "spot_no_shorts_no_leverage"


def test_a_run_without_a_market_block_is_unconstrained(monkeypatch):
    """The old contract still holds, defects included, so v1 results replay."""
    result, events = run(
        monkeypatch, config=config_with(None, exchange_type="spot", leverage=3.0), direction="short"
    )
    assert result.metrics["execution_costs"]["fills"] != []
    assert [e for e in events if e["event_type"] == "ORDER_REJECTED"] == []
    metadata = result.metrics["execution_model"]
    assert "market" not in metadata["resolved_config"]
    assert metadata["assumptions"]["market_constraints"] == "unconstrained_label_only"


def test_resolved_market_config_replays_unchanged(monkeypatch):
    first, _ = run(monkeypatch, config=config_with(spot_market()))
    replay, _ = run(monkeypatch, config=first.metrics["execution_model"]["resolved_config"])
    assert replay.trades == first.trades
    assert replay.metrics["final_capital"] == first.metrics["final_capital"]


def test_a_refused_spot_short_reports_the_same_payload_shape_as_any_refusal(monkeypatch):
    """An ORDER_REJECTED consumer must not need to special-case this reason.

    Every other refusal carries the size and price that were refused, and is
    preceded by the filter and signal events. A payload missing `size` breaks
    a consumer that reads it positionally.
    """
    result, events = run(monkeypatch, config=config_with(spot_market()), direction="short")
    assert result.trades == []
    rejected = [e for e in events if e["event_type"] == "ORDER_REJECTED"]
    assert len(rejected) == 1
    payload = rejected[0]["payload"]
    assert payload["reason"] == "spot_short_unsupported"
    assert payload["direction"] == "short"
    assert payload["entry_type"] == "market"
    assert payload["size"] == 2.0
    assert payload["price"] == 100.0

    # The filters did pass and the strategy did signal; only the venue refused.
    kinds = [e["event_type"] for e in events]
    assert kinds.index("FILTER_PASSED") < kinds.index("ORDER_REJECTED")
    assert kinds.index("SIGNAL_DETECTED") < kinds.index("ORDER_REJECTED")
    assert "ORDER_PLACED" not in kinds
