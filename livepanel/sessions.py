"""When can each asset class trade, and is the market "live" (worth refreshing)?

Everything is anchored in US/Eastern and converted, never hard-coded in a local timezone
(daylight saving moves the local clock twice a year; ET is the clock the exchanges keep).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import AssetClass

NY = ZoneInfo("America/New_York")

# NYSE full-day closures. Extend via config `market.extra_holidays`.
NYSE_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
# 13:00 ET closes. Only dates confirmed against the exchange calendar; do not add "usual" ones by habit.
NYSE_EARLY_CLOSE = {"2026-11-27": time(13, 0), "2026-12-24": time(13, 0), "2027-11-26": time(13, 0)}
HOLIDAY_TABLE_UNTIL = date(2027, 12, 31)

PRE_OPEN, RTH_OPEN, RTH_CLOSE, POST_CLOSE = time(4, 0), time(9, 30), time(16, 0), time(20, 0)


def add_holidays(days) -> None:
    NYSE_HOLIDAYS.update(str(d)[:10] for d in days or [])


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in NYSE_HOLIDAYS


def calendar_stale(d: date) -> bool:
    """Past the end of the built-in holiday table: weekdays will all look like trading days."""
    return d > HOLIDAY_TABLE_UNTIL


def rth_close(d: date) -> time:
    return NYSE_EARLY_CLOSE.get(d.isoformat(), RTH_CLOSE)


def us_session(now: datetime) -> str:
    """'pre' | 'rth' | 'post' | 'overnight' | 'closed' for US equities, at `now` (any tz)."""
    et = now.astimezone(NY)
    d, t = et.date(), et.time()
    if not is_trading_day(d):
        # Sunday 20:00 ET overnight session feeds Monday; keep it simple and call weekends closed.
        return "closed"
    close = rth_close(d)
    if RTH_OPEN <= t < close:
        return "rth"
    if PRE_OPEN <= t < RTH_OPEN:
        return "pre"
    if close <= t < POST_CLOSE:
        return "post"
    return "overnight"


def can_trade(asset_class: AssetClass, market: str, now: datetime, allow_extended: bool = False) -> tuple[bool, str]:
    """(tradable now?, session tag to send with the order).

    Options: regular hours only — there is no pre/post market for listed US options.
    Stocks: regular hours; pre/post only if the user enabled it (limit orders only there).
    Crypto: always.
    Anything else: not handled by the liquidator (no session table) -> False.
    """
    if asset_class in (AssetClass.CRYPTO_SPOT, AssetClass.CRYPTO_PERP):
        return True, "24x7"
    if market != "US":
        return False, "unsupported-market"
    s = us_session(now)
    if asset_class == AssetClass.OPTION:
        return s == "rth", s
    if asset_class == AssetClass.STOCK:
        if s == "rth":
            return True, s
        if allow_extended and s in ("pre", "post"):
            return True, s
        return False, s
    return False, s


def market_live(now: datetime, classes: set[AssetClass], lead_min: int = 30, grace_min: int = 15,
                extended: bool = False) -> bool:
    """Should the panel keep refreshing? From `lead_min` before the open until `grace_min` after
    the close (so the closing marks settle). When False the panel shows the last snapshot instead
    of re-pulling quotes that cannot change — options do not trade after the bell."""
    if any(c in (AssetClass.CRYPTO_SPOT, AssetClass.CRYPTO_PERP) for c in classes):
        return True
    et = now.astimezone(NY)
    if not is_trading_day(et.date()):
        return False
    stocks_ext = extended and AssetClass.STOCK in classes
    start = datetime.combine(et.date(), PRE_OPEN if stocks_ext else RTH_OPEN, NY) - timedelta(minutes=lead_min)
    end = datetime.combine(et.date(), POST_CLOSE if stocks_ext else rth_close(et.date()), NY)
    return start <= et < end + timedelta(minutes=grace_min)


def trading_day_of(now: datetime) -> date:
    """The ET trading day a timestamp belongs to (after-hours still counts as that day)."""
    return now.astimezone(NY).date()
