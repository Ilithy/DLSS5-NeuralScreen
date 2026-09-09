"""The window list filter: only real taskbar windows.

The list used to include background-process helpers (owned/tool windows,
DWM-cloaked shells like TextInputHost) - the user reported "сторонние
процессы попадают в список". The filter now keeps only windows that
would show in the taskbar: top-level, unowned, not a tool window, not
DWM-cloaked, with a title.

Checked: the live list is well-formed and every entry passes the filter
invariants (taskbar window, not the desktop, non-empty title, unique).
"""
import os
import sys
import importlib.util

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# main.py is a script with a `main()` entry point - import it under an
# explicit name so `import main` cannot pick up a different module.
_spec = importlib.util.spec_from_file_location("ns_main", os.path.join(ROOT, "main.py"))
ns_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ns_main)


def main() -> int:
    failures = []
    wins = ns_main.list_capturable_windows()
    print(f"live window list: {len(wins)} entries")
    if not isinstance(wins, list):
        failures.append("list_capturable_windows should return a list")
        print("FAIL: not a list")
        return 1

    seen = set()
    for hwnd, title in wins:
        if not isinstance(hwnd, int) or not isinstance(title, str):
            failures.append(f"malformed entry: {hwnd!r}, {title!r}")
            continue
        if not title.strip():
            failures.append(f"0x{hwnd:X}: empty title slipped through")
        if not ns_main._is_taskbar_window(hwnd):
            failures.append(f"0x{hwnd:X} ({title}): not a taskbar window")
        if ns_main._is_desktop_window(hwnd):
            failures.append(f"0x{hwnd:X} ({title}): the desktop slipped through")
        if hwnd in seen:
            failures.append(f"0x{hwnd:X}: duplicate hwnd")
        seen.add(hwnd)

    for hwnd, title in wins:
        print(f"  {hwnd:X}: {title[:60]}")

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: the window list holds only real taskbar windows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
