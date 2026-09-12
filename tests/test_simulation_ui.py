"""Exercise the real Qt event loop while solver work is blocked or cancelled."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import time
import unittest
from unittest.mock import patch

try:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QLabel, QTextBrowser
    from emi_assistant.ui import MainWindow, ContextDialog
    from emi_assistant.ui_simulation import ResponseChart, SimulationPane
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False

from emi_assistant.models import AnalysisResult, BoardSnapshot, default_settings


def simulation_result(name="Power response", state="complete"):
    return {"engine": "ngspice", "id": "power", "title": name, "status": state,
            "summary": "Estimated impedance of the stated model.",
            "assumptions": ["Capacitor ESR is assumed to be 0.03 ohm."],
            "warnings": ["This is not a measured emissions spectrum."],
            "missing": ["Assign U1's model."] if state == "needs_information" else [],
            "series": [{"name": "Baseline", "x": [.1, 1, 10, 100], "y": [3, 1, 2, 4], "x_unit": "MHz", "y_unit": "ohm"},
                       {"name": "Candidate", "x": [.1, 1, 10, 100], "y": [2, 1, 1, 2], "x_unit": "MHz", "y_unit": "ohm"}],
            "recommendations": ["Compare the candidate with component models."],
            "metrics": [{"name": "Peak impedance", "value": 4, "unit": "ohm"}]}


class SimController:
    def __init__(self):
        self.result = None
        self.settings = default_settings()
        self.mode = "demo"
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()
        self.raise_error = False
        self.ignore_cancel = False
        self.new_number = 0

    def analyze(self):
        self.new_number += 1
        self.result = AnalysisResult(BoardSnapshot(f"Board {self.new_number}", "", str(self.new_number)), [], [])
        return self.result

    def load_demo(self):
        self.calls.append("demo")
        return self.analyze()

    def connect(self):
        self.mode = "connected"
        self.calls.append("connect")
        return self.analyze()

    def open_board(self, path):
        self.calls.append(("open", path))
        return self.analyze()

    def run_simulations(self, progress=None, cancel_event=None):
        self.calls.append("simulate")
        self.started.set()
        if progress:
            progress("Preparing the field solver…")
        deadline = time.monotonic() + 3
        while not self.release.is_set() and time.monotonic() < deadline:
            if cancel_event.is_set():
                self.cancelled.set()
                if not self.ignore_cancel:
                    return [simulation_result(state="cancelled")]
            self.release.wait(.01)
        if self.raise_error:
            raise RuntimeError("The circuit solver could not start.")
        return [simulation_result()]


@unittest.skipUnless(QT_AVAILABLE, "PySide6 Essentials is not installed")
class SimulationInterfaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.controller = SimController()
        self.window = MainWindow(self.controller)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.controller.release.set()
        self.wait_until(lambda: not self.window.busy and self.window.simulation_worker is None)
        self.window.close()
        self.app.processEvents()

    def wait_until(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.003)
        self.app.processEvents()
        self.assertTrue(predicate())

    def launch(self):
        self.window.demo_button.click()
        self.wait_until(self.controller.started.is_set)
        self.wait_until(lambda: not self.window.busy)

    def test_one_click_shows_layout_and_runs_simulations_without_blocking(self):
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start(3)
        self.launch()
        self.assertEqual(self.window.pages.currentIndex(), 1)
        self.assertEqual(self.window.result.board.name, "Board 1")
        self.assertTrue(self.window.analyze_button.isEnabled())
        self.wait_until(lambda: len(ticks) > 5)
        self.assertIn("Preparing the field solver", self.window.simulation_summary.text())
        self.window.result_tabs.setCurrentIndex(1)
        self.assertTrue(self.window.simulations.cancel_button.isVisible())
        self.controller.release.set()
        self.wait_until(lambda: self.window.simulation_worker is None)
        timer.stop()
        self.assertEqual(self.window.simulations.results[0]["title"], "Power response")
        self.assertEqual(self.controller.calls.count("simulate"), 1)
        self.assertFalse(self.window.simulations.cancel_button.isVisible())
        self.assertEqual(len(self.window.simulations.findChildren(ResponseChart)), 1)

    def test_cancel_stops_solver_and_keeps_layout_available(self):
        self.launch()
        board = self.window.result
        self.window.cancel_simulations()
        self.wait_until(lambda: self.window.simulation_worker is None)
        self.assertTrue(self.controller.cancelled.is_set())
        self.assertIs(self.window.result, board)
        self.assertEqual(self.window.simulations.results[0]["status"], "cancelled")
        self.assertTrue(self.window.analyze_button.isEnabled())

    def test_stale_completion_and_progress_do_not_overwrite_new_board(self):
        self.controller.ignore_cancel = True
        self.launch()
        old_result = self.window.result
        old_generation = self.window._simulation_generation
        updated = self.controller.analyze()
        self.window._receive_poll(updated)
        self.window._simulation_progress("Old result still running", old_generation, old_result)
        self.controller.release.set()
        self.wait_until(lambda: self.window.simulation_worker is None)
        self.assertIs(self.window.result, updated)
        self.assertEqual(self.window.simulations.results, [])
        self.assertNotIn("Old result", self.window.simulation_summary.text())
        self.assertEqual(self.controller.calls.count("simulate"), 1)

    def test_poll_refresh_does_not_launch_full_solver(self):
        self.launch()
        self.controller.mode = "connected"
        self.controller.poll_and_analyze = self.controller.analyze
        self.window._check_file_changed()
        self.wait_until(lambda: not self.window.busy and self.window.simulation_worker is None)
        self.assertEqual(self.controller.calls.count("simulate"), 1)
        self.assertEqual(self.window.result.board.name, "Board 2")
        self.assertIn("Click Analyze EMI", self.window.simulation_summary.text())

    def test_explicit_recheck_waits_for_cancelled_solver_then_runs_new_board(self):
        self.launch()
        self.window.analyze_button.click()
        self.wait_until(lambda: self.controller.calls.count("simulate") == 2)
        self.assertEqual(self.window.result.board.name, "Board 2")
        self.controller.release.set()
        self.wait_until(lambda: self.window.simulation_worker is None)
        self.assertEqual(self.window.simulations.results[0]["status"], "complete")

    def test_connect_and_open_both_include_simulation(self):
        self.controller.release.set()
        self.window.connect_kicad()
        self.wait_until(lambda: self.controller.calls.count("simulate") == 1 and self.window.simulation_worker is None)
        with patch("emi_assistant.ui.QFileDialog.getOpenFileName", return_value=("example.kicad_pcb", "")):
            self.window.open_board()
        self.wait_until(lambda: self.controller.calls.count("simulate") == 2 and self.window.simulation_worker is None)
        self.assertIn("connect", self.controller.calls)
        self.assertIn(("open", "example.kicad_pcb"), self.controller.calls)

    def test_solver_failure_retains_layout_and_has_plain_explanation(self):
        self.controller.raise_error = True
        self.controller.release.set()
        self.window.load_demo()
        self.wait_until(lambda: self.controller.calls.count("simulate") == 1 and self.window.simulation_worker is None)
        self.assertIsNotNone(self.window.result)
        self.assertIn("circuit solver could not start", self.window.simulations.status.text())
        self.assertFalse(self.window.error.isVisible())

    def test_close_cancels_solver_and_closes_when_finished(self):
        self.launch()
        self.window.close()
        self.wait_until(lambda: self.window.simulation_worker is None and not self.window.isVisible())
        self.assertTrue(self.controller.cancelled.is_set())

    def test_optional_context_preserves_settings_and_checks_frequency_order(self):
        board = self.controller.analyze().board
        self.controller.settings.setdefault("simulation", {})["max_jobs"] = 2
        dialog = ContextDialog(self.controller.settings, board, self.window)
        dialog.sim_fields["frequency_start_mhz"].setValue(700)
        with patch("emi_assistant.ui.QMessageBox.warning") as warning:
            dialog._validate()
            self.assertIn("stop frequency", warning.call_args.args[2])
        dialog.sim_fields["frequency_stop_mhz"].setValue(1000)
        dialog.schematic_path.setText("custom.kicad_sch")
        dialog.sim_openems.setChecked(False)
        dialog._validate()
        self.assertEqual(dialog.settings["simulation"]["max_jobs"], 2)
        self.assertEqual(dialog.settings["simulation"]["schematic_path"], "custom.kicad_sch")
        self.assertFalse(dialog.settings["simulation"]["engines"]["openems"])
        dialog.close()

    def test_response_chart_and_missing_models_are_visible_and_finite(self):
        pane = SimulationPane()
        result = simulation_result(state="needs_information")
        result["series"][0]["x"].append(float("nan"))
        result["series"][0]["y"].append(float("inf"))
        pane.set_results([result])
        pane.resize(800, 800)
        pane.show()
        self.app.processEvents()
        chart = pane.findChildren(ResponseChart)[0]
        self.assertTrue(chart.log_x)
        self.assertEqual(len(chart.series[0][1]), 4)
        self.assertFalse(chart.grab().isNull())
        texts = " ".join(widget.text() for widget in pane.findChildren(QLabel))
        self.assertIn("Assign U1's model", texts)
        self.assertIn("not", pane.findChildren(QTextBrowser)[0].toPlainText())
        pane.close()


if __name__ == "__main__":
    unittest.main()
