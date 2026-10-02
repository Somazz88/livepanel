import socket
from pathlib import Path

from livepanel import config
from livepanel.__main__ import _already_running
from livepanel.window import app_window_args


def test_init_writes_example_once(tmp_path):
    t = tmp_path / "cfg" / "config.toml"
    p, created = config.init(t)
    assert created and p.read_text(encoding="utf-8").startswith("# `livepanel --init`")
    p.write_text("[brokers]\nuse = ['demo']\n", encoding="utf-8")
    p2, created2 = config.init(t)
    assert not created2 and "demo" in p2.read_text(encoding="utf-8")       # never overwrites an edited config


def test_resolve_prefers_explicit_then_local(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert config.resolve("x.toml") == Path("x.toml")
    assert config.resolve(None) == config.HOME / "config.toml"
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    assert config.resolve(None) == Path("config.toml")


def test_example_config_parses_with_safe_defaults():
    import tomllib
    d = tomllib.loads(config.EXAMPLE.read_text(encoding="utf-8"))
    assert d["trading"]["enabled"] is False and d["trading"]["dry_run"] is True
    assert d["window"]["mode"] == "app"


def test_app_window_args():
    a = app_window_args("chrome", "http://127.0.0.1:8790/", Path("/p"), 460, 720, -900, -2100)
    assert "--app=http://127.0.0.1:8790/" in a and "--window-size=460,720" in a and "--window-position=-900,-2100" in a


def test_already_running_false_on_a_free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert _already_running(port) is False


def test_window_rect_roundtrip(tmp_path):
    from livepanel.window import load_rect, save_rect
    assert load_rect(tmp_path) is None
    save_rect(tmp_path, (-900, -2100, 20, -660))
    assert load_rect(tmp_path) == (-900, -2100, 20, -660)
    (tmp_path / "window.json").write_text("{broken", encoding="utf-8")
    assert load_rect(tmp_path) is None
