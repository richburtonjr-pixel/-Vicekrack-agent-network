import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from vicekrack.__main__ import main
from vicekrack.errors import NetworkError
from vicekrack.orchestrator import ROOT,read_json
from vicekrack.preview import preflight, preview_text, render_preview, invoke, dependencies
from vicekrack.scene_plan import build_scene_plan


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.plan=build_scene_plan(read_json(ROOT/'examples/short-script-cooking.json'))
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)/'previews'

    def test_preflight_preserves_plan_and_blocks_draft_by_default(self):
        before=deepcopy(self.plan)
        preflight(self.plan)
        self.assertEqual(before,self.plan)
        draft=build_scene_plan(read_json(ROOT/'examples/short-script-gta.json'),draft=True)
        with self.assertRaises(NetworkError) as error: preflight(draft)
        self.assertEqual(error.exception.code,'draft_preview_required')
        preflight(draft,True)
        self.assertTrue(draft['blocked_for_production'])

    def test_unsupported_methods_never_fall_back_silently(self):
        plan=build_scene_plan(self.plan['script'],{'available_methods':['generated_image','text_card','motion_graphics']})
        with patch('vicekrack.preview.dependencies',side_effect=AssertionError('No launch')), self.assertRaises(NetworkError) as error:
            render_preview(plan,directory=self.folder)
        self.assertEqual(error.exception.code,'unsupported_render_method')
        self.assertFalse(self.folder.exists())

    def test_tampered_plan_is_rejected(self):
        self.plan['scenes'][0]['beat']['end_seconds']=99
        with self.assertRaises(NetworkError): render_preview(self.plan,directory=self.folder)
        self.assertFalse(self.folder.exists())

    def test_language_glyphs_and_control_characters(self):
        script=deepcopy(self.plan['script']);script['language']='es'
        with self.assertRaises(NetworkError): preflight(build_scene_plan(script))
        for text in ('\u4f60\u597d','bad\x1bcontrol'):
            with self.assertRaises(NetworkError): preview_text(text)
        self.assertEqual(preview_text('smart\u2019quote\u2014dash'),'smart\'quote-dash')

    def test_missing_dependencies_no_output(self):
        with patch('vicekrack.preview.dependencies',side_effect=NetworkError('renderer_unavailable','Install extras')), self.assertRaises(NetworkError):
            render_preview(self.plan,directory=self.folder)
        self.assertFalse(self.folder.exists())

    def test_subprocess_timeout_and_error_are_sanitized(self):
        for exception,code in [(subprocess.TimeoutExpired('private',1),'render_timeout'),(subprocess.CalledProcessError(1,'private'),'render_failed'),(OSError('private'),'render_failed')]:
            with patch('vicekrack.preview.subprocess.run',side_effect=exception), self.assertRaises(NetworkError) as error:
                invoke('ffmpeg',['-version'],self.folder)
            self.assertEqual(error.exception.code,code)
            self.assertNotIn('private',str(error.exception))

    def test_subprocess_has_no_shell_or_provider_environment(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'synthetic','ANTHROPIC_API_KEY':'synthetic'}), patch('vicekrack.preview.subprocess.run') as run:
            invoke('ffmpeg',['-version'],self.folder)
        kwargs=run.call_args.kwargs
        self.assertFalse(kwargs['shell'])
        self.assertEqual(kwargs['timeout'],90)
        self.assertNotIn('OPENAI_API_KEY',kwargs['env'])
        self.assertNotIn('ANTHROPIC_API_KEY',kwargs['env'])

    def fake_card(self,plan,scene,path,modules):
        path.write_bytes(b'\x89PNG\r\n\x1a\nfixture png')

    def fake_invoke(self,exe,args,cwd):
        if args[-1]=='preview.mp4': (cwd/'preview.mp4').write_bytes(b'fixture-video'*100)
        elif args[-1].endswith('.mp4'): (cwd/args[-1]).write_bytes(b'segment')

    def test_complete_package_and_no_mutation_or_overwrite(self):
        before=deepcopy(self.plan)
        with patch('vicekrack.preview.dependencies',return_value=(None,None,None,'ffmpeg')), patch('vicekrack.preview.make_card',side_effect=self.fake_card), patch('vicekrack.preview.invoke',side_effect=self.fake_invoke) as encoder, patch('vicekrack.preview.uuid4',return_value=SimpleNamespace(hex='fixed')):
            result=render_preview(self.plan,directory=self.folder)
            self.assertEqual(encoder.call_count,6)
            with self.assertRaises(NetworkError): render_preview(self.plan,directory=self.folder)
        manifest=read_json(Path(result['manifest_file']))
        self.assertFalse(manifest['publishable'])
        self.assertFalse(manifest['audio_present'])
        self.assertEqual(manifest['duration_seconds'],15)
        self.assertEqual(self.plan,before)
        self.assertEqual(len(list(Path(result['preview_file']).parent.iterdir())),6)
        self.assertEqual(list(self.folder.glob('*.lock')),[])
        self.assertEqual(list(self.folder.glob('.render-*')),[])

    def test_failure_removes_staging_without_publishing(self):
        with patch('vicekrack.preview.dependencies',return_value=(None,None,None,'ffmpeg')), patch('vicekrack.preview.make_card',side_effect=self.fake_card), patch('vicekrack.preview.invoke',side_effect=NetworkError('render_timeout','controlled')), self.assertRaises(NetworkError):
            render_preview(self.plan,directory=self.folder)
        self.assertEqual(list(self.folder.iterdir()),[])

    def test_empty_encoder_output_is_rejected(self):
        with patch('vicekrack.preview.dependencies',return_value=(None,None,None,'ffmpeg')), patch('vicekrack.preview.make_card',side_effect=self.fake_card), patch('vicekrack.preview.invoke'), self.assertRaises(NetworkError) as error:
            render_preview(self.plan,directory=self.folder)
        self.assertEqual(error.exception.code,'invalid_render_output')
        self.assertEqual(list(self.folder.iterdir()),[])

    def test_cli_error_excludes_input_contents(self):
        path=Path(self.temp.name)/'bad.json';path.write_text('{private text')
        output=io.StringIO()
        with patch('sys.argv',['vicekrack','render-preview',str(path)]), redirect_stdout(output):
            self.assertEqual(main(),1)
        self.assertNotIn('private text',output.getvalue())

    @unittest.skipUnless(os.environ.get('RUN_LOCAL_RENDER_TESTS')=='1','Enable optional real FFmpeg integration with RUN_LOCAL_RENDER_TESTS=1')
    def test_real_mp4_dimensions_duration_frames_and_scene_images(self):
        import imageio_ffmpeg
        from PIL import Image
        result=render_preview(self.plan,directory=self.folder)
        frames,seconds=imageio_ffmpeg.count_frames_and_secs(result['preview_file'])
        self.assertEqual(frames,360)
        self.assertAlmostEqual(seconds,15,places=1)
        reader=imageio_ffmpeg.read_frames(result['preview_file'])
        try:
            metadata=next(reader)
            self.assertEqual(metadata['size'],(1080,1920))
            self.assertEqual(metadata['fps'],24)
        finally: reader.close()
        folder=Path(result['preview_file']).parent
        for number in range(1,5):
            with Image.open(folder/f'scene-{number}.png') as image:
                self.assertEqual(image.size,(1080,1920))
