"""End-to-end application workflows on the bundled geometry examples."""
from pathlib import Path
import json
import tempfile
import unittest
from emi_assistant.controller import Controller

class WorkflowTests(unittest.TestCase):
    def test_demo_diagnose_exclude_restore_export(self):
        c = Controller()
        result = c.load_demo()
        categories = {f.rule for f in result.findings}
        self.assertTrue({'reference_gap', 'return_via', 'switching_loop', 'switch_copper'}.issubset(categories))
        original_count = len(result.findings)
        peak = c.diagnose(96)
        self.assertIn('harmonic 4', peak[0]['relationship'])
        finding = result.findings[0]
        reduced = c.ignore(finding, 'Intentional fixture condition')
        self.assertEqual(len(reduced.findings), original_count - 1)
        self.assertEqual(reduced.suppressed_count, 1)
        c.reset_ignored()
        self.assertEqual(len(c.result.findings), original_count)
        c.save_measurement('Example only', 96, 31, 'dBuV', 'Synthetic software fixture; no hardware measurement')
        with tempfile.TemporaryDirectory() as d:
            report = Path(d) / 'report.json'
            c.export_report(str(report))
            data = json.loads(report.read_text())
            self.assertEqual(len(data['findings']), original_count)
            self.assertEqual(data['measurements'][0]['frequency_mhz'], 96)
            self.assertIn('do not predict whole-board emissions', data['interpretation'].lower())
            self.assertEqual(data['simulations'], [])

    def test_improved_fixture_retains_honest_coverage(self):
        c = Controller()
        c.load_demo()
        improved = Path(c.path).with_name('clean.kicad_pcb')
        result = c.open_board(str(improved))
        self.assertEqual(result.findings, [])
        self.assertTrue(any(x['status'] == 'partial' for x in result.coverage))
        self.assertFalse(c.changes['has_baseline'])

    def test_poll_does_not_reanalyze_unchanged_board(self):
        c = Controller()
        result = c.load_demo()
        class Adapter:
            def snapshot(self):
                return result.board
        c._adapter = Adapter()
        c.mode = 'connected'
        self.assertIsNone(c.poll_and_analyze())
        self.assertIs(c.result, result)

if __name__ == '__main__':
    unittest.main()
