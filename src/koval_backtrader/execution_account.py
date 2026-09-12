# SPDX-License-Identifier: GPL-3.0-or-later
"""Account state a strategy can size risk from, sourced from the MIT engine.

The paper runtime derives balance, daily PnL, peak equity and drawdown from
``koval.engine.account_state.PlatformAccountState``. A backtest that computes
those differently will gate differently — the same fill sequence would produce
a different decision in the two runtimes, which defeats the point of having
both. So the adapter feeds that same class, in the same order the live runner
feeds it, and this module shapes the result into what a strategy sees.

Everything here reports what *happened*: the actual fill price, the actual
filled quantity, the fees already paid. A strategy that sizes from the price it
requested is sizing from a price it did not get.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

from koval.engine.account_ledger import AccountLedger
from koval.engine.account_state import AccountSnapshot, PlatformAccountState

# Default when no funding evidence is supplied: zero booked cashflow does
# not establish a historical zero rate.
FUNDING_STATUS_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class PositionInputs:
    """The open position as it actually exists, not as it was requested."""

    side: str  # "long" | "short", matching `position_direction`
    entry_price: float
    quantity: float
    current_stop: float
    entry_commission: float
    risk_amount: float


@dataclass(frozen=True, kw_only=True)
class AccountInputs(AccountSnapshot):
    """Engine snapshot plus backward-compatible plugin risk diagnostics."""

    funding_status: str
    position: PositionInputs | None

    @property
    def fees_paid(self) -> float:
        return self.fees

    @property
    def funding_paid(self) -> float:
        return self.funding


class IncrementalAccountLedger(AccountLedger):
    """Keep the public append-only ledger while deriving totals in constant time.

    The engine validates and records each entry. Only its derived totals are
    cached here, so bar cost does not depend on the number of previous trades.
    """

    def __init__(self, starting_balance: float) -> None:
        super().__init__(starting_balance)
        self._cash_total = 0.0
        self._fee_total = 0.0
        self._funding_total = 0.0
        self._trade_total = 0.0

    def record(self, **kwargs):
        entry = super().record(**kwargs)
        self._cash_total += entry.amount
        if entry.kind in {"commission", "liquidation_fee"}:
            self._fee_total -= entry.amount
        elif entry.kind == "funding":
            self._funding_total += entry.amount
        elif entry.kind == "trade_pnl":
            self._trade_total += entry.amount
        return entry

    @property
    def balance(self) -> float:
        return self.starting_balance + self._cash_total

    @property
    def fees(self) -> float:
        return self._fee_total

    @property
    def funding(self) -> float:
        return self._funding_total

    @property
    def trade_realized_pnl(self) -> float:
        return self._trade_total


def modelled_stop_exit(*, direction: str, stop_price: float, adjustment_fraction: float) -> float:
    """Price a stop-out is modelled to fill at, adverse adjustment included.

    Closing a long sells, so it pays the adjustment downward; closing a short
    buys and pays it upward. A bar that *gaps* through the stop fills worse
    than this and is not bounded by it — see the stop-gap rule in
    ``docs/execution-model.md``.
    """
    sign = -1.0 if direction == "long" else 1.0
    return float(stop_price) * (1.0 + sign * float(adjustment_fraction))


def risk_to_stop(
    *,
    direction: str,
    quantity: float,
    entry_price: float,
    stop_price: float,
    commission_bps: float,
    adjustment_fraction: float = 0.0,
) -> float:
    """Quote-currency loss if this position closes at its stop.

    Counts the price distance to the *modelled* stop fill plus the commission
    both legs pay, because a stop-out costs all three. Under a cost-free model
    this collapses to the plain price distance, which is what the legacy
    contract reported.
    """
    quantity = abs(float(quantity))
    exit_price = modelled_stop_exit(
        direction=direction, stop_price=stop_price, adjustment_fraction=adjustment_fraction
    )
    distance = (float(entry_price) - exit_price) * (1 if direction == "long" else -1)
    fees = (abs(float(entry_price)) + abs(exit_price)) * quantity * commission_bps / 10_000
    return max(0.0, quantity * distance + fees)


class ExecutionAccount:
    """Wraps the engine's account state with the fee and margin bookkeeping.

    ``PlatformAccountState`` owns balance, daily PnL, peak equity and drawdown.
    Broker-backed runs share its ledger without booking the same fills twice.
    Direct legacy use supplies a local ledger and books gross PnL and fees.
    """

    def __init__(
        self,
        starting_balance: float,
        *,
        commission_bps: float = 0.0,
        adjustment_fraction: float = 0.0,
        ledger: AccountLedger | None = None,
        funding_status: str = FUNDING_STATUS_UNAVAILABLE,
        daily_baseline_equity: float | None = None,
        peak_equity: float | None = None,
    ) -> None:
        self._state = PlatformAccountState(
            starting_balance=starting_balance,
            daily_baseline_equity=daily_baseline_equity,
            peak_equity=peak_equity,
            ledger=ledger if ledger is not None else IncrementalAccountLedger(starting_balance),
        )
        self._owns_ledger = ledger is None
        self._funding_status = funding_status
        self._commission_bps = float(commission_bps)
        self._adjustment_fraction = float(adjustment_fraction)
        self._entry_commission = 0.0

    def on_bar(self, *, equity: float, timestamp_ms: int) -> None:
        self._state.on_bar(equity=equity, timestamp_ms=timestamp_ms)

    def on_open(
        self,
        *,
        direction: str,
        fill_price: float,
        quantity: float,
        stop_price: float,
        commission: float,
        margin: float,
    ) -> None:
        self._state.on_open(
            side="buy" if direction == "long" else "sell",
            entry_price=fill_price,
            quantity=quantity,
            current_stop=stop_price,
            margin=margin,
        )
        self._entry_commission = float(commission)
        self.on_fee(commission)

    def on_fee(self, amount: float) -> None:
        if not amount:
            return
        if self._owns_ledger:
            self._state.on_fee(amount)

    def on_entry_fill(self, *, fill_price, quantity, commission, margin):
        self._state.on_entry_fill(entry_price=fill_price, quantity=quantity, margin=margin)
        self._entry_commission += commission
        self.on_fee(commission)

    def on_partial_close(self, *, quantity, realized_pnl, commission):
        self._state.on_partial_close(
            quantity=quantity, realized_pnl=realized_pnl, record_ledger=self._owns_ledger
        )
        self.on_fee(commission)

    def on_stop_moved(self, stop_price: float) -> None:
        self._state.on_stop_update(stop_price)

    def on_close(self, *, realized_pnl: float, commission: float) -> None:
        """Book gross trade PnL and exit commission as separate ledger entries."""
        self._state.on_close(realized_pnl=realized_pnl, record_ledger=self._owns_ledger)
        self.on_fee(commission)
        self._entry_commission = 0.0

    def snapshot(self) -> AccountInputs:
        snap = self._state.snapshot()
        position = snap.open_position
        return AccountInputs(
            **{field.name: getattr(snap, field.name) for field in fields(AccountSnapshot)},
            funding_status=self._funding_status,
            position=None
            if position is None
            else PositionInputs(
                side="long" if position.side == "buy" else "short",
                entry_price=position.entry_price,
                quantity=position.quantity,
                current_stop=position.current_stop,
                entry_commission=self._entry_commission,
                risk_amount=risk_to_stop(
                    direction="long" if position.side == "buy" else "short",
                    quantity=position.quantity,
                    entry_price=position.entry_price,
                    stop_price=position.current_stop,
                    commission_bps=self._commission_bps,
                    adjustment_fraction=self._adjustment_fraction,
                ),
            ),
        )

    def ledger_entries(self) -> list[dict]:
        return [asdict(entry) for entry in self._state.ledger.entries]

    @property
    def drawdown_pct(self) -> float:
        return self._state.snapshot().drawdown_pct

    @property
    def entry_commission(self) -> float:
        """Fee already debited for the open position's entry leg."""
        return self._entry_commission

    @property
    def peak_equity(self) -> float:
        return self._state.snapshot().peak_equity


def revalidated_risk(
    *,
    direction: str,
    requested_price: float,
    requested_size: float,
    fill_price: float,
    filled_size: float,
    stop_price: float,
    commission_bps: float,
    adjustment_fraction: float = 0.0,
) -> dict:
    """Risk as planned at signal time against risk as actually taken on.

    The gap between them is the number a risk gate should react to, and it is
    invisible if the strategy only ever sees the entry it asked for.
    """
    planned = abs(float(requested_size)) * abs(float(requested_price) - float(stop_price))
    actual = risk_to_stop(
        direction=direction,
        quantity=filled_size,
        entry_price=fill_price,
        stop_price=stop_price,
        commission_bps=commission_bps,
        adjustment_fraction=adjustment_fraction,
    )
    return {
        "requested_price": float(requested_price),
        "requested_size": float(requested_size),
        "filled_size": float(filled_size),
        "stop_loss": float(stop_price),
        "planned_risk": planned,
        "actual_risk": actual,
        "risk_drift": actual - planned,
    }
