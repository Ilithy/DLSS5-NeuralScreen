"""Модель GPU и его архитектура — через nvapi, без внешних процессов.

Нужно интерфейсу: показать, на чём мы работаем, и поддерживается ли
Neural Rendering. Архитектура берётся тем же путём, каким её узнаёт сама
библиотека NVIDIA (nvapi_QueryInterface -> NvAPI_GPU_GetArchInfo), поэтому
значение совпадает с тем, по которому она принимает решение.

Всё завёрнуто в try: без nvapi (не-NVIDIA машина, обрезанный драйвер)
модуль просто вернёт пустые поля, а не уронит программу.
"""
from __future__ import annotations

import ctypes

# id функций nvapi — хеши их имён
_ID_INITIALIZE = 0x0150E828
_ID_ENUM_GPUS = 0xE5AC921F
_ID_GET_ARCH = 0xD8265D24
_ID_GET_NAME = 0xCEEE8E9F

# NV_GPU_ARCHITECTURE_ID: группа в старших разрядах. Neural Rendering
# (feature 18) официально живёт только на Blackwell — см. NGXGpuArchitecture
# в самой nvngx_dlssnr.dll.
ARCH_NAMES = {
    0x170: ("Turing", "20xx"),
    0x180: ("Ampere", "30xx"),
    0x190: ("Ada", "40xx"),
    0x1A0: ("Hopper", ""),
    0x1B0: ("Blackwell", "50xx"),
    0x1C0: ("Blackwell", "50xx"),
}
ARCH_BLACKWELL = 0x1B0


class _ArchInfo(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32),
                ("architecture", ctypes.c_uint32),
                ("implementation", ctypes.c_uint32),
                ("revision", ctypes.c_uint32)]


def probe() -> dict:
    """{name, arch, arch_group, family, official} — пустые поля при неудаче."""
    out = {"name": "", "arch": "", "arch_group": 0, "family": "",
           "official": False}
    try:
        nvapi = ctypes.WinDLL("nvapi64.dll")
        qi = nvapi.nvapi_QueryInterface
        qi.restype = ctypes.c_void_p
        qi.argtypes = [ctypes.c_uint32]

        p_init, p_enum = qi(_ID_INITIALIZE), qi(_ID_ENUM_GPUS)
        if not p_init or not p_enum:
            return out
        if ctypes.CFUNCTYPE(ctypes.c_int)(p_init)() != 0:
            return out

        handles = (ctypes.c_void_p * 64)()
        count = ctypes.c_uint32(0)
        enum = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_void_p),
                                ctypes.POINTER(ctypes.c_uint32))(p_enum)
        if enum(handles, ctypes.byref(count)) != 0 or count.value == 0:
            return out
        gpu = handles[0]

        p_name = qi(_ID_GET_NAME)
        if p_name:
            buf = ctypes.create_string_buffer(64)
            fn = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                  ctypes.c_char_p)(p_name)
            if fn(gpu, buf) == 0:
                # «NVIDIA GeForce RTX 5070 Ti» -> «RTX 5070 Ti»: в строку меню
                # длинное имя не влезает, а вендор там и не нужен.
                name = buf.value.decode("ascii", "replace").strip()
                for prefix in ("NVIDIA GeForce ", "NVIDIA "):
                    if name.startswith(prefix):
                        name = name[len(prefix):]
                        break
                out["name"] = name

        p_arch = qi(_ID_GET_ARCH)
        if p_arch:
            fn = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                  ctypes.POINTER(_ArchInfo))(p_arch)
            for ver in (2, 1):
                info = _ArchInfo()
                info.version = ctypes.sizeof(_ArchInfo) | (ver << 16)
                if fn(gpu, ctypes.byref(info)) == 0:
                    group = info.architecture & 0xFFFFFFF0
                    arch, family = ARCH_NAMES.get(group, ("", ""))
                    out["arch_group"] = group
                    out["arch"] = arch
                    out["family"] = family
                    out["official"] = group >= ARCH_BLACKWELL
                    break
    except Exception:
        return out
    return out


def describe(info: dict) -> str:
    """Строка для меню: «RTX 5070 Ti · Blackwell»."""
    name = info.get("name") or "GPU неизвестен"
    arch = info.get("arch")
    return f"{name} · {arch}" if arch else name


if __name__ == "__main__":
    got = probe()
    print(describe(got))
    print(f"группа 0x{got['arch_group']:X}, официально поддерживается: "
          f"{'да' if got['official'] else 'нет'}")
