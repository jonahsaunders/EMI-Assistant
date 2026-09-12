"""Solver contract, input safety, and actual ngspice analytic comparisons.

Set EMI_TEST_NGSPICE and/or EMI_TEST_NGSPICE_SHARED to exercise installed
runtimes. Tests without a runtime still validate missing/error/cancel behavior.
"""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

from emi_assistant import sim_ngspice as sim
from emi_assistant.models import BoardSnapshot, Footprint, Pad, default_settings


def _fp(reference, value, nets, location=(0., 0.)):
    return Footprint(reference, reference, value, location, [
        Pad(reference + ":" + str(i), str(i), (location[0] + i, location[1]), (1., 1.), ["F.Cu"], net, reference)
        for i, net in enumerate(nets, 1)])


def _board(*footprints):
    return BoardSnapshot("test", "", "test", footprints=list(footprints))


def _runtimes():
    answer = []
    executable = os.environ.get("EMI_TEST_NGSPICE") or shutil.which("ngspice")
    shared = os.environ.get("EMI_TEST_NGSPICE_SHARED")
    for kind, path in (("executable", executable), ("shared", shared)):
        if path:
            answer.append({"available": True, "kind": kind, "path": path})
    return answer or [None]


@pytest.mark.parametrize("value,kind,expected", [("100nF", "C", 100e-9), ("4n7", "C", 4.7e-9),
    ("2R2", "R", 2.2), ("10uH", "L", 10e-6), ("4.7 µF", "C", 4.7e-6),
    ("1M", "R", 1e6), ("1m", "R", .001), ("0", "R", 0)])
def test_numeric_values(value, kind, expected):
    assert sim.component_value(value, kind) == pytest.approx(expected)


@pytest.mark.parametrize("value,kind", [("1nF\n.control\nshell evil", "C"), ("{parameter}", "C"),
    ("10nH", "C"), ("4n7H", "C"), ("0R1", "C"), ("nan", "R"), ("-1uF", "C"), ("1e999", "R")])
def test_expressions_and_wrong_units_rejected(value, kind):
    assert sim.component_value(value, kind) is None


def test_missing_engine_and_missing_circuit_never_complete(tmp_path):
    board = _board(_fp("U1", "Custom IC", ["CLK", "GND"]))
    missing = sim.run(board, {}, tmp_path, {"available": False})
    assert missing[0]["status"] == "unavailable"
    inputs = sim.run(board, {}, tmp_path, {"available": True, "path": "unused"})
    assert inputs[0]["status"] == "needs_information"
    assert inputs[0]["missing"]


def test_stale_schematic_value_not_used():
    board = _board(_fp("C1", "100nF", ["VCC", "GND"]))
    circuit = {"components": [{"reference": "C1", "value": "1uF", "board_match": False}]}
    assert list(sim._components(board, circuit))[0]["value"] == pytest.approx(100e-9)
    circuit["components"][0]["board_match"] = True
    assert list(sim._components(board, circuit))[0]["value"] == pytest.approx(1e-6)


def test_ambiguous_filter_ports_are_not_guessed():
    board = _board(_fp("R1", "100", ["A", "B"]), _fp("C1", "100nF", ["A", "GND"]),
                   _fp("C2", "100nF", ["B", "GND"]))
    assert list(sim._filter_jobs(board, {}, list(sim._components(board, None)), sim._settings({}))) == []


def test_custom_symbol_with_passive_pins_works():
    board = _board(_fp("C77", "Custom capacitor symbol", ["VCC", "GND"]))
    circuit = {"components": [{"reference": "C77", "value": "220nF", "board_match": True,
                               "properties": {"Sim.Model": "arbitrary model text ignored"}}]}
    comps = list(sim._components(board, circuit))
    assert comps[0]["value"] == pytest.approx(220e-9)
    deck = list(sim._cap_jobs(board, {}, comps, sim._settings({})))[0]["deck"]
    assert "arbitrary" not in deck and ".include" not in deck and ".control" not in deck


def test_cancelled_before_start(tmp_path):
    event = threading.Event()
    event.set()
    result = sim.run(_board(), {}, tmp_path, {"available": True, "path": "unused"}, cancel_event=event)
    assert result[0]["status"] == "cancelled"


def test_start_failure_preserves_evidence(tmp_path):
    board = _board(_fp("C1", "100nF", ["VCC", "GND"]))
    result = sim.run(board, {}, tmp_path, {"available": True, "path": str(tmp_path / "missing")})[0]
    assert result["status"] == "error"
    assert any(p.endswith("comparison.cir") for p in result["artifacts"])
    assert all(Path(p).exists() for p in result["artifacts"])
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("cancel", [False, True])
def test_child_process_is_killed_on_timeout_or_cancel(tmp_path, monkeypatch, cancel):
    original = subprocess.Popen
    children = []
    def sleeping_process(*args, **kwargs):
        child = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(sim.subprocess, "Popen", sleeping_process)
    event = threading.Event()
    timer = threading.Timer(.08, event.set)
    if cancel:
        timer.start()
    try:
        with pytest.raises(sim.SolverCancelled if cancel else sim.SolverFailure):
            sim._execute("title\n.end\n", tmp_path, {"path": "unused"}, 2 if cancel else .1, event)
    finally:
        timer.cancel()
    assert children and children[0].poll() is not None


def _raw(values):
    return ("Title: test\nFlags: complex\nNo. Variables: 3\nNo. Points: 3\nVariables:\n"
            "0 frequency frequency grid=3\n1 v(base) voltage\n2 v(candidate) voltage\nValues:\n" + values)


@pytest.mark.parametrize("values", ["0 1,0\n1,0\n1,0\n",  # truncated
    "0 1,0\n1,0\n1,0\n1 2,0\nnan,0\n1,0\n2 3,0\n1,0\n1,0\n",
    "0 1,0\n1,0\n1,0\n1 1,0\n1,0\n1,0\n2 3,0\n1,0\n1,0\n"])
def test_bad_solver_output_rejected(tmp_path, values):
    path = tmp_path / "result.raw"
    path.write_text(_raw(values))
    with pytest.raises(sim.SolverFailure):
        sim.parse_raw(path)


@pytest.mark.parametrize("runtime", _runtimes(), ids=lambda r: r["kind"] if r else "no-runtime")
def test_actual_solver_matches_analytic_rc(tmp_path, runtime):
    if runtime is None:
        pytest.skip("Set EMI_TEST_NGSPICE[_SHARED] for real solver validation")
    board = _board(_fp("R1", "100", ["IN", "OUT"]), _fp("C1", "10nF", ["OUT", "GND"]))
    settings = {"simulation": {"frequency_start_mhz": .000001, "frequency_stop_mhz": 1,
                               "source_ohm": 50, "termination_ohm": 1000}}
    config = sim._settings(settings)
    job = list(sim._filter_jobs(board, settings, list(sim._components(board, None)), config))[0]
    f, base, candidate = sim._execute(job["deck"], tmp_path, runtime, 20)
    for freq, actual, changed in zip(f, base, candidate):
        omega = 2 * math.pi * freq
        zcap = config["esr"] + 1j * omega * config["esl"] + 1 / (1j * omega * 10e-9)
        parallel = 1 / (1 / zcap + 1 / 1000)
        assert actual == pytest.approx(abs(parallel / (150 + 1e-9 + parallel)), rel=1e-8)
        assert changed == pytest.approx(abs(parallel / (172 + parallel)), rel=1e-8)
    sim._finish(job, (f, base, candidate), config)
    assert job["result"]["status"] == "complete"
    json.dumps(job["result"], allow_nan=False)


@pytest.mark.parametrize("runtime", _runtimes(), ids=lambda r: r["kind"] if r else "no-runtime")
def test_actual_capacitor_impedance_and_added_cap(tmp_path, runtime):
    if runtime is None:
        pytest.skip("Set EMI_TEST_NGSPICE[_SHARED] for real solver validation")
    board = _board(_fp("C1", "100nF", ["VCC", "GND"]))
    settings = default_settings()
    settings["simulation"] = {"max_jobs": 1}
    result = sim.run(board, settings, tmp_path, runtime)[0]
    assert result["status"] == "complete", result["summary"]
    first = result["series"][0]
    for mhz, impedance in zip(first["x"], first["y"]):
        omega = 2 * math.pi * mhz * 1e6
        z = .03 + 1j * omega * .807e-9 + 1 / (1j * omega * 100e-9)
        assert impedance == pytest.approx(abs(1 / (1 / z + 1e-12)), rel=1e-8)
    assert result["series"][1]["y"][0] < first["y"][0] * .51
    assert any("ESR" in a for a in result["assumptions"])
    assert len(result["artifacts"]) == 4
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("runtime", _runtimes(), ids=lambda r: r["kind"] if r else "no-runtime")
def test_actual_lc_ringing_is_reduced_by_damping(tmp_path, runtime):
    if runtime is None:
        pytest.skip("Set EMI_TEST_NGSPICE[_SHARED] for real solver validation")
    board = _board(_fp("L1", "1uH", ["IN", "OUT"]), _fp("C1", "10nF", ["OUT", "GND"]))
    settings = {"simulation": {"source_ohm": 1, "termination_ohm": 1e6, "candidate_series_ohm": 22}}
    config = sim._settings(settings)
    jobs = list(sim._filter_jobs(board, settings, list(sim._components(board, None)), config))
    transient = next(j for j in jobs if j["quantity"] == "transient")
    data = sim._execute(transient["deck"], tmp_path, runtime, 20, transient=True)
    sim._finish_transient(transient, data, config)
    result = transient["result"]
    assert result["status"] == "complete"
    metrics = {m["name"]: m["value"] for m in result["metrics"]}
    # This underdamped RLC has zeta ~= 0.0565; the analytic overshoot is ~84%.
    assert 81 < metrics["Baseline overshoot above its DC level"] < 86
    assert metrics["Candidate overshoot above its DC level"] < 1
    assert metrics["Baseline 2% settling time after input step"] > 0
    assert result["series"][0]["x_unit"] == "us"
    json.dumps(result, allow_nan=False)


def test_config_accepts_validated_settings_bounds_without_silent_defaults():
    config = sim._settings({"simulation": {"capacitor_esr_ohm": 0, "capacitor_esl_nh": 0,
        "candidate_series_ohm": 0, "points": 2001, "max_seconds": 3600, "max_jobs": 8,
        "source_ohm": 1e-6, "termination_ohm": 1e-6, "frequency_start_mhz": 90000,
        "frequency_stop_mhz": 100000}})
    assert config["esr"] == config["esl"] == config["series_r"] == 0
    assert config["points"] == 2001 and config["seconds"] == 3600 and config["jobs"] == 8
    assert config["source"] == config["load"] == 1e-6
    assert config["low"] == 9e10 and config["high"] == 1e11


def test_owned_sources_support_python_310():
    import ast
    root = Path(__file__).resolve().parents[1]
    for name in ("sim_ngspice.py", "sim_ngspice_worker.py"):
        path = root / "emi_assistant" / name
        ast.parse(path.read_text(), filename=str(path), feature_version=(3, 10))


@pytest.mark.parametrize("runtime", _runtimes(), ids=lambda r: r["kind"] if r else "no-runtime")
def test_actual_zero_parasitics_are_ideal_elements_and_warn_about_scope(tmp_path, runtime):
    if runtime is None:
        pytest.skip("Set EMI_TEST_NGSPICE[_SHARED] for real solver validation")
    board = _board(_fp("R1", "100", ["IN", "OUT"]), _fp("C1", "10nF", ["OUT", "GND"]))
    settings = {"simulation": {"capacitor_esr_ohm": 0, "capacitor_esl_nh": 0,
                                "candidate_series_ohm": 0, "max_jobs": 1}}
    results = sim.run(board, settings, tmp_path, runtime)
    result = results[0]
    assert result["status"] == "complete", result["summary"]
    assert result["series"][0]["y"] == pytest.approx(result["series"][1]["y"], abs=1e-10)
    assert any("Full IC and custom behavioral models are not executed" in s for s in result["warnings"])
    assert any("Zero-valued" in s for s in result["assumptions"])
    deck = Path(next(p for p in result["artifacts"] if p.endswith(".cir"))).read_text()
    assert "Rextra" not in deck and "Lc0" not in deck and "Rc0" not in deck
