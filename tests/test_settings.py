"""Settings page: TOML round-trip, form → config checks, the endpoints, and the safety rules around them."""
import ast
import http.client
import json
import socket
import threading
import tomllib
from datetime import datetime
from pathlib import Path

import pytest

from livepanel import brokers, settings
from livepanel.config import Config, load
from livepanel.liquidator import Liquidator
from livepanel.panel import Panel
from livepanel.server import serve

RAW = {
    "brokers": {"use": ["moomoo"]},
    "moomoo": {"host": "127.0.0.1", "port": 11111, "paper": False, "include_futures": True},
    "panel": {"port": 8790, "lang": "zh", "display_tz": "Asia/Kuala_Lumpur", "refresh_sec": 60, "data_dir": "~/.livepanel"},
    "fx": {},
    "market": {"extra_holidays": []},
    "trading": {"enabled": True, "dry_run": True, "exit_command": ["C:/py/pythonw.exe", "-X", "utf8", "C:/x/my_exit.py"],
                "exit_window_prefix": "My Exit"},
    "window": {"mode": "app", "width": 320, "height": 420},
}


def form(**over):
    f = settings.form_from(Config(json.loads(json.dumps(RAW))))
    f.update(over)
    return f


def test_toml_writer_round_trips_everything_the_config_holds():
    d = json.loads(json.dumps(RAW))
    d["moomoo"]["logins"] = [{"label": "主号", "port": 11111}, {"label": "号2", "port": 11112}]
    assert tomllib.loads(settings.dumps(d)) == d


def test_one_unnamed_login_stays_the_plain_form():
    out = settings.apply(RAW, form())
    assert out["moomoo"]["port"] == 11111 and "logins" not in out["moomoo"]


def test_several_logins_are_written_as_tables_and_checked():
    f = form()
    f["moomoo"]["logins"] = [{"label": "Main", "port": 11111}, {"label": "Mum", "port": 11112}]
    out = settings.apply(RAW, f)
    assert [(x["label"], x["port"]) for x in out["moomoo"]["logins"]] == [("Main", 11111), ("Mum", 11112)]
    assert "since" not in out["moomoo"]["logins"][0] and out["moomoo"]["logins"][1]["since"]   # Mum was added
    f["moomoo"]["logins"][1]["port"] = 11111
    with pytest.raises(ValueError, match="already taken"):
        settings.apply(RAW, f)
    f["moomoo"]["logins"] = [{"label": "", "port": 11111}, {"label": "B", "port": 11112}]
    with pytest.raises(ValueError, match="no label"):
        settings.apply(RAW, f)


@pytest.mark.parametrize("over, msg", [
    ({"brokers": []}, "at least one broker"),
    ({"display_tz": "Mars/Base"}, "time zone"),
    ({"refresh_sec": 2}, "5–600"),
    ({"lang": "fr"}, "en or zh"),
])
def test_bad_forms_are_refused(over, msg):
    with pytest.raises(ValueError, match=msg):
        settings.apply(RAW, form(**over))


def test_trading_and_other_sections_cannot_be_changed_from_the_page():
    f = form()
    f["trading"] = {"enabled": True, "dry_run": False}          # even if a page sent it
    out = settings.apply({**RAW, "trading": {"enabled": False, "dry_run": True}}, f)
    assert out["trading"] == {"enabled": False, "dry_run": True}
    assert out["window"] == RAW["window"] and out["fx"] == {}


def test_save_keeps_the_old_file(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("# my notes\n[brokers]\nuse = [\"demo\"]\n", encoding="utf-8")
    backup = settings.save(p, RAW, now=datetime(2026, 10, 18, 4, 30))
    assert backup.name == "config.toml.bak-20261018-043000" and "# my notes" in backup.read_text(encoding="utf-8")
    assert load(p).raw == RAW


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_test_connection_says_closed_for_a_port_nobody_listens_on():
    raw = {**RAW, "moomoo": {**RAW["moomoo"], "port": _free_port()}}
    rows = settings.check(raw)
    assert [r["code"] for r in rows] == ["closed"]


@pytest.fixture
def server(tmp_path):
    p = tmp_path / "config.toml"
    raw = {"brokers": {"use": ["demo"]}, "panel": {"data_dir": str(tmp_path), "lang": "en"},
           "trading": {"enabled": True, "dry_run": True}}
    p.write_text(settings.dumps(raw), encoding="utf-8")
    c = load(p)
    b = brokers.make(c)
    b.connect()
    panel = Panel(b, c)
    liq = Liquidator(b, tmp_path / "exit", "dry")
    calls = []
    port = _free_port()
    srv = serve(panel, liq, c, port, cfg_path=p, restart=lambda: calls.append("restart"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def req(method, path, token=None, body=None):
        con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"}
        if token:
            h["X-Token"] = token
        con.request(method, path, body=json.dumps(body or {}) if method == "POST" else None, headers=h)
        r = con.getresponse()
        return r.status, json.loads(r.read() or b"{}") if path.startswith("/api") else r.read()
    code, html = req("GET", "/")
    cfg_js = json.loads(html.decode("utf-8").split("const CFG = ", 1)[1].split(";", 1)[0])
    yield req, cfg_js, p, calls, liq
    srv.shutdown()
    srv.server_close()


def test_settings_endpoints_need_the_token(server):
    req, cfg_js, p, calls, _ = server
    assert cfg_js["settings"] is True and cfg_js["run_id"]
    assert req("GET", "/api/config")[0] == 403
    assert req("POST", "/api/config", body={"form": {}})[0] == 403
    assert req("POST", "/api/restart")[0] == 403 and calls == []


def test_settings_read_test_save_restart(server):
    req, cfg_js, p, calls, _ = server
    tok = cfg_js["token"]
    code, j = req("GET", "/api/config", token=tok)
    assert code == 200 and j["form"]["brokers"] == ["demo"] and j["path"] == str(p)
    f = j["form"]
    f["lang"] = "zh"
    code, j = req("POST", "/api/config/test", token=tok, body={"form": f})
    assert code == 200 and j["rows"][0]["code"] == "ok"
    code, j = req("POST", "/api/config", token=tok, body={"form": {**f, "refresh_sec": 1}})
    assert code == 400                                            # refused, file unchanged
    assert load(p).raw["panel"]["lang"] == "en"
    code, j = req("POST", "/api/config", token=tok, body={"form": f})
    assert code == 200 and load(p).raw["panel"]["lang"] == "zh" and Path(j["backup"]).exists()
    assert load(p).raw["trading"] == {"enabled": True, "dry_run": True}
    assert req("POST", "/api/restart", token=tok)[0] == 200 and calls == ["restart"]


def test_restart_is_refused_while_an_exit_job_runs(server):
    req, cfg_js, p, calls, liq = server
    from livepanel.brokers.fake import FakeBroker
    fb = FakeBroker.demo()
    fb.q["US.NVDA261218C150000"].bid = 1.0                      # never fills: the job keeps running
    liq.broker = fb
    liq.step_sec = 5
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    liq.now_fn = lambda: _dt(2026, 10, 2, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    p0 = fb.positions()[0]
    job = liq.start(fb.positions(), [f"{p0.account_id}|{p0.instrument.key}"])
    try:
        assert req("POST", "/api/restart", token=cfg_js["token"])[0] == 409 and calls == []
    finally:
        liq.stop(job.id)
        job.done.wait(10)


def test_settings_are_off_without_a_config_file(tmp_path):
    c = Config({"brokers": {"use": ["demo"]}, "panel": {"data_dir": str(tmp_path)}})
    b = brokers.make(c)
    b.connect()
    port = _free_port()
    srv = serve(Panel(b, c), None, c, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        con = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        con.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"})
        html = con.getresponse().read().decode("utf-8")
        assert json.loads(html.split("const CFG = ", 1)[1].split(";", 1)[0])["settings"] is False
    finally:
        srv.shutdown()
        srv.server_close()


def test_no_function_level_import_shadows_a_module_level_name():
    # a function-level `import x` makes x local for the whole function: an earlier use raises UnboundLocalError
    pkg = Path(__file__).resolve().parent.parent / "livepanel"
    bad = []
    for f in pkg.rglob("*.py"):
        t = ast.parse(f.read_text(encoding="utf-8"))
        top = {a.asname or a.name.split(".")[0] for n in t.body if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        for fn in (n for n in ast.walk(t) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            for n in ast.walk(fn):
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    bad += [f"{f.name}:{fn.name}:{a.asname or a.name}" for a in n.names if (a.asname or a.name.split(".")[0]) in top]
    assert bad == []


def test_shortcut_config_only_names_a_non_default_file(tmp_path):
    from livepanel.config import HOME
    from livepanel.window import launch_command, shortcut_config
    assert shortcut_config(None) is None
    assert shortcut_config(HOME / "config.toml") is None                  # default: the program finds it itself
    assert shortcut_config(tmp_path / "config.toml") == tmp_path / "config.toml"
    exe, args, wd, icon = launch_command(tmp_path / "config.toml")
    assert args.endswith(f'--window --config "{tmp_path / "config.toml"}"') and icon.endswith("icon.ico")


def test_tray_text_and_icon_ship_with_the_package():
    from livepanel import tray
    assert tray.icon_path().exists()
    assert set(tray.TEXT) == {"en", "zh"} and all(set(t) == {"tip", "open", "auto", "quit"} for t in tray.TEXT.values())


def test_with_live_orders_the_page_cannot_change_which_account_trades():
    live = {**RAW, "trading": {**RAW["trading"], "dry_run": False}}
    f = settings.form_from(Config(json.loads(json.dumps(live))))
    assert settings.apply(live, {**f, "lang": "en"})["panel"]["lang"] == "en"      # display settings: fine
    for change in ({"moomoo": {**f["moomoo"], "paper": True}},
                   {"moomoo": {**f["moomoo"], "logins": [{"label": "", "port": 11112}]}},
                   {"brokers": ["moomoo", "demo"]}):
        with pytest.raises(ValueError, match="orders are live"):
            settings.apply(live, {**f, **change})
    settings.apply(live, {**f, "ibkr": {**f["ibkr"], "port": 7497}})              # IBKR isn't used: harmless
    both = {**live, "brokers": {"use": ["moomoo", "ibkr"]}}
    fb = settings.form_from(Config(json.loads(json.dumps(both))))
    with pytest.raises(ValueError, match="orders are live"):
        settings.apply(both, {**fb, "ibkr": {**fb["ibkr"], "port": 7497}})        # used: live 7496 -> paper 7497


def test_a_date_in_the_file_is_refused_not_crashed(tmp_path):
    raw = {**RAW, "market": {"extra_holidays": [__import__("datetime").date(2028, 1, 17)]}}
    with pytest.raises(ValueError, match="can't edit"):
        settings.apply(raw, form())


def test_unlock_gives_moomoo_only_the_md5():
    import hashlib
    import types
    from livepanel.brokers.moomoo import MoomooBroker
    seen = []

    class TC:
        def unlock_trade(self, **kw):
            seen.append(kw)
            return 0, None
    b = MoomooBroker(label="X")
    b.m, b._ctx = types.SimpleNamespace(RET_OK=0), [TC(), TC()]                      # two security firms
    b.unlock("hunter22")
    assert seen == [{"password_md5": hashlib.md5(b"hunter22").hexdigest()}] * 2     # the password itself never passed


def test_update_check_only_announces_a_newer_full_release():
    from livepanel.updates import newer
    rel = lambda tag, pre=False: (lambda: {"tag_name": tag, "html_url": f"https://github.com/x/y/releases/tag/{tag}", "prerelease": pre})
    assert newer("0.4.0", rel("v0.5.0")) == {"version": "0.5.0", "url": "https://github.com/x/y/releases/tag/v0.5.0"}
    assert newer("0.4.0", rel("v0.4.0")) is None and newer("0.4.0", rel("v0.3.9")) is None
    assert newer("0.4.0", rel("v0.10.0")) is not None                     # numbers, not text: 0.10 > 0.4
    assert newer("0.4.0", rel("v0.5.0", pre=True)) is None
    def boom():
        raise OSError("offline")
    assert newer("0.4.0", boom) is None                                   # no network: quietly nothing


def test_phone_view_is_read_only_and_needs_its_key(tmp_path):
    from livepanel.server import serve_view, view_key
    c = Config({"brokers": {"use": ["demo"]}, "panel": {"data_dir": str(tmp_path)},
                "trading": {"enabled": True, "dry_run": True}})
    b = brokers.make(c)
    b.connect()
    p = Panel(b, c)
    p.tick(now=datetime(2026, 10, 2, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York")))
    key = view_key(tmp_path)
    assert view_key(tmp_path) == key and len(key) >= 20                     # the same link survives restarts
    port = _free_port()
    srv = serve_view(p, c, "127.0.0.1", port, key)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def req(method, path, host="phone.example:443"):
        con = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        con.request(method, path, body="{}" if method == "POST" else None, headers={"Host": host})
        r = con.getresponse()
        return r.status, r.read().decode("utf-8")
    try:
        assert req("GET", "/")[0] == 403 and req("GET", "/?k=wrong")[0] == 403 and req("GET", "/api/state")[0] == 403
        code, html = req("GET", f"/?k={key}")
        cfg_js = json.loads(html.split("const CFG = ", 1)[1].split(";", 1)[0])
        assert code == 200 and cfg_js["view_only"] and cfg_js["token"] == "" and not cfg_js["trading"] and not cfg_js["settings"]
        code, st = req("GET", f"/api/state?k={key}")
        s = json.loads(st)
        assert code == 200 and s["positions"] and s["jobs"] == [] and s["logins"] == []
        for path in ("/api/exit", "/api/unlock", "/api/config", "/api/restart", f"/api/exit?k={key}"):
            assert req("POST", path)[0] == 405                              # nothing on this server can act
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_exit_ladder_is_set_from_the_page_and_checked():
    f = form()
    f["trading"] = {"start_pct": 70, "step_sec": 10}
    assert "start_frac" not in settings.apply(RAW, f)["trading"]            # the defaults: file left alone
    f["trading"] = {"start_pct": 50, "step_sec": 5, "enabled": True, "dry_run": False}
    out = settings.apply(RAW, f)
    assert out["trading"]["start_frac"] == 0.5 and out["trading"]["step_sec"] == 5
    assert out["trading"]["dry_run"] is True                                # on/off still file-only
    assert Config(out).ladder == (0.5, 5)
    for bad, msg in (({"start_pct": 120, "step_sec": 10}, "0–100"), ({"start_pct": 70, "step_sec": 1}, "3–60")):
        f["trading"] = bad
        with pytest.raises(ValueError, match=msg):
            settings.apply(RAW, f)
    assert Config({"trading": {"start_frac": 7, "step_sec": "x"}}).ladder == (0.70, 10)   # a bad file can't stop the start
    assert settings.form_from(Config(out))["trading"]["start_pct"] == 50
