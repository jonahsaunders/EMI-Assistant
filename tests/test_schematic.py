"""Saved context must not silently attach a stale symbol to a PCB footprint."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from emi_assistant.models import BoardSnapshot, Footprint, Pad
from emi_assistant.schematic import enrich


def symbol(reference='U1', value='PART', uuid='u1', lib_id='Logic:PART', unit='1',
           instance_path='/root', project='board', extra='', modern=True):
    instances = (f'(instances (project "{project}" (path "{instance_path}" '
                 f'(reference "{reference}") (unit {unit}))))') if modern else ''
    return (f'(symbol (lib_id "{lib_id}") (uuid "{uuid}") (unit {unit}) (on_board yes) '
            f'(property "Reference" "{reference}") (property "Value" "{value}") '
            f'{extra} {instances})')


def schematic(items='', libraries='', root='root', extra=''):
    return (f'(kicad_sch (version 20250114) (uuid "{root}") '
            f'(lib_symbols {libraries}) {items} {extra})')


def library(name='Logic:PART'):
    return (f'(symbol "{name}" (symbol "PART_1_1" '
            '(pin input line (name "IN") (number "1")) '
            '(pin power_in line (name "VCC") (number "2"))) '
            '(symbol "PART_2_1" (pin power_in line (name "VEE") (number "3"))) '
            '(symbol "PART_0_1" (pin power_in line (name "GND") (number "4"))))')


class SchematicTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        pads = [Pad('p' + str(n), str(n), (n, 0), (1, 1), ['F.Cu'], net, 'fp1')
                for n, net in ((1, 'CLK'), (2, 'VCC'), (3, 'VEE'), (4, 'GND'))]
        self.fp = Footprint('fp1', 'U1', 'PART', (0, 0), pads,
                            {'Value': 'PART', '__kicad_path': '/root/u1'})
        self.board = BoardSnapshot('board', str(self.path / 'board.kicad_pcb'), 'unchanged',
                                   footprints=[self.fp])

    def write(self, text, name='board.kicad_sch'):
        (self.path / name).write_text(text, encoding='utf-8')

    def test_absent_schematic_is_optional_and_untitled_is_supported(self):
        self.assertEqual(enrich(self.board), [])
        self.board.path = ''
        self.assertEqual(enrich(self.board), [])

    def test_matched_path_and_pin_context_preserve_board_data(self):
        self.write(schematic(symbol(), library()))
        nets = [p.net for p in self.fp.pads]
        warnings = enrich(self.board)
        self.assertEqual(self.fp.properties['__schematic_match'], 'path')
        self.assertEqual(json.loads(self.fp.properties['__schematic_power_input_pins']), ['2', '4'])
        pins = json.loads(self.fp.properties['__schematic_pins'])
        self.assertNotIn('3', pins)  # Other symbol unit is not this instance.
        self.assertEqual(pins['2'], {'name': 'VCC', 'type': 'power_in'})
        self.assertEqual([p.net for p in self.fp.pads], nets)
        self.assertEqual(self.fp.value, 'PART')
        self.assertEqual(self.board.fingerprint, 'unchanged')
        self.assertTrue(any('Unsaved' in warning for warning in warnings))

    def test_pin_context_does_not_create_absent_board_pins(self):
        self.fp.pads = self.fp.pads[:1]
        self.write(schematic(symbol(), library()))
        enrich(self.board)
        self.assertEqual(json.loads(self.fp.properties['__schematic_power_input_pins']), [])

    def test_different_uuid_with_same_reference_is_stale(self):
        self.write(schematic(symbol(uuid='replacement'), library()))
        self.assertTrue(any('UUID/path' in warning for warning in enrich(self.board)))
        self.assertNotIn('__schematic_pins', self.fp.properties)

    def test_value_conflict_does_not_overwrite_live_value_or_properties(self):
        self.write(schematic(symbol(value='NEWPART'), library()))
        self.assertTrue(any('value differs' in warning for warning in enrich(self.board)))
        self.assertEqual(self.fp.value, 'PART')
        self.assertEqual(self.fp.properties['Value'], 'PART')
        self.assertNotIn('__schematic_lib_id', self.fp.properties)

    def test_unique_reference_fallback_is_marked(self):
        del self.fp.properties['__kicad_path']
        self.write(schematic(symbol(), library()))
        enrich(self.board)
        self.assertEqual(self.fp.properties['__schematic_match'], 'unique reference and value')

    def test_duplicate_reference_does_not_enrich(self):
        self.write(schematic(symbol() + symbol(uuid='u1other'), library()))
        self.assertTrue(any('Duplicate' in warning for warning in enrich(self.board)))
        self.assertNotIn('__schematic_pins', self.fp.properties)

    def test_multiunit_component_merges_selected_units(self):
        self.write(schematic(symbol() + symbol(uuid='u1b', unit='2'), library()))
        enrich(self.board)
        self.assertEqual(json.loads(self.fp.properties['__schematic_power_input_pins']), ['2', '3', '4'])

    def test_hierarchy_uses_instance_path_and_explicit_project_reference(self):
        self.write(schematic('(sheet (uuid "sheet1") (property "Sheetfile" "child.kicad_sch"))'))
        self.write(schematic(symbol(instance_path='/root/sheet1'), library(), root='childroot'), 'child.kicad_sch')
        self.fp.properties['__kicad_path'] = '/root/sheet1/u1'
        enrich(self.board)
        self.assertEqual(self.fp.properties['__schematic_source'], 'child.kicad_sch')
        self.assertEqual(self.fp.properties['__schematic_match'], 'path')

    def test_no_matching_instance_does_not_use_displayed_reference(self):
        self.write(schematic(symbol(project='different-project'), library()))
        self.assertTrue(any('missing or ambiguous' in warning for warning in enrich(self.board)))
        self.assertNotIn('__schematic_pins', self.fp.properties)

    def test_old_symbol_instance_annotation_supported(self):
        extra = '(symbol_instances (path "/u1" (reference "U1") (unit 1) (value "PART")))'
        self.write(schematic(symbol(reference='U?', modern=False), library(), extra=extra))
        enrich(self.board)
        self.assertIn('__schematic_pins', self.fp.properties)

    def test_missing_and_outside_sheets_warn_without_discovery(self):
        sheets = ('(sheet (uuid "a") (property "Sheet file" "missing.kicad_sch")) '
                  '(sheet (uuid "b") (property "Sheet file" "../outside.kicad_sch"))')
        self.write(schematic(sheets))
        warnings = enrich(self.board)
        self.assertTrue(any('missing.kicad_sch' in w and 'could not be loaded' in w for w in warnings))
        self.assertTrue(any('outside the PCB project' in w for w in warnings))

    def test_symlink_outside_project_is_not_read(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            target = Path(elsewhere) / 'outside.kicad_sch'
            target.write_text(schematic(symbol(), library()), encoding='utf-8')
            (self.path / 'board.kicad_sch').symlink_to(target)
            warnings = enrich(self.board)
            self.assertTrue(any('outside the PCB project' in w for w in warnings))
            self.assertNotIn('__schematic_pins', self.fp.properties)

    def test_hierarchy_cycle_is_bounded(self):
        self.write(schematic('(sheet (uuid "cycle") (property "Sheetfile" "board.kicad_sch"))'))
        self.assertTrue(any('cycle' in w for w in enrich(self.board)))

    def test_explicit_frequency_and_role_are_sourced_without_net_changes(self):
        self.fp.value = '16 MHz'
        self.write(schematic(symbol(value='16 MHz', lib_id='Oscillator:Generic'), library('Oscillator:Generic')))
        enrich(self.board)
        self.assertEqual(self.fp.properties['__schematic_role'], 'oscillator')
        self.assertEqual(self.fp.properties['__schematic_frequency_mhz'], '16')
        self.assertEqual(self.fp.properties['__schematic_frequency_source'], 'symbol value')
        self.assertEqual(self.fp.pads[0].net, 'CLK')

    def test_unitless_or_nonfinite_frequency_is_not_inferred(self):
        for frequency in ('16000000', '1e999 MHz', '16 MHz or 32 MHz'):
            self.write(schematic(symbol(extra=f'(property "Frequency" "{frequency}")'), library()))
            enrich(self.board)
            self.assertNotIn('__schematic_frequency_mhz', self.fp.properties)

    def test_frequency_property_is_namespaced_and_provenance_is_kept(self):
        self.write(schematic(symbol(extra='(property "Frequency" "32 kHz")'), library()))
        enrich(self.board)
        self.assertNotIn('Frequency', self.fp.properties)
        self.assertEqual(self.fp.properties['__schematic_frequency_mhz'], '0.032')
        self.assertEqual(self.fp.properties['__schematic_frequency_source'], 'property: Frequency')

    def test_conflicting_frequency_property_warns_and_cannot_override_live(self):
        self.fp.properties['Frequency'] = '100 MHz'
        self.write(schematic(symbol(extra='(property "Frequency" "32 kHz")'), library()))
        self.assertTrue(any('differing PCB' in w for w in enrich(self.board)))
        self.assertEqual(self.fp.properties['Frequency'], '100 MHz')
        self.assertNotIn('__schematic_frequency_mhz', self.fp.properties)

    def test_renamed_reference_with_matching_uuid_warns(self):
        self.write(schematic(symbol(reference='U2'), library()))
        self.assertTrue(any('named U2' in w for w in enrich(self.board)))
        self.assertNotIn('__schematic_pins', self.fp.properties)

    def test_duplicate_legacy_instance_path_does_not_silently_choose_one(self):
        extra = ('(symbol_instances (path "/u1" (reference "U1") (unit 1) (value "PART")) '
                 '(path "/u1" (reference "U1") (unit 1) (value "PART")))')
        self.write(schematic(symbol(modern=False), library(), extra=extra))
        self.assertTrue(any('Duplicate saved' in w for w in enrich(self.board)))
        self.assertNotIn('__schematic_pins', self.fp.properties)

    def test_reanalysis_clears_old_context_when_schematic_removed(self):
        self.write(schematic(symbol(), library()))
        enrich(self.board)
        (self.path / 'board.kicad_sch').unlink()
        enrich(self.board)
        self.assertNotIn('__schematic_pins', self.fp.properties)
        self.assertIn('__kicad_path', self.fp.properties)

    def test_malformed_and_large_input_fail_gracefully(self):
        self.write('(kicad_sch')
        self.assertTrue(any('unavailable' in w for w in enrich(self.board)))
        self.write(schematic(symbol(), library()))
        with patch('emi_assistant.schematic.MAX_FILE_BYTES', 10):
            self.assertTrue(any('size limit' in w for w in enrich(self.board)))


if __name__ == '__main__':
    unittest.main()
