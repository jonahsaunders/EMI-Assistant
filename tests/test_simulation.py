"""Simulation integration gates with deterministic local fake engines only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest

from emi_assistant.controller import Controller
from emi_assistant.models import AnalysisResult, BoardSnapshot, Track, default_settings
from emi_assistant.simulation import SimulationManager, _validated_results


def board():
    return BoardSnapshot("Test board", "", "revision-1", tracks=[
        Track("track", (1, 2), (10, 2), .25, "F.Cu", "CLK")])


def result(engine, artifact=None):
    return {"id": engine + "-response", "engine": engine, "title": "Test solver response",
            "status": "complete", "summary": "A fake response for an integration test.",
            "assumptions": [], "warnings": [], "missing": [], "item_ids": [],
            "recommendations": [], "metrics": [{"name": "Peak", "value": 1.0, "unit": "V"}],
            "series": [{"name": "Baseline", "x": [1., 2., 3.], "y": [1., 2., 1.], "x_unit": "MHz", "y_unit": "V"}],
            "artifacts": [str(artifact)] if artifact else []}


class Runtime:
    def __init__(self):
        self.calls = 0
        self.values = {
            "ngspice": {"available": True, "path": "", "version": "fake-spice-1", "kind": "executable"},
            "openems": {"available": True, "path": "", "version": "fake-fields-1", "kind": "executable"},
            "kicad_cli": {"available": True, "path": "", "version": "fake-kicad-1"},
        }

    def ensure(self, **kwargs):
        self.calls += 1
        return deepcopy(self.values)

    def discover(self):
        return deepcopy(self.values)


class Schematic:
    def __init__(self):
        self.fingerprint = "schematic-one"
        self.calls = 0

    def prepare(self, board, settings, workdir, cli, cancel_event=None):
        self.calls += 1
        workdir.mkdir(parents=True, exist_ok=True)
        artifact = workdir / "resolved.net"
        artifact.write_text("test connectivity", encoding="utf-8")
        return {"status": "complete", "message": "Test resolved schematic.",
                "source": "test.kicad_sch", "fingerprint": self.fingerprint,
                "components": [{"reference": "R1", "value": "50", "properties": {}, "pins": {"1": "CLK", "2": "GND"}}],
                "warnings": [], "missing": [], "artifacts": [str(artifact)]}

    def fingerprint_inputs(self, board, settings):
        return self.fingerprint


class Backend:
    def __init__(self, engine):
        self.engine = engine
        self.calls = 0
        self.operation = None

    def run(self, board, settings, workdir, runtime, **kwargs):
        self.calls += 1
        if self.operation:
            return self.operation(board, settings, workdir, runtime, **kwargs)
        artifact = workdir / "solver.log"
        artifact.write_text("fake numerical result", encoding="utf-8")
        return [result(self.engine, artifact)]


class SimulationManagerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = Runtime()
        self.schematic = Schematic()
        self.backends = {name: Backend(name) for name in ("ngspice", "openems")}
        self.manager = SimulationManager(self.root, self.runtime, self.schematic, self.backends)
        self.board = board()
        self.settings = default_settings()

    def tearDown(self):
        self.temporary.cleanup()

    def run_simulations(self, **kwargs):
        return self.manager.run(self.board, self.settings, **kwargs)

    def counts(self):
        return [self.backends[name].calls for name in ("ngspice", "openems")]

    def test_engine_failure_is_independent_and_error_log_is_preserved(self):
        def fail(*args, **kwargs):
            raise RuntimeError("Deliberate solver failure")
        self.backends["ngspice"].operation = fail
        responses = self.run_simulations()
        self.assertEqual([x["status"] for x in responses], ["error", "complete"])
        self.assertIn("Deliberate solver failure", responses[0]["summary"])
        self.assertTrue(Path(responses[0]["artifacts"][0]).is_file())
        self.assertEqual(self.counts(), [1, 1])

    def test_runtime_setup_and_discovery_failure_return_clear_unavailability(self):
        def fail(**kwargs):
            raise RuntimeError("Runtime installation is unavailable")
        self.runtime.ensure = fail
        self.runtime.discover = fail
        responses = self.run_simulations()
        self.assertEqual([x["status"] for x in responses], ["unavailable", "unavailable"])
        self.assertTrue(all("installation is unavailable" in x["summary"] for x in responses))
        self.assertEqual(self.counts(), [0, 0])

    def test_complete_cache_reuses_both_engines(self):
        initial = self.run_simulations()
        cached = self.run_simulations()
        self.assertEqual(self.counts(), [1, 1])
        self.assertTrue(all(x["cached"] for x in cached))
        self.assertTrue(all(not x["cached"] for x in initial))
        self.assertEqual(cached[0]["series"], initial[0]["series"])
        self.assertTrue(all(Path(path).is_file() for entry in cached for path in entry["artifacts"]))

    def test_cache_tracks_complete_board_geometry_even_if_fingerprint_unchanged(self):
        self.run_simulations()
        self.board.tracks[0].width = .7
        responses = self.run_simulations()
        self.assertEqual(self.counts(), [2, 2])
        self.assertTrue(all(not x["cached"] for x in responses))

    def test_cache_tracks_simulation_settings_and_saved_schematic_contents(self):
        self.run_simulations()
        self.settings["simulation"]["capacitor_esr_ohm"] = .07
        self.run_simulations()
        self.assertEqual(self.counts(), [2, 2])
        self.schematic.fingerprint = "changed hierarchy or model content"
        self.run_simulations()
        self.assertEqual(self.counts(), [3, 3])

    def test_prepared_snapshot_fingerprint_wins_over_later_file_changes(self):
        # prepare() identifies the exported snapshot. A later file read must not
        # relabel its numerical results as if they used the newer schematic.
        self.schematic.fingerprint_inputs = lambda *args: "later-saved-version-A"
        first = self.run_simulations()
        self.schematic.fingerprint_inputs = lambda *args: "later-saved-version-B"
        second = self.run_simulations()
        self.assertEqual(self.counts(), [1, 1])
        self.assertEqual(first[0]["input_fingerprint"], second[0]["input_fingerprint"])
        self.assertTrue(all(x["cached"] for x in second))
        self.schematic.fingerprint = "newly-exported-snapshot"
        third = self.run_simulations()
        self.assertEqual(self.counts(), [2, 2])
        self.assertNotEqual(first[0]["input_fingerprint"], third[0]["input_fingerprint"])

    def test_cache_tracks_engine_version_and_executable_changes_independently(self):
        executable = self.root / "fake-solver"
        executable.write_bytes(b"first version")
        self.runtime.values["ngspice"]["path"] = str(executable)
        self.run_simulations()
        self.runtime.values["ngspice"]["version"] = "fake-spice-2"
        self.run_simulations()
        self.assertEqual(self.counts(), [2, 1])
        executable.write_bytes(b"changed binary of a different size")
        self.run_simulations()
        self.assertEqual(self.counts(), [3, 1])
        self.runtime.values["kicad_cli"]["version"] = "different-exporter"
        self.run_simulations()
        self.assertEqual(self.counts(), [4, 2])

    def test_missing_evidence_file_invalidates_cache(self):
        first = self.run_simulations()
        Path(first[0]["artifacts"][0]).unlink()
        second = self.run_simulations()
        self.assertEqual(self.counts(), [2, 1])
        self.assertFalse(second[0]["cached"])
        self.assertTrue(second[1]["cached"])

    def test_cancellation_discards_backend_results_and_does_not_cache_them(self):
        cancel = threading.Event()
        def cancel_during_run(*args, **kwargs):
            cancel.set()
            return [result("ngspice")]
        self.backends["ngspice"].operation = cancel_during_run
        cancelled = self.run_simulations(cancel_event=cancel)
        self.assertEqual([x["status"] for x in cancelled], ["cancelled", "cancelled"])
        self.assertEqual(self.counts(), [1, 0])
        self.assertFalse(list((self.root / "results").glob("**/*.json")))
        self.backends["ngspice"].operation = None
        self.run_simulations()
        self.assertEqual(self.counts(), [2, 1])

    def test_disabled_empty_and_precancelled_inputs_never_initialize_engines(self):
        self.settings["simulation"]["enabled"] = False
        self.assertEqual(self.run_simulations(), [])
        self.settings["simulation"]["enabled"] = True
        self.settings["simulation"]["engines"] = {"ngspice": False, "openems": False}
        self.assertEqual(self.run_simulations(), [])
        self.settings["simulation"]["engines"] = {"ngspice": True, "openems": True}
        cancel = threading.Event()
        cancel.set()
        self.assertTrue(all(x["status"] == "cancelled" for x in self.run_simulations(cancel_event=cancel)))
        self.board.tracks.clear()
        self.assertTrue(all(x["status"] == "needs_information" for x in self.run_simulations()))
        self.assertEqual(self.runtime.calls, 0)
        self.assertEqual(self.schematic.calls, 0)
        self.assertEqual(self.counts(), [0, 0])

    def test_invalid_numeric_results_become_error_and_other_engine_runs(self):
        def invalid(*args, **kwargs):
            response = result("ngspice")
            response["series"][0]["y"][1] = float("nan")
            return [response]
        self.backends["ngspice"].operation = invalid
        responses = self.run_simulations()
        self.assertEqual([x["status"] for x in responses], ["error", "complete"])
        self.assertFalse(list((self.root / "results" / "ngspice").glob("*.json")))

    def test_malformed_cache_is_ignored_and_recomputed(self):
        self.run_simulations()
        cache = next((self.root / "results" / "ngspice").glob("*.json"))
        cache.write_text("[]", encoding="utf-8")
        results = self.run_simulations()
        self.assertEqual(self.counts(), [2, 1])
        self.assertEqual([x["status"] for x in results], ["complete", "complete"])


class SimulationValidationTest(unittest.TestCase):
    def test_nonfinite_and_mismatched_coordinates_are_rejected(self):
        for invalid in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid=invalid):
                data = result("ngspice")
                data["metrics"][0]["value"] = invalid
                with self.assertRaises((ValueError, TypeError)):
                    _validated_results([data], "ngspice")
        data = result("ngspice")
        data["series"][0]["x"].append(4.)
        with self.assertRaises(ValueError):
            _validated_results([data], "ngspice")

    def test_non_numeric_curve_and_metric_values_are_rejected(self):
        for invalid in ("unknown", None, True, {}, []):
            with self.subTest(field="curve", invalid=invalid):
                data = result("ngspice")
                data["series"][0]["y"][1] = invalid
                with self.assertRaises((ValueError, TypeError)):
                    _validated_results([data], "ngspice")
            with self.subTest(field="metric", invalid=invalid):
                data = result("ngspice")
                data["metrics"][0]["value"] = invalid
                with self.assertRaises((ValueError, TypeError)):
                    _validated_results([data], "ngspice")

    def test_complete_result_cannot_contain_only_empty_curve(self):
        data = result("ngspice")
        data["metrics"] = []
        data["series"][0]["x"] = []
        data["series"][0]["y"] = []
        with self.assertRaises(ValueError):
            _validated_results([data], "ngspice")


class ControllerSimulationTest(unittest.TestCase):
    def test_slow_solver_does_not_hold_controller_lock_or_replace_new_analysis(self):
        controller = Controller()
        controller.mode = "demo"
        first = AnalysisResult(board(), [], [])
        controller.result = first
        started, release = threading.Event(), threading.Event()
        captured = {}
        class Manager:
            def run(self, snapshot, settings, **kwargs):
                captured["board"] = snapshot
                captured["settings"] = settings
                captured["cancel"] = kwargs["cancel_event"]
                started.set()
                release.wait(3)
                return [result("ngspice")]
        controller._simulation_manager = Manager()
        thread = threading.Thread(target=controller.run_simulations)
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            self.assertTrue(controller._lock.acquire(timeout=.2))
            try:
                newer = AnalysisResult(board(), [], [])
                newer.board.fingerprint = "newer-board"
                controller.result = newer
                controller.settings["simulation"]["source_ohm"] = 75
                first.board.tracks[0].width = .9
            finally:
                controller._lock.release()
            controller.cancel_simulations()
            self.assertTrue(captured["cancel"].is_set())
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIs(controller.result, newer)
        self.assertEqual(newer.simulations, [])
        self.assertEqual(first.simulations, [])
        self.assertEqual(captured["board"].tracks[0].width, .25)
        self.assertEqual(captured["settings"]["simulation"]["source_ohm"], 50)

    def test_current_result_receives_simulations_and_requires_initial_analysis(self):
        controller = Controller()
        with self.assertRaisesRegex(RuntimeError, "Analyze a board"):
            controller.run_simulations()
        controller.result = AnalysisResult(board(), [], [])
        class Manager:
            def run(self, *args, **kwargs):
                return [result("ngspice")]
        controller._simulation_manager = Manager()
        responses = controller.run_simulations()
        self.assertIs(controller.result.simulations, responses)


if __name__ == "__main__":
    unittest.main()
