"""Exit engine: works one or more positions out with the ladder in ladder.py.

Orders only start from an explicit request that the web page sends after the user confirms.
Safety rules carried over from the original tool (each one came from a real incident):
  - re-read positions right before the first order; quantity = min(what the user saw, closable now).
    A window can sit open for hours; selling yesterday's quantity can open a new short.
  - can't re-read positions -> place nothing.
  - never a market order; never through the passive side; one tick per step.
  - on stop: fetch fresh order states before cancelling (an order that already filled can't be
    cancelled, and "cancel failed" would be a lie), re-check after a failed cancel, and record
    partial fills that happened during the cancel.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from .brokers.base import Broker
from .ladder import START_FRAC, STEP_SEC, bucket, next_price, start_price
from .models import Position, Side
from .sessions import can_trade
from .util import scrub

TERMINAL = {"filled", "cancelled", "rejected"}
# not at the exchange yet: modifying now just races the submit
PENDING_RAW = {"SUBMITTING", "WAITING_SUBMIT", "PendingSubmit", "ApiPending"}


class Job(threading.Thread):
    def __init__(self, broker: Broker, legs: list[dict], positions: dict[str, Position], log_dir: Path,
                 mode: str, allow_extended: bool = False, step_sec: float = STEP_SEC, now_fn=None,
                 start_frac: float = START_FRAC):
        super().__init__(daemon=True)
        self.start_frac = start_frac
        self.id = uuid.uuid4().hex[:8]
        self.b, self.legs, self.pos = broker, legs, positions
        self.log_dir, self.mode, self.allow_extended, self.step_sec = log_dir, mode, allow_extended, step_sec
        self.now_fn = now_fn or (lambda: datetime.now().astimezone())
        self.stop_flag = threading.Event()
        self.done = threading.Event()
        self.message = ""
        self.closed_check = None                           # after the run: all_closed | some_left | unknown | dry
        self._lock = threading.Lock()

    # ── bookkeeping ──────────────────────────────────────────────────────
    def log(self, ev: dict) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        ev = {"ts": datetime.now().isoformat(timespec="seconds"), "mode": self.mode, "job": self.id, **ev}
        with (self.log_dir / f"{datetime.now():%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")

    def set(self, leg: dict, **kw) -> None:
        with self._lock:
            leg.update(kw)

    def view(self) -> dict:
        with self._lock:
            return {"id": self.id, "mode": self.mode, "done": self.done.is_set(), "message": self.message,
                    "closed_check": self.closed_check,
                    "legs": [{k: v for k, v in l.items() if k != "inst"} for l in self.legs]}

    def _limit(self, kind: str, acc: str):
        br = self.b.broker_for_account(acc) if hasattr(self.b, "broker_for_account") else self.b
        cap, per = br.rate_limits.get(kind, (10, 30.0))
        return bucket(br.name, acc, kind, cap, per)

    # ── run ──────────────────────────────────────────────────────────────
    def run(self):
        try:
            self._run()
            self._confirm_closed()
        except Exception as ex:
            self.message = f"error: {scrub(ex)} — orders already placed are still live at the broker"
            self.log({"ev": "error", "err": scrub(repr(ex), 300)})
        finally:
            self.done.set()

    def _confirm_closed(self):
        """After the job, read the positions again and say what is still held. Order states can say "filled"
        while the account still holds the leg (a different leg, a partial, a lagging position list) — the
        question that matters is "is it gone?", and only the position list answers it."""
        if self.mode == "dry":
            self.closed_check = "dry"                      # a rehearsal never changes the real positions
            return
        left = None
        # the position list can lag a fill by a second or two (scaled with the step, so tests stay fast)
        for wait in (min(2.0, self.step_sec * 2), min(3.0, self.step_sec * 3)):
            time.sleep(wait)
            try:
                now = {f"{p.account_id}|{p.instrument.key}": p for p in self.b.positions()}
            except Exception as ex:
                self.log({"ev": "confirm_fail", "err": scrub(ex)})
                continue
            left = {}
            for leg in self.legs:
                p = now.get(leg["key"])
                same_side = p is not None and (p.qty > 0) == (leg["side"] == "SELL")
                left[leg["key"]] = abs(p.qty) if same_side and p.qty else 0.0
            if not any(left.values()):
                break
        if left is None:
            self.closed_check = "unknown"
            return
        for leg in self.legs:
            self.set(leg, left=left.get(leg["key"], 0.0))
        self.closed_check = "all_closed" if not any(left.values()) else "some_left"
        self.log({"ev": "confirm", "left": {k: v for k, v in left.items() if v}})

    def _run(self):
        b = self.b
        fresh = None
        for _ in range(3):
            try:
                fresh = {f"{p.account_id}|{p.instrument.key}": p for p in b.positions()}
                break
            except Exception as ex:
                self.log({"ev": "recheck_fail", "err": scrub(ex)})
                time.sleep(1.5)
        if fresh is None:
            for leg in self.legs:
                self.set(leg, state="recheck_failed")
            self.message = "could not re-read positions — nothing was placed"
            return

        now = self.now_fn()
        grown = []
        for leg in self.legs:
            p = fresh.get(leg["key"])
            now_q = 0.0
            if p is not None and (p.qty > 0) == (leg["side"] == Side.SELL.value):
                now_q = p.closable_qty if p.closable_qty is not None else abs(p.qty)
            if now_q != leg["qty"]:
                self.log({"ev": "qty_changed", "key": leg["key"], "window": leg["qty"], "now": now_q})
            if now_q < leg["qty"]:
                self.set(leg, qty=now_q)
            elif now_q > leg["qty"]:
                grown.append(f"{leg['name']} +{now_q - leg['qty']:g}")
            ok, sess = can_trade(leg["inst"].asset_class, leg["inst"].market, now, self.allow_extended)
            self.set(leg, session=sess, tradable=ok)

        todo = [l for l in self.legs if l["qty"] > 0 and l["tradable"]]
        qs = b.quotes([l["inst"] for l in todo]) if todo else {}
        for leg in self.legs:
            if leg["qty"] <= 0:
                self.set(leg, state="no_qty")
                continue
            if not leg["tradable"]:
                self.set(leg, state="not_tradable")
                continue
            q = qs.get(leg["inst"].key)
            side = Side(leg["side"])
            if q is None or not q.usable:
                self.set(leg, state="no_quote")
                continue
            while not self._limit("place", leg["account"]).take():
                time.sleep(0.5)
            px = start_price(side, q.bid, q.ask, q.tick or 0.01, self.start_frac)
            try:
                oid = b.place_limit(leg["account"], leg["inst"], side, leg["qty"], px, leg["session"])
                self.set(leg, oid=oid, price=px, start=px, state="placed", filled=0.0, status="working",
                         bid=q.bid, ask=q.ask)
                self.log({"ev": "place", "key": leg["key"], "side": side.value, "qty": leg["qty"], "price": px,
                          "bid": q.bid, "ask": q.ask, "tick": q.tick, "session": leg["session"], "oid": oid})
            except Exception as ex:
                self.set(leg, state="rejected", note=scrub(ex, 80))
                self.log({"ev": "place_fail", "key": leg["key"], "price": px, "err": scrub(ex)})
            time.sleep(0.1)
        n = sum(1 for l in self.legs if l.get("oid"))
        self.message = f"placed {n}" + (f" · added since the window opened (not included): {', '.join(grown)}" if grown else "")

        while not self.stop_flag.is_set():
            live = [l for l in self.legs if l.get("oid") and l.get("status") not in TERMINAL]
            if not live or self.stop_flag.wait(self.step_sec):
                break
            qs = b.quotes([l["inst"] for l in live])
            states = self._states(live)
            for leg in live:
                s = states.get((leg["account"], leg["oid"]))
                side, q = Side(leg["side"]), qs.get(leg["inst"].key)
                if q is not None and q.usable:
                    self.set(leg, bid=q.bid, ask=q.ask)        # before the fill check, so a fill row shows the quote it filled against
                if s and (s.status, s.filled) != (leg.get("status"), leg.get("filled")):
                    self.set(leg, status=s.status, filled=s.filled, avg=s.avg_price)
                    self.log({"ev": "status", "key": leg["key"], "status": s.raw_status, "filled": s.filled,
                              "avg": s.avg_price, "bid": leg.get("bid"), "ask": leg.get("ask")})
                if leg.get("status") in TERMINAL:
                    self.set(leg, state=leg["status"])
                    continue
                if s and s.raw_status in PENDING_RAW:
                    continue
                if q is None or not q.usable:
                    self.set(leg, state="no_quote_hold")
                    continue
                px = next_price(side, leg["price"], q.bid, q.ask, q.tick or 0.01)
                if abs(px - leg["price"]) < 1e-9:
                    self.set(leg, state="at_passive")
                    continue
                if not self._limit("modify", leg["account"]).take():
                    self.set(leg, state="rate_limited")
                    continue
                try:
                    # total quantity of the order, including anything already filled (moomoo semantics)
                    b.modify_price(leg["account"], leg["oid"], leg["inst"], leg["qty"], px)
                    self.log({"ev": "modify", "key": leg["key"], "from": leg["price"], "to": px, "bid": q.bid, "ask": q.ask})
                    self.set(leg, price=px, state="stepping")
                except Exception as ex:
                    self.set(leg, state="modify_failed", note=scrub(ex, 60))
                    self.log({"ev": "modify_fail", "key": leg["key"], "to": px, "err": scrub(ex)})

        if self.stop_flag.is_set():
            self._stop_all()

    def _states(self, legs: list[dict]) -> dict:
        """{(account, order id): state}. Keyed by account too: order ids are only unique within one broker connection
        (two dry-run logins both had "sim-1"; IBKR numbers per client), and one leg must never read another's fills."""
        out = {}
        by_acc: dict[str, list[str]] = {}
        for l in legs:
            if l.get("oid"):
                by_acc.setdefault(l["account"], []).append(l["oid"])
        for acc, oids in by_acc.items():
            try:
                out.update({(acc, k): v for k, v in self.b.order_states(acc, oids).items()})
            except Exception as ex:
                self.log({"ev": "status_fail", "acc": acc, "err": scrub(ex)})
        return out

    def _sync(self):
        st = self._states(self.legs)
        for leg in self.legs:
            s = st.get((leg["account"], leg.get("oid")))
            if s and (s.status, s.filled) != (leg.get("status"), leg.get("filled")):
                self.set(leg, status=s.status, filled=s.filled, avg=s.avg_price)
                self.log({"ev": "status", "key": leg["key"], "status": s.raw_status, "filled": s.filled, "at": "stop"})

    def _stop_all(self):
        self._sync()                                   # it may have filled since the last step
        for leg in self.legs:
            if not leg.get("oid"):
                continue
            if leg.get("status") in TERMINAL:
                self.set(leg, state=leg["status"])
                continue
            try:
                self.b.cancel(leg["account"], leg["oid"])
                self.set(leg, state="cancelled_by_user")
                self.log({"ev": "cancel", "key": leg["key"], "ok": True})
            except Exception as ex:
                self._sync()
                if leg.get("status") in TERMINAL:
                    self.set(leg, state=leg["status"], note="already done when stop was pressed")
                else:
                    self.set(leg, state="cancel_failed", note=scrub(ex, 60))
                self.log({"ev": "cancel", "key": leg["key"], "ok": False, "err": scrub(ex)})
        time.sleep(1.0)
        self._sync()                                   # partial fills during the cancel


class Liquidator:
    """Owns the jobs; the web server calls this."""

    def __init__(self, broker: Broker, log_dir: Path, mode: str, allow_extended: bool = False,
                 step_sec: float = STEP_SEC, now_fn=None, start_frac: float = START_FRAC):
        self.b, self.log_dir, self.mode, self.allow_extended = broker, Path(log_dir), mode, allow_extended
        self.step_sec, self.now_fn, self.start_frac = step_sec, now_fn, start_frac
        self.jobs: dict[str, Job] = {}
        self._busy: set[str] = set()
        self._lock = threading.Lock()
        self.on_done = None                                # called with each job when it finishes (a desktop notice)

    def start(self, positions: list[Position], keys: list[str]) -> Job:
        by_key = {f"{p.account_id}|{p.instrument.key}": p for p in positions}
        legs = []
        with self._lock:
            for k in keys:
                p = by_key.get(k)
                if p is None or k in self._busy:
                    continue
                legs.append({"key": k, "account": p.account_id, "inst": p.instrument, "name": p.instrument.symbol,
                             "side": p.side_to_close.value,
                             "qty": p.closable_qty if p.closable_qty is not None else abs(p.qty),
                             "state": "queued"})
                self._busy.add(k)
            if not legs:
                raise ValueError("nothing to exit (already being worked, or no longer held)")
        job = Job(self.b, legs, by_key, self.log_dir, self.mode, self.allow_extended, self.step_sec, self.now_fn,
                  self.start_frac)
        self.jobs[job.id] = job

        def _release():
            job.done.wait()
            with self._lock:
                for l in legs:
                    self._busy.discard(l["key"])
            if self.on_done:
                try:
                    self.on_done(job)
                except Exception:
                    pass                                   # a notice that fails must not touch the exit
        threading.Thread(target=_release, daemon=True).start()
        job.start()
        return job

    def preview(self, positions: list[Position]) -> list[dict]:
        """What an exit would do now, one row per closable leg: side, size, bid / ask and the first price the engine
        would place (same function, same settings). Read-only — quotes only. The engine re-reads positions and quotes
        when it actually places, so this is an estimate for the page."""
        rows = []
        for p in positions:
            n = p.closable_qty if p.closable_qty is not None else abs(p.qty)
            if p.qty and n and n > 0:
                rows.append((f"{p.account_id}|{p.instrument.key}", p, n))
        try:
            qs = self.b.quotes([p.instrument for _, p, _ in rows]) if rows else {}
        except Exception:
            qs = {}
        out = []
        for k, p, n in rows:
            q, side = qs.get(p.instrument.key), p.side_to_close
            r = {"key": k, "account": p.account_id, "symbol": p.instrument.symbol, "side": side.value, "qty": n,
                 "mult": p.instrument.multiplier, "busy": k in self._busy, "bid": None, "ask": None, "start": None}
            if q is not None and q.usable:
                r.update(bid=q.bid, ask=q.ask, start=start_price(side, q.bid, q.ask, q.tick or 0.01, self.start_frac))
            out.append(r)
        return out

    def stop(self, job_id: str | None = None) -> int:
        n = 0
        for j in self.jobs.values():
            if (job_id is None or j.id == job_id) and not j.done.is_set():
                j.stop_flag.set()
                n += 1
        return n

    def view(self) -> list[dict]:
        return [j.view() for j in list(self.jobs.values())[-10:]]


def job_summary(job: Job, lang: str = "en") -> tuple[str, str, bool]:
    """(title, text, is_warning) for the notice when a job ends: the answer to "is it gone?", in one line."""
    zh = lang == "zh"
    legs = job.legs
    n = len(legs)
    if str(job.message).startswith("error:"):
        return ("清仓出错" if zh else "Exit error",
                ("已经挂出的单还在券商那边，去券商确认" if zh else "Orders already placed are still live — check the broker"), True)
    if job.closed_check == "dry":
        done = sum(1 for l in legs if l.get("state") == "filled")
        return ("演练结束" if zh else "Rehearsal done",
                (f"{done}/{n} 条腿模拟成交，没有下任何单" if zh else f"{done}/{n} legs filled in simulation, no order sent"), False)
    if job.closed_check == "all_closed":
        return ("✅ 全部平掉" if zh else "✅ All closed",
                (f"{n} 条腿，清完已重新读持仓确认" if zh else f"{n} legs, positions re-read after the job"), False)
    if job.closed_check == "some_left":
        left = [f"{str(l['account']).split('-')[0]} {l['name']} ×{l['left']:g}" for l in legs if l.get("left")]
        return ("⚠️ 清完还剩" if zh else "⚠️ Still held", " · ".join(left)[:200], True)
    return ("⚠️ 读不到持仓" if zh else "⚠️ Couldn't re-read positions",
            ("去券商确认清掉了没有" if zh else "Check the broker"), True)
