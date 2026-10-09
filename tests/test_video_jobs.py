import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from vicekrack import video_jobs as jobs
from vicekrack.errors import NetworkError
from vicekrack.events.store import _Lock
from vicekrack.orchestrator import ROOT, read_json
from vicekrack.scene_plan import build_scene_plan
from vicekrack.video_transport import check_download_url, transport, NoRedirect


class VideoJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = patch.object(jobs, 'ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.plan = build_scene_plan(read_json(ROOT / 'examples/short-script-cooking.json'))
        self.job = jobs.prepare(self.plan, 1)
        self.id = self.job['job_id']

    def submit(self, transport, **kwargs):
        with patch.dict(os.environ, {'XAI_API_KEY': 'synthetic-test-value'}):
            return jobs.submit(self.id, consent='paid-generate:' + self.id, allow_network=True,
                               transport=transport, **kwargs)

    def test_prepare_does_not_reset_submitted_job(self):
        self.submit(Mock(return_value={'request_id': 'request-123'}))
        again = jobs.prepare(self.plan, 1)
        self.assertEqual(again['status'], 'submitted')
        no_call = Mock()
        with self.assertRaises(NetworkError): self.submit(no_call)
        no_call.assert_not_called()

    def test_consent_and_missing_key(self):
        sender = Mock()
        with self.assertRaises(NetworkError):
            jobs.submit(self.id, consent='wrong', allow_network=True, transport=sender)
        with patch.dict(os.environ, {'XAI_API_KEY': ''}), self.assertRaises(NetworkError):
            jobs.submit(self.id, consent='paid-generate:' + self.id, allow_network=True, transport=sender)
        sender.assert_not_called()

    def test_unknown_outcome_requires_explicit_retry(self):
        for response in (TimeoutError('private'), ValueError('private')):
            with self.assertRaises(NetworkError) as error:
                self.submit(Mock(side_effect=response), retry_uncertain=True)
            self.assertNotIn('private', str(error.exception))
            self.assertEqual(jobs.inspect(self.id)['status'], 'uncertain')
        with self.assertRaises(NetworkError): self.submit(Mock())
        result = self.submit(Mock(return_value={'request_id': 'retry-123'}), retry_uncertain=True)
        self.assertEqual(len(result['attempts']), 3)

    def test_malformed_acceptance_is_uncertain(self):
        with self.assertRaises(NetworkError): self.submit(Mock(return_value={}))
        self.assertTrue(jobs.inspect(self.id)['uncertain'])

    def test_interrupted_submission_is_uncertain(self):
        self.job['status'] = 'submitting'
        jobs._save(self.job)
        self.assertEqual(jobs.inspect(self.id)['status'], 'uncertain')
        with self.assertRaises(NetworkError): self.submit(Mock())

    def test_atomic_write_failure_prevents_paid_request(self):
        sender = Mock()
        with patch.object(jobs.os, 'replace', side_effect=OSError), self.assertRaises(NetworkError):
            self.submit(sender)
        sender.assert_not_called()
        self.assertEqual(jobs.inspect(self.id)['status'], 'prepared')

    def test_status_failure_preserves_id(self):
        self.submit(Mock(return_value={'request_id': 'request-123'}))
        for sender in [Mock(side_effect=TimeoutError), Mock(return_value={'status': 'unknown'})]:
            with self.assertRaises(NetworkError): jobs.status(self.id, allow_network=True, transport=sender)
        self.assertEqual(jobs.inspect(self.id)['request_id'], 'request-123')
        result = jobs.status(self.id, allow_network=True, transport=Mock(return_value={'status': 'pending'}))
        self.assertEqual(result['status'], 'pending')

    def test_download_credentials_and_probe(self):
        self.submit(Mock(return_value={'request_id': 'request-123'}))
        jobs.status(self.id, allow_network=True, transport=Mock(return_value={
            'status': 'done', 'video': {'url': 'https://vidgen.x.ai/a.mp4'}}))
        sender = Mock(return_value=b'0000ftyp' + b'0' * 64)
        with patch('vicekrack.media_render.inspect_video', return_value={'width': 720, 'height': 1280, 'duration': 3}):
            result = jobs.download(self.id, allow_network=True, transport=sender, media_root=self.root / 'media')
        self.assertFalse(sender.call_args.kwargs['credential'])
        self.assertEqual(result['status'], 'downloaded')

    def test_corruption_paths_concurrency_and_credentials(self):
        lock = _Lock(jobs.jobs_dir() / 'operation.lock')
        self.assertTrue(lock.acquire(create=True))
        try:
            with self.assertRaises(NetworkError): jobs.inspect(self.id)
        finally: lock.release()
        for value in ('../bad', 'vid-..\\bad', 'vid-/bad'):
            with self.assertRaises(NetworkError): jobs.inspect(value)
        jobs._path(self.id).write_text('{}')
        with self.assertRaises(NetworkError): jobs.inspect(self.id)
        with patch.dict(os.environ, {'XAI_API_KEY': self.plan['script']['title']}), self.assertRaises(NetworkError):
            jobs.prepare(self.plan, 2)

    def test_network_targets_and_redirects(self):
        for url in ('http://vidgen.x.ai/a', 'https://vidgen.x.ai.evil/a', 'https://user@vidgen.x.ai/a',
                    'https://vidgen.x.ai:8443/a', 'https://127.0.0.1/a'):
            with self.assertRaises(NetworkError): check_download_url(url)
        with self.assertRaises(NetworkError): transport('DOWNLOAD', 'https://vidgen.x.ai/a', None, credential=True)
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil/a'))

    def test_transport_never_sends_auth_to_media_and_bounds_response(self):
        from vicekrack import video_transport as http
        response = Mock()
        response.status = 200
        response.headers = {'Content-Length': '4'}
        response.read.side_effect = [b'data', b'']
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(http, 'build_opener', return_value=opener), patch.dict(os.environ, {'XAI_API_KEY': 'synthetic-key'}):
            self.assertEqual(http.transport('DOWNLOAD', 'https://vidgen.x.ai/clip', None, credential=False), b'data')
        request = opener.open.call_args.args[0]
        self.assertFalse(request.has_header('Authorization'))
        response.headers = {'Content-Length': str(http.MAX_DOWNLOAD + 1)}
        with patch.object(http, 'build_opener', return_value=opener), self.assertRaises(NetworkError):
            http.transport('DOWNLOAD', 'https://vidgen.x.ai/clip', None, credential=False)

    def test_four_downloaded_jobs_produce_renderer_manifest(self):
        data = b'0000ftyp' + b'0' * 64
        ids = []
        from vicekrack.artifact_binding import sha256_bytes
        for index in range(1, 5):
            record = jobs.prepare(self.plan, index)
            record.update(status='downloaded', request_id='request-' + str(index), download_url='https://vidgen.x.ai/video')
            name = record['job_id'] + '.mp4'
            (self.root / name).write_bytes(data)
            record['media'] = dict(relative_path=name, sha256=sha256_bytes(data), bytes=len(data))
            jobs._save(record)
            ids.append(record['job_id'])
        manifest = jobs.media_manifest(self.plan, ids, self.root)
        self.assertEqual(len(manifest['assignments']), 4)
        self.assertTrue(manifest['mute_source_audio'])
        with self.assertRaises(NetworkError): jobs.media_manifest(self.plan, [ids[0]] * 4, self.root)
