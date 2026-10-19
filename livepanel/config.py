"""config.toml loading with safe defaults (trading off unless the user turns it on)."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT = {
    "brokers": {"use": ["demo"]},
    "moomoo": {"host": "127.0.0.1", "port": 11111, "paper": False, "include_futures": True},
    "ibkr": {"host": "127.0.0.1", "port": 7496, "client_id": 17, "market_data_type": 1},
    "panel": {"port": 8790, "lang": "en", "display_tz": "America/New_York", "refresh_sec": 20,
              "lead_min": 30, "grace_min": 15, "base_currency": "USD", "data_dir": "~/.livepanel",
              "check_updates": True},
    "fx": {},
    "market": {"extra_holidays": []},
    "trading": {"enabled": False, "dry_run": True, "allow_extended_hours": False,
                "exit_command": None, "exit_window_prefix": None, "start_frac": 0.70, "step_sec": 10},
    "window": {"mode": "app", "width": 320, "height": 420, "tray": True},
    "phone": {"enabled": False, "bind": "127.0.0.1", "port": 8791, "poll_sec": 60},
}
HOME = Path.home() / ".livepanel"
EXAMPLE = Path(__file__).parent / "config.example.toml"


def resolve(path: str | None) -> Path:
    """--config if given; else ./config.toml if it exists; else ~/.livepanel/config.toml."""
    if path:
        return Path(path)
    local = Path("config.toml")
    return local if local.exists() else HOME / "config.toml"


def init(target: Path | None = None) -> tuple[Path, bool]:
    """Write the example config to ~/.livepanel/config.toml (never overwrites). Returns (path, created)."""
    t = Path(target or HOME / "config.toml")
    if t.exists():
        return t, False
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return t, True


@dataclass
class Config:
    raw: dict = field(default_factory=dict)

    def get(self, section: str, key: str):
        return self.raw.get(section, {}).get(key, DEFAULT.get(section, {}).get(key))

    @property
    def data_dir(self) -> Path:
        return Path(str(self.get("panel", "data_dir"))).expanduser()

    @property
    def brokers(self) -> list[str]:
        use = self.get("brokers", "use")
        return [use] if isinstance(use, str) else list(use)

    @property
    def trading_enabled(self) -> bool:
        return bool(self.get("trading", "enabled"))

    @property
    def dry_run(self) -> bool:
        return bool(self.get("trading", "dry_run"))

    @property
    def ladder(self) -> tuple[float, float]:
        """(start_frac, step_sec) for exits. A value out of range falls back to the default instead of stopping the
        panel from starting; the settings page refuses such values before they are written."""
        d = DEFAULT["trading"]
        try:
            f = float(self.get("trading", "start_frac"))
        except (TypeError, ValueError):
            f = d["start_frac"]
        try:
            s = float(self.get("trading", "step_sec"))
        except (TypeError, ValueError):
            s = d["step_sec"]
        return (f if 0.0 <= f <= 1.0 else d["start_frac"]), (s if 3 <= s <= 120 else d["step_sec"])


def load(path: str | Path | None) -> Config:
    if not path:
        return Config({})
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"{p} not found — run `livepanel --init` to create it, then edit it")
    with p.open("rb") as f:
        return Config(tomllib.load(f))
