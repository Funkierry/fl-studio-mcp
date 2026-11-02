"""Synthetic test material with known ground truth, for validating the analysis without real recordings."""

import numpy as np
from scipy import signal

SR = 22050


def _env(n, attack, decay, sr=SR):
    t = np.arange(n) / sr
    a = np.clip(t / max(attack, 1e-4), 0, 1)
    return a * np.exp(-t / decay)


def kick(sr=SR):
    n = int(0.35 * sr)
    t = np.arange(n) / sr
    f = 50 + 110 * np.exp(-t / 0.03)
    return 0.9 * np.sin(2 * np.pi * np.cumsum(f) / sr) * np.exp(-t / 0.18)


def snare(rng, sr=SR):
    n = int(0.25 * sr)
    noise = signal.sosfilt(signal.butter(2, [1200, 6000], "band", fs=sr, output="sos"), rng.standard_normal(n))
    t = np.arange(n) / sr
    return (0.5 * noise + 0.3 * np.sin(2 * np.pi * 190 * t)) * np.exp(-t / 0.09)


def hat(rng, sr=SR):
    n = int(0.08 * sr)
    noise = signal.sosfilt(signal.butter(4, 7000, "high", fs=sr, output="sos"), rng.standard_normal(n))
    return 0.25 * noise * np.exp(-np.arange(n) / sr / 0.02)


def saw(freq, n, sr=SR, harmonics=30):
    t = np.arange(n) / sr
    out = np.zeros(n)
    for k in range(1, harmonics + 1):
        if freq * k > sr / 2:
            break
        out += np.sin(2 * np.pi * freq * k * t) / k
    return out


def midi_hz(m):
    return 440 * 2 ** ((m - 69) / 12)


CHORDS = {"Am": [57, 60, 64], "F": [53, 57, 60], "C": [48, 52, 55], "G": [55, 59, 62], "Dm": [50, 53, 57], "E": [52, 56, 59]}
ROOT = {"Am": 45, "F": 41, "C": 48, "G": 43, "Dm": 38, "E": 40}

SECTIONS = [  # name, bars, chords per bar, kick steps, snare steps, hat steps, bass style, level
    ("Intro", 4, ["Am", "F", "C", "G"], [], [], [], None, 0.5),
    ("Verse", 8, ["Am", "F", "C", "G"], [0, 8], [4, 12], list(range(0, 16, 2)), "eighths", 0.7),
    ("Chorus", 8, ["F", "G", "C", "Am"], [0, 4, 8, 12], [4, 12], list(range(16)), "octaves", 1.0),
    ("Verse", 8, ["Am", "F", "C", "G"], [0, 8], [4, 12], list(range(0, 16, 2)), "eighths", 0.7),
    ("Chorus", 8, ["F", "G", "C", "Am"], [0, 4, 8, 12], [4, 12], list(range(16)), "octaves", 1.0),
    ("Outro", 4, ["Am", "Am", "Am", "Am"], [], [], [], None, 0.4),
]


# More chord shapes for other styles (7ths are written as voicings; the analyzer reports their triads).
CHORDS.update({
    "Dm7": [50, 53, 57, 60], "G7": [55, 59, 62, 65], "Cmaj7": [48, 52, 55, 59], "Am7": [57, 60, 64, 67],
    "Fm": [53, 56, 60], "Db": [49, 53, 56], "Ab": [56, 60, 63], "Eb": [51, 55, 58], "Bb": [46, 50, 53], "Gm": [55, 58, 62],
})
ROOT.update({"Dm7": 38, "G7": 43, "Cmaj7": 48, "Am7": 45, "Fm": 41, "Db": 37, "Ab": 44, "Eb": 39, "Bb": 46, "Gm": 43})
TRIAD = {"Dm7": "Dm", "G7": "G", "Cmaj7": "C", "Am7": "Am"}


def make_song(bpm=120.0, sr=SR, seed=1, sections=None, swing=0.0):
    """Render a song from section tuples (name, bars, chords per bar, kick/snare/hat 16th steps, bass style,
    level). Bass styles: 'eighths', 'octaves', 'offbeat'. swing delays odd 16ths by that fraction of a 16th."""
    sections = sections or SECTIONS
    rng = np.random.default_rng(seed)
    beat = 60 / bpm
    bar = 4 * beat
    total_bars = sum(s[1] for s in sections)
    out = np.zeros(int((total_bars * bar + 2) * sr))
    k, h = kick(sr), hat(rng, sr)
    truth = []
    bar_i = 0

    def put(sig, t):
        i = int(t * sr)
        j = min(len(out), i + len(sig))
        out[i:j] += sig[:j - i]

    def at(t0, step):
        return t0 + step * beat / 4 + (swing * beat / 4 if step % 2 else 0)

    for name, bars, chords, ks, ss, hs, bass, level in sections:
        truth.append({"name": name, "start_bar": bar_i, "bars": bars,
                      "chords": [TRIAD.get(c, c) for c in chords],
                      "drums": {"kick": "".join("x" if i in ks else "." for i in range(16)),
                                "snare": "".join("x" if i in ss else "." for i in range(16)),
                                "hat": "".join("x" if i in hs else "." for i in range(16))},
                      "bass": bass})
        for b in range(bars):
            t0 = (bar_i + b) * bar
            ch = chords[b % len(chords)]
            n = int(bar * sr)
            pad = sum(saw(midi_hz(m), n, sr, 12) for m in CHORDS[ch]) * 0.12 * level
            pad = signal.sosfilt(signal.butter(2, 2500, "low", fs=sr, output="sos"), pad) * _env(n, 0.05, 3.0, sr)
            put(pad, t0)
            for s in ks:
                put(k * level, at(t0, s))
            for s in ss:
                put(snare(rng, sr) * level, at(t0, s))
            for s in hs:
                put(h * level * (1.0 if s % 4 == 0 else 0.6), at(t0, s))
            if bass:
                for e in range(8):
                    if bass == "offbeat" and e % 2 == 0:
                        continue
                    pitch = ROOT[ch] - 12 + (12 if bass == "octaves" and e % 2 else 0)
                    m = int(beat / 2 * 0.9 * sr)
                    note = saw(midi_hz(pitch), m, sr, 20)
                    note = signal.sosfilt(signal.butter(2, 700, "low", fs=sr, output="sos"), note) * _env(m, 0.005, 0.4, sr)
                    put(0.35 * level * note, at(t0, e * 2))
        bar_i += bars
    out /= np.abs(out).max() * 1.1
    return out.astype(np.float32), truth


FOUR = [0, 4, 8, 12]
HOUSE = [  # 124 BPM, F minor; drums-only intro, a breakdown without drums
    ("Intro", 8, ["Fm", "Db", "Ab", "Eb"], FOUR, [], [2, 6, 10, 14], None, 0.6),
    ("Drop", 16, ["Fm", "Db", "Ab", "Eb"], FOUR, [4, 12], [2, 6, 10, 14], "offbeat", 1.0),
    ("Breakdown", 8, ["Db", "Eb", "Fm", "Fm"], [], [], [], None, 0.5),
    ("Drop", 16, ["Fm", "Db", "Ab", "Eb"], FOUR, [4, 12], [2, 6, 10, 14], "offbeat", 1.0),
    ("Outro", 8, ["Fm", "Fm", "Fm", "Fm"], FOUR, [], [2, 6, 10, 14], None, 0.6),
]
LOFI = [  # 85 BPM, D dorian-ish ii–V–I–vi with 7th chords and swing
    ("Intro", 4, ["Dm7", "G7", "Cmaj7", "Am7"], [], [], [], None, 0.5),
    ("Verse", 8, ["Dm7", "G7", "Cmaj7", "Am7"], [0, 7, 10], [4, 12], list(range(0, 16, 2)), "eighths", 0.8),
    ("Hook", 8, ["F", "G", "Am7", "Am7"], [0, 7, 10], [4, 12], list(range(16)), "eighths", 1.0),
    ("Verse", 8, ["Dm7", "G7", "Cmaj7", "Am7"], [0, 7, 10], [4, 12], list(range(0, 16, 2)), "eighths", 0.8),
    ("Hook", 8, ["F", "G", "Am7", "Am7"], [0, 7, 10], [4, 12], list(range(16)), "eighths", 1.0),
    ("Outro", 4, ["Dm7", "Dm7", "Dm7", "Dm7"], [], [], [], None, 0.4),
]


def make_note(kind, sr=44100):
    """One-shot test sounds with known character, for sound matching and timbre description."""
    dur = {"bass": 1.2, "pluck": 0.8, "pad": 3.0, "lead": 1.5}[kind]
    n = int(dur * sr)
    t = np.arange(n) / sr
    if kind == "bass":      # A1 saw through a low-pass, fast attack, medium decay
        x = signal.sosfilt(signal.butter(2, 500, "low", fs=sr, output="sos"), saw(55, n, sr, 120))
        env = np.minimum(t / 0.004, 1) * np.exp(-t / 0.45)
    elif kind == "pluck":   # C4 saw with a fast filter sweep — bright attack, short
        x = saw(261.63, n, sr, 80)
        hi = signal.sosfilt(signal.butter(2, 6000, "low", fs=sr, output="sos"), x)
        lo = signal.sosfilt(signal.butter(2, 600, "low", fs=sr, output="sos"), x)
        mixw = np.exp(-t / 0.06)
        x = hi * mixw + lo * (1 - mixw)
        env = np.minimum(t / 0.002, 1) * np.exp(-t / 0.18)
    elif kind == "pad":     # A3 two detuned saws, slow attack, darker filter
        x = saw(220, n, sr, 40) + saw(221.3, n, sr, 40)
        x = signal.sosfilt(signal.butter(2, 1800, "low", fs=sr, output="sos"), x)
        env = np.minimum(t / 0.45, 1) * np.where(t < dur - 0.6, 1.0, np.exp(-(t - (dur - 0.6)) / 0.2))
    else:                   # E4 square-ish lead (odd harmonics), sustained, bright
        f = 329.63
        x = sum(np.sin(2 * np.pi * f * k * t) / k for k in range(1, 30, 2) if f * k < sr / 2)
        x = signal.sosfilt(signal.butter(2, 4000, "low", fs=sr, output="sos"), x)
        env = np.minimum(t / 0.01, 1) * np.where(t < dur - 0.3, 0.8 + 0.2 * np.exp(-t / 0.1), np.exp(-(t - (dur - 0.3)) / 0.08))
    x = x * env
    x = x / np.abs(x).max() * 0.7
    return np.pad(x, (int(0.05 * sr), int(0.3 * sr))).astype(np.float32)


def make_lead_loop(cutoff, sr=44100, bpm=110, bars=4):
    """A melodic lead loop; render with a low cutoff for a 'dull' version and a high one for a 'bright' version."""
    beat = 60 / bpm
    melody = [69, 72, 76, 74, 72, 69, 67, 69] * bars
    out = np.zeros(int((len(melody) * beat / 2 + 1) * sr))
    for i, m in enumerate(melody):
        n = int(beat / 2 * 0.95 * sr)
        t = np.arange(n) / sr
        x = saw(midi_hz(m), n, sr, 60) * np.minimum(t / 0.005, 1) * np.exp(-t / 0.5)
        s = int(i * beat / 2 * sr)
        out[s:s + n] += x
    out = signal.sosfilt(signal.butter(2, cutoff, "low", fs=sr, output="sos"), out)
    out = out / np.abs(out).max() * 0.6
    # a little stereo width
    d = int(0.012 * sr)
    right = np.concatenate([np.zeros(d), out[:-d]])
    return np.stack([out, 0.85 * out + 0.15 * right], axis=1).astype(np.float32)
