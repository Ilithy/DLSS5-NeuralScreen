"""Замер GPU-времени NGX Evaluate против work-разрешения.

Отвечает на вопрос, упирается ли Evaluate в вычисления или в фиксированные
накладные расходы. Первый вариант — время растёт с числом пикселей, второй —
стоит на месте.

Меряется таймстемпами на очереди D3D12 внутри воркера (строка «eval на GPU» в
NeuralScreen.log при NS_PHASE=1), а не временем вокруг submit — то есть чистое
время счёта, без просыпания потока.

Запуск:  runtime\\python.exe _measure_eval_gpu.py [масштабы через запятую]
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
LOG = BASE / "NeuralScreen.log"
PY = BASE / "runtime" / "python.exe"
SCALES = [float(x) for x in (sys.argv[1].split(",") if len(sys.argv) > 1
                             else ["0.30", "0.45", "0.65", "0.85", "1.00"])]
WARMUP_S = 14.0     # прогрев: NGX перекалибровывается первые кадры
MEASURE_S = 12.0

PHASE_RE = re.compile(
    r"eval\s+([\d.]+)/[\d.]+.*?eval на GPU\s+([\d.]+)/([\d.]+)")
WORK_RE = re.compile(r"work (\d+)x(\d+)")


def run_one(scale: float) -> dict | None:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    cfg["work_scale"] = scale
    # Меню при запуске закрыто: открытое перехватывает ввод и мешает.
    cfg["open_menu_on_start"] = False
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")

    LOG.write_text("", encoding="utf-8")
    env = dict(os.environ, NS_PHASE="1")
    proc = subprocess.Popen([str(PY), "-u", "main.py"], cwd=str(BASE), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(WARMUP_S)
        LOG.write_text("", encoding="utf-8")   # выбросить прогрев
        time.sleep(MEASURE_S)
        text = LOG.read_text(encoding="utf-8", errors="replace")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(1.5)

    gpu, cpu = [], []
    for line in text.splitlines():
        m = PHASE_RE.search(line)
        if m:
            cpu.append(float(m.group(1)))
            gpu.append(float(m.group(2)))
    work = WORK_RE.search(text)
    if not gpu or work is None:
        print(f"  масштаб {scale:.2f}: замеров нет "
              f"(строк phase {len(gpu)}, work {'есть' if work else 'нет'})")
        return None
    w, h = int(work.group(1)), int(work.group(2))
    return {"scale": scale, "w": w, "h": h, "mpix": w * h / 1e6,
            "gpu": sum(gpu) / len(gpu), "cpu": sum(cpu) / len(cpu),
            "n": len(gpu)}


def main() -> int:
    backup = BASE / "config.json.measure-bak"
    shutil.copy2(CONFIG, backup)
    rows = []
    try:
        for scale in SCALES:
            print(f"замер на work_scale {scale:.2f} ...", flush=True)
            row = run_one(scale)
            if row:
                rows.append(row)
                print(f"  {row['w']}x{row['h']} ({row['mpix']:.2f} МПикс): "
                      f"GPU {row['gpu']:.2f} мс, CPU {row['cpu']:.2f} мс, "
                      f"выборок {row['n']}", flush=True)
    finally:
        shutil.copy2(backup, CONFIG)
        backup.unlink(missing_ok=True)
        print("config.json восстановлен")

    if len(rows) < 2:
        print("данных мало, вывод делать не на чем")
        return 1
    print("\nмасштаб  work         МПикс   GPU eval  мс/МПикс")
    for r in rows:
        print(f"{r['scale']:>6.2f}  {r['w']}x{r['h']:<6}  {r['mpix']:>5.2f}  "
              f"{r['gpu']:>7.2f}  {r['gpu'] / r['mpix']:>8.2f}")
    lo, hi = rows[0], rows[-1]
    dp = hi["mpix"] - lo["mpix"]
    dt = hi["gpu"] - lo["gpu"]
    print(f"\nпикселей больше в {hi['mpix'] / lo['mpix']:.2f} раза, "
          f"времени больше в {hi['gpu'] / lo['gpu']:.2f} раза")
    if dp > 0:
        # Линейная модель t = fixed + slope * mpix по краям диапазона: если
        # fixed забирает почти всё, ускорять математику бессмысленно.
        slope = dt / dp
        fixed = lo["gpu"] - slope * lo["mpix"]
        print(f"линейно: {fixed:.2f} мс постоянных + {slope:.2f} мс/МПикс")
        share = max(0.0, min(1.0, fixed / hi["gpu"]))
        print(f"на максимуме диапазона постоянная часть — {share * 100:.0f}% времени")
    return 0


if __name__ == "__main__":
    sys.exit(main())
