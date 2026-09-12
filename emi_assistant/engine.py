"""Deterministic, evidence-first PCB EMI screening.

Results establish geometry and identify plausible EMI mechanisms. Neither a
quiet result nor a lower finding count predicts regulatory compliance.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import math
import re
import time

from shapely.geometry import GeometryCollection, LineString, Point, Polygon
from shapely.ops import unary_union

from .geometry import CopperIndex, RouteGraph, best_path, layer_span, pad_layers, pad_radius, parts
from .models import AnalysisResult, BoardSnapshot, Finding, default_settings

TI_RETURN = "https://www.ti.com/lit/an/spraar7j/spraar7j.pdf"
TI_HIGH_SPEED = "https://www.ti.com/lit/an/scaa082a/scaa082a.pdf"
ADI_SWITCHING = "https://www.analog.com/en/resources/app-notes/an-139.html"


def _id(rule: str, items: list[str]) -> str:
    digest = hashlib.sha256((rule + "\0" + "\0".join(sorted(set(items)))).encode()).hexdigest()[:16]
    return f"{rule}:{digest}"


def _settings(custom):
    result = deepcopy(default_settings())
    for key, value in (custom or {}).items():
        if key == "thresholds" and isinstance(value, dict):
            result[key].update(value)
        else:
            result[key] = deepcopy(value)
    return result


def _positive(value, fallback):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else fallback
    except (TypeError, ValueError):
        return fallback


def _power(net):
    return bool(re.search(r"(?:^|[/_+\-])(VCC|VDD|VSS|VBAT|VIN|VOUT|[0-9]+V[0-9]*|[0-9]+\.\d+V)(?:$|[_/])", net, re.I))


def _fast_nets(board, settings):
    result = {}
    for net in board.nets:
        annotation = settings.get("fast_nets", {}).get(net)
        if annotation is not None:
            if not isinstance(annotation, dict):
                annotation = {"enabled": bool(annotation)}
            if annotation.get("enabled", True):
                result[net] = {**annotation, "inferred": False}
        elif re.search(r"(?:^|[/_])(CLK\w*|SCLK|SCK|SPI\w*|MCLK|BCLK|USB\w*|D[+\-])(?:$|[_/])", net, re.I):
            result[net] = {"inferred": True}
    return result


def _edge_missing(net, fast):
    information = fast.get(net, {})
    result = []
    if information.get("inferred"):
        result.append(f"Confirm that {net} carries fast transitions; its name is only a hint.")
    if _positive(information.get("rise_ns"), 0) <= 0:
        result.append("Signal rise/fall time is unknown; frequency alone does not establish EMI severity.")
    return result


class _Analyzer:
    def __init__(self, board, settings):
        self.board, self.settings = board, settings
        self.copper = CopperIndex(board)
        self.findings = []
        self.coverage = []
        self.assumptions = []
        self.fast = _fast_nets(board, settings)
        self.grounds = [n for n in settings.get("reference_nets", ["GND"]) if n in board.nets]
        self.graphs = {}
        self.fps = {f.reference: f for f in board.footprints}

    def graph(self, net):
        if net not in self.graphs:
            self.graphs[net] = RouteGraph(self.board, net)
        return self.graphs[net]

    def add(self, rule, title, priority, confidence, summary, recommendation,
            items, location, layer="", evidence=None, missing=None, preview=None, sources=None):
        self.findings.append(Finding(
            id=_id(rule, items), rule=rule, title=title, priority=priority,
            confidence=confidence, summary=summary, recommendation=recommendation,
            item_ids=sorted(set(items)), location=tuple(location), layer=layer,
            evidence=evidence or [], missing=missing or [], preview=preview or {},
            sources=sources or [TI_RETURN]))

    def cover(self, rule, status, detail):
        self.coverage.append({"rule": rule, "status": status, "detail": detail})

    def references(self, layer):
        configured = self.settings.get("reference_layers", {}).get(layer)
        if configured is not None:
            return [x for x in configured if x in self.board.layers and x != layer]
        if layer not in self.board.layers:
            return []
        index = self.board.layers.index(layer)
        # Only immediately adjacent copper layers are plausible default
        # references. Never silently look through an intervening signal plane.
        candidates = self.board.layers[max(0, index - 1):index] + self.board.layers[index + 1:index + 2]
        return [x for x in candidates if any(not self.copper.get(x, net).is_empty for net in self.grounds)]

    def _reference_choices(self, layer, line=None, position=None, radius=0):
        options = []
        for reference in self.references(layer):
            for net in self.grounds:
                shape = self.copper.get(reference, net)
                if shape.is_empty:
                    continue
                score = line.intersection(shape).length if line is not None else -(shape.distance(Point(position)))
                if position is None or shape.distance(Point(position)) <= radius:
                    options.append((score, reference, net, shape))
        return sorted(options, key=lambda x: (-x[0], x[1], x[2]))

    def return_paths(self):
        unknown, checked = 0, 0
        gaps = defaultdict(list)
        for track in self.board.tracks:
            if track.net not in self.fast or track.start == track.end:
                continue
            line = LineString([track.start, track.end])
            choices = self._reference_choices(track.layer, line=line)
            if not choices:
                unknown += 1
                continue
            checked += 1
            _, layer, ground, shape = choices[0]
            uncovered = line.difference(shape.buffer(0.005))
            # Small, bounded antipads belonging to this signal via are handled
            # by the layer-transition rule. Counting them as broken plane
            # routes would warn on every ordinary plated signal transition.
            antipads = []
            for via in self.board.vias:
                if via.net != track.net or line.distance(Point(via.position)) > via.diameter / 2 + track.width / 2:
                    continue
                for component in parts(shape):
                    for ring in component.interiors:
                        hole = Polygon(ring)
                        x0, y0, x1, y1 = hole.bounds
                        maximum_size = via.diameter + 1.5
                        if hole.covers(Point(via.position)) and max(x1 - x0, y1 - y0) <= maximum_size:
                            antipads.append(hole.buffer(0.005))
            if antipads:
                uncovered = uncovered.difference(unary_union(antipads))
            if uncovered.length < max(0.2, track.width):
                continue
            gaps[track.net, track.layer, layer, ground].append((track, uncovered, line))
        for (net, signal_layer, reference, ground), group in gaps.items():
            worst = max(group, key=lambda x: x[1].length)
            track, uncovered, line = worst
            location = uncovered.interpolate(0.5, normalized=True).coords[0]
            items = [item.id for item, _, _ in group]
            explicit = signal_layer in self.settings.get("reference_layers", {})
            confidence = "medium" if explicit and not self.fast[net].get("inferred") else "low"
            missing = _edge_missing(net, self.fast)
            if not explicit:
                missing.append(f"Confirm {reference} is the intended reference for {signal_layer}; adjacency was inferred.")
            if len(self.references(signal_layer)) > 1:
                missing.append("Return current may divide between adjacent planes; no single candidate covered the full trace.")
            length = sum(gap.length for _, gap, _ in group)
            self.add("reference_gap", f"Keep {net} over continuous reference copper", "high", confidence,
                     f"{length:.2f} mm of {net} runs over a gap in {ground} copper on {reference}.",
                     f"Reroute the highlighted section over continuous {ground} copper, or review the intended reference plane. Respect intentional RF voids and isolation boundaries.",
                     items, location, signal_layer,
                     evidence=[f"Actual filled copper and holes were evaluated on {reference}.",
                               f"Measured centerline projection on {len(group)} trace segment(s); small bounded signal-via antipads are evaluated by the transition check.",
                               "Each reference net was evaluated separately; different ground domains were not joined.",
                               "Observed absence of copper is geometric evidence; the emission contribution is an inference."],
                     missing=missing,
                     preview={"kind": "route", "points": [list(track.start), list(track.end)],
                              "net": net, "layers": [signal_layer, reference], "advisory": True,
                              "description": "Highlighted existing segment to reroute; this is not a generated safe route."})
        status = "partial" if unknown or not self.fast else "checked"
        self.cover("reference_gap", status,
                   f"Screened {checked} fast-net trace segments; {unknown} lacked a usable adjacent or annotated filled reference. "
                   + ("Mark fast nets to extend coverage." if not self.fast else "Copper-neck inductance, non-ground references, and field distribution are not solved."))

    def _return_contact(self, signal_via, first, second, radius, anchor_a, anchor_b):
        _, layer_a, net_a, _ = first
        _, layer_b, net_b, _ = second
        if net_a != net_b:
            return None
        candidates = []
        for via in self.board.vias:
            if via.net == net_a and via.id != signal_via.id:
                candidates.append((via.id, via.position, via.diameter / 2, layer_span(via.layers, self.board.layers)))
        for pad in self.board.pads:
            if pad.net == net_a:
                candidates.append((pad.id, pad.position, pad_radius(pad), pad_layers(pad, self.board.layers)))
        for item, position, contact_radius, layers in candidates:
            if layer_a not in layers or layer_b not in layers:
                continue
            if math.dist(position, signal_via.position) > radius:
                continue
            if all(self.copper.connected_near(layer, net_a, anchor, 0.05,
                                              position, contact_radius)
                   for layer, anchor in [(layer_a, anchor_a), (layer_b, anchor_b)]):
                return item
        return None

    def _via_preview(self, signal_via, first, second, radius, anchor_a, anchor_b):
        _, layer_a, net_a, shape_a = first
        _, layer_b, net_b, shape_b = second
        if net_a != net_b:
            return {}
        # Advisory candidate only. Full rules, keepouts, electrical constraints,
        # board thickness, annular ring and thermal design require KiCad DRC.
        safe_region = shape_a.intersection(shape_b).buffer(-0.45)
        if safe_region.is_empty:
            return {}
        for distance in [0.9, 1.25, 1.6, radius]:
            if distance > radius:
                continue
            for angle in range(0, 360, 30):
                position = (signal_via.position[0] + distance * math.cos(math.radians(angle)),
                            signal_via.position[1] + distance * math.sin(math.radians(angle)))
                if not safe_region.covers(Point(position)):
                    continue
                if not all(self.copper.connected_near(layer, net_a, anchor, 0.05, position, 0.3)
                           for layer, anchor in [(layer_a, anchor_a), (layer_b, anchor_b)]):
                    continue
                if any(t.net != net_a and LineString([t.start, t.end]).distance(Point(position)) < 0.3 + t.width / 2 + 0.25
                       for t in self.board.tracks):
                    continue
                if any(p.net != net_a and math.dist(p.position, position) < max(p.size) / 2 + 0.55
                       for p in self.board.pads):
                    continue
                if any(v.net != net_a and math.dist(v.position, position) < v.diameter / 2 + 0.55
                       for v in self.board.vias):
                    continue
                return {"kind": "via", "position": list(position), "net": net_a,
                        "layers": [self.board.layers[0], self.board.layers[-1]],
                        "reference_layers": [layer_a, layer_b], "diameter": 0.6, "drill": 0.3,
                        "advisory": True, "drc_checked": False,
                        "description": "Candidate location only. Check board rules, keepouts and intentional ground boundaries before adding a via."}
        return {}

    def transitions(self):
        radius = _positive(self.settings["thresholds"].get("return_via_mm"), 2.)
        checked, unknown, same_reference = 0, 0, 0
        tracks_by_net = defaultdict(list)
        for track in self.board.tracks:
            tracks_by_net[track.net].append(track)
        for via in self.board.vias:
            if via.net not in self.fast:
                continue
            active = sorted({t.layer for t in tracks_by_net[via.net]
                             if t.layer in layer_span(via.layers, self.board.layers)
                             and LineString([t.start, t.end]).distance(Point(via.position)) <= via.diameter / 2 + t.width / 2 + 1e-5},
                            key=lambda x: self.board.layers.index(x) if x in self.board.layers else 999)
            if len(active) < 2:
                continue
            anchors = {}
            for layer in active:
                routes = [t for t in tracks_by_net[via.net] if t.layer == layer and
                          LineString([t.start, t.end]).distance(Point(via.position)) <= via.diameter / 2 + t.width / 2 + 1e-5]
                endpoints = [point for track in routes for point in (track.start, track.end)]
                endpoint = max(endpoints, key=lambda p: math.dist(p, via.position))
                approach = LineString([via.position, endpoint])
                anchors[layer] = approach.interpolate(min(1.5, approach.length)).coords[0]
            all_choices = {layer: self._reference_choices(layer, position=anchors[layer], radius=0.05)
                           for layer in active}
            if any(not choices for choices in all_choices.values()):
                unknown += 1
                continue
            for source, destination in zip(active, active[1:]):
                choices_a, choices_b = all_choices[source], all_choices[destination]
                if {(x[1], x[2]) for x in choices_a} & {(x[1], x[2]) for x in choices_b}:
                    same_reference += 1
                    continue
                checked += 1
                pairs = [(a, b) for a in choices_a for b in choices_b if a[2] == b[2]]
                if not pairs:
                    self.add("reference_domain_change", f"Review {via.net}'s reference-domain change", "high", "medium",
                             f"The {source} to {destination} transition has reference copper on different annotated ground nets.",
                             "Review the return-current transfer with the circuit and isolation requirements. Do not bridge different reference domains automatically.",
                             [via.id], via.position, source,
                             evidence=[f"Reference candidates: {', '.join(x[1] + '/' + x[2] for x in choices_a + choices_b)}."],
                             missing=_edge_missing(via.net, self.fast))
                    continue
                if any(self._return_contact(via, a, b, radius, anchors[source], anchors[destination]) for a, b in pairs):
                    continue
                first, second = pairs[0]
                self.add("return_via", f"Review the return connection beside {via.net}", "high", "medium" if not self.fast[via.net].get("inferred") else "low",
                         f"{via.net} changes layers here, with no nearby ground connection joining {first[1]} and {second[1]}.",
                         "Consider a nearby ground stitching via connecting both reference layers. Verify filled-copper attachment and KiCad design rules; placement depends on edge rate and stackup.",
                         [via.id], via.position, source,
                         evidence=[f"Signal routing changes from {source} to {destination} at this via.",
                                   "Candidates had to span both reference layers and contact the same connected copper component near the signal on each layer.",
                                   f"{radius:.2f} mm is a configurable screening distance, not an EMC pass/fail limit."],
                         missing=_edge_missing(via.net, self.fast) + ["Return impedance and coupling through the stackup are not simulated."],
                         preview=self._via_preview(via, first, second, radius, anchors[source], anchors[destination]))
        self.cover("return_via", "partial" if unknown or not self.fast else "checked",
                   f"Screened {checked} reference-changing transitions; {same_reference} share an adjacent reference; {unknown} lack usable local reference data. Plated ground pads also count as return connections.")

    def _capacitor_pairs(self, fp):
        pairs = []
        for ground in self.grounds:
            grounds = [p for p in fp.pads if p.net == ground]
            for net in sorted({p.net for p in fp.pads if p.net and p.net not in self.grounds}):
                if grounds:
                    pairs.append((net, ground, [p for p in fp.pads if p.net == net], grounds))
        return pairs

    def _connection(self, ic, capacitor, power, ground):
        ic_power = [p for p in ic.pads if p.net == power]
        ic_ground = [p for p in ic.pads if p.net == ground]
        cap_power = [p for p in capacitor.pads if p.net == power]
        cap_ground = [p for p in capacitor.pads if p.net == ground]
        if not all([ic_power, ic_ground, cap_power, cap_ground]):
            return None
        supply = best_path(self.graph(power), ic_power, cap_power)
        returned = best_path(self.graph(ground), ic_ground, cap_ground)
        return supply, returned

    def decoupling(self):
        threshold = _positive(self.settings["thresholds"].get("decoupling_path_mm"), 5.)
        candidates = []
        explicit = self.settings.get("decoupling", [])
        annotated_ics = set()
        unavailable = 0
        for entry in explicit:
            ic, cap = self.fps.get(entry.get("ic")), self.fps.get(entry.get("capacitor"))
            annotated_ics.add(entry.get("ic"))
            if not ic or not cap or ic.properties.get("__schematic_dnp") == "yes" or cap.properties.get("__schematic_dnp") == "yes":
                unavailable += 1
                continue
            ground = entry.get("ground_net", "GND")
            power = entry.get("power_net")
            if not power:
                shared = sorted(({p.net for p in ic.pads} & {p.net for p in cap.pads}) - set(self.grounds))
                power = shared[0] if len(shared) == 1 else None
            if power:
                candidates.append((ic, cap, power, ground, False))
            else:
                unavailable += 1
        caps = [fp for fp in self.board.footprints if re.match(r"C\d+$", fp.reference, re.I)
                and fp.properties.get("__schematic_dnp") != "yes"]
        for ic in self.board.footprints:
            if ic.reference in annotated_ics or not re.match(r"U\d+$", ic.reference, re.I) or ic.properties.get("__schematic_dnp") == "yes":
                continue
            try:
                annotated_supply_pins = set(json.loads(ic.properties.get("__schematic_power_input_pins", "[]")))
            except (ValueError, TypeError):
                annotated_supply_pins = set()
            for power in sorted({p.net for p in ic.pads if (p.number in annotated_supply_pins or _power(p.net))
                                 and p.net not in self.grounds and p.net}):
                possible = []
                for cap in caps:
                    for cap_net, ground, _, _ in self._capacitor_pairs(cap):
                        if cap_net != power:
                            continue
                        connection = self._connection(ic, cap, power, ground)
                        if connection:
                            supply, _ = connection
                            possible.append((supply.length if supply else math.inf, cap.reference, cap, ground))
                if possible:
                    # A remote bulk capacitor is not a decoupling fault when
                    # the IC already has a shorter connected local capacitor.
                    _, _, cap, ground = min(possible, key=lambda x: (x[0], x[1]))
                    candidates.append((ic, cap, power, ground, True))
        complete, partial = 0, unavailable
        for ic, cap, power, ground, inferred in candidates:
            connection = self._connection(ic, cap, power, ground)
            if not connection:
                partial += 1
                continue
            supply, returned = connection
            if supply is None or returned is None:
                partial += 1
            else:
                complete += 1
            observed = [path for path in (supply, returned) if path is not None]
            if not observed or sum(p.length for p in observed) <= threshold:
                continue
            evidence, missing = [], []
            if supply:
                evidence.append(f"Explicit routed {power} connection between capacitor and IC pads: {supply.length:.2f} mm.")
            else:
                missing.append("Supply path was not recoverable from explicit pads, tracks and vias; it may use planes or unsupported geometry.")
            if returned:
                evidence.append(f"Explicit routed {ground} connection between capacitor and IC ground pads: {returned.length:.2f} mm.")
            else:
                missing.append("Ground return path length is unknown; a plane connection is not a zero-length return path.")
            evidence.append(f"Compared observed routed connection length with a configurable {threshold:.2f} mm screening threshold; component-center distance was not used.")
            if inferred:
                basis = "schematic power-pin roles" if ic.properties.get("__schematic_power_input_pins") else "net names"
                missing.append(f"Decoupling association was inferred from component prefixes and {basis}; confirm this capacitor serves this IC.")
            missing.append("Package inductance, capacitor impedance, via vertical length and current distribution are not modeled.")
            all_items = [ic.id, cap.id] + [item for path in observed for item in path.item_ids]
            worst = max(observed, key=lambda p: p.length)
            self.add("decoupling_path", f"Shorten {cap.reference}'s connection to {ic.reference}", "medium", "low" if inferred else "medium",
                     f"{cap.reference}'s measured connections to {ic.reference} total {sum(p.length for p in observed):.2f} mm" + ("; one side remains unresolved." if len(observed) < 2 else "."),
                     "Review both supply and ground pad connections together. Move or rotate the capacitor and shorten its explicit leads while preserving suitable reference copper.",
                     all_items, cap.position, evidence=evidence, missing=missing,
                     preview={"kind": "route", "points": [list(p) for p in worst.points], "advisory": True,
                              "description": "Observed routed connection; shorten this path as the rest of the loop allows."},
                     sources=[TI_HIGH_SPEED])
        self.cover("decoupling_path", "partial" if partial or not candidates else "checked",
                   f"{complete} capacitor/IC pairs had explicit supply and ground routes; {partial} pairs were unresolved or partly plane-mediated. "
                   "Automatic associations use U/C prefixes and schematic power pins or power-net names. The shortest candidate per IC/supply net is screened; individual multi-supply-pin loops and internal package connections are outside coverage.")

    def _switch_copper(self, net):
        grouped = defaultdict(list)
        for (layer, copper_net), shape in self.copper.geometry.items():
            if copper_net == net:
                grouped[layer].append(shape)
        for track in self.board.tracks:
            if track.net == net:
                grouped[track.layer].append(LineString([track.start, track.end]).buffer(track.width / 2))
        for pad in self.board.pads:
            if pad.net == net:
                for layer in pad_layers(pad, self.board.layers):
                    grouped[layer].append(Point(pad.position).buffer(pad_radius(pad)))
        for via in self.board.vias:
            if via.net == net:
                for layer in layer_span(via.layers, self.board.layers):
                    grouped[layer].append(Point(via.position).buffer(via.diameter / 2).difference(Point(via.position).buffer(via.drill / 2)))
        shapes = {layer: unary_union(group) for layer, group in grouped.items()}
        return sum(shape.area for shape in shapes.values()), shapes

    def regulators(self):
        annotations = [dict(entry, inferred=False) for entry in self.settings.get("regulators", [])]
        # These integrated synchronous converters support a bounded topology
        # hint. Naming alone does not confirm pin roles or cap association.
        known = {"TPS62160": "buck", "TPS62162": "buck", "TPS62130": "buck", "TPS54302": "buck",
                 "TPS61023": "boost", "TPS61022": "boost"}
        existing = {entry.get("reference") for entry in annotations}
        for fp in self.board.footprints:
            if fp.reference in existing:
                continue
            topology = next((kind for part, kind in known.items() if fp.value.upper().startswith(part)), None)
            if topology:
                annotations.append({"reference": fp.reference, "topology": topology, "inferred": True})
        assessed, unresolved = 0, 0
        area_threshold = _positive(self.settings["thresholds"].get("switch_copper_mm2"), 20.)
        path_threshold = _positive(self.settings["thresholds"].get("decoupling_path_mm"), 5.)
        for entry in annotations:
            fp = self.fps.get(entry.get("reference"))
            topology, inferred = entry.get("topology"), entry.get("inferred", False)
            if not fp or topology not in ("buck", "boost") or fp.properties.get("__schematic_dnp") == "yes":
                unresolved += 1
                continue
            switch = entry.get("switch_net")
            if not switch:
                sw_nets = sorted({pad.net for pad in fp.pads if re.search(r"(?:^|[/_])(?:SW|LX|PH)(?:$|[_/])", pad.net, re.I)})
                switch = sw_nets[0] if len(sw_nets) == 1 else None
            if switch and switch not in {pad.net for pad in fp.pads}:
                switch = None
                unresolved += 1
            if switch and switch in self.board.nets:
                area, shapes = self._switch_copper(switch)
                if area > area_threshold:
                    shape = max(shapes.values(), key=lambda x: x.area)
                    xmin, ymin, xmax, ymax = shape.bounds
                    ids = [fp.id] + [t.id for t in self.board.tracks if t.net == switch] + [p.id for p in self.board.copper if p.net == switch]
                    self.add("switch_copper", f"Review {fp.reference}'s switch-node copper", "medium", "low" if inferred else "medium",
                             f"{switch} covers at least {area:.2f} mm² across {len(shapes)} copper layer(s). Review whether all of it is needed.",
                             "Review unnecessary switch-node extent and proximity to sensitive nets or cables. Preserve required thermal area, current capacity, clearance and the device manufacturer's layout guidance.",
                             ids, fp.position,
                             evidence=[f"{topology.capitalize()} topology {'inferred from a curated part-name hint' if inferred else 'annotated by the user'}.",
                                       f"Filled copper, track strokes, conservative pad footprints and via annuli were unioned per layer; threshold {area_threshold:.2f} mm² is configurable."],
                             missing=["Projected area is a layout screen, not an emission or capacitance prediction.",
                                      "Pad outlines are conservatively approximated; internal shielding, edge rate and switching voltage are not modeled."],
                             preview={"kind": "region", "points": [[xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax]],
                                      "net": switch, "advisory": True}, sources=[ADI_SWITCHING])
            cap_key = "input_cap" if topology == "buck" else "output_cap"
            cap = self.fps.get(entry.get(cap_key))
            ground = entry.get("ground_net", "GND")
            if not cap or cap.properties.get("__schematic_dnp") == "yes":
                unresolved += 1
                continue
            shared = sorted(({p.net for p in fp.pads} & {p.net for p in cap.pads}) - {ground, switch, ""})
            if len(shared) != 1:
                unresolved += 1
                continue
            connection = self._connection(fp, cap, shared[0], ground)
            if not connection:
                unresolved += 1
                continue
            supply, returned = connection
            if supply is None or returned is None:
                unresolved += 1
            else:
                assessed += 1
            observed = [path for path in (supply, returned) if path is not None]
            if observed and sum(p.length for p in observed) > path_threshold:
                missing = ["Full commutation-loop impedance, switch/rectifier pin roles and package paths require device-specific confirmation."]
                if len(observed) < 2:
                    missing.append("One capacitor connection is unresolved; the full loop length is unknown.")
                if inferred:
                    missing.append("Confirm inferred topology and component roles before changing the layout.")
                worst = max(observed, key=lambda p: p.length)
                cap_role = "input" if topology == "buck" else "output"
                self.add("switching_loop", f"Shorten {fp.reference}'s {cap_role}-capacitor connections", "high", "low" if inferred else "medium",
                         f"{cap.reference}'s measured connections to {fp.reference} total {sum(p.length for p in observed):.2f} mm in this {topology} circuit. Shorter connections can reduce its switching loop.",
                         f"Review the {cap_role} capacitor, switching devices and ground connections as one commutation loop. Follow the device layout guide; do not optimize the inductor loop as a substitute.",
                         [fp.id, cap.id] + [item for p in observed for item in p.item_ids], cap.position,
                         evidence=[f"Topology selects the {cap_role} capacitor: the buck input loop and boost output loop switch current abruptly.",
                                   f"Observed supply lead: {f'{supply.length:.2f} mm' if supply else 'unresolved'}; ground lead: {f'{returned.length:.2f} mm' if returned else 'unresolved'}.",
                                   "Only explicit connected pads/tracks/vias are measured; no inductor-center polygon was used."],
                         missing=missing,
                         preview={"kind": "route", "points": [list(p) for p in worst.points], "advisory": True},
                         sources=[ADI_SWITCHING])
        self.cover("switching_loop", "partial" if unresolved or not annotations else "checked",
                   f"{assessed} topology-selected capacitor pairs had explicit routes; {unresolved} regulator annotations/hints need topology, capacitor, pin-role or connection detail. "
                   "Unrecognized converters and discrete rectifier loops are not automatically solved.")
        self.cover("switch_copper", "partial" if not annotations or unresolved else "checked",
                   "Switch-node area is screened only where a switch net and buck/boost topology are identified; thermal suitability and radiation are not calculated.")

    def coupling(self):
        """Small, explicitly labelled proximity screen; no mutual-C/L estimate."""
        external = set(self.settings.get("external_connectors", []))
        cable_nets = {p.net for fp in self.board.footprints if fp.reference in external for p in fp.pads
                      if p.net and p.net not in self.grounds and not _power(p.net)}
        switch_nets = {entry.get("switch_net") for entry in self.settings.get("regulators", []) if entry.get("switch_net")}
        aggressors = set(self.fast) | switch_nets
        sensitive = {net for net in self.board.nets if re.search(r"(?:^|[/_])(FB|VREF|REF|ADC\w*|RESET|NRST|XTAL\w*)(?:$|[_/])", net, re.I)} | cable_nets
        victim_tracks = [t for t in self.board.tracks if t.net in sensitive]
        if not victim_tracks:
            self.cover("coupling", "partial", "No named sensitive or annotated external-cable routes found. Filter topology and differential imbalance are not evaluated in this release.")
            return
        victim_lines = [LineString([t.start, t.end]) for t in victim_tracks]
        from shapely.strtree import STRtree
        tree = STRtree(victim_lines)
        found = {}
        for source in self.board.tracks:
            if source.net not in aggressors or source.start == source.end:
                continue
            line = LineString([source.start, source.end])
            for index in tree.query(line.buffer(0.6)):
                target, victim = victim_tracks[int(index)], victim_lines[int(index)]
                if target.net == source.net or target.layer != source.layer or target.start == target.end:
                    continue
                dx, dy = source.end[0] - source.start[0], source.end[1] - source.start[1]
                ex, ey = target.end[0] - target.start[0], target.end[1] - target.start[1]
                cosine = abs((dx * ex + dy * ey) / (math.hypot(dx, dy) * math.hypot(ex, ey)))
                if cosine < 0.966:
                    continue
                spacing = line.distance(victim) - source.width / 2 - target.width / 2
                overlap = victim.intersection(line.buffer(0.6)).length
                if spacing > 0.5 or overlap < 3.0:
                    continue
                key = source.net, target.net, source.layer
                if key not in found or overlap > found[key][0]:
                    found[key] = (overlap, spacing, source, target)
        for (source_net, target_net, layer), (overlap, spacing, source, target) in found.items():
            self.add("coupling", f"Review {source_net} beside {target_net}", "medium", "low",
                     f"These same-layer routes run approximately parallel for {overlap:.2f} mm with about {max(0, spacing):.2f} mm edge spacing.",
                     "Increase separation or reduce the parallel run if this is a fast aggressor and a sensitive or cable-connected victim. Preserve each route's reference copper.",
                     [source.id, target.id], ((source.start[0] + source.end[0]) / 2, (source.start[1] + source.end[1]) / 2), layer,
                     evidence=["Same-layer geometry and parallel orientation were measured.",
                               "Victim is connected to an annotated external connector." if target_net in cable_nets else "Victim role is inferred from its net name."],
                     missing=_edge_missing(source_net, self.fast) + ["Coupled voltage, shielding and victim susceptibility are not calculated."],
                     preview={"kind": "route", "points": [list(source.start), list(source.end)], "advisory": True})
        self.cover("coupling", "partial", f"Screened {len(victim_tracks)} candidate sensitive/cable track segments for close parallel same-layer routing. Filter topology, broadside coupling and differential imbalance are outside coverage.")


def analyze(board: BoardSnapshot, settings: dict | None = None) -> AnalysisResult:
    """Analyze an immutable snapshot. Ignored findings stay keyed to item IDs."""
    start = time.perf_counter()
    state = _Analyzer(board, _settings(settings))
    state.return_paths()
    state.transitions()
    state.decoupling()
    state.regulators()
    state.coupling()
    if any(info.get("inferred") for info in state.fast.values()):
        state.assumptions.append("Some fast-net roles were inferred from names; confirm them in board setup.")
    if any(_edge_missing(net, state.fast) for net in state.fast):
        state.assumptions.append("Unknown transition times limit EMI prioritization; clock frequency is not an edge-rate substitute.")
    if not state.settings.get("reference_layers"):
        state.assumptions.append("Reference layers were inferred from immediately adjacent copper layers containing annotated ground nets.")
    state.assumptions.append("Geometric thresholds are configurable screening heuristics, not EMC limits or a predicted emission score.")
    for warning in board.warnings:
        state.cover("board_data", "partial", warning)
    ignored = state.settings.get("ignored", {})
    # A topology-aware capacitor finding already explains the same external
    # paths. Preserve its stronger recommendation instead of showing a second
    # generic decoupling warning for the same pair in the top findings.
    switching_items = [set(f.item_ids) for f in state.findings if f.rule == "switching_loop"]
    deduplicated = [f for f in state.findings if not (f.rule == "decoupling_path" and
                    any(set(f.item_ids).issubset(items) for items in switching_items))]
    unique = {finding.id: finding for finding in deduplicated}
    suppressed = sum(1 for finding in unique.values() if finding.id in ignored)
    priority = {"high": 0, "medium": 1, "low": 2}
    confidence = {"high": 0, "medium": 1, "low": 2}
    findings = sorted((f for f in unique.values() if f.id not in ignored),
                      key=lambda f: (priority.get(f.priority, 9), confidence.get(f.confidence, 9), f.rule, f.id))
    return AnalysisResult(board, findings, state.coverage, state.assumptions,
                          (time.perf_counter() - start) * 1000, suppressed)
