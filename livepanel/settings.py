"""The settings page's back end: read the editable part of config.toml, check it, write it back.

Connection and display settings are editable here. Of [trading], the page can only turn the exit buttons on as a
rehearsal (dry_run = true: nothing is sent) or off again; dry_run = false stays file-only on purpose, so a click in
the page can never turn live orders on, and while orders are live the page can't change [trading] at all. Every save keeps the previous
file as config.toml.bak-<time>; comments in the file are not kept.
"""
from __future__ import annotations

import json
import re
import socket
import threading
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .util import scrub

BROKERS = ("moomoo", "ibkr", "demo")
TIMEZONES = ("Asia/Kuala_Lumpur", "Asia/Singapore", "Asia/Hong_Kong", "Asia/Shanghai", "Asia/Taipei",
             "Asia/Tokyo", "Australia/Sydney", "Europe/London", "America/New_York", "America/Los_Angeles")


def form_from(cfg: Config) -> dict:
    """What the page edits, filled from the file plus defaults."""
    logins = cfg.get("moomoo", "logins") or [{"label": "", "port": int(cfg.get("moomoo", "port")),
                                              "opend": cfg.get("moomoo", "opend") or ""}]
    return {
        "lang": cfg.get("panel", "lang"),
        "display_tz": cfg.get("panel", "display_tz"),
        "refresh_sec": int(cfg.get("panel", "refresh_sec")),
        "brokers": list(cfg.brokers),
        "moomoo": {"host": cfg.get("moomoo", "host"), "paper": bool(cfg.get("moomoo", "paper")),
                   "logins": [{"label": str(x.get("label") or ""), "port": int(x.get("port", cfg.get("moomoo", "port"))),
                               "opend": str(x.get("opend") or "")} for x in logins]},
        "ibkr": {"host": cfg.get("ibkr", "host"), "port": int(cfg.get("ibkr", "port"))},
        # dry run is shown, not editable (on/off only as a rehearsal, see apply); the exit ladder (where the first order sits, how often it
        # moves one tick) is editable: it never crosses the bid / ask either way
        "trading": {"enabled": cfg.trading_enabled, "dry_run": cfg.dry_run,
                    "start_pct": round(cfg.ladder[0] * 100), "step_sec": int(cfg.ladder[1])},
    }


def apply(raw: dict, form: dict) -> dict:
    """New raw config = old raw with the form's fields written in. Raises ValueError with a readable reason."""
    from .brokers import moomoo_logins
    try:
        out = json.loads(json.dumps(raw))                 # deep copy; the file normally holds JSON-like values only
    except TypeError:
        raise ValueError("the config file holds a value the page can't edit (a date?) — edit it by hand") from None
    use = [b for b in form.get("brokers") or [] if b in BROKERS]
    if not use:
        raise ValueError("pick at least one broker")
    lang = form.get("lang")
    if lang not in ("en", "zh"):
        raise ValueError("language must be en or zh")
    tz = str(form.get("display_tz") or "")
    try:
        ZoneInfo(tz)
    except Exception:
        raise ValueError(f"unknown time zone {tz!r}") from None
    refresh = _int(form.get("refresh_sec"), "refresh interval")
    if not 5 <= refresh <= 600:
        raise ValueError("refresh interval must be 5–600 seconds")
    out.setdefault("brokers", {})["use"] = use
    panel = out.setdefault("panel", {})
    panel.update({"lang": lang, "display_tz": tz, "refresh_sec": refresh})

    mm = form.get("moomoo") or {}
    m = out.setdefault("moomoo", {})
    m["host"] = str(mm.get("host") or "127.0.0.1").strip()
    m["paper"] = bool(mm.get("paper"))
    logins = [{"label": str(x.get("label") or "").strip(), "port": _port(x.get("port")),
               "opend": str(x.get("opend") or "").strip().strip('"')} for x in mm.get("logins") or []]
    # a login that wasn't in the file before joins with its whole balance: mark it, so the panel books that
    # balance as a deposit instead of this week's profit (see brokers.moomoo_logins). Existing ones keep theirs.
    old_logins = (raw.get("moomoo") or {}).get("logins")
    old_by_label = {str(x.get("label")): x for x in old_logins or []}
    old_port = (raw.get("moomoo") or {}).get("port", 11111)
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    for x in logins:
        if x["label"] in old_by_label:
            if old_by_label[x["label"]].get("since"):
                x["since"] = old_by_label[x["label"]]["since"]
        elif not (old_logins is None and x["port"] == old_port):   # one login turning into several keeps its history
            x["since"] = today
    if "moomoo" in use and not logins:
        raise ValueError("moomoo needs at least one OpenD port")
    if len(logins) == 1 and not logins[0]["label"]:
        m["port"] = logins[0]["port"]                     # one login: the plain form, account keys stay "SG-1234"
        m.pop("logins", None)
        if logins[0]["opend"]:
            m["opend"] = logins[0]["opend"]
        else:
            m.pop("opend", None)
    elif logins:
        m["logins"] = [{k: x[k] for k in ("label", "port", "since", "opend") if x.get(k)} for x in logins]

    ib = form.get("ibkr") or {}
    i = out.setdefault("ibkr", {})
    i["host"] = str(ib.get("host") or "127.0.0.1").strip()
    i["port"] = _port(ib.get("port", i.get("port", 7496)))

    ft = form.get("trading") or {}
    if "start_pct" in ft or "step_sec" in ft:
        pct = _int(ft.get("start_pct", 70), "first price")
        if not 0 <= pct <= 100:
            raise ValueError("first price must be 0–100% of the spread")
        step = _int(ft.get("step_sec", 10), "step interval")
        if not 3 <= step <= 60:
            raise ValueError("step interval must be 3–60 seconds")
        f0, s0 = Config(raw).ladder
        if (pct, step) != (round(f0 * 100), int(s0)):         # untouched: leave the file as it was
            out.setdefault("trading", {}).update({"start_frac": pct / 100, "step_sec": step})
    tr = raw.get("trading") or {}
    live = bool(tr.get("enabled")) and not tr.get("dry_run", True)
    if "rehearsal" in ft and not live and bool(ft["rehearsal"]) != Config(raw).trading_enabled:
        # the page's only on/off: exit buttons as a rehearsal (real quotes, simulated fills), or no exit buttons.
        # It always writes dry_run = true, never false; going live is a hand edit of the file.
        t = out.setdefault("trading", {})
        t["enabled"] = bool(ft["rehearsal"])
        t["dry_run"] = True

    if "moomoo" in use:
        moomoo_logins(Config(out))                        # same checks the panel runs at start: labels, ports
    if live:
        # orders are live: switching paper → real, a port or the logins here would change which account the next
        # exit trades. Those changes go through the file (or turn dry_run on first).
        def where(r):                                     # the effective values, defaults filled in
            c = Config(r)
            mm = [(x["host"], x["port"], x["paper"], x["label"]) for x in moomoo_logins(c)] if "moomoo" in c.brokers else []
            ib = (c.get("ibkr", "host"), int(c.get("ibkr", "port"))) if "ibkr" in c.brokers else None
            return tuple(c.brokers), tuple(mm), ib
        if where(out) != where(raw):
            raise ValueError("orders are live: brokers, logins, ports and paper can't be changed here — edit the file, "
                             "or set dry_run = true first")
    return out


def _int(v, what: str) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number") from None


def _port(v) -> int:
    p = _int(v, "port")
    if not 1 <= p <= 65535:
        raise ValueError(f"port {p} is out of range")
    return p


# ── writing TOML (the standard library only reads it) ───────────────────────────
_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k: str) -> str:
    return k if _BARE.match(k) else json.dumps(k, ensure_ascii=False)


def _val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)          # a JSON string is a valid TOML basic string
    if isinstance(v, list) and all(not isinstance(x, (dict, list)) for x in v):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    raise TypeError(f"can't write {type(v).__name__} to TOML")


def _is_tables(v) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def _table(lines: list[str], name: str, t: dict) -> None:
    lines += ["", f"[{name}]"]
    later = []
    for k, v in t.items():
        if isinstance(v, dict) or _is_tables(v):
            later.append((k, v))
        else:
            lines.append(f"{_key(k)} = {_val(v)}")
    for k, v in later:
        if isinstance(v, dict):
            _table(lines, f"{name}.{_key(k)}", v)
        else:
            for item in v:
                lines += ["", f"[[{name}.{_key(k)}]]"]
                lines += [f"{_key(kk)} = {_val(vv)}" for kk, vv in item.items()]


def dumps(d: dict) -> str:
    lines = [f"{_key(k)} = {_val(v)}" for k, v in d.items() if not (isinstance(v, dict) or _is_tables(v))]
    for k, v in d.items():
        if isinstance(v, dict):
            _table(lines, _key(k), v)
    text = "\n".join(lines).lstrip("\n") + "\n"
    if tomllib.loads(text) != d:                          # never write a file that reads back differently
        raise ValueError("internal error: the settings did not survive a write/read check")
    return text


def save(path: Path, raw: dict, now: datetime | None = None) -> Path | None:
    """Write raw to path; the old file is kept next to it. Returns the backup's path (None if there was no file)."""
    path = Path(path)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    text = (f"# Written by the Live Panel settings page {stamp}. The previous file (with any comments) is kept\n"
            f"# next to this one as {path.name}.bak-{stamp}. dry_run = false can only be set here, by hand.\n\n"
            + dumps(raw))
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        backup.write_bytes(path.read_bytes())
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return backup


# ── "test connection" ─────────────────────────────────────────────────────────────
def check(raw: dict, timeout: float = 25.0) -> list[dict]:
    """One row per thing to connect to: {name, code: ok|closed|error|timeout, detail, accounts}."""
    from .brokers import moomoo_logins
    cfg = Config(raw)
    rows = []
    for b in cfg.brokers:
        if b == "demo":
            rows.append({"name": "demo", "code": "ok", "detail": "", "accounts": ["DEMO-0001"]})
        elif b == "ibkr":
            host, port = cfg.get("ibkr", "host"), int(cfg.get("ibkr", "port"))
            ok = _port_open(host, port)
            rows.append({"name": f"IBKR {port}", "code": "ok" if ok else "closed", "detail": f"{host}:{port}",
                         "accounts": []})
        elif b == "moomoo":
            for lg in moomoo_logins(cfg):
                rows.append(_check_moomoo(lg, timeout))
    return rows


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _check_moomoo(lg: dict, timeout: float) -> dict:
    name = f"{lg['label'] or 'moomoo'} {lg['port']}"
    if not _port_open(lg["host"], lg["port"]):
        return {"name": name, "code": "closed", "detail": f"{lg['host']}:{lg['port']}", "accounts": []}
    box: dict = {}

    def run():
        from .brokers.moomoo import MoomooBroker
        b = MoomooBroker(lg["host"], lg["port"], paper=lg["paper"], include_futures=False, label=lg["label"])
        try:
            b.connect()
            box["accounts"] = sorted(b.acc)
        except Exception as e:                            # BrokerError or a missing moomoo-api
            box["error"] = scrub(e, 200)
        finally:
            try:
                b.close()
            except Exception:
                pass
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return {"name": name, "code": "timeout", "detail": f"{lg['host']}:{lg['port']}", "accounts": []}
    if "error" in box:
        return {"name": name, "code": "error", "detail": box["error"], "accounts": []}
    return {"name": name, "code": "ok", "detail": "", "accounts": box.get("accounts", [])}
