"""livepanel [--init] [--window] [--make-shortcut] [--config PATH] [--demo] [--no-browser]

First time:   livepanel --window          (no config yet: one is created and the settings page opens)
Every day:    livepanel --window          (or the desktop shortcut from --make-shortcut)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
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
    ap.add_argument("--no-tray", action="store_true", help="no tray icon (Windows)")
    ap.add_argument("--port", type=int)
    ap.add_argument("--restarted", action="store_true", help=argparse.SUPPRESS)   # set by the settings page's restart
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

    path, first_run = None, False
    if a.demo:
        cfg = config.Config({"brokers": {"use": ["demo"]}})
    else:
        path = config.resolve(a.config)
        if not path.exists():
            if a.config:                                  # an explicit path that isn't there is a typo, not a first run
                print(f"no config at {path}", file=sys.stderr)
                return 1
            path, _ = config.init(path)                   # first run: start from the example, open the settings page
            first_run = True
        path = path.resolve()
        cfg = config.load(path)
    port = a.port or int(cfg.get("panel", "port"))
    url = f"http://127.0.0.1:{port}/"
    how = "none" if a.no_browser else ("window" if a.window else "tab")

    if _already_running(port):                       # second click on the shortcut: bring the open window back
        if how == "window":
            from .window import show_existing
            if show_existing():                      # also un-minimizes it
                return 0
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
        mode = "dry" if cfg.dry_run else ("paper" if _all_paper(cfg) else "real")
        liq = Liquidator(b, cfg.data_dir / "exit_log", mode, bool(cfg.get("trading", "allow_extended_hours")))
    restart_requested = threading.Event()
    srv_box: list = []

    def restart():
        restart_requested.set()
        threading.Thread(target=lambda: (time.sleep(0.3), srv_box[0].shutdown()), daemon=True).start()

    srv = None
    for attempt in range(40 if a.restarted else 1):      # after a restart the old process may hold the port a moment
        try:
            srv = serve(panel, liq, cfg, port, cfg_path=path, restart=restart if path else None, first_run=first_run)
            break
        except OSError as e:
            err = e
            time.sleep(0.25)
    if srv is None:
        print(f"port {port} is taken by another program ({err}); set [panel] port in the config", file=sys.stderr)
        b.close()
        return 1
    srv_box.append(srv)
    print(f"Live Panel on {url}  (trading: {'off' if not liq else liq.mode})")
    if sys.platform == "win32" and str(cfg.get("window", "mode") or "app") == "app":
        from .window import remember_loop
        stop = threading.Event()
        threading.Thread(target=remember_loop, args=(cfg.data_dir, stop), daemon=True).start()
    if not a.restarted:                                  # after a restart the window is already open and reloads itself
        _show(url, cfg, how)
    tray = _start_tray(cfg, url, path, srv, liq) if (sys.platform == "win32" and cfg.get("window", "tray")
                                                      and not a.no_tray) else None
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if tray:
            tray.stop()                                   # take the icon away before exiting (no ghost icon)
        srv.server_close()                                # free the port before a restarted copy binds it
        if liq:
            liq.stop()
        b.close()
    if restart_requested.is_set():
        args = (["--no-browser", "--restarted", "--config", str(path)] + (["--port", str(a.port)] if a.port else [])
                + (["--no-tray"] if a.no_tray else []))
        flags = 0x00000008 if sys.platform == "win32" else 0     # DETACHED_PROCESS
        env = dict(os.environ)
        if getattr(sys, "frozen", False):
            # a one-file .exe that starts itself must not reuse this copy's unpacked files: they are deleted when
            # this process exits, and this process's launcher waits on them (PyInstaller >= 6.9)
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        subprocess.Popen(_self_command() + args, cwd=os.getcwd(), creationflags=flags, env=env)   # no console: logs to data_dir
    return 0


def _start_tray(cfg, url, path, srv, liq):
    from .tray import Tray
    from .window import close_windows, shortcut_config, show_existing, startup_shortcut, write_shortcut

    def on_open():
        if not show_existing():
            _show(url, cfg, "window")

    def on_quit():
        if liq and any(not j["done"] for j in liq.view()):
            import ctypes
            zh = cfg.get("panel", "lang") == "zh"
            ctypes.windll.user32.MessageBoxW(None, "清仓任务还在跑：先在窗口里按「停止并撤单」，再结束。" if zh else
                                             "An exit job is still running: press Stop & cancel in the window first.",
                                             "Live Panel", 0x30 | 0x40000)
            return
        close_windows()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    def auto_set(on: bool):
        lnk = startup_shortcut()
        if on:
            write_shortcut(lnk, shortcut_config(path))
        else:
            lnk.unlink(missing_ok=True)

    t = Tray(cfg.get("panel", "lang"), on_open, on_quit,
             autostart=(lambda: startup_shortcut().exists(), auto_set) if path else None)
    if not t.start():
        print("tray icon could not be added", file=sys.stderr)
    return t


def _all_paper(cfg) -> bool:
    """Every broker in use trades a simulate / paper account: only then may an exit job be labelled "paper"."""
    kinds = []
    for name in cfg.brokers:
        if name == "moomoo":
            kinds += [lg["paper"] for lg in brokers.moomoo_logins(cfg)]
        elif name == "ibkr":
            kinds.append(int(cfg.get("ibkr", "port")) in (7497, 4002))
    return bool(kinds) and all(kinds)


def _self_command() -> list[str]:
    """How to start this program again: the packaged .exe itself, or python(w) -m livepanel."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    exe = os.path.basename(sys.executable).lower()
    return [sys.executable, "-m", "livepanel"] if exe.startswith("python") else [sys.executable]


def _log_if_windowless(cfg) -> None:
    """Started from the desktop shortcut (pythonw) there is no console: send output to a log file."""
    if sys.stdout is None or sys.stderr is None:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        f = open(cfg.data_dir / "livepanel.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f


if __name__ == "__main__":
    sys.exit(main())
