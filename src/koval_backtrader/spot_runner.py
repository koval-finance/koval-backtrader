# SPDX-License-Identifier: GPL-3.0-or-later
"""Dedicated contract-v2 entry point; legacy Backtrader execution stays separate."""

from copy import deepcopy
from datetime import UTC, datetime

import backtrader as bt
from koval.engine.backtest_engine import (
    BacktestResult,
    ExecutionCapabilities,
    negotiate_execution_capabilities,
)

from koval_backtrader.spot_identity import source_identity
from koval_backtrader.spot_matching import exact, number
from koval_backtrader.spot_session import HistoricalSpotSession

FEATURES = (
    "spot_multi_order_v1",
    "spot_reservations_v1",
    "spot_causal_ohlcv_v1",
    "spot_state_restore_v1",
    "spot_dca_v1",
    "spot_grid_v1",
)


def _configuration(spec):
    from koval.engine.spot_multi_order import SpotBindingV1

    config = spec.execution_config
    if not isinstance(config, dict) or set(config) != {"spot_multi_order"}:
        raise ValueError("native recipes require explicit exclusive spot_multi_order configuration")
    config = deepcopy(config["spot_multi_order"])
    allowed = {
        "version",
        "binding",
        "instrument",
        "policy",
        "driver",
        "observations",
        "finish",
        "checkpoint",
        "journal_suffix",
    }
    if (
        not isinstance(config, dict)
        or set(config) - allowed
        or config.get("version") != "koval_spot_historical_v1"
    ):
        raise ValueError("unsupported historical Spot configuration/version")
    if not isinstance(config.get("finish", True), bool):
        raise ValueError("Spot finish must be a boolean")
    if config.get("journal_suffix") and config.get("checkpoint") is None:
        raise ValueError("historical journal_suffix requires a checkpoint")
    binding = SpotBindingV1.model_validate(config["binding"])
    if binding.mode != "historical" or binding.initial_quote_cash != number(spec.initial_capital):
        raise ValueError("historical binding initial cash must match initial_capital")
    if spec.runtime_contract is not None or set(spec.feeds) != {binding.timeframe}:
        raise ValueError("native Spot requires one bound timeframe and no legacy runtime_contract")
    if config.get("driver", "candles") not in {"candles", "observations"}:
        raise ValueError("unsupported historical Spot driver")
    if (
        config.get("driver") == "observations"
        and config["instrument"]["status"] != "synthetic_fixture"
    ):
        raise ValueError("explicit observation replay requires synthetic fixture evidence")
    return config, binding


def _new_session(config, graph, on_record):
    return HistoricalSpotSession(
        config["binding"], graph, config["instrument"], config["policy"], on_record=on_record
    )


def _candle_run(spec, config, session):
    # Cerebro supplies the clock. Prices/accounting remain Decimal in the
    # engine ledger, never a second floating-point BackBroker account.
    from koval_backtrader.backtest_runner import _feed_from_ndarray

    candles = spec.feeds[session.state.binding.timeframe]
    steps = config.get("observations")
    if steps is None:
        steps = [{"kind": "candle", "candle": list(row)} for row in candles]
    supplied = [s["candle"] for s in steps if s["kind"] == "candle"]
    if [[number(v) for v in row] for row in supplied] != [
        [number(v) for v in row] for row in candles
    ]:
        raise ValueError("Spot observations must match the supplied feed exactly")
    for step in steps:
        if step["kind"] not in {"candle", "command"} or set(step) - {
            "kind",
            "candle",
            "payload",
            "mark",
            "observation_id",
            "timestamp_ms",
        }:
            raise ValueError("unsupported historical candle observation")
    # Validate all chronology/evidence and marks before the first emitted write.
    # This is input validation only; graph/controller execution remains causal.
    from koval.engine.timeframe_utils import timeframe_to_minutes

    interval = timeframe_to_minutes(session.state.binding.timeframe) * 60000
    previous = session.state.last_candle
    mark = None if previous is None else previous[4]
    for step in steps:
        if step["kind"] == "candle":
            values = session._validate_candle(step["candle"], previous=previous)
            if values is not None:
                timestamp = values[0]
                if previous is not None and timestamp != previous[0] + interval:
                    if values != previous:
                        raise ValueError("Spot candle gap or out-of-order input")
                previous, mark = values, values[4]
        if "mark" in step and (mark is None or number(step["mark"]) != mark):
            raise ValueError("historical candle mark must equal the confirmed close")

    class Clock(bt.Strategy):
        def __init__(self):
            self.cursor = 0

        def next(self):
            nonlocal session
            while self.cursor < len(steps):
                step = steps[self.cursor]
                self.cursor += 1
                if step["kind"] == "candle":
                    session.on_candle(step["candle"])
                    break
                session.command(step["payload"])
            while self.cursor < len(steps) and steps[self.cursor]["kind"] == "command":
                session.command(steps[self.cursor]["payload"])
                self.cursor += 1

    cerebro = bt.Cerebro(stdstats=False)
    cerebro.adddata(_feed_from_ndarray(candles))
    cerebro.addstrategy(Clock)
    cerebro.run()
    return session


def _observation_run(config, graph, session, on_record):
    for step in config.get("observations", []):
        if set(step) - {"kind", "payload", "mark", "observation_id", "timestamp_ms"}:
            raise ValueError("unsupported historical replay fields")
        if step["kind"] == "restore":
            document = session.export_state()
            session = _new_session(config, graph, on_record)
            session.restore_state(document)
        else:
            session.replay(step)
    return session


@exact
def run_spot(spec, on_event=None):
    from koval_backtrader.backtest_runner import _validate_fixed_inputs

    negotiated = negotiate_execution_capabilities(spec, ExecutionCapabilities((2,), FEATURES))
    config, binding = _configuration(spec)
    _validate_fixed_inputs(spec)
    records = []

    def append(record):
        if on_event is not None:
            on_event(
                {
                    "event_type": "SPOT_EXECUTION_RECORD",
                    "bar_index": record["state"]["state"]["bar_index"],
                    "timestamp_ms": int(record["state"]["state"]["last_candle"][0])
                    if record["state"]["state"].get("last_candle")
                    else None,
                    "payload": deepcopy(record),
                }
            )
        records.append({key: value for key, value in record.items() if key != "state"})

    session = _new_session(config, spec.graph, append)
    if config.get("checkpoint") is not None:
        session.restore_state(config["checkpoint"], journal_suffix=config.get("journal_suffix", ()))
    if config.get("driver", "candles") == "candles":
        session = _candle_run(spec, config, session)
    else:
        session = _observation_run(config, spec.graph, session, append)
    if config.get("finish", True):
        session.finish()
    state = session.state
    steps = config.get("observations") or []
    mark = steps[-1].get("mark") if steps else None
    mark = mark or state.mark
    if mark is None:
        raise ValueError("Spot result requires a confirmed mark or explicit replay mark")
    report = session.report(mark=mark)
    equity = [
        {
            "timestamp": datetime.fromtimestamp(p["timestamp_ms"] / 1000, UTC).isoformat(),
            "equity": float(p["net_equity"]),
        }
        for p in state.equity_curve
    ]
    final = None if report["net_equity"] is None else float(report["net_equity"])
    metrics = {
        "initial_capital": spec.initial_capital,
        "final_capital": final,
        "total_pnl": None if final is None else final - spec.initial_capital,
        "total_return_pct": None if final is None else (final / spec.initial_capital - 1) * 100,
        "max_drawdown": None
        if final is None
        else max(
            float(state.maximum_drawdown * 100),
            float((state.peak_equity - number(report["net_equity"])) / state.peak_equity * 100),
        ),
        "total_trades": 0,
        "spot_execution_v1": report,
        "execution_model": {
            "version": "koval_spot_historical_v1",
            "policy": report["policy"],
            "negotiated_capabilities": {
                "execution_contract_version": negotiated.execution_contract_version,
                "features": list(negotiated.features),
            },
            "software": source_identity(),
            "accounting": "engine_decimal_spot_ledger",
            "clock": "explicit_observation_replay"
            if config.get("driver") == "observations"
            else "backtrader_cerebro",
            "end_of_data": binding.end_of_data_policy,
        },
        "execution_audit": {
            "version": "koval_spot_historical_audit_v1",
            "records": records,
            "checkpoint": session.export_state(),
        },
    }
    # A synthetic final-close liquidation changes terminal equity without
    # inventing another candle or another liquidity allowance.
    if equity and final is not None:
        equity[-1]["equity"] = final
    return BacktestResult(metrics=metrics, trades=[], equity_curve=equity)
