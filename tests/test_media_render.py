import io
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from vicekrack.artifact_binding import sha256_bytes, check_package
from vicekrack.errors import NetworkError
from vicekrack.media_render import prepare_media
from vicekrack.orchestrator import ROOT, read_json
from vicekrack.preview import render_preview
from vicekrack.scene_plan import build_scene_plan


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = build_scene_plan(read_json(ROOT / 'examples/short-script-cooking.json'))
        self.data = b'\x89PNG\r\n\x1a\n' + b'fixture' * 12
        self.write_fixture(self.data)

    def write_fixture(self, data):
        (self.root / 'image.png').write_bytes(data)
        self.manifest = {'contract': 'media_manifest', 'version': '1.0', 'manifest_id': 'media-test',
            'plan_id': self.plan['plan_id'], 'mute_source_audio': True,
            'assets': [{'asset_id': 'image-one', 'kind': 'image', 'relative_path': 'image.png',
                        'sha256': sha256_bytes(data), 'bytes': len(data),
                        'provenance': {'label': 'synthetic'},
                        'rights': {'status': 'declared_not_verified', 'declaration': 'Synthetic test fixture', 'declared_by': 'tests'}}],
            'assignments': [{'scene_index': s['index'], 'asset_id': 'image-one', 'fit': 'cover', 'anchor': 'center',
                             'image_duration_seconds': s['beat']['end_seconds'] - s['beat']['start_seconds']}
                            for s in self.plan['scenes']]}

    def test_hash_plan_and_duration_checked_before_render(self):
        prepare_media(self.plan, self.manifest, self.root)
        for change in ('hash', 'plan', 'duration', 'path'):
            manifest = deepcopy(self.manifest)
            if change == 'hash': manifest['assets'][0]['sha256'] = '0' * 64
            if change == 'plan': manifest['plan_id'] = '0' * 64
            if change == 'duration': manifest['assignments'][0]['image_duration_seconds'] = 9
            if change == 'path': manifest['assets'][0]['relative_path'] = '../outside.png'
            with self.assertRaises(NetworkError): prepare_media(self.plan, manifest, self.root)

    @unittest.skipUnless(os.getenv('RUN_LOCAL_RENDER_TESTS') == '1', 'set RUN_LOCAL_RENDER_TESTS=1')
    def test_real_image_render_decodes_and_preserves_package_binding(self):
        from PIL import Image
        from vicekrack.quality import probe_media
        buffer = io.BytesIO()
        Image.new('RGB', (640, 480), '#267f58').save(buffer, format='PNG')
        self.write_fixture(buffer.getvalue())
        result = render_preview(self.plan, media=self.manifest, media_root=self.root, directory=self.root / 'out')
        manifest = read_json(Path(result['manifest_file']))
        self.assertIsNone(check_package(Path(result['manifest_file']).parent, manifest))
        info = probe_media(Path(result['preview_file']))
        self.assertEqual((info['width'], info['height'], info['frames']), (1080, 1920, 360))
        self.assertFalse(info['audio_present'])
        self.assertFalse(result['publishable'])
