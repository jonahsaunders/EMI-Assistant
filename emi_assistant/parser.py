"""Read KiCad PCB snapshots without importing KiCad or executing file content.

The parser consumes saved filled copper, never the editable zone boundary. KiCad
serializes holes as bridged polygon contours; retain their vertex order so the
engine's even-odd containment test also excludes those holes.

Format: https://dev-docs.kicad.org/en/file-formats/sexpr-pcb/
Footprint transforms follow BOARD_ITEM::SetFPRelativePosition in KiCad source:
saved back-side pad coordinates are already mirrored, so do not reflect twice.
"""
from __future__ import annotations

from hashlib import sha256
import math
from pathlib import Path
import re
from typing import Iterator

from .models import BoardSnapshot, CopperPolygon, Footprint, Pad, Point, Track, Via

MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_DEPTH = 256
ARC_CHORD_ERROR_MM = 0.025
_BOARD_METADATA = {
    'version', 'generator', 'generator_version', 'general', 'paper', 'page',
    'title_block', 'layers', 'setup', 'property', 'net', 'net_class', 'group',
    'embedded_fonts', 'embedded_files', 'image', 'dimension',
}


class BoardParseError(ValueError):
    """A file cannot be interpreted safely as a KiCad board."""


def _tokens(text: str) -> Iterator[tuple[str, bool]]:
    """Yield atoms and delimiters with quotedness; no regex string unescaping."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace() or c == '\ufeff':
            i += 1
        elif c == ';':
            end = text.find('\n', i)
            i = n if end < 0 else end + 1
        elif c in '()':
            yield c, False
            i += 1
        elif c == '"':
            start = i
            i += 1
            value = []
            while i < n and text[i] != '"':
                if text[i] == '\\':
                    i += 1
                    if i >= n:
                        raise BoardParseError('Unterminated escape at end of file.')
                    escape = text[i]
                    value.append({'n': '\n', 'r': '\r', 't': '\t', '"': '"', '\\': '\\'}.get(escape, '\\' + escape))
                else:
                    value.append(text[i])
                i += 1
            if i >= n:
                raise BoardParseError(f'Unterminated quoted string near character {start}.')
            i += 1
            yield ''.join(value), True
        else:
            start = i
            while i < n and not text[i].isspace() and text[i] not in '();"':
                i += 1
            if i == start:
                raise BoardParseError(f'Unexpected character near {i}.')
            yield text[start:i], False


def _sexpr(text: str, root_kind: str = 'kicad_pcb') -> list:
    stack: list[list] = []
    root = None
    for token, quoted in _tokens(text):
        if not quoted and token == '(':
            if len(stack) >= MAX_DEPTH:
                raise BoardParseError('Board contains excessively nested data.')
            node: list = []
            if stack:
                stack[-1].append(node)
            elif root is not None:
                raise BoardParseError('Expected one board, but found additional root data.')
            else:
                root = node
            stack.append(node)
        elif not quoted and token == ')':
            if not stack:
                raise BoardParseError('Unexpected closing parenthesis.')
            stack.pop()
        elif not stack:
            raise BoardParseError('Expected a KiCad board beginning with (kicad_pcb ...).')
        else:
            stack[-1].append(token)
    if stack:
        raise BoardParseError('Incomplete board: a closing parenthesis is missing.')
    if not root or root[0] != root_kind:
        if root_kind == 'kicad_pcb':
            raise BoardParseError('This is not a .kicad_pcb board. Open a PCB file, not a schematic or footprint.')
        raise BoardParseError(f'This is not a {root_kind} file.')
    return root


def _children(node: list, key: str) -> list[list]:
    return [x for x in node[1:] if isinstance(x, list) and x and x[0] == key]


def _child(node: list, key: str) -> list:
    return next(iter(_children(node, key)), [])


def _value(node: list, key: str, default: str = '') -> str:
    value = _child(node, key)
    if len(value) > 1 and isinstance(value[1], str):
        return value[1]
    return default


def _float(value: str, context: str) -> float:
    try:
        number = float(value)
        if not math.isfinite(number) or abs(number) > 1e7:
            raise ValueError()
        return number
    except (ValueError, TypeError):
        raise BoardParseError(f'Invalid numeric value {value!r} in {context}.') from None


def _number(node: list, key: str, default: float | None = None) -> float:
    value = _value(node, key)
    if not value:
        if default is not None:
            return default
        raise BoardParseError(f'Missing {key} in {node[0]}.')
    return _float(value, f'{node[0]} {key}')


def _point(node: list, key: str = 'at', default: Point | None = None) -> Point:
    value = _child(node, key)
    if len(value) < 3:
        if default is not None:
            return default
        raise BoardParseError(f'Missing or incomplete {key} coordinates in {node[0]}.')
    return (_float(value[1], f'{node[0]} {key}'), _float(value[2], f'{node[0]} {key}'))


def _identifier(node: list, context: str) -> str:
    return _value(node, 'uuid') or _value(node, 'tstamp') or 'generated-' + sha256((context + repr(node)).encode()).hexdigest()[:24]


def _layer_key(layer: str) -> tuple[int, int]:
    if layer == 'F.Cu':
        return (0, 0)
    match = re.fullmatch(r'In(\d+)\.Cu', layer)
    if match:
        return (1, int(match.group(1)))
    return (2, 0)


def _transform(point: Point, origin: Point, angle: float) -> Point:
    # KiCad's positive orientation is counterclockwise with screen Y down.
    a = math.radians(angle)
    x, y = point
    return (origin[0] + x * math.cos(a) + y * math.sin(a),
            origin[1] - x * math.sin(a) + y * math.cos(a))


def _arc_points(start: Point, mid: Point, end: Point) -> list[Point]:
    """Approximate a 3-point circular arc with bounded chord error."""
    ax, ay = start
    bx, by = mid
    cx, cy = end
    determinant = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(determinant) < 1e-12:
        raise BoardParseError('Arc has collinear or duplicate control points; repair it in KiCad.')
    aa, bb, cc = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    center = ((aa * (by - cy) + bb * (cy - ay) + cc * (ay - by)) / determinant,
              (aa * (cx - bx) + bb * (ax - cx) + cc * (bx - ax)) / determinant)
    angles = [math.atan2(p[1] - center[1], p[0] - center[0]) for p in (start, mid, end)]
    tau = 2 * math.pi
    ccw = (angles[2] - angles[0]) % tau
    sweep = ccw if (angles[1] - angles[0]) % tau <= ccw else ccw - tau
    radius = math.dist(start, center)
    step = min(math.pi / 18, 2 * math.acos(max(-1, 1 - ARC_CHORD_ERROR_MM / radius)))
    count = max(2, math.ceil(abs(sweep) / max(step, 1e-6)))
    if count > 100000:
        raise BoardParseError('Arc is too large to analyze with bounded accuracy.')
    points = [(center[0] + radius * math.cos(angles[0] + sweep * i / count),
               center[1] + radius * math.sin(angles[0] + sweep * i / count)) for i in range(count + 1)]
    points[0], points[-1] = start, end
    return points


def parse_board(text: str, path: str = '') -> BoardSnapshot:
    """Parse KiCad 6+ geometry; report omitted constructs as coverage warnings.

    No CAD validity or fill freshness is implied by successful parsing. The live
    adapter can supply the exact unsaved get_as_string() snapshot to this reader.
    """
    if not isinstance(text, str):
        raise BoardParseError('Board content must be UTF-8 text.')
    if len(text.encode('utf-8')) > MAX_FILE_BYTES:
        raise BoardParseError('Board exceeds the 128 MB analysis limit.')
    root = _sexpr(text)
    board = BoardSnapshot(name=Path(path).stem if path else 'Untitled board', path=path,
                          fingerprint=sha256(text.encode('utf-8')).hexdigest(), layers=[])
    warnings: list[str] = board.warnings
    version = _value(root, 'version')
    if not version:
        warnings.append('Board file has no version header; geometry support cannot be confirmed.')
    elif not version.isdigit():
        raise BoardParseError('Invalid KiCad board version header.')
    elif int(version) < 20210101:
        warnings.append('Legacy board version: resave in KiCad 8 or later to improve geometry coverage.')

    aliases: dict[str, str] = {}
    layer_section = _child(root, 'layers')
    for entry in layer_section[1:]:
        if not isinstance(entry, list) or len(entry) < 3:
            raise BoardParseError('Invalid board layer declaration.')
        name = entry[1]
        aliases[name] = name
        if len(entry) > 3 and isinstance(entry[3], str):
            aliases[entry[3]] = name
        if name.endswith('.Cu'):
            board.layers.append(name)
    board.layers = sorted(set(board.layers), key=_layer_key)
    if not board.layers:
        observed: set[str] = set()
        def scan_layers(node: list) -> None:
            if node and node[0] in ('layer', 'layers'):
                observed.update(x for x in node[1:] if isinstance(x, str) and x.endswith('.Cu') and '*' not in x)
            for child in node:
                if isinstance(child, list):
                    scan_layers(child)
        scan_layers(root)
        board.layers = sorted(observed or {'F.Cu', 'B.Cu'}, key=_layer_key)
        warnings.append('Layer stack is missing; copper layer order is inferred from names.')

    net_names = {'0': ''}
    for net in _children(root, 'net'):
        if len(net) >= 3:
            if net[1] in net_names and net_names[net[1]] != net[2]:
                raise BoardParseError(f'Conflicting declarations for net {net[1]}.')
            net_names[net[1]] = net[2]
        elif len(net) == 2:
            net_names[net[1]] = net[1]  # Named-net format in newer snapshots.
        else:
            raise BoardParseError('Empty net declaration.')

    def net_of(node: list) -> str:
        net = _child(node, 'net')
        explicit = _value(node, 'net_name')
        if len(net) >= 3:
            if net[1] in net_names and net_names[net[1]] != net[2]:
                raise BoardParseError(f'Net code/name mismatch in {node[0]}.')
            return net[2]
        if len(net) == 2:
            if net[1] in net_names:
                result = net_names[net[1]]
                if explicit and explicit != result:
                    raise BoardParseError(f'Net name mismatch in {node[0]}.')
                return result
            if re.fullmatch(r'\d+', net[1]):
                if explicit:
                    return explicit
                warnings.append(f'Unknown net code {net[1]} in {node[0]}; connectivity is incomplete.')
                return f'<unknown net {net[1]}>'
            return net[1]
        return explicit

    def layer_of(node: list, default: str = '') -> str:
        name = _value(node, 'layer', default)
        return aliases.get(name, name)

    def layers_of(node: list, default: list[str] | None = None, span: bool = False) -> list[str]:
        value = _child(node, 'layers')
        names = value[1:] if value else default or [layer_of(node)]
        expanded: list[str] = []
        for name in names:
            if not isinstance(name, str):
                raise BoardParseError(f'Invalid layer in {node[0]}.')
            name = aliases.get(name, name)
            if name == '*.Cu':
                expanded.extend(board.layers)
            elif name == 'F&B.Cu':
                expanded.extend(x for x in ('F.Cu', 'B.Cu') if x in board.layers)
            elif name.endswith('.Cu'):
                expanded.append(name)
        if span and len(expanded) == 2 and all(x in board.layers for x in expanded):
            a, b = sorted(board.layers.index(x) for x in expanded)
            return board.layers[a:b + 1]
        return sorted(set(expanded), key=_layer_key)

    setup = _child(root, 'setup')
    for layer in _children(_child(setup, 'stackup'), 'layer'):
        if len(layer) < 2:
            raise BoardParseError('Invalid stackup layer.')
        info: dict = {'name': layer[1]}
        for field in layer[2:]:
            if isinstance(field, list) and len(field) >= 2:
                info[field[0]] = _float(field[1], f'stackup {field[0]}') if field[0] in ('thickness', 'epsilon_r', 'loss_tangent') else field[1]
        board.stackup.append(info)
    if not board.stackup:
        warnings.append('Physical stackup is unavailable; reference-layer selection uses layer order unless configured.')

    def points_of(node: list) -> list[Point]:
        pts = _child(node, 'pts')
        result = []
        for point in pts[1:]:
            if not isinstance(point, list) or not point:
                raise BoardParseError('Invalid polygon point.')
            if point[0] == 'xy' and len(point) >= 3:
                result.append((_float(point[1], 'polygon x'), _float(point[2], 'polygon y')))
            else:
                raise BoardParseError(f'Unsupported polygon coordinate {point[0]!r}; refill zones in KiCad and save.')
        if len(result) < 3:
            raise BoardParseError('Filled polygon has fewer than three points.')
        return result

    def graphic(node: list, origin: Point = (0., 0.), angle: float = 0.) -> None:
        layer = layer_of(node)
        kind = node[0]
        if layer != 'Edge.Cuts':
            if layer.endswith('.Cu'):
                warnings.append(f'Copper graphic {kind} is not included in electrical connectivity; inspect it manually.')
            return
        if kind in ('gr_line', 'fp_line'):
            pts = [_point(node, 'start'), _point(node, 'end')]
        elif kind in ('gr_rect', 'fp_rect'):
            start, end = _point(node, 'start'), _point(node, 'end')
            pts = [start, (end[0], start[1]), end, (start[0], end[1]), start]
        elif kind in ('gr_poly', 'fp_poly'):
            pts = points_of(node)
            pts = pts + [pts[0]]
        elif kind in ('gr_arc', 'fp_arc') and _child(node, 'mid'):
            pts = _arc_points(_point(node, 'start'), _point(node, 'mid'), _point(node, 'end'))
        elif kind in ('gr_circle', 'fp_circle'):
            center, end = _point(node, 'center'), _point(node, 'end')
            radius = math.dist(center, end)
            if radius <= 0:
                raise BoardParseError('Edge.Cuts circle has zero radius.')
            count = max(36, math.ceil(2 * math.pi / max(1e-6, 2 * math.acos(max(-1, 1 - ARC_CHORD_ERROR_MM / radius)))))
            if count > 100000:
                raise BoardParseError('Board circle is too large.')
            pts = [(center[0] + radius * math.cos(2 * math.pi * i / count), center[1] + radius * math.sin(2 * math.pi * i / count)) for i in range(count + 1)]
            pts[-1] = pts[0]
        else:
            warnings.append(f'Unsupported Edge.Cuts {kind}; board-outline coverage is incomplete.')
            return
        pts = [_transform(p, origin, angle) for p in pts]
        board.outline.extend(zip(pts, pts[1:]))

    arc_count = 0
    for index, item in enumerate(root[1:]):
        if not isinstance(item, list) or not item:
            continue
        kind = item[0]
        item_id = _identifier(item, str(index))
        if kind in ('segment', 'arc'):
            width, layer, net = _number(item, 'width'), layer_of(item), net_of(item)
            if width <= 0 or not layer.endswith('.Cu'):
                raise BoardParseError(f'Invalid track width or copper layer for {item_id}.')
            if layer not in board.layers:
                warnings.append(f'Track layer {layer} is absent from the declared copper stack.')
            if kind == 'arc':
                pts = _arc_points(_point(item, 'start'), _point(item, 'mid'), _point(item, 'end'))
                arc_count += 1
            else:
                pts = [_point(item, 'start'), _point(item, 'end')]
            board.tracks.extend(Track(item_id, a, b, width, layer, net) for a, b in zip(pts, pts[1:]))
        elif kind == 'via':
            size, drill = _number(item, 'size'), _number(item, 'drill')
            if drill <= 0 or size <= drill:
                raise BoardParseError(f'Via {item_id} has invalid drill or diameter.')
            layers = layers_of(item, [board.layers[0], board.layers[-1]], span=True)
            if len(layers) < 2:
                raise BoardParseError(f'Via {item_id} must span at least two copper layers.')
            board.vias.append(Via(item_id, _point(item), size, drill, layers, net_of(item)))
            if _child(item, 'padstack') or _child(item, 'remove_unused_layers'):
                warnings.append('Via padstack or removed annuli are simplified; layer contact requires manual verification.')
        elif kind in ('footprint', 'module'):
            props = {p[1]: p[2] for p in _children(item, 'property') if len(p) >= 3}
            if _value(item, 'path'):
                props['__kicad_path'] = _value(item, 'path')
            for field in _children(item, 'fp_text'):
                if len(field) >= 3 and field[1] in ('reference', 'value'):
                    props.setdefault(field[1].title(), field[2])
            fp = Footprint(item_id, props.get('Reference', '?'), props.get('Value', item[1] if len(item) > 1 else ''),
                           _point(item, default=(0., 0.)), properties=props)
            at = _child(item, 'at')
            angle = _float(at[3], 'footprint angle') if len(at) > 3 else 0.
            for pi, pad in enumerate(_children(item, 'pad')):
                if len(pad) < 4:
                    raise BoardParseError(f'Malformed pad in {fp.reference}.')
                layers = layers_of(pad)
                if not layers or pad[2] == 'np_thru_hole':
                    continue
                size = _point(pad, 'size')
                if min(size) <= 0:
                    raise BoardParseError(f'Pad {pad[1]} in {fp.reference} has invalid size.')
                # Pad shape orientation is intentionally not needed for centerline
                # graph checks; exact annuli/clearance are not modeled here.
                fp.pads.append(Pad(_identifier(pad, f'{item_id}:{pi}'), pad[1],
                                   _transform(_point(pad, default=(0., 0.)), fp.position, angle),
                                   size, layers, net_of(pad), item_id))
                if pad[3] == 'custom' or _child(pad, 'padstack'):
                    warnings.append(f'{fp.reference} pad {pad[1]} uses custom/padstack geometry; only its anchor is analyzed.')
            for child in item[1:]:
                if isinstance(child, list) and child and child[0].startswith('fp_'):
                    graphic(child, fp.position, angle)
                elif isinstance(child, list) and child and child[0] == 'zone':
                    warnings.append(f'Footprint-local zone in {fp.reference} is not modeled; inspect it manually.')
            board.footprints.append(fp)
        elif kind == 'zone':
            if _child(item, 'keepout'):
                continue
            zone_layers = layers_of(item)
            if not zone_layers:
                continue
            fills = _children(item, 'filled_polygon')
            if not fills:
                warnings.append(f'Zone {item_id} on {", ".join(zone_layers)} has no saved filled copper. Press B in KiCad to refill, save, then analyze again.')
            for polygon in fills:
                layer = layer_of(polygon, zone_layers[0] if len(zone_layers) == 1 else '')
                if not layer:
                    warnings.append(f'A filled polygon in zone {item_id} has no layer and was skipped.')
                    continue
                holes = [points_of(hole) for hole in _children(polygon, 'hole')]
                board.copper.append(CopperPolygon(item_id, net_of(item), layer, points_of(polygon), holes))
            filled_layers = {layer_of(polygon, zone_layers[0] if len(zone_layers) == 1 else '') for polygon in fills}
            for missing_layer in set(zone_layers) - filled_layers:
                if fills:
                    warnings.append(f'Zone {item_id} has no saved fill on {missing_layer}; reference copper on that layer is unknown.')
            if _child(item, 'fill_segments') or _child(item, 'filled_segments'):
                warnings.append(f'Zone {item_id} uses legacy segmented fills; refill and save in KiCad.')
        elif kind.startswith('gr_'):
            graphic(item)
        elif kind not in _BOARD_METADATA:
            warnings.append(f'Unsupported board construct {kind!r} was not analyzed; inspect it in KiCad before relying on coverage.')

    if arc_count:
        warnings.append(f'{arc_count} track arc(s) were flattened with at most {ARC_CHORD_ERROR_MM:g} mm chord error; source object IDs are preserved.')
    if not board.copper:
        warnings.append('No saved filled copper was found. Reference-plane continuity cannot be established from zone outlines.')
    if not board.outline:
        warnings.append('No supported Edge.Cuts outline was found; board-edge checks are unavailable.')
    warnings.append('Saved zone fill freshness cannot be established from file contents; refill zones in KiCad before relying on plane checks.')
    board.warnings = list(dict.fromkeys(warnings))
    return board


def load_board(path: str) -> BoardSnapshot:
    """Read a local board with actionable errors and bounded input size."""
    file = Path(path).expanduser()
    try:
        if file.stat().st_size > MAX_FILE_BYTES:
            raise BoardParseError('Board exceeds the 128 MB analysis limit.')
        text = file.read_text(encoding='utf-8-sig')
    except UnicodeError as exc:
        raise BoardParseError('Board is not valid UTF-8 text. Open and resave it in KiCad.') from exc
    except OSError as exc:
        raise BoardParseError(f'Could not read PCB: {exc.strerror or str(exc)}') from exc
    return parse_board(text, str(file.resolve()))
