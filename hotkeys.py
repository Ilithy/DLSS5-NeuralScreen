"""HotkeyController - global hotkeys through RegisterHotKey + a polling fallback.

The difference from polling GetAsyncKeyState is fundamental: the system
delivers WM_HOTKEY only to us and does NOT pass the keypress to the active
application. F10 inside a game toggles NR and the game never sees the key.
Polling cannot do that — it only peeks at the key state while the press still
reaches the game.

The flip side of the same property: while NeuralScreen runs, F10 and F11
belong to it and other programs (debuggers, games) will not get them.

The arrow and quit combinations are on Ctrl+Alt deliberately: bare arrows
must not be registered — they would stop working system-wide — and quitting
on a single key is far too easy to hit by accident.

RegisterHotKey(NULL, ...) posts WM_HOTKEY to the message queue of the CALLING
thread, so no window is needed — only a message loop in our own thread.
Commands go into the same queue the tray uses: the command vocabulary is
shared ("quit", "settings", "toggle", "scale_up", "scale_down").
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
from ctypes import wintypes

user32 = ctypes.windll.user32

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000  # holding the key does not spam repeats

VK_F7 = 0x76
VK_F8 = 0x77
VK_F9 = 0x78
VK_F10 = 0x79
VK_F11 = 0x7A
VK_UP = 0x26
VK_DOWN = 0x28
VK_Q = 0x51
VK_INSERT = 0x2D
VK_HOME = 0x24
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_SHIFT = 0x10

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000
# Our own messages to the hotkey thread. RegisterHotKey/UnregisterHotKey with
# hWnd=None are bound to the CALLING thread, so they can only be removed and
# reinstalled from inside that same thread — never straight from main.
MSG_SUSPEND = 0x8000 + 1
MSG_RESUME = 0x8000 + 2
MSG_REBIND = 0x8000 + 3

# id -> (modifiers, VK, command, human-readable name)
# The defaults avoid keys games use: F9 is quickload in Bethesda titles,
# F8/F7 are screenshots in some engines, PrtScr belongs to Snip & Sketch.
# F10/F11/Home are nearly dead in games. The polling fallback (see below)
# does not swallow the key - it still reaches the game - so a collision
# would fire both sides.
DEFAULT_BINDINGS = {
    1: (MOD_NOREPEAT, VK_F10, "toggle", "F10"),
    2: (MOD_NOREPEAT, VK_F11, "settings", "F11"),
    3: (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_UP, "scale_up", "Ctrl+Alt+Up"),
    4: (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_DOWN, "scale_down", "Ctrl+Alt+Down"),
    5: (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_Q, "quit", "Ctrl+Alt+Q"),
    6: (MOD_NOREPEAT, VK_INSERT, "record", "Insert"),
    # The screenshot lived only as a menu button, with no key to print on it.
    # Home sits far from the WASD cluster and is free in games. Not PrtScr:
    # Snip & Sketch takes it, and RegisterHotKey may well refuse it.
    7: (MOD_NOREPEAT, VK_HOME, "screenshot_menu", "Home"),
}

# Key name -> VK (for parsing the config)
_KEY_NAMES = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74,
    "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79,
    "F11": 0x7A, "F12": 0x7B,
    "INSERT": VK_INSERT, "DELETE": 0x2E, "HOME": 0x24, "END": 0x23,
    "PGUP": 0x21, "PGDN": 0x22,
    "UP": VK_UP, "DOWN": VK_DOWN, "LEFT": 0x25, "RIGHT": 0x27,
    "Q": VK_Q, "W": 0x57, "E": 0x45, "R": 0x52, "T": 0x54, "Y": 0x59,
    "U": 0x55, "I": 0x49, "O": 0x4F, "P": 0x50, "A": 0x41, "S": 0x53,
    "D": 0x44, "F": 0x46, "G": 0x47, "H": 0x48, "J": 0x4A, "K": 0x4B,
    "L": 0x4C, "Z": 0x5A, "X": 0x58, "C": 0x43, "V": 0x56, "B": 0x42,
    "N": 0x4E, "M": 0x4D,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
}


def parse_binding(text: str) -> tuple[int, int] | None:
    """Parse a string like 'F10', 'Ctrl+Alt+Q', 'Insert' -> (mods, vk).

    Returns None when the string is not recognised, in which case the binding
    is left alone.
    """
    if not text:
        return None
    parts = [p.strip().upper() for p in text.split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    for p in parts[:-1]:
        if p == "CTRL" or p == "CONTROL":
            mods |= MOD_CONTROL
        elif p == "ALT":
            mods |= MOD_ALT
        elif p == "SHIFT":
            mods |= MOD_SHIFT
        else:
            return None
    vk = _KEY_NAMES.get(parts[-1])
    if vk is None:
        return None
    return mods | MOD_NOREPEAT, vk


def build_bindings(overrides: dict | None = None) -> dict:
    """Bindings with the user's overrides from the config applied.

    overrides: {"toggle": "F10", "record": "Insert", ...} — command -> string.
    Unknown or malformed strings are ignored and the default stays.
    """
    bindings = {hk_id: tuple(entry) for hk_id, entry in DEFAULT_BINDINGS.items()}
    if not overrides:
        return bindings
    for hk_id, (mods, vk, cmd, name) in list(bindings.items()):
        text = overrides.get(cmd)
        if not text:
            continue
        parsed = parse_binding(text)
        if parsed is None:
            continue
        new_mods, new_vk = parsed
        bindings[hk_id] = (new_mods, new_vk, cmd, text)
    return bindings


def describe(bindings: dict | None = None) -> str:
    """A line like 'F10=toggle, F11=settings, ...' for the startup log."""
    src = bindings or DEFAULT_BINDINGS
    return ", ".join(f"{name}={cmd}" for _, (_, _, cmd, name) in sorted(src.items()))


# --- polling fallback -----------------------------------------------------
# Some engines (id Tech 5, UE3) grab the keyboard exclusively even in a
# window: the system does not deliver WM_HOTKEY while the game holds the
# device, so RegisterHotKey alone is dead under them. The poller below reads
# the raw key state (GetAsyncKeyState) and fires the command when the press
# never arrived as WM_HOTKEY. It runs ALWAYS - when RegisterHotKey works it
# fires first and the poller's duplicate is suppressed by the cooldown; when
# the game swallows the hotkey the poller is the only path. The key still
# reaches the game (unlike RegisterHotKey), which is fine for F9/F8/Insert
# and the Ctrl+Alt combinations.

POLL_INTERVAL = 0.03
POLL_COOLDOWN = 0.25


def _pressed(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def _mods_down(mods: int) -> bool:
    if mods & MOD_CONTROL and not _pressed(VK_CONTROL):
        return False
    if mods & MOD_ALT and not _pressed(VK_MENU):
        return False
    if mods & MOD_SHIFT and not _pressed(VK_SHIFT):
        return False
    return True


def _mods_clear(mods: int) -> bool:
    """A bare key (no modifiers) must not fire while Ctrl/Alt/Shift is down."""
    if mods & (MOD_CONTROL | MOD_ALT | MOD_SHIFT):
        return True
    return not (_pressed(VK_CONTROL) or _pressed(VK_MENU) or _pressed(VK_SHIFT))


class HotkeyController:
    """Registers the global hotkeys; commands go into a queue."""

    def __init__(self, commands: queue.Queue, bindings: dict | None = None):
        self._commands = commands
        self._bindings = bindings or DEFAULT_BINDINGS
        self._thread: threading.Thread | None = None
        self._tid = 0
        self.registered: list[str] = []
        self.failed: list[str] = []
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._pending: dict | None = None   # bindings for MSG_REBIND
        self._active = False                # hotkeys are currently registered
        # Polling fallback state: vk -> last time the command fired. The
        # cooldown suppresses both repeats and the duplicate that arrives
        # when RegisterHotKey DID deliver the press.
        self._poll_last: dict[int, float] = {}
        self._poll_stop = threading.Event()

    def start(self, timeout: float = 3.0) -> None:
        """Start the thread and wait for the registration result."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="hotkeys")
        self._thread.start()
        self._ready.wait(timeout)
        # The polling fallback is a separate daemon: it must keep running
        # even while the message loop is blocked inside GetMessageW.
        self._poll_stop.clear()
        threading.Thread(target=self._poll_loop, daemon=True,
                         name="hotkeys-poll").start()

    def _run(self) -> None:
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        # A thread's message queue is created lazily — force it into
        # existence BEFORE RegisterHotKey, or the first WM_HOTKEY may vanish.
        user32.PeekMessageW(ctypes.byref(msg), None, WM_HOTKEY, WM_HOTKEY, PM_NOREMOVE)
        self._register()
        self._ready.set()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                binding = self._bindings.get(msg.wParam)
                if binding is not None:
                    self._commands.put(binding[2])
            elif msg.message == MSG_SUSPEND:
                self._unregister()
            elif msg.message == MSG_RESUME:
                self._register()
            elif msg.message == MSG_REBIND:
                self._unregister()
                with self._lock:
                    if self._pending is not None:
                        self._bindings = self._pending
                        self._pending = None
                self._register()
        self._unregister()

    # Registration lives only in the hotkey thread — see MSG_* above.
    def _register(self) -> None:
        if self._active:
            return
        self.registered = []
        self.failed = []
        for hk_id, (mods, vk, _cmd, name) in self._bindings.items():
            if user32.RegisterHotKey(None, hk_id, mods, vk):
                self.registered.append(name)
            else:
                # Someone else already holds the combination — not fatal,
                # the remaining hotkeys keep working.
                self.failed.append(name)
        self._active = True

    def _poll_loop(self) -> None:
        """Raw-key fallback: fires commands RegisterHotKey cannot deliver.

        Runs every POLL_INTERVAL. A binding fires when its key is down, the
        modifiers match, and the same key has not fired within POLL_COOLDOWN.
        The cooldown is what makes the fallback invisible when the normal
        path works: WM_HOTKEY arrives first, the poller sees the key still
        down and skips it.
        """
        while not self._poll_stop.wait(POLL_INTERVAL):
            with self._lock:
                bindings = dict(self._bindings)
            now = time.monotonic()
            for hk_id, (mods, vk, cmd, _name) in bindings.items():
                if not _pressed(vk):
                    continue
                if not _mods_down(mods) or not _mods_clear(mods):
                    continue
                last = self._poll_last.get(vk, 0.0)
                if now - last < POLL_COOLDOWN:
                    continue
                self._poll_last[vk] = now
                self._commands.put(cmd)

    def _unregister(self) -> None:
        if not self._active:
            return
        for hk_id in self._bindings:
            user32.UnregisterHotKey(None, hk_id)
        self._active = False

    def suspend(self) -> None:
        """Suspend the hotkeys: while the menu waits for a key, F8 must land
        in the field instead of toggling the menu."""
        if self._tid:
            user32.PostThreadMessageW(self._tid, MSG_SUSPEND, 0, 0)

    def resume(self) -> None:
        if self._tid:
            user32.PostThreadMessageW(self._tid, MSG_RESUME, 0, 0)

    def rebind(self, bindings: dict) -> None:
        """Replace the assignments on the fly, without restarting."""
        if not self._tid:
            return
        with self._lock:
            self._pending = {k: tuple(v) for k, v in bindings.items()}
        user32.PostThreadMessageW(self._tid, MSG_REBIND, 0, 0)

    def stop(self) -> None:
        self._poll_stop.set()
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._tid = 0
