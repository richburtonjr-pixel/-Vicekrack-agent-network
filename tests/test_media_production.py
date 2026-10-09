from pathlib import Path
from unittest.mock import patch
from test_artifact_binding import Step36Base, BoundRenderer
from vicekrack.errors import NetworkError
from vicekrack.media_production import render_production


class MediaProductionTests(Step36Base):
    def revise(self, manifest=None, renderer=None):
        # Media decode is covered separately; this exercises real production persistence/binding.
        def local_renderer(plan, **kwargs):
            kwargs.pop('media'); kwargs.pop('media_root')
            kwargs['directory'] = Path(kwargs['directory']) / 'revision'
            return BoundRenderer()(plan, **kwargs)
        with patch('vicekrack.media_render.prepare_media'):
            return render_production(self.pid, manifest or {'synthetic': True}, self.root, root=self.root,
                                     clock=lambda: self.clock, renderer=renderer or local_renderer)

    def test_revision_invalidates_old_report_and_new_report_binds_media(self):
        self.make()
        old_report, _ = self.quality()
        old_path = self.path('preview', 'preview_file')
        result = self.revise()
        self.assertTrue(result['quality_review_required'])
        self.assertTrue(old_path.is_file())
        self.assertEqual(self.binding(old_report)['status'], 'changed')
        new_report, _ = self.quality()
        self.assertEqual(self.binding(new_report)['status'], 'matching')
        self.path('preview', 'media_path').write_text('changed')
        self.assertEqual(self.binding(new_report)['status'], 'changed')

    def test_duplicate_revision_refused(self):
        self.make()
        self.revise()
        with self.assertRaises(NetworkError) as error:
            self.revise()
        self.assertEqual(error.exception.code, 'media_already_attached')

    def test_failure_preserves_previous_production(self):
        self.make()
        before = self.state()
        def failing(*args, **kwargs):
            raise NetworkError('render_failed', 'Synthetic failure')
        with self.assertRaises(NetworkError): self.revise(renderer=failing)
        self.assertEqual(self.state(), before)
