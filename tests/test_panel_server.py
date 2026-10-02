import http.client
import json
import socket
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from livepanel import brokers
from livepanel.config import Config
from livepanel.liquidator import Liquidator
from livepanel.panel import Panel
from livepanel.server import serve

NY = ZoneInfo("America/New_York")


def cfg(tmp_path, trading=True):
    return Config({"brokers": {"use": ["demo"]}, "panel": {"data_dir": str(tmp_path)},
                   "trading": {"enabled": trading, "dry_run": True}})


def test_tick_builds_state_and_freezes_after_hours(tmp_path):
    c = cfg(tmp_path)
    b = brokers.make(c)
    b.connect()
    p = Panel(b, c)
    st = p.tick(now=datetime(2026, 10, 2, 10, 0, tzinfo=NY))
    assert st["ok"] and not st["frozen"] and len(st["positions"]) == 3
    assert st["today"] == 85 - 30 + 44
    assert st["equity"] == 50_000
    st2 = p.tick(now=datetime(2026, 10, 3, 10, 0, tzinfo=NY))         # Saturday
    assert st2["frozen"] and st2["as_of"] == st["as_of"]            # last snapshot, not re-pulled


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_server_token_and_host_checks(tmp_path):
    c = cfg(tmp_path)
    b = brokers.make(c)
    b.connect()
    p = Panel(b, c)
    p.tick(now=datetime(2026, 10, 2, 10, 0, tzinfo=NY))
    liq = Liquidator(b, tmp_path / "exit", "dry")
    port = _free_port()
    srv = serve(p, liq, c, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def req(method, path, host=f"127.0.0.1:{port}", token=None, body=None):
            con = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            h = {"Host": host, "Content-Type": "application/json"}
            if token:
                h["X-Token"] = token
            con.request(method, path, body=json.dumps(body or {}) if method == "POST" else None, headers=h)
            r = con.getresponse()
            return r.status, r.read().decode("utf-8")

        code, html = req("GET", "/")
        assert code == 200 and '"token"' in html
        token = json.loads(html.split("const CFG = ", 1)[1].split(";", 1)[0])["token"]
        assert req("GET", "/", host="evil.example:80")[0] == 403          # DNS-rebinding guard
        assert req("POST", "/api/exit", body={"keys": ["x"]})[0] == 403    # no token
        code, st = req("GET", "/api/state")
        keys = [r["key"] for r in json.loads(st)["positions"]]
        code, body = req("POST", "/api/exit", token=token, body={"keys": keys[:1]})
        assert code == 200 and json.loads(body)["job"]
        assert req("POST", "/api/stop", token=token, body={})[0] == 200
    finally:
        srv.shutdown()


def test_after_hours_cold_start_pulls_once_but_records_no_close(tmp_path):
    c = cfg(tmp_path)
    b = brokers.make(c)
    b.connect()
    p = Panel(b, c)
    st = p.tick(now=datetime(2026, 10, 1, 21, 43, tzinfo=NY))       # Thursday evening, after the close
    assert st["ok"] and st["positions"] and st["frozen"] and st["pulled_live"] is False
    assert p.store.closes() == {}                                     # an after-hours mark is not a close
    p.tick(now=datetime(2026, 10, 2, 16, 10, tzinfo=NY), force=True)  # inside the grace window
    assert list(p.store.closes()) == ["2026-10-02"]
