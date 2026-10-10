"""Optional local narration for previews: strict 16-bit PCM WAV validation and normalization.

The input file is parsed with a small bounded RIFF reader (no provider clients, no network,
no subprocess). Only the format and sample data are kept; every other chunk (LIST/INFO,
bext, iXML, ID3, cue points and so on) is discarded. The samples are padded with silence
to the fixed preview duration and rewritten as a canonical 44-byte-header WAV. Audio that
is longer than the preview is rejected rather than truncated.
"""
import hashlib
import os
import stat
import struct
from pathlib import Path

from .errors import NetworkError

MAX_BYTES = 12 * 1024 * 1024
MAX_SECONDS = 15
MIN_RATE, MAX_RATE = 8000, 48000
PCM = 0x0001
EXTENSIBLE = 0xFFFE
# KSDATAFORMAT_SUBTYPE_PCM, used by WAVE_FORMAT_EXTENSIBLE files.
PCM_SUBFORMAT = bytes.fromhex("0100000000001000800000aa00389b71")


def _fail(code, message):
    # Messages are fixed text: never include the path, file bytes or parser exceptions.
    raise NetworkError(code, message)


def read_bounded(path):
    """Read a regular local file without following it past the size limit."""
    try:
        with open(Path(path), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                _fail("narration_unreadable", "Narration must be a readable local file.")
            if info.st_size > MAX_BYTES:
                _fail("narration_too_large", "Narration WAV must be 12 MB or smaller.")
            data = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        _fail("narration_not_found", "Narration file was not found.")
    except NetworkError:
        raise
    except (OSError, ValueError, TypeError):
        _fail("narration_unreadable", "Narration must be a readable local file.")
    if len(data) > MAX_BYTES:
        _fail("narration_too_large", "Narration WAV must be 12 MB or smaller.")
    if not data:
        _fail("narration_empty", "Narration file is empty.")
    return data


def parse_wav(data):
    """Return (channels, sample_rate, pcm_bytes) for a supported WAV, else raise."""
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        _fail("narration_unsupported_format", "Narration must be a WAV (RIFF/WAVE) file.")
    fmt = pcm = None
    offset = 12
    while offset + 8 <= len(data):
        chunk_id, size = data[offset:offset + 4], struct.unpack_from("<I", data, offset + 4)[0]
        body_start = offset + 8
        body_end = body_start + size
        if body_end > len(data):
            _fail("narration_corrupt", "Narration WAV is truncated or corrupt.")
        if chunk_id == b"fmt ":
            if fmt is not None:
                _fail("narration_corrupt", "Narration WAV has duplicate format chunks.")
            fmt = data[body_start:body_end]
        elif chunk_id == b"data":
            if pcm is not None:
                _fail("narration_corrupt", "Narration WAV has duplicate data chunks.")
            pcm = data[body_start:body_end]
        offset = body_end + (size & 1)  # RIFF chunks are word aligned.
    if fmt is None or pcm is None:
        _fail("narration_corrupt", "Narration WAV is missing its format or audio data.")
    if len(fmt) < 16:
        _fail("narration_corrupt", "Narration WAV format chunk is malformed.")
    tag, channels, rate, byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", fmt)
    if tag == EXTENSIBLE:
        if len(fmt) < 40 or fmt[24:40] != PCM_SUBFORMAT:
            _fail("narration_unsupported_format", "Narration must be uncompressed 16-bit PCM WAV.")
    elif tag != PCM:
        _fail("narration_unsupported_format", "Narration must be uncompressed 16-bit PCM WAV.")
    if bits != 16:
        _fail("narration_unsupported_format", "Narration must be 16-bit PCM WAV.")
    if channels not in (1, 2):
        _fail("narration_unsupported_format", "Narration must be mono or stereo.")
    if not MIN_RATE <= rate <= MAX_RATE:
        _fail("narration_unsupported_format", "Narration sample rate must be from 8 to 48 kHz.")
    if block_align != channels * 2 or byte_rate != rate * block_align:
        _fail("narration_corrupt", "Narration WAV format fields are inconsistent.")
    if not pcm:
        _fail("narration_empty", "Narration WAV contains no audio samples.")
    if len(pcm) % block_align:
        _fail("narration_corrupt", "Narration WAV audio data ends mid-sample.")
    return channels, rate, pcm


def canonical_wav(channels, rate, pcm):
    """A metadata-free WAV: RIFF header, one PCM fmt chunk and one data chunk only."""
    block = channels * 2
    header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16,
                         PCM, channels, rate, rate * block, block, 16, b"data", len(pcm))
    return header + pcm


def load_narration(path, target_seconds=MAX_SECONDS):
    """Validate, strip metadata and pad with silence. Returns safe metadata and WAV bytes."""
    return normalize_narration(read_bounded(path), target_seconds)


def has_sound(data):
    """True if a supported WAV contains at least one non-zero sample (Step 45: all-silent input is refused)."""
    _, _, pcm = parse_wav(data)
    return any(pcm)


def normalize_narration(data, target_seconds=MAX_SECONDS):
    """Step 14 rules applied to bytes already read (Step 45 validates exactly the bytes it stores)."""
    if not data:
        _fail("narration_empty", "Narration file is empty.")
    if len(data) > MAX_BYTES:
        _fail("narration_too_large", "Narration WAV must be 12 MB or smaller.")
    channels, rate, pcm = parse_wav(data)
    frames = len(pcm) // (channels * 2)
    target_frames = target_seconds * rate
    if frames > target_frames:
        _fail("narration_too_long", "Narration must be 15 seconds or shorter; it is never truncated.")
    padded = pcm + bytes((target_frames - frames) * channels * 2)
    wav = canonical_wav(channels, rate, padded)
    metadata = {"source_duration_seconds": round(frames / rate, 3), "channels": channels,
                "sample_rate": rate, "padded_duration_seconds": target_seconds,
                "normalized_sha256": hashlib.sha256(wav).hexdigest()}
    return metadata, wav
