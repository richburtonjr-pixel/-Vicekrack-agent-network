"""Step 14 optional local narration. Mocked by default; real FFmpeg only with RUN_LOCAL_RENDER_TESTS=1."""
import hashlib
import io
import json
import math
import os
import struct
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vicekrack.__main__ import main
from vicekrack.errors import NetworkError
from vicekrack.narration import MAX_BYTES, load_narration
from vicekrack.orchestrator import ROOT, read_json
from vicekrack.preview import render_preview
from vicekrack.scene_plan import build_scene_plan

MARKER = b"PRIVATE-NARRATION-MARKER"


def chunk(name, body):
    return name + struct.pack("<I", len(body)) + body + (b"\0" if len(body) % 2 else b"")


def wav_bytes(seconds=2.0, rate=16000, channels=1, bits=16, tag=1, extra=(), pcm=None, fmt=None, data_size=None):
    block = channels * bits // 8
    if pcm is None:
        frames = int(round(seconds * rate))
        sample = struct.pack("<h", 1000) * channels if bits == 16 else b"\x10" * block
        pcm = sample * frames
    if fmt is None:
        fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * block, block, bits)
    data = b"data" + struct.pack("<I", len(pcm) if data_size is None else data_size) + pcm
    body = b"WAVE" + b"".join(extra[:1]) + chunk(b"fmt ", fmt) + b"".join(extra[1:]) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def metadata_chunks():
    info = b"INFO" + chunk(b"INAM", MARKER + b"\0") + chunk(b"IART", MARKER + b"-artist\0")
    return (chunk(b"LIST", info), chunk(b"bext", MARKER * 3 + b"!"), chunk(b"junk", b"odd"))


class NarrationValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def write(self, data, name="private-recording.wav"):
        path = self.folder / name
        path.write_bytes(data)
        return path

    def assert_code(self, data_or_path, code):
        path = data_or_path if isinstance(data_or_path, Path) else self.write(data_or_path)
        with self.assertRaises(NetworkError) as raised:
            load_narration(path)
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn("private", raised.exception.message.lower())
        self.assertNotIn(str(self.folder), raised.exception.message)
        return raised.exception

    def test_valid_mono_is_padded_to_fifteen_seconds(self):
        meta, wav = load_narration(self.write(wav_bytes(2.5, 16000, 1)))
        self.assertEqual(meta, {"source_duration_seconds": 2.5, "channels": 1, "sample_rate": 16000,
                                "padded_duration_seconds": 15,
                                "normalized_sha256": hashlib.sha256(wav).hexdigest()})
        self.assertEqual(len(wav), 44 + 15 * 16000 * 2)
        self.assertEqual(wav[44:44 + 4], struct.pack("<h", 1000) * 2)
        self.assertEqual(set(wav[44 + 40000 * 2:]), {0})

    def test_supported_bounds_stereo_and_extensible(self):
        for rate, channels in ((8000, 1), (48000, 2), (44100, 2)):
            with self.subTest(rate=rate, channels=channels):
                meta, wav = load_narration(self.write(wav_bytes(15, rate, channels)))
                self.assertEqual((meta["sample_rate"], meta["channels"]), (rate, channels))
                self.assertEqual(meta["source_duration_seconds"], 15)
                self.assertEqual(len(wav), 44 + 15 * rate * channels * 2)
        guid = bytes.fromhex("0100000000001000800000aa00389b71")
        extensible = struct.pack("<HHIIHHHHI", 0xFFFE, 2, 22050, 22050 * 4, 4, 16, 22, 16, 3) + guid
        meta, wav = load_narration(self.write(wav_bytes(1, 22050, 2, fmt=extensible)))
        self.assertEqual(struct.unpack_from("<H", wav, 20)[0], 1)  # Rewritten as plain PCM.

    def test_overlong_audio_is_rejected_never_truncated(self):
        pcm = struct.pack("<h", 5) * (15 * 16000 + 1)
        self.assert_code(wav_bytes(pcm=pcm), "narration_too_long")
        self.assert_code(wav_bytes(20, 8000, 1), "narration_too_long")

    def test_missing_unreadable_and_empty(self):
        self.assert_code(self.folder / "private-missing.wav", "narration_not_found")
        directory = self.folder / "private-directory.wav"
        directory.mkdir()
        self.assert_code(directory, "narration_unreadable")
        self.assert_code(b"", "narration_empty")
        self.assert_code(wav_bytes(pcm=b""), "narration_empty")

    def test_unsupported_formats(self):
        float_fmt = struct.pack("<HHIIHH", 3, 1, 16000, 64000, 4, 32)
        guid_float = bytes.fromhex("0300000000001000800000aa00389b71")
        ext_float = struct.pack("<HHIIHHHHI", 0xFFFE, 1, 16000, 32000, 2, 16, 22, 16, 4) + guid_float
        cases = [b"ID3\x04" + b"\0" * 100, b"RIFF\x04\0\0\0AVI ",
                 wav_bytes(bits=8), wav_bytes(bits=24), wav_bytes(fmt=float_fmt, pcm=b"\0" * 64),
                 wav_bytes(fmt=ext_float), wav_bytes(tag=2), wav_bytes(channels=3),
                 wav_bytes(rate=7999), wav_bytes(rate=48001)]
        for data in cases:
            with self.subTest(data=data[:24]):
                self.assert_code(data, "narration_unsupported_format")

    def test_corrupt_files(self):
        good = wav_bytes(1)
        bad_block = struct.pack("<HHIIHH", 1, 1, 16000, 32000, 4, 16)
        cases = [good[:-100], wav_bytes(data_size=10 ** 7), good[:36],
                 wav_bytes(pcm=b"\0\0\0"), wav_bytes(fmt=bad_block),
                 wav_bytes(fmt=b"\x01\0\x01\0"), good + chunk(b"data", b"\0\0")]
        for data in cases:
            with self.subTest(size=len(data)):
                self.assert_code(data, "narration_corrupt")

    def test_size_limit_checked_before_parsing(self):
        oversized = self.write(b"\0" * (MAX_BYTES + 1))
        with patch("vicekrack.narration.parse_wav", side_effect=AssertionError("Not parsed")):
            self.assert_code(oversized, "narration_too_large")
        # Exactly 12 MB is accepted when the audio itself fits (padding is large metadata).
        base = wav_bytes(1, extra=(chunk(b"LIST", b"INFO"),))
        filler = MAX_BYTES - len(base) - 8
        exact = wav_bytes(1, extra=(chunk(b"LIST", b"INFO"), chunk(b"pad ", b"\0" * filler)))
        self.assertEqual(len(exact), MAX_BYTES)
        meta, wav = load_narration(self.write(exact))
        self.assertEqual(meta["source_duration_seconds"], 1)
        self.assertLess(len(wav), 1024 * 1024)

    def test_metadata_chunks_are_stripped_and_hash_is_normalized(self):
        plain = load_narration(self.write(wav_bytes(3, 22050, 2), "a.wav"))
        tagged = load_narration(self.write(wav_bytes(3, 22050, 2, extra=metadata_chunks()), "b.wav"))
        self.assertNotIn(MARKER, tagged[1])
        self.assertEqual(tagged[1][:4] + tagged[1][8:16], b"RIFFWAVEfmt ")
        self.assertEqual(tagged[1][36:40], b"data")
        self.assertEqual(tagged, plain)  # Same samples give the same normalized hash.
        self.assertNotIn("private", json.dumps(tagged[0]).lower())


class NarratedRenderTests(unittest.TestCase):
    def setUp(self):
        self.plan = build_scene_plan(read_json(ROOT / "examples/short-script-cooking.json"))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "previews"
        self.audio = Path(self.temp.name) / "private-recording.wav"
        self.audio.write_bytes(wav_bytes(4, 22050, 1, extra=metadata_chunks()))
        self.calls = []
        self.staged_audio = None

    def fake_card(self, plan, scene, path, modules, **options):
        self.calls.append(("card", options))
        path.write_bytes(b"\x89PNG\r\n\x1a\nfixture png")

    def fake_invoke(self, exe, args, cwd, fail_mux=False):
        self.calls.append(("ffmpeg", list(args)))
        if "narration.wav" in args:
            self.staged_audio = (cwd / "narration.wav").read_bytes()
            if fail_mux:
                raise NetworkError("render_failed", "Local encoding failed; no output was published.")
        if args[-1].endswith(".mp4"):
            (cwd / args[-1]).write_bytes(b"fixture-video" * 100)

    def render(self, fail_mux=False, **options):
        with patch("vicekrack.preview.dependencies", return_value=(None, None, None, "ffmpeg")), \
             patch("vicekrack.preview.make_card", side_effect=self.fake_card), \
             patch("vicekrack.preview.invoke", side_effect=lambda *a: self.fake_invoke(*a, fail_mux=fail_mux)), \
             patch("vicekrack.preview.uuid4", return_value=SimpleNamespace(hex="fixed")):
            return render_preview(self.plan, directory=self.folder, **options)

    def encoder_calls(self):
        return [args for kind, args in self.calls if kind == "ffmpeg"]

    def test_narrated_package_manifest_and_mux_arguments(self):
        result = self.render(narration=self.audio)
        self.assertTrue(result["audio_present"])
        self.assertFalse(result["publishable"])
        calls = self.encoder_calls()
        self.assertEqual(len(calls), 7)
        self.assertEqual(calls[4][-1], "silent.mp4")
        mux = calls[5]
        for flag in (["-map_metadata", "-1"], ["-map_chapters", "-1"], ["-c:v", "copy"], ["-map", "1:a:0"]):
            self.assertIn(" ".join(flag), " ".join(mux))
        for forbidden in ("-shortest", "-t", "-to", "-af"):
            self.assertNotIn(forbidden, mux)
        self.assertEqual(calls[6], ["-xerror", "-i", "preview.mp4", "-f", "null", "-"])
        self.assertNotIn(MARKER, self.staged_audio)
        self.assertEqual(len(self.staged_audio), 44 + 15 * 22050 * 2)
        self.assertTrue(all(options == {"narrated": True} for kind, options in self.calls if kind == "card"))
        manifest_text = Path(result["manifest_file"]).read_text()
        manifest = json.loads(manifest_text)
        self.assertTrue(manifest["audio_present"])
        self.assertFalse(manifest["publishable"])
        self.assertTrue(manifest["preview_only"])
        self.assertEqual(manifest["duration_seconds"], 15)
        self.assertEqual(manifest["audio"], {"source_duration_seconds": 4.0, "channels": 1, "sample_rate": 22050,
                                             "padded_duration_seconds": 15,
                                             "normalized_sha256": hashlib.sha256(self.staged_audio).hexdigest()})
        for private in ("private", "recording", self.temp.name.lower(), MARKER.decode().lower()):
            self.assertNotIn(private, manifest_text.lower())
        package = sorted(path.name for path in Path(result["preview_file"]).parent.iterdir())
        self.assertEqual(package, ["manifest.json", "preview.mp4"] + [f"scene-{n}.png" for n in range(1, 5)])
        self.assertEqual([p.name for p in self.folder.iterdir()], [Path(result["preview_file"]).parent.name])

    def test_invalid_narration_fails_before_any_rendering_work(self):
        self.audio.write_bytes(wav_bytes(16, 8000, 1))
        with patch("vicekrack.preview.dependencies", side_effect=AssertionError("No launch")), \
             self.assertRaises(NetworkError) as error:
            render_preview(self.plan, directory=self.folder, narration=self.audio)
        self.assertEqual(error.exception.code, "narration_too_long")
        self.assertFalse(self.folder.exists())

    def test_mux_failure_cleans_staging_and_publishes_nothing(self):
        with self.assertRaises(NetworkError) as error:
            self.render(fail_mux=True, narration=self.audio)
        self.assertEqual(error.exception.code, "render_failed")
        self.assertIsNotNone(self.staged_audio)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_silent_mode_is_unchanged(self):
        result = self.render()
        calls = self.encoder_calls()
        self.assertEqual(len(calls), 6)
        self.assertEqual(calls[4][-1], "preview.mp4")
        self.assertFalse(any("narration.wav" in args for args in calls))
        self.assertTrue(all(options == {} for kind, options in self.calls if kind == "card"))
        manifest = read_json(Path(result["manifest_file"]))
        self.assertFalse(manifest["audio_present"])
        self.assertNotIn("audio", manifest)
        self.assertEqual(manifest["limitations"][0], "silent storyboard")
        self.assertFalse(result["audio_present"])

    def test_draft_still_requires_consent_with_narration(self):
        self.plan = build_scene_plan(read_json(ROOT / "examples/short-script-gta.json"), draft=True)
        with self.assertRaises(NetworkError) as error:
            self.render(narration=self.audio)
        self.assertEqual(error.exception.code, "draft_preview_required")
        result = self.render(narration=self.audio, allow_draft=True)
        manifest = read_json(Path(result["manifest_file"]))
        self.assertTrue(manifest["source_blocked_for_production"])
        self.assertFalse(manifest["publishable"])

    def test_cli_narration_errors_are_sanitized(self):
        plan_path = Path(self.temp.name) / "plan.json"
        plan_path.write_text(json.dumps(self.plan))
        self.audio.write_bytes(b"private bytes, not audio")
        output = io.StringIO()
        with patch("sys.argv", ["vicekrack", "render-preview", str(plan_path), "--narration", str(self.audio)]), \
             patch("vicekrack.preview.dependencies", side_effect=AssertionError("No launch")), redirect_stdout(output):
            self.assertEqual(main(), 1)
        self.assertEqual(json.loads(output.getvalue()), {"error": {"code": "narration_unsupported_format"}})


@unittest.skipUnless(os.environ.get("RUN_LOCAL_RENDER_TESTS") == "1",
                     "Enable optional real FFmpeg integration with RUN_LOCAL_RENDER_TESTS=1")
class RealNarrationRenderTests(unittest.TestCase):
    def test_real_mp4_has_fifteen_seconds_of_audio_and_no_temporary_files(self):
        import imageio_ffmpeg
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "previews"
            audio = Path(temp) / "recording.wav"
            rate, seconds = 22050, 4.5
            pcm = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * n / rate)))
                           for n in range(int(rate * seconds)))
            audio.write_bytes(wav_bytes(rate=rate, pcm=pcm, extra=metadata_chunks()))
            plan = build_scene_plan(read_json(ROOT / "examples/short-script-cooking.json"))
            result = render_preview(plan, directory=folder, narration=audio)
            video = Path(result["preview_file"])
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            decoded = subprocess.run([exe, "-v", "error", "-nostdin", "-i", str(video), "-map", "0:a:0",
                                      "-f", "s16le", "-ac", "1", "-ar", "48000", "-"],
                                     capture_output=True, check=True, timeout=90).stdout
            self.assertAlmostEqual(len(decoded) / 2 / 48000, 15, delta=0.05)
            frames, video_seconds = imageio_ffmpeg.count_frames_and_secs(str(video))
            self.assertEqual(frames, 360)
            self.assertAlmostEqual(video_seconds, 15, places=1)
            self.assertNotIn(MARKER, video.read_bytes())
            manifest = read_json(Path(result["manifest_file"]))
            self.assertTrue(manifest["audio_present"])
            self.assertFalse(manifest["publishable"])
            self.assertEqual(manifest["audio"]["source_duration_seconds"], 4.5)
            self.assertEqual(sorted(p.name for p in video.parent.iterdir()),
                             ["manifest.json", "preview.mp4"] + [f"scene-{n}.png" for n in range(1, 5)])
            self.assertEqual([p.name for p in folder.iterdir()], [video.parent.name])


if __name__ == "__main__":
    unittest.main()
