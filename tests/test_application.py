"""Application-level validation independent of a running KiCad instance."""
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from emi_assistant.models import AnalysisResult, BoardSnapshot, Finding, Footprint
from emi_assistant.diagnostics import diagnose, import_spectrum
from emi_assistant.storage import load_settings, validate_settings, write_json
from emi_assistant.report import export_report
from emi_assistant.controller import Controller

class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.board = BoardSnapshot("board", "", "abc")

    def test_known_harmonic_is_explicitly_a_hypothesis(self):
        result = diagnose(self.board, {"fast_nets": {"CLK": {"frequency_mhz": 24}}}, 96)
        self.assertEqual(result[0]["candidate"], "CLK")
        self.assertIn("harmonic 4", result[0]["relationship"])
        self.assertIn("hypothesis", result[0]["evidence"])
        self.assertIn("change", result[0]["experiment"])

    def test_disabled_sources_and_unrelated_peaks(self):
        result = diagnose(self.board, {"fast_nets": {"CLK": {"frequency_mhz": 24, "enabled": False}}}, 96)
        self.assertEqual(result[0]["candidate"], "No matching known clock")
        self.assertEqual(diagnose(self.board, {"fast_nets": {"CLK": {"frequency_mhz": 24}}}, 37)[0]["candidate"], "No matching known clock")

    def test_frequency_inference_only_for_oscillator_components(self):
        self.board.footprints = [Footprint("1", "Y1", "16 MHz", (0, 0)), Footprint("2", "R1", "24 MHz", (1, 0))]
        result = diagnose(self.board, {}, 48)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["candidate"].startswith("Y1"))
        self.assertIn("inferred", result[0]["evidence"])

    def test_nonfinite_and_zero_frequency_rejected(self):
        for value in (math.nan, math.inf, 0, -1):
            with self.assertRaises(ValueError):
                diagnose(self.board, {}, value)

    def test_csv_units_and_bad_rows(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "scan.csv"
            path.write_text("frequency_hz,amplitude\n96000000,42\n24000000,28\n")
            rows = import_spectrum(str(path))
            self.assertEqual([r["frequency_mhz"] for r in rows], [24, 96])
            path.write_text("frequency,amplitude\n96,42\n")
            with self.assertRaisesRegex(ValueError, "headers"):
                import_spectrum(str(path))
            path.write_text("frequency_mhz,amplitude\n96,nan\n")
            with self.assertRaisesRegex(ValueError, "row 2"):
                import_spectrum(str(path))

class PersistenceTests(unittest.TestCase):
    def test_invalid_context_does_not_overwrite_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "board.emi.json"
            path.write_text('{"broken":')
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                load_settings(path)
            self.assertEqual(path.read_bytes(), before)

    def test_atomic_json_replacement_rejects_nan_before_write(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "board.emi.json"
            write_json(path, {"valid": 1})
            with self.assertRaises(ValueError):
                write_json(path, {"invalid": float("nan")})
            self.assertEqual(json.loads(path.read_text()), {"valid": 1})

    def test_context_numbers_and_schema(self):
        self.assertEqual(validate_settings({"fast_nets": {"CLK": {"frequency_mhz": "24"}}})["fast_nets"]["CLK"]["frequency_mhz"], 24)
        for bad in [{"schema_version": 2}, {"fast_nets": {"CLK": {"rise_ns": 0}}}, {"reference_layers": {"F.Cu": "In1.Cu"}}, {"thresholds": {"return_via_mm": -2}}]:
            with self.assertRaises(ValueError):
                validate_settings(bad)

    def test_html_report_escapes_board_and_measurement_text(self):
        board = BoardSnapshot('<script>alert(1)</script>', '', 'abc')
        finding = Finding('f', 'return', '<b>finding</b>', 'High', 'Medium', 'why', 'fix', ['item'], (1, 2), evidence=['<script>bad</script>'])
        result = AnalysisResult(board, [finding])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'report.html'
            export_report(result, str(path), measurements=[{'notes': '<img src=x onerror=bad>'}])
            html = path.read_text()
            self.assertNotIn('<script>', html)
            self.assertNotIn('<img ', html)
            self.assertIn('&lt;script&gt;', html)

class ControllerTests(unittest.TestCase):
    def test_failed_open_preserves_previous_project_and_write_target(self):
        with tempfile.TemporaryDirectory() as d:
            first = Path(d) / 'a.kicad_pcb'
            second = Path(d) / 'b.kicad_pcb'
            first.write_text('(kicad_pcb (version 20241229) (generator test) (layers (0 "F.Cu" signal) (31 "B.Cu" signal)))')
            second.write_bytes(first.read_bytes())
            bad = second.with_suffix('.emi.json')
            bad.write_text('{bad')
            c = Controller()
            c.open_board(str(first))
            original_result = c.result
            with self.assertRaises(ValueError):
                c.open_board(str(second))
            self.assertIs(c.result, original_result)
            self.assertEqual(c.path, str(first))
            self.assertEqual(c._settings_path, first.with_suffix('.emi.json'))
            c.update_settings(c.settings)
            self.assertEqual(bad.read_text(), '{bad')

    def test_stale_findings_cannot_apply_or_locate_wrong_revision(self):
        class Adapter:
            def snapshot(self):
                return BoardSnapshot('board', '', 'new')
            def locate(self, finding):
                raise AssertionError('must not locate stale data')
        controller = Controller(Adapter())
        finding = Finding('f', 'return', 'title', 'High', 'Medium', 'why', 'fix', ['id'], (0, 0))
        controller.result = AnalysisResult(BoardSnapshot('board', '', 'old'), [finding])
        with self.assertRaisesRegex(RuntimeError, 'board changed'):
            controller.locate(finding)

    def test_report_and_measurements_require_analysis(self):
        c = Controller()
        with self.assertRaises(RuntimeError):
            c.diagnose(96)
        with self.assertRaises(RuntimeError):
            c.export_report('should-not-exist.json')

    def test_demo_measurements_do_not_write_plugin_files(self):
        c = Controller()
        c.mode = 'demo'
        c.result = AnalysisResult(BoardSnapshot('demo', '', 'abc'), [])
        c.save_measurement('baseline', 96, 42, 'dBuV', 'Same probe position')
        self.assertEqual(len(c.measurements), 1)
        self.assertEqual(c.measurements[0]['board_fingerprint'], 'abc')

if __name__ == '__main__':
    unittest.main()
