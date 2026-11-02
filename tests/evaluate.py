"""
Score the analysis against the ground truth in samples/ (run tests/make_samples.py first).

    python tests/evaluate.py
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from fl_studio_mcp.audio import dsp, perception, song, timbre  # noqa: E402

SAMPLES = ROOT / "samples"


def drum_acc(found, truth):
    hits = sum((a != ".") == (b != ".") for a, b in zip(found, truth))
    return hits / len(truth)


def songs():
    print("== songs")
    rows = []
    for wav in sorted((SAMPLES / "songs").glob("*.wav")):
        truth = json.loads(wav.with_suffix(".truth.json").read_text(encoding="utf-8"))
        audio, sr = dsp.load_audio(wav)
        t = time.time()
        r = song.analyze_song(audio, sr)
        took = time.time() - t
        bounds_ok = [(s["start_bar"], s["bars"]) for s in r["sections"]] == [(s["start_bar"], s["bars"]) for s in truth["sections"]]
        # chords: compare each analyzed section's bar chords with the truth section at the same position
        chord_hits = chord_total = 0
        drum_scores = []
        for found, ref in zip(r["sections"], truth["sections"]):
            bars = {}
            for c in found["chords"]:
                bars.setdefault(c["bar"], c["chord"])  # chord at the start of each bar
            last = None
            for b in range(ref["bars"]):
                last = bars.get(b, last)
                chord_total += 1
                chord_hits += last == ref["chords"][b % len(ref["chords"])]
        parts = {p["label"]: p for p in r["parts"]}
        for found, ref in zip(r["sections"], truth["sections"]):
            p = parts.get(found["label"])
            if p and any(set(v) != {"."} for v in ref["drums"].values()):
                drum_scores += [drum_acc(p["drums"][k], ref["drums"][k]) for k in ("kick", "snare", "hat")]
        rows.append((wav.stem, truth["tempo"], r["tempo_bpm"], truth["key"], r["key"]["name"], bounds_ok,
                     [s["name"] for s in r["sections"]], chord_hits / chord_total,
                     sum(drum_scores) / len(drum_scores) if drum_scores else None, took))
    for name, t_tempo, tempo, t_key, key, ok, names, chords, drums, took in rows:
        print(f"{name:14} tempo {tempo:>6} (truth {t_tempo})  key {key:9} (truth {t_key.split(' (')[0]})  "
              f"sections {'OK ' if ok else 'BAD'} {names}")
        print(f"{'':14} chords {chords:.0%}  drums {drums:.0%}  ({took:.1f} s)" if drums is not None else
              f"{'':14} chords {chords:.0%}  ({took:.1f} s)")


def sounds():
    print("\n== sounds")
    truth = json.loads((SAMPLES / "sounds" / "truth.json").read_text(encoding="utf-8"))
    descs = {}
    for wav in sorted((SAMPLES / "sounds").glob("*.wav")):
        audio, sr = dsp.load_audio(wav)
        d = timbre.describe(audio, sr)
        descs[wav.stem] = d
        print(f"{wav.stem:9} note {d['midi_note']} ({d['f0_hz']} Hz)  attack {d['attack_s'] * 1000:.0f} ms  "
              f"decay {d['decay_to_-20db_s']:.2f} s  centroid {d['centroid_hz']:.0f} Hz  | truth: {truth[wav.stem]}")
    names = list(descs)
    print("distance matrix (smaller = more similar):")
    print(" " * 10 + "".join(f"{n:>10}" for n in names))
    for a in names:
        print(f"{a:10}" + "".join(f"{timbre.distance(descs[a], descs[b])[0]:>10.1f}" for b in names))


def mix():
    print("\n== diagnosis ('不够亮')")
    dull, sr = dsp.load_audio(SAMPLES / "mix" / "lead_dull.wav")
    bright, _ = dsp.load_audio(SAMPLES / "mix" / "lead_bright.wav")
    m_dull, m_bright = perception.metrics(dull, sr), perception.metrics(bright, sr)
    ids = perception.match_descriptors("这个lead听起来有点不够明亮")
    for label, m, ref in (("dull, rules of thumb", m_dull, None), ("bright, rules of thumb", m_bright, None),
                          ("dull vs bright reference", m_dull, m_bright)):
        f = perception.diagnose(m, ids, role="lead", reference=ref)[0]
        extra = f"difference {f.get('difference')} dB" if ref else f"range {f.get('typical_range')}"
        print(f"{label:26} → {f['verdict']:34} measured {f['measured']} dB above 4 kHz, {extra}")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    songs()
    sounds()
    mix()
