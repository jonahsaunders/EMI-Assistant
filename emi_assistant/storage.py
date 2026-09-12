"""Small, explicit, atomic project sidecars."""
from __future__ import annotations
import json
import math
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from .models import default_settings

def write_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as out:
            temp_name = out.name
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)

def validate_settings(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Board context must be a JSON object.")
    settings = default_settings()
    settings.update(deepcopy(value))
    if settings.get("schema_version") != 1:
        raise ValueError("This board context uses an unsupported version.")
    for name in ["fast_nets", "reference_layers", "ignored", "thresholds"]:
        if not isinstance(settings.get(name), dict):
            raise ValueError(f"{name} must be an object.")
    for name in ["reference_nets", "external_connectors", "regulators"]:
        if not isinstance(settings.get(name), list):
            raise ValueError(f"{name} must be a list.")
    if not settings["reference_nets"] or any(not isinstance(x, str) or not x.strip() for x in settings["reference_nets"]):
        raise ValueError("Choose at least one ground/reference net name.")
    for net, meta in settings["fast_nets"].items():
        if not isinstance(net, str) or not isinstance(meta, dict):
            raise ValueError("Each fast net must have a name and settings object.")
        for key in ["frequency_mhz", "rise_ns"]:
            if meta.get(key) is not None:
                try:
                    number = float(meta[key])
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"{net}: enter a numeric {key}.") from exc
                if not math.isfinite(number) or number <= 0:
                    raise ValueError(f"{net}: {key} must be greater than zero.")
                meta[key] = number
    for layer, references in settings["reference_layers"].items():
        if not isinstance(layer, str) or not isinstance(references, list) or any(not isinstance(r, str) for r in references):
            raise ValueError("Reference layers must map a signal layer to a list of copper layers.")
    for name, number in settings["thresholds"].items():
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number <= 0:
            raise ValueError(f"Threshold {name} must be a finite positive number.")
    for regulator in settings["regulators"]:
        if not isinstance(regulator, dict) or not regulator.get("reference") or regulator.get("topology") not in ("buck", "boost"):
            raise ValueError("Each regulator needs a reference and a buck or boost topology.")
    sim_input = settings.get("simulation", {})
    if not isinstance(sim_input, dict):
        raise ValueError("Simulation settings must be an object.")
    sim = default_settings()["simulation"]
    sim.update(deepcopy(sim_input))
    if not isinstance(sim["enabled"], bool):
        raise ValueError("Simulation enabled must be true or false.")
    engines = sim.get("engines")
    if not isinstance(engines, dict) or any(k not in ("ngspice", "openems") or not isinstance(v, bool) for k,v in engines.items()):
        raise ValueError("Choose enabled ngspice and openEMS engines using true/false.")
    sim["engines"] = {"ngspice": True, "openems": True, **engines}
    ranges = {
        "max_seconds": (5, 3600), "max_jobs": (1, 8), "points": (21, 2001),
        "frequency_start_mhz": (0.000001, 100000), "frequency_stop_mhz": (0.000001, 100000),
        "capacitor_esr_ohm": (0, 1000), "capacitor_esl_nh": (0, 10000),
        "source_ohm": (0.000001, 1000000), "termination_ohm": (0.000001, 1000000),
        "candidate_cap_nf": (.001, 1000000), "candidate_series_ohm": (0, 1000000),
    }
    for key, (lo, hi) in ranges.items():
        number = sim.get(key)
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not lo <= number <= hi:
            raise ValueError(f"Simulation {key} must be between {lo:g} and {hi:g}.")
        if key in ("max_jobs", "points"):
            if int(number) != number:
                raise ValueError(f"Simulation {key} must be a whole number.")
            sim[key] = int(number)
    if sim["frequency_stop_mhz"] <= sim["frequency_start_mhz"]:
        raise ValueError("Simulation stop frequency must be greater than start frequency.")
    if not isinstance(sim.get("schematic_path"), str):
        raise ValueError("Choose a schematic filename or leave it blank.")
    if not isinstance(sim.get("external_model_paths"), list) or any(not isinstance(p,str) for p in sim["external_model_paths"]):
        raise ValueError("External model paths must be a list of filenames.")
    settings["simulation"] = sim
    return settings

def load_settings(path: Path | None) -> dict:
    if path is None or not path.exists():
        return default_settings()
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Board context file is unexpectedly large.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError(f"Could not read {path.name}. Correct its JSON or rename it to start new board context.") from exc
    return validate_settings(value)
