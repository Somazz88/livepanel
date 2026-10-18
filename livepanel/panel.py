"""The poller: pulls from the broker(s), keeps the last good state, works out today / week / month.

Outside market hours it stops pulling and serves the last snapshot, labelled with when it was taken
("frozen"): option quotes cannot change after the bell, and re-pulling them only produces noise.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .brokers import Composite
from .models import AssetClass, CashFlow
from .pnl import EquityStore, period_pnl
from .sessions import calendar_stale, market_live, trading_day_of, us_session
from .view import account_today, daily_funding, group_positions, merge_fills, spark
from .util import scrub

FLOWS_EVERY_SEC = 600
SPARK_DAYS = 60                # trading days shown on the equity chart
FILLS_EVERY_SEC = 300          # outside the session
FILLS_LIVE_SEC = 60            # during it — and immediately whenever the positions change


class Panel:
    def __init__(self, broker: Composite, cfg):
        self.b, self.cfg = broker, cfg
        self.store = EquityStore(cfg.data_dir)
        self.tz = ZoneInfo(str(cfg.get("panel", "display_tz")))
        self.state: dict = {"ok": False, "message": "starting…"}
        self.positions = []
        self._flows, self._flows_at, self._flows_ready, self._flows_busy = None, 0.0, False, False
        self._fills, self._fills_at, self._fills_busy = None, 0.0, False
        self._pos_sig = None
        self._joins_f = cfg.data_dir / "joins.json"
        self._joins = _read_json(self._joins_f)          # {login label: {"day", "equity"}}: logins added later
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._force = threading.Event()

    # ── loop ─────────────────────────────────────────────────────────────
    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="poller").start()

    def refresh_now(self) -> None:
        self._force.set()

    def _loop(self):
        every = float(self.cfg.get("panel", "refresh_sec"))
        while not self._stop.is_set():
            forced = self._force.is_set()
            self._force.clear()
            try:
                self.tick(force=forced)
            except Exception as e:                          # keep serving the last good state
                with self._lock:
                    self.state = {**self.state, "ok": False, "message": f"refresh failed: {scrub(e)}"}
            self._force.wait(every)

    def _held_classes(self) -> set:
        return {p.instrument.asset_class for p in self.positions} or {AssetClass.OPTION, AssetClass.STOCK}

    def tick(self, now: datetime | None = None, force: bool = False) -> dict:
        now = now or datetime.now().astimezone()
        live = market_live(now, self._held_classes(), int(self.cfg.get("panel", "lead_min")),
                           int(self.cfg.get("panel", "grace_min")),
                           extended=bool(self.cfg.get("trading", "allow_extended_hours")))
        have = bool(self.state.get("ok"))
        if have and not live and not force:
            with self._lock:
                self.state = {**self.state, "frozen": True}
            return self.state
        if self.b.errors:
            self.b.reconnect_failed()
        snaps = self.b.snapshot()
        positions = self.b.positions()
        self.positions = positions
        equity = sum(s.equity for s in snaps if s.ok and s.equity is not None)
        all_ok = all(s.ok for s in snaps) and not self.b.errors and bool(snaps)
        if all_ok and live:
            # only inside the refresh window: a pull at 21:43 ET is an after-hours mark, not the close
            self.store.record(now, equity)
        base = str(self.cfg.get("panel", "base_currency"))

        today_vals = [p.today_pnl for p in positions if p.instrument.currency == base]
        today = sum(v for v in today_vals if v is not None) if today_vals else 0.0
        missing_today = sum(1 for v in today_vals if v is None)
        other_ccy = [{"symbol": p.instrument.symbol, "ccy": p.instrument.currency, "today": p.today_pnl}
                     for p in positions if p.instrument.currency != base and p.today_pnl]

        day = trading_day_of(now)
        closes = self.store.closes(int(self.cfg.get("panel", "grace_min")))
        periods = {}
        funding = []
        if all_ok:
            marks = []
            for start in (day - timedelta(days=day.weekday()), day.replace(day=1)):
                before = [d for d in closes if d < start.isoformat()]
                if before:
                    marks.append(max(before))
            if marks:
                stale = time.monotonic() - self._flows_at > FLOWS_EVERY_SEC
                if (stale or not self._flows_ready) and not self._flows_busy:
                    # deposits come one API call per account per day on moomoo (~2 min cold, cached after):
                    # fetch them off the refresh path so positions show up immediately
                    # from the earliest point the chart shows, so its deposit / withdrawal markers are right too
                    chart_from = sorted(closes)[-SPARK_DAYS:][0] if closes else min(marks)
                    first = datetime.fromisoformat(min(min(marks), chart_from)).date() + timedelta(days=1)
                    self._flows_busy = True
                    threading.Thread(target=self._fetch_flows, args=(first, day), daemon=True).start()
                if self._flows_ready:
                    rates = {**{k: float(v) for k, v in (self.cfg.raw.get("fx") or {}).items()}, **self.b.fx_rates()}
                    flows = self._with_joins(snaps, day, base)
                    periods = period_pnl(day, equity, closes, flows, rates, base)
                    funding = daily_funding(flows, rates, base)
                else:
                    periods = {"loading": True}

        # ── the grouped view (one row per underlying) ──
        syms = sorted({(p.instrument.underlying or p.instrument.symbol) for p in positions
                       if p.instrument.market == "US" and p.instrument.asset_class in (AssetClass.OPTION, AssetClass.STOCK)})
        try:
            und = self.b.underlying_quotes(syms) if syms else {}
        except Exception:
            und = {}
        # before the open, brokers still report the day-before's close as prev_close -> the % would be wrong
        groups, foot = group_positions(positions, und, day, show_und=us_session(now) != "pre", base=base)
        # 10-02 side-by-side check: a buy at 22:45 was missing from the list 3 minutes later (5-min cadence).
        # A trade changes the positions, so refetch as soon as they change.
        sig = tuple(sorted((p.account_id, p.instrument.key, p.qty) for p in positions))
        changed, self._pos_sig = sig != self._pos_sig, sig
        every = FILLS_LIVE_SEC if live else FILLS_EVERY_SEC
        if (changed or time.monotonic() - self._fills_at > every) and not self._fills_busy:
            self._fills_busy = True
            threading.Thread(target=self._fetch_fills, daemon=True).start()
        fills, fills_today = merge_fills(self._fills or [], self.tz, day) if self._fills is not None else ([], 0)

        rows = [{"key": f"{p.account_id}|{p.instrument.key}", "account": p.account_id, "symbol": p.instrument.symbol,
                 "class": p.instrument.asset_class.value, "market": p.instrument.market, "ccy": p.instrument.currency,
                 "qty": p.qty, "closable": p.closable_qty, "avg": p.avg_price, "price": p.price,
                 "value": p.market_value, "pnl": p.unrealized_pnl, "today": p.today_pnl,
                 "mult": p.instrument.multiplier}
                for p in sorted(positions, key=lambda p: (p.account_id, p.instrument.asset_class.value, p.instrument.symbol))]
        st = {
            # pulled outside the refresh window (e.g. started after the close): these are after-hours marks,
            # not the closing ones — the page must not call them "closing marks"
            "ok": all_ok, "frozen": not live, "pulled_live": live,
            "as_of": now.astimezone(self.tz).strftime("%m-%d %H:%M"),
            "trading_day": day.isoformat(), "base": base, "equity": round(equity, 2) if snaps else None,
            "today": round(today, 2), "today_missing": missing_today, "other_ccy": other_ccy,
            "periods": periods, "accounts": [s.__dict__ for s in snaps], "positions": rows,
            "errors": {k: scrub(v) for k, v in self.b.errors.items()},
            "calendar_stale": calendar_stale(day), "message": "",
            "acct_today": account_today(positions, base), "groups": groups, "foot": foot,
            "fills": fills, "fills_today": fills_today, "fills_known": self._fills is not None,
            "spark": spark(closes, round(equity, 2) if all_ok else None, day, SPARK_DAYS),
            "funding": funding,
            "intraday": self.store.day_points(day, self.tz),
        }
        with self._lock:
            self.state = st
        return st

    def _fetch_fills(self):
        first = self._fills is None
        try:
            f = self.b.recent_fills(3)
            if f is not None or self._fills is None:
                self._fills = f
        except Exception:
            pass
        finally:
            self._fills_at, self._fills_busy = time.monotonic(), False
            if first and self._fills is not None:
                self.refresh_now()             # after the close the panel is frozen; show the first load anyway

    def _with_joins(self, snaps, day, base):
        """A login added later (since = ...) brings its whole balance into the totals at once. Book that balance as a
        deposit on the day the panel first saw it, so the week / month numbers and the chart's markers stay honest."""
        if self._flows is None:
            return None                                    # broker can't report deposits: already labelled as such
        try:
            from .brokers import moomoo_logins
            new = [lg["label"] for lg in moomoo_logins(self.cfg) if lg.get("since")] if "moomoo" in self.cfg.brokers else []
        except ValueError:
            new = []
        changed = False
        for label in new:
            if label in self._joins:
                continue
            mine = [s for s in snaps if str(s.account_id).startswith(f"{label}·")]
            if mine and all(s.ok and s.equity is not None for s in mine):
                self._joins[label] = {"day": day.isoformat(), "equity": round(sum(s.equity for s in mine), 2)}
                changed = True
        if changed:
            try:
                self._joins_f.write_text(json.dumps(self._joins, ensure_ascii=False, indent=1), encoding="utf-8")
            except OSError:
                pass
        extra = [CashFlow(f"{label}·", date.fromisoformat(j["day"]), float(j["equity"]), base, "deposit", "added to the panel")
                 for label, j in self._joins.items()]
        return list(self._flows) + extra

    def _fetch_flows(self, first, day):
        try:
            self._flows = self.b.cash_flows(first, day)
        except Exception:
            self._flows = None                 # shown as "excludes deposits", never silently as 0
        finally:
            self._flows_at, self._flows_ready, self._flows_busy = time.monotonic(), True, False
            self.refresh_now()

    def view(self) -> dict:
        with self._lock:
            return dict(self.state)


def _read_json(path) -> dict:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}
