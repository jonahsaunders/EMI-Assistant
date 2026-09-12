"""Background solver orchestration with independent failure handling and caching."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import threading
import uuid

from .storage import validate_settings, write_json

VERSION = "0.2.0-simulation-1"
STATUSES = {"complete", "needs_information", "unavailable", "error", "cancelled"}


def cache_root() -> Path:
    explicit = os.environ.get("EMI_ASSISTANT_CACHE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "EMI-Assistant"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "emi-assistant"


def message_result(engine, status, summary, missing=None):
    return {"id": engine + "-status", "engine": engine,
            "title": "Circuit simulations" if engine == "ngspice" else "Field simulations",
            "status": status, "summary": summary, "missing": missing or [],
            "assumptions": [], "warnings": [], "metrics": [], "series": [],
            "recommendations": [], "item_ids": [], "artifacts": []}


def _identity(runtime):
    identity = {k: runtime.get(k) for k in ("available", "path", "kind", "version", "identity")}
    path = Path(runtime.get("path") or "")
    if path.is_file():
        stat = path.stat()
        identity["file_size"] = stat.st_size
        identity["modified_ns"] = stat.st_mtime_ns
    return identity


def _validated_results(results, engine):
    if not isinstance(results, list) or not results:
        raise ValueError("The solver returned no result or coverage explanation.")
    # Strict serialization rejects NaN/Infinity before graphs or cache can use it.
    json.dumps(results, allow_nan=False)
    def finite_number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    for result in results:
        if not isinstance(result, dict) or result.get("engine") != engine or result.get("status") not in STATUSES:
            raise ValueError("The solver returned an invalid result status.")
        for key in ("assumptions", "missing", "warnings", "metrics", "series", "recommendations", "item_ids", "artifacts"):
            result.setdefault(key, [])
            if not isinstance(result[key], list):
                raise ValueError(f"The solver returned an invalid {key} list.")
        for metric in result["metrics"]:
            if not isinstance(metric, dict) or not finite_number(metric.get("value")):
                raise ValueError("The solver returned a non-numerical metric.")
        for series in result["series"]:
            if not isinstance(series, dict):
                raise ValueError("The solver returned an invalid curve.")
            x, y = series.get("x"), series.get("y")
            if not isinstance(x, list) or not isinstance(y, list) or not x or len(x) != len(y):
                raise ValueError("The solver curve has mismatched coordinates.")
            if not all(finite_number(value) for value in [*x, *y]):
                raise ValueError("The solver curve contains non-numerical coordinates.")
        if result["status"] == "complete" and not (result["metrics"] or result["series"]):
            raise ValueError("The solver reported success without numerical results.")
    return results


class SimulationManager:
    def __init__(self, root=None, runtime_module=None, schematic_module=None, backends=None):
        self.root = Path(root) if root else cache_root() / "simulations"
        self.runtime_module = runtime_module
        self.schematic_module = schematic_module
        self.backends = backends

    def run(self, board, settings, progress=None, cancel_event=None):
        settings = validate_settings(settings)
        sim = settings["simulation"]
        if not sim["enabled"]:
            return []
        engines = [engine for engine in ("ngspice", "openems") if sim["engines"].get(engine, True)]
        if not engines:
            return []
        cancel = cancel_event or threading.Event()
        def emit(message):
            if progress:
                progress(message)
        if cancel.is_set():
            return [message_result(engine, "cancelled", "Simulation cancelled.") for engine in engines]
        if not board.tracks and not board.pads:
            return [message_result(engine, "needs_information", "Open a populated, routed PCB to prepare this simulation.",
                                   ["This board has no tracks or component pads."]) for engine in engines]
        self.root.mkdir(parents=True, exist_ok=True)
        run_directory = self.root / "runs" / uuid.uuid4().hex
        run_directory.mkdir(parents=True)
        runtime_module = self.runtime_module or importlib.import_module(".simulation_runtime", __package__)
        try:
            emit("Checking simulation engines…")
            runtimes = runtime_module.ensure(progress=emit, cancel_event=cancel, engines=engines)
        except Exception as exc:
            # Discovery and individual backends may still work after a setup error.
            try:
                runtimes = runtime_module.discover()
            except Exception:
                runtimes = {}
            for engine in engines:
                if not runtimes.get(engine, {}).get("available"):
                    runtimes.setdefault(engine, {})["message"] = str(exc)
        if cancel.is_set():
            return [message_result(engine, "cancelled", "Simulation cancelled during setup.") for engine in engines]
        schematic = self.schematic_module or importlib.import_module(".sim_schematic", __package__)
        try:
            emit("Reading the saved schematic and checking PCB connections…")
            circuit = schematic.prepare(board, settings, run_directory / "schematic", runtimes.get("kicad_cli", {}), cancel_event=cancel)
        except Exception as exc:
            circuit = {"status": "error", "message": f"Schematic preparation failed: {exc}",
                       "missing": [str(exc)], "warnings": [], "components": [], "artifacts": []}
        try:
            # Hash the exact staged input that was exported. A schematic saved
            # immediately after export must not relabel the previous snapshot.
            source_fingerprint = circuit.get("fingerprint") or schematic.fingerprint_inputs(board, settings)
        except (AttributeError, OSError, ValueError):
            source_fingerprint = circuit.get("fingerprint", "unavailable")
        fingerprint_data = {"implementation": VERSION, "board": asdict(board), "settings": settings,
                            "schematic": source_fingerprint, "circuit_status": circuit.get("status"),
                            "circuit_components": circuit.get("components", []),
                            "kicad_cli": _identity(runtimes.get("kicad_cli", {}))}
        fingerprint = hashlib.sha256(json.dumps(fingerprint_data, sort_keys=True, allow_nan=False).encode()).hexdigest()
        all_results = []
        for engine in engines:
            runtime = runtimes.get(engine, {})
            if cancel.is_set():
                all_results.append(message_result(engine, "cancelled", "Simulation cancelled."))
                continue
            if not runtime.get("available"):
                all_results.append(message_result(engine, "unavailable", runtime.get("message") or "This simulation engine is not ready.",
                                                  [runtime.get("message") or "Simulation engine unavailable."]))
                continue
            engine_key = hashlib.sha256(json.dumps([fingerprint, _identity(runtime)], sort_keys=True).encode()).hexdigest()
            cached_file = self.root / "results" / engine / (engine_key + ".json")
            cached = self._read_cache(cached_file, engine)
            if cached is not None:
                emit(f"Reusing unchanged {engine} simulation results…")
                all_results.extend(cached)
                continue
            engine_directory = run_directory / engine
            engine_directory.mkdir()
            emit(f"Running {engine} simulations…")
            try:
                backend = self.backends[engine] if self.backends else importlib.import_module(".sim_" + engine, __package__)
                results = backend.run(board, settings, engine_directory, runtime, circuit=circuit,
                                      cancel_event=cancel, progress=emit)
                results = _validated_results(results, engine)
                for result in results:
                    result["cached"] = False
                    result["board_fingerprint"] = board.fingerprint
                    result["input_fingerprint"] = fingerprint
                    result["engine_version"] = runtime.get("version", "unknown")
                    result["generated_at"] = datetime.now(timezone.utc).isoformat()
                    result["schematic_status"] = circuit.get("status", "unavailable")
                    result["schematic_message"] = circuit.get("message", "")
                    if engine == "ngspice":
                        context = ([circuit["message"]] if circuit.get("message") else [])
                        context.extend(circuit.get("warnings", []))
                        context.extend(circuit.get("missing", []))
                        for warning in context:
                            if warning not in result["warnings"]:
                                result["warnings"].append(warning)
                    if circuit.get("source"):
                        result["schematic_source"] = circuit["source"]
                    for artifact in circuit.get("artifacts", []):
                        if artifact not in result["artifacts"]:
                            result["artifacts"].append(artifact)
                if cancel.is_set():
                    results = [message_result(engine, "cancelled", "Simulation cancelled; incomplete results were discarded.")]
                elif all(result["status"] in ("complete", "needs_information") for result in results):
                    write_json(cached_file, {"version": VERSION, "results": results})
            except Exception as exc:
                status = "cancelled" if cancel.is_set() else "error"
                error = f"{engine} could not finish: {exc}"
                (engine_directory / "error.txt").write_text(error, encoding="utf-8")
                result = message_result(engine, status, error)
                result["artifacts"] = [str(engine_directory / "error.txt")]
                results = [result]
            all_results.extend(results)
        emit("Simulation checks finished." if not cancel.is_set() else "Simulation cancelled.")
        return all_results

    @staticmethod
    def _read_cache(path, engine):
        try:
            if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
                return None
            cache = json.loads(path.read_text(encoding="utf-8"))
            if cache.get("version") != VERSION:
                return None
            results = _validated_results(cache["results"], engine)
            for result in results:
                if result["status"] not in ("complete", "needs_information"):
                    return None
                if any(not Path(artifact).is_file() for artifact in result["artifacts"]):
                    return None
                result["cached"] = True
            return deepcopy(results)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None
