import copy
import json
import unittest

from emi_assistant.engine import analyze
from emi_assistant.geometry import CopperIndex, RouteGraph, polygon_geometry
from emi_assistant.models import BoardSnapshot, CopperPolygon, Footprint, Pad, Track, Via


def copper(identifier="plane", layer="B.Cu", net="GND", bounds=(0, 0, 20, 20), holes=None, filled=True):
    x0, y0, x1, y1 = bounds
    return CopperPolygon(identifier, net, layer, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], holes or [], filled)


def board(**kwargs):
    return BoardSnapshot("Test", "", "fingerprint", **kwargs)


def fast_settings(**extra):
    return {"fast_nets": {"CLK": {"rise_ns": 1}}, "reference_layers": {"F.Cu": ["B.Cu"]}, **extra}


def rule(result, name):
    return [finding for finding in result.findings if finding.rule == name]


def pad(identifier, number, x, y, net, layers=None, size=(1, 1)):
    return Pad(identifier, str(number), (x, y), size, layers or ["F.Cu"], net)


class FilledCopperChecks(unittest.TestCase):
    def test_actual_hole_detected_and_zone_outline_never_substituted(self):
        trace = Track("clock", (2, 10), (18, 10), 0.2, "F.Cu", "CLK")
        hole = [(8, 5), (12, 5), (12, 15), (8, 15)]
        result = analyze(board(tracks=[trace], copper=[copper(holes=[hole])]), fast_settings())
        self.assertEqual(len(rule(result, "reference_gap")), 1)
        self.assertIn("3.99 mm", rule(result, "reference_gap")[0].summary)
        outline_only = analyze(board(tracks=[trace], copper=[copper(filled=False)]), fast_settings())
        self.assertFalse(rule(outline_only, "reference_gap"))
        coverage = next(c for c in outline_only.coverage if c["rule"] == "reference_gap")
        self.assertEqual(coverage["status"], "partial")
        self.assertIn("1 lacked", coverage["detail"])

    def test_connected_filled_reference_has_no_gap(self):
        result = analyze(board(tracks=[Track("clock", (2, 10), (18, 10), .2, "F.Cu", "CLK")],
                               copper=[copper()]), fast_settings())
        self.assertFalse(rule(result, "reference_gap"))

    def test_bridge_encoded_kicad_hole_remains_empty(self):
        # A repeated bridge carries the inner contour into the outer contour.
        points = [(0, 0), (20, 0), (20, 20), (0, 20), (0, 0),
                  (8, 8), (8, 12), (12, 12), (12, 8), (8, 8), (0, 0)]
        plane = CopperPolygon("bridged", "GND", "B.Cu", points)
        from shapely.geometry import Point
        shape = polygon_geometry(plane)
        self.assertFalse(shape.covers(Point(10, 10)))
        self.assertTrue(shape.covers(Point(2, 2)))

    def test_different_ground_domains_not_unioned_to_hide_gap(self):
        result = analyze(board(tracks=[Track("clock", (2, 10), (18, 10), .2, "F.Cu", "CLK")],
                               copper=[copper("a", bounds=(0, 0, 10, 20)),
                                       copper("b", net="AGND", bounds=(10, 0, 20, 20))]),
                         fast_settings(reference_nets=["GND", "AGND"]))
        self.assertTrue(rule(result, "reference_gap"))

    def test_disabled_inferred_fast_net_is_respected(self):
        pcb = board(tracks=[Track("clock", (2, 10), (18, 10), .2, "F.Cu", "CLK")],
                    copper=[copper(bounds=(0, 0, 3, 20))])
        self.assertTrue(rule(analyze(pcb), "reference_gap"))
        self.assertFalse(rule(analyze(pcb, {"fast_nets": {"CLK": {"enabled": False}}}), "reference_gap"))


class LayerTransitionChecks(unittest.TestCase):
    def setUp(self):
        self.pcb = board(layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
                         tracks=[Track("front", (2, 10), (10, 10), .2, "F.Cu", "CLK"),
                                 Track("back", (10, 10), (18, 10), .2, "B.Cu", "CLK")],
                         vias=[Via("signal", (10, 10), .6, .3, ["F.Cu", "B.Cu"], "CLK")],
                         copper=[copper("inner1", "In1.Cu"), copper("inner2", "In2.Cu")])
        self.settings = fast_settings(reference_layers={"F.Cu": ["In1.Cu"], "B.Cu": ["In2.Cu"]})

    def test_missing_return_and_preview_are_advisory(self):
        findings = rule(analyze(self.pcb, self.settings), "return_via")
        self.assertEqual(len(findings), 1)
        preview = findings[0].preview
        self.assertEqual(preview["reference_layers"], ["In1.Cu", "In2.Cu"])
        self.assertEqual(preview["layers"], ["F.Cu", "B.Cu"])
        self.assertFalse(preview["drc_checked"])
        self.assertTrue(preview["advisory"])

    def test_through_via_spans_internal_planes_but_blind_via_does_not(self):
        self.pcb.vias.append(Via("return", (10, 11), .6, .3, ["F.Cu", "In1.Cu"], "GND"))
        self.assertTrue(rule(analyze(self.pcb, self.settings), "return_via"))
        self.pcb.vias[-1].layers = ["F.Cu", "B.Cu"]
        self.assertFalse(rule(analyze(self.pcb, self.settings), "return_via"))

    def test_wrong_net_via_cannot_supply_ground_return(self):
        self.pcb.vias.append(Via("return", (10, 11), .6, .3, ["F.Cu", "B.Cu"], "AGND"))
        self.assertTrue(rule(analyze(self.pcb, self.settings), "return_via"))

    def test_same_net_via_on_disconnected_island_cannot_count(self):
        self.pcb.copper = [copper("left1", "In1.Cu", bounds=(0, 0, 10.2, 20)),
                           copper("right1", "In1.Cu", bounds=(10.8, 0, 20, 20)),
                           copper("left2", "In2.Cu", bounds=(0, 0, 10.2, 20)),
                           copper("right2", "In2.Cu", bounds=(10.8, 0, 20, 20))]
        self.pcb.vias.append(Via("return", (11.3, 10), .6, .3, ["F.Cu", "B.Cu"], "GND"))
        self.assertTrue(rule(analyze(self.pcb, self.settings), "return_via"))

    def test_same_net_via_without_plane_contact_cannot_count(self):
        hole = [(9, 10.5), (11, 10.5), (11, 12.5), (9, 12.5)]
        self.pcb.copper[1].holes = [hole]
        self.pcb.vias.append(Via("return", (10, 11.5), .6, .3, ["F.Cu", "B.Cu"], "GND"))
        self.assertTrue(rule(analyze(self.pcb, self.settings), "return_via"))

    def test_plated_ground_pad_counts_and_smd_pad_does_not(self):
        ground = pad("j1g", 1, 10, 11, "GND", ["F.Cu"])
        self.pcb.footprints = [Footprint("j1", "J1", "Connector", (10, 11), [ground])]
        self.assertTrue(rule(analyze(self.pcb, self.settings), "return_via"))
        ground.layers = ["*.Cu"]
        self.assertFalse(rule(analyze(self.pcb, self.settings), "return_via"))

    def test_shared_reference_does_not_require_stitching(self):
        self.settings["reference_layers"]["B.Cu"] = ["In1.Cu"]
        self.assertFalse(rule(analyze(self.pcb, self.settings), "return_via"))

    def test_ordinary_antipad_is_handled_by_transition_rule(self):
        hole = [(9.25, 9.25), (10.75, 9.25), (10.75, 10.75), (9.25, 10.75)]
        for plane in self.pcb.copper:
            plane.holes = [hole]
        result = analyze(self.pcb, self.settings)
        self.assertFalse(rule(result, "reference_gap"))
        self.assertTrue(rule(result, "return_via"))
        self.pcb.vias.append(Via("return", (10, 11.25), .6, .3, ["F.Cu", "B.Cu"], "GND"))
        result = analyze(self.pcb, self.settings)
        self.assertFalse(rule(result, "reference_gap"))
        self.assertFalse(rule(result, "return_via"))

    def test_large_void_around_via_is_not_exempted_as_antipad(self):
        for plane in self.pcb.copper:
            plane.holes = [[(7, 7), (13, 7), (13, 13), (7, 13)]]
        self.assertTrue(rule(analyze(self.pcb, self.settings), "reference_gap"))


class RoutedDecouplingChecks(unittest.TestCase):
    def fixture(self, detour=True, ground_route=True):
        ic = Footprint("ic", "U1", "IC", (0, 0), [pad("up", 1, 0, 0, "+3V3"), pad("ug", 2, 0, 2, "GND")])
        cap = Footprint("cap", "C1", "100n", (2, 0), [pad("cp", 1, 2, 0, "+3V3"), pad("cg", 2, 2, 2, "GND")])
        tracks = ([Track("p1", (0, 0), (0, -10), .2, "F.Cu", "+3V3"),
                   Track("p2", (0, -10), (2, -10), .2, "F.Cu", "+3V3"),
                   Track("p3", (2, -10), (2, 0), .2, "F.Cu", "+3V3")]
                  if detour else [Track("p1", (0, 0), (2, 0), .2, "F.Cu", "+3V3")])
        if ground_route:
            tracks.append(Track("g1", (0, 2), (2, 2), .2, "F.Cu", "GND"))
        return board(footprints=[ic, cap], tracks=tracks)

    def test_close_capacitor_with_long_route_is_flagged(self):
        findings = rule(analyze(self.fixture()), "decoupling_path")
        self.assertEqual(len(findings), 1)
        self.assertIn("24.00 mm", findings[0].summary)
        self.assertIn("22.00 mm", " ".join(findings[0].evidence))

    def test_plane_return_is_unknown_not_zero(self):
        pcb = self.fixture(ground_route=False)
        pcb.copper = [copper(bounds=(-1, -11, 3, 3))]
        result = analyze(pcb)
        finding = rule(result, "decoupling_path")[0]
        self.assertIn("one side remains unresolved", finding.summary)
        self.assertTrue(any("not a zero-length" in missing for missing in finding.missing))
        self.assertEqual(next(c for c in result.coverage if c["rule"] == "decoupling_path")["status"], "partial")

    def test_unrouted_connection_has_no_invented_length_and_partial_coverage(self):
        pcb = self.fixture()
        pcb.tracks = []
        result = analyze(pcb)
        self.assertFalse(rule(result, "decoupling_path"))
        coverage = next(c for c in result.coverage if c["rule"] == "decoupling_path")
        self.assertEqual(coverage["status"], "partial")
        self.assertIn("1 pairs", coverage["detail"])

    def test_short_local_cap_suppresses_remote_bulk_false_positive(self):
        pcb = self.fixture(detour=False)
        bulk = Footprint("bulk", "C2", "47u", (20, 0),
                         [pad("bp", 1, 20, 0, "+3V3"), pad("bg", 2, 20, 2, "GND")])
        pcb.footprints.append(bulk)
        pcb.tracks.extend([Track("bp", (2, 0), (20, 0), .2, "F.Cu", "+3V3"),
                           Track("bg", (2, 2), (20, 2), .2, "F.Cu", "GND")])
        self.assertFalse(rule(analyze(pcb), "decoupling_path"))

    def test_explicit_annotation_strengthens_confidence(self):
        pcb = self.fixture()
        automatic = rule(analyze(pcb), "decoupling_path")[0]
        explicit = rule(analyze(pcb, {"decoupling": [{"ic": "U1", "capacitor": "C1", "power_net": "+3V3", "ground_net": "GND"}]}), "decoupling_path")[0]
        self.assertEqual(automatic.confidence, "low")
        self.assertEqual(explicit.confidence, "medium")
        self.assertEqual(automatic.id, explicit.id)

    def test_graph_joins_track_intersections_and_correct_via_span(self):
        start, end = pad("start", 1, 0, 0, "VDD"), pad("end", 2, 5, 5, "VDD", ["B.Cu"])
        pcb = board(footprints=[Footprint("f", "U1", "", (0, 0), [start, end])],
                    tracks=[Track("horizontal", (0, 0), (10, 0), .2, "F.Cu", "VDD"),
                            Track("vertical", (5, 0), (5, 5), .2, "B.Cu", "VDD")],
                    vias=[Via("via", (5, 0), .6, .3, ["F.Cu", "B.Cu"], "VDD")])
        path = RouteGraph(pcb, "VDD").shortest(start, end)
        self.assertIsNotNone(path)
        self.assertAlmostEqual(path.length, 10)
        pcb.vias[0].net = "OTHER"
        self.assertIsNone(RouteGraph(pcb, "VDD").shortest(start, end))

    def test_schematic_supply_pin_can_recognize_nonstandard_power_net(self):
        pcb = self.fixture()
        for item in [*pcb.pads, *pcb.tracks]:
            if item.net == "+3V3":
                item.net = "/quiet_rail"
        self.assertFalse(rule(analyze(pcb), "decoupling_path"))
        pcb.footprints[0].properties["__schematic_power_input_pins"] = json.dumps(["1", "2"])
        found = rule(analyze(pcb), "decoupling_path")
        self.assertEqual(len(found), 1)
        self.assertTrue(any("schematic power-pin" in text for text in found[0].missing))

    def test_dnp_capacitor_is_not_assumed_populated(self):
        pcb = self.fixture()
        pcb.footprints[1].properties["__schematic_dnp"] = "yes"
        self.assertFalse(rule(analyze(pcb), "decoupling_path"))

    def test_track_strokes_can_connect_without_centerline_intersection(self):
        start, end = pad("a", 1, 0, 0, "VDD"), pad("b", 1, 5, 5, "VDD")
        pcb = board(footprints=[Footprint("fp", "U1", "", (0, 0), [start, end])],
                    tracks=[Track("wide", (0, 0), (10, 0), 1., "F.Cu", "VDD"),
                            Track("branch", (5, .4), (5, 5), .2, "F.Cu", "VDD")])
        path = RouteGraph(pcb, "VDD").shortest(start, end)
        self.assertIsNotNone(path)
        self.assertAlmostEqual(path.length, 10)
        pcb.tracks[1].start = (5, .8)
        self.assertIsNone(RouteGraph(pcb, "VDD").shortest(start, end))


class SwitcherChecks(unittest.TestCase):
    def fixture(self):
        regulator = Footprint("reg", "U2", "Unknown converter", (0, 0),
                              [pad("vin", 1, 0, 0, "VIN"), pad("gnd", 2, 0, 2, "GND"),
                               pad("vout", 3, 0, 4, "VOUT"), pad("sw", 4, 0, 6, "SW")])
        input_cap = Footprint("cin", "C1", "10u", (1, 0), [pad("cip", 1, 1, 0, "VIN"), pad("cig", 2, 1, 2, "GND")])
        output_cap = Footprint("cout", "C2", "10u", (12, 4), [pad("cop", 1, 12, 4, "VOUT"), pad("cog", 2, 12, 2, "GND")])
        pcb = board(footprints=[regulator, input_cap, output_cap],
                    tracks=[Track("input", (0, 0), (1, 0), .3, "F.Cu", "VIN"),
                            Track("ingnd", (0, 2), (1, 2), .3, "F.Cu", "GND"),
                            Track("output", (0, 4), (12, 4), .3, "F.Cu", "VOUT"),
                            Track("outgnd", (1, 2), (12, 2), .3, "F.Cu", "GND")],
                    copper=[copper("swplane", "F.Cu", "SW", (-3, 5, 3, 10))])
        config = {"regulators": [{"reference": "U2", "topology": "buck", "input_cap": "C1", "output_cap": "C2", "switch_net": "SW"}]}
        return pcb, config

    def test_buck_uses_input_cap_boost_uses_output_cap(self):
        pcb, config = self.fixture()
        self.assertFalse(rule(analyze(pcb, config), "switching_loop"))
        config["regulators"][0]["topology"] = "boost"
        found = rule(analyze(pcb, config), "switching_loop")
        self.assertEqual(len(found), 1)
        self.assertIn("output-capacitor", found[0].title)
        self.assertIn("24.00 mm", found[0].summary)

    def test_switch_copper_uses_fills_and_unknown_topology_is_unresolved(self):
        pcb, config = self.fixture()
        found = rule(analyze(pcb, config), "switch_copper")
        self.assertEqual(len(found), 1)
        self.assertIn("30.00 mm²", found[0].summary)
        config["regulators"][0]["topology"] = "unknown"
        result = analyze(pcb, config)
        self.assertFalse(rule(result, "switching_loop"))
        self.assertEqual(next(c for c in result.coverage if c["rule"] == "switching_loop")["status"], "partial")

    def test_switching_loop_groups_duplicate_generic_decap_finding(self):
        pcb, config = self.fixture()
        config["regulators"][0]["topology"] = "boost"
        config["decoupling"] = [{"ic": "U2", "capacitor": "C2", "power_net": "VOUT", "ground_net": "GND"}]
        result = analyze(pcb, config)
        self.assertTrue(rule(result, "switching_loop"))
        self.assertFalse(rule(result, "decoupling_path"))


class ResultIntegrity(unittest.TestCase):
    def test_stable_ids_ignore_and_snapshot_immutability(self):
        pcb = board(tracks=[Track("clock", (2, 10), (18, 10), .2, "F.Cu", "CLK")],
                    copper=[copper(bounds=(0, 0, 5, 20))])
        before = copy.deepcopy(pcb)
        first = analyze(pcb)
        second = analyze(pcb)
        self.assertEqual([f.id for f in first.findings], [f.id for f in second.findings])
        self.assertEqual(pcb, before)
        ignored = analyze(pcb, {"ignored": {first.findings[0].id: "Intentional RF structure"}})
        self.assertEqual(ignored.suppressed_count, 1)
        self.assertNotIn(first.findings[0].id, [f.id for f in ignored.findings])

    def test_empty_board_reports_missing_coverage_without_clean_claim(self):
        result = analyze(board())
        self.assertFalse(result.findings)
        self.assertTrue(result.coverage)
        self.assertTrue(all(c["status"] == "partial" for c in result.coverage))

    def test_annotation_does_not_invent_edge_rate(self):
        pcb = board(tracks=[Track("clock", (2, 10), (18, 10), .2, "F.Cu", "CLK")],
                    copper=[copper(bounds=(0, 0, 5, 20))])
        result = analyze(pcb, {"fast_nets": {"CLK": {"frequency_mhz": 24}}})
        self.assertTrue(any("rise/fall" in missing for missing in result.findings[0].missing))


if __name__ == "__main__":
    unittest.main()
