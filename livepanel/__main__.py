"""python -m livepanel [--config config.toml] [--demo] [--no-browser]"""
from __future__ import annotations

import argparse
import sys
import webbrowser

from . import brokers, config, sessions
from .liquidator import Liquidator
from .panel import Panel
from .server import serve


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="livepanel", description="Local live positions panel for moomoo / IBKR")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--demo", action="store_true", help="fake positions, nothing to install or log into")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--port", type=int)
    a = ap.parse_args(argv)

    cfg = config.Config({"brokers": {"use": ["demo"]}}) if a.demo else config.load(a.config)
    sessions.add_holidays(cfg.get("market", "extra_holidays"))
    b = brokers.make(cfg)
    b.connect()
    for name, err in b.errors.items():
        print(f"[{name}] {err}", file=sys.stderr)
    if not b.connected():
        print("no broker connected — see the messages above", file=sys.stderr)

    panel = Panel(b, cfg)
    panel.start()
    liq = None
    if cfg.trading_enabled:
        paper = bool(cfg.get("moomoo", "paper")) or int(cfg.get("ibkr", "port")) in (7497, 4002)
        mode = "dry" if cfg.dry_run else ("paper" if paper else "real")
        liq = Liquidator(b, cfg.data_dir / "exit_log", mode, bool(cfg.get("trading", "allow_extended_hours")))
    port = a.port or int(cfg.get("panel", "port"))
    srv = serve(panel, liq, cfg, port)
    url = f"http://127.0.0.1:{port}/"
    print(f"Live Panel on {url}  (trading: {'off' if not liq else liq.mode})")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if liq:
            liq.stop()
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
