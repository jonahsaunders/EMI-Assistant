"""Conservative 2-D copper geometry and explicit routed-path measurements.

These are geometric screening primitives, not an electromagnetic solver. A
plane is never turned into a zero-length wire in the routed-path graph.
"""
from __future__ import annotations

from collections import defaultdict, OrderedDict
from dataclasses import dataclass
import heapq
import math

from shapely import make_valid
from shapely.geometry import GeometryCollection, LineString, Point, Polygon
from shapely.ops import nearest_points, unary_union
from shapely.strtree import STRtree

from .models import BoardSnapshot, CopperPolygon, Pad


def polygon_geometry(copper: CopperPolygon):
    if not copper.is_filled or len(copper.points) < 3:
        return GeometryCollection()
    polygon = Polygon(copper.points, holes=[h for h in copper.holes if len(h) >= 3])
    if not polygon.is_valid:
        polygon = make_valid(polygon)
    # KiCad holes may be represented by a zero-width bridge to the outer ring.
    # make_valid retains these as polygons plus lines; only area is copper.
    if polygon.geom_type in ("Polygon", "MultiPolygon"):
        return polygon
    return unary_union([g for g in getattr(polygon, "geoms", [])
                        if g.geom_type in ("Polygon", "MultiPolygon")])


def parts(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    return [p for p in getattr(geometry, "geoms", []) if p.geom_type == "Polygon"]


def layer_span(endpoints: list[str], board_layers: list[str]) -> list[str]:
    if "*.Cu" in endpoints:
        return list(board_layers)
    indices = [board_layers.index(layer) for layer in endpoints if layer in board_layers]
    if not indices:
        return []
    return board_layers[min(indices):max(indices) + 1]


def pad_layers(pad: Pad, board_layers: list[str]) -> list[str]:
    return list(board_layers) if "*.Cu" in pad.layers else [x for x in pad.layers if x in board_layers]


def pad_radius(pad: Pad) -> float:
    # Model intentionally does not pretend to have exact custom/rotated pad
    # outlines. An inscribed circle is conservative for standard pad shapes.
    return max(0.0, min(pad.size) / 2)


class CopperIndex:
    def __init__(self, board: BoardSnapshot):
        grouped = defaultdict(list)
        self.ids = defaultdict(list)
        for copper in board.copper:
            shape = polygon_geometry(copper)
            if not shape.is_empty and shape.area > 0:
                grouped[copper.layer, copper.net].append(shape)
                self.ids[copper.layer, copper.net].append(copper.id)
        self.geometry = {key: unary_union(value) for key, value in grouped.items()}

    def get(self, layer: str, net: str):
        return self.geometry.get((layer, net), GeometryCollection())

    def connected_near(self, layer: str, net: str, a, a_radius: float, b, b_radius: float) -> bool:
        """Both copper contacts must touch the *same* filled polygon component."""
        pa, pb = Point(a), Point(b)
        return any(component.distance(pa) <= a_radius + 1e-6 and
                   component.distance(pb) <= b_radius + 1e-6
                   for component in parts(self.get(layer, net)))


@dataclass
class RoutedPath:
    length: float
    points: list[tuple[float, float]]
    item_ids: list[str]


class RouteGraph:
    """Shortest explicit same-net pad/track/via centerline route.

    Includes geometric intersections and pad/via contacts on their copper
    layers. It deliberately excludes plane shortcuts, internal IC connections,
    and guesses across unrouted airwires. Via vertical length is not measured.
    """
    def __init__(self, board: BoardSnapshot, net: str):
        self.adj = defaultdict(list)
        self.pad_nodes = defaultdict(list)
        self._search_cache = OrderedDict()
        self.net = net
        tracks = [t for t in board.tracks if t.net == net and t.start != t.end]
        lines = [LineString([t.start, t.end]) for t in tracks]
        splits = [{0.0, line.length} for line in lines]
        attachments = []

        def node(point, layer):
            return (round(point[0], 6), round(point[1], 6), layer)

        def edge(a, b, length, item):
            self.adj[a].append((b, max(0., length), item))
            self.adj[b].append((a, max(0., length), item))

        trees = {}
        layer_indices = defaultdict(list)
        for index, track in enumerate(tracks):
            layer_indices[track.layer].append(index)
        for layer, indices in layer_indices.items():
            trees[layer] = (STRtree([lines[i] for i in indices]), indices)
            tree, lookup = trees[layer]
            max_width = max(tracks[i].width for i in indices)
            for i in indices:
                for local_j in tree.query(lines[i].buffer((tracks[i].width + max_width) / 2 + 1e-6)):
                    j = lookup[int(local_j)]
                    if j <= i:
                        continue
                    crossing = lines[i].intersection(lines[j])
                    if crossing.is_empty:
                        # Copper can connect even when centerlines do not
                        # meet, such as a T junction ending inside a wide
                        # power trace. Join only actual overlapping strokes.
                        a, b = nearest_points(lines[i], lines[j])
                        if a.distance(b) <= (tracks[i].width + tracks[j].width) / 2 + 1e-6:
                            splits[i].add(lines[i].project(a))
                            splits[j].add(lines[j].project(b))
                            attachments.append((node(a.coords[0], layer), node(b.coords[0], layer),
                                                a.distance(b), tracks[i].id))
                        continue
                    if crossing.geom_type == "Point":
                        locations = [crossing]
                    elif crossing.geom_type == "MultiPoint":
                        locations = list(crossing.geoms)
                    elif crossing.geom_type == "LineString":
                        locations = [Point(crossing.coords[0]), Point(crossing.coords[-1])]
                    else:
                        locations = []
                    for location in locations:
                        splits[i].add(lines[i].project(location))
                        splits[j].add(lines[j].project(location))

        # Layer-specific anchors also capture an SMD pad touching a via.
        anchors = defaultdict(list)
        for pad in board.pads:
            if pad.net != net:
                continue
            nodes = []
            for layer in pad_layers(pad, board.layers):
                anchor = node(pad.position, layer)
                self.pad_nodes[pad.id].append(anchor)
                anchors[layer].append((anchor, pad_radius(pad), pad.id))
                nodes.append(anchor)
            # Only plated through pads have multiple copper layers in parsed
            # model. Unsupported pad constructions are parser limitations.
            for a, b in zip(nodes, nodes[1:]):
                edge(a, b, 0, pad.id)
        for via in board.vias:
            if via.net != net:
                continue
            nodes = []
            for layer in layer_span(via.layers, board.layers):
                anchor = node(via.position, layer)
                anchors[layer].append((anchor, via.diameter / 2, via.id))
                nodes.append(anchor)
            for a, b in zip(nodes, nodes[1:]):
                edge(a, b, 0, via.id)

        for layer, contacts in anchors.items():
            if layer in trees:
                tree, lookup = trees[layer]
                max_width = max(tracks[i].width for i in lookup)
                for anchor, radius, item in contacts:
                    position = Point(anchor[:2])
                    for local_index in tree.query(position.buffer(radius + max_width / 2 + 1e-5)):
                        index = lookup[int(local_index)]
                        line = lines[index]
                        if line.distance(position) > radius + tracks[index].width / 2 + 1e-5:
                            continue
                        distance = line.project(position)
                        splits[index].add(distance)
                        target = line.interpolate(distance)
                        attachments.append((anchor, node(target.coords[0], layer),
                                            position.distance(target), item))
            if contacts:
                contact_points = [Point(c[0][:2]) for c in contacts]
                tree = STRtree(contact_points)
                max_radius = max(c[1] for c in contacts)
                for i, (anchor, radius, item) in enumerate(contacts):
                    for j in tree.query(contact_points[i].buffer(radius + max_radius + 1e-5)):
                        j = int(j)
                        if j <= i:
                            continue
                        other, other_radius, _ = contacts[j]
                        distance = math.dist(anchor[:2], other[:2])
                        if distance <= radius + other_radius + 1e-5:
                            edge(anchor, other, distance, item)

        for i, distances in enumerate(splits):
            line, track = lines[i], tracks[i]
            ordered = sorted(distances)
            for start, end in zip(ordered, ordered[1:]):
                edge(node(line.interpolate(start).coords[0], track.layer),
                     node(line.interpolate(end).coords[0], track.layer), end - start, track.id)
        for a, b, length, item in attachments:
            edge(a, b, length, item)

    def shortest(self, start: Pad, end: Pad) -> RoutedPath | None:
        return self.shortest_many([start], [end])

    def shortest_many(self, start_pads: list[Pad], end_pads: list[Pad]) -> RoutedPath | None:
        starts = tuple(sorted({node for pad in start_pads for node in self.pad_nodes.get(pad.id, [])}))
        targets = {node for pad in end_pads for node in self.pad_nodes.get(pad.id, [])}
        if not starts or not targets:
            return None
        if starts in self._search_cache:
            distances, previous = self._search_cache.pop(starts)
        else:
            queue = [(0., point) for point in starts]
            heapq.heapify(queue)
            distances = {point: 0. for point in starts}
            previous = {}
            while queue:
                distance, current = heapq.heappop(queue)
                if distance > distances.get(current, math.inf):
                    continue
                for target, length, item in self.adj[current]:
                    candidate = distance + length
                    if candidate + 1e-9 < distances.get(target, math.inf):
                        distances[target] = candidate
                        previous[target] = (current, item)
                        heapq.heappush(queue, (candidate, target))
        self._search_cache[starts] = distances, previous
        # Bound memory while reusing the same IC's tree across its capacitor
        # candidates. Multi-source search handles ICs with many supply pads.
        while len(self._search_cache) > 8:
            self._search_cache.popitem(last=False)
        current = min(targets, key=lambda target: distances.get(target, math.inf))
        distance = distances.get(current)
        if distance is None:
            return None
        nodes, items = [current], []
        while current in previous:
            current, item = previous[current]
            nodes.append(current)
            items.append(item)
        return RoutedPath(distance, [p[:2] for p in reversed(nodes)], sorted(set(items)))


def best_path(graph: RouteGraph, starts: list[Pad], ends: list[Pad]) -> RoutedPath | None:
    return graph.shortest_many(starts, ends)
