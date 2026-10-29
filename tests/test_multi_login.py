"""Several moomoo logins in one panel: one OpenD per login, keys kept apart by a label, one exit across all."""
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from livepanel import brokers
from livepanel.brokers import Composite, moomoo_logins
from livepanel.brokers.fake import FakeBroker
from livepanel.brokers.moomoo import MoomooBroker, acc_key
from livepanel.config import Config
from livepanel.liquidator import Liquidator
from livepanel.models import AssetClass, Instrument, Position, Quote
from livepanel.view import group_positions

NY = ZoneInfo("America/New_York")
RTH = lambda: datetime(2026, 10, 2, 10, 0, tzinfo=NY)          # noqa: E731


def cfg(logins=None, **moomoo):
    m = {**moomoo}
    if logins is not None:
        m["logins"] = logins
    return Config({"brokers": {"use": ["moomoo"]}, "moomoo": m})


def test_no_logins_list_is_one_login_without_label():
    assert moomoo_logins(cfg(port=11111)) == [{"host": "127.0.0.1", "port": 11111, "paper": False,
                                               "include_futures": True, "label": None, "since": None, "opend": None}]


def test_three_logins_build_three_brokers_with_distinct_names():
    c = cfg([{"label": "Main", "port": 11111}, {"label": "B", "port": 11112}, {"label": "C", "port": 11113}])
    b = brokers.make(c)
    ms = [x for x in b.brokers if isinstance(x, MoomooBroker)]
    assert [(x.label, x.port, x.name) for x in ms] == [("Main", 11111, "moomoo·Main"), ("B", 11112, "moomoo·B"),
                                                       ("C", 11113, "moomoo·C")]
    assert len({x._cache_path() for x in ms}) == 3                   # paths only, nothing is written


@pytest.mark.parametrize("logins, msg", [
    ([{"port": 11111}], "no label"),
    ([{"label": "A", "port": 11111}, {"label": "A", "port": 11112}], "used twice"),
    ([{"label": "A", "port": 11111}, {"label": "B", "port": 11111}], "already taken"),
    ([{"label": "my-acct", "port": 11111}], "at most 12"),
    ([{"label": "a b", "port": 11111}], "at most 12"),
])
def test_bad_login_lists_fail_loudly(logins, msg):
    with pytest.raises(ValueError, match=msg):
        moomoo_logins(cfg(logins))


def test_account_key_and_cache_file_follow_the_label(tmp_path):
    assert acc_key(None, "SG", 123456781234) == "SG-1234"            # one login: unchanged
    assert acc_key("Main", "MY", 123456785678) == "Main·MY-5678"
    a = MoomooBroker(cache_dir=tmp_path, label="Main")
    b = MoomooBroker(cache_dir=tmp_path, label="号2")
    one = MoomooBroker(cache_dir=tmp_path)
    assert one._cache_path().name == "moomoo_cashflow_cache.json"    # existing single-login file keeps its name
    assert len({a._cache_path(), b._cache_path(), one._cache_path()}) == 3


def _login(label, acc, qty, bid):
    b = FakeBroker()
    b.name = f"fake·{label}"
    opt = Instrument("US.ACME261218C50000", "ACME 12/18 50C", AssetClass.OPTION, multiplier=100.0,
                     underlying="ACME", expiry=date(2026, 12, 18), strike=50.0, right="C")
    b.pos = [Position(acc, opt, qty, 1.20, bid, qty * bid * 100, 0, 0, qty)]
    b.q = {opt.key: Quote(bid, bid + 0.04, 0.01, bid)}
    return b


def test_one_underlying_across_three_logins_is_one_group_and_one_exit(tmp_path):
    # the case that started this: "I thought I sold it in all three accounts; one still had it"
    logins = [_login("Main", "Main·MY-0001", 8, 0.90), _login("B", "B·MY-0002", 2, 0.90),
              _login("C", "C·MY-0003", 3, 0.90)]
    comp = Composite(logins)
    pos = comp.positions()
    groups, _ = group_positions(pos, {}, date(2026, 10, 2), show_und=False)
    assert [g["tk"] for g in groups] == ["ACME"]
    assert sorted(l["acct"] for l in groups[0]["legs"]) == ["B·MY", "C·MY", "Main·MY"]   # every login shown
    liq = Liquidator(comp, tmp_path, "test", step_sec=0.05, now_fn=RTH)
    job = liq.start(pos, [l["key"] for l in groups[0]["legs"]])
    job.done.wait(10)
    for b, qty in zip(logins, (8, 2, 3)):
        places = [c for c in b.calls if c[0] == "place"]
        assert len(places) == 1 and places[0][3] == qty                # each login sold its own legs, once
    assert {l["state"] for l in job.legs} == {"filled"}


def test_adding_a_login_books_its_balance_as_a_deposit_not_profit(tmp_path):
    from datetime import datetime as _dt
    from livepanel.models import AccountSnapshot
    from livepanel.panel import Panel

    class Login(FakeBroker):
        def __init__(self, label, acc, equity):
            super().__init__()
            self.name, self.acc_id, self.equity = f"fake·{label}", acc, equity

        def snapshot(self):
            return [AccountSnapshot(self.acc_id, self.acc_id, self.equity)]

    raw = {"brokers": {"use": ["moomoo"]}, "panel": {"data_dir": str(tmp_path)},
           "moomoo": {"logins": [{"label": "Main", "port": 11111}, {"label": "Acct2", "port": 11112, "since": "2026-10-18"}]}}
    c = Config(raw)
    main, acct2 = Login("Main", "Main·MY-0001", 50_000.0), Login("Acct2", "Acct2·MY-0002", 6_000.0)
    b = Composite([main, acct2])
    p = Panel(b, c)
    p.store.record(_dt(2026, 10, 16, 16, 0, tzinfo=NY), 50_000.0)        # last close: only Main existed
    p._fetch_flows(None, None)                                            # fake brokers report no deposits
    st = p.tick(now=_dt(2026, 10, 19, 10, 0, tzinfo=NY))
    assert st["equity"] == 56_000.0
    assert st["periods"]["week"]["pnl"] == 0.0                            # not +6,000
    assert st["periods"]["week"]["net_deposits"] == 6_000.0
    assert json.loads((tmp_path / "joins.json").read_text(encoding="utf-8"))["Acct2"] == {"day": "2026-10-19", "equity": 6000.0}
    acct2.equity = 6_100.0                                                  # her account then moves by itself
    st = p.tick(now=_dt(2026, 10, 19, 11, 0, tzinfo=NY), force=True)
    assert st["periods"]["week"]["pnl"] == 100.0                          # only the move counts
    # the login is taken out of the config: its balance leaves the totals, and so must its join deposit
    p.cfg = Config({**raw, "moomoo": {"logins": [{"label": "Main", "port": 11111}]}})
    p.b = Composite([main])
    p.b.connect()
    st = p.tick(now=_dt(2026, 10, 20, 10, 0, tzinfo=NY), force=True)
    assert st["equity"] == 50_000.0 and st["periods"]["week"]["net_deposits"] == 0.0
    assert st["periods"]["week"]["pnl"] == 0.0                            # not -6,000
    assert "Acct2" not in json.loads((tmp_path / "joins.json").read_text(encoding="utf-8"))


def test_settings_page_marks_only_new_logins():
    from livepanel import settings
    raw = {"brokers": {"use": ["moomoo"]}, "moomoo": {"port": 11111}}
    f = settings.form_from(Config(raw))
    f["moomoo"]["logins"] = [{"label": "Main", "port": 11111}, {"label": "Acct2", "port": 11112}]
    out = settings.apply(raw, f)
    assert "since" not in out["moomoo"]["logins"][0] and out["moomoo"]["logins"][1]["since"]   # existing vs added
    again = settings.apply(out, settings.form_from(Config(out)))
    assert again["moomoo"]["logins"] == out["moomoo"]["logins"]                              # saving again keeps the date


def test_a_spread_shows_dollars_not_a_percent_of_its_net_cost():
    # a sold put + a bought call on one symbol: % of the net cost blew up (here it would be -180 / 160 = -113%)
    put = Instrument("US.XYZ261120P10000", "XYZ 11/20 10P", AssetClass.OPTION, multiplier=100.0, underlying="XYZ",
                     expiry=date(2026, 11, 20), strike=10.0, right="P")
    call = Instrument("US.XYZ271217C12000", "XYZ 12/17 12C", AssetClass.OPTION, multiplier=100.0, underlying="XYZ",
                      expiry=date(2027, 12, 17), strike=12.0, right="C")
    legs = [Position("Acct2·MY-0002", put, -2, 0.80, 1.20, -240.0, -80.0, 0, 2),      # seller: sold at 0.80, now 1.20
            Position("Acct2·MY-0002", call, 2, 2.00, 1.50, 300.0, -100.0, 0, 2)]      # buyer: paid 2.00, now 1.50
    g = group_positions(legs, {}, date(2026, 10, 19), show_und=False)[0][0]
    assert g["mixed"] and g["pct"] is None and not g["hot"] and g["pl"] == -180.0 and g["mv"] == 60.0
    by = {l["what"]: l["pct"] for l in g["legs"]}
    assert by == {"11/20 10P": -50.0, "12/17 12C": -25.0}                 # each leg against what went into it
    single = group_positions(legs[1:], {}, date(2026, 10, 19), show_und=False)[0][0]
    assert not single["mixed"] and single["pct"] == -25.0                 # one-sided groups keep their %


def test_unlock_goes_to_the_named_login_only():
    a, b = _login("Main", "Main·MY-0001", 1, 1.0), _login("Acct2", "Acct2·MY-0002", 1, 1.0)
    comp = Composite([a, b])
    assert [(l["label"], l["unlock"]) for l in comp.logins()] == [("fake·Main", True), ("fake·Acct2", True)]
    comp.unlock("fake·Acct2", "123456")
    assert ("unlock",) in b.calls and ("unlock",) not in a.calls
    with pytest.raises(Exception, match="wrong trade password"):
        comp.unlock("fake·Main", "nope")
    b.unlock_in_opend = True                                                # GUI OpenD: the dialog shows its button instead of a box
    assert [l["in_opend"] for l in comp.logins()] == [False, True]
    from livepanel.brokers.fake import DryRun
    d = DryRun(_login("C", "C·MY-0003", 1, 1.0))
    assert d.needs_unlock and d.unlock("123456") is None                  # a rehearsal can test unlocking too


def test_unlock_endpoint_guards_and_never_echoes_the_password(tmp_path):
    import http.client
    import socket
    import threading
    from livepanel.panel import Panel
    from livepanel.server import serve

    def run(trading):
        a, b = _login("Main", "Main·MY-0001", 1, 1.0), _login("Acct2", "Acct2·MY-0002", 1, 1.0)
        c = Config({"brokers": {"use": ["demo"]}, "panel": {"data_dir": str(tmp_path)},
                    "trading": {"enabled": trading, "dry_run": True}})
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        srv = serve(Panel(Composite([a, b]), c), None, c, port)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def req(method, path, token=None, body=None):
            con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            h = {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json", **({"X-Token": token} if token else {})}
            con.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
            r = con.getresponse()
            return r.status, r.read().decode("utf-8")
        html = req("GET", "/")[1]
        tok = json.loads(html.split("const CFG = ", 1)[1].split(";", 1)[0])["token"]
        return srv, req, tok, b

    srv, req, tok, acct2 = run(trading=False)
    try:
        assert req("POST", "/api/unlock", tok, {"login": "fake·Acct2", "password": "123456"})[0] == 403   # trading off
    finally:
        srv.shutdown(); srv.server_close()
    srv, req, tok, acct2 = run(trading=True)
    try:
        assert req("POST", "/api/unlock", None, {"login": "fake·Acct2", "password": "123456"})[0] == 403  # no token
        code, body = req("POST", "/api/unlock", tok, {"login": "fake·Acct2", "password": "wrong-pw-xyz"})
        assert code == 400 and "wrong-pw-xyz" not in body
        code, body = req("POST", "/api/unlock", tok, {"login": "fake·Acct2", "password": "123456"})
        assert code == 200 and "123456" not in body and ("unlock",) in acct2.calls
        st = req("GET", "/api/state")[1]
        assert "fake·Acct2" in json.loads(st)["unlocked"] and "123456" not in st
    finally:
        srv.shutdown(); srv.server_close()


def test_a_closed_opend_fails_fast_instead_of_hanging_the_panel():
    import socket
    import time
    pytest.importorskip("moomoo")
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()     # nobody listens here
    b = MoomooBroker("127.0.0.1", port, label="Gone")
    t0 = time.monotonic()
    with pytest.raises(Exception, match="not reachable"):
        b.connect()
    assert time.monotonic() - t0 < 5
    c = Composite([b, _login("Main", "Main·MY-0001", 1, 1.0)])
    c.connect()                                                          # the other login still connects
    assert "moomoo·Gone" in c.errors and [x.name for x in c.connected()] == ["fake·Main"]


def test_a_short_leg_is_closable_even_though_moomoo_reports_it_negative(tmp_path):
    from livepanel.brokers.moomoo import closable
    assert closable(-2.0, -2.0) == 2.0                 # a sold put x2, as moomoo reports it
    assert closable(29.0, 29.0) == 29.0 and closable(0.0, 5.0) == 0.0 and closable(None, 3.0) is None
    assert closable(-3.0, -2.0) == 2.0                 # never more than the position
    # and the exit engine then buys it back
    b = _login("Acct2", "Acct2·MY-0002", -2, 1.25)
    b.pos[0].closable_qty = closable(-2.0, -2.0)
    liq = Liquidator(b, tmp_path, "test", step_sec=0.05, now_fn=RTH)
    job = liq.start(b.positions(), [f"Acct2·MY-0002|{b.pos[0].instrument.key}"])
    job.done.wait(10)
    places = [c for c in b.calls if c[0] == "place"]
    assert places and places[0][2].value == "BUY" and places[0][3] == 2.0


def test_a_seller_only_group_still_has_a_percent():
    put = Instrument("US.XYZ261120P10000", "XYZ 11/20 10P", AssetClass.OPTION, multiplier=100.0, underlying="XYZ",
                     expiry=date(2026, 11, 20), strike=10.0, right="P")
    g = group_positions([Position("A·MY-0001", put, -2, 0.80, 0.60, -120.0, 40.0, 0, 2)], {}, date(2026, 10, 19),
                        show_und=False)[0][0]
    assert not g["mixed"] and g["pct"] == 25.0                            # sold at 0.80, now 0.60: +40 on 160 received


def test_the_keeper_starts_a_closed_opend_once_per_window(tmp_path):
    from livepanel.__main__ import OpenDKeeper
    exe = tmp_path / "OpenD.exe"
    exe.write_bytes(b"")
    c = cfg([{"label": "Main", "port": 11111}, {"label": "B", "port": 11112, "opend": str(exe)}])
    up, started, clock = {11111}, [], [1000.0]
    k = OpenDKeeper(c, popen=started.append, port_open=lambda h, p: p in up, now=lambda: clock[0])
    assert k.start_missing() == ["B"] and started == [str(exe)]           # only the login with an OpenD path
    assert k.start_missing() == []                                       # not again within 5 minutes
    clock[0] += 301
    assert k.start_missing() == ["B"]                                    # still closed later: try again
    up.add(11112)
    clock[0] += 301
    assert k.start_missing() == []                                       # running: leave it alone


def test_only_an_opend_program_can_be_configured(tmp_path):
    with pytest.raises(ValueError, match="OpenD program"):
        moomoo_logins(cfg([{"label": "A", "port": 11111, "opend": r"C:\Windows\System32\calc.exe"}]))
    with pytest.raises(ValueError, match="OpenD program"):
        moomoo_logins(cfg([{"label": "A", "port": 11111, "opend": r"C:\x\OpenD.exe\..\evil.exe"}]))
    ok = moomoo_logins(cfg([{"label": "A", "port": 11111, "opend": r'"C:\x\OpenD_2\OpenD.exe"'}]))
    assert ok[0]["opend"] == r"C:\x\OpenD_2\OpenD.exe"                     # quotes from a copied path are fine
    from livepanel import settings
    f = settings.form_from(cfg([{"label": "A", "port": 11111}, {"label": "B", "port": 11112}]))
    f["moomoo"]["logins"][1]["opend"] = r"C:\x\notepad.exe"
    with pytest.raises(ValueError, match="OpenD program"):
        settings.apply({"brokers": {"use": ["moomoo"]}, "moomoo": {}}, f)


def test_a_disconnected_login_keeps_its_last_positions_greyed_and_out_of_the_totals():
    a, b2 = _login("Main", "Main·SG-0001", 3, 1.00), _login("B", "B·MY-0002", 5, 2.00)
    comp = Composite([a, b2])
    comp.connect()
    comp.snapshot(); comp.positions()
    assert comp.offline_positions() == {}
    def boom():
        raise RuntimeError("OpenD not reachable at 127.0.0.1:11112")
    b2.positions = boom                                                   # B's OpenD goes away
    live = comp.positions()
    off = comp.offline_positions()
    assert [p.account_id for p in live] == ["Main·SG-0001"] and list(off) == [b2.name]
    gs, foot = group_positions(live, {}, date(2026, 10, 19), show_und=False, offline={n: ps for n, (ps, _) in off.items()})
    g = gs[0]
    stale = [l for l in g["legs"] if l.get("stale")]
    assert len(stale) == 1 and stale[0]["closable"] == 0 and stale[0]["account"] == "B·MY-0002"
    assert g["live"] == 1 and g["mv"] == a.pos[0].market_value and foot["mv"] == a.pos[0].market_value
    keys = [l["key"] for l in g["legs"] if l["closable"] > 0]             # what "exit all" would send
    assert keys == [f"Main·SG-0001|{a.pos[0].instrument.key}"]


def test_nearest_expiry_and_option_vs_stock_split():
    c = Instrument("US.NVDA261218C150000", "NVDA 12/18 150C", AssetClass.OPTION, multiplier=100.0, underlying="NVDA",
                   expiry=date(2026, 12, 18), strike=150.0, right="C")
    p = Instrument("US.NVDA261023P150000", "NVDA 10/23 150P", AssetClass.OPTION, multiplier=100.0, underlying="NVDA",
                   expiry=date(2026, 10, 23), strike=150.0, right="P")
    s = Instrument("US.NVDA", "NVDA", AssetClass.STOCK)
    legs = [Position("A·SG-1", c, 1, 30.0, 33.0, 3300.0, 300.0, 0, 1), Position("A·SG-1", p, -1, 1.0, 0.5, -50.0, 50.0, 0, 1),
            Position("A·SG-1", s, 100, 170.0, 180.0, 18000.0, 1000.0, 0, 100)]
    g = group_positions(legs, {}, date(2026, 10, 21), show_und=False)[0][0]
    assert g["min_dte"] == 2 and g["split"] == {"opt": 350.0, "stk": 1000.0} and g["pl"] == 1350.0
    only_opt = group_positions(legs[:2], {}, date(2026, 10, 21), show_und=False)[0][0]
    assert only_opt["split"] is None


def test_a_percent_is_rounded_once():
    s = Instrument("US.TSLA", "TSLA", AssetClass.STOCK)
    g = group_positions([Position("A·MY-2", s, -20, 260.0, 251.0, -5020.0, 180.0, 0, 20)], {}, date(2026, 10, 21),
                        show_und=False)[0][0]
    assert g["pct"] == 3.46 and g["legs"][0]["pct"] == 3.46               # was 3.5, which the page showed as "4%"


def test_exit_preview_shows_the_first_price_the_engine_would_place(tmp_path):
    long_, short = _login("Main", "Main·SG-0001", 3, 1.00), _login("B", "B·MY-0002", -2, 1.00)
    for b in (long_, short):                                              # one contract, one market: same quote
        b.q = {k: Quote(1.00, 1.20, 0.01, 1.10) for k in b.q}
    short.pos[0].closable_qty = 2.0                                       # what MoomooBroker reports after closable()
    comp = Composite([long_, short])
    comp.connect()
    pos = comp.positions()
    rows = {r["account"]: r for r in Liquidator(comp, tmp_path, "test").preview(pos)}
    assert rows["Main·SG-0001"]["side"] == "SELL" and rows["Main·SG-0001"]["start"] == 1.14     # bid + 70% of 0.20
    assert rows["B·MY-0002"]["side"] == "BUY" and rows["B·MY-0002"]["start"] == 1.06           # ask - 70% of 0.20
    assert rows["B·MY-0002"]["qty"] == 2 and rows["B·MY-0002"]["mult"] == 100.0
    half = {r["account"]: r["start"] for r in Liquidator(comp, tmp_path, "test", start_frac=0.5).preview(pos)}
    assert half == {"Main·SG-0001": 1.10, "B·MY-0002": 1.10}
    assert not any(c[0] == "place" for b in (long_, short) for c in b.calls)                 # quotes only


def test_a_rehearsal_across_two_logins_keeps_each_legs_fills_apart(tmp_path):
    # 10-19: two dry-run logins both numbered their orders sim-1, sim-2 …, and a 10-lot showed "filled 20"
    from livepanel.brokers.fake import DryRun
    a, b2 = _login("Main", "Main·SG-0001", 10, 1.00), _login("B", "B·MY-0002", 20, 1.00)
    comp = Composite([DryRun(a), DryRun(b2)])
    comp.connect()
    pos = comp.positions()
    liq = Liquidator(comp, tmp_path, "dry", step_sec=0.05, now_fn=RTH)
    job = liq.start(pos, [f"{p.account_id}|{p.instrument.key}" for p in pos])
    job.done.wait(15)
    got = {l["account"]: l["filled"] for l in job.view()["legs"]}
    assert got == {"Main·SG-0001": 10, "B·MY-0002": 20}
    assert len({l["oid"] for l in job.view()["legs"]}) == 2
