"""Rehearse the exit engine on your real positions and live quotes without sending a single order.

    python scripts/dry_run_exit.py --config config.toml            # every position
    python scripts/dry_run_exit.py --config config.toml --only NVDA  # positions whose symbol contains NVDA

Run it during market hours. Every broker is wrapped in DryRun: reads go to the broker, orders are
simulated locally and "fill" when the market crosses them. The script refuses to start otherwise.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livepanel import brokers, config, sessions             # noqa: E402
from livepanel.brokers.fake import DryRun, FakeBroker       # noqa: E402
from livepanel.liquidator import Liquidator                 # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--only", default="")
    ap.add_argument("--timeout", type=float, default=300.0)
    a = ap.parse_args(argv)

    cfg = config.load(a.config)
    cfg.raw["trading"] = {**cfg.raw.get("trading", {}), "enabled": True, "dry_run": True}
    sessions.add_holidays(cfg.get("market", "extra_holidays"))
    b = brokers.make(cfg)
    bad = [x.name for x in b.brokers if not isinstance(x, (DryRun, FakeBroker))]
    if bad:
        print(f"refusing: {bad} would place real orders", file=sys.stderr)
        return 2
    b.connect()
    try:
        if b.errors:
            print("broker errors:", b.errors, file=sys.stderr)
        pos = b.positions()
        keys = [f"{p.account_id}|{p.instrument.key}" for p in pos if a.only.upper() in p.instrument.symbol.upper()]
        if not keys:
            print("no matching positions")
            return 1
        liq = Liquidator(b, cfg.data_dir / "exit_log", "dry", bool(cfg.get("trading", "allow_extended_hours")))
        job = liq.start(pos, keys)
        print(f"dry-run job {job.id}: {len(keys)} position(s)")
        seen, t0 = {}, time.monotonic()
        while not job.done.wait(1.0):
            for l in job.view()["legs"]:
                row = (l.get("state"), l.get("price"), l.get("bid"), l.get("ask"), l.get("filled"))
                if seen.get(l["key"]) != row:
                    seen[l["key"]] = row
                    print(f"{time.monotonic() - t0:6.1f}s  {l['name']:<22} {l['side']:<4} x{l['qty']:g}  "
                          f"price {l.get('price')}  bid/ask {l.get('bid')}/{l.get('ask')}  {l.get('state')}")
            if time.monotonic() - t0 > a.timeout:
                print("timeout -> stop")
                liq.stop(job.id)
        v = job.view()
        print("message:", v["message"])
        for l in v["legs"]:
            print(f"  {l['name']:<22} {l.get('state'):<18} start {l.get('start')}  last {l.get('price')}  filled {l.get('filled')}")
        return 0
    finally:
        b.close()


if __name__ == "__main__":
    sys.exit(main())
