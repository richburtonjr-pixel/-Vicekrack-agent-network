"""Explicit local preview revision; existing approvals become stale by state hash."""
import json
import os
from pathlib import Path

from .artifact_binding import sha256_bytes, check_package
from .errors import NetworkError
from .orchestrator import read_json
from .preview import render_preview
from .production import Pipeline, _check_saved_config


def render_production(production_id, media, media_root, *, root=None, renderer=render_preview, clock=None):
    pipeline = Pipeline(root=root, clock=clock)
    store = pipeline.store
    with store.lock(production_id):
        state = store.read(production_id)
        if state["status"] != "completed":
            raise NetworkError("media_production_incomplete", "Complete the original production before revising its preview.")
        loaded = _check_saved_config(state)
        pipeline._verify_artifacts(state, loaded)
        pipeline._check_evidence(state, loaded)
        folder = store.folder(production_id).resolve()
        plan = pipeline._load_json(state, "plan", "plan_path")
        from .media_render import prepare_media
        prepare_media(plan, media, media_root)
        encoded = json.dumps(media, sort_keys=True, allow_nan=False).encode()
        digest = sha256_bytes(encoded)
        if state['stages'][-1]['artifacts'].get('media_sha256') == digest:
            raise NetworkError("media_already_attached", "This media manifest is already the current preview.")
        if len(state['trace']) >= 100:
            raise NetworkError("production_step_limit", "Production trace limit reached.")
        narration = state['config']['narration']
        result = renderer(plan, allow_draft=state['config']['allow_draft_preview'],
                          directory=folder / 'previews', narration=Path(narration['path']) if narration else None,
                          media=media, media_root=media_root)
        video, manifest = Path(result['preview_file']).resolve(), Path(result['manifest_file']).resolve()
        if not video.is_relative_to(folder) or not manifest.is_relative_to(folder):
            raise NetworkError('invalid_render_output', 'Preview must stay inside its production.')
        document = read_json(manifest)
        if document.get('version') != '1.1' or check_package(manifest.parent, document) is not None:
            raise NetworkError('invalid_render_output', 'Preview package failed verification.')
        media_path = manifest.parent / 'media-manifest.json'
        with media_path.open('xb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        artifacts = dict(preview_file=str(video.relative_to(folder)), video_sha256=sha256_bytes(video.read_bytes()),
                         manifest_file=str(manifest.relative_to(folder)), manifest_sha256=sha256_bytes(manifest.read_bytes()),
                         blocked_for_production=plan['blocked_for_production'], audio_present=document['audio_present'],
                         media_path=str(media_path.relative_to(folder)), media_sha256=digest)
        state['stages'][-1]['artifacts'] = artifacts
        state['stages'][-1]['finished_at'] = pipeline.clock()
        state['updated_at'] = pipeline.clock()
        state['trace'].append(dict(stage='preview', event='completed', at=pipeline.clock(), error_code=None))
        state['result'] = dict(preview_file=str(video), manifest_file=str(manifest), publishable=False,
                               preview_only=True, blocked_for_production=plan['blocked_for_production'],
                               audio_present=document['audio_present'])
        # Only this atomic pointer update activates the revision. Old packages/reports stay intact.
        store.write(state)
        return {'production_id': production_id, 'result': state['result'], 'quality_review_required': True}
