"""First-click setup remains visible, private, and retryable after failure."""
from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from emi_assistant import bootstrap


def _process(returncode=0, output="Installed libraries\n"):
    process = MagicMock()
    process.__enter__.return_value = process
    process.stdout = io.StringIO(output)
    process.wait.return_value = returncode
    process.poll.return_value = returncode
    return process


def test_entrypoint_imports_without_site_packages():
    launch = bootstrap.ROOT / "launch.py"
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c",
         "import runpy, sys; runpy.run_path(sys.argv[1], run_name='entrypoint_import_check')", str(launch)],
        cwd=str(launch.parent.parent), capture_output=True, text=True,
        timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_probe_catches_native_library_import_failure_and_preserves_ipc_environment():
    failed = {"PySide6.QtWidgets": "ImportError: DLL load failed"}
    result = subprocess.CompletedProcess([], 1, json.dumps(failed), "")
    with patch.object(bootstrap.subprocess, "run", return_value=result) as run, \
            patch.dict(bootstrap.os.environ, {"KICAD_API_TOKEN": "test-token", "KICAD_API_SOCKET": "test-socket"}):
        assert bootstrap.dependency_failures() == failed
    arguments, options = run.call_args
    assert arguments[0][:2] == [sys.executable, "-I"]
    assert options["env"]["KICAD_API_TOKEN"] == "test-token"
    assert options["env"]["KICAD_API_SOCKET"] == "test-socket"


@pytest.mark.parametrize("output,code", [("{}", 5), ("", 1), ("[]", 0)])
def test_probe_does_not_treat_crash_or_malformed_output_as_success(output, code):
    with patch.object(bootstrap.subprocess, "run", return_value=subprocess.CompletedProcess([], code, output, "")):
        assert "Python environment" in bootstrap.dependency_failures()


def test_managed_python_refuses_global_python_even_with_environment_marker(tmp_path):
    (tmp_path / "pyvenv.cfg").write_text("home = global\n")
    with patch.object(sys, "prefix", str(tmp_path)), patch.object(sys, "base_prefix", str(tmp_path)):
        with pytest.raises(RuntimeError, match="private plugin environment"):
            bootstrap.managed_python()


def test_managed_python_selects_console_executable_in_same_windows_virtualenv(tmp_path):
    (tmp_path / "pyvenv.cfg").write_text("home = global\n")
    (tmp_path / "Scripts").mkdir()
    expected = tmp_path / "Scripts" / "python.exe"
    expected.touch()
    with patch.object(sys, "prefix", str(tmp_path)), \
            patch.object(sys, "base_prefix", str(tmp_path / "global")), \
            patch.object(bootstrap, "_windows", return_value=True):
        assert bootstrap.managed_python() == expected


def test_pip_runs_only_in_private_environment_with_tls_and_inherited_ipc(tmp_path):
    (tmp_path / "runtime-requirements.txt").write_text("kicad-python==0.8.0\n")
    python = tmp_path / "managed" / "Scripts" / "python.exe"
    with patch.object(bootstrap, "managed_python", return_value=python), \
            patch.object(bootstrap.subprocess, "Popen", return_value=_process()) as popen, \
            patch.object(bootstrap, "dependency_failures", return_value={}) as probe, \
            patch.dict(bootstrap.os.environ, {"KICAD_API_TOKEN": "test-token", "PIP_TARGET": "unsafe", "PIP_TRUSTED_HOST": "unsafe", "PIP_INDEX_URL": "http://unsafe"}):
        bootstrap.setup_environment(tmp_path, io.StringIO())
    command = popen.call_args.args[0]
    env = popen.call_args.kwargs["env"]
    assert command[:4] == [str(python), "-I", "-m", "pip"]
    assert "--require-virtualenv" in command
    assert "--only-binary=:all:" in command
    assert command[command.index("--index-url") + 1] == "https://pypi.org/simple"
    assert command[-1] == str(tmp_path / "runtime-requirements.txt")
    assert env["KICAD_API_TOKEN"] == "test-token"
    assert "PIP_TARGET" not in env
    assert "PIP_TRUSTED_HOST" not in env
    assert "PIP_INDEX_URL" not in env
    assert env["PIP_CONFIG_FILE"] == bootstrap.os.devnull
    probe.assert_called_once_with(tmp_path)


def test_pip_failure_stops_before_success_check(tmp_path):
    (tmp_path / "runtime-requirements.txt").write_text("kicad-python==0.8.0\n")
    with patch.object(bootstrap, "managed_python", return_value=tmp_path / "python"), \
            patch.object(bootstrap.subprocess, "Popen", return_value=_process(1, "Download failed\n")), \
            patch.object(bootstrap, "dependency_failures") as probe:
        with pytest.raises(RuntimeError, match="download or installation failed"):
            bootstrap.setup_environment(tmp_path, io.StringIO())
        probe.assert_not_called()


def test_successful_pip_exit_still_requires_successful_imports(tmp_path):
    (tmp_path / "runtime-requirements.txt").write_text("kicad-python==0.8.0\n")
    log = io.StringIO()
    with patch.object(bootstrap, "managed_python", return_value=tmp_path / "python"), \
            patch.object(bootstrap.subprocess, "Popen", return_value=_process()), \
            patch.object(bootstrap, "dependency_failures", return_value={"kipy": "ModuleNotFoundError"}):
        with pytest.raises(RuntimeError, match="startup checks"):
            bootstrap.setup_environment(tmp_path, log)
    assert "IMPORT FAILED: kipy" in log.getvalue()


def test_installed_dependencies_open_ui_offline_without_setup():
    with patch.object(bootstrap, "dependency_failures", return_value={}), \
            patch.object(bootstrap, "_start_ui", return_value=0) as start, \
            patch.object(bootstrap, "setup_environment") as setup, \
            patch.object(bootstrap, "_setup_console") as console:
        assert bootstrap.main([]) == 0
    start.assert_called_once_with(True)
    setup.assert_not_called()
    console.assert_not_called()


def test_windows_missing_dependencies_start_visible_console_once():
    with patch.object(bootstrap, "dependency_failures", return_value={"kipy": "missing"}), \
            patch.object(bootstrap, "_windows", return_value=True), \
            patch.object(bootstrap, "_setup_console", return_value=0) as console, \
            patch.object(bootstrap, "_start_ui") as start:
        assert bootstrap.main([]) == 0
    console.assert_called_once_with(bootstrap.ROOT, True)
    start.assert_not_called()


def test_console_runs_absolute_entrypoint_and_retains_ipc(tmp_path):
    python = tmp_path / "Scripts" / "python.exe"
    with patch.object(bootstrap, "managed_python", return_value=python), \
            patch.object(bootstrap.subprocess, "Popen", return_value=_process()) as popen, \
            patch.dict(bootstrap.os.environ, {"KICAD_API_TOKEN": "test-token"}):
        assert bootstrap._setup_console(tmp_path, True) == 0
    assert popen.call_args.args[0] == [str(python), str(tmp_path / "launch.py"), "--setup", "--connect"]
    assert popen.call_args.kwargs["env"]["KICAD_API_TOKEN"] == "test-token"
    assert popen.call_args.kwargs["cwd"] == str(tmp_path)


def test_failed_first_setup_keeps_diagnostic_and_does_not_open_ui(tmp_path):
    logfile = tmp_path / "startup.log"
    with patch.object(bootstrap, "dependency_failures", return_value={"kipy": "missing"}), \
            patch.object(bootstrap, "_windows", return_value=False), \
            patch.object(bootstrap, "_log_path", return_value=logfile), \
            patch.object(bootstrap, "setup_environment", side_effect=RuntimeError("Connection failed")), \
            patch.object(bootstrap, "_start_ui") as start:
        assert bootstrap.main(["--setup"]) == 2
    start.assert_not_called()
    assert "Connection failed" in logfile.read_text()


def test_archive_dependency_is_checked_when_declared(tmp_path):
    (tmp_path / "runtime-requirements.txt").write_text("# engine extraction\npy7zr>=1,<2\nnumpy>=1.26,<3\n")
    assert "py7zr" in bootstrap.dependency_modules(tmp_path)
    assert "numpy" in bootstrap.dependency_modules(tmp_path)
