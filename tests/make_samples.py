"""
Generate test material with ground truth into samples/ (gitignored):

  songs/   pop_120_Am.wav, house_124_Fm.wav, lofi_85_C.wav  (+ .truth.json each)
  sounds/  bass_A1.wav, pluck_C4.wav, pad_A3.wav, lead_E4.wav — one-shots for fl_match_sound
  mix/     lead_dull.wav, lead_bright.wav — the same lead loop, filtered dark vs open, for fl_diagnose

    python tests/make_samples.py
"""

import json
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import synth  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "samples"
SR = 44100


def write(path, audio, sr=SR):
    path.parent.mkdir(parents=True, exist_ok=True)
    if audio.ndim == 1:
        audio = np.stack([audio, audio], axis=1)
    wavfile.write(str(path), sr, (np.clip(audio, -1, 1) * 32767).astype(np.int16))
    print(f"  {path.relative_to(OUT.parent)}  ({len(audio) / sr:.1f} s)")


def main():
    print("songs")
    songs = [
        ("pop_120_Am", dict(bpm=120, seed=1), {"tempo": 120, "key": "A minor"}),
        ("house_124_Fm", dict(bpm=124, seed=2, sections=synth.HOUSE), {"tempo": 124, "key": "F minor"}),
        ("lofi_85_C", dict(bpm=85, seed=3, sections=synth.LOFI, swing=0.25),
         {"tempo": 85, "key": "C major (ii–V–I–vi; A minor / D dorian readings are defensible)"}),
    ]
    for name, kw, meta in songs:
        audio, truth = synth.make_song(sr=SR, **kw)
        write(OUT / "songs" / f"{name}.wav", audio)
        (OUT / "songs" / f"{name}.truth.json").write_text(
            json.dumps({**meta, "time_signature": "4/4", "sections": truth}, ensure_ascii=False, indent=2), encoding="utf-8")

    print("sounds")
    truths = {"bass": "A1 (55 Hz) saw, 500 Hz low-pass, 4 ms attack, ~0.45 s decay",
              "pluck": "C4 saw, filter sweeping 6 kHz→600 Hz in ~60 ms, 2 ms attack, short",
              "pad": "A3 two detuned saws, 1.8 kHz low-pass, 0.45 s attack, sustained",
              "lead": "E4 square-like (odd harmonics), 4 kHz low-pass, sustained"}
    names = {"bass": "bass_A1", "pluck": "pluck_C4", "pad": "pad_A3", "lead": "lead_E4"}
    for kind, desc in truths.items():
        write(OUT / "sounds" / f"{names[kind]}.wav", synth.make_note(kind, SR))
    (OUT / "sounds" / "truth.json").write_text(json.dumps({names[k]: v for k, v in truths.items()}, indent=2),
                                               encoding="utf-8")

    print("mix")
    write(OUT / "mix" / "lead_dull.wav", synth.make_lead_loop(700, SR))
    write(OUT / "mix" / "lead_bright.wav", synth.make_lead_loop(7000, SR))
    (OUT / "mix" / "truth.json").write_text(json.dumps({
        "lead_dull.wav": "same melody, 700 Hz low-pass: should be diagnosed as dull ('不够亮')",
        "lead_bright.wav": "same melody, 7 kHz low-pass: bright reference"}, indent=2), encoding="utf-8")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
