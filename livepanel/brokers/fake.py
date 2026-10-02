"""A broker that needs nothing installed (demo + tests), and a dry-run wrapper for real brokers.

DryRun: real positions and real quotes, but no order ever leaves the machine. A simulated order
fills when the market crosses it (bid >= our sell price / ask <= our buy price).
"""
from __future__ import annotations

import itertools
import threading
from datetime import date

from ..models import AccountSnapshot, AssetClass, CashFlow, Instrument, OrderState, Position, Quote, Side
from .base import Broker, BrokerError


class _SimOrders:
    def __init__(self, quote_fn):
        self.quote_fn = quote_fn
        self.orders: dict[str, dict] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def place(self, account_id, inst, side, qty, price, session="rth") -> str:
        with self._lock:
            oid = f"sim-{next(self._ids)}"
            self.orders[oid] = {"inst": inst, "side": side, "qty": qty, "price": price, "filled": 0.0,
                                "status": "working", "account": account_id}
            return oid

    def modify(self, order_id, price):
        with self._lock:
            o = self.orders[order_id]
            if o["status"] == "working":
                o["price"] = price

    def cancel(self, order_id):
        with self._lock:
            o = self.orders[order_id]
            if o["status"] == "working":
                o["status"] = "cancelled"

    def states(self, order_ids):
        live = [o for o in self.orders.values() if o["status"] == "working"]
        qs = self.quote_fn([o["inst"] for o in live]) if live else {}
        out = {}
        with self._lock:
            for oid in order_ids:
                o = self.orders.get(oid)
                if o is None:
                    continue
                q = qs.get(o["inst"].key)
                if o["status"] == "working" and q and q.usable:
                    crossed = q.bid >= o["price"] - 1e-9 if o["side"] == Side.SELL else q.ask <= o["price"] + 1e-9
                    if crossed:
                        o.update(status="filled", filled=o["qty"], avg=o["price"])
                out[oid] = OrderState(oid, o["status"], o["filled"], o.get("avg"), o["status"])
        return out


class FakeBroker(Broker):
    name = "fake"
    rate_limits = {"place": (100, 30.0), "modify": (100, 30.0)}

    def __init__(self):
        self.pos: list[Position] = []
        self.q: dict[str, Quote] = {}
        self.flows: list[CashFlow] | None = []
        self.equity = 50_000.0
        self.sim = _SimOrders(self.quotes)
        self.calls: list[tuple] = []

    @classmethod
    def demo(cls) -> "FakeBroker":
        b = cls()
        opt = Instrument("US.INTC261120C120000", "INTC 11/20 120C", AssetClass.OPTION, multiplier=100.0,
                         underlying="INTC", expiry=date(2026, 11, 20), strike=120.0, right="C")
        stk = Instrument("US.AAPL", "AAPL", AssetClass.STOCK)
        shrt = Instrument("US.TSLA", "TSLA", AssetClass.STOCK)
        b.pos = [Position("DEMO-0001", opt, 10, 11.80, 12.50, 12_500, 700, 85, 10),
                 Position("DEMO-0001", stk, 50, 210.0, 228.4, 11_420, 920, -30, 50),
                 Position("DEMO-0001", shrt, -20, 260.0, 251.0, -5_020, 180, 44, 20)]
        b.q = {opt.key: Quote(12.45, 12.65, 0.05, 12.51), stk.key: Quote(228.38, 228.41, 0.01, 228.4),
               shrt.key: Quote(250.95, 251.02, 0.01, 251.0)}
        return b

    def connect(self) -> None:
        self.calls.append(("connect",))

    def snapshot(self):
        return [AccountSnapshot("DEMO-0001", "DEMO-0001", self.equity)]

    def positions(self):
        return [Position(**{**p.__dict__}) for p in self.pos]

    def quotes(self, instruments):
        return {i.key: self.q[i.key] for i in instruments if i.key in self.q}

    def cash_flows(self, start, end):
        return None if self.flows is None else [f for f in self.flows if start <= f.day <= end]

    def place_limit(self, account_id, inst, side, qty, price, session="rth"):
        self.calls.append(("place", inst.key, side, qty, price, session))
        return self.sim.place(account_id, inst, side, qty, price, session)

    def modify_price(self, account_id, order_id, inst, qty_total, price):
        self.calls.append(("modify", order_id, price))
        self.sim.modify(order_id, price)

    def cancel(self, account_id, order_id):
        self.calls.append(("cancel", order_id))
        self.sim.cancel(order_id)

    def order_states(self, account_id, order_ids):
        return self.sim.states(order_ids)


class DryRun(Broker):
    """Wraps a real broker: reads pass through, orders are simulated locally."""

    def __init__(self, inner: Broker):
        self.inner = inner
        self.name = f"{inner.name} (dry-run)"
        self.rate_limits = inner.rate_limits
        self.sim = _SimOrders(inner.quotes)

    def connect(self):
        self.inner.connect()

    def close(self):
        self.inner.close()

    def snapshot(self):
        return self.inner.snapshot()

    def positions(self):
        return self.inner.positions()

    def quotes(self, instruments):
        return self.inner.quotes(instruments)

    def cash_flows(self, start, end):
        return self.inner.cash_flows(start, end)

    def fx_rates(self):
        return self.inner.fx_rates()

    def place_limit(self, account_id, inst, side, qty, price, session="rth"):
        return self.sim.place(account_id, inst, side, qty, price, session)

    def modify_price(self, account_id, order_id, inst, qty_total, price):
        self.sim.modify(order_id, price)

    def cancel(self, account_id, order_id):
        self.sim.cancel(order_id)

    def order_states(self, account_id, order_ids):
        return self.sim.states(order_ids)
