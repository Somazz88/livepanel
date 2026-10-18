"""Windows tray icon: open the window, start with Windows, quit. Plain Win32 through ctypes, nothing to install.

The panel keeps running when its window is closed or minimized; this icon is how you find it again and how you
stop it.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import threading
from pathlib import Path

WM_APP_TRAY = 0x8000 + 1
WM_CLOSE, WM_DESTROY, WM_COMMAND, WM_NULL = 0x0010, 0x0002, 0x0111, 0x0000
WM_LBUTTONUP, WM_RBUTTONUP, WM_LBUTTONDBLCLK = 0x0202, 0x0205, 0x0203
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
MF_STRING, MF_SEPARATOR, MF_CHECKED = 0x0, 0x800, 0x8
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
ID_OPEN, ID_AUTO, ID_QUIT = 1, 2, 3

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


TEXT = {"en": {"tip": "Live Panel", "open": "Open window", "auto": "Start with Windows", "quit": "Quit Live Panel"},
        "zh": {"tip": "Live Panel 实时持仓", "open": "打开窗口", "auto": "开机自动启动", "quit": "结束 Live Panel"}}


def _api():
    u, s, k = ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32
    u.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    u.DefWindowProcW.restype = LRESULT
    u.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
    u.CreateWindowExW.restype = wt.HWND
    u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    u.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
    u.LoadImageW.restype = wt.HANDLE
    u.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID]
    u.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
    u.CreatePopupMenu.restype = wt.HMENU
    u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    u.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
    s.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    s.Shell_NotifyIconW.restype = wt.BOOL
    k.GetModuleHandleW.restype = wt.HMODULE
    return u, s, k


def icon_path() -> Path:
    return Path(__file__).resolve().parent / "web" / "icon.ico"


class Tray:
    """Runs its own message loop on a thread. on_open / on_quit are called from that thread."""

    def __init__(self, lang: str, on_open, on_quit, autostart: tuple | None = None):
        """autostart = (is_on(), set_on(bool)) for the "Start with Windows" item; None hides it."""
        self.t = TEXT.get(lang, TEXT["en"])
        self.on_open, self.on_quit, self.autostart = on_open, on_quit, autostart
        self.hwnd = None
        self.ready = threading.Event()
        self.added = False
        self._thread = threading.Thread(target=self._loop, name="tray", daemon=True)

    # ── lifecycle ──
    def start(self, timeout: float = 10.0) -> bool:
        self._thread.start()
        self.ready.wait(timeout)
        return self.added

    def stop(self) -> None:
        if self.hwnd:
            ctypes.windll.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        self._thread.join(5)

    def _nid(self) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd, nid.uID = self.hwnd, 1
        return nid

    def _loop(self) -> None:
        u, s, k = _api()
        hinst = k.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wndproc)                    # keep a reference: the callback must outlive the window
        wc = WNDCLASSW()
        wc.lpfnWndProc, wc.hInstance, wc.lpszClassName = self._proc, hinst, "LivePanelTray"
        u.RegisterClassW(ctypes.byref(wc))
        self.hwnd = u.CreateWindowExW(0, "LivePanelTray", "Live Panel tray", 0, 0, 0, 0, 0, None, None, hinst, None)
        self._taskbar_created = u.RegisterWindowMessageW("TaskbarCreated")
        self.hicon = u.LoadImageW(None, str(icon_path()), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
        self._add()
        self.ready.set()
        msg = wt.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))

    def _add(self) -> None:
        nid = self._nid()
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage, nid.hIcon, nid.szTip = WM_APP_TRAY, self.hicon, self.t["tip"]
        self.added = bool(ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))

    def _menu(self) -> None:
        u = ctypes.windll.user32
        m = u.CreatePopupMenu()
        u.AppendMenuW(m, MF_STRING, ID_OPEN, self.t["open"])
        if self.autostart:
            on = False
            try:
                on = bool(self.autostart[0]())
            except Exception:
                pass
            u.AppendMenuW(m, MF_STRING | (MF_CHECKED if on else 0), ID_AUTO, self.t["auto"])
        u.AppendMenuW(m, MF_SEPARATOR, 0, None)
        u.AppendMenuW(m, MF_STRING, ID_QUIT, self.t["quit"])
        pt = wt.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        u.SetForegroundWindow(self.hwnd)                      # without this the menu won't close on an outside click
        cmd = u.TrackPopupMenu(m, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY, pt.x, pt.y, 0, self.hwnd, None)
        u.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        u.DestroyMenu(m)
        if cmd == ID_OPEN:
            self._safe(self.on_open)
        elif cmd == ID_AUTO:
            self._safe(lambda: self.autostart[1](not self.autostart[0]()))
        elif cmd == ID_QUIT:
            self._safe(self.on_quit)

    @staticmethod
    def _safe(fn) -> None:
        try:
            fn()
        except Exception as e:                                 # a failed click must not kill the tray thread
            print(f"[tray] {e}", file=sys.stderr)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        u = ctypes.windll.user32
        if msg == WM_APP_TRAY:
            if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._safe(self.on_open)
            elif lparam == WM_RBUTTONUP:
                self._menu()
            return 0
        if msg == getattr(self, "_taskbar_created", None):    # Explorer restarted: put the icon back
            self._add()
            return 0
        if msg == WM_CLOSE:
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid()))
            u.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            u.PostQuitMessage(0)
            return 0
        return u.DefWindowProcW(hwnd, msg, wparam, lparam)
