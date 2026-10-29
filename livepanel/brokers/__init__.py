"""Build the broker(s) named in config. Several brokers can feed one panel; Composite routes calls."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

from ..models import Instrument
from .base import Broker, BrokerError


class Composite(Broker):
    """Several brokers behind one interface. Account keys are unique per broker ("SG-1234", "IB-1234"),
    so trading calls route by account; quotes route by which broker reported the instrument."""

    def __init__(self, brokers: list[Broker]):
        self.brokers = brokers
        self.name = "+".join(b.name for b in brokers)
        self._acc: dict[str, Broker] = {}
        self._inst: dict[str, Broker] = {}
        self.errors: dict[str, str] = {}
        self._last_pos: dict[str, tuple[list, datetime]] = {}      # broker name -> last positions it reported, when

    def connect(self) -> None:
        for b in self.brokers:
            try:
                b.connect()
                self.errors.pop(b.name, None)
            except Exception as e:                      # one broker down must not hide the other
                self.errors[b.name] = str(e)

    def close(self) -> None:
        for b in self.brokers:
            try:
                b.close()
            except Exception:
                pass

    def reconnect_failed(self) -> None:
        for b in self.brokers:
            if b.name in self.errors:
                try:
                    b.close()
                    b.connect()
                    self.errors.pop(b.name, None)
                except Exception as e:
                    self.errors[b.name] = str(e)

    def connected(self) -> list[Broker]:
        return [b for b in self.brokers if b.name not in self.errors]

    def logins(self) -> list[dict]:
        """One row per broker connection, for the unlock dialog."""
        out = []
        for b in self.brokers:
            inner = getattr(b, "inner", b)
            out.append({"name": b.name, "label": getattr(inner, "label", None) or inner.name,
                        "unlock": bool(getattr(inner, "needs_unlock", False)), "connected": b.name not in self.errors,
                        "in_opend": bool(getattr(inner, "unlock_in_opend", False))})
        return out

    def unlock(self, name: str, password: str) -> None:
        for b in self.brokers:
            if b.name == name:
                return b.unlock(password)
        raise BrokerError(f"unknown login {name}")

    def broker_for_account(self, acc: str) -> Broker:
        if acc not in self._acc:
            raise BrokerError(f"unknown account {acc}")
        return self._acc[acc]

    def snapshot(self):
        out = []
        for b in self.connected():
            try:
                snaps = b.snapshot()
            except Exception as e:                      # e.g. gateway dropped: mark it, keep the others
                self.errors[b.name] = str(e)
                continue
            for s in snaps:
                self._acc[s.account_id] = b
                out.append(s)
        return out

    def positions(self):
        out = []
        for b in self.connected():
            try:
                ps = b.positions()
            except Exception as e:
                self.errors[b.name] = str(e)
                continue
            for p in ps:
                self._acc[p.account_id] = b
                self._inst[p.instrument.key] = b
            self._last_pos[b.name] = (ps, datetime.now(timezone.utc))
            out.extend(ps)
        return out

    def offline_positions(self) -> dict[str, tuple[list, datetime]]:
        """{broker name: (positions it last reported, when)} for brokers that are disconnected now. Shown greyed:
        a login that drops out must not look like its positions were closed. Never traded on."""
        return {n: v for n, v in self._last_pos.items() if n in self.errors}

    def quotes(self, instruments: list[Instrument]):
        out, by = {}, {}
        for i in instruments:
            b = self._inst.get(i.key)
            if b is not None:
                by.setdefault(id(b), (b, []))[1].append(i)
        for b, insts in by.values():
            out.update(b.quotes(insts))
        return out

    def cash_flows(self, start: date, end: date):
        out = []
        for b in self.connected():
            f = b.cash_flows(start, end)
            if f is None:
                return None
            out.extend(f)
        return out

    def underlying_quotes(self, symbols):
        out = {}
        for b in self.connected():
            missing = [s for s in symbols if s not in out]
            if not missing:
                break
            try:
                out.update(b.underlying_quotes(missing))
            except Exception:
                continue                                # no stock quotes from this one; try the next
        return out

    def recent_fills(self, days=3):
        out, any_known = [], False
        for b in self.connected():
            try:
                f = b.recent_fills(days)
            except Exception:
                f = None
            if f is not None:
                any_known = True
                out.extend(f)
        return out if any_known else None

    def fx_rates(self):
        out = {}
        for b in self.connected():
            out.update(b.fx_rates())
        return out

    def place_limit(self, account_id, inst, side, qty, price, session="rth"):
        return self.broker_for_account(account_id).place_limit(account_id, inst, side, qty, price, session)

    def modify_price(self, account_id, order_id, inst, qty_total, price):
        return self.broker_for_account(account_id).modify_price(account_id, order_id, inst, qty_total, price)

    def cancel(self, account_id, order_id):
        return self.broker_for_account(account_id).cancel(account_id, order_id)

    def order_states(self, account_id, order_ids):
        return self.broker_for_account(account_id).order_states(account_id, order_ids)


_BAD_LABEL = re.compile(r"[-|·\s]")


_OPEND_NAME = re.compile(r"(?i).*opend.*\.exe")         # OpenD.exe, moomoo_OpenD.exe, FutuOpenD.exe


def _is_opend(path: str) -> bool:
    from pathlib import PureWindowsPath
    return bool(_OPEND_NAME.fullmatch(PureWindowsPath(path).name))   # the file that would run, not a folder name


def _opend_path(v, who) -> str | None:
    """Where this login's OpenD program is, so the panel can start it when it isn't running. Only an OpenD
    executable is accepted: the settings page can write this value, and it must not become "run any program"."""
    if not v:
        return None
    p = str(v).strip().strip('"')
    if not _is_opend(p):
        raise ValueError(f"moomoo login {who!r}: opend must point to the OpenD program (…OpenD.exe), not {p!r}")
    return p


def moomoo_logins(cfg) -> list[dict]:
    """One OpenD serves one moomoo login. For several logins, list them as [[moomoo.logins]] tables, each with
    a label and its own OpenD api_port; fields a login leaves out come from [moomoo]. No list = one login, no label.
    since = "YYYY-MM-DD" marks a login added after the panel already had history: its balance the first time the
    panel sees it is booked as a deposit, so this week / this month don't count the whole account as profit."""
    keys = ("host", "port", "paper", "include_futures")
    base = {k: cfg.get("moomoo", k) for k in keys}
    raw = cfg.get("moomoo", "logins")
    if not raw:
        return [{"host": str(base["host"]), "port": int(base["port"]), "paper": bool(base["paper"]),
                 "include_futures": bool(base["include_futures"]), "label": None, "since": None,
                 "opend": _opend_path(cfg.get("moomoo", "opend"), "moomoo")}]
    if not isinstance(raw, list) or not all(isinstance(x, dict) for x in raw):
        raise ValueError("moomoo logins must be [[moomoo.logins]] tables")
    out, labels, ports = [], set(), set()
    for i, lg in enumerate(raw, 1):
        label = str(lg.get("label") or "").strip()
        if not label:
            raise ValueError(f'moomoo login #{i} has no label — add e.g. label = "Main"')
        if _BAD_LABEL.search(label) or len(label) > 12:
            raise ValueError(f"moomoo login {label!r}: a label is at most 12 characters, without spaces, '-', '|' or '·'")
        if label in labels:
            raise ValueError(f"moomoo login label {label!r} is used twice")
        host, port = str(lg.get("host", base["host"])), int(lg.get("port", base["port"]))
        if (host, port) in ports:
            raise ValueError(f"moomoo login {label!r}: {host}:{port} is already taken by another login — "
                             f"each login needs its own OpenD with its own api_port")
        since = lg.get("since")
        if since is not None:
            try:
                since = date.fromisoformat(str(since)).isoformat()
            except ValueError:
                raise ValueError(f"moomoo login {label!r}: since must be a date like 2026-10-19") from None
        labels.add(label)
        ports.add((host, port))
        out.append({"host": host, "port": port, "paper": bool(lg.get("paper", base["paper"])),
                    "include_futures": bool(lg.get("include_futures", base["include_futures"])), "label": label,
                    "since": since, "opend": _opend_path(lg.get("opend"), label)})
    return out


def make(cfg) -> Composite:
    bs: list[Broker] = []
    for name in cfg.brokers:
        if name == "moomoo":
            from .moomoo import MoomooBroker
            for lg in moomoo_logins(cfg):
                bs.append(MoomooBroker(lg["host"], lg["port"], paper=lg["paper"], include_futures=lg["include_futures"],
                                       cache_dir=cfg.data_dir, label=lg["label"]))
        elif name == "ibkr":
            from .ibkr import IbkrBroker
            bs.append(IbkrBroker(cfg.get("ibkr", "host"), int(cfg.get("ibkr", "port")),
                                 client_id=int(cfg.get("ibkr", "client_id")),
                                 readonly=not (cfg.trading_enabled and not cfg.dry_run),
                                 market_data_type=int(cfg.get("ibkr", "market_data_type"))))
        elif name == "demo":
            from .fake import FakeBroker
            bs.append(FakeBroker.demo())
        else:
            raise ValueError(f"unknown broker {name!r} (moomoo / ibkr / demo)")
    if cfg.trading_enabled and cfg.dry_run:
        from .fake import DryRun
        bs = [DryRun(b) if b.name != "fake" else b for b in bs]
    return Composite(bs)
