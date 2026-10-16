"""Auto Studio voices: one xAI text-to-speech request per spoken line, with the speaker's stock voice and
per-character timestamps (used to time the captions exactly). Audio is converted to a canonical 48 kHz
mono WAV and measured; nothing is cut, sped up or rewritten."""

import json
import struct
import wave
from pathlib import Path

from ..errors import NetworkError
from ..speech_jobs import _envelope, decode_audio
from ..speech_transport import SpeechRejected, transport as http_transport
from .common import run_ffmpeg, sha256_bytes

SAMPLE_RATE = 24000


def assign_voices(config, speakers):
    """Speaker name -> stock voice. Narrator keeps the narrator voice; characters alternate by kind."""
    voices, pools = {}, {"woman": list(config["voices"]["female"]), "man": list(config["voices"]["male"])}
    for s in speakers:
        if s["kind"] == "narrator":
            voices[s["name"]] = config["voices"]["narrator"]
        else:
            pool = pools[s["kind"]]
            voices[s["name"]] = pool.pop(0) if pool else config["voices"]["female" if s["kind"] == "woman" else "male"][0]
    return voices


def request_body(text, voice):
    return {"text": text, "voice_id": voice, "language": "en",
            "output_format": {"codec": "wav", "sample_rate": SAMPLE_RATE}, "with_timestamps": True}


def speak(text, voice, target, *, transport=None):
    """One paid TTS request. Writes target (WAV) and returns {"seconds", "words": [(word, start, end)], "sha256"}.
    Raises NetworkError speech_rejected (not billed), speech_not_sent (not billed) or speech_uncertain."""
    body = request_body(text, voice)
    try:
        response = (transport or http_transport)(body, timeout_seconds=90, max_bytes=16 * 1024 * 1024)
        raw = response["audio"]
        audio, _, stamps = _envelope(raw, 16 * 1024 * 1024)
        channels, rate, pcm = decode_audio(audio, "wav", SAMPLE_RATE)
    except SpeechRejected:
        raise NetworkError("speech_rejected", "xAI refused a voice line; nothing was generated.") from None
    except NetworkError as error:
        if error.code in ("speech_not_sent", "missing_speech_credential"):
            raise
        raise NetworkError("speech_uncertain", "A voice request failed; it may have been billed.") from None
    except (KeyError, TypeError):
        raise NetworkError("speech_uncertain", "A voice request returned something unusable.") from None
    write_wav(target, channels, rate, pcm)
    seconds = len(pcm) / (2 * channels * rate)
    if seconds < 0.3 or _silent(pcm):
        raise NetworkError("speech_uncertain", "A voice line came back silent.")
    return {"seconds": round(seconds, 3), "words": words_from_timestamps(text, stamps, seconds),
            "sha256": sha256_bytes(Path(target).read_bytes())}


def write_wav(target, channels, rate, pcm):
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)


def _silent(pcm):
    count = len(pcm) // 2
    if count == 0:
        return True
    step = max(1, count // 4000)
    peak = max(abs(v) for v in struct.unpack_from(f"<{count}h", pcm)[::step])
    return peak < 300


def words_from_timestamps(text, stamps, seconds):
    """(word, start, end) for every word: from the provider's character times when they line up with the text,
    otherwise spread over the line by character count (marked estimated)."""
    words = text.split()
    chars, times = (stamps or {}).get("graph_chars") or [], (stamps or {}).get("graph_times") or []
    out = []
    if chars and len(chars) == len(times):
        joined = "".join(str(c) for c in chars)
        cursor = 0
        try:
            for word in words:
                found = joined.find(word, cursor)
                if found < 0:
                    raise ValueError
                start = _time(times[found], 0)
                end = _time(times[found + len(word) - 1], 1)
                out.append([word, round(start, 3), round(max(end, start + 0.05), 3)])
                cursor = found + len(word)
            return {"method": "provider", "words": out}
        except (ValueError, IndexError, TypeError):
            out = []
    total = sum(len(w) + 1 for w in words) or 1
    at = 0.0
    for word in words:
        span = seconds * (len(word) + 1) / total
        out.append([word, round(at, 3), round(at + span, 3)])
        at += span
    return {"method": "estimated", "words": out}


def _time(value, side):
    if isinstance(value, (list, tuple)):
        return float(value[side])
    if isinstance(value, dict):
        return float(value.get("start" if side == 0 else "end", value.get("time")))
    return float(value)


def mock_transport(seed="demo"):
    """Offline stand-in for the demo and tests: a tone-burst 'voice' as long as the words would take to say,
    returned in the documented timestamped envelope. Clearly synthetic, never a real voice."""
    import base64
    import io
    import math

    def send(body, *, timeout_seconds, max_bytes):
        text = body["text"]
        seconds = max(0.8, len(text.split()) / 2.6)
        rate = body["output_format"]["sample_rate"]
        frames = bytearray()
        pitch = 180 + (sum(map(ord, body["voice_id"])) % 6) * 30
        for i in range(int(seconds * rate)):
            t = i / rate
            envelope = 0.5 + 0.5 * math.sin(2 * math.pi * 3.2 * t)
            frames += struct.pack("<h", int(9000 * envelope * math.sin(2 * math.pi * pitch * t)))
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(rate); out.writeframes(bytes(frames))
        chars = list(text)
        times = [[round(seconds * i / len(chars), 3), round(seconds * (i + 1) / len(chars), 3)] for i in range(len(chars))]
        document = {"audio": base64.b64encode(buffer.getvalue()).decode(), "content_type": "audio/wav",
                    "audio_timestamps": {"graph_chars": chars, "graph_times": times}, "duration": seconds}
        return {"content_type": "application/json", "audio": json.dumps(document).encode()}
    return send


def to_stereo48(source, target, cwd):
    run_ffmpeg(["-y", "-i", str(source), "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(target)], cwd, timeout=120)
