"""Build the broker(s) named in config. Several brokers can feed one panel; Composite routes calls."""
from __future__ import annotations

from datetime import date

from ..models import Instrument
from .base import Broker, BrokerError


class Composite(Broker):
    """Several brokers behind one interface. Account keys are unique per broker ("SG-1234", "IB-1234"),
    so trading calls route by account; quotes route by which broker reported the instrument."""

    def __init__(self, brokers: list[Broker]):
        self.brokers = brokers
        self.name = "+".join(b.name for b in brokers)
        self._acc: dict[str, Broker] = {}
        self._inst: dict[str, Broker] = {}
        self.errors: dict[str, str] = {}

    def connect(self) -> None:
        for b in self.brokers:
            try:
                b.connect()
                self.errors.pop(b.name, None)
            except Exception as e:                      # one broker down must not hide the other
                self.errors[b.name] = str(e)

    def close(self) -> None:
        for b in self.brokers:
            try:
                b.close()
            except Exception:
                pass

    def reconnect_failed(self) -> None:
        for b in self.brokers:
            if b.name in self.errors:
                try:
                    b.close()
                    b.connect()
                    self.errors.pop(b.name, None)
                except Exception as e:
                    self.errors[b.name] = str(e)

    def connected(self) -> list[Broker]:
        return [b for b in self.brokers if b.name not in self.errors]

    def broker_for_account(self, acc: str) -> Broker:
        if acc not in self._acc:
            raise BrokerError(f"unknown account {acc}")
        return self._acc[acc]

    def snapshot(self):
        out = []
        for b in self.connected():
            try:
                snaps = b.snapshot()
            except Exception as e:                      # e.g. gateway dropped: mark it, keep the others
                self.errors[b.name] = str(e)
                continue
            for s in snaps:
                self._acc[s.account_id] = b
                out.append(s)
        return out

    def positions(self):
        out = []
        for b in self.connected():
            try:
                ps = b.positions()
            except Exception as e:
                self.errors[b.name] = str(e)
                continue
            for p in ps:
                self._acc[p.account_id] = b
                self._inst[p.instrument.key] = b
            out.extend(ps)
        return out

    def quotes(self, instruments: list[Instrument]):
        out, by = {}, {}
        for i in instruments:
            b = self._inst.get(i.key)
            if b is not None:
                by.setdefault(id(b), (b, []))[1].append(i)
        for b, insts in by.values():
            out.update(b.quotes(insts))
        return out

    def cash_flows(self, start: date, end: date):
        out = []
        for b in self.connected():
            f = b.cash_flows(start, end)
            if f is None:
                return None
            out.extend(f)
        return out

    def underlying_quotes(self, symbols):
        out = {}
        for b in self.connected():
            missing = [s for s in symbols if s not in out]
            if not missing:
                break
            try:
                out.update(b.underlying_quotes(missing))
            except Exception:
                continue                                # no stock quotes from this one; try the next
        return out

    def recent_fills(self, days=3):
        out, any_known = [], False
        for b in self.connected():
            try:
                f = b.recent_fills(days)
            except Exception:
                f = None
            if f is not None:
                any_known = True
                out.extend(f)
        return out if any_known else None

    def fx_rates(self):
        out = {}
        for b in self.connected():
            out.update(b.fx_rates())
        return out

    def place_limit(self, account_id, inst, side, qty, price, session="rth"):
        return self.broker_for_account(account_id).place_limit(account_id, inst, side, qty, price, session)

    def modify_price(self, account_id, order_id, inst, qty_total, price):
        return self.broker_for_account(account_id).modify_price(account_id, order_id, inst, qty_total, price)

    def cancel(self, account_id, order_id):
        return self.broker_for_account(account_id).cancel(account_id, order_id)

    def order_states(self, account_id, order_ids):
        return self.broker_for_account(account_id).order_states(account_id, order_ids)


def make(cfg) -> Composite:
    bs: list[Broker] = []
    for name in cfg.brokers:
        if name == "moomoo":
            from .moomoo import MoomooBroker
            bs.append(MoomooBroker(cfg.get("moomoo", "host"), int(cfg.get("moomoo", "port")),
                                   paper=bool(cfg.get("moomoo", "paper")),
                                   include_futures=bool(cfg.get("moomoo", "include_futures")),
                                   cache_dir=cfg.data_dir))
        elif name == "ibkr":
            from .ibkr import IbkrBroker
            bs.append(IbkrBroker(cfg.get("ibkr", "host"), int(cfg.get("ibkr", "port")),
                                 client_id=int(cfg.get("ibkr", "client_id")),
                                 readonly=not (cfg.trading_enabled and not cfg.dry_run),
                                 market_data_type=int(cfg.get("ibkr", "market_data_type"))))
        elif name == "demo":
            from .fake import FakeBroker
            bs.append(FakeBroker.demo())
        else:
            raise ValueError(f"unknown broker {name!r} (moomoo / ibkr / demo)")
    if cfg.trading_enabled and cfg.dry_run:
        from .fake import DryRun
        bs = [DryRun(b) if b.name != "fake" else b for b in bs]
    return Composite(bs)
