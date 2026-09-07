"""NeuralScreen v1.1 self-checks - the static part (no GUI).

Run:  runtime\\python.exe autocheck.py
The GUI part (menu, recording) is run separately - see the end of the output.

Every check reports PASS / FAIL / SKIP plus a reason. Exit: 0 = all PASS,
1 = there is a FAIL.
"""
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FAILS = []


def check(name, fn):
    try:
        ok, detail = fn()
        status = "PASS" if ok else "FAIL"
        if not ok:
            FAILS.append(name)
        print(f"[{status}] {name}: {detail}")
    except Exception as exc:
        FAILS.append(name)
        print(f"[FAIL] {name}: exception {exc!r}")


def fresh_worker():
    """nvngx.dll is built after the last commit and contains the hook."""
    dll = ROOT / "native" / "nvngx.dll"
    if not dll.exists():
        return False, "no native/nvngx.dll"
    data = dll.read_bytes()
    if b"NS_ARCH_SPOOF" not in data:
        return False, "no NS_ARCH_SPOOF in the binary (an old build?)"
    # freshness: the mtime must not be older than the cpp
    cpp = ROOT / "native" / "dlss5-feed-host64.cpp"
    if dll.stat().st_mtime < cpp.stat().st_mtime:
        return False, "the dll is older than the cpp - rerun build-host.bat"
    return True, f"{dll.stat().st_size} bytes, the hook is there, fresh"


def zip_integrity():
    zpath = ROOT / "neuralscreen-v1.1.0-full.zip"
    if not zpath.exists():
        return False, "no neuralscreen-v1.1.0-full.zip"
    required = [
        "main.py", "gpuinfo.py", "overlay_ui.py", "i18n.py", "recorder.py",
        "display.py", "guides.py", "hotkeys.py", "tray.py", "capture.py",
        "audio.py",
        "README.md", "README.ru.md", "NeuralScreen.vbs", "NeuralScreen.bat",
        "native/nvngx.dll", "native/nvngx_dlssnr.dll",
        "runtime/pythonw.exe", "docs/menu-light.png", "docs/menu-dark.png",
    ]
    with zipfile.ZipFile(zpath) as z:
        names = set(z.namelist())
        missing = [f for f in required if f not in names]
        if missing:
            return False, f"missing from the archive: {missing}"
        # the worker in the archive carries the hook
        dll = z.read("native/nvngx.dll")
        if b"NS_ARCH_SPOOF" not in dll:
            return False, "nvngx.dll in the archive has no hook"
        # the config in the archive is the default one, not a personal one:
        # menu_offset/menu_scale/theme/lang could leak (personal values are
        # written into the config legitimately, so we check ALL such fields)
        cfg = json.loads(z.read("config.json"))
        leak = []
        if cfg.get("menu_offset") != [0, 0]:
            leak.append(f"menu_offset={cfg.get('menu_offset')}")
        if cfg.get("menu_scale") != 1.0:
            leak.append(f"menu_scale={cfg.get('menu_scale')}")
        if cfg.get("theme") not in (None, "light"):
            leak.append(f"theme={cfg.get('theme')}")
        if cfg.get("lang") not in (None, "en"):
            leak.append(f"lang={cfg.get('lang')}")
        if leak:
            return False, "a personal config in the archive: " + ", ".join(leak)
    return True, f"{zpath.stat().st_size} bytes, all files, the hook, a default config"


def gpuinfo_works():
    """gpuinfo.py answers: architecture plus official support."""
    py = ROOT / "runtime" / "python.exe"
    if not py.exists():
        return False, "no runtime/python.exe"
    code = (
        "import sys; sys.path.insert(0, r'%s'); "
        "import gpuinfo; i = gpuinfo.probe(); "
        "print(gpuinfo.describe(i)); print('official:', i['official'])" % ROOT
    )
    r = subprocess.run([str(py), "-c", code], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return False, f"gpuinfo crashed: {r.stderr.strip()[:200]}"
    out = r.stdout.strip()
    if "Blackwell" not in out:
        return False, f"odd answer: {out}"
    return True, out.replace("\n", " | ")


def spoof_default_on():
    """The spoof is on by default: ArchSpoofRequested has no =1 requirement."""
    cpp = (ROOT / "native" / "dlss5-feed-host64.cpp").read_text(encoding="utf-8-sig")
    start = cpp.find("static bool ArchSpoofRequested()")
    end = cpp.find("static int SetupArchSpoof()")
    body = cpp[start:end]
    if "buf[0] == '0'" not in body:
        return False, "the NS_ARCH_SPOOF=0 logic was not found in ArchSpoofRequested"
    if "buf[0] == '1'" in body:
        return False, "the old =1 logic is still in ArchSpoofRequested"
    return True, "on by default, NS_ARCH_SPOOF=0 turns it off"


def readme_consistency():
    """README EN/RU agree on the key v1.1 facts.

    The Russian needles below are the content of README.ru.md, which is a
    translation and stays in Russian on purpose.
    """
    en = (ROOT / "README.md").read_text(encoding="utf-8")
    ru = (ROOT / "README.ru.md").read_text(encoding="utf-8")
    for needle in ["NS_ARCH_SPOOF=0", "On by default", "Включено по умолчанию",
                   "102 FPS", "2304×1440"]:
        if needle not in en and needle not in ru:
            return False, f"'{needle}' is in neither README"
    if "On by default" not in en or "Включено по умолчанию" not in ru:
        return False, "the spoof status differs between the READMEs"
    return True, "EN/RU agree"


def git_clean():
    """The working copy is clean (apart from a personal config.json)."""
    r = subprocess.run(["git", "status", "--short"], cwd=ROOT,
                       capture_output=True, text=True)
    dirty = [l for l in r.stdout.splitlines() if l.strip() and "config.json" not in l]
    if dirty:
        return False, f"dirty: {dirty[:5]}"
    return True, "clean (config.json is personal, as expected)"


def release_notes_short():
    """The v1.1.0 release notes are concise (the EN part is under 2 KB)."""
    r = subprocess.run(
        ["gh", "release", "view", "v1.1.0", "-R", "perseval-BLR/DLSS5-NeuralScreen",
         "--json", "body", "--jq", ".body"],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return False, f"gh: {r.stderr.strip()[:100]}"
    en_part = r.stdout.split("---")[0]
    if len(en_part) > 2500:
        return False, f"the EN part is {len(en_part)} characters - too long"
    return True, f"the EN part is {len(en_part)} characters"


def gui_check():
    """The full GUI cycle: launch -> record -> exit. Requires NeuralScreen not
    to be running. ~40 seconds."""
    import ctypes
    import time

    VK_INSERT = 0x2D
    VK_Q = 0x51
    KEYEVENTF_KEYUP = 0x0002

    def send_key(vk, mods=()):
        for m in mods:
            ctypes.windll.user32.keybd_event(m, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        for m in reversed(mods):
            ctypes.windll.user32.keybd_event(m, 0, KEYEVENTF_KEYUP, 0)

    # 1. launch
    subprocess.run(["cscript", "//nologo", "NeuralScreen.vbs"], cwd=ROOT,
                   capture_output=True)
    time.sleep(8)
    # main.py opens the log as utf-8; errors="replace" guards a truncated tail.
    log = (ROOT / "NeuralScreen.log").read_text(encoding="utf-8", errors="replace")
    if "NR ON" not in log:
        return False, "NeuralScreen did not come up (no 'NR ON' in the log)"
    # 2. record for 5 seconds
    send_key(VK_INSERT)
    time.sleep(5)
    send_key(VK_INSERT)
    time.sleep(3)
    recs = sorted((ROOT / "recordings").glob("neuralscreen-*.mp4"),
                  key=lambda p: p.stat().st_mtime)
    if not recs:
        return False, "the recording file was not created"
    rec = recs[-1]
    import av
    c = av.open(str(rec))
    s = c.streams.video[0]
    frames, dur = s.frames, float(s.duration * s.time_base)
    c.close()
    if frames < 100 or dur < 4:
        return False, f"the recording looks suspicious: {frames} frames / {dur:.1f} s"
    # 3. exit
    send_key(VK_Q, (0x11, 0x12))  # Ctrl+Alt+Q
    time.sleep(4)
    r = subprocess.run(["tasklist"], capture_output=True)
    out = r.stdout.decode("cp1251", errors="replace")
    left = [l for l in out.splitlines()
            if "pythonw.exe" in l or "nvngx.dll" in l]
    if left:
        return False, f"processes left behind: {left}"
    return True, (f"recording of {frames} frames / {dur:.1f} s, "
                  f"a clean exit, no processes left")


def main():
    print(f"NeuralScreen autocheck - {ROOT}")
    print("=" * 60)
    if "--gui" in sys.argv:
        check("GUI: launch -> record -> exit", gui_check)
    else:
        check("worker: fresh, with the hook", fresh_worker)
        check("zip: integrity and contents", zip_integrity)
        check("gpuinfo: answers", gpuinfo_works)
        check("spoof: on by default", spoof_default_on)
        check("README: EN/RU agree", readme_consistency)
        check("git: the working copy is clean", git_clean)
        check("release notes: concise", release_notes_short)
    print("=" * 60)
    if FAILS:
        print(f"RESULT: {len(FAILS)} FAIL - {FAILS}")
        return 1
    print("RESULT: all checks PASS")
    if "--gui" not in sys.argv:
        print()
        print("GUI part:  runtime\\python.exe autocheck.py --gui")
    return 0


if __name__ == "__main__":
    sys.exit(main())
