"""Перерегистрация хоткеев на ходу: snap/resume/rebind.

RegisterHotKey с hWnd=None привязан к вызывающему потоку, поэтому снимать и
ставить назначения можно только изнутри потока хоткеев — через свои
сообщения. Тест проверяет, что это действительно происходит, а не только
компилируется.

Проверяется по факту: после rebind новая комбинация в registered, старой там
нет; после suspend повторная регистрация той же комбинации СО СТОРОНЫ
проходит (значит хоткей действительно снят), после resume — не проходит.
"""
import ctypes
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hotkeys import (DEFAULT_BINDINGS, HotkeyController,  # noqa: E402
                     MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, build_bindings)

user32 = ctypes.windll.user32
# Берём заведомо свободную комбинацию, чтобы не воевать с системой.
PROBE_ID = 900
PROBE_MODS = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
VK_F6 = 0x75


def can_grab(mods: int, vk: int) -> bool:
    """Удалось ли зарегистрировать комбинацию из ЭТОГО потока.

    Если да — значит её никто не держит. Сразу отпускаем.
    """
    if user32.RegisterHotKey(None, PROBE_ID, mods, vk):
        user32.UnregisterHotKey(None, PROBE_ID)
        return True
    return False


def main() -> int:
    failures = []
    commands: queue.Queue = queue.Queue()

    # Наши биндинги: только одна комбинация, чтобы проба была однозначной.
    bindings = {1: (PROBE_MODS, VK_F6, "toggle", "Ctrl+Alt+F6")}
    if not can_grab(PROBE_MODS, VK_F6):
        print("SKIP: Ctrl+Alt+F6 уже занята в системе — проба недостоверна")
        return 0

    hk = HotkeyController(commands, bindings)
    hk.start()
    print(f"старт: registered={hk.registered} failed={hk.failed}")
    if "Ctrl+Alt+F6" not in hk.registered:
        failures.append("не зарегистрировалась при старте")
    if can_grab(PROBE_MODS, VK_F6):
        failures.append("комбинация свободна, хотя контроллер её взял")

    hk.suspend()
    time.sleep(0.4)
    if not can_grab(PROBE_MODS, VK_F6):
        failures.append("после suspend комбинация всё ещё занята")
    else:
        print("suspend: комбинация освободилась")

    hk.resume()
    time.sleep(0.4)
    if can_grab(PROBE_MODS, VK_F6):
        failures.append("после resume комбинация не занята обратно")
    else:
        print("resume: комбинация занята снова")

    # Переназначение: с F6 на F5
    VK_F5 = 0x74
    if not can_grab(PROBE_MODS, VK_F5):
        print("SKIP: Ctrl+Alt+F5 занята — часть про rebind пропущена")
    else:
        hk.rebind({1: (PROBE_MODS, VK_F5, "toggle", "Ctrl+Alt+F5")})
        time.sleep(0.5)
        print(f"после rebind: registered={hk.registered}")
        if "Ctrl+Alt+F5" not in hk.registered:
            failures.append("новая комбинация не зарегистрирована")
        if can_grab(PROBE_MODS, VK_F5):
            failures.append("новая комбинация свободна — rebind не сработал")
        if not can_grab(PROBE_MODS, VK_F6):
            failures.append("старая комбинация осталась занятой после rebind")
        else:
            print("rebind: старая освободилась, новая занята")

    hk.stop()
    time.sleep(0.4)
    if not can_grab(PROBE_MODS, VK_F5) and not can_grab(PROBE_MODS, VK_F6):
        failures.append("после stop комбинации не освободились")

    # build_bindings из конфига: имя команды -> строка
    over = build_bindings({"toggle": "Ctrl+Shift+K"})
    got = next(b for b in over.values() if b[2] == "toggle")
    print(f"build_bindings: toggle -> {got[3]}")
    if got[3] != "Ctrl+Shift+K":
        failures.append(f"переопределение из конфига не применилось: {got}")
    # Остальные должны остаться дефолтными
    if len(over) != len(DEFAULT_BINDINGS):
        failures.append("переопределение потеряло часть биндингов")

    if failures:
        for f in failures:
            print("ПРОВАЛ:", f)
        return 1
    print("OK: suspend/resume/rebind работают на живых хоткеях")
    return 0


if __name__ == "__main__":
    sys.exit(main())
