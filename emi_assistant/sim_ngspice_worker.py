"""Run a KiCad-bundled ngspice shared library in a disposable child process.

No application state or arbitrary model/control text is passed into this worker.
The parent generates the passive netlist and enforces timeout/cancellation.
"""
from __future__ import annotations
import ctypes
import os
from pathlib import Path
import sys


def main():
    if len(sys.argv) != 4:
        raise SystemExit("Expected library, generated netlist, and result paths")
    library, source, destination = map(Path, sys.argv[1:])
    # Windows dependencies of KiCad's DLL live beside it or in the KiCad bin.
    handles = []
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        handles.append(os.add_dll_directory(str(library.parent.resolve())))
    dll = ctypes.CDLL(str(library.resolve()))
    send_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
    exit_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_bool, ctypes.c_bool, ctypes.c_int, ctypes.c_void_p)
    failed = []

    @send_type
    def send(message, identifier, user):
        print(message.decode("utf-8", "replace"), flush=True)
        return 0

    @exit_type
    def controlled_exit(status, immediate, quitexit, identifier, user):
        if status:
            failed.append(status)
        return 0

    dll.ngSpice_Init.argtypes = [ctypes.c_void_p] * 7
    dll.ngSpice_Init.restype = ctypes.c_int
    dll.ngSpice_Command.argtypes = [ctypes.c_char_p]
    dll.ngSpice_Command.restype = ctypes.c_int
    dll.ngSpice_Circ.argtypes = [ctypes.POINTER(ctypes.c_char_p)]
    dll.ngSpice_Circ.restype = ctypes.c_int
    if dll.ngSpice_Init(send, None, controlled_exit, None, None, None, None):
        raise RuntimeError("ngSpice_Init failed")
    lines = source.read_text(encoding="ascii").splitlines()
    # Defense in depth: no control/model/include directives are needed here.
    allowed = {".ac", ".tran", ".save", ".options", ".end"}
    for line in lines[1:]:
        if line.strip().startswith(".") and line.split()[0].lower() not in allowed:
            raise RuntimeError("Unexpected directive in generated circuit")
    encoded = [line.encode("ascii") for line in lines]
    array = (ctypes.c_char_p * (len(encoded) + 1))(*encoded, None)
    if dll.ngSpice_Circ(array):
        raise RuntimeError("ngSpice_Circ failed")
    for command in (b"set filetype=ascii", b"run", b"write result.raw v(base) v(candidate)"):
        if dll.ngSpice_Command(command):
            raise RuntimeError("ngSpice_Command failed")
    if failed or not destination.exists():
        raise RuntimeError("ngspice did not finish the AC simulation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
