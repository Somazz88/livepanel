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
    page_cfg = {"lang": cfg.get("panel", "lang"), "trading": cfg.trading_enabled, "dry_run": cfg.dry_run,
                "token": token}

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
            if self.path in ("/", "/index.html"):
                html = (WEB / "index.html").read_text(encoding="utf-8")
                html = html.replace("/*__CFG__*/{}", json.dumps(page_cfg))
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._json(200, {**panel.view(), "jobs": liquidator.view() if liquidator else [],
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
