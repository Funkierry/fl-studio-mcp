"""
Turn vague listening language ("不够亮", "a bit muddy", "没力") into measurements, a verdict and concrete fixes.

Each descriptor names the measurement that operationalizes it and which direction is "the problem". Judgement uses,
in order of preference: a reference recording (band-by-band comparison), the rest of the mix (how the track sits
against it), or heuristic ranges per role. The heuristic ranges are rules of thumb, and the output says which basis
was used so the assistant can be honest about confidence.
"""

import numpy as np

from . import dsp

# measurement helpers ------------------------------------------------------------------------------------------

def metrics(audio, sr):
    mag = dsp.stft_mag(audio, 4096, 1024)
    bands = dsp.band_energies_db(mag, sr, 4096)
    stats = dsp.spectral_stats(mag, sr, 4096)
    loud = dsp.loudness(audio, sr)
    st = dsp.stereo_stats(audio)
    env = dsp.envelope_db(audio, sr, 0.01)
    active = env > env.max() - 40
    hf = 10 * np.log10(10 ** (bands["presence"] / 10) + 10 ** (bands["air"] / 10) + dsp.EPS)
    return {
        "loudness": loud,
        "bands_db": bands,
        "high_freq_db": round(float(hf), 1),            # energy above 4 kHz relative to total
        "spectral": stats,
        "stereo": st,
        "dynamics_db": round(float(np.percentile(env[active], 95) - np.percentile(env[active], 10)), 1) if active.any() else 0.0,
        "active_ratio": round(float(active.mean()), 2),
    }


def _g(m, path):
    v = m
    for k in path.split("."):
        v = v[k]
    return v


# descriptor vocabulary -------------------------------------------------------------------------------------------
# metric: dotted path into metrics(); problem: "low" means the complaint is that the metric is too low.
# ranges: rough (min, max) by role for the metric when no reference is available.

DESCRIPTORS = [
    {"id": "dull", "problem": "low", "metric": "high_freq_db",
     "words": ["不够亮", "不亮", "暗", "发暗", "闷", "发闷", "沉闷", "糊住", "不通透", "没空气感", "dull", "dark", "muffled",
               "not bright", "lacks air", "lacks sparkle", "明亮", "亮"],
     "meaning": "too little energy above ~4 kHz (presence + air)",
     "ranges": {"lead": (-20, -8), "pad": (-28, -12), "pluck": (-22, -8), "vocal": (-22, -10), "drums": (-18, -6),
                "bass": (-45, -20), "mix": (-22, -12)},
     "fixes": [("mixer_eq", "high", +3.0, "high shelf / high band boost around 6–10 kHz"),
               ("plugin_param", ["cutoff", "filter", "freq", "bright", "tone", "滤波"], "+", "open the synth's filter"),
               ("advice", None, None, "brighter source: saw/square instead of sine, more unison, an exciter/saturation")]},
    {"id": "harsh", "problem": "high", "metric": "bands_db.high_mid",
     "words": ["刺耳", "扎耳", "尖", "太尖", "刺", "harsh", "piercing", "shrill", "fatiguing", "太亮"],
     "meaning": "too much 2–4 kHz (or presence) energy",
     "ranges": {"lead": (-18, -7), "pad": (-22, -9), "vocal": (-16, -6), "drums": (-16, -6), "mix": (-18, -8), "bass": (-35, -14)},
     "fixes": [("mixer_eq", "mid", -3.0, "cut around 2.5–4 kHz (set the mid band there)"),
               ("plugin_param", ["cutoff", "filter", "滤波"], "-", "close the filter a little")]},
    {"id": "muddy", "problem": "high", "metric": "bands_db.low_mid",
     "words": ["浑", "浑浊", "糊", "脏", "低中频太多", "muddy", "boxy", "cloudy", "murky"],
     "meaning": "too much 250–500 Hz",
     "ranges": {"lead": (-16, -6), "pad": (-14, -5), "vocal": (-14, -6), "bass": (-14, -4), "drums": (-16, -6), "mix": (-12, -6)},
     "fixes": [("mixer_eq", "mid", -3.0, "cut around 250–400 Hz (set the mid band there)"),
               ("mixer_eq", "low", -2.0, "or reduce the low band if the whole low end is heavy")]},
    {"id": "boomy", "problem": "high", "metric": "bands_db.bass",
     "words": ["轰", "低频太多", "太肥", "嗡", "boomy", "bloated", "too much bass", "woolly"],
     "meaning": "too much 60–250 Hz",
     "ranges": {"lead": (-20, -6), "pad": (-16, -4), "vocal": (-18, -6), "bass": (-4, -0.5), "drums": (-8, -1), "mix": (-7, -2)},
     "fixes": [("mixer_eq", "low", -3.0, "reduce the low band / high-pass non-bass parts")]},
    {"id": "thin", "problem": "low", "metric": "bands_db.bass",
     "words": ["薄", "单薄", "没厚度", "没低频", "不够厚", "thin", "weak low end", "lacks body", "no bottom"],
     "meaning": "too little 60–250 Hz (body)",
     "ranges": {"lead": (-24, -8), "pad": (-18, -5), "vocal": (-22, -8), "bass": (-5, -0.5), "drums": (-10, -1), "mix": (-9, -2)},
     "fixes": [("mixer_eq", "low", +2.5, "boost the low band around 100–200 Hz"),
               ("advice", None, None, "layer a sub/octave-down, or a fuller waveform")]},
    {"id": "narrow", "problem": "low", "metric": "stereo.side_to_mid_db",
     "words": ["窄", "不够宽", "没立体感", "太集中", "narrow", "mono", "not wide", "lacks width"],
     "meaning": "little stereo difference between left and right",
     "ranges": {"lead": (-18, -6), "pad": (-12, -3), "pluck": (-15, -5), "mix": (-15, -6), "bass": (-60, -20), "drums": (-25, -8), "vocal": (-30, -12)},
     "fixes": [("plugin_param", ["width", "stereo", "spread", "unison", "detune", "宽"], "+", "more unison spread / stereo width"),
               ("advice", None, None, "chorus or a short stereo delay; keep bass and kick mono")]},
    {"id": "weak", "problem": "low", "metric": "loudness.crest_db",
     "words": ["没力", "不够冲", "软", "没冲击力", "不punch", "weak", "no punch", "lacks punch", "soft", "flat"],
     "meaning": "transients don't stand out (low crest factor) — often over-compressed or slow attack",
     "ranges": {"drums": (12, 22), "mix": (8, 16), "bass": (8, 16), "lead": (9, 18), "pad": (6, 14), "vocal": (8, 16)},
     "fixes": [("plugin_param", ["attack"], "-", "faster amp attack on the instrument"),
               ("advice", None, None, "less compression / slower compressor attack; transient shaper")]},
    {"id": "quiet", "problem": "low", "metric": "loudness.integrated_lufs",
     "words": ["太小声", "听不见", "被盖住", "不突出", "quiet", "buried", "too soft", "can't hear", "not upfront"],
     "meaning": "low loudness (or masked by other parts)",
     "ranges": {"lead": (-24, -12), "pad": (-30, -16), "bass": (-24, -12), "drums": (-22, -10), "vocal": (-22, -12), "mix": (-16, -8)},
     "fixes": [("mixer_volume", None, +2.0, "raise the mixer track by ~2 dB"),
               ("mixer_eq", "high", +2.0, "presence lift helps it cut through without getting louder")]},
    {"id": "loud", "problem": "high", "metric": "loudness.integrated_lufs",
     "words": ["太大声", "太响", "太冲", "too loud", "overpowering", "sticks out"],
     "meaning": "high loudness relative to its role",
     "ranges": {"lead": (-24, -12), "pad": (-30, -16), "bass": (-24, -12), "drums": (-22, -10), "vocal": (-22, -12), "mix": (-16, -8)},
     "fixes": [("mixer_volume", None, -2.0, "lower the mixer track by ~2 dB")]},
]
BY_ID = {d["id"]: d for d in DESCRIPTORS}


def match_descriptors(text):
    """Descriptor ids mentioned in free text (Chinese or English). Longest matches first, so '太亮' beats '亮'."""
    text = (text or "").lower()
    hits = []
    for d in DESCRIPTORS:
        for w in sorted(d["words"], key=len, reverse=True):
            if w.lower() in text:
                hits.append((len(w), d["id"]))
                break
    hits.sort(reverse=True)
    out = []
    for _, i in hits:
        # "亮" alone is ambiguous: in "太亮" it means harsh; keep the longer, more specific reading
        if i == "dull" and "harsh" in out:
            continue
        if i not in out:
            out.append(i)
    return out


def guess_role(name="", plugin=""):
    s = f"{name} {plugin}".lower()
    for role, keys in (("drums", ["kick", "snare", "clap", "hat", "drum", "perc", "808 k", "鼓"]),
                       ("bass", ["bass", "sub", "808", "贝斯"]), ("pad", ["pad", "string", "铺底"]),
                       ("pluck", ["pluck", "arp", "key", "piano", "钢琴"]), ("vocal", ["vox", "vocal", "人声"]),
                       ("lead", ["lead", "synth", "主音"])):
        if any(k in s for k in keys):
            return role
    return "lead"


def diagnose(target, descriptor_ids, role="lead", reference=None, mix=None):
    """Judge each descriptor for the `target` metrics. Optional `reference` (metrics of a reference recording)
    and `mix` (metrics of the full mix the target sits in)."""
    findings = []
    for did in descriptor_ids:
        d = BY_ID[did]
        value = _g(target, d["metric"])
        f = {"descriptor": did, "meaning": d["meaning"], "metric": d["metric"], "measured": value}
        if reference is not None:
            ref = _g(reference, d["metric"])
            diff = value - ref
            f.update(basis="reference recording", reference=ref, difference=round(diff, 1))
            problem = diff < -2.5 if d["problem"] == "low" else diff > 2.5
            f["severity"] = round(abs(diff), 1)
        else:
            lo, hi = d["ranges"].get(role, d["ranges"].get("mix"))
            f.update(basis=f"heuristic range for a {role}", typical_range=[lo, hi])
            problem = value < lo if d["problem"] == "low" else value > hi
            edge = lo if d["problem"] == "low" else hi
            f["severity"] = round(abs(value - edge), 1) if problem else 0.0
            if not problem:
                near = (value - lo) < 2 if d["problem"] == "low" else (hi - value) < 2
                f["borderline"] = bool(near)
        f["verdict"] = "confirmed" if problem else ("borderline" if f.get("borderline") else "not supported by the measurement")
        if mix is not None and did in ("dull", "harsh", "muddy", "boomy", "thin"):
            band = {"dull": "high_freq_db", "harsh": "bands_db.high_mid", "muddy": "bands_db.low_mid",
                    "boomy": "bands_db.bass", "thin": "bands_db.bass"}[did]
            f["vs_mix"] = round(_g(target, band) - _g(mix, band), 1)
        f["fixes"] = [{"type": t, "target": tgt, "amount": amt, "how": how} for t, tgt, amt, how in d["fixes"]]
        findings.append(f)
    findings.sort(key=lambda f: (f["verdict"] != "confirmed", -f.get("severity", 0)))
    return findings


def auto_findings(target, role="lead", reference=None):
    """Everything that looks off, for 'what's wrong with this?' with no specific complaint."""
    ids = [d["id"] for d in DESCRIPTORS if d["id"] not in ("loud",)]
    return [f for f in diagnose(target, ids, role, reference) if f["verdict"] == "confirmed"]


def compare_bands(target, reference):
    """Band-by-band difference to a reference, in dB (positive = target has more)."""
    return {b: round(target["bands_db"][b] - reference["bands_db"][b], 1) for b in target["bands_db"]}
