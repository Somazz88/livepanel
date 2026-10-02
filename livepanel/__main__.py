"""livepanel [--init] [--window] [--make-shortcut] [--config PATH] [--demo] [--no-browser]

First time:   livepanel --init            (writes ~/.livepanel/config.toml — edit it)
Every day:    livepanel --window          (or the desktop shortcut from --make-shortcut)
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import webbrowser

from . import brokers, config, sessions
from .liquidator import Liquidator
from .panel import Panel
from .server import serve


def _already_running(port: int) -> bool:
    """Is a Live Panel (not some other program) already serving this port? Its /api/state always has 'trading'."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
            d = json.loads(r.read() or b"{}")
            return isinstance(d, dict) and "trading" in d
    except Exception:
        return False


def _show(url: str, cfg, how: str) -> None:
    if how == "window":
        from .window import open_window
        open_window(url, cfg)
    elif how == "tab":
        webbrowser.open(url)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="livepanel", description="Local live positions panel for moomoo / IBKR")
    ap.add_argument("--config", help="default: ./config.toml, else ~/.livepanel/config.toml")
    ap.add_argument("--init", action="store_true", help="create ~/.livepanel/config.toml from the example")
    ap.add_argument("--window", action="store_true", help="open as its own small window (Chrome / Edge app mode)")
    ap.add_argument("--make-shortcut", action="store_true", help="put a 'Live Panel' shortcut on the desktop (Windows)")
    ap.add_argument("--demo", action="store_true", help="fake positions, nothing to install or log into")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--port", type=int)
    a = ap.parse_args(argv)

    if a.init:
        path, created = config.init()
        print(("created " if created else "already exists: ") + str(path))
        print("edit it (broker, ports, language), then run:  livepanel --window")
        return 0
    if a.make_shortcut:
        from .window import make_shortcut
        print("created", make_shortcut(config=config.resolve(a.config) if a.config else None))
        return 0

    if a.demo:
        cfg = config.Config({"brokers": {"use": ["demo"]}})
    else:
        path = config.resolve(a.config)
        if not path.exists():
            print(f"no config at {path} — run `livepanel --init` first", file=sys.stderr)
            return 1
        cfg = config.load(path)
    port = a.port or int(cfg.get("panel", "port"))
    url = f"http://127.0.0.1:{port}/"
    how = "none" if a.no_browser else ("window" if a.window else "tab")

    if _already_running(port):                       # second click on the shortcut: just show it
        _show(url, cfg, how)
        return 0

    _log_if_windowless(cfg)
    sessions.add_holidays(cfg.get("market", "extra_holidays"))
    b = brokers.make(cfg)
    b.connect()
    for name, err in b.errors.items():
        print(f"[{name}] {err}", file=sys.stderr)
    if not b.connected():
        print("no broker connected yet — the panel will keep retrying", file=sys.stderr)

    panel = Panel(b, cfg)
    panel.start()
    liq = None
    if cfg.trading_enabled:
        paper = bool(cfg.get("moomoo", "paper")) or int(cfg.get("ibkr", "port")) in (7497, 4002)
        mode = "dry" if cfg.dry_run else ("paper" if paper else "real")
        liq = Liquidator(b, cfg.data_dir / "exit_log", mode, bool(cfg.get("trading", "allow_extended_hours")))
    try:
        srv = serve(panel, liq, cfg, port)
    except OSError as e:
        print(f"port {port} is taken by another program ({e}); set [panel] port in the config", file=sys.stderr)
        b.close()
        return 1
    print(f"Live Panel on {url}  (trading: {'off' if not liq else liq.mode})")
    if sys.platform == "win32" and str(cfg.get("window", "mode") or "app") == "app":
        import threading
        from .window import remember_loop
        stop = threading.Event()
        threading.Thread(target=remember_loop, args=(cfg.data_dir, stop), daemon=True).start()
    _show(url, cfg, how)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if liq:
            liq.stop()
        b.close()
    return 0


def _log_if_windowless(cfg) -> None:
    """Started from the desktop shortcut (pythonw) there is no console: send output to a log file."""
    if sys.stdout is None or sys.stderr is None:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        f = open(cfg.data_dir / "livepanel.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f


if __name__ == "__main__":
    sys.exit(main())
