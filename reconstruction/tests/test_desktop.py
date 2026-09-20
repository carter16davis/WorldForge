import importlib.util
from pathlib import Path
import tempfile
import sys
import json
import struct
sys.path.insert(0, str(Path(__file__).parents[1]))
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('desktop', Path(__file__).parents[1] / 'desktop.py')
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)


class DesktopTests(unittest.TestCase):
    def test_stage_boundaries(self):
        with patch.object(desktop, 'desktop_path', side_effect=str):
            prepare = desktop.prepare_commands(Path('photos with spaces'), Path('aligned.rsproj'))
            self.assertNotIn('-quit', prepare)
            self.assertNotIn('-calculateNormalModel', prepare)
            build = desktop.build_commands(Path('adjusted.rsproj'), Path('output'), Path('preset.xml'), 1000)
            self.assertEqual(build[:2], ['-load', 'adjusted.rsproj'])
            for forbidden in ('-align', '-selectMaximalComponent', '-setReconstructionRegionAuto', '-setReconstructionRegion'):
                self.assertNotIn(forbidden, build)
            self.assertLess(build.index('-simplify'), build.index('-unwrap'))
            self.assertLess(build.index('-unwrap'), build.index('-calculateTexture'))
            self.assertNotIn('-simplify', desktop.build_commands(Path('p'), Path('o'), Path('s')))

    def test_mesh_detail(self):
        with patch.object(desktop, 'desktop_path', side_effect=str):
            # High detail meshes from the photographs at full resolution. It is
            # the default in both the CLI and the web app: anything less throws
            # away the facade detail the capture exists to record.
            self.assertIn('-calculateHighModel', desktop.build_commands(Path('p'), Path('o'), Path('s')))
            self.assertIn('-calculateNormalModel',
                          desktop.build_commands(Path('p'), Path('o'), Path('s'), None, 'normal'))
            self.assertIn('-calculatePreviewModel',
                          desktop.build_commands(Path('p'), Path('o'), Path('s'), None, 'PREVIEW'))
        self.assertEqual(desktop.mesh_command('high'), ('-calculateHighModel', 'high'))
        with self.assertRaises(ValueError):
            desktop.mesh_command('ultra')

    def test_glb_embedded_and_external_textures(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def write(uri):
                doc = {'meshes': [{}], 'materials': [{}], 'textures': [{'source': 0}],
                       'images': [{'uri': uri}]}
                data = json.dumps(doc).encode()
                data += b' ' * (-len(data) % 4)
                (root / 'building.glb').write_bytes(struct.pack('<4sII', b'glTF', 2, 20 + len(data)) +
                    struct.pack('<II', len(data), 0x4E4F534A) + data)
            write('texture.png')
            with self.assertRaisesRegex(ValueError, 'external resources'):
                desktop.check_export(root)
            write('data:image/png;base64,fixture')
            self.assertEqual(desktop.check_export(root), [root / 'building.glb'])

    def test_continue_is_required(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / 'aligned.rsproj'
            project.write_text('<RealityScan><reconstructions selectedComponentId="a"><component id="a"/></reconstructions></RealityScan>')
            preset = root / 'preset.xml'
            preset.write_text('<ModelExport formatAndVersionUID="glb 000 " embedTextures="1"/>')  # Structural fixture only, never sent to RealityScan.
            with patch.object(desktop, 'executable_path', return_value=Path('mock.exe')), \
                 patch('builtins.input', return_value='no'), patch.object(desktop, 'execute') as execute:
                with self.assertRaisesRegex(ValueError, 'Cancelled'):
                    desktop.build(project, preset)
                execute.assert_not_called()

    def test_launch_failure_recorded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'run'
            with patch.object(desktop, 'desktop_path', side_effect=str), \
                 patch.object(desktop.subprocess, 'run', side_effect=OSError('launch failed')):
                with self.assertRaises(OSError):
                    desktop.execute('mock.exe', [], root, False, lambda: None, {})
            import json
            self.assertEqual(json.loads((root / 'run.json').read_text())['status'], 'failed')
