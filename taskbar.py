"""TaskbarWindow - a taskbar button for the program.

The overlay is a borderless click-through window and the worker window is
a tool window, so neither shows in the taskbar - the program lived only
in the tray. A taskbar button is what users expect from a desktop app
(user rule 2026-09-09: "всегда отображалась в панели задач а не только
в трее").

The button is a real top-level window with WS_EX_APPWINDOW: a 1x1 visible
window parked at the corner of the screen. Clicking its taskbar button
activates it; the window procedure turns that into the same "settings"
command the tray's left click sends, so both entry points open the same
overlay menu. The window itself never shows anything.

The icon comes from native/neuralscreen.ico (the same one the launcher
uses), so the taskbar button looks like the program.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import queue
import threading
import time
from pathlib import Path

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t

WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_EX_APPWINDOW = 0x00040000
WM_ACTIVATE = 0x0006
WA_CLICKACTIVE = 0x2
WA_ACTIVE = 0x1
WM_QUIT = 0x0012
WM_SETICON = 0x0080
ICON_SMALL = 0
ICON_BIG = 1
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    """WNDCLASSW - not in ctypes.wintypes, defined here."""
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class TaskbarWindow:
    """The taskbar button: a 1x1 APPWINDOW window that reports clicks.

    Commands go into the same queue.Queue the tray uses - the main loop
    already drains it, so a click here opens the overlay menu exactly
    like a left click on the tray icon.
    """

    def __init__(self, commands: queue.Queue, title: str = "NeuralScreen"):
        self._commands = commands
        self._title = title
        self._hwnd = None
        self._thread: threading.Thread | None = None
        self._proc = WNDPROC(self._wnd_proc)
        self._last_cmd = 0.0

    def _wnd_proc(self, hwnd, msg, wparam, lparam) -> int:
        if msg == WM_ACTIVATE:
            # The user clicked the taskbar button (or Alt+Tab'd to us):
            # open the overlay menu, the same command the tray's left
            # click sends. WA_CLICKACTIVE is the click, WA_ACTIVE covers
            # Alt+Tab - both are the user asking for the program.
            # A single click can deliver both (the click activates, then
            # the system re-activates with WA_ACTIVE) - dedupe, or the
            # menu would open and immediately close.
            if wparam in (WA_CLICKACTIVE, WA_ACTIVE):
                now = time.monotonic()
                if now - self._last_cmd > 0.5:
                    self._last_cmd = now
                    try:
                        self._commands.put("settings")
                    except Exception:
                        pass
            return 0
        if msg == WM_QUIT:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def start(self) -> None:
        """Create the window in its own thread (the message loop blocks)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="taskbar")
        self._thread.start()

    def _run(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        cls = "NeuralScreenTaskbar"
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = cls
        wc.hCursor = user32.LoadCursorW(None, 32512)  # IDC_ARROW
        if not user32.RegisterClassW(ctypes.byref(wc)):
            # Already registered (a second instance in the same process).
            pass
        # A 1x1 window at the corner: visible to the system (so the
        # taskbar button exists) but nothing the eye can catch.
        self._hwnd = user32.CreateWindowExW(
            WS_EX_APPWINDOW, cls, self._title, WS_POPUP | WS_VISIBLE,
            0, 0, 1, 1, None, None, hinst, None)
        if not self._hwnd:
            return
        self._set_icon(hinst)
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.DestroyWindow(self._hwnd)
        self._hwnd = None

    def _set_icon(self, hinst) -> None:
        """The launcher's icon, so the taskbar button looks like the app."""
        ico = Path(__file__).resolve().parent / "native" / "neuralscreen.ico"
        if not ico.is_file():
            return
        hicon = user32.LoadImageW(hinst, str(ico), IMAGE_ICON, 32, 32,
                                  LR_LOADFROMFILE)
        if hicon:
            user32.SendMessageW(self._hwnd, WM_SETICON, ICON_SMALL, hicon)
            user32.SendMessageW(self._hwnd, WM_SETICON, ICON_BIG, hicon)

    def stop(self) -> None:
        """Close the window and join the thread."""
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    @property
    def hwnd(self):
        return self._hwnd
