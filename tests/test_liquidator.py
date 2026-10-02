import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from livepanel.brokers.fake import FakeBroker
from livepanel.liquidator import Liquidator
from livepanel.models import Position

NY = ZoneInfo("America/New_York")
RTH = lambda: datetime(2026, 10, 2, 10, 0, tzinfo=NY)          # noqa: E731
PRE = lambda: datetime(2026, 10, 2, 8, 0, tzinfo=NY)           # noqa: E731


def run(liq, b, keys, timeout=5.0):
    job = liq.start(b.positions(), keys)
    job.done.wait(timeout)
    if not job.done.is_set():
        liq.stop(job.id)
        job.done.wait(timeout)
    return job


@pytest.fixture
def setup(tmp_path):
    b = FakeBroker.demo()
    return b, Liquidator(b, tmp_path, "test", step_sec=0.05, now_fn=RTH)


def key(b, sym):
    p = next(p for p in b.pos if p.instrument.symbol == sym)
    return f"{p.account_id}|{p.instrument.key}"


def test_long_option_walks_down_to_bid_and_fills(setup):
    b, liq = setup
    job = run(liq, b, [key(b, "INTC 11/20 120C")])
    prices = [c[4] if c[0] == "place" else c[2] for c in b.calls if c[0] in ("place", "modify")]
    assert prices == [12.55, 12.50, 12.45]                       # 12.45 + 70% x 0.20 = 12.59 -> 12.55, then -0.05
    assert job.legs[0]["state"] == "filled"


def test_short_stock_is_bought_back_walking_up_to_ask(setup):
    b, liq = setup
    job = run(liq, b, [key(b, "TSLA")])
    places = [c for c in b.calls if c[0] == "place"]
    assert places[0][2].value == "BUY" and places[0][4] == 250.98   # 251.02 - 0.049 -> 250.98
    assert job.legs[0]["state"] == "filled"
    assert job.legs[0]["price"] == 251.02


def test_quantity_shrinks_to_what_is_closable_now(setup):
    b, liq = setup
    k = key(b, "AAPL")
    seen = b.positions()                                          # the user looked at 50
    b.pos[1] = Position(**{**b.pos[1].__dict__, "qty": 20, "closable_qty": 20})   # sold 30 by hand meanwhile
    job = liq.start(seen, [k])
    job.done.wait(5)
    assert [c[3] for c in b.calls if c[0] == "place"] == [20]


def test_position_gone_places_nothing(setup):
    b, liq = setup
    k = key(b, "AAPL")
    seen = b.positions()
    b.pos.pop(1)
    job = liq.start(seen, [k])
    job.done.wait(5)
    assert not [c for c in b.calls if c[0] == "place"] and job.legs[0]["state"] == "no_qty"


def test_options_refused_outside_regular_hours(tmp_path):
    b = FakeBroker.demo()
    liq = Liquidator(b, tmp_path, "test", allow_extended=True, step_sec=0.05, now_fn=PRE)
    job = run(liq, b, [key(b, "INTC 11/20 120C"), key(b, "AAPL")])
    st = {l["name"]: l["state"] for l in job.legs}
    assert st["INTC 11/20 120C"] == "not_tradable"
    assert [c[5] for c in b.calls if c[0] == "place"] == ["pre"]    # the stock goes out in the pre-market session


def test_recheck_failure_places_nothing(setup):
    b, liq = setup
    seen = b.positions()

    def boom():
        raise RuntimeError("gateway down")
    b.positions = boom
    job = liq.start(seen, [f"{seen[0].account_id}|{seen[0].instrument.key}"])
    job.done.wait(10)
    assert not [c for c in b.calls if c[0] == "place"] and job.legs[0]["state"] == "recheck_failed"


def test_stop_cancels_working_orders(tmp_path):
    b = FakeBroker.demo()
    b.q["US.INTC261120C120000"].bid = 10.0                        # bid far below: will never fill
    liq = Liquidator(b, tmp_path, "test", step_sec=0.5, now_fn=RTH)
    job = liq.start(b.positions(), [key(b, "INTC 11/20 120C")])
    time.sleep(0.3)
    liq.stop(job.id)
    job.done.wait(5)
    assert any(c[0] == "cancel" for c in b.calls)
    assert job.legs[0]["state"] == "cancelled_by_user"


def test_same_position_cannot_be_worked_twice(setup):
    b, liq = setup
    b.q["US.AAPL"].bid = 1.0
    k = key(b, "AAPL")
    liq.step_sec = 0.5
    j = liq.start(b.positions(), [k])
    with pytest.raises(ValueError):
        liq.start(b.positions(), [k])
    liq.stop(j.id)
    j.done.wait(5)


def test_dry_run_never_reaches_the_real_broker(tmp_path):
    from livepanel.brokers.fake import DryRun
    inner = FakeBroker.demo()

    def forbidden(*a, **k):
        raise AssertionError("a real order was attempted in dry-run")
    inner.place_limit = inner.modify_price = inner.cancel = forbidden
    b = DryRun(inner)
    liq = Liquidator(b, tmp_path, "dry", step_sec=0.05, now_fn=RTH)
    job = liq.start(b.positions(), [key(inner, "INTC 11/20 120C")])
    job.done.wait(5)
    assert job.legs[0]["state"] == "filled"
