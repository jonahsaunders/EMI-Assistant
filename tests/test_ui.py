"""Offscreen behavior checks for the real Qt interface, without a KiCad process."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
import unittest

try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt, QTimer
    from emi_assistant.ui import MainWindow, ContextDialog, DiagnosisDialog
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False

from emi_assistant.models import AnalysisResult, BoardSnapshot, Finding, Track, CopperPolygon, Footprint, default_settings


def example():
    board = BoardSnapshot("Example board", "", "abc", tracks=[Track("track-1", (10, 20), (50, 20), .3, "F.Cu", "CLK")],
                          footprints=[Footprint("connector-1", "J1", "Connector", (8, 25))],
                          copper=[CopperPolygon("copper-1", "GND", "B.Cu", [(5, 5), (60, 5), (60, 50), (5, 50)])])
    findings = [Finding(f"finding-{i}", "return-path", f"Return path {i}", "high" if i == 0 else "medium", "medium",
                        "The trace crosses a break in reference copper.", "Route over continuous ground copper.",
                        ["track-1"], (30, 20), "F.Cu", ["A filled copper gap was observed."],
                        preview={"kind": "route", "points": [[10, 20], [15, 35], [45, 35], [50, 20]], "can_apply": False})
                for i in range(5)]
    return AnalysisResult(board, findings, [{"check": "Return paths", "status": "checked"}], elapsed_ms=51)


class FakeController:
    def __init__(self):
        self.result = None
        self.mode = "demo"
        self.settings = default_settings()
        self.calls = []
    def load_demo(self):
        self.calls.append("demo")
        self.result = example()
        return self.result
    def analyze(self):
        self.calls.append("analyze")
        return self.result or self.load_demo()
    def connect(self):
        self.calls.append("connect")
        self.mode = "connected"
        return self.load_demo()
    def locate(self, finding):
        self.calls.append(("locate", finding.id))
        return "Selected in KiCad"
    def diagnose(self, frequency):
        return [{"candidate": "CLK", "relationship": "2nd harmonic", "evidence": "Known frequency", "experiment": "Shift the clock and compare the peak."}]
    def import_spectrum(self, path):
        return [{"frequency_mhz": 50, "amplitude": 10}, {"frequency_mhz": 100, "amplitude": 20}]
    def save_measurement(self, *args):
        self.calls.append(("measurement", args))
        return "Measurement saved."
    def export_report(self, path):
        self.calls.append(("report", path))
    def reset_ignored(self):
        return self.result


@unittest.skipUnless(QT_AVAILABLE, "PySide6 Essentials is not installed")
class InterfaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.controller = FakeController()
        self.window = MainWindow(self.controller)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.wait_idle()
        self.window.close()
        self.app.processEvents()

    def wait_idle(self):
        deadline = time.monotonic()+3
        while self.window.busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertFalse(self.window.busy)

    def load(self):
        self.window.load_demo()
        self.wait_idle()

    def test_one_click_demo_prioritizes_three_and_reveals_all(self):
        self.assertEqual(self.window.pages.currentIndex(), 0)
        self.window.demo_button.click()
        self.wait_idle()
        self.assertEqual(self.window.pages.currentIndex(), 1)
        self.assertEqual(self.window.findings_list.count(), 3)
        self.assertEqual(self.window.finding.id, "finding-0")
        self.assertIn("What to change", self.window.detail.toPlainText())
        self.assertIn("medium confidence", self.window.detail.toPlainText())
        self.window.more_button.click()
        self.assertEqual(self.window.findings_list.count(), 5)

    def test_selection_changes_layer_and_preview_is_advisory(self):
        self.load()
        self.assertEqual(self.window.layers.currentText(), "F.Cu")
        self.assertEqual(self.window.canvas.finding.id, "finding-0")
        self.window.preview_button.click()
        self.assertTrue(self.window.canvas.preview)
        self.assertFalse(self.window.apply_button.isVisible())
        self.assertFalse(self.window.locate_button.isEnabled())
        self.window.findings_list.setCurrentRow(1)
        self.assertFalse(self.window.canvas.preview)
        self.assertEqual(self.window.preview_button.text(), "Highlight path to improve")

    def test_apply_requires_validated_capability_and_preview(self):
        self.controller.mode = "connected"
        result = example()
        result.findings[0].preview.update(kind="via", position=[31, 21], can_apply=True)
        self.window.show_result(result)
        self.assertTrue(self.window.apply_button.isVisible())
        self.assertFalse(self.window.apply_button.isEnabled())
        self.window.preview_button.click()
        self.assertTrue(self.window.apply_button.isEnabled())
        self.assertIn("Applying saves current project settings", self.window.detail.toPlainText())

    def test_worker_keeps_ui_responsive_serializes_and_recovers_error(self):
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start(5)
        def operation():
            time.sleep(.08)
            raise RuntimeError("Enable KiCad's IPC API, then retry.")
        self.window.run_job(operation, lambda _: None, "Working…")
        self.window.load_demo()  # Ignored while the single worker is active.
        self.wait_idle()
        timer.stop()
        self.assertGreater(len(ticks), 2)
        self.assertNotIn("demo", self.controller.calls)
        self.assertIn("Enable KiCad", self.window.error.text())
        self.assertTrue(self.window.analyze_button.isEnabled())
        self.load()
        self.assertFalse(self.window.error.isVisible())

    def test_connect_button_uses_connect_and_locates_current_finding(self):
        self.load()
        self.window.connect_button.click()
        self.wait_idle()
        self.assertIn("connect", self.controller.calls)
        self.assertTrue(self.window.locate_button.isEnabled())
        self.window.locate_button.click()
        self.wait_idle()
        self.assertIn(("locate", "finding-0"), self.controller.calls)

    def test_context_preserves_unrelated_settings_and_parses_units(self):
        self.load()
        self.controller.settings["external_connectors"] = ["J1"]
        dialog = ContextDialog(self.controller.settings, self.window.result.board, self.window)
        dialog._add_row(dialog.fast, ["CLK", "50", "1.2"])
        dialog._validate()
        self.assertEqual(dialog.settings["fast_nets"]["CLK"]["rise_ns"], 1.2)
        self.assertEqual(dialog.settings["external_connectors"], ["J1"])
        dialog.close()

    def test_context_preserves_disabled_signals_and_accepts_partial_regulator(self):
        self.load()
        self.controller.settings["fast_nets"] = {"CLK_UNUSED": {"enabled": False, "frequency_mhz": 50}}
        dialog = ContextDialog(self.controller.settings, self.window.result.board, self.window)
        dialog.grounds.setText("DGND, AGND")
        dialog._add_row(dialog.regs, ["U2", "buck", "", "", "", ""])
        dialog._validate()
        self.assertFalse(dialog.settings["fast_nets"]["CLK_UNUSED"]["enabled"])
        self.assertEqual(dialog.settings["fast_nets"]["CLK_UNUSED"]["frequency_mhz"], 50)
        self.assertEqual(dialog.settings["regulators"], [{"reference": "U2", "topology": "buck", "ground_net": "DGND"}])
        dialog.close()

    def test_context_validates_connector_and_fast_net_names(self):
        from unittest.mock import patch
        self.load()
        dialog = ContextDialog(self.controller.settings, self.window.result.board, self.window)
        dialog.connectors.setText("J99")
        with patch("emi_assistant.ui.QMessageBox.warning") as warning:
            dialog._validate()
            self.assertIn("J99", warning.call_args.args[2])
        dialog.connectors.setText("j1")
        dialog._add_fast_signal(["CLK_TYPO", "50", "1"])
        with patch("emi_assistant.ui.QMessageBox.warning") as warning:
            dialog._validate()
            self.assertIn("CLK_TYPO", warning.call_args.args[2])
        dialog.fast.item(dialog.fast.rowCount()-1, 0).setText("CLK")
        dialog._validate()
        self.assertEqual(dialog.settings["external_connectors"], ["J1"])
        dialog.close()

    def test_diagnosis_peak_selection_and_next_experiment(self):
        self.load()
        dialog = DiagnosisDialog(self.window)
        dialog._show_peaks(self.controller.import_spectrum("unused"))
        self.assertEqual(dialog.frequency.value(), 100)
        self.assertEqual(dialog.amplitude.value(), 20)
        dialog.diagnose()
        self.wait_idle()
        self.assertIn("Shift the clock", dialog.results.toPlainText())
        dialog.close()

    def test_evidence_is_available_without_overwhelming_default_action(self):
        self.load()
        self.assertNotIn("A filled copper gap was observed.", self.window.detail.toPlainText())
        self.window.details_button.click()
        self.assertIn("A filled copper gap was observed.", self.window.detail.toPlainText())
        self.assertEqual(self.window.canvas.finding.id, "finding-0")

    def test_auto_recheck_preserves_finding_layer_and_zoom(self):
        self.load()
        self.controller.mode = "connected"
        self.window.toggle_all()
        self.window.findings_list.setCurrentRow(4)
        self.window.canvas.scale(1.6, 1.6)
        zoom = self.window.canvas.transform().m11()
        updated = example()
        updated.board.fingerprint = "changed"
        self.controller.poll_and_analyze = lambda: updated
        self.window._check_file_changed()
        self.wait_idle()
        self.assertEqual(self.window.finding.id, "finding-4")
        self.assertEqual(self.window.layers.currentText(), "F.Cu")
        self.assertAlmostEqual(self.window.canvas.transform().m11(), zoom)
        self.assertIn("automatically updated", self.window.status.text())

    def test_auto_recheck_connection_failure_is_quiet_and_click_is_queued(self):
        self.load()
        self.controller.mode = "connected"
        def poll():
            time.sleep(.05)
            raise RuntimeError("KiCad is not responding.")
        self.controller.poll_and_analyze = poll
        self.window._check_file_changed()
        self.wait_idle()
        self.assertFalse(self.window.error.isVisible())
        self.assertIn("KiCad is not responding", self.window.status.text())
        self.window._check_file_changed()
        self.window.analyze()
        self.wait_idle()
        self.assertIn("analyze", self.controller.calls)

    def test_no_findings_leaves_coverage_and_context_available(self):
        result = example()
        result.findings = []
        self.window.show_result(result)
        self.assertIsNone(self.window.finding)
        self.assertFalse(self.window.preview_button.isEnabled())
        self.assertTrue(self.window.context_button.isEnabled())
        self.assertIn("coverage", self.window.detail.toPlainText())


if __name__ == "__main__":
    unittest.main()
