"""Is there a newer release on GitHub? One small request every 12 hours; nothing is sent about you or your accounts.
Turn it off with `check_updates = false` under [panel]."""
from __future__ import annotations

import json
import re
import threading
import urllib.request

from . import __version__

LATEST = "https://api.github.com/repos/Somazz88/livepanel/releases/latest"   # full releases only, not pre-releases


def _ver(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3])


def newer(current: str, fetch=None) -> dict | None:
    """{"version", "url"} when the latest full release is newer than `current`, else None. Errors are None too."""
    try:
        if fetch is None:
            req = urllib.request.Request(LATEST, headers={"User-Agent": f"livepanel/{current}",
                                                          "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read())
        else:
            d = fetch()
        tag = str(d.get("tag_name") or "")
        if tag and _ver(tag) > _ver(current) and not d.get("prerelease") and not d.get("draft"):
            return {"version": tag.lstrip("v"), "url": str(d.get("html_url") or "")}
    except Exception:
        pass
    return None


class Watcher:
    def __init__(self, every_sec: float = 12 * 3600, current: str = __version__, fetch=None):
        self.every, self.current, self.fetch = every_sec, current, fetch
        self.latest: dict | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        def loop():
            while not self._stop.is_set():
                self.latest = newer(self.current, self.fetch)
                self._stop.wait(self.every)
        threading.Thread(target=loop, daemon=True, name="update-check").start()

    def stop(self) -> None:
        self._stop.set()
