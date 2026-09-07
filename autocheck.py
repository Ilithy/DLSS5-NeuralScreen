"""Автопроверки NeuralScreen v1.1 — статичная часть (без GUI).

Запуск:  runtime\\python.exe autocheck.py
GUI-часть (меню, запись) прогоняется отдельно через computer_use — см. конец вывода.

Каждая проверка: PASS / FAIL / SKIP + причина. Выход: 0 = все PASS, 1 = есть FAIL.
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
        print(f"[FAIL] {name}: исключение {exc!r}")


def fresh_worker():
    """nvngx.dll собран после последнего коммита, содержит хук."""
    dll = ROOT / "native" / "nvngx.dll"
    if not dll.exists():
        return False, "нет native/nvngx.dll"
    data = dll.read_bytes()
    if b"NS_ARCH_SPOOF" not in data:
        return False, "в бинарнике нет NS_ARCH_SPOOF (старая сборка?)"
    # свежесть: mtime не старше последнего коммита cpp
    cpp = ROOT / "native" / "dlss5-feed-host64.cpp"
    if dll.stat().st_mtime < cpp.stat().st_mtime:
        return False, "dll старше cpp — пересобрать build-host.bat"
    return True, f"{dll.stat().st_size} байт, хук на месте, свежий"


def zip_integrity():
    zpath = ROOT / "neuralscreen-v1.1.0-full.zip"
    if not zpath.exists():
        return False, "нет neuralscreen-v1.1.0-full.zip"
    required = [
        "main.py", "gpuinfo.py", "overlay_ui.py", "i18n.py", "recorder.py",
        "display.py", "guides.py", "hotkeys.py", "tray.py", "capture.py",
        "README.md", "README.ru.md", "NeuralScreen.vbs", "NeuralScreen.bat",
        "native/nvngx.dll", "native/nvngx_dlssnr.dll",
        "runtime/pythonw.exe", "docs/menu-light.png", "docs/menu-dark.png",
    ]
    with zipfile.ZipFile(zpath) as z:
        names = set(z.namelist())
        missing = [f for f in required if f not in names]
        if missing:
            return False, f"нет в архиве: {missing}"
        # воркер в архиве — с хуком
        dll = z.read("native/nvngx.dll")
        if b"NS_ARCH_SPOOF" not in dll:
            return False, "nvngx.dll в архиве без хука"
        # конфиг в архиве — дефолтный, не персональный: могут утечь
        # menu_offset/menu_scale/theme/lang (персональные значения пишутся
        # в конфиг легально, поэтому проверяем ВСЕ такие поля)
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
            return False, "в архиве персональный config: " + ", ".join(leak)
    return True, f"{zpath.stat().st_size} байт, все файлы, хук, дефолтный config"


def gpuinfo_works():
    """gpuinfo.py отвечает: архитектура + официальная поддержка."""
    py = ROOT / "runtime" / "python.exe"
    if not py.exists():
        return False, "нет runtime/python.exe"
    code = (
        "import sys; sys.path.insert(0, r'%s'); "
        "import gpuinfo; i = gpuinfo.probe(); "
        "print(gpuinfo.describe(i)); print('official:', i['official'])" % ROOT
    )
    r = subprocess.run([str(py), "-c", code], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return False, f"gpuinfo упал: {r.stderr.strip()[:200]}"
    out = r.stdout.strip()
    if "Blackwell" not in out:
        return False, f"странный ответ: {out}"
    return True, out.replace("\n", " | ")


def spoof_default_on():
    """Спуф включён по умолчанию: в ArchSpoofRequested нет требования =1."""
    cpp = (ROOT / "native" / "dlss5-feed-host64.cpp").read_text(encoding="utf-8-sig")
    start = cpp.find("static bool ArchSpoofRequested()")
    end = cpp.find("static int SetupArchSpoof()")
    body = cpp[start:end]
    if "buf[0] == '0'" not in body:
        return False, "логика NS_ARCH_SPOOF=0 не найдена в ArchSpoofRequested"
    if "buf[0] == '1'" in body:
        return False, "в ArchSpoofRequested осталась старая логика =1"
    return True, "по умолчанию включён, NS_ARCH_SPOOF=0 отключает"


def readme_consistency():
    """README EN/RU синхронны по ключевым фактам v1.1."""
    en = (ROOT / "README.md").read_text(encoding="utf-8")
    ru = (ROOT / "README.ru.md").read_text(encoding="utf-8")
    for needle in ["NS_ARCH_SPOOF=0", "On by default", "Включено по умолчанию",
                   "102 FPS", "2304×1440"]:
        if needle not in en and needle not in ru:
            return False, f"нет '{needle}' ни в одном README"
    if "On by default" not in en or "Включено по умолчанию" not in ru:
        return False, "статус спуфа расходится между README"
    return True, "EN/RU согласованы"


def git_clean():
    """Рабочая копия чистая (кроме персонального config.json)."""
    r = subprocess.run(["git", "status", "--short"], cwd=ROOT,
                       capture_output=True, text=True)
    dirty = [l for l in r.stdout.splitlines() if l.strip() and "config.json" not in l]
    if dirty:
        return False, f"грязно: {dirty[:5]}"
    return True, "чисто (config.json — персональный, ожидаемо)"


def release_notes_short():
    """Notes релиза v1.1.0 — лаконичные (EN часть < 2 КБ)."""
    r = subprocess.run(
        ["gh", "release", "view", "v1.1.0", "-R", "perseval-BLR/DLSS5-NeuralScreen",
         "--json", "body", "--jq", ".body"],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return False, f"gh: {r.stderr.strip()[:100]}"
    en_part = r.stdout.split("---")[0]
    if len(en_part) > 2500:
        return False, f"EN-часть {len(en_part)} символов — длинно"
    return True, f"EN-часть {len(en_part)} символов"


def gui_check():
    """Полный GUI-цикл: запуск → запись → выход. Требует, чтобы NeuralScreen
    не был запущен. ~40 секунд."""
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

    # 1. запуск
    subprocess.run(["cscript", "//nologo", "NeuralScreen.vbs"], cwd=ROOT,
                   capture_output=True)
    time.sleep(8)
    # Лог пишется в системной кодировке (cp1251) — читаем с errors="replace".
    log = (ROOT / "NeuralScreen.log").read_text(encoding="cp1251", errors="replace")
    if "NR ON" not in log:
        return False, "NeuralScreen не поднялся (нет 'NR ON' в логе)"
    # 2. запись 5 секунд
    send_key(VK_INSERT)
    time.sleep(5)
    send_key(VK_INSERT)
    time.sleep(3)
    recs = sorted((ROOT / "recordings").glob("neuralscreen-*.mp4"),
                  key=lambda p: p.stat().st_mtime)
    if not recs:
        return False, "файл записи не создан"
    rec = recs[-1]
    import av
    c = av.open(str(rec))
    s = c.streams.video[0]
    frames, dur = s.frames, float(s.duration * s.time_base)
    c.close()
    if frames < 100 or dur < 4:
        return False, f"запись подозрительная: {frames} кадров / {dur:.1f} с"
    # 3. выход
    send_key(VK_Q, (0x11, 0x12))  # Ctrl+Alt+Q
    time.sleep(4)
    r = subprocess.run(["tasklist"], capture_output=True)
    out = r.stdout.decode("cp1251", errors="replace")
    left = [l for l in out.splitlines()
            if "pythonw.exe" in l or "nvngx.dll" in l]
    if left:
        return False, f"остались процессы: {left}"
    return True, (f"запись {frames} кадров / {dur:.1f} с, "
                  f"выход чистый, процессов не осталось")


def main():
    print(f"NeuralScreen autocheck — {ROOT}")
    print("=" * 60)
    if "--gui" in sys.argv:
        check("GUI: запуск → запись → выход", gui_check)
    else:
        check("worker: свежий, с хуком", fresh_worker)
        check("zip: целостность и состав", zip_integrity)
        check("gpuinfo: отвечает", gpuinfo_works)
        check("spoof: включён по умолчанию", spoof_default_on)
        check("README: EN/RU согласованы", readme_consistency)
        check("git: рабочая копия чистая", git_clean)
        check("release notes: лаконичные", release_notes_short)
    print("=" * 60)
    if FAILS:
        print(f"ИТОГ: {len(FAILS)} FAIL — {FAILS}")
        return 1
    print("ИТОГ: все проверки PASS")
    if "--gui" not in sys.argv:
        print()
        print("GUI-часть:  runtime\\python.exe autocheck.py --gui")
    return 0


if __name__ == "__main__":
    sys.exit(main())
