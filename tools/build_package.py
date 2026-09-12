"""Build a reproducible KiCad install-from-file ZIP, including all source."""
from pathlib import Path
import ast
import hashlib
import json
import shutil
import struct
import sys
import zipfile
import zlib

from jsonschema import Draft7Validator

ROOT = Path(__file__).resolve().parent.parent

def validate_metadata(metadata, plugin):
    """Use actual upstream schemas, including required but undocumented fields."""
    for name, payload in [('pcm.v1.schema.json', metadata), ('pcm.v2.schema.json', metadata), ('api.v1.schema.json', plugin)]:
        schema = json.loads((ROOT / 'validation' / 'schemas' / name).read_text())
        Draft7Validator.check_schema(schema)
        errors = sorted(Draft7Validator(schema).iter_errors(payload), key=lambda error: str(error.path))
        if errors:
            messages = [f"{'/'.join(map(str,e.path)) or '<root>'}: {e.message}" for e in errors]
            raise ValueError(f"KiCad {name} validation failed: " + '; '.join(messages))

def icon_png(size):
    # A small code-native PCB trace icon; no runtime graphics dependency.
    rgba = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            u, v = x / size, y / size
            color = (19, 121, 111, 255) if .06 < u < .94 and .06 < v < .94 else (0, 0, 0, 0)
            track = (.20 < u < .80 and abs(v - .5) < .035) or (.47 < u < .53 and .23 < v < .77)
            pad = any((u - px)**2 + (v - py)**2 < .065**2 for px, py in [(.22,.5),(.78,.5),(.5,.25),(.5,.75)])
            if track or pad:
                color = (236, 246, 234, 255)
            rgba[(y*size+x)*4:(y*size+x+1)*4] = bytes(color)
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)
    raw = b''.join(b'\x00' + rgba[y*size*4:(y+1)*size*4] for y in range(size))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', size, size, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')

def validate_payload(archive):
    """Check the first-launch and simulation files needed by the installed ZIP."""
    required = [
        'metadata.json', 'plugins/plugin.json', 'plugins/launch.py',
        'plugins/requirements.txt', 'plugins/runtime-requirements.txt',
        'plugins/LICENSE', 'plugins/THIRD_PARTY_NOTICES.md',
        'plugins/LICENSES/GPL-3.0.txt', 'plugins/LICENSES/LGPL-3.0.txt',
        'plugins/LICENSES/LGPL-2.1.txt', 'plugins/LICENSES/KiCad-LICENSE-README.txt',
        'plugins/emi_assistant/licensing.py',
        'plugins/emi_assistant/data/licenses/THIRD_PARTY_NOTICES.md',
        'plugins/emi_assistant/bootstrap.py',
        'plugins/emi_assistant/simulation.py',
        'plugins/emi_assistant/simulation_runtime.py',
        'plugins/emi_assistant/sim_schematic.py',
        'plugins/emi_assistant/sim_ngspice.py',
        'plugins/emi_assistant/sim_ngspice_worker.py',
        'plugins/emi_assistant/sim_openems.py',
        'plugins/emi_assistant/ui_simulation.py',
        'plugins/emi_assistant/data/examples/demo.kicad_pcb',
    ]
    missing = sorted(set(required) - set(archive.namelist()))
    if missing:
        raise ValueError('Missing installed plugin files: ' + ', '.join(missing))
    registration = archive.read('plugins/requirements.txt').decode('utf-8')
    if any(line.strip() and not line.lstrip().startswith('#') for line in registration.splitlines()):
        raise ValueError('Registration requirements must be comment-only; install dependencies on first launch.')
    runtime = archive.read('plugins/runtime-requirements.txt').decode('utf-8')
    dependencies = [line.strip().casefold() for line in runtime.splitlines()
                    if line.strip() and not line.lstrip().startswith('#')]
    for package in ('kicad-python', 'pyside6-essentials', 'shapely', 'numpy', 'py7zr'):
        if not any(line.startswith(package + operator) for line in dependencies for operator in ('==', '>=', '~=')):
            raise ValueError(f'Missing bounded runtime dependency: {package}')

def main(output_dir=None):
    (ROOT / 'resources').mkdir(exist_ok=True)
    (ROOT / 'resources' / 'icon.png').write_bytes(icon_png(24))
    destination = ROOT / 'emi_assistant' / 'data' / 'examples'
    destination.mkdir(parents=True, exist_ok=True)
    for example in (ROOT / 'examples').iterdir():
        if example.suffix in ('.json', '.kicad_pcb'):
            shutil.copyfile(example, destination / example.name)
    license_data = ROOT / 'emi_assistant' / 'data' / 'licenses'
    license_data.mkdir(parents=True, exist_ok=True)
    for source in [ROOT / 'LICENSE', ROOT / 'THIRD_PARTY_NOTICES.md',
                   *sorted((ROOT / 'LICENSES').glob('*.txt'))]:
        shutil.copyfile(source, license_data / source.name)
    for path in ROOT.rglob('*.py'):
        if '__pycache__' not in path.parts:
            ast.parse(path.read_text(encoding='utf-8'), filename=str(path), feature_version=(3, 10))
    files = []
    directories = {'emi_assistant', 'tests', 'examples', 'docs', 'tools', 'resources', 'validation', 'LICENSES'}
    top_files = {'README.md', 'CONTRIBUTING.md', 'CHANGELOG.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'requirements.txt', 'runtime-requirements.txt', 'plugin.json', 'metadata.json', 'pyproject.toml', 'CONTRACT.md', 'SIMULATION_CONTRACT.md', 'launch.py'}
    for path in sorted(ROOT.rglob('*')):
        relative = path.relative_to(ROOT)
        if (len(relative.parts) > 1 and relative.parts[0] not in directories) or (len(relative.parts) == 1 and path.name not in top_files):
            continue
        if not path.is_file() or any(p.startswith('.') or p in ('__pycache__', 'dist', 'build') or p.endswith('.egg-info') for p in relative.parts):
            continue
        if path.suffix in ('.pyc', '.zip'):
            continue
        # The repository front page links to its hosted download. An installed
        # plugin instead needs the self-contained user guide at its root.
        guide = ROOT / 'docs' / 'USER-GUIDE.md'
        source = guide if relative.as_posix() == 'README.md' and guide.is_file() else path
        files.append((source, 'plugins/' + relative.as_posix()))
    metadata = json.loads((ROOT / 'metadata.json').read_text())
    metadata['versions'][0]['install_size'] = sum(p.stat().st_size for p, _ in files)
    plugin = json.loads((ROOT / 'plugin.json').read_text())
    validate_metadata(metadata, plugin)
    out = (Path(output_dir) if output_dir is not None else ROOT / 'dist') / f"EMI-Assistant-{metadata['versions'][0]['version']}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        def add(name, content):
            info = zipfile.ZipInfo(name, (2026, 9, 12, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
        add('metadata.json', json.dumps(metadata, indent=2).encode())
        add('resources/icon.png', icon_png(64))
        for path, name in files:
            add(name, path.read_bytes())
    with zipfile.ZipFile(out) as archive:
        assert archive.testzip() is None
        validate_metadata(json.loads(archive.read('metadata.json')), json.loads(archive.read('plugins/plugin.json')))
        validate_payload(archive)
    print(f'{out}\n{out.stat().st_size} bytes\nSHA256 {hashlib.sha256(out.read_bytes()).hexdigest()}')
    return out

if __name__ == '__main__':
    main()
