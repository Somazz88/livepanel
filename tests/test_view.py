from datetime import date, datetime
from zoneinfo import ZoneInfo

from livepanel.models import AssetClass, Fill, Instrument, Position, Quote
from livepanel.view import account_today, group_positions, merge_fills, spark

NY = ZoneInfo("America/New_York")
KL = ZoneInfo("Asia/Kuala_Lumpur")
D = date(2026, 10, 1)


def opt(und, strike, exp=date(2026, 11, 20)):
    return Instrument(f"US.{und}{exp:%y%m%d}C{int(strike * 1000)}", f"{und} {exp:%m/%d} {strike:g}C", AssetClass.OPTION,
                      multiplier=100, underlying=und, expiry=exp, strike=strike, right="C")


def stk(sym):
    return Instrument(f"US.{sym}", sym, AssetClass.STOCK)


POS = [
    Position("SG-1111", opt("NVDA", 150), 10, 8.00, 8.50, 8_500, 500, 120, 10),
    Position("MY-2222", opt("NVDA", 150), 5, 8.10, 8.50, 4_250, 200, 60, 5),
    Position("MY-2222", opt("TSLA", 300), 3, 20.0, 21.0, 6_300, 300, 90, 3),
    Position("SG-1111", stk("WOLF"), 100, 30.0, 12.0, 1_200, -1_800, -40, 100),          # stock only, -60%
    Position("SG-1111", opt("META", 700), 0, None, None, 0, None, 120.5, 0),             # closed today
    Position("MY-2222", Instrument("MY.1155", "1155", AssetClass.STOCK, currency="MYR", market="MY"), 100, 9.0, 9.5, 950, 50, 10, 100),
]


def test_groups_sorted_and_summed():
    gs, foot = group_positions(POS, {"NVDA": Quote(None, None, last=180.0, prev_close=182.0)}, D, show_und=True)
    assert [g["tk"] for g in gs] == ["NVDA", "TSLA", "WOLF", "META"]   # options by value, stock-only after, closed last
    nvda = gs[0]
    assert nvda["mv"] == 12_750 and nvda["today"] == 180 and len(nvda["legs"]) == 2
    assert nvda["und_chg"] == round((180.0 / 182.0 - 1) * 100, 2)
    assert nvda["legs"][0]["dte"] == 50
    assert gs[2]["hot"] is True and gs[2]["pct"] == -60.0              # WOLF down 60% -> red cell
    assert gs[3]["legs"] == [] and gs[3]["closed"][0]["today"] == 120.5
    assert foot == {"mv": 20_250.0, "expiry": "2026-11-20"}
    assert all(g["tk"] != "1155" for g in gs)                           # MYR leg not summed into USD groups


def test_stock_move_hidden_before_the_open():
    gs, _ = group_positions(POS, {"NVDA": Quote(None, None, last=181.0, prev_close=182.0)}, D, show_und=False)
    assert gs[0]["und_chg"] is None


def test_account_today_includes_closed_legs_and_skips_other_currency():
    assert account_today(POS, "USD") == {"SG-1111": 120 - 40 + 120.5, "MY-2222": 150.0}


def test_fills_merged_per_order_newest_first():
    f = [Fill("SG-1111", "o1", "NVDA 11/20 150C", "BUY", 4, 8.00, datetime(2026, 10, 1, 11, 0, 0, tzinfo=NY)),
         Fill("SG-1111", "o1", "NVDA 11/20 150C", "BUY", 6, 8.00, datetime(2026, 10, 1, 11, 0, 1, tzinfo=NY)),
         Fill("MY-2222", "o2", "TSLA 11/20 300C", "BUY", 3, 20.0, datetime(2026, 10, 1, 14, 10, tzinfo=NY)),
         Fill("SG-1111", "o3", "META 11/20 700C", "SELL", 1, 30.0, datetime(2026, 9, 30, 15, 50, tzinfo=NY))]
    rows, n_today = merge_fills(f, KL, D)
    assert n_today == 2
    assert rows[0]["symbol"] == "TSLA 11/20 300C" and rows[0]["time"] == "10-02 02:10"     # shown in local time
    assert rows[1]["qty"] == 10 and rows[1]["avg"] == 8.0 and rows[1]["acct"] == "SG"


def test_spark_adds_today_live_value():
    s = spark({"2026-09-29": 1.0, "2026-09-30": 2.0}, 3.0, D)
    assert s == [["2026-09-29", 1.0], ["2026-09-30", 2.0], ["2026-10-01", 3.0]]
