"""Turn positions / fills / equity history into what the page shows. Pure functions, no broker calls.

Layout follows the compact window this project grew out of:
  - one row per underlying: the stock's move today, market value, today's P&L, position P&L %
  - click a row for its legs, one per account: strike, size, days to expiry, cost -> price
  - option groups first by market value, stock-only groups after, groups closed today last
"""
from __future__ import annotations

from datetime import date, datetime

from .models import AssetClass, Fill, Position, Quote

HOT_PCT = -40.0          # a position down this much gets a red cell


def _group_key(p: Position) -> str:
    i = p.instrument
    if i.asset_class == AssetClass.OPTION and i.underlying:
        return i.underlying
    return i.symbol


def account_today(positions: list[Position], base: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for p in positions:
        if p.instrument.currency == base and p.today_pnl is not None:
            out[p.account_id] = out.get(p.account_id, 0.0) + p.today_pnl
    return {k: round(v, 2) for k, v in out.items()}


def group_positions(positions: list[Position], und: dict[str, Quote], today_et: date, show_und: bool,
                    base: str = "USD") -> tuple[list[dict], dict]:
    groups: dict[str, dict] = {}
    expiries = set()
    for p in positions:
        i = p.instrument
        if i.currency != base:
            continue                                   # other currencies are listed separately, not summed in
        g = groups.setdefault(_group_key(p), {"tk": _group_key(p), "mv": 0.0, "today": 0.0, "pl": 0.0, "legs": [],
                                              "closed": [], "has_opt": False})
        g["today"] += p.today_pnl or 0.0
        if p.qty == 0:
            if p.today_pnl:
                g["closed"].append({"what": _what(p), "today": round(p.today_pnl, 2), "account": p.account_id})
            continue
        if i.asset_class == AssetClass.OPTION:
            g["has_opt"] = True
            if i.expiry:
                expiries.add(i.expiry)
        g["mv"] += p.market_value or 0.0
        g["pl"] += p.unrealized_pnl or 0.0
        g["legs"].append({
            "key": f"{p.account_id}|{i.key}", "account": p.account_id, "acct": str(p.account_id).split("-")[0],
            "what": _what(p), "qty": p.qty, "closable": p.closable_qty if p.closable_qty is not None else abs(p.qty),
            "side": p.side_to_close.value, "dte": (i.expiry - today_et).days if i.expiry else None,
            "cost": p.avg_price, "px": p.price, "today": p.today_pnl, "mv": p.market_value, "pl": p.unrealized_pnl,
            "symbol": i.symbol})
    out = []
    for g in groups.values():
        if not g["legs"] and not any(abs(c["today"]) > 0.5 for c in g["closed"]):
            continue
        cost = g["mv"] - g["pl"]
        pct = g["pl"] / cost * 100 if g["legs"] and cost > 0 else None
        q = und.get(g["tk"]) if show_und else None
        chg = None
        if q and q.last and q.prev_close:
            chg = (q.last / q.prev_close - 1) * 100
        g["legs"].sort(key=lambda l: (l["what"], l["acct"]))
        out.append({**g, "mv": round(g["mv"], 2), "today": round(g["today"], 2), "pl": round(g["pl"], 2),
                    "cost": round(cost, 2), "pct": None if pct is None else round(pct, 1),
                    "hot": pct is not None and pct <= HOT_PCT, "und_chg": None if chg is None else round(chg, 2)})
    out.sort(key=lambda g: (0 if g["has_opt"] else (1 if g["legs"] else 2), -g["mv"]))
    big = max((abs(g["today"]) for g in out), default=0) or 1
    for g in out:
        g["bar"] = round(abs(g["today"]) / big, 3)
    foot = {"mv": round(sum(g["mv"] for g in out), 2),
            "expiry": next(iter(expiries)).isoformat() if len(expiries) == 1 else None}
    return out, foot


def _what(p: Position) -> str:
    i = p.instrument
    if i.asset_class == AssetClass.OPTION and i.strike is not None:
        return f"{i.expiry:%m/%d} {i.strike:g}{i.right}" if i.expiry else f"{i.strike:g}{i.right}"
    return "stock"


def merge_fills(fills: list[Fill], tz, today_et: date, n: int = 10) -> tuple[list[dict], int]:
    """Several fills of one order -> one row at the volume-weighted price, newest first."""
    orders: dict[tuple, dict] = {}
    for f in fills:
        k = (f.account_id, f.order_id)
        o = orders.setdefault(k, {"account": f.account_id, "acct": str(f.account_id).split("-")[0], "symbol": f.symbol,
                                  "side": f.side, "qty": 0.0, "amt": 0.0, "time": f.time})
        o["qty"] += f.qty
        o["amt"] += f.qty * f.price
        o["time"] = min(o["time"], f.time)
    rows = sorted(orders.values(), key=lambda o: o["time"], reverse=True)
    n_today = sum(1 for o in rows if o["time"].astimezone(_ny()).date() == today_et)
    out = []
    for o in rows[:n]:
        out.append({"acct": o["acct"], "symbol": o["symbol"], "side": o["side"], "qty": o["qty"],
                    "avg": round(o["amt"] / o["qty"], 4) if o["qty"] else None,
                    "time": o["time"].astimezone(tz).strftime("%m-%d %H:%M")})
    return out, n_today


def spark(closes: dict[str, float], equity_now: float | None, day: date, n: int = 40) -> list[list]:
    """Daily closes (deposits included, like the broker's own equity curve) plus today's live value."""
    pts = dict(closes)
    if equity_now:
        pts[day.isoformat()] = equity_now
    return [[d, round(v, 2)] for d, v in sorted(pts.items())[-n:]]


def _ny():
    from .sessions import NY
    return NY
