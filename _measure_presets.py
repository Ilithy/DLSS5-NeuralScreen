"""Перебор DLSSNR.Hint.Render.Preset: цена на GPU, FPS, создаётся ли feature.

Подсказка модели стояла в 0 (default) и никогда не проверялась. Поскольку
время Evaluate не зависит от разрешения (модель считает на своём внутреннем),
пресет — единственный оставшийся рычаг на эти 8 мс.

Пресет берётся воркером из NS_NR_PRESET (см. NrPresetHint в
dlss5-feed-host64.cpp). Скрипт меряет только цену; картинку по пресетам надо
смотреть отдельно — цена без качества смысла не имеет.

Запуск:  runtime\\python.exe _measure_presets.py [пресеты через запятую]
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
PRESETS = [int(x) for x in (sys.argv[1].split(",") if len(sys.argv) > 1
                            else ["0", "1", "2", "3", "4", "5", "6", "7"])]
WARMUP_S = 14.0
MEASURE_S = 10.0

FPS_RE = re.compile(r"NR ON \| FPS\s+([\d.]+)")
GPU_RE = re.compile(r"eval на GPU\s+([\d.]+)")
READY_RE = re.compile(r"feature 18 ready: (\d+)x(\d+).*?preset=(\d+) result=0x([0-9A-Fa-f]+)")
FAIL_RE = re.compile(r"feature 18 create failed 0x([0-9A-Fa-f]+) \(([^)]+)\)")


def run_one(preset: int) -> dict:
    LOG.write_text("", encoding="utf-8")
    env = dict(os.environ, NS_PHASE="1", NS_NR_PRESET=str(preset))
    proc = subprocess.Popen([str(PY), "-u", "main.py"], cwd=str(BASE), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(WARMUP_S)
        head = LOG.read_text(encoding="utf-8", errors="replace")
        LOG.write_text("", encoding="utf-8")
        time.sleep(MEASURE_S)
        tail = LOG.read_text(encoding="utf-8", errors="replace")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(1.5)

    text = head + tail
    row = {"preset": preset, "work": "?"}
    fail = FAIL_RE.search(text)
    ready = READY_RE.search(text)
    if ready:
        row["work"] = f"{ready.group(1)}x{ready.group(2)}"
        row["used"] = int(ready.group(3))
        row["result"] = "0x" + ready.group(4).upper()
    elif fail:
        row["result"] = "0x" + fail.group(1).upper()
        row["error"] = fail.group(2)
        return row
    else:
        # Строки создания может не быть в окне замера (она печатается один раз
        # при старте) — это не повод выбрасывать цифры.
        row["result"] = "?"
    fps = [float(x) for x in FPS_RE.findall(text)]
    gpu = [float(x) for x in GPU_RE.findall(text)]
    row["fps"] = sum(fps) / len(fps) if fps else 0.0
    row["gpu"] = sum(gpu) / len(gpu) if gpu else 0.0
    row["n"] = len(gpu)
    return row


def main() -> int:
    backup = BASE / "config.json.preset-bak"
    shutil.copy2(CONFIG, backup)
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    cfg["open_menu_on_start"] = False
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    rows = []
    try:
        for preset in PRESETS:
            print(f"пресет {preset} ...", flush=True)
            row = run_one(preset)
            rows.append(row)
            if "fps" in row:
                print(f"  {row['work']}: GPU eval {row['gpu']:.2f} мс, "
                      f"FPS {row['fps']:.1f}, result {row['result']}", flush=True)
            else:
                print(f"  не создалось: {row['result']} "
                      f"{row.get('error', '')}", flush=True)
    finally:
        shutil.copy2(backup, CONFIG)
        backup.unlink(missing_ok=True)

    print("\nпресет  GPU eval  FPS    результат")
    for r in rows:
        if "fps" in r:
            print(f"{r['preset']:>6}  {r['gpu']:>8.2f}  {r['fps']:>5.1f}  {r['result']}")
        else:
            print(f"{r['preset']:>6}  {'—':>8}  {'—':>5}  {r['result']} "
                  f"{r.get('error', '')}")
    ok = [r for r in rows if "fps" in r and r["gpu"] > 0]
    if len(ok) >= 2:
        best = min(ok, key=lambda r: r["gpu"])
        base = next((r for r in ok if r["preset"] == 0), None)
        if base and best["preset"] != 0:
            print(f"\nсамый дешёвый — пресет {best['preset']}: "
                  f"{best['gpu']:.2f} мс против {base['gpu']:.2f} мс у нулевого "
                  f"({(1 - best['gpu'] / base['gpu']) * 100:.0f}% быстрее)")
        else:
            print("\nразницы по цене между пресетами нет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
