"""Discover solvers and provision verified, private Windows portable runtimes.

No downloads, DLL loading, or process execution happen during module import.
The app never changes PATH, the registry, KiCad settings, or system packages.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request
import zipfile

# These exact official release archives were retrieved and SHA-256 checked when
# preparing 0.2.0. URLs are immutable release paths, never "latest" redirects.
DOWNLOADS = {
    "ngspice": {
        "version": "47", "archive": "7z", "size": 13814879,
        "url": "https://downloads.sourceforge.net/project/ngspice/ng-spice-rework/47/ngspice-47_64.7z",
        "sha256": "59225971bd68cdd1199443649aa4615a9e6d684933f205ab49006a3942518f5a",
        "executable": "Spice64/bin/ngspice_con.exe",
        "source": "https://sourceforge.net/projects/ngspice/files/ng-spice-rework/47/",
        "license": "https://sourceforge.net/p/ngspice/ngspice/ci/ngspice-47/tree/COPYING",
    },
    "openems": {
        "version": "0.0.36", "archive": "zip", "size": 50805808,
        "url": "https://github.com/thliebig/openEMS-Project/releases/download/v0.0.36/openEMS_v0.0.36.zip",
        "sha256": "e0d62b1176c0897ad18876b45667de877d7d3b58b37c0be95545f9b988896059",
        "executable": "openEMS/openEMS.exe",
        "source": "https://github.com/thliebig/openEMS-Project/releases/tag/v0.0.36",
        "license": "https://github.com/thliebig/openEMS/blob/master/COPYING",
    },
}
_INSTALL_LOCK = threading.Lock()
MAX_UNPACKED = 512 * 1024 * 1024
MAX_FILES = 15000


class SetupCancelled(RuntimeError):
    pass


def _cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise SetupCancelled("Simulation setup cancelled. Click Analyze EMI to try again.")


def _notify(progress, message):
    if progress:
        progress(message)


def cache_directory() -> Path:
    custom = os.environ.get("EMI_SOLVER_CACHE")
    shared_cache = os.environ.get("EMI_ASSISTANT_CACHE_DIR")
    if shared_cache and not custom:
        return Path(shared_cache).expanduser() / "solvers"
    if custom:
        return Path(custom).expanduser()
    if platform.system() == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "EMI-Assistant" / "solvers"
    if platform.system() == "Darwin":
        return Path.home() / "Library/Caches/EMI Assistant/solvers"
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "emi-assistant/solvers"


def _missing(message):
    return {"available": False, "path": "", "kind": "executable", "version": "", "message": message}


def _digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _python_console():
    candidate = Path(sys.executable).with_name("python.exe")
    return str(candidate) if platform.system() == "Windows" and candidate.is_file() else sys.executable


def _probe(path, engine, kind="executable", version=""):
    path = Path(path)
    if not path.is_file():
        return _missing(f"{engine} was not found at {path}.")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if platform.system() == "Windows" else 0
    try:
        if kind == "shared":
            # Probe in an isolated process: a broken DLL cannot crash the GUI.
            script = (
                "import ctypes,os,sys; p=sys.argv[1]; "
                "d=os.add_dll_directory(os.path.dirname(p)) if hasattr(os,'add_dll_directory') else None; "
                "lib=ctypes.CDLL(p); "
                "assert all(hasattr(lib,n) for n in ('ngSpice_Init','ngSpice_Command','ngSpice_Circ')); "
                "print('ngspice shared library')"
            )
            command = [_python_console(), "-I", "-c", script, str(path.resolve())]
        else:
            command = [str(path.resolve())] if engine == "openems" else [str(path.resolve()), "--version"]
        run = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=8, check=False,
                             creationflags=creationflags, cwd=str(path.parent))
        output = run.stdout.decode("utf-8", errors="replace")[-12000:]
        marker = "openems" if engine == "openems" else "ngspice" if engine == "ngspice" else ""
        if (kind == "shared" and run.returncode != 0) or (marker and marker not in output.lower()):
            return _missing(f"{engine} could not start: {output.strip()[-1000:] or 'no version response'}")
        if engine == "kicad_cli" and run.returncode != 0:
            return _missing(f"KiCad circuit exporter could not start: {output.strip()[-1000:]}")
        match = re.search(r"(?:ngspice[- ]*|openEMS(?:\s+\d+bit)?\s*(?:--\s*)?(?:version\s*)?v?)(\d+(?:\.\d+)*(?:[-+][\w.]+)?)", output, re.I)
        # openEMS 0.0.35/0.0.36 have no --version flag; invoking without
        # an XML file prints the version and usage, then returns -1 / 255.
        valid_return = run.returncode == 0 or (engine == "openems" and "usage:" in output.lower())
        if kind == "executable" and engine in DOWNLOADS and (match is None or not valid_return):
            return _missing(f"{engine} did not report a usable solver version: {output.strip()[-1000:]}")
        identity = _digest(path)
        resolved_version = version or (match.group(1) if match else output.strip().splitlines()[0] if output.strip() else "unknown")
        return {"available": True, "path": str(path.resolve()), "kind": kind,
                "version": resolved_version, "identity": identity, "message": "Ready"}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return _missing(f"{engine} could not start: {exc}")


def _kicad_directories():
    paths = [Path(sys.base_prefix), Path(getattr(sys, "_base_executable", sys.executable)).parent,
             Path(sys.executable).parent]
    if platform.system() == "Windows":
        for env_name in ("ProgramW6432", "ProgramFiles"):
            root = Path(os.environ.get(env_name, r"C:\Program Files")) / "KiCad"
            if root.is_dir():
                paths.extend(sorted(root.glob("*/bin"), reverse=True))
    elif platform.system() == "Darwin":
        paths.append(Path("/Applications/KiCad/KiCad.app/Contents/MacOS"))
    return list(dict.fromkeys(paths))


def _cached(engine):
    spec = DOWNLOADS[engine]
    root = cache_directory() / f"{engine}-{spec['version']}"
    marker = root / "emi-runtime.json"
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
        if record.get("archive_sha256") != spec["sha256"]:
            return None
        exe = root / spec["executable"]
        if record.get("executable_sha256") != _digest(exe):
            return None
        return exe
    except (OSError, ValueError, TypeError):
        return None


def discover() -> dict:
    """Find installed working engines without changing files or using the network."""
    kicad_dirs = _kicad_directories()
    inventory = {}
    for engine, names, override in (
        ("kicad_cli", ["kicad-cli.exe", "kicad-cli"], "EMI_KICAD_CLI"),
        ("ngspice", ["ngspice_con.exe", "ngspice", "ngspice.exe"], "EMI_NGSPICE"),
        ("openems", ["openEMS.exe", "openEMS", "openems"], "EMI_OPENEMS"),
    ):
        candidates = []
        custom = os.environ.get(override)
        if custom:
            kind = "shared" if engine == "ngspice" and (custom.lower().endswith((".dll", ".dylib")) or ".so" in Path(custom).name) else "executable"
            candidates.append((Path(custom), kind))
        if engine in DOWNLOADS:
            cached = _cached(engine)
            if cached:
                candidates.append((cached, "executable"))
        for name in names:
            found = shutil.which(name)
            if found:
                candidates.append((Path(found), "executable"))
            if engine != "openems":
                candidates.extend((directory / name, "executable") for directory in kicad_dirs)
        if engine == "ngspice" and platform.system() == "Windows":
            for directory in kicad_dirs:
                candidates.extend((directory / name, "shared") for name in ("ngspice.dll", "ngspice-0.dll", "libngspice-0.dll", "libngspice.dll"))
        last_error = None
        for path, kind in dict.fromkeys(candidates):
            if not path.is_file():
                continue
            result = _probe(path, engine, kind)
            if result["available"]:
                inventory[engine] = result
                break
            last_error = result
        else:
            inventory[engine] = last_error or _missing(f"{engine} is not installed or could not be found.")
    return inventory


class _HTTPSRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise ValueError("Refusing a solver download redirect without HTTPS.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(spec, destination, progress=None, cancel_event=None):
    if urlsplit(spec["url"]).scheme != "https":
        raise ValueError("Solver downloads require HTTPS.")
    expected = int(spec["size"])
    deadline = time.monotonic() + 300
    digest = hashlib.sha256()
    received = 0
    next_report = 0
    request = Request(spec["url"], headers={"User-Agent": "EMI-Assistant/0.2 solver-setup"})
    opener = build_opener(_HTTPSRedirects())
    with opener.open(request, timeout=15) as response, Path(destination).open("wb") as output:
        if urlsplit(response.geturl()).scheme != "https":
            raise ValueError("Solver download did not use HTTPS.")
        length = response.headers.get("Content-Length")
        if length and int(length) != expected:
            raise ValueError("The solver archive size changed; this release needs a verified package update.")
        while True:
            _cancel(cancel_event)
            if time.monotonic() > deadline:
                raise TimeoutError("Solver download timed out. Check the internet connection and analyze again.")
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            received += len(chunk)
            if received > expected:
                raise ValueError("Solver download exceeded its verified size.")
            digest.update(chunk)
            output.write(chunk)
            percent = received * 100 // expected
            if percent >= next_report:
                _notify(progress, f"Downloading simulation engine: {percent}% ({received / 1048576:.1f} MB)")
                next_report = percent + 5
    if received != expected or digest.hexdigest() != spec["sha256"]:
        raise ValueError("Solver download verification failed. The incomplete or changed archive was not installed.")


def _safe_member(name):
    # Enforce Windows-safe paths even when validating archives on Linux.
    normal = name.replace("\\", "/")
    path = PurePosixPath(normal)
    if not normal or path.is_absolute() or normal.startswith("/"):
        raise ValueError("Unsafe absolute path in solver archive.")
    for part in path.parts:
        if part in ("..", ".") or ":" in part or part.endswith((" ", ".")):
            raise ValueError("Unsafe path in solver archive.")
        if part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            raise ValueError("Reserved device path in solver archive.")
    return path


def _extract(archive, destination, kind, cancel_event=None):
    destination = Path(destination)
    if kind == "zip":
        with zipfile.ZipFile(archive) as source:
            files = source.infolist()
            if len(files) > MAX_FILES or sum(f.file_size for f in files) > MAX_UNPACKED:
                raise ValueError("Solver archive exceeds extraction limits.")
            seen = set()
            for entry in files:
                path = _safe_member(entry.filename)
                key = str(path).casefold()
                if key in seen:
                    raise ValueError("Duplicate path in solver archive.")
                seen.add(key)
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                    raise ValueError("Links and special files are not allowed in solver archives.")
                _cancel(cancel_event)
                target = destination.joinpath(*path.parts)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as inp, target.open("xb") as out:
                    while chunk := inp.read(256 * 1024):
                        _cancel(cancel_event)
                        out.write(chunk)
    elif kind == "7z":
        try:
            import py7zr
        except ImportError as exc:
            raise RuntimeError("The ngspice downloader needs py7zr. Recreate EMI Assistant's plugin environment in KiCad Preferences → PCB Editor → Plugins.") from exc
        with py7zr.SevenZipFile(archive, mode="r") as source:
            files = source.list()
            if len(files) > MAX_FILES or sum(f.uncompressed or 0 for f in files) > MAX_UNPACKED:
                raise ValueError("Solver archive exceeds extraction limits.")
            seen = set()
            for entry in files:
                path = _safe_member(entry.filename)
                key = str(path).casefold()
                if key in seen:
                    raise ValueError("Duplicate path in solver archive.")
                seen.add(key)
                if entry.is_symlink or not (entry.is_file or entry.is_directory):
                    raise ValueError("Links and special files are not allowed in solver archives.")
            _cancel(cancel_event)
            source.extractall(path=destination)
            _cancel(cancel_event)
        # The checked official archive is small; still verify the extracted tree.
        for path in destination.rglob("*"):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError("Unsafe extracted file.")
    else:
        raise ValueError("Unsupported solver archive format.")


def _install(engine, progress=None, cancel_event=None):
    spec = DOWNLOADS[engine]
    root = cache_directory()
    root.mkdir(parents=True, exist_ok=True)
    final = root / f"{engine}-{spec['version']}"
    # A failed/cancelled download never leaves a partially installed runtime.
    with tempfile.TemporaryDirectory(prefix=f".{engine}-setup-", dir=root) as temporary:
        stage = Path(temporary)
        archive = stage / f"download.{spec['archive']}"
        payload = stage / "payload"
        payload.mkdir()
        _notify(progress, f"First-time setup: downloading {engine} {spec['version']} from its official release ({spec['size'] / 1048576:.1f} MB).")
        _download(spec, archive, progress, cancel_event)
        _notify(progress, f"Verifying and unpacking {engine}…")
        _extract(archive, payload, spec["archive"], cancel_event)
        executable = payload / spec["executable"]
        result = _probe(executable, engine, version=spec["version"])
        if not result["available"]:
            raise RuntimeError(result["message"])
        record = {"engine": engine, "version": spec["version"], "archive_sha256": spec["sha256"],
                  "executable_sha256": _digest(executable), "source": spec["source"], "license": spec["license"]}
        (payload / "emi-runtime.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        _cancel(cancel_event)
        if final.exists():
            # Preserve any prior files; replace an incomplete managed install by
            # moving it aside within our cache only, then clean it after commit.
            backup = stage / "previous"
            final.rename(backup)
            try:
                payload.rename(final)
            except OSError:
                backup.rename(final)
                raise
        else:
            payload.rename(final)
        result["path"] = str((final / spec["executable"]).resolve())
        _notify(progress, f"{engine} is ready. Future analyses reuse this installation.")
        return result


def ensure(progress=None, cancel_event=None, engines=None) -> dict:
    """Find engines; on Windows x64 install missing official portable releases.

    Normal missing engines, offline operation and cancellation are returned as
    readable per-engine states so layout checks and the other solver continue.
    """
    inventory = discover()
    enabled = set(DOWNLOADS if engines is None else engines)
    for engine in DOWNLOADS:
        if engine not in enabled or inventory[engine]["available"]:
            continue
        try:
            _cancel(cancel_event)
            if platform.system() != "Windows":
                inventory[engine]["message"] += " Install this engine using your platform's package manager, then Analyze EMI again."
                continue
            if platform.machine().lower() not in ("amd64", "x86_64"):
                inventory[engine]["message"] += " Automatic portable setup supports Windows x64."
                continue
            while not _INSTALL_LOCK.acquire(timeout=0.1):
                _cancel(cancel_event)
            try:
                cached = _cached(engine)
                if cached:
                    result = _probe(cached, engine, version=DOWNLOADS[engine]["version"])
                    if result["available"]:
                        inventory[engine] = result
                        continue
                inventory[engine] = _install(engine, progress, cancel_event)
            finally:
                _INSTALL_LOCK.release()
        except SetupCancelled as exc:
            inventory[engine] = _missing(str(exc))
            inventory[engine]["cancelled"] = True
        except Exception as exc:
            inventory[engine] = _missing(f"{engine} setup could not finish: {exc} Click Analyze EMI to retry. Layout checks remain available.")
            _notify(progress, inventory[engine]["message"])
    return inventory
