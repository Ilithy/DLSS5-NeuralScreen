"""Зависимость стоимости NGX (eval) от work-разрешения.

Запускает NeuralScreen с NS_PHASE=1, ступенями поднимает work_scale
(Ctrl+Alt+Up) и сопоставляет строки [phase] с активным масштабом по
временным меткам лога. В конце — таблица и линейная подгонка eval по
мегапикселям: постоянная часть против зависящей от разрешения.

От человека нужно одно: чтобы на экране всё время что-то менялось
(таскать окно, крутить страницу) — иначе Desktop Duplication не отдаёт
кадры и мерить нечего.

config.json восстанавливается в исходное состояние.
"""
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
PY = BASE / "runtime" / "python.exe"
LOG = BASE / "NeuralScreen.log"
CFG = BASE / "config.json"

START_SCALE = 0.25
STEPS = 5          # 0.25 -> 0.65, по 2 нажатия (+0.10) между замерами
HOLD = 12.0        # секунд на ступень
PRESSES_PER_STEP = 2

VK_CTRL, VK_ALT, VK_UP, VK_Q = 0x11, 0x12, 0x26, 0x51
KEYEVENTF_KEYUP = 0x0002
user32 = ctypes.windll.user32


def chord(vk, hold=0.10):
    for k in (VK_CTRL, VK_ALT, vk):
        user32.keybd_event(k, 0, 0, 0)
    try:
        time.sleep(hold)
    finally:
        for k in (vk, VK_ALT, VK_CTRL):
            user32.keybd_event(k, 0, KEYEVENTF_KEYUP, 0)


def release_stuck():
    for vk in (VK_CTRL, VK_ALT, VK_UP, VK_Q):
        if user32.GetAsyncKeyState(vk) & 0x8000:
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


# --- config: выставить стартовый масштаб, сохранив оригинал ---------------
backup = CFG.with_suffix(".json.measure-bak")
shutil.copy2(CFG, backup)
cfg = json.loads(CFG.read_text(encoding="utf-8"))
cfg["work_scale"] = START_SCALE
CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"work_scale выставлен в {START_SCALE}, оригинал в {backup.name}")

if LOG.exists():
    LOG.unlink()

release_stuck()
lines = []
env = dict(os.environ, PYTHONIOENCODING="utf-8", NS_PHASE="1")
proc = subprocess.Popen([str(PY), "-u", str(BASE / "main.py")], cwd=str(BASE), env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        bufsize=1, text=True, encoding="utf-8", errors="replace")
threading.Thread(target=lambda: [lines.append(l.rstrip()) for l in proc.stdout],
                 daemon=True).start()

try:
    print("Жду первых кадров… ДЕРЖИ ЭКРАН МЕНЯЮЩИМСЯ (таскай окно).")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if any("NR ON | FPS" in l for l in lines):
            break
        if proc.poll() is not None:
            print("!! программа не стартовала")
            sys.exit(1)
        time.sleep(0.3)

    for step in range(STEPS):
        print(f"  ступень {step + 1}/{STEPS}: держу {HOLD:.0f} с…")
        time.sleep(HOLD)
        if step < STEPS - 1:
            for _ in range(PRESSES_PER_STEP):
                chord(VK_UP)
                time.sleep(0.5)
finally:
    if proc.poll() is None:
        chord(VK_Q)
        try:
            proc.wait(timeout=40)
        except subprocess.TimeoutExpired:
            proc.kill()
    release_stuck()
    shutil.copy2(backup, CFG)
    backup.unlink(missing_ok=True)
    print("config.json восстановлен")

# --- разбор лога -----------------------------------------------------------
text = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else "\n".join(lines)
rows = []          # (время, work_w, work_h) — смены масштаба
phases = []        # (время, eval, dda, frame)
cur = None

t_re = re.compile(r"^(\d\d):(\d\d):(\d\d)\.(\d+)")


def stamp(line):
    m = t_re.match(line)
    if not m:
        return None
    h, mi, s, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000.0


for line in text.splitlines():
    m = re.search(r"work_scale -> ([\d.]+) \((\d+)x(\d+)\)", line)
    if m:
        cur = (float(m.group(1)), int(m.group(2)), int(m.group(3)))
        continue
    if "NGX-разрешение" in line:
        m = re.search(r"work_scale ([\d.]+) \(NGX-разрешение (\d+)x(\d+)\)", line)
        if m:
            cur = (float(m.group(1)), int(m.group(2)), int(m.group(3)))
        continue
    if "[phase]" in line and "eval" in line and cur is not None:
        me = re.search(r"eval ([\d.]+)/", line)
        md = re.search(r"dda\(всего\) ([\d.]+)/", line)
        mf = re.search(r"frame ([\d.]+)/", line)
        if me:
            phases.append((cur, float(me.group(1)),
                           float(md.group(1)) if md else float("nan"),
                           float(mf.group(1)) if mf else float("nan")))

if not phases:
    print("\n!! строк [phase] с eval не нашлось — экран был статичен?")
    sys.exit(1)

agg = {}
for cur, ev, dda, fr in phases:
    agg.setdefault(cur, []).append((ev, dda, fr))

print(f"\n{'work':>11} {'МПикс':>7} {'eval мс':>9} {'dda':>6} {'frame':>7} {'FPS':>6} {'замеров':>8}")
points = []
for (scale, w, hgt), vals in sorted(agg.items(), key=lambda kv: kv[0][1] * kv[0][2]):
    ev = sum(v[0] for v in vals) / len(vals)
    dd = sum(v[1] for v in vals) / len(vals)
    fr = sum(v[2] for v in vals) / len(vals)
    mp = w * hgt / 1e6
    points.append((mp, ev))
    print(f"{w:>5}x{hgt:<5} {mp:7.3f} {ev:9.2f} {dd:6.2f} {fr:7.2f} {1000/fr if fr else 0:6.1f} {len(vals):>8}")

if len(points) >= 3:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0
    a = my - b * mx
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot else float("nan")
    print(f"\neval = {a:.2f} мс + {b:.2f} мс/МПикс   (R^2={r2:.3f})")
    print(f"  постоянная часть NGX: {a:.2f} мс")
    for w, hgt in ((1920, 1080), (2560, 1440)):
        mp = w * hgt / 1e6
        print(f"  work {w}x{hgt}: eval ~{a + b*mp:.2f} мс")
    dmp = (2560 * 1440 - 1920 * 1080) / 1e6
    print(f"\n  переход 1920x1080 -> 2560x1440 стоит {b*dmp:+.2f} мс на кадр")
