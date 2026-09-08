"""Re-registering hotkeys on the fly: suspend/resume/rebind.

RegisterHotKey with hWnd=None is bound to the calling thread, so assignments
can only be removed and installed from inside the hotkey thread - through our
own messages. This test checks that it actually happens rather than merely
compiling.

It is checked by fact: after a rebind the new combination is in registered and
the old one is not; after suspend, registering the same combination FROM THE
OUTSIDE succeeds (so the hotkey really was released) and after resume it fails.
"""
import ctypes
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the project root
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)
from hotkeys import (DEFAULT_BINDINGS, HotkeyController,  # noqa: E402
                     MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, build_bindings)

user32 = ctypes.windll.user32
# A combination that is certainly free, so we do not fight the system.
PROBE_ID = 900
PROBE_MODS = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
VK_F6 = 0x75


def can_grab(mods: int, vk: int) -> bool:
    """Could the combination be registered from THIS thread?

    If yes, nobody is holding it. We release it straight away.
    """
    if user32.RegisterHotKey(None, PROBE_ID, mods, vk):
        user32.UnregisterHotKey(None, PROBE_ID)
        return True
    return False


def main() -> int:
    failures = []
    commands: queue.Queue = queue.Queue()

    # Our bindings: a single combination, so the probe is unambiguous.
    bindings = {1: (PROBE_MODS, VK_F6, "toggle", "Ctrl+Alt+F6")}
    if not can_grab(PROBE_MODS, VK_F6):
        print("SKIP: Ctrl+Alt+F6 is already taken - the probe is unreliable")
        return 0

    hk = HotkeyController(commands, bindings)
    hk.start()
    print(f"start: registered={hk.registered} failed={hk.failed}")
    if "Ctrl+Alt+F6" not in hk.registered:
        failures.append("it did not register at startup")
    if can_grab(PROBE_MODS, VK_F6):
        failures.append("the combination is free although the controller took it")

    hk.suspend()
    time.sleep(0.4)
    if not can_grab(PROBE_MODS, VK_F6):
        failures.append("the combination is still taken after suspend")
    else:
        print("suspend: the combination was released")

    hk.resume()
    time.sleep(0.4)
    if can_grab(PROBE_MODS, VK_F6):
        failures.append("the combination was not taken back after resume")
    else:
        print("resume: the combination is taken again")

    # Remapping: from F6 to F5
    VK_F5 = 0x74
    if not can_grab(PROBE_MODS, VK_F5):
        print("SKIP: Ctrl+Alt+F5 is taken - the rebind part is skipped")
    else:
        hk.rebind({1: (PROBE_MODS, VK_F5, "toggle", "Ctrl+Alt+F5")})
        time.sleep(0.5)
        print(f"after rebind: registered={hk.registered}")
        if "Ctrl+Alt+F5" not in hk.registered:
            failures.append("the new combination is not registered")
        if can_grab(PROBE_MODS, VK_F5):
            failures.append("the new combination is free - rebind did not work")
        if not can_grab(PROBE_MODS, VK_F6):
            failures.append("the old combination stayed taken after the rebind")
        else:
            print("rebind: the old one was released, the new one is taken")

    hk.stop()
    time.sleep(0.4)
    if not can_grab(PROBE_MODS, VK_F5) and not can_grab(PROBE_MODS, VK_F6):
        failures.append("the combinations were not released after stop")

    # build_bindings from the config: command name -> string
    over = build_bindings({"toggle": "Ctrl+Shift+K"})
    got = next(b for b in over.values() if b[2] == "toggle")
    print(f"build_bindings: toggle -> {got[3]}")
    if got[3] != "Ctrl+Shift+K":
        failures.append(f"the config override was not applied: {got}")
    # The rest must stay at their defaults
    if len(over) != len(DEFAULT_BINDINGS):
        failures.append("the override lost some of the bindings")

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: suspend/resume/rebind work on live hotkeys")
    return 0


if __name__ == "__main__":
    sys.exit(main())
