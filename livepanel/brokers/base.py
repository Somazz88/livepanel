"""The broker interface. One adapter per broker; the panel and the liquidator only talk to this.

Read side (always on):   connect / snapshot / positions / quotes / cash_flows
Trade side (opt-in):     place_limit / modify_price / cancel / order_states
An adapter that cannot answer something returns None (cash_flows) or raises BrokerError with a
human-readable reason. It never returns 0 for "unknown".
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from ..models import AccountSnapshot, CashFlow, Fill, Instrument, OrderState, Position, Quote, Side


class BrokerError(RuntimeError):
    pass


class Broker(ABC):
    name: str = "?"
    # (max actions, per seconds) per account; the liquidator shares one budget across legs
    rate_limits: dict[str, tuple[int, float]] = {"place": (10, 30.0), "modify": (10, 30.0)}

    @abstractmethod
    def connect(self) -> None: ...

    def close(self) -> None:
        pass

    @abstractmethod
    def snapshot(self) -> list[AccountSnapshot]:
        """Equity per account in the base currency."""

    @abstractmethod
    def positions(self) -> list[Position]: ...

    @abstractmethod
    def quotes(self, instruments: list[Instrument]) -> dict[str, Quote]:
        """{instrument.key: Quote}. Missing key = no quote."""

    def cash_flows(self, start: date, end: date) -> list[CashFlow] | None:
        """Deposits / withdrawals between start and end inclusive. None = this broker can't say."""
        return None

    def underlying_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """{"INTC": Quote} for the stocks under option positions — the 'is the stock up today' column."""
        return {}

    def recent_fills(self, days: int = 3) -> list[Fill] | None:
        """Executions of the last few days. None = this broker can't say."""
        return None

    def fx_rates(self) -> dict[str, float]:
        """{ccy: units per 1 base}, if the broker can observe them. Config rates fill the rest."""
        return {}

    # ── trading ──────────────────────────────────────────────────────────
    needs_unlock = False                 # True: live orders need the trade password once per session (moomoo)

    def unlock(self, password: str) -> None:
        """Unlock live trading with the trade password. Brokers that don't use one raise."""
        raise BrokerError(f"{self.name}: no trade password is used here")

    def place_limit(self, account_id: str, inst: Instrument, side: Side, qty: float, price: float,
                    session: str = "rth") -> str:
        raise BrokerError(f"{self.name}: trading not implemented")

    def modify_price(self, account_id: str, order_id: str, inst: Instrument, qty_total: float, price: float) -> None:
        raise BrokerError(f"{self.name}: trading not implemented")

    def cancel(self, account_id: str, order_id: str) -> None:
        raise BrokerError(f"{self.name}: trading not implemented")

    def order_states(self, account_id: str, order_ids: list[str]) -> dict[str, OrderState]:
        raise BrokerError(f"{self.name}: trading not implemented")
