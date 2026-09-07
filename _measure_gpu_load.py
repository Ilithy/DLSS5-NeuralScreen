"""Проверить, занят ли GPU во время Evaluate, и сравнить FPS по work_scale.

GPU-таймстемпы показали 8.5 мс на Evaluate независимо от разрешения (0.37 и
3.32 МПикс — одно время). Такое бывает по двум причинам: либо модель считает на
своём фиксированном внутреннем разрешении, либо в этом окне GPU простаивает
между мелкими запусками ядер. Первое видно по загрузке SM около 100%, второе —
по низкой загрузке при том же времени.

Запуск:  runtime\\python.exe _measure_gpu_load.py [масштабы через запятую]
"""
import io
import json
import re
import os
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
                             else ["0.65", "1.00"])]
WARMUP_S = 14.0
MEASURE_S = 14.0

FPS_RE = re.compile(r"NR ON \| FPS\s+([\d.]+)")
GPU_RE = re.compile(r"eval на GPU\s+([\d.]+)")
WORK_RE = re.compile(r"work (\d+)x(\d+)")


def sample_gpu(seconds: float) -> dict:
    """Средняя загрузка SM и частоты за интервал (nvidia-smi раз в 200 мс)."""
    sm, mem, clk = [], [], []
    end = time.time() + seconds
    while time.time() < end:
        try:
            out = subprocess.check_output(
                ["nvidia-smi",
                 "--query-gpu=utilization.gpu,utilization.memory,clocks.sm",
                 "--format=csv,noheader,nounits"],
                text=True, timeout=5).strip().splitlines()[0]
            a, b, c = [x.strip() for x in out.split(",")]
            sm.append(float(a)); mem.append(float(b)); clk.append(float(c))
        except Exception:
            pass
        time.sleep(0.2)
    if not sm:
        return {}
    return {"sm": sum(sm) / len(sm), "mem": sum(mem) / len(mem),
            "clk": sum(clk) / len(clk), "n": len(sm)}


def run_one(scale: float) -> dict | None:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    cfg["work_scale"] = scale
    cfg["open_menu_on_start"] = False
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    LOG.write_text("", encoding="utf-8")
    env = dict(os.environ, NS_PHASE="1")
    proc = subprocess.Popen([str(PY), "-u", "main.py"], cwd=str(BASE), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(WARMUP_S)
        LOG.write_text("", encoding="utf-8")
        load = sample_gpu(MEASURE_S)
        text = LOG.read_text(encoding="utf-8", errors="replace")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(1.5)

    fps = [float(m) for m in FPS_RE.findall(text)]
    gpu = [float(m) for m in GPU_RE.findall(text)]
    work = WORK_RE.search(text)
    if not fps or work is None:
        print(f"  масштаб {scale:.2f}: замеров нет")
        return None
    return {"scale": scale, "work": f"{work.group(1)}x{work.group(2)}",
            "fps": sum(fps) / len(fps), "gpu_eval": sum(gpu) / len(gpu) if gpu else 0.0,
            "load": load}


def main() -> int:
    backup = BASE / "config.json.load-bak"
    shutil.copy2(CONFIG, backup)
    rows = []
    try:
        for scale in SCALES:
            print(f"замер на work_scale {scale:.2f} ...", flush=True)
            row = run_one(scale)
            if row:
                rows.append(row)
                ld = row["load"]
                print(f"  {row['work']}: FPS {row['fps']:.1f}, "
                      f"eval на GPU {row['gpu_eval']:.2f} мс, "
                      f"SM {ld.get('sm', -1):.0f}%, память {ld.get('mem', -1):.0f}%, "
                      f"частота {ld.get('clk', -1):.0f} МГц", flush=True)
    finally:
        shutil.copy2(backup, CONFIG)
        backup.unlink(missing_ok=True)
        print("config.json восстановлен")

    print("\nмасштаб  work         FPS    eval GPU  SM%   память%  МГц")
    for r in rows:
        ld = r["load"]
        print(f"{r['scale']:>6.2f}  {r['work']:<12} {r['fps']:>5.1f}  "
              f"{r['gpu_eval']:>7.2f}  {ld.get('sm', -1):>4.0f}  "
              f"{ld.get('mem', -1):>7.0f}  {ld.get('clk', -1):>4.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
