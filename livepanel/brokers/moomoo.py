"""moomoo / Futu via OpenD (pip install moomoo-api).

The user runs OpenD and logs in there; this adapter only talks to OpenD on localhost and never sees
a password. Live trading on the GUI OpenD has to be unlocked by hand in the OpenD window.

Accounts are discovered, not configured: every SecurityFirm is asked for its account list and the
REAL accounts that belong to it are kept (paper mode keeps the US stock+option simulate account).
Each account gets a public key like "SG-1234"; the full account number never leaves this module.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, timedelta
from pathlib import Path

from ..models import AccountSnapshot, AssetClass, CashFlow, Fill, Instrument, OrderState, Position, Quote, Side
from ..util import scrub
from .base import Broker, BrokerError

OPT_RE = re.compile(r"^US\.([A-Z.]+?)(\d{2})(\d{2})(\d{2})([CP])(\d+)$")
FIRM_SHORT = {"FUTUSG": "SG", "FUTUMY": "MY", "FUTUINC": "US", "FUTUSECURITIES": "HK",
              "FUTUAU": "AU", "FUTUCA": "CA", "FUTUJP": "JP"}
_DONE = {"FILLED_ALL": "filled", "CANCELLED_ALL": "cancelled", "CANCELLED_PART": "cancelled",
         "FAILED": "rejected", "SUBMIT_FAILED": "rejected", "DELETED": "cancelled", "DISABLED": "cancelled"}


def instrument_from_code(code: str, currency: str, name: str = "") -> Instrument:
    m = OPT_RE.match(code)
    if m:
        und, yy, mm, dd, right, k = m.groups()
        strike = int(k) / 1000
        exp = date(2000 + int(yy), int(mm), int(dd))
        return Instrument(key=code, symbol=f"{und} {mm}/{dd} {strike:g}{right}", asset_class=AssetClass.OPTION,
                          currency=currency, market="US", multiplier=100.0, underlying=und,
                          expiry=exp, strike=strike, right=right)
    market, _, sym = code.partition(".")
    return Instrument(key=code, symbol=sym or code, asset_class=AssetClass.STOCK, currency=currency,
                      market=market or "?", multiplier=1.0)


def classify_flow(cf_type: str, remark: str) -> str:
    """moomoo cash-flow rows -> deposit / withdrawal / fx / other.
    Transfers between accounts of the same customer are not deposits (money moved, it didn't arrive)."""
    t, rm = (cf_type or "").lower(), (remark or "").strip()
    if "INTER-ACCOUNT TRANSFER" in rm.upper():
        return "other"
    if "withdraw" in t:
        return "withdrawal"
    if "deposit" in t and "coupon" not in t:
        return "deposit"
    if rm.upper().startswith("WISE_PAY"):
        return "deposit"
    if "currency exchange" in t:
        return "fx"
    return "other"


def _f(x):
    try:
        v = float(x)
        return None if v != v else v          # NaN -> None
    except (TypeError, ValueError):
        return None


class MoomooBroker(Broker):
    name = "moomoo"
    # docs: place 15 / 30 s, modify 20 / 30 s per account — keep 2 spare of each
    rate_limits = {"place": (13, 30.0), "modify": (18, 30.0)}

    def __init__(self, host: str = "127.0.0.1", port: int = 11111, paper: bool = False,
                 include_futures: bool = True, cache_dir: Path | None = None):
        self.host, self.port, self.paper, self.include_futures = host, port, paper, include_futures
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.acc: dict[str, tuple] = {}          # public key -> (trade ctx, acc_id, firm name)
        self._ctx: list = []
        self._lock = threading.RLock()            # one OpenD connection set, used from several threads
        self.m = None
        self.q = None

    # ── connection ───────────────────────────────────────────────────────
    def connect(self) -> None:
        try:
            import moomoo as m
        except ImportError as e:
            raise BrokerError("moomoo-api is not installed: pip install moomoo-api") from e
        self.m = m
        with self._lock:
            self.q = m.OpenQuoteContext(host=self.host, port=self.port)
            ret, st = self.q.get_global_state()
            if ret != m.RET_OK:
                self.close()
                raise BrokerError(f"OpenD not reachable at {self.host}:{self.port} — start OpenD and log in ({scrub(st)})")
            env = "SIMULATE" if self.paper else "REAL"
            seen_sim = False
            for fname in FIRM_SHORT:
                firm = getattr(m.SecurityFirm, fname, None)
                if firm is None:
                    continue
                tc = m.OpenSecTradeContext(filter_trdmarket=m.TrdMarket.NONE, host=self.host, port=self.port,
                                           security_firm=firm)
                ret, df = tc.get_acc_list()
                keep = []
                if ret == m.RET_OK and len(df):
                    for r in df.itertuples():
                        if str(r.trd_env) != env or str(getattr(r, "acc_status", "ACTIVE")) != "ACTIVE":
                            continue
                        if self.paper:
                            # the simulate accounts are shared across firms; keep the US stock+option one once
                            if seen_sim or "OPTION" not in str(getattr(r, "sim_acc_type", "")):
                                continue
                            seen_sim = True
                        elif str(getattr(r, "security_firm", fname)) != fname:
                            continue
                        keep.append(int(r.acc_id))
                if keep:
                    self._ctx.append(tc)
                    for aid in keep:
                        self.acc[f"{'PAPER' if self.paper else FIRM_SHORT[fname]}-{str(aid)[-4:]}"] = (tc, aid, fname)
                else:
                    tc.close()
            if not self.acc:
                self.close()
                raise BrokerError("OpenD is up but no active accounts were found for this login"
                                  + (" (paper: no US stock+option simulate account)" if self.paper else ""))

    def close(self) -> None:
        with self._lock:
            for c in self._ctx + ([self.q] if self.q else []):
                try:
                    c.close()
                except Exception:
                    pass
            self._ctx, self.q = [], None

    def _env(self):
        return self.m.TrdEnv.SIMULATE if self.paper else self.m.TrdEnv.REAL

    # ── read side ────────────────────────────────────────────────────────
    def snapshot(self) -> list[AccountSnapshot]:
        m, out = self.m, []
        with self._lock:
            for key, (tc, aid, fname) in self.acc.items():
                ret, a = tc.accinfo_query(trd_env=self._env(), acc_id=aid, currency=m.Currency.USD)
                if ret != m.RET_OK or not len(a):
                    out.append(AccountSnapshot(key, key, None, ok=False, error=scrub(a), paper=self.paper))
                    continue
                eq = _f(a.iloc[0]["total_assets"])
                label = key
                if self.include_futures and not self.paper:
                    fut = self._futures_usd(fname)
                    if fut is None:
                        label += " (futures unknown)"
                    elif fut:
                        eq = (eq or 0) + fut
                        label += f" (+futures {fut:,.0f})"
                out.append(AccountSnapshot(key, label, eq, paper=self.paper))
        return out

    def _futures_usd(self, fname: str) -> float | None:
        """A universal account can hold a separate futures account (only visible through the futures
        context). Money moved there is an internal transfer, so its equity belongs to this account.
        0 = no futures account; None = couldn't tell."""
        m = self.m
        try:
            tc = m.OpenFutureTradeContext(host=self.host, port=self.port, security_firm=getattr(m.SecurityFirm, fname))
        except Exception:
            return None
        try:
            ret, df = tc.get_acc_list()
            if ret != m.RET_OK:
                return None
            total = 0.0
            for r in df.itertuples():
                if str(r.trd_env) != "REAL" or str(getattr(r, "security_firm", fname)) not in (fname, "N/A"):
                    continue
                ret, a = tc.accinfo_query(trd_env=m.TrdEnv.REAL, acc_id=int(r.acc_id), currency=m.Currency.USD)
                if ret != m.RET_OK or not len(a):
                    return None
                total += _f(a.iloc[0]["total_assets"]) or 0.0
            return round(total, 2)
        finally:
            tc.close()

    def positions(self) -> list[Position]:
        m, out = self.m, []
        with self._lock:
            for key, (tc, aid, _) in self.acc.items():
                ret, df = tc.position_list_query(trd_env=self._env(), acc_id=aid)
                if ret != m.RET_OK:
                    raise BrokerError(f"{key}: position query failed: {scrub(df)}")
                for r in df.itertuples():
                    qty = _f(r.qty) or 0.0
                    if str(getattr(r, "position_side", "LONG")) == "SHORT" and qty > 0:
                        qty = -qty
                    if qty == 0 and not (_f(r.today_pl_val) or 0):
                        continue        # a leg closed today keeps qty 0 + today_pl_val: its P&L belongs in today's total
                    inst = instrument_from_code(str(r.code), str(r.currency), str(getattr(r, "stock_name", "")))
                    out.append(Position(
                        account_id=key, instrument=inst, qty=qty,
                        avg_price=_f(getattr(r, "average_cost", None)), price=_f(r.nominal_price),
                        market_value=_f(r.market_val), unrealized_pnl=_f(r.pl_val),
                        today_pnl=_f(r.today_pl_val), closable_qty=_f(r.can_sell_qty)))
        return out

    def quotes(self, instruments: list[Instrument]) -> dict[str, Quote]:
        m, out = self.m, {}
        codes = sorted({i.key for i in instruments})
        self.quote_errors = {}
        with self._lock:
            for i in range(0, len(codes), 200):
                batch = codes[i:i + 200]
                ret, df = self.q.get_market_snapshot(batch)
                if ret != m.RET_OK:
                    # one code without quote permission (e.g. MY.4456 on an account without Bursa quotes)
                    # fails the whole batch — ask one by one so the rest still get prices
                    for code in batch:
                        r1, d1 = self.q.get_market_snapshot([code])
                        if r1 == m.RET_OK:
                            self._put(out, d1)
                        else:
                            self.quote_errors[code] = scrub(d1, 100)
                    continue
                self._put(out, df)
        return out

    @staticmethod
    def _put(out: dict, df) -> None:
        for r in df.itertuples():
            out[str(r.code)] = Quote(bid=_f(r.bid_price), ask=_f(r.ask_price),
                                     tick=_f(r.price_spread) or 0.01, last=_f(r.last_price),
                                     bid_size=_f(getattr(r, "bid_vol", None)),
                                     ask_size=_f(getattr(r, "ask_vol", None)),
                                     prev_close=_f(getattr(r, "prev_close_price", None)))

    def underlying_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        insts = [Instrument(key=f"US.{s}", symbol=s, asset_class=AssetClass.STOCK) for s in symbols]
        q = self.quotes(insts)
        return {s: q[f"US.{s}"] for s in symbols if f"US.{s}" in q}

    def recent_fills(self, days: int = 3) -> list[Fill] | None:
        """history_deal_list_query + today's deal_list_query, de-duplicated by deal id.
        moomoo stamps US deals in exchange time (ET)."""
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        if self.paper:
            return []
        m, ny, out = self.m, ZoneInfo("America/New_York"), []
        end = date.today() + timedelta(days=1)
        start = end - timedelta(days=days + 1)
        with self._lock:
            for key, (tc, aid, _) in self.acc.items():
                seen = set()
                for call in (lambda: tc.history_deal_list_query(start=f"{start} 00:00:00", end=f"{end} 23:59:59",
                                                                trd_env=self._env(), acc_id=aid),
                             lambda: tc.deal_list_query(trd_env=self._env(), acc_id=aid)):
                    ret, df = call()
                    if ret != m.RET_OK:
                        return None
                    for r in df.itertuples():
                        did = str(r.deal_id)
                        if did in seen:
                            continue
                        seen.add(did)
                        try:
                            t = _dt.fromisoformat(str(r.create_time)[:19]).replace(tzinfo=ny)
                        except ValueError:
                            continue
                        inst = instrument_from_code(str(r.code), "USD")
                        out.append(Fill(key, str(r.order_id), inst.symbol, "BUY" if "BUY" in str(r.trd_side) else "SELL",
                                        _f(r.qty) or 0.0, _f(r.price) or 0.0, t))
        return out

    # ── cash flows (cached per account per clearing day; past days never change) ──
    def _cache_path(self) -> Path | None:
        return self.cache_dir / "moomoo_cashflow_cache.json" if self.cache_dir else None

    def _load_cache(self) -> dict:
        p = self._cache_path()
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p and p.exists() else {}
        except (OSError, ValueError):
            return {}

    def _save_cache(self, c: dict) -> None:
        p = self._cache_path()
        if p:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(c), encoding="utf-8")

    def cash_flows(self, start: date, end: date) -> list[CashFlow] | None:
        if self.paper:
            return []
        m = self.m
        cache = self._load_cache()
        today = date.today()
        out: list[CashFlow] = []
        with self._lock:
            for key, (tc, aid, _) in self.acc.items():
                d = start
                while d <= end:
                    ck = f"{key}|{d.isoformat()}"
                    rows = cache.get(ck)
                    if rows is None or d >= today - timedelta(days=1):    # last two days can still change
                        rows = None
                        for attempt in range(4):
                            ret, df = tc.get_acc_cash_flow(clearing_date=d.isoformat(), trd_env=self._env(), acc_id=aid)
                            if ret == m.RET_OK:
                                rows = [[str(r.currency), str(r.cashflow_type), _f(r.cashflow_amount) or 0.0,
                                         str(r.cashflow_remark or "")] for r in df.itertuples()]
                                break
                            if "frequen" in str(df).lower():
                                time.sleep(5 * (attempt + 1))
                                continue
                            break
                        if rows is None:
                            return None          # can't vouch for the period -> caller labels it
                        cache[ck] = rows
                        time.sleep(1.6)          # 20 calls / 30 s
                    for ccy, typ, amt, rm in rows:
                        out.append(CashFlow(key, d, amt, ccy, classify_flow(typ, rm), rm[:60]))
                    d += timedelta(days=1)
        self._save_cache(cache)
        return out

    def fx_rates(self) -> dict[str, float]:
        """Weighted rate from the account's own conversions (remark 'MYR -> USD'): units of ccy per 1 USD."""
        rows = [r for v in self._load_cache().values() for r in v]
        out = {}
        for ccy in {r[0] for r in rows} - {"USD"}:
            paid = sum(-r[2] for r in rows if r[0] == ccy and r[2] < 0 and f"{ccy} -> USD" in r[3])
            got = sum(r[2] for r in rows if r[0] == "USD" and r[2] > 0 and f"{ccy} -> USD" in r[3])
            if paid and got:
                out[ccy] = paid / got
        return out

    # ── trading ──────────────────────────────────────────────────────────
    def _tc(self, account_id: str):
        if account_id not in self.acc:
            raise BrokerError(f"unknown account {account_id}")
        return self.acc[account_id]

    def place_limit(self, account_id, inst, side, qty, price, session="rth") -> str:
        m = self.m
        tc, aid, _ = self._tc(account_id)
        kw = {}
        if inst.asset_class == AssetClass.STOCK and inst.market == "US" and session in ("pre", "post"):
            kw["session"] = m.Session.ETH          # outside regular hours: limit orders only
        with self._lock:
            ret, d = tc.place_order(price=price, qty=qty, code=inst.key,
                                    trd_side=m.TrdSide.SELL if side == Side.SELL else m.TrdSide.BUY,
                                    order_type=m.OrderType.NORMAL, trd_env=self._env(), acc_id=aid,
                                    remark="livepanel-exit", **kw)
        if ret != m.RET_OK:
            raise BrokerError(scrub(d))
        return str(d.iloc[0]["order_id"])

    def modify_price(self, account_id, order_id, inst, qty_total, price) -> None:
        # moomoo: qty is the order's TOTAL quantity (including any filled part), not the remainder
        m = self.m
        tc, aid, _ = self._tc(account_id)
        with self._lock:
            ret, d = tc.modify_order(m.ModifyOrderOp.NORMAL, order_id, qty_total, price,
                                     trd_env=self._env(), acc_id=aid)
        if ret != m.RET_OK:
            raise BrokerError(scrub(d))

    def cancel(self, account_id, order_id) -> None:
        m = self.m
        tc, aid, _ = self._tc(account_id)
        with self._lock:
            ret, d = tc.modify_order(m.ModifyOrderOp.CANCEL, order_id, 0, 0, trd_env=self._env(), acc_id=aid)
        if ret != m.RET_OK:
            raise BrokerError(scrub(d))

    def order_states(self, account_id, order_ids) -> dict[str, OrderState]:
        m = self.m
        tc, aid, _ = self._tc(account_id)
        with self._lock:
            ret, df = tc.order_list_query(trd_env=self._env(), acc_id=aid)
        if ret != m.RET_OK:
            raise BrokerError(scrub(df))
        want, out = set(order_ids), {}
        for r in df.itertuples():
            oid = str(r.order_id)
            if oid in want:
                raw = str(r.order_status)
                out[oid] = OrderState(oid, _DONE.get(raw, "working"), _f(r.dealt_qty) or 0.0,
                                      _f(getattr(r, "dealt_avg_price", None)), raw)
        return out
