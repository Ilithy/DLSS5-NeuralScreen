"""Hotkey parsing, rebinding and default-binding sanity.

The point of this test is that the hotkey layer does not break when the
default bindings change (they moved from Insert to Num0 in v1.4 and a
measurement script broke because it hard-coded the old key). So:

  * every key name the parser knows round-trips through parse_binding;
  * aliases (CONTROL == CTRL, case, whitespace) resolve to the same VK;
  * build_bindings applies overrides and silently ignores bad ones;
  * DEFAULT_BINDINGS are internally consistent: unique commands, valid
    keys, and numlock_needed() names exactly the numpad bindings;
  * the test itself never hard-codes a key: it reads DEFAULT_BINDINGS.

Run:  runtime\\python.exe test_hotkey_bindings.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the project root
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

from hotkeys import (DEFAULT_BINDINGS, _KEY_NAMES, _NUMPAD_VKS,  # noqa: E402
                     MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, MOD_SHIFT,
                     build_bindings, numlock_needed, parse_binding)


def main() -> int:
    failures = []

    # 1. Every key name the parser knows must parse back to a VK.
    for name, vk in _KEY_NAMES.items():
        parsed = parse_binding(name)
        if parsed is None:
            failures.append(f"parse_binding({name!r}) returned None")
        elif parsed[1] != vk:
            failures.append(f"parse_binding({name!r}) -> {parsed[1]:#x}, "
                            f"expected {vk:#x}")

    # 2. Aliases and case/whitespace tolerance.
    for text, expect in (
        ("Ctrl+Alt+Q", (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x51)),
        ("CONTROL+ALT+Q", (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x51)),
        (" ctrl + alt + q ", (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x51)),
        ("Shift+F1", (MOD_SHIFT | MOD_NOREPEAT, 0x70)),
        ("Num0", (MOD_NOREPEAT, 0x60)),
        ("Insert", (MOD_NOREPEAT, 0x2D)),
    ):
        got = parse_binding(text)
        if got != expect:
            failures.append(f"parse_binding({text!r}) -> {got}, expected {expect}")

    # 3. Malformed strings are rejected, not half-parsed.
    for bad in ("", "Ctrl+", "+Q", "Ctrl+Alt+Q+W", "FakeKey", "Ctrl+Q+Alt"):
        if parse_binding(bad) is not None:
            failures.append(f"parse_binding({bad!r}) should be None")

    # 4. build_bindings: overrides apply, bad ones are ignored.
    overrides = {"toggle": "F10", "record": "Insert", "settings": "NotAKey"}
    built = build_bindings(overrides)
    by_cmd = {entry[2]: entry for entry in built.values()}
    if by_cmd["toggle"][1] != 0x79:  # F10
        failures.append("toggle override did not apply (F10)")
    if by_cmd["record"][1] != 0x2D:  # Insert
        failures.append("record override did not apply (Insert)")
    if by_cmd["settings"][1] != DEFAULT_BINDINGS[2][1]:
        failures.append("bad override replaced the default settings binding")

    # 5. DEFAULT_BINDINGS: unique commands, valid keys, sane mods.
    cmds = [entry[2] for entry in DEFAULT_BINDINGS.values()]
    if len(cmds) != len(set(cmds)):
        failures.append("DEFAULT_BINDINGS has duplicate commands")
    for hk_id, (mods, vk, cmd, name) in DEFAULT_BINDINGS.items():
        if vk not in _KEY_NAMES.values():
            failures.append(f"binding {name} has an unknown VK {vk:#x}")
        if not (mods & MOD_NOREPEAT):
            failures.append(f"binding {name} lacks MOD_NOREPEAT")
        if parse_binding(name) is None:
            failures.append(f"binding name {name!r} does not parse")

    # 6. numlock_needed names exactly the numpad bindings.
    needed = set(numlock_needed())
    numpad_names = {name for _, vk, _c, name in DEFAULT_BINDINGS.values()
                    if vk in _NUMPAD_VKS}
    if needed != numpad_names:
        failures.append(f"numlock_needed {sorted(needed)} != "
                        f"numpad bindings {sorted(numpad_names)}")

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"OK: {len(_KEY_NAMES)} key names, aliases, overrides, "
          f"{len(DEFAULT_BINDINGS)} default bindings, numlock set")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
