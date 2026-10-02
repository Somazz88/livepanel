"""Broker-neutral data model.

Designed from the awkward cases first (IBKR contract ids, crypto perps), then fitted to moomoo:
  - an instrument's identity is whatever the broker uses natively (`key`): moomoo "US.AAPL",
    IBKR a conId, ccxt "BTC/USDT:USDT". Everything else is descriptive.
  - fields a broker may not provide are Optional and stay None. None means "unknown", never 0.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class AssetClass(str, Enum):
    STOCK = "stock"
    OPTION = "option"
    FUTURE = "future"
    CRYPTO_SPOT = "crypto_spot"
    CRYPTO_PERP = "crypto_perp"
    OTHER = "other"


class Side(str, Enum):
    SELL = "SELL"      # closes a long
    BUY = "BUY"        # closes a short


@dataclass(frozen=True)
class Instrument:
    key: str                         # broker-native id, unique within one broker
    symbol: str                      # human label: "AAPL", "INTC 11/20 120C", "BTC/USDT perp"
    asset_class: AssetClass
    currency: str = "USD"
    market: str = "US"               # "US", "HK", "MY", "CRYPTO", ...
    multiplier: float = 1.0
    underlying: str | None = None
    expiry: date | None = None
    strike: float | None = None
    right: str | None = None         # "C" / "P"


@dataclass
class Position:
    account_id: str
    instrument: Instrument
    qty: float                       # signed: >0 long, <0 short
    avg_price: float | None = None   # per unit (per share / per contract-unit, NOT times multiplier)
    price: float | None = None       # broker's current mark
    market_value: float | None = None
    unrealized_pnl: float | None = None
    today_pnl: float | None = None   # broker-provided; None = broker doesn't say
    closable_qty: float | None = None  # e.g. moomoo can_sell_qty; None = assume |qty|

    @property
    def side_to_close(self) -> Side:
        return Side.SELL if self.qty > 0 else Side.BUY


@dataclass
class Quote:
    bid: float | None
    ask: float | None
    tick: float | None = None        # price increment at the current price
    last: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    ts: datetime | None = None

    @property
    def usable(self) -> bool:
        return bool(self.bid and self.ask and self.bid > 0 and self.ask > 0)


@dataclass
class AccountSnapshot:
    account_id: str                  # already masked for display (see util.mask)
    label: str
    equity: float | None             # in base currency
    currency: str = "USD"
    ok: bool = True
    error: str | None = None
    paper: bool = False


@dataclass
class CashFlow:
    account_id: str
    day: date
    amount: float                    # signed, in `currency`
    currency: str
    kind: str                        # "deposit" | "withdrawal" | "fx" | "other"
    remark: str = ""


@dataclass
class OrderState:
    order_id: str
    status: str                      # normalized: "working" | "filled" | "cancelled" | "rejected" | "unknown"
    filled: float = 0.0
    avg_price: float | None = None
    raw_status: str = ""


@dataclass
class BrokerStatus:
    name: str
    connected: bool
    detail: str = ""
    accounts: list[AccountSnapshot] = field(default_factory=list)
