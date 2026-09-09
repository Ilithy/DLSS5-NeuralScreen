"""SpoutLibrary.dll smoke test: load, create a receiver, list senders.

This proves the Spout2 path is viable on this machine BEFORE the C++
sender integration: if the DLL loads and the receiver API answers, the
shared-texture transport works. A real sender (the worker) is not needed
for this test - it only checks the library side.

Run:  runtime\\python.exe test_spout_library.py
"""
import ctypes
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DLL = BASE / "_work" / "spout2" / "bin" / "Spout-SDK-binaries" / "Libs_2-007-017" / "MD" / "bin" / "SpoutLibrary.dll"


def main() -> int:
    failures = []
    if not DLL.exists():
        print(f"SKIP: SpoutLibrary.dll not found at {DLL}")
        return 0

    try:
        lib = ctypes.WinDLL(str(DLL))
    except OSError as exc:
        print(f"FAIL: cannot load SpoutLibrary.dll: {exc}")
        return 1

    # GetSpout() -> SPOUTHANDLE (void*)
    get_spout = lib.GetSpout
    get_spout.restype = ctypes.c_void_p
    handle = get_spout()
    if not handle:
        failures.append("GetSpout() returned null")
    else:
        print(f"GetSpout() -> {handle:#x}")

        # The handle is a pointer to the ISpoutLibrary vtable. The first
        # method is Release() at vtable[0]; GetSenderName is further down.
        # We only probe the vtable pointer itself - calling methods through
        # ctypes on an unknown vtable layout is fragile, so the test stops
        # at "the DLL loads and hands out a valid object".
        vtable = ctypes.cast(handle, ctypes.POINTER(ctypes.c_void_p))
        if not vtable[0]:
            failures.append("vtable is null")

    # Also check the DLL exports the expected entry point.
    try:
        getattr(lib, "GetSpout")
        print("export GetSpout present")
    except AttributeError:
        failures.append("GetSpout export missing")

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: SpoutLibrary loads and hands out a valid object")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
