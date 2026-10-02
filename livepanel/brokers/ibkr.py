"""Interactive Brokers via TWS / IB Gateway (pip install ib_async).

STATUS: written against the ib_async API and tested against a fake gateway only.
        It has NOT been run against a real TWS / IB Gateway yet.

User setup: run TWS or IB Gateway and log in; Configure -> API -> Settings ->
"Enable ActiveX and Socket Clients". Ports: TWS 7496 live / 7497 paper, Gateway 4001 live / 4002 paper.
Leave "Read-Only API" ticked unless you want the exit button to send orders.
Real-time option quotes over the API need an OPRA Top of Book subscription in Client Portal.

ib_async runs on an asyncio loop that must stay on one thread. Every call is therefore marshalled
onto a single worker thread that owns the IB object; streaming values (P&L, quotes) update while
that thread runs ib.sleep().
"""
from __future__ import annotations

import asyncio
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from ..models import AccountSnapshot, AssetClass, Fill, Instrument, OrderState, Position, Quote, Side
from ..util import scrub
from .base import Broker, BrokerError

_SEC = {"STK": AssetClass.STOCK, "OPT": AssetClass.OPTION, "FUT": AssetClass.FUTURE, "CRYPTO": AssetClass.CRYPTO_SPOT}
_STATUS = {"Filled": "filled", "Cancelled": "cancelled", "ApiCancelled": "cancelled", "Inactive": "rejected",
           "PendingSubmit": "working", "PreSubmitted": "working", "Submitted": "working", "PendingCancel": "working",
           "ApiPending": "working"}


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or v == -1.0 or math.isinf(v)) else v   # IB uses -1 / nan for "no data"


def instrument_from_contract(c) -> Instrument:
    sec = getattr(c, "secType", "")
    ac = _SEC.get(sec, AssetClass.OTHER)
    mult = _num(getattr(c, "multiplier", None)) or (100.0 if ac == AssetClass.OPTION else 1.0)
    exp = None
    raw = str(getattr(c, "lastTradeDateOrContractMonth", "") or "")
    if len(raw) >= 8:
        try:
            exp = datetime.strptime(raw[:8], "%Y%m%d").date()
        except ValueError:
            exp = None
    ccy = getattr(c, "currency", "USD") or "USD"
    market = "US" if ccy == "USD" and ac in (AssetClass.STOCK, AssetClass.OPTION) else (getattr(c, "primaryExchange", "") or "?")
    sym = getattr(c, "symbol", "?")
    if ac == AssetClass.OPTION and exp:
        label = f"{sym} {exp:%m/%d} {_num(c.strike):g}{c.right}"
    else:
        label = getattr(c, "localSymbol", "") or sym
    return Instrument(key=str(c.conId), symbol=label, asset_class=ac, currency=ccy, market=market,
                      multiplier=mult, underlying=sym if ac == AssetClass.OPTION else None, expiry=exp,
                      strike=_num(getattr(c, "strike", None)) if ac == AssetClass.OPTION else None,
                      right=getattr(c, "right", None) if ac == AssetClass.OPTION else None)


class IbkrBroker(Broker):
    name = "ibkr"
    # IB's own limit is ~50 messages/s; the exit ladder never gets near it
    rate_limits = {"place": (40, 30.0), "modify": (40, 30.0)}

    def __init__(self, host: str = "127.0.0.1", port: int = 7496, client_id: int = 17,
                 readonly: bool = True, market_data_type: int = 1, ib_factory=None, limit_order_cls=None):
        self.host, self.port, self.client_id = host, port, client_id
        self.readonly, self.market_data_type = readonly, market_data_type
        self._ib_factory, self._limit_cls = ib_factory, limit_order_cls
        self._ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ibkr",
                                      initializer=lambda: asyncio.set_event_loop(asyncio.new_event_loop()))
        self.ib = None
        self.acc: dict[str, str] = {}             # public key -> real account id
        self._contracts: dict[str, object] = {}   # conId -> contract (from the portfolio)
        self._pnl: dict[tuple, object] = {}       # (account, conId) -> PnLSingle (live-updating)
        self._tickers: dict[str, object] = {}
        self._ticks: dict[str, list] = {}         # conId -> [(lowEdge, increment), ...]
        self._trades: dict[str, object] = {}
        self._lock = threading.Lock()

    def _run(self, fn, *a, **k):
        return self._ex.submit(fn, *a, **k).result(timeout=60)

    # ── connection ───────────────────────────────────────────────────────
    def connect(self) -> None:
        self._run(self._connect)

    def _connect(self):
        if self._ib_factory is None:
            try:
                from ib_async import IB, LimitOrder
            except ImportError as e:
                raise BrokerError("ib_async is not installed: pip install ib_async") from e
            self._ib_factory, self._limit_cls = IB, LimitOrder
        self.ib = self._ib_factory()
        try:
            self.ib.connect(self.host, self.port, clientId=self.client_id, readonly=self.readonly, timeout=8)
        except Exception as e:
            raise BrokerError(f"TWS / IB Gateway not reachable at {self.host}:{self.port} — start it, log in, "
                              f"and enable 'ActiveX and Socket Clients' ({scrub(e)})") from e
        self.ib.reqMarketDataType(self.market_data_type)
        for a in self.ib.managedAccounts():
            self.acc[f"IB-{str(a)[-4:]}"] = a
        if not self.acc:
            raise BrokerError("connected, but the gateway reports no accounts")

    def close(self) -> None:
        """Disconnect but keep the worker thread, so a reconnect can reuse it."""
        if self.ib is not None:
            try:
                self._run(self.ib.disconnect)
            except Exception:
                pass
        self.ib = None
        self.acc.clear()
        self._contracts.clear()
        self._pnl.clear()
        self._tickers.clear()

    # ── read side ────────────────────────────────────────────────────────
    def snapshot(self) -> list[AccountSnapshot]:
        return self._run(self._snapshot)

    def _snapshot(self):
        out = []
        for key, a in self.acc.items():
            vals = [v for v in self.ib.accountSummary(a) if v.tag == "NetLiquidation"]
            if not vals:
                out.append(AccountSnapshot(key, key, None, ok=False, error="no NetLiquidation in account summary"))
                continue
            v = vals[0]
            eq, ccy = _num(v.value), v.currency
            if eq is not None and ccy not in ("USD",):
                # account base currency isn't USD: ExchangeRate(USD) = base units per 1 USD
                rate = [_num(x.value) for x in self.ib.accountValues(a) if x.tag == "ExchangeRate" and x.currency == "USD"]
                eq = eq / rate[0] if rate and rate[0] else None
            out.append(AccountSnapshot(key, key, eq, ok=eq is not None,
                                       error=None if eq is not None else f"can't convert {ccy} to USD"))
        return out

    def positions(self) -> list[Position]:
        return self._run(self._positions)

    def _positions(self):
        out = []
        for key, a in self.acc.items():
            for it in self.ib.portfolio(a):
                if not it.position:
                    continue
                c = it.contract
                inst = instrument_from_contract(c)
                self._contracts[inst.key] = c
                pk = (a, int(c.conId))
                if pk not in self._pnl:
                    self._pnl[pk] = self.ib.reqPnLSingle(a, "", int(c.conId))
                avg = _num(it.averageCost)
                if avg is not None and inst.multiplier:
                    avg = avg / inst.multiplier           # IB's averageCost includes the multiplier
                out.append(Position(account_id=key, instrument=inst, qty=float(it.position), avg_price=avg,
                                    price=_num(it.marketPrice), market_value=_num(it.marketValue),
                                    unrealized_pnl=_num(it.unrealizedPNL), today_pnl=None, closable_qty=None))
        self.ib.sleep(0.5)                                # let the P&L subscriptions deliver
        for p in out:
            ps = self._pnl.get((self.acc[p.account_id], int(p.instrument.key)))
            p.today_pnl = _num(getattr(ps, "dailyPnL", None)) if ps is not None else None
        return out

    def quotes(self, instruments: list[Instrument]) -> dict[str, Quote]:
        return self._run(self._quotes, instruments)

    def _tick_at(self, key: str, contract, price: float | None) -> float:
        if key not in self._ticks:
            rules = []
            try:
                det = self.ib.reqContractDetails(contract)
                ids = str(det[0].marketRuleIds).split(",") if det else []
                if ids and ids[0]:
                    rules = sorted((float(p.lowEdge), float(p.increment)) for p in self.ib.reqMarketRule(int(ids[0])))
            except Exception:
                rules = []
            self._ticks[key] = rules or [(0.0, 0.01)]
        inc = self._ticks[key][0][1]
        for low, step in self._ticks[key]:
            if price is not None and price >= low:
                inc = step
        return inc

    def _quotes(self, instruments):
        out = {}
        for inst in instruments:
            c = self._contracts.get(inst.key)
            if c is None:
                continue
            if not getattr(c, "exchange", ""):
                c.exchange = "SMART"                      # portfolio contracts come without a routing exchange
            if inst.key not in self._tickers:
                self._tickers[inst.key] = self.ib.reqMktData(c, "", False, False)
        self.ib.sleep(1.0)
        for inst in instruments:
            t = self._tickers.get(inst.key)
            if t is None:
                continue
            bid, ask = _num(t.bid), _num(t.ask)
            out[inst.key] = Quote(bid=bid, ask=ask, tick=self._tick_at(inst.key, self._contracts[inst.key], bid or ask),
                                  last=_num(t.last), bid_size=_num(t.bidSize), ask_size=_num(t.askSize),
                                  prev_close=_num(getattr(t, "close", None)))
        return out

    def underlying_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return self._run(self._und_quotes, symbols)

    def _und_quotes(self, symbols):
        from ib_async import Stock          # only reached with a real gateway; tests inject their own
        for s in symbols:
            k = f"und:{s}"
            if k not in self._tickers:
                c = Stock(s, "SMART", "USD")
                self.ib.qualifyContracts(c)
                self._tickers[k] = self.ib.reqMktData(c, "", False, False)
        self.ib.sleep(1.0)
        out = {}
        for s in symbols:
            t = self._tickers.get(f"und:{s}")
            if t is not None:
                out[s] = Quote(bid=_num(t.bid), ask=_num(t.ask), last=_num(t.last), prev_close=_num(getattr(t, "close", None)))
        return out

    def recent_fills(self, days: int = 3) -> list[Fill] | None:
        """The TWS API only hands back this session's executions (older ones live in Flex reports)."""
        return self._run(self._fills)

    def _fills(self):
        out = []
        rev = {v: k for k, v in self.acc.items()}
        for f in self.ib.fills():
            ex, c = f.execution, f.contract
            out.append(Fill(rev.get(ex.acctNumber, "IB"), str(ex.orderId), instrument_from_contract(c).symbol,
                            "BUY" if ex.side in ("BOT", "BUY") else "SELL", float(ex.shares), float(ex.price), ex.time))
        return out

    # cash_flows: the TWS API has no deposit/withdrawal history (that lives in Flex reports) -> base returns None

    # ── trading ──────────────────────────────────────────────────────────
    def place_limit(self, account_id, inst, side, qty, price, session="rth") -> str:
        if self.readonly:
            raise BrokerError("connected read-only: set trading.enabled and untick 'Read-Only API' in TWS")
        return self._run(self._place, account_id, inst, side, qty, price, session)

    def _place(self, account_id, inst, side, qty, price, session):
        c = self._contracts.get(inst.key)
        if c is None:
            raise BrokerError(f"no contract for {inst.symbol}")
        o = self._limit_cls("SELL" if side == Side.SELL else "BUY", qty, price)
        o.account, o.tif = self.acc[account_id], "DAY"
        o.outsideRth = session in ("pre", "post")
        tr = self.ib.placeOrder(c, o)
        oid = str(tr.order.orderId)
        self._trades[oid] = tr
        return oid

    def modify_price(self, account_id, order_id, inst, qty_total, price) -> None:
        self._run(self._modify, order_id, price)

    def _modify(self, order_id, price):
        tr = self._trades.get(order_id)
        if tr is None:
            raise BrokerError(f"unknown order {order_id}")
        tr.order.lmtPrice = price
        self.ib.placeOrder(tr.contract, tr.order)         # same orderId = modify

    def cancel(self, account_id, order_id) -> None:
        self._run(self._cancel, order_id)

    def _cancel(self, order_id):
        tr = self._trades.get(order_id)
        if tr is None:
            raise BrokerError(f"unknown order {order_id}")
        self.ib.cancelOrder(tr.order)

    def order_states(self, account_id, order_ids) -> dict[str, OrderState]:
        return self._run(self._states, order_ids)

    def _states(self, order_ids):
        self.ib.sleep(0.2)
        out = {}
        for oid in order_ids:
            tr = self._trades.get(oid)
            if tr is None:
                continue
            st = tr.orderStatus
            out[oid] = OrderState(oid, _STATUS.get(st.status, "unknown"), float(st.filled or 0),
                                  _num(st.avgFillPrice), st.status)
        return out
