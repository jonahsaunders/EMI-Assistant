"""First-launch setup using only Python's standard library.

KiCad registers the action with an empty requirements.txt. Actual application
dependencies are installed only after the user presses Analyze EMI, so a failed
download cannot hide the toolbar action. All installs stay in KiCad's existing
per-plugin virtual environment; the system interpreter is never modified.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback

ROOT = Path(__file__).resolve().parent.parent
CORE_MODULES = ("kipy", "PySide6.QtWidgets", "shapely")
_PROBE_SCRIPT = """import importlib, json
failures = {}
for name in json.loads(__import__('sys').argv[1]):
    try:
        importlib.import_module(name)
    except Exception as exc:
        failures[name] = type(exc).__name__ + ': ' + str(exc)
print(json.dumps(failures))
raise SystemExit(bool(failures))
"""


def _windows() -> bool:
    return sys.platform == "win32"


def dependency_modules(root: Path = ROOT) -> tuple[str, ...]:
    modules = list(CORE_MODULES)
    requirements = root / "runtime-requirements.txt"
    if requirements.is_file():
        lines = requirements.read_text(encoding="utf-8").splitlines()
        for package in ("numpy", "py7zr"):
            if any(line.strip().lower().startswith(package) for line in lines):
                modules.append(package)
    return tuple(modules)


def dependency_failures(root: Path = ROOT) -> dict[str, str]:
    """Probe in a child interpreter, including native DLL loads, without a UI."""
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", _PROBE_SCRIPT,
             json.dumps(dependency_modules(root))],
            cwd=str(root), env=os.environ.copy(), capture_output=True,
            text=True, timeout=60, check=False,
        )
        failures = json.loads(result.stdout.strip().splitlines()[-1])
        if not isinstance(failures, dict):
            raise ValueError("Unexpected import-check result")
        if result.returncode and not failures:
            raise ValueError("Import-check process did not complete successfully")
        return {str(name): str(error) for name, error in failures.items()}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        return {"Python environment": f"{type(exc).__name__}: {exc}"}


def managed_python() -> Path:
    """Return the console executable of this virtualenv, never global Python."""
    prefix = Path(sys.prefix).absolute()
    if (prefix.resolve() == Path(sys.base_prefix).resolve()
            or not (prefix / "pyvenv.cfg").is_file()):
        raise RuntimeError(
            "Automatic setup requires KiCad's private plugin environment. "
            "Open this plugin using Analyze EMI in KiCad; no global Python "
            "installation has been changed."
        )
    executable = prefix / ("Scripts/python.exe" if _windows() else "bin/python")
    if not executable.is_file():
        raise RuntimeError(
            "KiCad's plugin Python environment is incomplete. In PCB Editor "
            "Preferences > Plugins, right-click Analyze EMI and choose "
            "Recreate Plugin Environment, then click Analyze EMI again."
        )
    # Resolving bin/python would bypass a POSIX virtual environment.
    return executable


def _log_path() -> Path:
    try:
        prefix = Path(sys.prefix)
        if (prefix / "pyvenv.cfg").is_file():
            return prefix / "EMI-first-launch.log"
    except OSError:
        pass
    return Path(tempfile.gettempdir()) / f"EMI-first-launch-{os.getpid()}.log"


def _print(message: str) -> None:
    stream = sys.stdout or sys.stderr
    if stream is not None:
        try:
            print(message, file=stream, flush=True)
        except (OSError, ValueError):
            pass


def _failure(message: str, log_path: Path, *, native: bool = True) -> int:
    text = ("EMI Assistant could not finish starting.\n\n" + message
            + "\n\nDetails: " + str(log_path)
            + "\n\nThe Analyze EMI button will stay available. "
              "Check your internet connection and click it again to retry.")
    if sys.stderr is not None:
        try:
            print(text, file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass
    if _windows() and native:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, "EMI Assistant", 0x10)
            if log_path.is_file():
                os.startfile(str(log_path))
        except (AttributeError, OSError):
            pass
    return 2


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def setup_environment(root: Path, log) -> None:
    python = managed_python()
    requirements = root / "runtime-requirements.txt"
    if not requirements.is_file():
        raise RuntimeError("runtime-requirements.txt is missing. Reinstall the EMI Assistant package.")
    # Retain IPC socket/token, but reject pip settings that change the index,
    # disable TLS verification, or install outside this virtual environment.
    env = {name: value for name, value in os.environ.items()
           if not name.upper().startswith("PIP_")}
    env["PIP_CONFIG_FILE"] = os.devnull
    command = [
        str(python), "-I", "-m", "pip", "--disable-pip-version-check",
        "--require-virtualenv", "install", "--no-input",
        "--only-binary=:all:", "--index-url", "https://pypi.org/simple",
        "-r", str(requirements.resolve()),
    ]
    _print("Downloading and checking the application libraries. This may take a few minutes.")
    with subprocess.Popen(
        command, cwd=str(root), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
    ) as process:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                log.write(line)
                log.flush()
                _print(line.rstrip())
            returncode = process.wait()
        except BaseException:
            _stop_process(process)
            raise
    if returncode:
        raise RuntimeError(
            "The library download or installation failed. The setup log contains "
            "the pip error. Your PCB and other plugins have not been changed."
        )
    failures = dependency_failures(root)
    if failures:
        for module, error in failures.items():
            log.write(f"IMPORT FAILED: {module}: {error}\n")
        raise RuntimeError("The installed libraries did not pass their startup checks.")


def _start_ui(connect: bool) -> int:
    from .__main__ import main as application_main
    return application_main(["--connect"] if connect else [])


def _setup_console(root: Path, connect: bool) -> int:
    python = managed_python()
    command = [str(python), str((root / "launch.py").resolve()), "--setup"]
    if connect:
        command.append("--connect")
    with subprocess.Popen(
        command, cwd=str(root), env=os.environ.copy(), close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    ) as process:
        try:
            code = process.wait()
        except BaseException:
            _stop_process(process)
            raise
    if code:
        return _failure("First-launch preparation did not complete.", _log_path(), native=False)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Start EMI Assistant")
    parser.add_argument("--setup", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--connect", action="store_true", default=True)
    args = parser.parse_args(argv)
    root = ROOT
    log_path = _log_path()
    try:
        failures = dependency_failures(root)
        if not failures:
            return _start_ui(args.connect)
        if _windows() and not args.setup:
            return _setup_console(root, args.connect)
        _print("Preparing EMI Assistant for its first launch...")
        _print("This installs application libraries only in EMI Assistant's private environment.")
        _print(f"Setup log: {log_path}")
        with log_path.open("w", encoding="utf-8", buffering=1) as log:
            log.write("EMI Assistant first-launch setup\n")
            for module, error in failures.items():
                log.write(f"Initial check: {module}: {error}\n")
            try:
                setup_environment(root, log)
            except BaseException:
                traceback.print_exc(file=log)
                raise
        _print("Ready. Opening EMI Assistant...")
        if _windows() and args.setup:
            # Detach only the console this launcher created. Keep subsequent
            # application diagnostics in the log when the console disappears.
            import ctypes
            ctypes.windll.kernel32.FreeConsole()
            with log_path.open("a", encoding="utf-8", buffering=1) as log:
                old_out, old_err = sys.stdout, sys.stderr
                try:
                    sys.stdout = sys.stderr = log
                    return _start_ui(args.connect)
                finally:
                    sys.stdout, sys.stderr = old_out, old_err
        return _start_ui(args.connect)
    except (Exception, KeyboardInterrupt) as exc:
        try:
            with log_path.open("a", encoding="utf-8") as log:
                traceback.print_exc(file=log)
        except OSError:
            pass
        return _failure(str(exc) or "Setup was cancelled.", log_path)


if __name__ == "__main__":
    raise SystemExit(main())
