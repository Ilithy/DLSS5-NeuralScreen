"""Display module for the DLSS 5 Desktop NR prototype.

Borderless fullscreen window with a branded HUD overlay (dark #0D1117,
amber #FFBF00 accent, Consolas). The window is excluded from screen
capture via SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) so that
screen-capture tools (dxcam, OBS) do not see it.

Usage:
    disp = Display(2560, 1440)
    disp.set_hud({"fps": 60, "status": "NR ON", "resolution": "2560x1440",
                  "params": {"intensity": 0.5, "local_tone": 0.3,
                             "local_structure": 0.7}, "frames": 1234})
    while True:
        disp.show(frame_bgra)
        for ev in disp.poll_events():
            if ev == "quit":
                break
    disp.close()
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from typing import Dict, List

# --- argtypes for user32: WITHOUT them ctypes passes ints as a 32-bit c_int.
# HWND_TOPMOST=-1 turns into 0xFFFFFFFF instead of 0xFFFFFFFFFFFFFFFF and
# SetWindowPos silently FAILS (ret=0) on 64-bit Windows - topmost is not
# applied. hwnd (64-bit) would also be truncated at values >= 2^31.
user32 = ctypes.windll.user32
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
user32.SetWindowLongW.restype = wintypes.LONG
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF,
                                              wintypes.BYTE, wintypes.DWORD]
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]


CURSOR_SHOWING = 0x00000001


def system_cursor_visible() -> bool:
    """Is there a mouse pointer on screen right now? (Nothing uses this yet.)

    Kept for the open problem it belongs to: a fullscreen game hides the
    cursor and our menu is then unusable. Drawing our own pointer was tried
    and reverted - it produced a SECOND pointer in real use, which is worse
    than none. See "The menu pointer is missing or frozen" in the README.

    A fullscreen game hides it (ShowCursor(FALSE) on its own input queue) and
    it stays hidden while our menu is up. When it is gone the overlay draws
    its own; when it is there, drawing one would mean two pointers.

    Unknown counts as visible: a missing answer must not put a second pointer
    on a normal desktop.
    """
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    try:
        if not user32.GetCursorInfo(ctypes.byref(ci)):
            return True
    except Exception:
        return True
    return bool(ci.flags & CURSOR_SHOWING)

# DPI-aware BEFORE import pygame: on load SDL freezes the process awareness,
# and a later SetProcessDpiAwarenessContext no longer takes effect (it returns
# an error). Without this Windows scales the window: 125% -> 3072x1728 instead
# of 3840x2160 and the frame no longer matches the screen. Verified by
# diagnostics.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import numpy as np
import pygame

from overlay_ui import OverlayMenu, palette as ui_palette

from i18n import STRINGS

# --- Brand palette (DLSS5-Video-Converter) -------------------------------
BG_COLOR = (0x0D, 0x11, 0x17)      # #0D1117 dark background
BG_ALPHA = 235                     # HUD panel translucency (nearly opaque, so text stays readable)
WDA_EXCLUDEFROMCAPTURE = 0x00000011

# Chroma key for HUD mode: pixels of exactly this colour are not drawn at all
# by the layered window, and the worker's overlay shows through them. Picked so
# it cannot occur in the HUD palette (#0D1117 / #FFBF00 / #E6EDF3 / #8B949E).
CHROMA_KEY = (0xFF, 0x00, 0xFF)
LWA_COLORKEY = 0x1
LWA_ALPHA = 0x2

FONT_NAME = "consolas"
# The base layout sizes are set for 1440p. On taller screens the interface is
# scaled up, on shorter ones it stays as is: there is nowhere left to shrink to,
# the text would become unreadable. Hence the "up only" rule (see ui_scale).
FONT_SIZE = 18
UI_BASE_HEIGHT = 1800
ALERT_FONT_SIZE = 28


def ui_scale_for(height: int) -> float:
    """Interface multiplier derived from the screen height.

    Up only: 1.0 at 1800p and below, 1.2 at 4K. Scaling proportionally down at
    1080p would give a nine-pixel font, so we never scale down. The base was
    raised from 1440 to 1800 - at 1.5 the interface came out too large.
    """
    return max(1.0, float(height) / UI_BASE_HEIGHT)


class Display:
    """Fullscreen borderless window that renders frames + branded HUD."""

    def __init__(self, width: int, height: int, fullscreen: bool = True, click_through: bool = True):
        # DPI awareness is already set at module level (before import pygame).
        # Calling it again here has no effect - it is kept as a fallback for
        # cases where the module is imported without the top block.
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        # SDL hints BEFORE pygame.init():
        # SDL_WINDOWS_DPI_AWARENESS=permonitorv2 - without it Windows scales
        # the window (125% -> 3072x1728 instead of 3840x2160).
        # SDL_MOUSE_FOCUS_CLICKTHROUGH=1 - a click on the window is not
        # intercepted by SDL (the window is not activated, focus is not taken
        # away), so clicks reach the applications underneath the overlay.
        try:
            os.environ["SDL_WINDOWS_DPI_AWARENESS"] = "permonitorv2"
        except Exception:
            pass
        try:
            os.environ["SDL_MOUSE_FOCUS_CLICKTHROUGH"] = "1"
        except Exception:
            pass
        pygame.init()
        pygame.display.set_caption("NeuralScreen")
        # Borderless windowed instead of FULLSCREEN: a pygame fullscreen window
        # loses its rendering on click/focus (the screen freezes while the loop
        # keeps spinning). A window the size of the monitor at position (0,0)
        # looks the same but is stable, and click-through works.
        flags = pygame.NOFRAME
        self.screen = pygame.display.set_mode((width, height), flags)
        self.width, self.height = self.screen.get_size()
        self._move_to_origin()
        # Force the physical window size: even if DPI awareness did not apply
        # (a 3072x1728 window instead of 3840x2160), we stretch the window to
        # the requested size so the pygame surface matches.
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, width, height, 0x0004)  # SWP_NOZORDER
        except Exception:
            pass
        self._set_topmost()
        self.clock = pygame.time.Clock()
        self._hud: Dict = {}
        self._alerts: List[tuple[str, float]] = []  # (text, expires_at)
        # Interface scale and the layout sizes derived from it.
        self.ui_scale = ui_scale_for(self.height)
        self.font_size = max(8, int(round(FONT_SIZE * self.ui_scale)))
        # The settings menu lives in this same layer. A separate window on top
        # of the game would steal focus and fight for topmost, whereas here we
        # are already above the frame and already transparent by key.
        self.menu = OverlayMenu(self.ui_scale, self._load_font)
        self._font = self._load_font(size=self.font_size)
        self._alert_font = self._load_font(
            size=max(10, int(round(ALERT_FONT_SIZE * self.ui_scale))))
        # Disable vsync: flip() must not wait for vblank (otherwise the FPS is
        # tied to the monitor refresh rate and frames are lost on a slow
        # pipeline).
        try:
            pygame.display.set_swap_interval(0)
        except Exception:
            pass
        self._excluded = self._exclude_from_capture()
        self._click_through = False
        if click_through:
            self._set_click_through()
        self._lang = "ru"  # HUD language (NR ON/NR OFF), see set_lang()
        self._visible = True
        # HUD mode: the worker draws the frame, the window shows only the HUD
        self._hud_only = False
        self._last_overlay = 0.0
        self._last_alert_count = 0

    def _sync_cursor(self) -> None:
        """Cursor over the menu area: move and resize arrows.

        Set only on change - calling set_cursor every frame makes the cursor
        flicker noticeably.
        """
        want = (self.menu.desired_cursor if self.menu.visible
                else pygame.SYSTEM_CURSOR_ARROW)
        if want == getattr(self, "_cursor", None):
            return
        self._cursor = want
        try:
            pygame.mouse.set_cursor(want)
        except Exception:
            pass

    @property
    def theme(self) -> dict:
        """The menu theme palette - alerts are kept in the same look."""
        return ui_palette(self.menu.state.get("theme", "light"))

    def get_hwnd(self) -> int:
        """HWND of the overlay window - the parent for native dialogs."""
        try:
            return int(pygame.display.get_wm_info()["window"])
        except Exception:
            return 0

    @staticmethod
    def _rgb(color: str) -> tuple[int, int, int]:
        c = color.lstrip("#")
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)

    def set_lang(self, lang: str) -> None:
        """Switch the HUD status language (en/ru)."""
        if lang in STRINGS and lang != self._lang:
            self._lang = lang

    def set_visible(self, visible: bool) -> None:
        """Show/hide the window (SW_SHOW/SW_HIDE).

        With NR OFF the window is hidden completely so the desktop does not
        slow down (no capture/blit/flip). With NR ON it is shown again.
        """
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            user32.ShowWindow(hwnd, 5 if visible else 0)  # SW_SHOW=5, SW_HIDE=0
            self._visible = visible
        except Exception:
            pass

    def is_visible(self) -> bool:
        return getattr(self, "_visible", True)

    def _set_topmost(self) -> None:
        """HWND_TOPMOST - the window always sits above the rest (overlay)."""
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0,
                                              0x0001 | 0x0002)  # SWP_NOSIZE|NOMOVE
        except Exception:
            pass

    def move_to(self, x: int, y: int) -> None:
        """Put the overlay's top-left corner at (x, y) on the desktop.

        The one-window mode needs it: the overlay is the size of the window
        being processed and has to sit exactly on it. Topmost is reasserted on
        the way (the insert-after argument), so a game raising itself does not
        end up above the HUD.
        """
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            # SWP_NOSIZE | SWP_NOACTIVATE - move only, never take the focus.
            user32.SetWindowPos(hwnd, -1, int(x), int(y), 0, 0, 0x0001 | 0x0010)
        except Exception as exc:
            print(f"Display: WARNING could not move the overlay: {exc}")

    def _move_to_origin(self) -> None:
        """Move the window to (0,0) - the monitor's top left corner."""
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                              0x0001 | 0x0002 | 0x0040)  # SWP_NOSIZE|NOMOVE|SHOWWINDOW
        except Exception:
            pass

    # -- window plumbing --------------------------------------------------

    def _load_font(self, size: int = FONT_SIZE):
        try:
            return pygame.font.SysFont(FONT_NAME, size)
        except Exception:
            return pygame.font.Font(None, size)

    def _set_click_through(self) -> bool:
        """Click-through: WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_LAYERED.

        Verified by diagnostics (_work/diag_click.py, Win11 4K): WS_EX_TRANSPARENT
        WITHOUT WS_EX_LAYERED does NOT work - WindowFromPoint still returns the
        overlay and the click goes to the overlay. The working combination (per
        MSDN "Layered Windows"): a layered window + WS_EX_TRANSPARENT -> hit
        testing ignores the window shape and clicks go to the window under the
        cursor.

        The order is mandatory:
          1. SetWindowLongW(GWL_EXSTYLE, ... | WS_EX_LAYERED | WS_EX_TRANSPARENT
             | WS_EX_NOACTIVATE)
          2. SetLayeredWindowAttributes(alpha=255, LWA_ALPHA) - activates
             layered mode (without the call the window stays ordinary: the
             LAYERED flag is in exstyle but hit testing does not change)
          3. SetWindowPos(SWP_FRAMECHANGED) - drops the window style cache
             (required by the SetWindowLongW documentation)
        WS_EX_NOACTIVATE: the window does not take focus, the keyboard stays
        with the active application. Control is via global hotkeys
        (GetAsyncKeyState in main.py).
        """
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_LAYERED = 0x00080000
            LWA_ALPHA = 0x2
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOZORDER = 0x0004
            SWP_FRAMECHANGED = 0x0020
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                  style | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_LAYERED)
            # Activate layered mode: alpha=255 (an opaque window, only hit
            # testing changes, visually we touch nothing)
            user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
            # Drop the style cache - without SWP_FRAMECHANGED the
            # SetWindowLongW changes may not take effect
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
            self._click_through = True
            return True
        except Exception as exc:
            print(f"Display: WARNING click-through failed: {exc}")
            return False

    def _exclude_from_capture(self) -> bool:
        """SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) on the window.

        Makes the window invisible to screen capture (dxcam, OBS, etc.).
        Best-effort: warn on failure, never crash.
        """
        return self.set_excluded_from_capture(True)

    def set_excluded_from_capture(self, hide: bool) -> bool:
        """Hide the overlay from screen capture, or stop hiding it.

        Hiding is mandatory while the input is Desktop Duplication of the whole
        screen: without it the pipeline would capture its own output. With one
        window as the input there is no such loop, and then hiding is pure
        loss - it is what stops OBS from seeing the overlay and stops the
        NVIDIA App from recording at all.
        """
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            user32 = ctypes.windll.user32
            want = WDA_EXCLUDEFROMCAPTURE if hide else 0  # 0 = WDA_NONE
            ok = user32.SetWindowDisplayAffinity(hwnd, want)
            if not ok:
                print(f"Display: WARNING SetWindowDisplayAffinity({want}) failed "
                      f"(the overlay may be visible to screen capture)")
                return False
            self._excluded = bool(hide)
            return True
        except Exception as exc:
            print(f"Display: WARNING cannot change the capture affinity: {exc}")
            return False

    # -- public API -------------------------------------------------------

    def set_menu_input(self, enabled: bool) -> None:
        """Whether input should reach our layer (while the menu is open).

        Normally the window is click-through: WS_EX_TRANSPARENT gives clicks to
        whatever is underneath. While the menu is up the flag is removed - the
        clicks are ours and the game under the overlay does not get them.
        Exactly the ReShade behaviour. WS_EX_NOACTIVATE is removed too,
        otherwise there is no keyboard.
        """
        try:
            hwnd = pygame.display.get_wm_info()["window"]
        except Exception as exc:
            print(f"Display: WARNING no hwnd for menu input: {exc}")
            return
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_NOACTIVATE = 0x08000000
        SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x2, 0x1, 0x4, 0x20
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if enabled:
            style &= ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        else:
            style |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
        if enabled:
            # Focus is needed for the keyboard. The mouse works without it -
            # the click goes to the window under the cursor now that it is no
            # longer transparent. SetForegroundWindow alone is refused when
            # the foreground window belongs to another process that has not
            # received input from the user (a game in the foreground): the
            # system blocks the steal. AttachThreadInput is the standard
            # workaround - it makes the foreground thread share its input
            # state with ours, so the activation is treated as user-initiated.
            try:
                fg = user32.GetForegroundWindow()
                fg_tid = user32.GetWindowThreadProcessId(fg, None)
                my_tid = ctypes.windll.kernel32.GetCurrentThreadId()
                if fg_tid and fg_tid != my_tid:
                    user32.AttachThreadInput(my_tid, fg_tid, True)
                user32.SetForegroundWindow(hwnd)
                user32.SetActiveWindow(hwnd)
                user32.SetFocus(hwnd)
                if fg_tid and fg_tid != my_tid:
                    user32.AttachThreadInput(my_tid, fg_tid, False)
            except Exception:
                pass
        self._click_through = not enabled

    def set_menu_opaque(self, opaque: bool) -> None:
        """Drop the global window translucency while the menu is open.

        The layer lives at BG_ALPHA so the HUD does not plaster over the
        picture. But the menu panel is nearly black and a bright frame shows
        through it - it reads as "too transparent". While the menu is up we set
        255.
        """
        if not self._hud_only:
            return
        try:
            hwnd = pygame.display.get_wm_info()["window"]
        except Exception:
            return
        r, g, b = CHROMA_KEY
        key = (b << 16) | (g << 8) | r
        alpha = 255 if opaque else BG_ALPHA
        user32.SetLayeredWindowAttributes(hwnd, key, alpha, LWA_COLORKEY | LWA_ALPHA)

    def set_hud_only(self, enabled: bool, force: bool = False) -> None:
        """HUD mode: the worker draws the frame in its own window, only the HUD
        stays here.

        The background is filled with CHROMA_KEY and made transparent through
        SetLayeredWindowAttributes(LWA_COLORKEY) - the worker's overlay shows
        through it, while the HUD, alerts and watermark are drawn on top as
        before. Turning it off restores the ordinary opaque mode (LWA_ALPHA).
        force=True: reapply the attributes even when the mode did not change -
        z-order operations (SetWindowPos/TopMost after the settings menu) can
        drop LWA_COLORKEY, and an early return would leave the window opaque.
        """
        if enabled == self._hud_only and not force:
            return
        self._hud_only = enabled
        try:
            hwnd = pygame.display.get_wm_info()["window"]
        except Exception as exc:
            print(f"Display: WARNING cannot get hwnd for HUD mode: {exc}")
            return
        if enabled:
            # COLORREF is 0x00BBGGRR, not RGB
            r, g, b = CHROMA_KEY
            key = (b << 16) | (g << 8) | r
            # The panel translucency comes from the window's GLOBAL alpha
            # (LWA_ALPHA), NOT from the alpha of the panel pixels: a
            # semi-transparent SRCALPHA blend with the magenta background would
            # give a colour != key and a pink slab (the colour key does not cut
            # out a blended colour). The colour key removes the background
            # entirely, and the opaque panel (plus text) becomes slightly
            # see-through through the global alpha.
            ok = user32.SetLayeredWindowAttributes(hwnd, key, BG_ALPHA, LWA_COLORKEY | LWA_ALPHA)
        else:
            ok = user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
        if not ok:
            print(f"Display: WARNING SetLayeredWindowAttributes failed "
                  f"(HUD mode {'on' if enabled else 'off'})")
        self._last_overlay = 0.0  # the next draw_overlay redraws immediately

    def raise_topmost(self) -> None:
        """Raise the window above the worker's window.

        Both windows are topmost, and inside that group the one raised last
        ends up on top. The worker creates its window after ours, so after
        every raise of its overlay the HUD has to be brought back up, otherwise
        it ends up under the frame and becomes invisible.
        """
        self._set_topmost()

    def refresh_colorkey(self) -> None:
        """Reapply LWA_COLORKEY on the pygame window (the HUD layer).

        Needed after operations that can drop the window's layered attributes
        (z-order shuffling with the tkinter settings menu: the window stays
        opaque and the HUD is not visible). Recreates nothing - only
        SetLayeredWindowAttributes, unlike set_hud_only(force=True).
        """
        if not self._hud_only:
            return
        try:
            hwnd = pygame.display.get_wm_info()["window"]
        except Exception:
            return
        r, g, b = CHROMA_KEY
        key = (b << 16) | (g << 8) | r
        user32.SetLayeredWindowAttributes(hwnd, key, BG_ALPHA, LWA_COLORKEY | LWA_ALPHA)

    def draw_capture_overlay(self, surface: pygame.Surface) -> None:
        """Bake the open menu into a recorded or screenshot frame.

        The frame is grabbed WITHOUT our pygame layer: the window is marked
        WDA_EXCLUDEFROMCAPTURE, otherwise DDA would capture our own output.
        So everything that must end up in the file is drawn here, on top of the
        pixels already received.

        We bake the menu only. The HUD and the watermark used to be baked too,
        but they are not on screen - in the file they looked like someone
        else's caption.
        """
        if not self.menu.visible:
            return
        # The "grabbed an edge" highlight is cursor state and has no place in
        # the file: it would freeze on the frame for no visible reason.
        hover, self.menu.hover = self.menu.hover, None
        try:
            self.menu.set_stats(self._hud)
            self.menu.draw(surface)
        finally:
            self.menu.hover = hover

    def draw_overlay(self, min_interval: float = 0.1) -> None:
        """Redraw the HUD over the frame the worker is showing.

        Throttling: a 4K fill plus flip costs ~5 ms while the HUD changes a
        couple of times per second - doing it every frame is wasted time.
        Alerts appear and disappear out of band, so for them the redraw is
        immediate.
        """
        try:
            pygame.event.pump()
        except Exception:
            pass
        now = time.monotonic()
        alerts = len(self._alerts)
        # With the menu open throttling is disabled: 10 Hz is enough for a
        # static HUD, but a slider under the mouse jitters at that rate.
        if self.menu.visible:
            min_interval = 0.0
        if now - self._last_overlay < min_interval and alerts == self._last_alert_count:
            return
        self._last_overlay = now
        self._last_alert_count = alerts
        self.screen.fill(CHROMA_KEY)
        self._draw_alerts()
        self.menu.set_stats(self._hud)
        self.menu.draw(self.screen)
        self._sync_cursor()
        pygame.display.flip()

    def set_hud(self, data: dict) -> None:
        """Update HUD data: fps, status, resolution, params, frames."""
        self._hud = dict(data)

    def alert(self, text: str, duration: float = 2.5) -> None:
        """Show a pop-up alert centred on the screen (amber border)."""
        self._alerts.append((text, time.monotonic() + duration))

    def show(self, frame_rgba: np.ndarray) -> None:
        """Blit frame (RGBA uint8) fullscreen and draw the HUD on top.

        The frame: pygame.image.frombuffer(frame_rgba, ..., "RGBX") is
        zero-copy (the surface references the numpy buffer, without tobytes and
        a 33 MB copy at 4K). The RGBX format (32-bit, no alpha channel) blits
        to the screen faster than an RGBA surface with SRCALPHA (no alpha
        blending needed). The frame's alpha is unused - the frame is opaque
        (A=255).

        IMPORTANT: frombuffer does NOT copy the data - the surface lives as
        long as the numpy buffer does. The frame arrives from WorkerReader as a
        separate array per frame, so the surface is recreated each time (2 ms
        at 4K). event.pump() drains the Windows message queue (WM_PAINT and
        friends) - without it the window freezes on click/focus.
        """
        if frame_rgba is None:
            return
        try:
            pygame.event.pump()
        except Exception:
            pass
        try:
            surface = pygame.image.frombuffer(
                frame_rgba, (frame_rgba.shape[1], frame_rgba.shape[0]), "RGBX")
        except Exception:
            # Fallback: not a numpy array / incompatible shape - via tobytes
            surface = pygame.image.frombuffer(
                frame_rgba.tobytes(), (frame_rgba.shape[1], frame_rgba.shape[0]),
                "RGBA")
        # One blit+flip for both paths (the main RGBX and the RGBA fallback)
        self.screen.blit(surface, (0, 0))
        self._draw_alerts()
        self.menu.set_stats(self._hud)
        self.menu.draw(self.screen)
        self._sync_cursor()
        pygame.display.flip()

    def poll_events(self) -> List[str]:
        """Return event names: 'quit' (Esc / window close), 'toggle' (Num1/F10).

        This is the local path, for when our own window has the focus - the
        global hotkeys are RegisterHotKey in hotkeys.py. Num1 matches the
        default binding; F10 stays because it was the default before and is
        still what a habit reaches for.
        """
        events = []
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                events.append("quit")
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    events.append("quit")
                elif event.key in (pygame.K_KP1, pygame.K_F10):
                    events.append("toggle")
        return events

    def close(self) -> None:
        pygame.quit()

    # -- HUD --------------------------------------------------------------

    def _draw_alerts(self) -> None:
        """Pop-up alert: the menu palette, top centre.

        It used to hang a third of the way down, centred, as a dark slab with
        an amber border - it clashed with the overall look and got into the
        middle of the frame.
        """
        now = time.monotonic()
        self._alerts = [(text, expires) for text, expires in self._alerts if expires > now]
        if not self._alerts:
            return
        c = self.theme
        text, _ = self._alerts[-1]
        surf = self._alert_font.render(text, True, self._rgb(c["text"]))
        pad_x = int(round(26 * self.ui_scale))
        pad_y = int(round(14 * self.ui_scale))
        w = surf.get_width() + pad_x * 2
        h = surf.get_height() + pad_y * 2
        rect = pygame.Rect((self.width - w) // 2, int(round(self.height * 0.045)), w, h)
        radius = int(round(10 * self.ui_scale))
        # The panel is opaque: translucency would blend with the chroma key and
        # give a dirty tint (the colour key does not cut out a blended colour).
        pygame.draw.rect(self.screen, self._rgb(c["bg"]), rect, border_radius=radius)
        pygame.draw.rect(self.screen, self._rgb(c["border"]), rect,
                         max(1, int(round(self.ui_scale))), border_radius=radius)
        self.screen.blit(surf, (rect.x + pad_x, rect.y + pad_y))

def main() -> int:
    """Standalone smoke test: gradient frames + HUD until Esc/Num1."""
    disp = Display(2560, 1440)
    disp.set_hud({
        "fps": 60.0,
        "status": "NR ON",
        "resolution": "2560x1440",
        "params": {"intensity": 0.5, "local_tone": 0.3,
                   "local_structure": 0.7},
        "frames": 0,
    })
    h, w = disp.height, disp.width
    yy, xx = np.mgrid[0:h, 0:w]
    frame = np.zeros((h, w, 4), dtype=np.uint8)
    frame[..., 0] = (xx * 255 // max(w - 1, 1)).astype(np.uint8)   # B
    frame[..., 1] = (yy * 255 // max(h - 1, 1)).astype(np.uint8)   # G
    frame[..., 2] = 40                                              # R
    frame[..., 3] = 255                                             # A
    n = 0
    running = True
    while running:
        disp.show(frame)
        for ev in disp.poll_events():
            if ev == "quit":
                running = False
        n += 1
        disp.set_hud({"fps": disp.clock.get_fps(), "status": "NR ON",
                      "resolution": f"{w}x{h}",
                      "params": {"intensity": 0.5, "local_tone": 0.3,
                                 "local_structure": 0.7},
                      "frames": n})
        disp.clock.tick(60)
    disp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
