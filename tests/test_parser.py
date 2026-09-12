"""Geometry regression tests: errors here can reverse an EMI recommendation."""
import math
from pathlib import Path
import tempfile
import unittest

from emi_assistant.parser import BoardParseError, load_board, parse_board


def pcb(items='', layers='(0 "F.Cu" signal) (31 "B.Cu" signal)', nets='(net 0 "") (net 1 "GND") (net 2 "CLK")'):
    return f'(kicad_pcb (version 20240108) (generator "emi_assistant_tests") (layers {layers}) {nets} {items})'


def track(start='0 0', end='10 0', layer='F.Cu', net='2', identifier='track-1'):
    return f'(segment (start {start}) (end {end}) (width 0.2) (layer "{layer}") (net {net}) (uuid "{identifier}"))'


def zone(fill=True):
    fill_text = '(filled_polygon (layer "B.Cu") (pts (xy 1 1) (xy 9 1) (xy 9 9) (xy 1 9)))' if fill else ''
    return f'(zone (net 1) (net_name "GND") (layer "B.Cu") (uuid "zone-1") (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10))) {fill_text})'


class ParserTests(unittest.TestCase):
    def test_actual_filled_copper_not_editable_boundary(self):
        board = parse_board(pcb(zone()))
        self.assertEqual(board.copper[0].points, [(1., 1.), (9., 1.), (9., 9.), (1., 9.)])
        self.assertEqual(board.copper[0].net, 'GND')
        self.assertEqual(board.copper[0].id, 'zone-1')
        self.assertTrue(board.copper[0].is_filled)

    def test_missing_fills_cannot_be_clean_plane(self):
        board = parse_board(pcb(zone(False)))
        self.assertEqual(board.copper, [])
        self.assertTrue(any('no saved filled copper' in warning for warning in board.warnings))

    def test_escaped_strings_and_parentheses_are_data(self):
        board = parse_board(pcb(r'(footprint "test" (layer "F.Cu") (at 0 0) (property "Reference" "U1") (property "Value" "IC \"name\" (a) \\path\nline"))'))
        self.assertEqual(board.footprints[0].value, 'IC "name" (a) \\path\nline')
        self.assertEqual(board.footprints[0].reference, 'U1')

    def test_legacy_footprint_fields(self):
        board = parse_board(pcb('(footprint "chip" (at 10 20) (fp_text reference U9) (fp_text value PART))'))
        self.assertEqual((board.footprints[0].reference, board.footprints[0].value), ('U9', 'PART'))

    def test_rotated_pad_position_uses_kicad_screen_coordinates(self):
        footprint = '(footprint "chip" (at 10 20 90) (layer "F.Cu") (property "Reference" "U1") (pad "1" smd rect (at 2 3 90) (size 1 2) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "CLK") (uuid "p1")))'
        board = parse_board(pcb(footprint))
        pad = board.pads[0]
        self.assertAlmostEqual(pad.position[0], 13)
        self.assertAlmostEqual(pad.position[1], 18)
        self.assertEqual(pad.layers, ['F.Cu'])

    def test_backside_pad_saved_coordinates_are_not_mirrored_twice(self):
        footprint = '(footprint "back" (at 10 20 90) (layer "B.Cu") (pad "1" smd rect (at -2 3 90) (size 1 1) (layers "B.Cu" "B.Paste" "B.Mask") (net 1)))'
        pad = parse_board(pcb(footprint)).pads[0]
        self.assertAlmostEqual(pad.position[0], 13)
        self.assertAlmostEqual(pad.position[1], 22)
        self.assertEqual(pad.layers, ['B.Cu'])

    def test_layer_order_is_physical_not_lexical_or_enum(self):
        # KiCad 10 enums need not be in physical order; In10 must follow In2.
        layers = '(0 "F.Cu" signal) (2 "B.Cu" signal) (4 "In1.Cu" power) (6 "In2.Cu" power) (22 "In10.Cu" signal)'
        board = parse_board(pcb(layers=layers))
        self.assertEqual(board.layers, ['F.Cu', 'In1.Cu', 'In2.Cu', 'In10.Cu', 'B.Cu'])

    def test_via_endpoints_expand_internal_layers_and_blind_span(self):
        layers = '(0 "F.Cu" signal) (1 "In1.Cu" power) (2 "In2.Cu" power) (31 "B.Cu" signal)'
        vias = '(via (at 2 3) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1)) (via blind (at 4 3) (size 0.6) (drill 0.3) (layers "In1.Cu" "B.Cu") (net 2))'
        board = parse_board(pcb(vias, layers=layers))
        self.assertEqual(board.vias[0].layers, board.layers)
        self.assertEqual(board.vias[1].layers, ['In1.Cu', 'In2.Cu', 'B.Cu'])

    def test_through_hole_layers_and_non_plated_holes(self):
        fp = '(footprint "connector" (at 0 0) (pad "1" thru_hole circle (at 0 0) (size 2 2) (drill 1) (layers "*.Cu" "*.Mask") (net 1)) (pad "" np_thru_hole circle (at 5 0) (size 3 3) (drill 3) (layers "*.Cu" "*.Mask")))'
        board = parse_board(pcb(fp))
        self.assertEqual(len(board.pads), 1)
        self.assertEqual(board.pads[0].layers, ['F.Cu', 'B.Cu'])

    def test_arc_flattens_curve_keeps_source_id_and_endpoints(self):
        board = parse_board(pcb('(arc (start 1 0) (mid 0 1) (end -1 0) (width 0.2) (layer "F.Cu") (net 2) (uuid "arc-source"))'))
        self.assertGreater(len(board.tracks), 5)
        self.assertEqual({t.id for t in board.tracks}, {'arc-source'})
        self.assertEqual(board.tracks[0].start, (1., 0.))
        self.assertEqual(board.tracks[-1].end, (-1., 0.))
        self.assertGreater(max(t.end[1] for t in board.tracks), .99)
        length = sum(math.dist(t.start, t.end) for t in board.tracks)
        self.assertAlmostEqual(length, math.pi, delta=.02)
        self.assertTrue(any('flattened' in x for x in board.warnings))

    def test_arc_clockwise_and_major_sweep(self):
        board = parse_board(pcb('(arc (start 1 0) (mid -1 0) (end 0 1) (width 0.2) (layer "F.Cu") (net 2))'))
        self.assertLess(min(t.end[1] for t in board.tracks), -.99)
        self.assertGreater(sum(math.dist(t.start, t.end) for t in board.tracks), 4.6)

    def test_outline_rect_and_rotated_footprint_lines(self):
        items = '(gr_rect (start 0 0) (end 20 10) (layer "Edge.Cuts")) (footprint "slot" (at 10 20 90) (fp_line (start 0 0) (end 4 0) (layer "Edge.Cuts")))'
        board = parse_board(pcb(items))
        self.assertEqual(len(board.outline), 5)
        self.assertEqual(board.outline[0], ((0., 0.), (20., 0.)))
        self.assertAlmostEqual(board.outline[-1][1][1], 16)

    def test_stackup_thickness_and_material(self):
        board = parse_board(pcb('(setup (stackup (layer "F.Cu" (type "copper") (thickness 0.035)) (layer "dielectric 1" (type "prepreg") (material "FR4") (thickness 0.2) (epsilon_r 4.1))))'))
        self.assertEqual(board.stackup[1]['material'], 'FR4')
        self.assertEqual(board.stackup[1]['thickness'], .2)

    def test_named_nets_and_display_layer_alias(self):
        board = parse_board(pcb(track(layer='Top signal', net='"CLK"'), layers='(0 "F.Cu" signal "Top signal") (31 "B.Cu" signal)', nets='(net "GND") (net "CLK")'))
        self.assertEqual(board.tracks[0].net, 'CLK')
        self.assertEqual(board.tracks[0].layer, 'F.Cu')

    def test_unknown_net_reports_missing_connectivity(self):
        board = parse_board(pcb(track(net='99')))
        self.assertIn('unknown net 99', board.tracks[0].net)
        self.assertTrue(any('Unknown net code 99' in w for w in board.warnings))

    def test_missing_fill_on_one_layer_is_explicit(self):
        board = parse_board(pcb('(zone (net 1) (layers "F.Cu" "B.Cu") (filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 1 0) (xy 0 1))))'))
        self.assertTrue(any('no saved fill on B.Cu' in w for w in board.warnings))

    def test_new_unknown_construct_cannot_disappear_silently(self):
        board = parse_board(pcb('(future_copper_shape (layer "F.Cu"))'))
        self.assertTrue(any('future_copper_shape' in w for w in board.warnings))

    def test_schematic_link_path_is_preserved(self):
        board = parse_board(pcb('(footprint "chip" (at 0 0) (property "Reference" "U1") (path "/sheet-id/symbol-id"))'))
        self.assertEqual(board.footprints[0].properties['__kicad_path'], '/sheet-id/symbol-id')

    def test_bridged_hole_contour_vertex_order_is_preserved(self):
        pts = '(xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10) (xy 0 5) (xy 4 5) (xy 4 6) (xy 6 6) (xy 6 4) (xy 4 4) (xy 4 5) (xy 0 5)'
        board = parse_board(pcb(f'(zone (net 1) (layer "B.Cu") (filled_polygon (layer "B.Cu") (pts {pts})))'))
        self.assertEqual(len(board.copper[0].points), 12)
        self.assertEqual(board.copper[0].points[5], board.copper[0].points[10])

    def test_keepouts_do_not_become_reference_copper(self):
        board = parse_board(pcb('(zone (net 0) (layer "F.Cu") (keepout (tracks not_allowed)) (polygon (pts (xy 0 0) (xy 1 0) (xy 0 1))))'))
        self.assertEqual(board.copper, [])
        self.assertFalse(any('Zone ' in x and 'no saved' in x for x in board.warnings))

    def test_unsupported_electrical_geometry_is_explicit(self):
        items = '(gr_poly (pts (xy 0 0) (xy 1 0) (xy 1 1)) (layer "F.Cu")) (footprint "custom" (at 0 0) (property "Reference" "U1") (pad "1" smd custom (at 0 0) (size 1 1) (layers "F.Cu") (net 1)))'
        board = parse_board(pcb(items))
        self.assertTrue(any('Copper graphic' in x for x in board.warnings))
        self.assertTrue(any('custom/padstack' in x for x in board.warnings))

    def test_malformed_and_nonfinite_inputs_fail(self):
        cases = ['', '(kicad_sch)', '(kicad_pcb', '(kicad_pcb))', '(kicad_pcb) (kicad_pcb)', '(kicad_pcb "unclosed)', pcb(track(start='nan 0')), pcb(track(start='inf 0')), pcb(track(net='2') + '(net 2 "OTHER")'), pcb('(segment (end 0 1) (width 0.2) (layer "F.Cu"))'), pcb('(arc (start 0 0) (mid 1 0) (end 2 0) (width 0.2) (layer "F.Cu"))')]
        for text in cases:
            with self.subTest(text=text[:90]):
                with self.assertRaises(BoardParseError):
                    parse_board(text)

    def test_stable_fingerprint_and_fallback_identifiers(self):
        text = pcb('(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 2))')
        a, b = parse_board(text), parse_board(text)
        self.assertEqual(a.fingerprint, b.fingerprint)
        self.assertEqual(a.tracks[0].id, b.tracks[0].id)
        self.assertNotEqual(a.fingerprint, parse_board(text + '\n').fingerprint)

    def test_file_errors_and_utf8_bom(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'unicode.kicad_pcb'
            path.write_text('\ufeff' + pcb(track()), encoding='utf-8')
            self.assertEqual(load_board(str(path)).name, 'unicode')
            with self.assertRaises(BoardParseError):
                load_board(str(Path(folder) / 'missing.kicad_pcb'))
            path.write_bytes(b'\xff\xff')
            with self.assertRaises(BoardParseError):
                load_board(str(path))

    def test_bundled_demo_and_clean_have_real_fills_and_stable_ids(self):
        examples = Path(__file__).resolve().parents[1] / 'examples'
        for name in ('demo', 'clean'):
            with self.subTest(name=name):
                board = load_board(str(examples / f'{name}.kicad_pcb'))
                self.assertGreater(len(board.tracks), 3)
                self.assertGreater(len(board.copper), 1)
                self.assertEqual(board.layers, ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'])
                self.assertTrue(all(not x.id.startswith('generated-') for x in board.tracks + board.vias + board.pads))


if __name__ == '__main__':
    unittest.main()
