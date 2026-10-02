"""Local web server: the page, a JSON state endpoint, and the exit endpoints.

Bound to 127.0.0.1 only. Order endpoints need the per-run token embedded in the page (another
website open in the same browser can't read it) and reject foreign Host headers (DNS rebinding).
"""
from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB = Path(__file__).parent / "web"


def serve(panel, liquidator, cfg, port: int):
    token = secrets.token_urlsafe(18)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    exit_cmd = cfg.get("trading", "exit_command")
    page_cfg = {"lang": cfg.get("panel", "lang"), "trading": cfg.trading_enabled, "dry_run": cfg.dry_run,
                "token": token, "exit_command": bool(exit_cmd)}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                html = (WEB / "index.html").read_text(encoding="utf-8")
                html = html.replace("/*__CFG__*/{}", json.dumps({**page_cfg, "ui_ver": _ui_ver()}))
                # declare the real language up front, or Chrome offers to "translate from Chinese" over the numbers
                html = html.replace('<html lang="en">', f'<html lang="{"zh-CN" if page_cfg["lang"] == "zh" else "en"}">', 1)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(200, {**panel.view(), "ui_ver": _ui_ver(), "jobs": liquidator.view() if liquidator else [],
                                        "trading": {"enabled": cfg.trading_enabled, "dry_run": cfg.dry_run}})
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Token") != token:
                return self._json(403, {"error": "forbidden"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad json"})
            if self.path == "/api/refresh":
                panel.refresh_now()
                return self._json(200, {"ok": True})
            if self.path == "/api/exit":
                if not (cfg.trading_enabled and liquidator):
                    return self._json(403, {"error": "trading is disabled in config"})
                keys = [str(k) for k in body.get("keys") or []]
                if not keys:
                    return self._json(400, {"error": "no positions selected"})
                try:
                    job = liquidator.start(panel.positions, keys)
                except ValueError as e:
                    return self._json(409, {"error": str(e)})
                return self._json(200, {"job": job.id})
            if self.path == "/api/exit_command":
                if not exit_cmd:
                    return self._json(403, {"error": "no exit_command configured"})
                return self._json(200, run_exit_command(exit_cmd, cfg.get("trading", "exit_window_prefix")))
            if self.path == "/api/stop":
                return self._json(200, {"stopped": liquidator.stop(body.get("job")) if liquidator else 0})
            return self._json(404, {"error": "not found"})

    srv = _Server(("127.0.0.1", port), H)
    return srv


class _Server(ThreadingHTTPServer):
    # HTTPServer turns on SO_REUSEADDR. On Windows that lets a second process bind a port that is already
    # in use, and requests then go to whichever socket Windows picks — found when this panel and another
    # local tool both sat on 8765. Bind exclusively so a taken port fails loudly instead.
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _ui_ver() -> int:
    """Changes when the page file changes: an open window reloads itself after an upgrade."""
    try:
        return int((WEB / "index.html").stat().st_mtime)
    except OSError:
        return 0


def run_exit_command(cmd, window_prefix: str | None = None) -> dict:
    """[trading] exit_command: hand the exit to your own tool (it does its own confirming).
    If its window is already open (title starts with exit_window_prefix), bring that one back instead of
    starting a second copy — a second copy could work the same positions twice."""
    import shlex
    import subprocess
    import sys
    if window_prefix and sys.platform == "win32":
        h = _find_window(window_prefix)
        if h:
            _bring_front(h)
            return {"ok": True, "how": "front"}
    args = cmd if isinstance(cmd, list) else shlex.split(str(cmd), posix=(sys.platform != "win32"))
    subprocess.Popen([str(a) for a in args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "how": "started"}


def _find_window(prefix: str):
    import ctypes
    import ctypes.wintypes as wt
    u, hit = ctypes.windll.user32, []

    def cb(h, _):
        b = ctypes.create_unicode_buffer(128)
        u.GetWindowTextW(h, b, 128)
        if u.IsWindowVisible(h) and b.value.startswith(prefix):
            hit.append(h)
        return True
    u.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(cb), 0)
    return hit[0] if hit else None


def _bring_front(h) -> None:
    import ctypes
    u = ctypes.windll.user32
    if u.IsIconic(h):
        u.ShowWindow(h, 9)                                         # SW_RESTORE
    flags = 0x0001 | 0x0002                                        # NOSIZE | NOMOVE
    u.SetWindowPos(h, ctypes.c_void_p(-1), 0, 0, 0, 0, flags)      # topmost, then back: lands on top even if
    u.SetWindowPos(h, ctypes.c_void_p(-2), 0, 0, 0, 0, flags)      # Windows refuses the foreground switch
    u.SetForegroundWindow(h)
