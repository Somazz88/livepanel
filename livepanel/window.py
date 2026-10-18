"""Open the panel as its own small window (no tabs, no address bar) and make a desktop shortcut.

The window is Chrome / Edge in --app mode with a separate profile under data_dir, so it remembers
its own size and position and never touches the user's normal browser tabs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

_CANDIDATES = {
    "win32": [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")],
    "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
               "/Applications/Chromium.app/Contents/MacOS/Chromium"],
}


def find_browser() -> str | None:
    for p in _CANDIDATES.get(sys.platform, []):
        if p and Path(p).exists():
            return p
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge", "msedge", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    return None


def app_window_args(browser: str, url: str, profile: Path, width: int, height: int,
                    x: int | None = None, y: int | None = None) -> list[str]:
    args = [browser, f"--app={url}", f"--user-data-dir={profile}", f"--window-size={width},{height}",
            "--no-first-run", "--no-default-browser-check"]
    if x is not None and y is not None:
        args.append(f"--window-position={x},{y}")
    return args


def open_window(url: str, cfg) -> str:
    """Returns how it opened: 'app' or 'tab'."""
    mode = str(cfg.get("window", "mode") or "app")
    browser = find_browser() if mode == "app" else None
    if not browser:
        webbrowser.open(url)
        return "tab"
    profile = cfg.data_dir / "window-profile"
    profile.mkdir(parents=True, exist_ok=True)
    saved = load_rect(cfg.data_dir)
    args = app_window_args(browser, url, profile, int(cfg.get("window", "width")), int(cfg.get("window", "height")))
    if sys.platform == "win32" and saved and _win().on_a_monitor(saved):
        # Chrome reads --window-position / --window-size in its own scaled pixels: per monitor, real pixels
        # divided by that monitor's scale (200% screen at x=-960 is x=-480 to Chrome). Converting here makes
        # the window open where it belongs instead of flashing on the main screen first.
        sc = _win().scale_at(saved)
        args = [a for a in args if not a.startswith("--window-size=")]
        args += [f"--window-position={round(saved[0] / sc)},{round(saved[1] / sc)}",
                 f"--window-size={round((saved[2] - saved[0]) / sc)},{round((saved[3] - saved[1]) / sc)}"]
    flags = 0x00000008 if sys.platform == "win32" else 0          # DETACHED_PROCESS: closing the console won't kill it
    subprocess.Popen(args, creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == "win32" and saved:
        # Chrome's --window-position is in scaled pixels and lands in the wrong place on mixed-DPI
        # multi-monitor setups (tested: 200% screen above a 100% one), and its own saved app-window
        # placement was not restored either. So put the window back ourselves, in real pixels.
        h = _win().find(TITLE, timeout=10)
        if h and _win().on_a_monitor(saved):
            _win().place(h, saved)
    return "app"


# ── Windows: remember where the user left the window, in real pixels ─────
TITLE = "Live Panel"


def load_rect(data_dir: Path):
    import json
    try:
        r = json.loads((Path(data_dir) / "window.json").read_text(encoding="utf-8"))["rect"]
        return tuple(int(v) for v in r) if len(r) == 4 else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_rect(data_dir: Path, rect) -> None:
    import json
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    (Path(data_dir) / "window.json").write_text(json.dumps({"rect": list(rect)}), encoding="utf-8")


def remember_loop(data_dir: Path, stop, every: float = 3.0) -> None:
    """Runs in the server process: whenever the app window moves, store its rectangle."""
    if sys.platform != "win32":
        return
    last = load_rect(data_dir)
    while not stop.wait(every):
        try:
            h = _win().find(TITLE, timeout=0)
            if h and not _win().minimized(h):
                r = _win().rect(h)
                if r != last and _win().on_a_monitor(r):
                    save_rect(data_dir, r)
                    last = r
        except Exception:
            pass


class _Win:
    def __init__(self):
        import ctypes
        import ctypes.wintypes as wt
        self.c, self.wt, self.u = ctypes, wt, ctypes.windll.user32
        try:                                    # real pixels, whatever each monitor's scaling is
            self.u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass

    def find(self, title: str, timeout: float = 10):
        import time
        c, wt, u = self.c, self.wt, self.u
        end = time.monotonic() + timeout
        while True:
            hit = []

            def cb(h, _):
                n = u.GetWindowTextLengthW(h)
                if n and u.IsWindowVisible(h):
                    b = c.create_unicode_buffer(n + 1)
                    u.GetWindowTextW(h, b, n + 1)
                    cls = c.create_unicode_buffer(64)
                    u.GetClassNameW(h, cls, 64)
                    if b.value == title and cls.value.startswith("Chrome_WidgetWin"):
                        hit.append(h)
                return True
            u.EnumWindows(c.WINFUNCTYPE(c.c_bool, wt.HWND, wt.LPARAM)(cb), 0)
            if hit or time.monotonic() >= end:
                return hit[0] if hit else None
            time.sleep(0.25)

    def rect(self, h):
        r = self.wt.RECT()
        self.u.GetWindowRect(h, self.c.byref(r))
        return (r.left, r.top, r.right, r.bottom)

    def minimized(self, h) -> bool:
        return bool(self.u.IsIconic(h))

    def on_a_monitor(self, rect) -> bool:
        r = self.wt.RECT(*rect)
        return bool(self.u.MonitorFromRect(self.c.byref(r), 0))          # MONITOR_DEFAULTTONULL

    def scale_at(self, rect) -> float:
        """Display scale (1.0 = 100%) of the monitor holding most of `rect`."""
        r = self.wt.RECT(*rect)
        mon = self.u.MonitorFromRect(self.c.byref(r), 2)                  # MONITOR_DEFAULTTONEAREST
        x, y = self.c.c_uint(), self.c.c_uint()
        try:
            self.c.windll.shcore.GetDpiForMonitor(mon, 0, self.c.byref(x), self.c.byref(y))   # MDT_EFFECTIVE_DPI
            return (x.value or 96) / 96
        except Exception:
            return 1.0

    def place(self, h, rect) -> None:
        import time
        left, top, right, bottom = rect
        for _ in range(3):              # moving across monitors with different scaling resizes the window; repeat until it sticks
            self.u.SetWindowPos(h, 0, left, top, right - left, bottom - top, 0x0004 | 0x0010)
            time.sleep(0.3)
            if self.rect(h) == tuple(rect):
                break


_W = None


def _win() -> _Win:
    global _W
    if _W is None:
        _W = _Win()
    return _W


def _desktop() -> Path:
    """The real Desktop folder: often moved into OneDrive, so ~/Desktop can be the wrong place."""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "[Environment]::GetFolderPath('Desktop')"], capture_output=True, text=True, timeout=20)
        p = Path(r.stdout.strip())
        if r.returncode == 0 and r.stdout.strip() and p.is_dir():
            return p
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(os.path.expanduser("~")) / "Desktop"


def launch_command(config: Path | None = None) -> tuple[str, str, str, str]:
    """(program, arguments, working dir, icon) that open the panel window without a console.
    Used by the desktop shortcut and by "Start with Windows"."""
    cfg_arg = f' --config "{config}"' if config else ""
    if getattr(sys, "frozen", False):                       # LivePanel.exe: point at the .exe itself
        exe = Path(sys.executable)
        return str(exe), "--window" + cfg_arg, str(exe.parent), str(exe)
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    src = Path(__file__).resolve().parent.parent            # running from a source checkout (not pip-installed)?
    wd = src if (src / "pyproject.toml").exists() else Path.home()
    return str(exe), "-m livepanel --window" + cfg_arg, str(wd), str(Path(__file__).resolve().parent / "web" / "icon.ico")


def write_shortcut(lnk: Path, config: Path | None = None) -> Path:
    exe, args, wd, icon = launch_command(config)
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LP_LNK);"
          "$s.TargetPath=$env:LP_EXE;$s.Arguments=$env:LP_ARGS;$s.WorkingDirectory=$env:LP_WD;"
          "if (Test-Path $env:LP_ICO) { $s.IconLocation=$env:LP_ICO };"
          "$s.Description='Live Panel';$s.Save()")
    env = {**os.environ, "LP_LNK": str(lnk), "LP_EXE": exe, "LP_ARGS": args, "LP_WD": wd, "LP_ICO": icon}
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env, check=True,
                   capture_output=True, timeout=30)
    return lnk


def make_shortcut(target_dir: Path | None = None, config: Path | None = None) -> Path:
    """Desktop shortcut that starts the panel without a console window. Windows only for now."""
    if sys.platform != "win32":
        raise RuntimeError("shortcut creation is Windows-only; on macOS/Linux run `livepanel --window` from a launcher")
    return write_shortcut(Path(target_dir or _desktop()) / "Live Panel.lnk", config)


def show_existing() -> bool:
    """Bring the open panel window back to the front, un-minimizing it. False when no panel window is open.
    (A minimized window looks like the panel is gone: 10-15 it sat minimized for hours.)"""
    if sys.platform != "win32":
        return False
    h = _win().find(TITLE, timeout=0)
    if not h:
        return False
    import ctypes
    u = ctypes.windll.user32
    if u.IsIconic(h):
        u.ShowWindow(h, 9)                                  # SW_RESTORE: back to its last normal place
    flags = 0x0001 | 0x0002                                 # NOSIZE | NOMOVE
    u.SetWindowPos(h, ctypes.c_void_p(-1), 0, 0, 0, 0, flags)   # topmost then back: lands on top even when
    u.SetWindowPos(h, ctypes.c_void_p(-2), 0, 0, 0, 0, flags)   # Windows refuses the foreground switch
    u.SetForegroundWindow(h)
    return True


def close_windows() -> int:
    """Close every open panel window (used by Quit in the tray)."""
    if sys.platform != "win32":
        return 0
    import ctypes
    n = 0
    while n < 10:
        h = _win().find(TITLE, timeout=0)
        if not h:
            break
        ctypes.windll.user32.PostMessageW(h, 0x0010, 0, 0)      # WM_CLOSE
        n += 1
        import time
        time.sleep(0.3)
    return n


def startup_shortcut() -> Path:
    """The "Start with Windows" shortcut: Live Panel.lnk in the user's Startup folder."""
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "[Environment]::GetFolderPath('Startup')"],
                       capture_output=True, text=True, timeout=20)
    d = Path(r.stdout.strip())
    if r.returncode != 0 or not r.stdout.strip() or not d.is_dir():
        raise RuntimeError("can't find the Startup folder")
    return d / "Live Panel.lnk"


def shortcut_config(path):
    """--config for a shortcut: only when the config isn't the default ~/.livepanel/config.toml."""
    if not path:
        return None
    from .config import HOME
    return None if Path(path).resolve() == (HOME / "config.toml").resolve() else path
