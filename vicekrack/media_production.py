"""Explicit local preview revision; existing approvals become stale by state hash."""
import json
import os
from pathlib import Path
from uuid import uuid4

from .artifact_binding import sha256_bytes, check_package
from .errors import NetworkError
from .narration import normalize_narration
from .orchestrator import read_json
from .preview import render_preview
from .production import Pipeline, _check_saved_config


def _narration_override(narration):
    """Re-check a supplied narration before anything is locked, staged or rendered."""
    if narration is None:
        return None
    data = narration.get("data") if isinstance(narration, dict) else None
    if not isinstance(data, (bytes, bytearray)) or sha256_bytes(bytes(data)) != narration.get("sha256"):
        raise NetworkError("narration_changed", "The narration bytes do not match their recorded hash.")
    metadata, _ = normalize_narration(bytes(data))
    return {"data": bytes(data), "sha256": narration["sha256"], "normalized_sha256": metadata["normalized_sha256"]}


def render_production(production_id, media, media_root, *, root=None, renderer=render_preview, clock=None,
                      narration=None):
    """Revise a completed production's preview with local media.

    `narration` (Step 45, optional) is {"data": WAV bytes, "sha256": their hash} from a workflow's
    managed copy. It replaces the production's own Step 14 narration for this revision only. The
    exact bytes are re-hashed and validated here, staged for the renderer, then kept beside the
    rendered package as `narration.wav` so the quality report can bind and re-check them. When
    omitted, the production's Step 14 narration (if any) is used exactly as before."""
    override = _narration_override(narration)
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
        current = state['stages'][-1]['artifacts']
        # A revision is identified by its media AND its narration: the same clips with different
        # (or no) narration are a different video and must be rendered and checked again.
        if current.get('media_sha256') == digest and current.get('narration_sha256') == (override or {}).get('sha256'):
            raise NetworkError("media_already_attached", "This media manifest is already the current preview.")
        if len(state['trace']) >= 100:
            raise NetworkError("production_step_limit", "Production trace limit reached.")
        previews = folder / 'previews'
        previews.mkdir(parents=True, exist_ok=True)
        staged = None
        try:
            if override is not None:
                staged = previews / f".narration-{uuid4().hex}.wav"    # the renderer reads these exact, verified bytes
                with staged.open('xb') as stream:
                    stream.write(override['data'])
                narration_path = staged
            else:
                narration = state['config']['narration']
                narration_path = Path(narration['path']) if narration else None
            result = renderer(plan, allow_draft=state['config']['allow_draft_preview'], directory=previews,
                              narration=narration_path, media=media, media_root=media_root)
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
        video, manifest = Path(result['preview_file']).resolve(), Path(result['manifest_file']).resolve()
        if not video.is_relative_to(folder) or not manifest.is_relative_to(folder):
            raise NetworkError('invalid_render_output', 'Preview must stay inside its production.')
        document = read_json(manifest)
        if document.get('version') != '1.1' or check_package(manifest.parent, document) is not None:
            raise NetworkError('invalid_render_output', 'Preview package failed verification.')
        if override is not None and (document.get('audio_present') is not True
                                     or (document.get('audio') or {}).get('normalized_sha256') != override['normalized_sha256']):
            raise NetworkError('invalid_render_output', 'The rendered preview does not carry the supplied narration.')
        media_path = manifest.parent / 'media-manifest.json'
        with media_path.open('xb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        artifacts = dict(preview_file=str(video.relative_to(folder)), video_sha256=sha256_bytes(video.read_bytes()),
                         manifest_file=str(manifest.relative_to(folder)), manifest_sha256=sha256_bytes(manifest.read_bytes()),
                         blocked_for_production=plan['blocked_for_production'], audio_present=document['audio_present'],
                         media_path=str(media_path.relative_to(folder)), media_sha256=digest)
        if override is not None:
            kept = manifest.parent / 'narration.wav'
            with kept.open('xb') as stream:
                stream.write(override['data'])
                stream.flush()
                os.fsync(stream.fileno())
            artifacts.update(narration_path=str(kept.relative_to(folder)), narration_sha256=override['sha256'])
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
