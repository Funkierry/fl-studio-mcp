"""
Describe a single sound (one note / one-shot) and measure how far apart two sounds are.

The descriptor is level-invariant (everything relative to the sound's own peak), so a quieter render of the same
patch still matches. Distance combines spectral shape (body and tail), amplitude envelope and brightness over time.
"""

import numpy as np

from . import dsp

SR = 44100
N_FFT, HOP = 2048, 256
# envelope sample times (s after onset), log-spaced to resolve attacks and long tails alike
ENV_TIMES = np.array([0.005, 0.01, 0.02, 0.04, 0.07, 0.1, 0.15, 0.25, 0.4, 0.6, 0.9, 1.3, 1.8])
FLOOR_DB = -60.0


def describe(audio, sr, max_len=2.0):
    x = dsp.to_mono(audio)
    if sr != SR:
        x = dsp.resample(x, sr, SR)
    x = dsp.trim_to_onset(x, SR)[: int(max_len * SR)]
    if len(x) < SR // 20 or np.abs(x).max() < 1e-4:
        raise dsp.AudioError("no audible sound to describe")
    x = x / (np.abs(x).max() + dsp.EPS)

    env = dsp.envelope_db(x, SR, 0.005)
    env -= env.max()
    t_env = np.arange(len(env)) * 0.005
    env_curve = np.interp(ENV_TIMES, t_env, env, right=FLOOR_DB)
    peak_i = int(np.argmax(env))
    above = np.nonzero(env[:peak_i + 1] > -20)[0]
    attack_s = float((peak_i - (above[0] if len(above) else 0)) * 0.005)
    after = env[peak_i:]
    decay_i = np.nonzero(after < -20)[0]
    decay_s = float(decay_i[0] * 0.005) if len(decay_i) else float(len(after) * 0.005)

    mag = dsp.stft_mag(x, N_FFT, HOP)
    lm = dsp.log_mel(mag, SR, N_FFT, 64)
    frame_t = np.arange(lm.shape[1]) * HOP / SR
    peak_db = lm.max()

    def profile(t0, t1):
        sel = (frame_t >= t0) & (frame_t < t1)
        if not sel.any():
            return np.full(lm.shape[0], FLOOR_DB)
        return np.maximum(lm[:, sel].mean(axis=1) - peak_db, FLOOR_DB)

    freqs = dsp.fft_freqs(SR, N_FFT)[:, None]
    p = mag ** 2
    cen = ((freqs * p).sum(axis=0) / (p.sum(axis=0) + dsp.EPS))
    cen_times = np.array([0.02, 0.1, 0.3, 0.7])
    cen_curve = np.interp(cen_times, frame_t, cen)
    f0 = dsp.yin_f0(x[: int(0.8 * SR)], SR)
    stats = dsp.spectral_stats(mag[:, : max(1, int(0.5 * SR / HOP))], SR, N_FFT)

    return {
        "f0_hz": round(f0, 2) if f0 else None,
        "midi_note": int(round(dsp.hz_to_midi(f0))) if f0 else None,
        "attack_s": round(attack_s, 3),
        "decay_to_-20db_s": round(decay_s, 3),
        "centroid_hz": stats["centroid_hz"],
        "brightness_ratio": round(stats["centroid_hz"] / f0, 2) if f0 else None,
        "noisiness": stats["flatness"],
        "bands_db": dsp.band_energies_db(mag, SR, N_FFT),
        "_env": env_curve.tolist(),
        "_body": profile(0.02, 0.3).tolist(),
        "_tail": profile(0.3, 1.0).tolist(),
        "_centroid": np.log2(np.maximum(cen_curve, 20.0)).tolist(),
    }


def public(desc):
    """The descriptor without the internal curves (for showing to the assistant)."""
    return {k: v for k, v in desc.items() if not k.startswith("_")}


def distance(a, b):
    body = float(np.mean(np.abs(np.array(a["_body"]) - np.array(b["_body"]))))
    tail = float(np.mean(np.abs(np.array(a["_tail"]) - np.array(b["_tail"]))))
    env = float(np.mean(np.abs(np.array(a["_env"]) - np.array(b["_env"]))))
    cen = float(np.mean(np.abs(np.array(a["_centroid"]) - np.array(b["_centroid"]))))
    parts = {"spectrum_body_db": round(body, 2), "spectrum_tail_db": round(tail, 2),
             "envelope_db": round(env, 2), "brightness_octaves": round(cen, 3)}
    total = body + 0.5 * tail + 0.6 * env + 8.0 * cen
    return round(total, 3), parts


def compare(target, current):
    """Plain-language differences of `current` relative to `target`."""
    notes = []
    dc = np.mean(np.array(current["_centroid"]) - np.array(target["_centroid"]))
    if abs(dc) > 0.15:
        notes.append(f"{'brighter' if dc > 0 else 'darker'} than the target by {abs(dc):.1f} octaves of spectral centroid")
    da = current["attack_s"] - target["attack_s"]
    if abs(da) > 0.01 + 0.3 * target["attack_s"]:
        notes.append(f"attack {'slower' if da > 0 else 'faster'} ({current['attack_s']*1000:.0f} ms vs {target['attack_s']*1000:.0f} ms)")
    dd = current["decay_to_-20db_s"] - target["decay_to_-20db_s"]
    if abs(dd) > 0.05 + 0.3 * target["decay_to_-20db_s"]:
        notes.append(f"{'longer' if dd > 0 else 'shorter'} decay ({current['decay_to_-20db_s']:.2f} s vs {target['decay_to_-20db_s']:.2f} s)")
    dn = current["noisiness"] - target["noisiness"]
    if abs(dn) > 0.05:
        notes.append(f"{'noisier' if dn > 0 else 'cleaner/more tonal'} than the target")
    for band in ("sub", "bass", "low_mid", "mid", "high_mid", "presence", "air"):
        d = current["bands_db"][band] - target["bands_db"][band]
        if abs(d) >= 6:
            notes.append(f"{band} band {d:+.0f} dB relative to the target")
    return notes or ["very close to the target"]
