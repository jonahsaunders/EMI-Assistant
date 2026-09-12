"""Optional context from saved KiCad schematics; never changes PCB connectivity.

Only the board's same-stem schematic and its explicit in-project sheet references
are read. Identity, value, and duplicate-reference conflicts fail closed.

Format references:
https://dev-docs.kicad.org/en/file-formats/sexpr-schematic/
https://dev-docs.kicad.org/en/file-formats/sexpr-intro/
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re

from .models import BoardSnapshot
from .parser import BoardParseError, _child, _children, _sexpr, _value

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 64
MAX_INSTANCES = 128
MAX_SHEET_DEPTH = 16
PREFIX = '__schematic_'


@dataclass
class _Symbol:
    reference: str
    value: str
    uuid: str
    path: tuple[str, ...]
    unit: str
    lib_id: str
    properties: dict[str, str]
    pins: dict[str, dict[str, str]]
    source: str
    dnp: bool


def _properties(node: list) -> dict[str, str]:
    return {p[1]: p[2] for p in _children(node, 'property')
            if len(p) >= 3 and isinstance(p[1], str) and isinstance(p[2], str)}


def _path(value: str, root_uuid: str) -> tuple[str, ...]:
    parts = tuple(x for x in value.split('/') if x)
    return parts[1:] if parts and root_uuid and parts[0] == root_uuid else parts


def _pins(definition: list, unit: str) -> dict[str, dict[str, str]]:
    """Select the instance's unit plus common pins; use the normal body style."""
    result: dict[str, dict[str, str]] = {}
    sections = [definition]
    for section in _children(definition, 'symbol'):
        match = re.search(r'_(\d+)_(\d+)$', section[1] if len(section) > 1 else '')
        if match and match[1] in ('0', unit) and match[2] in ('0', '1'):
            sections.append(section)
    for section in sections:
        for pin in _children(section, 'pin'):
            number = _value(pin, 'number')
            if number and len(pin) > 1 and isinstance(pin[1], str):
                value = {'name': _value(pin, 'name'), 'type': pin[1]}
                # Ambiguous stacked definitions must not produce power claims.
                if number in result and result[number] != value:
                    result[number] = {'name': '', 'type': 'unspecified'}
                else:
                    result[number] = value
    return result


def _role(lib_id: str) -> str:
    namespace, _, name = lib_id.partition(':')
    if namespace == 'Oscillator':
        return 'oscillator'
    if namespace == 'Device' and name in ('Crystal', 'Crystal_Small', 'Crystal_GND2', 'Crystal_GND3'):
        return 'crystal'
    if namespace == 'Device' and name in ('C', 'C_Small', 'C_Polarized', 'C_Polarized_Small'):
        return 'capacitor'
    if namespace == 'Regulator_Switching':
        return 'switching_regulator'
    if namespace in ('Connector', 'Connector_Generic', 'Connector_Generic_MountingPin'):
        return 'connector'
    return ''


def _frequency(properties: dict[str, str], role: str, value: str) -> tuple[str, str]:
    candidates = [(text, 'property: ' + key) for key, text in properties.items()
                  if key.casefold().replace('_', ' ') in ('frequency', 'clock frequency', 'oscillator frequency')]
    if role in ('oscillator', 'crystal'):
        candidates.append((value, 'symbol value'))
    for text, source in candidates:
        match = re.fullmatch(r'\s*(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(GHz|MHz|kHz|Hz)\s*', text, re.I)
        if match:
            number = float(match[1]) * {'ghz': 1000, 'mhz': 1, 'khz': .001, 'hz': .000001}[match[2].lower()]
            if math.isfinite(number) and 0 < number <= 1e6:
                return format(number, '.12g'), source
    return '', ''


def enrich(board: BoardSnapshot) -> list[str]:
    """Enrich matched footprints with namespaced saved context, return warnings.

    This is supplemental context, not a schematic-to-PCB synchronization check.
    The caller should append returned warnings to the current analysis snapshot.
    Repeated calls remove previous supplemental fields before loading fresh data.
    No file is written and no schematic net is assigned to a PCB item.
    """
    for footprint in board.footprints:
        for key in list(footprint.properties):
            if key.startswith(PREFIX):
                del footprint.properties[key]
    if not board.path:
        return []
    board_file = Path(board.path).expanduser().absolute()
    project_root = board_file.parent.resolve()
    root_file = board_file.with_suffix('.kicad_sch')
    if not root_file.exists():
        return []
    warnings: list[str] = []
    cache: dict[Path, list] = {}
    total_bytes = 0
    instances = 0
    sheet_references = 0

    def read(file: Path) -> list:
        nonlocal total_bytes
        resolved = file.resolve()
        if not resolved.is_relative_to(project_root):
            raise ValueError('sheet points outside the PCB project directory')
        if resolved in cache:
            return cache[resolved]
        if len(cache) >= MAX_FILES:
            raise ValueError('schematic exceeds the 64-file context limit')
        size = resolved.stat().st_size
        if size > MAX_FILE_BYTES or total_bytes + size > MAX_TOTAL_BYTES:
            raise ValueError('schematic exceeds the context size limit')
        with resolved.open('rb') as stream:
            content = stream.read(min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total_bytes) + 1)
        if len(content) > MAX_FILE_BYTES or total_bytes + len(content) > MAX_TOTAL_BYTES:
            raise ValueError('schematic exceeds the context size limit')
        total_bytes += len(content)
        root = _sexpr(content.decode('utf-8-sig'), root_kind='kicad_sch')
        cache[resolved] = root
        return root

    try:
        root = read(root_file)
    except (OSError, UnicodeError, ValueError) as exc:
        return [f'Saved schematic context unavailable: {exc}. PCB analysis can continue.']
    root_uuid = _value(root, 'uuid')
    project_name = root_file.stem
    legacy_instances: dict[tuple[str, ...], list] = {}
    duplicate_legacy_paths: set[tuple[str, ...]] = set()
    for entry in _children(_child(root, 'symbol_instances'), 'path'):
        if len(entry) > 1 and isinstance(entry[1], str):
            path = _path(entry[1], root_uuid)
            if path in legacy_instances:
                duplicate_legacy_paths.add(path)
            legacy_instances[path] = entry
    symbols: list[_Symbol] = []

    def visit(file: Path, document: list, instance_path: tuple[str, ...], ancestry: tuple[Path, ...]) -> None:
        nonlocal instances, sheet_references
        instances += 1
        if instances > MAX_INSTANCES or len(ancestry) >= MAX_SHEET_DEPTH:
            warnings.append('Schematic hierarchy exceeds the context instance/depth limit; remaining sheets were skipped.')
            return
        resolved = file.resolve()
        if resolved in ancestry:
            warnings.append(f'Schematic cycle at {file.name}; that sheet was skipped.')
            return
        source = str(resolved.relative_to(project_root))
        libraries = {s[1]: s for s in _children(_child(document, 'lib_symbols'), 'symbol')
                     if len(s) > 1 and isinstance(s[1], str)}
        for symbol in _children(document, 'symbol'):
            if _value(symbol, 'on_board', 'yes') == 'no':
                continue
            props = _properties(symbol)
            reference, value = props.get('Reference', ''), props.get('Value', '')
            if reference.startswith('#'):
                continue
            uuid, unit = _value(symbol, 'uuid'), _value(symbol, 'unit', '1')
            full_path = instance_path + (uuid,)
            modern = _child(symbol, 'instances')
            if modern:
                candidates = [path for project in _children(modern, 'project')
                              if len(project) > 1 and project[1] == project_name
                              for path in _children(project, 'path')
                              if len(path) > 1 and isinstance(path[1], str)
                              and _path(path[1], root_uuid) == instance_path]
                if len(candidates) != 1:
                    warnings.append(f'Saved schematic instance for {reference or uuid} in {source} is missing or ambiguous; context skipped.')
                    continue
                reference = _value(candidates[0], 'reference')
                unit = _value(candidates[0], 'unit', unit)
            elif full_path in legacy_instances:
                if full_path in duplicate_legacy_paths:
                    warnings.append(f'Duplicate saved schematic instance path for {reference or uuid}; context skipped.')
                    continue
                instance = legacy_instances[full_path]
                reference = _value(instance, 'reference', reference)
                value = _value(instance, 'value', value)
                unit = _value(instance, 'unit', unit)
            if not reference or reference.startswith('#') or reference.endswith('?') or not uuid:
                continue
            lib_id = _value(symbol, 'lib_id')
            definition = libraries.get(lib_id, [])
            if _child(definition, 'extends'):
                warnings.append(f'{reference} uses an inherited library symbol; inherited pin context is unavailable.')
            symbols.append(_Symbol(reference, value, uuid, instance_path, unit, lib_id, props,
                                   _pins(definition, unit), source, _value(symbol, 'dnp') == 'yes'))
        for sheet in _children(document, 'sheet'):
            sheet_references += 1
            if sheet_references > MAX_INSTANCES:
                warnings.append('Schematic exceeds the 128-sheet reference limit; remaining references were skipped.')
                break
            props = _properties(sheet)
            name = props.get('Sheetfile', props.get('Sheet file', ''))
            uuid = _value(sheet, 'uuid')
            if not name or not uuid:
                warnings.append(f'A sheet in {source} has no file or UUID; its context was skipped.')
                continue
            # Do not expand variables, resolve URLs, or crawl for missing files.
            target = Path(name.replace('\\', '/'))
            if target.is_absolute() or '$' in name or target.suffix.lower() != '.kicad_sch':
                warnings.append(f'Sheet reference {name!r} in {source} is not a supported project-relative schematic.')
                continue
            child_file = resolved.parent / target
            try:
                child_document = read(child_file)
                visit(child_file, child_document, instance_path + (uuid,), ancestry + (resolved,))
            except (OSError, UnicodeError, ValueError) as exc:
                warnings.append(f'Saved sheet {name!r} in {source} could not be loaded: {exc}.')

    try:
        visit(root_file, root, (), ())
    except (BoardParseError, TypeError, IndexError) as exc:
        return [f'Saved schematic context is malformed and was skipped: {exc}.']
    by_reference: dict[str, list[_Symbol]] = defaultdict(list)
    for symbol in symbols:
        by_reference[symbol.reference].append(symbol)
    by_path = {s.path + (s.uuid,): s for s in symbols}
    pcb_counts = Counter(fp.reference for fp in board.footprints)
    matched = 0
    for footprint in board.footprints:
        group = by_reference.get(footprint.reference, [])
        if not group:
            path = footprint.properties.get('__kicad_path', '')
            renamed = by_path.get(_path(path, root_uuid)) if path else None
            if renamed:
                warnings.append(f'PCB {footprint.reference} is named {renamed.reference} in the saved schematic at the same UUID/path; context may be stale and was skipped.')
            continue
        if pcb_counts[footprint.reference] != 1:
            warnings.append(f'Duplicate PCB reference {footprint.reference}; schematic context skipped for that reference.')
            continue
        # Multiple units may share a physical footprint. Repeated units or paths
        # indicate duplicate annotations and cannot be merged safely.
        if (len({s.path for s in group}) != 1 or len({s.unit for s in group}) != len(group)
                or len({s.lib_id for s in group}) != 1 or len({s.value for s in group}) != 1):
            warnings.append(f'Duplicate or conflicting schematic reference {footprint.reference}; context skipped.')
            continue
        primary = group[0]
        pcb_path = footprint.properties.get('__kicad_path', '')
        if pcb_path and _path(pcb_path, root_uuid) not in {s.path + (s.uuid,) for s in group}:
            warnings.append(f'{footprint.reference} has a different schematic UUID/path in the PCB; saved context may be stale and was skipped.')
            continue
        if footprint.value.strip() != primary.value.strip():
            warnings.append(f'{footprint.reference} value differs between PCB ({footprint.value!r}) and saved schematic ({primary.value!r}); context may be stale and was skipped.')
            continue
        props = footprint.properties
        props[PREFIX + 'source'] = primary.source
        props[PREFIX + 'match'] = 'path' if pcb_path else 'unique reference and value'
        props[PREFIX + 'path'] = '/' + '/'.join(primary.path + (primary.uuid,))
        props[PREFIX + 'lib_id'] = primary.lib_id
        props[PREFIX + 'properties'] = json.dumps(primary.properties, sort_keys=True)
        conflicting_properties = {key for key, value in primary.properties.items()
                                  if key not in ('Reference', 'Value') and key in props
                                  and props[key].strip() != value.strip()}
        if conflicting_properties:
            warnings.append(f'{footprint.reference} has differing PCB and saved schematic properties ({", ".join(sorted(conflicting_properties))}); conflicting saved values are not used for frequency inference.')
        pin_numbers = {p.number for p in footprint.pads}
        pins: dict[str, dict[str, str]] = {}
        for symbol in group:
            for number, definition in symbol.pins.items():
                if number in pin_numbers:
                    if number in pins and pins[number] != definition:
                        pins[number] = {'name': '', 'type': 'unspecified'}
                    else:
                        pins[number] = definition
        props[PREFIX + 'pins'] = json.dumps(pins, sort_keys=True)
        props[PREFIX + 'power_input_pins'] = json.dumps(sorted(n for n, p in pins.items() if p['type'] == 'power_in'))
        role = _role(primary.lib_id)
        if role:
            props[PREFIX + 'role'] = role
            props[PREFIX + 'role_basis'] = 'Library symbol category: ' + primary.lib_id
        frequency_properties = {key: value for key, value in primary.properties.items()
                                if key not in conflicting_properties}
        frequency, frequency_source = _frequency(frequency_properties, role, primary.value)
        if frequency:
            props[PREFIX + 'frequency_mhz'] = frequency
            props[PREFIX + 'frequency_source'] = frequency_source
        if any(s.dnp for s in group):
            props[PREFIX + 'dnp'] = 'yes'
            warnings.append(f'{footprint.reference} is marked DNP in the saved schematic; actual assembly status is unverified.')
        matched += 1
    if matched:
        warnings.append(f'Saved schematic context matched {matched} footprint(s). Unsaved schematic edits and schematic-to-PCB netlist synchronization are not checked.')
    return list(dict.fromkeys(warnings))
