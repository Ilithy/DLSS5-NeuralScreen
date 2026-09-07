"""Проверка механизма, которым DLSSNR узнаёт архитектуру GPU.

Библиотека NVIDIA грузит nvapi64.dll, берёт единственный экспорт
nvapi_QueryInterface и запрашивает по числовому id функции по именам
(NvAPI_EnumPhysicalGPUs, NvAPI_GPU_GetArchInfo — оба имени лежат строками
в nvngx_dlssnr.dll). Здесь мы делаем то же самое сами: если получится
вызвать GetArchInfo и получить осмысленную архитектуру, значит прокси-
библиотека с подменой этого одного ответа — рабочий путь.

Запуск:  runtime\\python.exe _probe_nvapi_arch.py
"""
import ctypes
import sys
from ctypes import wintypes

# id функций NvAPI — это хеши их имён; значения общеизвестны из
# реверс-инженерных заголовков nvapi. Если какой-то вернёт NULL, значит
# конкретный id не тот, и это будет видно сразу.
IDS = {
    "NvAPI_Initialize": 0x0150E828,
    "NvAPI_Unload": 0xD22BDD7E,
    "NvAPI_EnumPhysicalGPUs": 0xE5AC921F,
    "NvAPI_GPU_GetArchInfo": 0xD8265D24,
    "NvAPI_GPU_GetFullName": 0xCEEE8E9F,
    "NvAPI_SYS_GetDriverAndBranchVersion": 0x2926AAAD,
}

# NV_GPU_ARCHITECTURE_ID — старший байт группы; значения из тех же заголовков
ARCH_NAMES = {
    0x100: "T30", 0x110: "T40",
    0x120: "Kepler GK100", 0x130: "Maxwell GM000", 0x140: "Maxwell GM200",
    0x150: "Pascal GP100", 0x160: "Volta GV000", 0x170: "Turing TU100",
    0x180: "Ampere GA100", 0x190: "Ada AD100", 0x1A0: "Hopper GH100",
    0x1B0: "Blackwell GB100", 0x1C0: "Blackwell GB200",
}


class ArchInfo(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32),
                ("architecture", ctypes.c_uint32),
                ("implementation", ctypes.c_uint32),
                ("revision", ctypes.c_uint32)]


def main() -> int:
    try:
        nvapi = ctypes.WinDLL("nvapi64.dll")
    except OSError as exc:
        print(f"ПРОВАЛ: nvapi64.dll не загрузилась: {exc}")
        return 1
    qi = nvapi.nvapi_QueryInterface
    qi.restype = ctypes.c_void_p
    qi.argtypes = [ctypes.c_uint32]

    ptrs = {}
    for name, ident in IDS.items():
        p = qi(ident)
        ptrs[name] = p
        print(f"  {name:<36} id 0x{ident:08X} -> {'0x%X' % p if p else 'NULL'}")

    if not ptrs["NvAPI_Initialize"] or not ptrs["NvAPI_EnumPhysicalGPUs"]:
        print("ПРОВАЛ: базовые функции не разрешились — id не те")
        return 1

    init = ctypes.CFUNCTYPE(ctypes.c_int)(ptrs["NvAPI_Initialize"])
    rc = init()
    print(f"NvAPI_Initialize -> {rc}")
    if rc != 0:
        print("ПРОВАЛ: NvAPI не инициализировался")
        return 1

    handles = (ctypes.c_void_p * 64)()
    count = ctypes.c_uint32(0)
    enum = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_void_p),
                            ctypes.POINTER(ctypes.c_uint32))(
        ptrs["NvAPI_EnumPhysicalGPUs"])
    rc = enum(handles, ctypes.byref(count))
    print(f"NvAPI_EnumPhysicalGPUs -> {rc},GPU: {count.value}")
    if rc != 0 or count.value == 0:
        print("ПРОВАЛ: список GPU не получен")
        return 1

    if ptrs["NvAPI_GPU_GetFullName"]:
        name_buf = ctypes.create_string_buffer(64)
        full = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_char_p)(
            ptrs["NvAPI_GPU_GetFullName"])
        if full(handles[0], name_buf) == 0:
            print(f"GPU: {name_buf.value.decode('ascii', 'replace')}")

    if not ptrs["NvAPI_GPU_GetArchInfo"]:
        print("ПРОВАЛ: NvAPI_GPU_GetArchInfo не разрешился — нужен другой id")
        return 1

    get_arch = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                ctypes.POINTER(ArchInfo))(
        ptrs["NvAPI_GPU_GetArchInfo"])
    # version = (sizeof | версия << 16); NV_GPU_ARCH_INFO_VER_2 = 2
    for ver in (2, 1):
        info = ArchInfo()
        info.version = ctypes.sizeof(ArchInfo) | (ver << 16)
        rc = get_arch(handles[0], ctypes.byref(info))
        print(f"NvAPI_GPU_GetArchInfo (ver {ver}) -> {rc}")
        if rc == 0:
            group = info.architecture & 0xFFFFFFF0
            print(f"  architecture   0x{info.architecture:X} "
                  f"({ARCH_NAMES.get(group, 'неизвестно')})")
            print(f"  implementation 0x{info.implementation:X}")
            print(f"  revision       0x{info.revision:X}")
            print("\nOK: механизм рабочий — архитектуру отдаёт именно эта функция,")
            print("    значит прокси-nvapi с подменой одного ответа сработает.")
            return 0
    print("ПРОВАЛ: GetArchInfo не вернул успех ни на одной версии структуры")
    return 1


if __name__ == "__main__":
    sys.exit(main())
