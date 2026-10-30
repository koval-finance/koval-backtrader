# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent public fixture expectations through the normal plugin loader."""

from copy import deepcopy

import numpy as np
import pytest
from koval.engine.backtest_engine import (
    EngineRunSpec,
    ProtocolVersionError,
    load_backtest_engine,
)
from koval.engine.spot_multi_order import (
    SpotPaperSessionV1,
    compare_spot_execution_reports,
    run_spot_fixture,
)
from koval.examples import spot_multi_order_fixtures


def spec_for(case, observations=None, *, finish=False):
    observations = deepcopy(case["observations"] if observations is None else observations)
    for index, step in enumerate(observations):
        step.setdefault("observation_id", f"fixture-{index}")
        step.setdefault("timestamp_ms", index)
        step.pop("expected", None)
        step.pop("expect_error", None)
    candles = [s["candle"] for s in observations if s["kind"] == "candle"]
    binding = deepcopy(case["binding"])
    binding["mode"] = "historical"
    return EngineRunSpec(
        graph=deepcopy(case["graph"]),
        feeds={binding["timeframe"]: np.array(candles or [[0, 11, 11, 11, 11, 1000]], dtype=float)},
        initial_capital=float(binding["initial_quote_cash"]),
        execution_contract_version=2,
        execution_config={
            "spot_multi_order": {
                "version": "koval_spot_historical_v1",
                "binding": binding,
                "instrument": deepcopy(case["instrument"]),
                "policy": deepcopy(case["policy"]),
                "driver": "candles" if case["driver"] == "paper" else "observations",
                "observations": observations,
                "finish": finish,
            }
        },
    )


CASES = spot_multi_order_fixtures()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_all_shared_fixtures_through_loader(case):
    assert run_spot_fixture(case).matches
    valid = []
    for index, source in enumerate(case["observations"]):
        step = deepcopy(source)
        step["observation_id"] = f"fixture-{index}"
        step["timestamp_ms"] = index
        if source.get("expect_error"):
            with pytest.raises(ValueError, match=source["expect_error"]):
                load_backtest_engine().run(spec_for(case, valid + [step]))
            continue
        valid.append(step)
        result = load_backtest_engine().run(spec_for(case, valid))
        actual = result.metrics["spot_execution_v1"]
        comparison = compare_spot_execution_reports(source["expected"], actual)
        assert comparison.matches, comparison.differences
        assert result.trades == []  # Additions/lots are not independent bracket trades.


@pytest.mark.parametrize(
    "case", [c for c in CASES if c["driver"] == "paper"], ids=lambda c: c["id"]
)
def test_full_paper_report_and_terminal_parity(case):
    paper = SpotPaperSessionV1(
        case["binding"], case["graph"], case["instrument"], case["policy"], on_record=lambda _: None
    )
    for step in case["observations"]:
        if step["kind"] == "candle":
            paper.on_candle(step["candle"])
        else:
            paper.command(step["payload"])
    paper.finish()
    result = load_backtest_engine().run(spec_for(case, finish=True))
    expected = paper.report(mark=case["observations"][-1]["mark"])
    actual = deepcopy(result.metrics["spot_execution_v1"])
    actual["binding"]["mode"] = "paper"  # Only the host mode differs.
    comparison = compare_spot_execution_reports(expected, actual)
    assert comparison.matches, comparison.differences


def test_legacy_configuration_cannot_silently_run_native_recipes():
    spec = spec_for(CASES[0])
    spec.execution_config = None
    with pytest.raises(ValueError, match="explicit.*spot_multi_order"):
        load_backtest_engine().run(spec)


def case_named(name):
    return deepcopy(next(c for c in CASES if c["id"] == name))


def historical_session(case, on_record=lambda _: None):
    from koval_backtrader.spot_session import HistoricalSpotSession

    config = spec_for(case).execution_config["spot_multi_order"]
    return HistoricalSpotSession(
        config["binding"], case["graph"], case["instrument"], case["policy"], on_record=on_record
    )


def test_hand_calculated_controls_keep_account_and_pair_profit_distinct():
    grid = (
        load_backtest_engine()
        .run(spec_for(case_named("grid_control")))
        .metrics["spot_execution_v1"]
    )
    assert (
        grid["quote_cash"],
        grid["base_quantity"],
        grid["quote_reserved"],
        grid["free_quote"],
        grid["realized_profit"],
    ) == ("210", "0", "190", "20", "10")
    dca = (
        load_backtest_engine().run(spec_for(case_named("dca_control"))).metrics["spot_execution_v1"]
    )
    assert dca["base_quantity"] == "0"
    assert abs(float(dca["quote_cash"]) - 1020) < 1e-12
    assert len([f for f in dca["fills"] if f["side"] == "sell"]) == 1


def test_hold_releases_reserved_base_and_restore_cannot_restart():
    case = case_named("grid_control")
    session = historical_session(case)
    for step in case["observations"][:3]:
        session.on_candle(step["candle"])
    assert session.report(mark="10")["base_reserved"] == "10"
    command = {"command_id": "hold", "action": "stop_and_hold", "reason": "manual"}
    session.command(command)
    session.command(command)
    checkpoint = session.export_state()
    restored = historical_session(case)
    restored.restore_state(checkpoint)
    restored.on_candle([180000, 7, 7, 7, 7, 100])
    report = restored.report(mark="7")
    assert (
        report["base_quantity"],
        report["base_reserved"],
        report["quote_reserved"],
        report["phase"],
    ) == ("10", "0", "0", "stopped_holding")
    assert len(report["intents"]) == len(session.report(mark="10")["intents"])


def test_flatten_partial_close_finish_idempotence_and_dust():
    case = case_named("grid_control")
    session = historical_session(case)
    for step in case["observations"][:3]:
        session.on_candle(step["candle"])
    command = {"command_id": "flatten", "action": "stop_and_flatten", "reason": "manual"}
    session.command(command)
    session.command(command)
    session.on_candle([180000, 10, 10, 10, 10, 3])
    assert session.state.account.net_base == 7
    assert len([o for o in session.state.orders.values() if o.intent.purpose == "flatten"]) == 1
    restored = historical_session(case)
    restored.restore_state(session.export_state())
    restored.on_candle([240000, 10, 10, 10, 10, 100])
    assert restored.state.account.net_base == 0
    restored.finish()
    checkpoint = restored.export_state()
    restored.finish()
    assert restored.export_state() == checkpoint
    with pytest.raises(ValueError, match="end of data"):
        restored.on_candle([300000, 10, 10, 10, 10, 100])


def test_journal_failure_and_incomplete_checkpoint_require_fresh_recovery():
    case = case_named("grid_control")
    records = []

    def fail_at_second_crossing(record):
        records.append(deepcopy(record))
        if len(records) == 3:
            raise OSError("storage unavailable")

    session = historical_session(case, fail_at_second_crossing)
    with pytest.raises(OSError, match="storage"):
        session.on_candle(case["observations"][0]["candle"])
    with pytest.raises(RuntimeError, match="fresh"):
        session.on_candle(case["observations"][0]["candle"])
    restored = historical_session(case)
    with pytest.raises(ValueError, match="incomplete"):
        restored.restore_state(session.export_state())


def test_journal_suffix_recovery_matches_uninterrupted_session():
    case = case_named("grid_control")
    records = []
    session = historical_session(case, records.append)
    session.on_candle(case["observations"][0]["candle"])
    checkpoint = session.export_state()
    count = len(records)
    session.on_candle(case["observations"][1]["candle"])
    restored = historical_session(case)
    restored.restore_state(checkpoint, journal_suffix=records[count:])
    assert restored.export_state() == session.export_state()
    for instance in (session, restored):
        instance.on_candle(case["observations"][2]["candle"])
    assert restored.export_state() == session.export_state()


def test_native_run_state_is_independent_under_concurrency():
    from concurrent.futures import ThreadPoolExecutor

    case = case_named("grid_control")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: load_backtest_engine().run(spec_for(case)), range(2)))
    assert results[0].metrics["spot_execution_v1"] == results[1].metrics["spot_execution_v1"]
    results[0].metrics["spot_execution_v1"]["orders"].clear()
    assert results[1].metrics["spot_execution_v1"]["orders"]


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda s: setattr(s, "execution_contract_version", 1), "contract v2"),
        (
            lambda s: setattr(s, "required_execution_capabilities", ("unavailable",)),
            "missing required",
        ),
        (lambda s: s.execution_config.update({"exchange_type": "future"}), "exclusive"),
        (
            lambda s: s.execution_config["spot_multi_order"]["binding"].update({"mode": "paper"}),
            "historical",
        ),
        (lambda s: s.execution_config["spot_multi_order"].update({"finish": "yes"}), "boolean"),
        (
            lambda s: s.execution_config["spot_multi_order"].update({"journal_suffix": [{}]}),
            "checkpoint",
        ),
    ],
)
def test_invalid_admission_emits_no_records(mutation, match):
    spec = spec_for(case_named("grid_control"))
    mutation(spec)
    events = []
    with pytest.raises((ValueError, RuntimeError), match=match):
        load_backtest_engine().run(spec, events.append)
    assert events == []


def test_native_spot_requires_protocol_two_before_emitting_records():
    spec = spec_for(case_named("grid_control"))
    spec.protocol_version = 1
    events = []
    with pytest.raises(ProtocolVersionError, match="Spot.*protocol version 2"):
        load_backtest_engine().run(spec, events.append)
    assert events == []


def test_float_feed_is_the_only_candle_source_and_unknown_fields_are_refused():
    spec = spec_for(case_named("grid_control"))
    spec.execution_config["spot_multi_order"]["observations"][0]["candle"][1] = "123"
    with pytest.raises(ValueError, match="match the supplied feed"):
        load_backtest_engine().run(spec)


def test_stop_and_final_fees_are_in_root_account_drawdown():
    case = case_named("quote_fee_grid")
    spec = spec_for(case, case["observations"][:3], finish=True)
    config = spec.execution_config["spot_multi_order"]
    config["binding"]["end_of_data_policy"] = "stop_and_flatten"
    result = load_backtest_engine().run(spec)
    final = result.metrics["final_capital"]
    assert result.metrics["total_pnl"] == final - spec.initial_capital
    assert result.equity_curve[-1]["equity"] == final
    assert (
        result.metrics["max_drawdown"]
        >= (spec.initial_capital - final) / spec.initial_capital * 100
    )


def test_restore_refuses_changed_software_even_when_caller_reuses_binding():
    case = case_named("grid_control")
    session = historical_session(case)
    session.on_candle(case["observations"][0]["candle"])
    document = session.export_state()
    assert "software" in document
    document["software"] = {"koval-engine": {"source_sha256": "0" * 64}}
    restored = historical_session(case)
    with pytest.raises(ValueError, match="software"):
        restored.restore_state(document)


def test_candle_mark_override_is_refused_before_output():
    spec = spec_for(case_named("grid_control"))
    spec.execution_config["spot_multi_order"]["observations"][-1]["mark"] = "1000000"
    events = []
    with pytest.raises(ValueError, match="mark.*close"):
        load_backtest_engine().run(spec, events.append)
    assert events == []


def test_candle_gaps_are_refused_before_any_execution():
    spec = spec_for(case_named("grid_control"))
    spec.feeds["1m"][-1, 0] += 60000
    spec.execution_config["spot_multi_order"]["observations"][-1]["candle"][0] += 60000
    events = []
    with pytest.raises(ValueError, match="gap"):
        load_backtest_engine().run(spec, events.append)
    assert events == []


def test_dust_is_reported_as_inventory_after_terminal_flatten():
    from decimal import Decimal

    from koval.engine.spot_multi_order import SpotInstrumentEvidenceV1, spot_content_hash

    case = case_named("base_asset_fee_dca")
    case["instrument"].update(quantity_step="0.1", market_quantity_step="0.1")
    case["binding"]["instrument_sha256"] = spot_content_hash(
        SpotInstrumentEvidenceV1.model_validate(case["instrument"])
    )
    session = historical_session(case)
    for step in case["observations"][:3]:
        session.on_candle(step["candle"])
    session.command({"command_id": "flatten", "action": "stop_and_flatten", "reason": "manual"})
    session.on_candle([180000, 100, 100, 100, 100, 100])
    report = session.report(mark="100")
    assert Decimal(report["base_quantity"]) > 0
    assert report["phase"] == "stopped_with_dust"
    assert Decimal(report["inventory_cost"]) > 0


def test_returned_audit_retains_compact_journal_and_final_recovery_checkpoint():
    result = load_backtest_engine().run(spec_for(case_named("grid_control")))
    audit = result.metrics["execution_audit"]
    assert audit["checkpoint"]["version"] == "koval_spot_historical_checkpoint_v1"
    assert audit["records"]
    assert all("state" not in row for row in audit["records"])
    software = result.metrics["execution_model"]["software"]
    # A source override resolves source-tree metadata, not an installed wheel.
    assert "distribution_metadata_version" in software["koval-engine"]


def rebind(case):
    from koval.engine.spot_multi_order import (
        SpotFillPolicyV1,
        SpotInstrumentEvidenceV1,
        spot_content_hash,
    )

    case["binding"]["policy_sha256"] = spot_content_hash(
        SpotFillPolicyV1.model_validate(case["policy"])
    )
    case["binding"]["instrument_sha256"] = spot_content_hash(
        SpotInstrumentEvidenceV1.model_validate(case["instrument"])
    )
    case["binding"]["graph_sha256"] = spot_content_hash(case["graph"])
    return case


def test_hand_calculated_costs_tick_rounding_and_distinct_maker_taker_fees():
    case = case_named("grid_control")
    case["policy"].update(
        maker_fee_bps="10",
        taker_fee_bps="20",
        buy_quote_fee_cap_bps="20",
        spread_bps="20",
        slippage_bps="10",
    )
    case["instrument"]["price_tick"] = "0.01"
    rebind(case)
    observations = case["observations"][:2] + [
        {"kind": "candle", "candle": [120000, "9.5", "9.5", "9.5", "9.5", 1000], "mark": "9.5"},
        {"kind": "candle", "candle": [180000, "10.5", 11, "10.5", 11, 1000], "mark": "11"},
    ]
    result = load_backtest_engine().run(spec_for(case, observations))
    report = result.metrics["spot_execution_v1"]
    buy, sell = report["fills"]
    # 9.5 * (1 + .001 spread + .001 slip), ceiling to .01 -> 9.52.
    assert (buy["quantity"], buy["price"], buy["fees"][0]["quantity"]) == ("10", "9.52", "0.1904")
    # Intrabar SELL costs cannot take a limit below 11; maker fee = 110*.001.
    assert (sell["price"], sell["fees"][0]["quantity"]) == ("11", "0.11")
    assert report["quote_cash"] == "214.4996"
    assert report["realized_profit"] == "14.4996"


def test_fractional_participation_is_shared_across_all_orders_and_both_sides():
    from decimal import Decimal

    case = case_named("grid_control")
    case["policy"]["maximum_volume_participation"] = "0.25"
    rebind(case)
    observations = case["observations"][:2] + [
        {"kind": "candle", "candle": [120000, 11, 12, 9, 11, 12], "mark": "11"},
    ]
    report = load_backtest_engine().run(spec_for(case, observations)).metrics["spot_execution_v1"]
    assert sum(Decimal(f["quantity"]) for f in report["fills"]) == 3
    assert all(f["side"] == "buy" for f in report["fills"])


@pytest.mark.parametrize(
    "opening,expected_purpose,expected_price",
    [
        (120, "target", "120"),
        (65, "flatten", "65"),
        (100, "flatten", "70"),
    ],
)
def test_existing_target_opening_gap_precedes_later_stop_but_intrabar_stop_wins(
    opening, expected_purpose, expected_price
):
    case = case_named("dca_control")
    observations = case["observations"][:3] + [
        {"kind": "candle", "candle": [180000, opening, 130, 60, 100, 100], "mark": "100"},
    ]
    report = load_backtest_engine().run(spec_for(case, observations)).metrics["spot_execution_v1"]
    sells = [f for f in report["fills"] if f["side"] == "sell"]
    assert len(sells) == 1
    assert (sells[0]["purpose"], sells[0]["price"]) == (expected_purpose, expected_price)


def test_unvalued_third_asset_fees_keep_performance_unavailable():
    case = case_named("deferred_fee_valuation")
    result = load_backtest_engine().run(spec_for(case, case["observations"][:3]))
    assert result.metrics["spot_execution_v1"]["unvalued_fees"]
    assert result.metrics["final_capital"] is None
    assert result.metrics["total_pnl"] is None
    assert result.metrics["max_drawdown"] is None


def test_candle_event_timestamps_are_integer_milliseconds():
    events = []
    load_backtest_engine().run(spec_for(case_named("grid_control")), events.append)
    assert events
    assert all(isinstance(e["timestamp_ms"], int) for e in events)


def test_native_candles_accept_integer_arrays_without_an_observation_override():
    spec = spec_for(case_named("grid_control"))
    spec.feeds["1m"] = spec.feeds["1m"].astype("int64")
    del spec.execution_config["spot_multi_order"]["observations"]
    result = load_backtest_engine().run(spec)
    assert result.metrics["spot_execution_v1"]["quote_cash"] == "210"


@pytest.mark.parametrize(
    "case_id,mutation,reason",
    [
        ("grid_control", lambda c: c["instrument"].update(price_tick="2"), "duplicate"),
        (
            "grid_control",
            lambda c: c["graph"]["blocks"][-1]["params"].update(lower_price="-1"),
            "outside supported bounds",
        ),
        (
            "dca_control",
            lambda c: (
                c["graph"]["blocks"][-1]["params"].update(budget_quote="200"),
                c["policy"].update(buy_quote_fee_cap_bps="10"),
            ),
            "budget",
        ),
        ("dca_control", lambda c: c["instrument"].update(maximum_open_orders=1), "capacity"),
        ("grid_control", lambda c: c["policy"].update(taker_fee_bps="10"), "fee reserve cap"),
    ],
)
def test_invalid_recipes_and_fee_headroom_fail_before_execution(case_id, mutation, reason):
    case = case_named(case_id)
    mutation(case)
    rebind(case)
    events = []
    with pytest.raises(ValueError, match=reason):
        load_backtest_engine().run(spec_for(case), events.append)
    assert events == []


def test_grid_never_silently_resizes_for_insufficient_activation_cash():
    case = case_named("grid_control")
    case["graph"]["blocks"][-1]["params"]["quantity_per_slot"] = "20"
    rebind(case)
    with pytest.raises(ValueError, match="budget|cash|allocation"):
        load_backtest_engine().run(spec_for(case))


def test_current_instrument_snapshot_is_reported_as_historical_approximation():
    case = case_named("grid_control")
    case["instrument"]["status"] = "current_snapshot"
    rebind(case)
    report = load_backtest_engine().run(spec_for(case)).metrics["spot_execution_v1"]
    assert report["instrument_usage"]["status"] == "approximation"
    assert report["instrument_usage"]["source_status"] == "current_snapshot"
