"""Read saved circuit connectivity with KiCad's native XML netlist exporter.

Schematics are staged in a private job directory; original files are never
modified. Embedded symbol definitions make custom symbols work without guessing
connections from the drawing. SPICE source text is never executed here.

CLI: https://docs.kicad.org/10.0/en/cli/cli.html#schematic-export-netlist
XML: https://docs.kicad.org/8.0/en/eeschema/eeschema.html#intermediate-netlist-file
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import xml.etree.ElementTree as ET

from .parser import _children, _sexpr, _value

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 128
MAX_DEPTH = 16
MAX_XML_BYTES = 32 * 1024 * 1024
MAX_COMPONENTS = 100000
MODEL_FIELDS = {'sim.library', 'spice_lib_file', 'spice.lib.file'}


class _InputError(ValueError):
    pass


def _props(node):
    return {p[1]: p[2] for p in _children(node, 'property')
            if len(p) >= 3 and isinstance(p[1], str) and isinstance(p[2], str)}


def _root_source(board, settings):
    selected = settings.get('simulation', {}).get('schematic_path', '')
    if selected:
        file = Path(selected).expanduser()
        if not file.is_absolute():
            if not board.path:
                raise _InputError('Choose an absolute schematic path or save the PCB first.')
            file = Path(board.path).resolve().parent / file
    elif board.path:
        file = Path(board.path).expanduser().resolve().with_suffix('.kicad_sch')
    else:
        raise _InputError('Save the PCB and its matching schematic, or choose the root schematic.')
    file = file.resolve()
    if file.suffix.lower() != '.kicad_sch':
        raise _InputError('Choose a native .kicad_sch root schematic.')
    if not file.is_file():
        raise _InputError(f'No saved schematic found at {file}. Layout-based simulations can still run.')
    # Explicit root selection establishes the project boundary. No search occurs.
    return file


def _input_snapshot(board, settings, cancel_event=None):
    source = _root_source(board, settings)
    project = source.parent
    files = {}
    schematics = {}
    missing = []
    warnings = ['Uses saved schematic files; unsaved schematic edits are not included.']
    total = 0
    external = set()
    for value in settings.get('simulation', {}).get('external_model_paths', []):
        path = Path(value).expanduser()
        if path.is_absolute():
            external.add(path.resolve())

    def read(path, *, model=False):
        nonlocal total
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError('Schematic preparation cancelled.')
        path = path.resolve()
        if not path.is_relative_to(project) and not (model and path in external):
            raise _InputError('Referenced file is outside the selected project directory: ' + path.name)
        if path in files:
            return files[path]
        if len(files) >= MAX_FILES:
            raise _InputError('Schematic/model dependencies exceed the 128-file limit.')
        if not path.is_file():
            raise _InputError('Referenced file is missing: ' + path.name)
        size = path.stat().st_size
        if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
            raise _InputError('Schematic/model dependencies exceed the size limit.')
        with path.open('rb') as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES or total + len(data) > MAX_TOTAL_BYTES:
            raise _InputError('Schematic/model dependencies exceed the size limit.')
        total += len(data)
        files[path] = data
        return data

    def model_path(value, parent):
        value = value.strip().strip('"')
        value = value.replace('${KIPRJMOD}', str(project)).replace('$(KIPRJMOD)', str(project))
        if '$' in value or '\x00' in value or '://' in value:
            raise _InputError('Model path contains an unresolved variable or URL: ' + value)
        value = value.replace('\\', '/')
        target = Path(value)
        return (target if target.is_absolute() else parent / target).resolve()

    seen_models = set()

    def visit_model(path, depth=0):
        if depth > MAX_DEPTH:
            raise _InputError('Model include depth exceeds the limit.')
        if path in seen_models:
            return
        seen_models.add(path)
        data = read(path, model=True)
        text = data.decode('utf-8-sig', errors='replace')
        # Hash references only. Never execute a library or exported SPICE deck.
        if re.search(r'^\s*\.(?:control|endc)\b|^\s*(?:shell|system)\b', text, re.I | re.M):
            warnings.append(f'{path.name} contains SPICE control commands; arbitrary model/control execution is not supported.')
        for line in text.splitlines():
            match = re.match(r'^\s*\.(include|inc|lib)\s+(?:"([^"]+)"|\x27([^\x27]+)\x27|(\S+))(.*)$', line, re.I)
            if not match:
                continue
            value = next(x for x in match.groups()[1:4] if x is not None)
            # .lib SECTION starts a local library section; .lib FILE SECTION
            # includes a dependency. One-token .lib file is also used in practice.
            if match[1].lower() == 'lib' and not match[5].strip() and not re.search(r'[./\\]', value):
                continue
            visit_model(model_path(value, path.parent), depth + 1)

    def visit_schematic(path, ancestry=()):
        path = path.resolve()
        if path in ancestry:
            raise _InputError('Schematic hierarchy contains a cycle at ' + path.name)
        if len(ancestry) >= MAX_DEPTH:
            raise _InputError('Schematic hierarchy exceeds the 16-level limit.')
        if path in schematics:
            return
        doc = _sexpr(read(path).decode('utf-8-sig'), root_kind='kicad_sch')
        schematics[path] = doc
        symbol_nodes = list(_children(doc, 'symbol'))
        for library in _children(doc, 'lib_symbols'):
            symbol_nodes.extend(_children(library, 'symbol'))
        for symbol in symbol_nodes:
            for key, value in _props(symbol).items():
                if key.casefold() in MODEL_FIELDS and value.strip():
                    try:
                        visit_model(model_path(value, project))
                    except (OSError, ValueError) as exc:
                        missing.append(str(exc))
        for sheet in _children(doc, 'sheet'):
            props = _props(sheet)
            value = props.get('Sheetfile', props.get('Sheet file', ''))
            if not value or '$' in value:
                raise _InputError('A hierarchical sheet has a missing or unresolved file path.')
            target = Path(value.replace('\\', '/'))
            if target.is_absolute() or target.suffix.lower() != '.kicad_sch':
                raise _InputError('Hierarchical sheet must use a project-relative .kicad_sch path.')
            visit_schematic(path.parent / target, ancestry + (path,))

    visit_schematic(source)
    # Project text variables/variant definitions can influence native netlisting.
    project_file = source.with_suffix('.kicad_pro')
    if project_file.is_file():
        read(project_file)
    digest = hashlib.sha256()
    for path, data in sorted(files.items(), key=lambda x: str(x[0])):
        name = str(path.relative_to(project)) if path.is_relative_to(project) else str(path)
        digest.update(name.encode('utf-8'))
        digest.update(b'\0')
        digest.update(hashlib.sha256(data).digest())
    digest.update(json.dumps(sorted(set(missing)), ensure_ascii=True).encode())
    return {'source': source, 'project': project, 'files': files, 'schematics': schematics,
            'root_uuid': _value(schematics[source], 'uuid'), 'fingerprint': digest.hexdigest(),
            'warnings': list(dict.fromkeys(warnings)), 'missing': list(dict.fromkeys(missing))}


def fingerprint_inputs(board, settings):
    """Content-only cache identity, including saved hierarchy/model dependencies."""
    try:
        return _input_snapshot(board, settings)['fingerprint']
    except (OSError, ValueError) as exc:
        return hashlib.sha256(('unavailable:' + str(exc)).encode()).hexdigest()


def _text(node, path, default=''):
    item = node.find(path)
    return item.text if item is not None and item.text is not None else default


def _uuid_path(value, root_uuid):
    parts = tuple(p.lower() for p in value.split('/') if p)
    if parts and parts[0] == root_uuid.lower():
        parts = parts[1:]
    return parts


def parse_netlist(data: bytes, board, root_uuid=''):
    """Parse native XML and validate saved circuit identity against the live PCB."""
    if len(data) > MAX_XML_BYTES:
        raise _InputError('Exported netlist exceeds the 32 MiB limit.')
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', data, re.I):
        raise _InputError('Exported XML contains unsupported entity declarations.')
    document = ET.fromstring(data)
    if document.tag != 'export' or document.find('components') is None or document.find('nets') is None:
        raise _InputError('KiCad did not produce a complete XML connectivity netlist.')
    components = []
    by_ref = {}
    warnings, missing = [], []
    for node in document.findall('./components/comp'):
        if len(components) >= MAX_COMPONENTS:
            raise _InputError('Netlist exceeds the component limit.')
        reference = node.get('ref', '')
        if not reference or reference.startswith('#'):
            continue
        if reference in by_ref:
            raise _InputError('Duplicate schematic reference ' + reference)
        properties = {field.get('name', ''): field.text or '' for field in node.findall('./fields/field')
                      if field.get('name')}
        for prop in node.findall('./property'):
            if prop.get('name'):
                properties[prop.get('name')] = prop.get('value', prop.text or '')
        sheet = node.find('sheetpath')
        path = sheet.get('tstamps', '') if sheet is not None else ''
        stamps = _text(node, 'tstamp') or _text(node, 'tstamps')
        # XML tstamp may contain space-separated UUIDs for a multi-unit symbol.
        identifiers = stamps.split()
        paths = [_uuid_path(path.rstrip('/') + '/' + stamp, root_uuid) for stamp in identifiers]
        lib = node.find('libsource')
        component = {'reference': reference, 'value': _text(node, 'value'),
                     'properties': properties, 'pins': {}, 'pin_metadata': {},
                     'footprint': _text(node, 'footprint'),
                     'lib_id': ((lib.get('lib', '') + ':' + lib.get('part', '')) if lib is not None else ''),
                     'uuid_paths': [list(p) for p in paths], 'board_match': False,
                     'match_errors': [], 'item_ids': []}
        if any(k.casefold() in {'exclude_from_board', 'exclude from board'}
               and value.casefold() not in {'no', 'false', '0'} for k, value in properties.items()):
            component['excluded_from_board'] = True
        components.append(component)
        by_ref[reference] = component
    nets = []
    for net in document.findall('./nets/net'):
        name = net.get('name', '')
        if not name:
            raise _InputError('Exported net has no name.')
        pins = []
        for node in net.findall('./node'):
            ref, pin = node.get('ref', ''), node.get('pin', '')
            comp = by_ref.get(ref)
            if comp is None:
                continue
            if not pin:
                raise _InputError('Exported net node has no pin number.')
            if pin in comp['pins'] and comp['pins'][pin] != name:
                raise _InputError(f'{ref} pin {pin} occurs on multiple schematic nets.')
            comp['pins'][pin] = name
            comp['pin_metadata'][pin] = {'name': node.get('pinfunction', ''), 'type': node.get('pintype', '')}
            pins.append({'reference': ref, 'pin': pin})
        nets.append({'name': name, 'code': net.get('code', ''), 'nodes': pins})

    footprints = defaultdict(list)
    for fp in board.footprints:
        footprints[fp.reference].append(fp)
    board_nodes, sch_nodes = defaultdict(set), defaultdict(set)
    for ref, fps in footprints.items():
        if len(fps) == 1:
            for pad in fps[0].pads:
                if pad.net and pad.number:
                    board_nodes[pad.net].add((ref, pad.number))
    for comp in components:
        for pin, name in comp['pins'].items():
            sch_nodes[name].add((comp['reference'], pin))
    aliases = {}
    # Permit differences in escaped/generated names only when the complete
    # physical pin membership proves a unique one-to-one net identity.
    reverse_nodes = defaultdict(list)
    for name, nodes in board_nodes.items():
        reverse_nodes[frozenset(nodes)].append(name)
    for name, nodes in sch_nodes.items():
        if name in board_nodes:
            continue
        candidates = reverse_nodes.get(frozenset(nodes), [])
        if nodes and len(candidates) == 1 and candidates[0] not in sch_nodes:
            aliases[name] = candidates[0]
            warnings.append(f'Schematic net {name!r} maps to PCB net {candidates[0]!r} by identical complete pin membership.')
    for comp in components:
        ref = comp['reference']
        errors = comp['match_errors']
        fps = footprints.get(ref, [])
        if len(fps) != 1:
            if not comp.get('excluded_from_board'):
                errors.append('No unique PCB footprint matches the saved schematic reference.')
        else:
            fp = fps[0]
            comp['item_ids'] = [fp.id]
            if fp.value.strip() != comp['value'].strip():
                errors.append('Component value differs between saved schematic and PCB.')
            pcb_path = fp.properties.get('__kicad_path', '')
            if pcb_path and comp['uuid_paths'] and list(_uuid_path(pcb_path, root_uuid)) not in comp['uuid_paths']:
                errors.append('Schematic UUID/path differs from the PCB footprint.')
            pads = defaultdict(set)
            for pad in fp.pads:
                if pad.number:
                    pads[pad.number].add(pad.net)
            comp['schematic_pins'] = dict(comp['pins'])
            comp['pins'] = {pin: aliases.get(net, net) for pin, net in comp['pins'].items()}
            for pin, name in comp['pins'].items():
                if name.startswith('unconnected-(') and not pads.get(pin, set()) - {''}:
                    continue
                if pads.get(pin) != {name}:
                    errors.append(f'Pin {pin} connectivity differs or has no matching PCB pad.')
            for pin, names in pads.items():
                if names - {''} and pin not in comp['pins']:
                    errors.append(f'Connected PCB pad {pin} is absent from the saved schematic netlist.')
            comp['board_match'] = not errors
            if not pcb_path or not comp['uuid_paths']:
                warnings.append(f'{ref}: identity checked by unique reference, value and pad connectivity; no complete UUID/path available.')
        for error in errors:
            missing.append(ref + ': ' + error)
    for ref, fps in footprints.items():
        if ref not in by_ref and any(p.net for fp in fps for p in fp.pads):
            missing.append(ref + ': connected PCB footprint is absent from the saved schematic netlist.')
    return components, nets, list(dict.fromkeys(warnings)), list(dict.fromkeys(missing)), aliases


def _export(command, cwd, log_path, seconds, cancel_event):
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    with log_path.open('wb') as log:
        process = subprocess.Popen(command, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, creationflags=creationflags, shell=False)
        deadline = time.monotonic() + seconds
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                process.kill()
                process.wait()
                raise InterruptedError('Schematic netlist export cancelled.')
            if time.monotonic() > deadline:
                process.kill()
                process.wait()
                raise TimeoutError('KiCad schematic netlist export timed out.')
            if log_path.stat().st_size > MAX_FILE_BYTES:
                process.kill()
                process.wait()
                raise _InputError('KiCad netlist export exceeded the log size limit.')
            time.sleep(.05)
        return process.returncode


def prepare(board, settings, workdir: Path, kicad_cli: dict, cancel_event=None) -> dict:
    """Return native saved connectivity and explicit partial/missing-input status."""
    result = {'status': 'needs_information', 'message': '', 'source': '', 'fingerprint': '',
              'components': [], 'nets': [], 'warnings': [], 'missing': [], 'artifacts': []}
    try:
        snapshot = _input_snapshot(board, settings, cancel_event)
        result.update(source=str(snapshot['source']), fingerprint=snapshot['fingerprint'],
                      warnings=snapshot['warnings'], missing=snapshot['missing'])
        if not kicad_cli.get('available') or not kicad_cli.get('path'):
            result['message'] = 'KiCad CLI is unavailable; saved schematic connectivity could not be exported.'
            result['missing'].append('Install/detect the KiCad command-line tool for circuit connectivity.')
            return result
        workdir = Path(workdir).resolve()
        staged = workdir / 'schematic-input'
        staged.mkdir(parents=True, exist_ok=True)
        for path in snapshot['schematics']:
            target = staged / path.relative_to(snapshot['project'])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(snapshot['files'][path])
        project_path = snapshot['source'].with_suffix('.kicad_pro')
        if project_path in snapshot['files']:
            (staged / project_path.name).write_bytes(snapshot['files'][project_path])
        source = staged / snapshot['source'].name
        output = workdir / 'schematic-netlist.xml'
        log = workdir / 'schematic-export.log'
        output.unlink(missing_ok=True)
        command = [str(kicad_cli['path']), 'sch', 'export', 'netlist', '--format', 'kicadxml',
                   '--output', str(output), str(source)]
        seconds = max(1., min(60., float(settings.get('simulation', {}).get('max_seconds', 90))))
        result['artifacts'].append(str(log))
        code = _export(command, staged, log, seconds, cancel_event)
        if code != 0 or not output.is_file():
            raise _InputError(f'KiCad netlist export failed (exit {code}); see schematic-export.log.')
        if output.stat().st_size > MAX_XML_BYTES:
            raise _InputError('Exported netlist exceeds the 32 MiB limit.')
        result['artifacts'].append(str(output))
        with output.open('rb') as stream:
            xml_data = stream.read(MAX_XML_BYTES + 1)
        components, nets, warnings, missing, aliases = parse_netlist(xml_data, board, snapshot['root_uuid'])
        result.update(components=components, nets=nets, net_aliases=aliases)
        result['warnings'].extend(warnings)
        result['missing'].extend(missing)
        # Detect inputs edited during native export; never cache a mixed snapshot.
        if _input_snapshot(board, settings, cancel_event)['fingerprint'] != snapshot['fingerprint']:
            result['missing'].append('Schematic/model files changed during export; analyze again after saving.')
            for comp in result['components']:
                comp['board_match'] = False
        if not components:
            result['missing'].append('Saved schematic contains no physical circuit components.')
        result['status'] = 'complete' if not result['missing'] else 'needs_information'
        result['message'] = (f'Read {len(components)} saved schematic components and {len(nets)} nets. '
                             + ('Connectivity matches the PCB.' if not result['missing'] else
                                'Some circuit inputs are missing or differ from the PCB; unsupported checks are skipped.'))
        # A circuit is not a complete behavioral model. Preserve declarations,
        # never infer custom IC behavior from its drawing or pin names.
        model_missing = [c['reference'] for c in components
                         if c['reference'].startswith(('U', 'Q')) and not any(
                             key.casefold() in {'sim.name', 'sim.type', 'spice_model', 'spice.model'}
                             for key in c['properties'])]
        if model_missing:
            result['warnings'].append('Behavioral simulation models are not declared for: ' + ', '.join(model_missing)
                                      + '. Custom-symbol connections remain available; arbitrary IC behavior is not inferred.')
        result['warnings'] = list(dict.fromkeys(result['warnings']))
        result['missing'] = list(dict.fromkeys(result['missing']))
    except InterruptedError as exc:
        result.update(status='cancelled', message=str(exc))
    except (OSError, ValueError, ET.ParseError, TimeoutError) as exc:
        result['message'] = str(exc)
        result['missing'].append(str(exc))
        result['fingerprint'] = result['fingerprint'] or hashlib.sha256(str(exc).encode()).hexdigest()
    return result
