"""Exit pricing ladder + per-account rate limiter. Pure logic, no broker calls.

The rule is how a careful trader exits by hand:
  - first limit = the passive side + 70% of the spread, rounded to the tick *toward the passive side*
    (selling: bid + 0.7 x spread, rounded down;  buying back: ask - 0.7 x spread, rounded up);
  - every STEP_SEC move one tick toward the passive side, never past it, never a market order;
  - a one-tick spread starts directly at the passive side (bid when selling, ask when covering);
  - if the passive side keeps moving away, follow it one tick per step (no jumping to the bottom).
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque

from .models import Side

START_FRAC = 0.70
STEP_SEC = 10


def _decimals(tick: float) -> int:
    s = f"{tick:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def _snap(p: float, tick: float, up: bool) -> float:
    n = p / tick
    k = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(k * tick, _decimals(tick))


def start_price(side: Side, bid: float, ask: float, tick: float, frac: float = START_FRAC) -> float:
    """`frac`: how far into the spread the first order sits, measured from the passive side ([trading] start_frac)."""
    if side == Side.SELL:
        if ask <= bid:
            return bid
        return max(bid, _snap(bid + frac * (ask - bid), tick, up=False))
    if ask <= bid:
        return ask
    return min(ask, _snap(ask - frac * (ask - bid), tick, up=True))


def next_price(side: Side, cur: float, bid: float, ask: float, tick: float) -> float:
    """One tick toward the passive side, never past it.
    Already at (or through) the passive side -> stay: the order is marketable and should fill.
    The passive side moved away -> keep stepping one tick per call; it follows without jumping."""
    d = _decimals(tick)
    if side == Side.SELL:
        if cur <= bid + 1e-9:
            return cur
        return max(round(cur - tick, d), bid)
    if cur >= ask - 1e-9:
        return cur
    return min(round(cur + tick, d), ask)


class Bucket:
    """At most `cap` actions per `per` seconds."""

    def __init__(self, cap: int, per: float):
        self.cap, self.per, self.t = cap, per, deque()
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            now = time.monotonic()
            while self.t and now - self.t[0] >= self.per:
                self.t.popleft()
            if len(self.t) < self.cap:
                self.t.append(now)
                return True
            return False


_BUCKETS: dict = {}
_LOCK = threading.Lock()


def bucket(broker: str, account: str, kind: str, cap: int, per: float) -> Bucket:
    """Broker rate limits are per account, not per thread: legs selling at the same time share one budget."""
    with _LOCK:
        k = (broker, account, kind)
        if k not in _BUCKETS:
            _BUCKETS[k] = Bucket(cap, per)
        return _BUCKETS[k]
