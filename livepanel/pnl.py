"""Equity history and this-week / this-month P&L.

period P&L = equity now - equity at the last close BEFORE the period - net deposits inside the period.
The anchor is the previous period's last close, not the cost of today's positions: anchoring on cost
would count money already made in earlier weeks as this week's.

Brokers don't hand out historical account equity, so the app records its own: every refresh appends
one line to <data_dir>/equity.jsonl. Week/month numbers appear once a close before the period exists.
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from .models import CashFlow
from .sessions import NY, is_trading_day, rth_close


class EquityStore:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "equity.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_write: datetime | None = None

    def record(self, now: datetime, equity: float, min_gap_sec: int = 60) -> None:
        with self._lock:
            if self._last_write and (now - self._last_write).total_seconds() < min_gap_sec:
                return
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": now.isoformat(timespec="seconds"), "equity": round(equity, 2)}) + "\n")
            self._last_write = now

    def closes(self, grace_min: int = 15) -> dict[str, float]:
        """{ET trading day: last equity recorded up to `grace_min` after that day's close}.
        Later rows of the same ET day are after-hours marks (option marks keep drifting after the bell)
        and must not become next period's anchor. Non-trading days are skipped."""
        out: dict[str, float] = {}
        try:
            with self.path.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        et = datetime.fromisoformat(r["ts"]).astimezone(NY)
                    except (ValueError, KeyError):
                        continue
                    d = et.date()
                    if not is_trading_day(d):
                        continue
                    cut = datetime.combine(d, rth_close(d), NY) + timedelta(minutes=grace_min)
                    if et <= cut:
                        out[d.isoformat()] = float(r["equity"])
        except OSError:
            pass
        return out


def to_base(amount: float, ccy: str, rates: dict[str, float], base: str = "USD") -> float | None:
    """rates = {ccy: units of ccy per 1 base}. Unknown currency -> None (excluded, and the caller says so)."""
    if ccy == base:
        return amount
    r = rates.get(ccy)
    return amount / r if r else None


def period_pnl(today_et: date, equity_now: float | None, closes: dict[str, float],
               flows: list[CashFlow] | None, rates: dict[str, float], base: str = "USD") -> dict:
    """{'week': {...}, 'month': {...}}; a period is missing when no close before it was recorded.
    flows=None means the broker can't report deposits -> the number is labelled 'excludes deposits'."""
    if equity_now is None:
        return {}
    out = {}
    for key, start in (("week", today_et - timedelta(days=today_et.weekday())), ("month", today_et.replace(day=1))):
        prior = [d for d in closes if d < start.isoformat()]
        if not prior:
            continue
        mark = max(prior)
        net, unconverted = 0.0, []
        for f in flows or []:
            if f.kind not in ("deposit", "withdrawal"):
                continue
            if mark < f.day.isoformat() <= today_et.isoformat():
                v = to_base(f.amount, f.currency, rates, base)
                if v is None:
                    unconverted.append(f.currency)
                else:
                    net += v
        out[key] = {"pnl": round(equity_now - closes[mark] - net, 2), "from": mark,
                    "start_equity": round(closes[mark], 2), "net_deposits": round(net, 2),
                    "flows_known": flows is not None, "unconverted": sorted(set(unconverted))}
    return out
