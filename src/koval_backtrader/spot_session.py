# SPDX-License-Identifier: GPL-3.0-or-later
"""Historical session: public reducer, write-ahead journal and bound recovery."""

from copy import deepcopy

from koval.engine.spot_multi_order import (
    SpotObservationV1,
    SpotRecipeV1,
    advance_spot,
    cancel_spot_order,
    create_spot_session,
    export_spot_state,
    paper_reconciliation,
    replace_spot_order,
    restore_spot_state,
    spot_content_hash,
    spot_execution_report,
    submit_spot_order,
)
from koval.strategy.block_assembler import assemble_from_graph

from koval_backtrader.spot_candles import SpotCandleExecution
from koval_backtrader.spot_identity import source_identity
from koval_backtrader.spot_matching import TERMINAL, exact, match_event, status_event


class HistoricalSpotSession(SpotCandleExecution):
    """Serialized, offline driver. The engine owns every recipe/account decision."""

    def __init__(self, binding, graph, instrument, policy, *, on_record):
        recipes = [b for b in graph["blocks"] if b["type"] in {"exec.spot_dca", "exec.spot_grid"}]
        if len(recipes) != 1:
            raise ValueError("exactly one native Spot recipe required")
        recipe = SpotRecipeV1(
            kind=recipes[0]["type"].removeprefix("exec.spot_"), params=recipes[0]["params"]
        )
        self._state = create_spot_session(binding, recipe, instrument, policy)
        if (
            self._state.binding.mode != "historical"
            or self._state.binding.graph_sha256 != spot_content_hash(graph)
        ):
            raise ValueError("historical mode and matching graph binding required")
        self._strategy = assemble_from_graph(graph)
        self._strategy.config = {
            "symbol": self._state.binding.symbol,
            "timeframe": self._state.binding.timeframe,
        }
        self._sink = on_record
        self._failed = False
        self._fresh = True
        self._timestamp = 0
        self._software = source_identity()

    @property
    def state(self):
        return self._state.model_copy(deep=True)

    def _persist(self, candidate, kind):
        if self._failed:
            raise RuntimeError("Spot journal failed; restore a fresh historical session")
        record = {
            "version": "koval_spot_historical_journal_v1",
            "session_id": candidate.binding.session_id,
            "sequence": self._state.journal_sequence + 1,
            "previous_sha256": self._state.journal_sha256,
            "kind": kind,
            "state": export_spot_state(candidate),
        }
        record["sha256"] = spot_content_hash(record)
        try:
            self._sink(deepcopy(record))
        except Exception:
            self._failed = True
            raise
        candidate.journal_sequence = record["sequence"]
        candidate.journal_sha256 = record["sha256"]
        self._state, self._fresh = candidate, False

    def _observe(self, kind, payload, *, observation_id=None, timestamp_ms=None):
        sequence = self._state.observation_sequence + 1
        observation = SpotObservationV1(
            observation_id=observation_id or f"paper-o{sequence}",
            sequence=sequence,
            timestamp_ms=self._timestamp if timestamp_ms is None else timestamp_ms,
            kind=kind,
            payload=payload,
        )
        candidate = self._state.model_copy(deep=True)
        candidate.pending_observation = observation.model_dump(mode="json")
        self._persist(candidate, "observation_received")
        try:
            transition = advance_spot(self._state, observation)
        except Exception:
            self._failed = True
            raise
        transition.state.pending_observation = None
        self._persist(transition.state, kind)

    def _execution(self, event):
        self._observe("executions", {"events": [event.model_dump(mode="json")]})

    def _drain(self):
        for _ in range(512):
            state = self._state
            if state.reconciliation_required or state.quarantine:
                return
            pending = next(
                (o for o in state.orders.values() if o.status in {"pending", "cancel_pending"}),
                None,
            )
            if pending:
                status = "accepted" if pending.status == "pending" else "canceled"
                self._execution(status_event(state, pending, status, self._timestamp))
            elif any(
                o.status in TERMINAL and not o.terminal_reconciled for o in state.orders.values()
            ):
                self._observe("reconciliation", paper_reconciliation(state).model_dump(mode="json"))
            else:
                return
        raise RuntimeError("historical Spot reconciliation did not converge")

    @exact
    def _match(self, key, reference, at_open):
        event = match_event(
            self._state,
            self._state.orders[key],
            reference,
            at_open=at_open,
            timestamp_ms=self._timestamp,
        )
        if event is None:
            return
        if event.quantity:
            candidate = self._state.model_copy(deep=True)
            candidate.liquidity_used += event.quantity
            self._persist(candidate, "liquidity_allocation")
        self._execution(event)
        self._drain()

    def command(self, command):
        self._observe("command", command)
        self._drain()

    @exact
    def finish(self):
        action = self._state.binding.end_of_data_policy
        if self._state.finished:
            return
        self.command({"command_id": "end-of-data", "action": action, "reason": "end_of_data"})
        if action == "stop_and_flatten" and not self._state.reconciliation_required:
            for order in list(self._state.orders.values()):
                if (
                    order.intent.purpose == "flatten"
                    and order.status not in TERMINAL
                    and self._state.mark is not None
                ):
                    self._match(order.intent.client_order_id, self._state.mark, False)
        candidate = self._state.model_copy(deep=True)
        candidate.finished = True
        self._persist(candidate, "end_of_data")

    def report(self, *, mark):
        state = self._state
        report = spot_execution_report(state, mark=mark)
        report["phase"] = state.controller.phase
        report["order_states"] = [
            {
                "client_order_id": key,
                "generation": o.intent.generation,
                "status": o.status,
                "unknown_outcome": o.unknown_outcome,
                "filled_quantity": str(o.filled_quantity),
                "reserved_quote": str(o.reserved_quote),
                "reserved_base": str(o.reserved_base),
            }
            for key, o in state.orders.items()
        ]
        report["fills"] = [
            {
                "client_order_id": e.client_order_id,
                "purpose": e.purpose,
                "side": e.side,
                "quantity": str(e.quantity),
                "price": str(e.price),
                "fees": [f.model_dump(mode="json") for f in e.fees],
            }
            for e in state.events.values()
            if e.quantity
        ]
        report["intents"] = [
            {
                name: o.intent.model_dump(mode="json")[name]
                for name in (
                    "client_order_id",
                    "purpose",
                    "slot_id",
                    "generation",
                    "quantity",
                    "quote_notional_cap",
                    "limit_price",
                )
            }
            for o in state.orders.values()
        ]
        return report

    def export_state(self):
        return {
            "version": "koval_spot_historical_checkpoint_v1",
            "software": deepcopy(self._software),
            "engine_state": export_spot_state(self._state),
        }

    def restore_state(self, document, *, journal_suffix=()):
        if not self._fresh or self._failed:
            raise ValueError("restore requires a fresh historical session")
        if (
            set(document) != {"version", "software", "engine_state"}
            or document["version"] != "koval_spot_historical_checkpoint_v1"
            or document["software"] != self._software
        ):
            raise ValueError("historical checkpoint version/software mismatch")
        state = restore_spot_state(document["engine_state"], expected_binding=self._state.binding)
        for record in journal_suffix:
            unsigned = {key: value for key, value in record.items() if key != "sha256"}
            if (
                record["version"] != "koval_spot_historical_journal_v1"
                or record["session_id"] != state.binding.session_id
                or spot_content_hash(unsigned) != record["sha256"]
                or record["sequence"] != state.journal_sequence + 1
                or record["previous_sha256"] != state.journal_sha256
            ):
                raise ValueError("incomplete or conflicting historical journal suffix")
            state = restore_spot_state(record["state"], expected_binding=self._state.binding)
            state.journal_sequence, state.journal_sha256 = record["sequence"], record["sha256"]
        if state.bar_in_progress or state.pending_observation is not None:
            raise ValueError(
                "incomplete candle checkpoint requires complete committed journal suffix"
            )
        if state.graph_state:
            self._strategy.restore_graph_state(state.graph_state)
        self._state = state
        self._timestamp = int(state.last_candle[0]) if state.last_candle else 0
        self._fresh = False

    def replay(self, step):
        """Explicit synthetic execution evidence, with no auto-ACK or auto-cancel."""
        kind, payload = step["kind"], step.get("payload", {})
        if kind == "restore":
            raise ValueError("restore is a host operation; use a fresh session")
        if kind == "simulator_reconciliation":
            kind, payload = (
                "reconciliation",
                paper_reconciliation(self._state).model_dump(mode="json"),
            )
        operations = {
            "submit_request": lambda: submit_spot_order(self._state, payload),
            "replace_request": lambda: replace_spot_order(self._state, payload),
            "cancel_request": lambda: cancel_spot_order(
                self._state,
                payload["client_order_id"],
                command_id=payload["command_id"],
                generation=payload["generation"],
            ),
        }
        if kind in operations:
            self._persist(operations[kind](), kind)
        else:
            self._observe(
                kind,
                payload,
                observation_id=step.get("observation_id"),
                timestamp_ms=step.get("timestamp_ms"),
            )
