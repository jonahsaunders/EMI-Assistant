import hashlib
import io
import json
from pathlib import Path
import stat
import threading
import zipfile

import pytest

from emi_assistant import simulation_runtime as runtime


def _inventory():
    return {key: runtime._missing("Not found") for key in ("ngspice", "openems", "kicad_cli")}


@pytest.mark.parametrize("name", ["../escape.exe", "/absolute.exe", r"C:\escape.exe", "x/../../escape", "x/file:stream", "x/NUL.txt", "x/trailing."])
def test_archive_paths_cannot_escape_or_create_windows_devices(tmp_path, name):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as source:
        source.writestr(name, "bad")
    with pytest.raises(ValueError):
        runtime._extract(archive, tmp_path / "out", "zip")
    assert not (tmp_path / "escape.exe").exists()


def test_zip_rejects_symlink_and_duplicate_case_paths(tmp_path):
    archive = tmp_path / "bad.zip"
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as source:
        source.writestr(link, "../outside")
    with pytest.raises(ValueError, match="Links"):
        runtime._extract(archive, tmp_path / "out", "zip")
    with zipfile.ZipFile(archive, "w") as source:
        source.writestr("Engine/a", "one")
        source.writestr("engine/A", "two")
    with pytest.raises(ValueError, match="Duplicate"):
        runtime._extract(archive, tmp_path / "out2", "zip")


def test_extract_regular_tree_and_cancel(tmp_path):
    archive = tmp_path / "good.zip"
    with zipfile.ZipFile(archive, "w") as source:
        source.writestr("engine/bin/solver.exe", b"contents")
    output = tmp_path / "out"
    runtime._extract(archive, output, "zip")
    assert (output / "engine/bin/solver.exe").read_bytes() == b"contents"
    event = threading.Event()
    event.set()
    with pytest.raises(runtime.SetupCancelled):
        runtime._extract(archive, tmp_path / "cancelled", "zip", event)


class _Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def geturl(self):
        return "https://release-assets.githubusercontent.com/verified-asset"


def test_downloader_rejects_changed_bytes_and_uses_verified_tls(tmp_path, monkeypatch):
    data = b"official contents"
    spec = {"url": "https://github.com/project/releases/download/v1/a.zip", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    class Opener:
        def open(self, request, timeout):
            assert request.full_url.startswith("https:")
            return _Response(data)
    monkeypatch.setattr(runtime, "build_opener", lambda *_: Opener())
    runtime._download(spec, tmp_path / "good.zip")
    assert (tmp_path / "good.zip").read_bytes() == data
    with pytest.raises(ValueError, match="verification"):
        runtime._download({**spec, "sha256": "0" * 64}, tmp_path / "bad.zip")
    with pytest.raises(ValueError, match="HTTPS"):
        runtime._download({**spec, "url": "http://example.com/file"}, tmp_path / "bad-http.zip")


def test_download_cancellation_leaves_no_install(tmp_path, monkeypatch):
    event = threading.Event()
    event.set()
    monkeypatch.setattr(runtime, "discover", _inventory)
    monkeypatch.setattr(runtime, "_install", lambda *_: pytest.fail("must not install after cancel"))
    result = runtime.ensure(cancel_event=event)
    assert result["ngspice"]["cancelled"] and result["openems"]["cancelled"]


def test_disabled_engines_do_not_download_and_setup_errors_are_independent(monkeypatch):
    monkeypatch.setattr(runtime, "discover", _inventory)
    monkeypatch.setattr(runtime.platform, "system", lambda: "Windows")
    monkeypatch.setattr(runtime.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(runtime, "_cached", lambda *_: None)
    calls = []
    def install(engine, *_):
        calls.append(engine)
        if engine == "ngspice":
            raise OSError("offline")
        return {"available": True, "path": "solver", "kind": "executable", "version": "test", "message": "Ready"}
    monkeypatch.setattr(runtime, "_install", install)
    result = runtime.ensure(engines=["openems"])
    assert calls == ["openems"] and result["openems"]["available"]
    calls.clear()
    result = runtime.ensure()
    assert calls == ["ngspice", "openems"]
    assert "offline" in result["ngspice"]["message"] and result["openems"]["available"]


def test_valid_cached_solver_requires_archive_and_executable_hashes(tmp_path, monkeypatch):
    monkeypatch.setenv("EMI_SOLVER_CACHE", str(tmp_path))
    spec = runtime.DOWNLOADS["openems"]
    root = tmp_path / f"openems-{spec['version']}"
    exe = root / spec["executable"]
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"verified executable")
    record = {"archive_sha256": spec["sha256"], "executable_sha256": runtime._digest(exe)}
    (root / "emi-runtime.json").write_text(json.dumps(record))
    assert runtime._cached("openems") == exe
    exe.write_bytes(b"changed executable")
    assert runtime._cached("openems") is None


def test_install_commits_only_after_validation_and_preserves_previous(tmp_path, monkeypatch):
    monkeypatch.setenv("EMI_SOLVER_CACHE", str(tmp_path))
    spec = runtime.DOWNLOADS["openems"]
    root = tmp_path / f"openems-{spec['version']}"
    root.mkdir()
    (root / "keep").write_text("previous installation")
    monkeypatch.setattr(runtime, "_download", lambda spec, target, *_: Path(target).write_bytes(b"archive"))
    def extract(_, destination, *__):
        target = destination / spec["executable"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"solver")
    monkeypatch.setattr(runtime, "_extract", extract)
    monkeypatch.setattr(runtime, "_probe", lambda *a, **k: runtime._missing("broken binary"))
    with pytest.raises(RuntimeError, match="broken binary"):
        runtime._install("openems")
    assert (root / "keep").read_text() == "previous installation"
    assert not list(tmp_path.glob(".openems-setup-*"))
    monkeypatch.setattr(runtime, "_probe", lambda *a, **k: {"available": True, "version": spec["version"]})
    installed = runtime._install("openems")
    assert Path(installed["path"]).read_bytes() == b"solver"
    assert runtime._cached("openems") == root / spec["executable"]
    assert not list(tmp_path.glob(".openems-setup-*"))


def test_shared_runtime_discovery_and_custom_cache(tmp_path, monkeypatch):
    library = tmp_path / "ngspice.dll"
    library.write_bytes(b"test")
    monkeypatch.setenv("EMI_NGSPICE", str(library))
    monkeypatch.setenv("EMI_ASSISTANT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("EMI_SOLVER_CACHE", raising=False)
    monkeypatch.setattr(runtime, "_kicad_directories", lambda: [])
    monkeypatch.setattr(runtime.shutil, "which", lambda _: None)
    monkeypatch.setattr(runtime, "_cached", lambda _: None)
    def probe(path, engine, kind):
        assert kind == "shared"
        return {"available": True, "path": str(path), "kind": kind, "version": "test", "message": "Ready"}
    monkeypatch.setattr(runtime, "_probe", probe)
    result = runtime.discover()
    assert result["ngspice"]["kind"] == "shared"
    assert runtime.cache_directory() == tmp_path / "cache/solvers"


def test_openems_usage_probe_handles_native_exit_255(tmp_path, monkeypatch):
    import subprocess
    binary = tmp_path / "openEMS.exe"
    binary.write_bytes(b"binary")
    def run(command, **kwargs):
        assert command == [str(binary)]
        return subprocess.CompletedProcess(command, 255, b"openEMS 64bit -- version v0.0.36\nUsage: openEMS <FDTD_XML_FILE>\n")
    monkeypatch.setattr(runtime.subprocess, "run", run)
    result = runtime._probe(binary, "openems")
    assert result["available"] and result["version"] == "0.0.36"


def test_loader_error_is_not_mistaken_for_version(tmp_path, monkeypatch):
    import subprocess
    binary = tmp_path / "ngspice"
    binary.write_bytes(b"binary")
    monkeypatch.setattr(runtime.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 127, b"ngspice: error while loading shared libraries"))
    assert not runtime._probe(binary, "ngspice")["available"]
