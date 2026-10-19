from datetime import date, datetime
from zoneinfo import ZoneInfo

from livepanel.ladder import Bucket, next_price, start_price
from livepanel.models import AssetClass, CashFlow, Side
from livepanel.pnl import EquityStore, period_pnl
from livepanel.sessions import can_trade, market_live, us_session
from livepanel.util import mask, scrub

NY = ZoneInfo("America/New_York")
KL = ZoneInfo("Asia/Kuala_Lumpur")


# ── ladder: the cases the original tool was built from ─────────────────────
def test_sell_start_70pct_rounded_down():
    assert start_price(Side.SELL, 3.05, 3.70, 0.05) == 3.50          # DRAM 62.5C: 3.505 -> 3.50


def test_one_tick_spread_starts_at_bid():
    assert start_price(Side.SELL, 3.05, 3.10, 0.05) == 3.05
    assert start_price(Side.SELL, 120.20, 120.21, 0.01) == 120.20


def test_sell_steps_one_tick_never_below_bid():
    assert next_price(Side.SELL, 3.50, 3.05, 3.70, 0.05) == 3.45
    assert next_price(Side.SELL, 3.10, 3.08, 3.70, 0.05) == 3.08      # clamp at bid
    assert next_price(Side.SELL, 3.05, 3.05, 3.70, 0.05) == 3.05      # at bid: stay


def test_sell_follows_a_falling_bid_one_tick_at_a_time():
    assert next_price(Side.SELL, 3.05, 2.80, 3.40, 0.05) == 3.00


def test_buy_side_mirrors():
    assert start_price(Side.BUY, 3.05, 3.70, 0.05) == 3.25           # 3.70 - 0.455 = 3.245 -> up to 3.25
    assert start_price(Side.BUY, 3.05, 3.10, 0.05) == 3.10           # one tick: straight to ask
    assert next_price(Side.BUY, 3.25, 3.05, 3.70, 0.05) == 3.30
    assert next_price(Side.BUY, 3.68, 3.05, 3.70, 0.05) == 3.70
    assert next_price(Side.BUY, 3.70, 3.05, 3.70, 0.05) == 3.70


def test_crypto_sized_ticks_keep_precision():
    assert start_price(Side.SELL, 0.1234, 0.1244, 0.0001) == 0.1241


def test_bucket():
    b = Bucket(2, 30)
    assert b.take() and b.take() and not b.take()


# ── sessions ───────────────────────────────────────────────────────────────
def test_sessions_rth_pre_post_holiday():
    assert us_session(datetime(2026, 10, 2, 10, 0, tzinfo=NY)) == "rth"
    assert us_session(datetime(2026, 10, 2, 8, 0, tzinfo=NY)) == "pre"
    assert us_session(datetime(2026, 10, 2, 17, 0, tzinfo=NY)) == "post"
    assert us_session(datetime(2026, 11, 26, 10, 0, tzinfo=NY)) == "closed"        # Thanksgiving
    assert us_session(datetime(2026, 11, 27, 13, 30, tzinfo=NY)) == "post"         # early close 13:00


def test_options_never_outside_rth_stocks_only_if_allowed():
    pre = datetime(2026, 10, 2, 8, 0, tzinfo=NY)
    assert can_trade(AssetClass.OPTION, "US", pre, allow_extended=True)[0] is False
    assert can_trade(AssetClass.STOCK, "US", pre, allow_extended=False)[0] is False
    assert can_trade(AssetClass.STOCK, "US", pre, allow_extended=True) == (True, "pre")
    assert can_trade(AssetClass.STOCK, "MY", datetime(2026, 10, 2, 10, 0, tzinfo=NY))[0] is False
    assert can_trade(AssetClass.CRYPTO_PERP, "CRYPTO", pre)[0] is True


def test_market_live_window_from_malaysia():
    opt = {AssetClass.OPTION}
    # 21:00 MYT = 09:00 EDT: inside the 30-min lead before the open
    assert market_live(datetime(2026, 10, 2, 21, 0, tzinfo=KL), opt)
    # 04:10 MYT next day = 16:10 EDT: inside the 15-min grace
    assert market_live(datetime(2026, 10, 3, 4, 10, tzinfo=KL), opt)
    assert not market_live(datetime(2026, 10, 3, 4, 20, tzinfo=KL), opt)
    assert not market_live(datetime(2026, 10, 3, 22, 0, tzinfo=KL), opt)          # Saturday


# ── period P&L ─────────────────────────────────────────────────────────────
def test_period_pnl_anchors_on_previous_close_and_strips_deposits():
    closes = {"2026-09-25": 45_000.00, "2026-08-31": 25_000.00, "2026-09-29": 41_000.0}
    flows = [CashFlow("A", date(2026, 9, 10), 5_000, "USD", "deposit"),
             CashFlow("A", date(2026, 9, 30), -1_000, "USD", "withdrawal"),
             CashFlow("A", date(2026, 9, 30), 999, "USD", "other"),
             CashFlow("A", date(2026, 9, 15), 4_200, "MYR", "deposit")]
    r = period_pnl(date(2026, 10, 1), 40_000.0, closes, flows, {"MYR": 4.2})
    assert r["week"]["from"] == "2026-09-25"
    assert r["week"]["pnl"] == round(40_000 - 45_000.00 - (-1_000), 2)
    assert r["month"]["from"] == "2026-09-29"
    assert r["month"]["pnl"] == round(40_000 - 41_000 + 1_000, 2)


def test_period_pnl_month_window_counts_deposits_inside_it():
    closes = {"2026-08-31": 25_000.00}
    flows = [CashFlow("A", date(2026, 9, 10), 5_000, "USD", "deposit"),
             CashFlow("A", date(2026, 9, 15), 4_200, "MYR", "deposit"),
             CashFlow("A", date(2026, 9, 16), 100, "SGD", "deposit")]
    r = period_pnl(date(2026, 9, 30), 40_000.0, closes, flows, {"MYR": 4.2})
    assert r["month"]["net_deposits"] == 6_000.0
    assert r["month"]["unconverted"] == ["SGD"]


def test_period_pnl_without_flows_is_labelled():
    r = period_pnl(date(2026, 10, 1), 10.0, {"2026-09-25": 5.0}, None, {})
    assert r["week"]["flows_known"] is False and r["week"]["pnl"] == 5.0


def test_equity_store_last_value_per_trading_day(tmp_path):
    s = EquityStore(tmp_path)
    s.record(datetime(2026, 10, 1, 15, 0, tzinfo=NY), 100.0)
    s.record(datetime(2026, 10, 1, 16, 10, tzinfo=NY), 110.0)
    s.record(datetime(2026, 10, 3, 12, 0, tzinfo=NY), 999.0)            # Saturday: ignored
    assert s.closes() == {"2026-10-01": 110.0}


# ── account numbers never shown in full ────────────────────────────────────
def test_mask_and_scrub():
    assert mask("123456789012345678") == "…5678"
    assert "123456789012345678" not in scrub("Account 123456789012345678 only supports …")


def test_closes_ignore_rows_after_the_grace_window(tmp_path):
    s = EquityStore(tmp_path)
    s.record(datetime(2026, 9, 25, 16, 8, tzinfo=NY), 40_000.00)
    s.record(datetime(2026, 9, 25, 23, 53, tzinfo=NY), 40_250.00)    # after-hours mark, same ET day
    s.record(datetime(2026, 11, 27, 13, 10, tzinfo=NY), 1.0)          # early-close day: 13:00 + 15 min
    s.record(datetime(2026, 11, 27, 13, 20, tzinfo=NY), 2.0)
    assert s.closes() == {"2026-09-25": 40_000.00, "2026-11-27": 1.0}


def test_the_first_price_follows_the_configured_fraction():
    assert start_price(Side.SELL, 3.05, 3.70, 0.05, 0.5) == 3.35       # 3.375 -> down to 3.35
    assert start_price(Side.BUY, 3.05, 3.70, 0.05, 0.5) == 3.40        # 3.375 -> up to 3.40
    assert start_price(Side.SELL, 3.05, 3.70, 0.05, 0.0) == 3.05       # 0%: straight to the bid
    assert start_price(Side.BUY, 3.05, 3.70, 0.05, 0.0) == 3.70        # buying back: straight to the ask
