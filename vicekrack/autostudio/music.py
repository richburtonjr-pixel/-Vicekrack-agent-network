"""Auto Studio music: original instrumental beds composed on your computer with FFmpeg's audio synthesizer.

No downloaded songs, samples or loops: every bed is generated from a fixed chord progression, a synthesized
kick and filtered noise hats, so it is original and free to use in your videos. A mood picks the tempo,
progression and drums; the same mood and length always give the same file.
"""

from pathlib import Path

from .common import run_ffmpeg

# Note frequencies (Hz).
N = {"A2": 110.0, "C3": 130.81, "D3": 146.83, "E3": 164.81, "F3": 174.61, "G3": 196.0, "A3": 220.0, "Bb3": 233.08,
     "B3": 246.94, "C4": 261.63, "D4": 293.66, "E4": 329.63, "F4": 349.23, "G4": 392.0, "A4": 440.0}

MOODS = {
    "steady": {"bpm": 96, "chords": [("A3", "C4", "E4"), ("F3", "A3", "C4"), ("C4", "E4", "G4"), ("G3", "B3", "D4")],
               "bass": ["A2", "F3", "C3", "G3"], "kick": "1111", "hats": 0.05, "pad": 0.16},
    "upbeat": {"bpm": 118, "chords": [("C4", "E4", "G4"), ("G3", "B3", "D4"), ("A3", "C4", "E4"), ("F3", "A3", "C4")],
               "bass": ["C3", "G3", "A2", "F3"], "kick": "1111", "hats": 0.08, "pad": 0.14},
    "epic": {"bpm": 80, "chords": [("D4", "F4", "A4"), ("Bb3", "D4", "F4"), ("F3", "A3", "C4"), ("C4", "E4", "G4")],
             "bass": ["D3", "Bb3", "F3", "C3"], "kick": "1010", "hats": 0.0, "pad": 0.2},
    "tense": {"bpm": 90, "chords": [("E3", "G3", "B3"), ("C4", "E4", "G4"), ("E3", "G3", "B3"), ("D4", "F4", "A4")],
              "bass": ["E3", "C3", "E3", "D3"], "kick": "1001", "hats": 0.04, "pad": 0.17},
    "calm": {"bpm": 72, "chords": [("C4", "E4", "G4"), ("A3", "C4", "E4"), ("F3", "A3", "C4"), ("G3", "B3", "D4")],
             "bass": ["C3", "A2", "F3", "G3"], "kick": "0000", "hats": 0.0, "pad": 0.18},
}


def _select(values, index_expr):
    expr = f"{values[-1]}"
    for i in range(len(values) - 2, -1, -1):
        expr = f"if(eq({index_expr},{i}),{values[i]},{expr})"
    return expr


def expression(mood):
    m = MOODS[mood]
    beat = 60.0 / m["bpm"]
    bar = 4 * beat
    chord_len = 2 * bar
    k = f"mod(floor(t/{chord_len:.5f}),4)"
    pos = f"mod(t,{chord_len:.5f})"
    env = f"min(1,{pos}/0.8)*min(1,({chord_len:.5f}-{pos})/0.4+0.15)"
    pads = []
    for note in range(3):
        f = _select([N[c[note]] for c in m["chords"]], k)
        pads.append(f"sin(2*PI*{f}*t)+0.5*sin(2*PI*{f}*1.004*t)+0.18*sin(4*PI*{f}*t)")
    pad = f"{m['pad']}*({env})*(" + "+".join(pads) + ")/3.6"
    bass_f = _select([N[b] for b in m["bass"]], k)
    bass = f"0.22*({env})*sin(2*PI*{bass_f}/2*t)"
    b = f"mod(t,{beat:.5f})"
    beat_no = f"mod(floor(t/{beat:.5f}),4)"
    kick_on = _select([int(x) for x in m["kick"]], beat_no)
    kick = f"{kick_on}*0.55*exp(-{b}*9)*sin(2*PI*(48+70*exp(-{b}*30))*{b})"
    half = beat / 2
    hats = f"{m['hats']}*(random(0)*2-1)*exp(-mod(t+{half / 2:.5f},{half:.5f})*55)" if m["hats"] else "0"
    return f"{pad}+{bass}+{kick}+{hats}"


def compose(mood, seconds, target, cwd):
    """Write an original stereo 48 kHz WAV bed of `seconds` length to target."""
    mood = mood if mood in MOODS else "steady"
    fade = min(2.0, seconds / 4)
    expr = expression(mood)
    run_ffmpeg(["-y", "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d={seconds:.3f}",
                "-af", (f"highpass=f=35,lowpass=f=9000,aecho=0.8:0.6:60|120:0.25|0.18,"
                        f"afade=t=in:d=1.2,afade=t=out:st={max(0, seconds - fade):.3f}:d={fade:.3f},"
                        "loudnorm=I=-20:TP=-3:LRA=11,aformat=channel_layouts=stereo"),
                "-ar", "48000", "-c:a", "pcm_s16le", str(target)], cwd, timeout=600)
    return Path(target)
