"""The localization contract: every language has the same keys, and the
HUD/alert strings are not empty in any of them.

Pure unit test - no worker, no window. A missing key in one language
silently falls back to the default in the UI, which is exactly the bug
this guards against: a new string added to "en" and forgotten in "ru"
renders English in the Russian UI.
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

from i18n import DEFAULT_LANG, STRINGS  # noqa: E402


def main() -> int:
    failures = []

    if DEFAULT_LANG not in STRINGS:
        failures.append(f"the default language {DEFAULT_LANG!r} is not in STRINGS")
        return 1

    en = STRINGS[DEFAULT_LANG]
    for lang, table in STRINGS.items():
        if lang == DEFAULT_LANG:
            continue
        missing = set(en) - set(table)
        if missing:
            failures.append(f"{lang}: missing keys: {sorted(missing)}")
        extra = set(table) - set(en)
        if extra:
            failures.append(f"{lang}: extra keys not in {DEFAULT_LANG}: {sorted(extra)}")
        empty = [k for k, v in table.items() if not str(v).strip()]
        if empty:
            failures.append(f"{lang}: empty strings: {empty}")

    # No empty strings in the default language either.
    empty = [k for k, v in en.items() if not str(v).strip()]
    if empty:
        failures.append(f"{DEFAULT_LANG}: empty strings: {empty}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    langs = ", ".join(sorted(STRINGS))
    print(f"OK: {len(STRINGS)} languages ({langs}) share the same {len(en)} keys, none empty")
    return 0


if __name__ == "__main__":
    sys.exit(main())
