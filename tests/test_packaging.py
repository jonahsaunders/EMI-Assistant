"""Regress the actual schema errors that blocked KiCad Install from File."""
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('build_package', ROOT / 'tools' / 'build_package.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.metadata = json.loads((ROOT / 'metadata.json').read_text())
        self.plugin = json.loads((ROOT / 'plugin.json').read_text())

    def test_current_metadata_passes_both_pcm_schemas_and_ipc_schema(self):
        builder.validate_metadata(self.metadata, self.plugin)

    def test_previous_release_is_rejected_for_both_missing_fields(self):
        old = deepcopy(self.metadata)
        del old['resources']
        del old['author']['contact']
        with self.assertRaises(ValueError) as context:
            builder.validate_metadata(old, self.plugin)
        self.assertIn("'resources' is a required property", str(context.exception))
        self.assertIn("'contact' is a required property", str(context.exception))

    def test_missing_contact_alone_is_rejected(self):
        del self.metadata['author']['contact']
        with self.assertRaisesRegex(ValueError, 'contact'):
            builder.validate_metadata(self.metadata, self.plugin)

    def test_missing_resources_alone_is_rejected(self):
        del self.metadata['resources']
        with self.assertRaisesRegex(ValueError, 'resources'):
            builder.validate_metadata(self.metadata, self.plugin)

    def test_malformed_ipc_entrypoint_is_rejected(self):
        del self.plugin['actions'][0]['entrypoint']
        with self.assertRaisesRegex(ValueError, 'entrypoint'):
            builder.validate_metadata(self.metadata, self.plugin)

    def test_archive_contains_valid_manifest_and_complete_first_launch_and_solver_payload(self):
        # Build only a disposable archive. Tests must not overwrite the release
        # artifact while another part of the project is being integrated.
        with tempfile.TemporaryDirectory() as output:
            target = builder.main(output_dir=output)
            version = self.metadata['versions'][0]['version']
            self.assertEqual(target.name, f'EMI-Assistant-{version}.zip')
            with zipfile.ZipFile(target) as archive:
                builder.validate_metadata(json.loads(archive.read('metadata.json')), json.loads(archive.read('plugins/plugin.json')))
                builder.validate_payload(archive)
                self.assertEqual(json.loads(archive.read('metadata.json'))['versions'][0]['version'], version)
                self.assertIn('plugins/metadata.json', archive.namelist())
                self.assertIn('plugins/validation/schemas/pcm.v2.schema.json', archive.namelist())
                self.assertIn('plugins/docs/SIMULATION.md', archive.namelist())
                guide = ROOT / 'docs' / 'USER-GUIDE.md'
                if guide.is_file():
                    self.assertEqual(archive.read('plugins/README.md'), guide.read_bytes())
                self.assertEqual(archive.read('plugins/runtime-requirements.txt'), (ROOT / 'runtime-requirements.txt').read_bytes())
                content = {name: archive.read(name) for name in archive.namelist()}
            for missing in ('plugins/runtime-requirements.txt', 'plugins/emi_assistant/bootstrap.py',
                            'plugins/emi_assistant/sim_ngspice_worker.py', 'plugins/emi_assistant/sim_openems.py',
                            'plugins/THIRD_PARTY_NOTICES.md', 'plugins/LICENSES/GPL-3.0.txt'):
                with self.subTest(missing=missing):
                    payload = io.BytesIO()
                    with zipfile.ZipFile(payload, 'w') as modified:
                        for name, value in content.items():
                            if name != missing:
                                modified.writestr(name, value)
                    payload.seek(0)
                    with zipfile.ZipFile(payload) as modified:
                        with self.assertRaisesRegex(ValueError, 'Missing installed plugin files'):
                            builder.validate_payload(modified)
            for changed, value, expected in (
                ('plugins/requirements.txt', b'shapely>=2.0,<3\n', 'comment-only'),
                ('plugins/runtime-requirements.txt', b'kicad-python==0.8.0\n', 'Missing bounded runtime dependency'),
            ):
                with self.subTest(changed=changed):
                    payload = io.BytesIO()
                    with zipfile.ZipFile(payload, 'w') as modified:
                        for name, original in content.items():
                            modified.writestr(name, value if name == changed else original)
                    payload.seek(0)
                    with zipfile.ZipFile(payload) as modified:
                        with self.assertRaisesRegex(ValueError, expected):
                            builder.validate_payload(modified)

if __name__ == '__main__':
    unittest.main()
