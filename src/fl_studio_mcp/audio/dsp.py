"""Signal-processing building blocks (numpy + scipy only)."""

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy import fft as sfft
from scipy import signal

EPS = 1e-10

# Named frequency bands used throughout (Hz).
BANDS = [
    ("sub", 20, 60), ("bass", 60, 250), ("low_mid", 250, 500), ("mid", 500, 2000),
    ("high_mid", 2000, 4000), ("presence", 4000, 6000), ("air", 6000, 20000),
]


from ..errors import AudioError  # noqa: E402,F401  (re-exported)


# ---------------------------------------------------------------- I/O

def load_audio(path, sr=None, mono=False, start=None, end=None):
    """Load an audio file → (float32 array [frames, channels], sr). WAV natively; other formats need ffmpeg."""
    path = Path(path)
    if not path.exists():
        raise AudioError(f"file not found: {path}")
    if path.suffix.lower() == ".wav":
        from scipy.io import wavfile
        file_sr, data = wavfile.read(str(path))
        data = _to_float(data)
    else:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise AudioError(f"{path.suffix} files need ffmpeg on PATH (or convert to WAV first)")
        file_sr = sr or 44100
        proc = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-ar", str(file_sr), "-"],
                              capture_output=True, check=False)
        if proc.returncode != 0:
            raise AudioError(f"ffmpeg failed: {proc.stderr.decode(errors='replace')[:300]}")
        data = np.frombuffer(proc.stdout, dtype=np.float32).reshape(-1, 2)
    if data.ndim == 1:
        data = data[:, None]
    if start is not None or end is not None:
        a = int((start or 0) * file_sr)
        b = int(end * file_sr) if end is not None else len(data)
        data = data[a:b]
    if sr and sr != file_sr:
        data = resample(data, file_sr, sr)
        file_sr = sr
    if mono:
        data = data.mean(axis=1, keepdims=True)
    return data.astype(np.float32), file_sr


def save_wav(path, audio, sr):
    from scipy.io import wavfile
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wavfile.write(str(path), sr, np.clip(audio, -1, 1).astype(np.float32))


def _to_float(data):
    if data.dtype == np.int16:
        return data.astype(np.float32) / 32768.0
    if data.dtype == np.int32:
        return data.astype(np.float32) / 2147483648.0
    if data.dtype == np.uint8:
        return (data.astype(np.float32) - 128) / 128.0
    return data.astype(np.float32)


def resample(x, sr_in, sr_out):
    if sr_in == sr_out:
        return x
    g = np.gcd(int(sr_in), int(sr_out))
    return signal.resample_poly(x, sr_out // g, sr_in // g, axis=0).astype(np.float32)


def to_mono(x):
    return x.mean(axis=1) if x.ndim == 2 else x


# ---------------------------------------------------------------- spectra

def stft_mag(x, n_fft=2048, hop=512):
    """Magnitude STFT of a mono signal → [bins, frames]."""
    x = to_mono(x)
    if len(x) < n_fft:
        x = np.pad(x, (0, n_fft - len(x)))
    _, _, z = signal.stft(x, nperseg=n_fft, noverlap=n_fft - hop, window="hann", boundary=None, padded=True)
    return np.abs(z) * (n_fft / 2)


def fft_freqs(sr, n_fft):
    return np.linspace(0, sr / 2, n_fft // 2 + 1)


def _hz_to_mel(f):
    return 2595.0 * np.log10(1.0 + np.asarray(f) / 700.0)


def _mel_to_hz(m):
    return 700.0 * (10 ** (np.asarray(m) / 2595.0) - 1.0)


@lru_cache(maxsize=16)
def mel_filterbank(sr, n_fft, n_mels=64, fmin=30.0, fmax=None):
    fmax = fmax or sr / 2
    freqs = fft_freqs(sr, n_fft)
    edges = _mel_to_hz(np.linspace(_hz_to_mel(fmin), _hz_to_mel(fmax), n_mels + 2))
    fb = np.zeros((n_mels, len(freqs)))
    for i in range(n_mels):
        lo, c, hi = edges[i], edges[i + 1], edges[i + 2]
        up = (freqs - lo) / max(c - lo, EPS)
        down = (hi - freqs) / max(hi - c, EPS)
        fb[i] = np.maximum(0, np.minimum(up, down))
        fb[i] *= 2.0 / max(hi - lo, EPS)  # area normalization
    return fb


def log_mel(mag, sr, n_fft, n_mels=64):
    return 10 * np.log10(mel_filterbank(sr, n_fft, n_mels) @ (mag ** 2) + EPS)


def mfcc(mag, sr, n_fft, n_mfcc=13, n_mels=40):
    return sfft.dct(log_mel(mag, sr, n_fft, n_mels), axis=0, norm="ortho")[:n_mfcc]


@lru_cache(maxsize=16)
def chroma_filterbank(sr, n_fft, fmin=55.0, fmax=5000.0):
    """Map FFT bins to 12 pitch classes with a Gaussian around each semitone (0 = C)."""
    freqs = fft_freqs(sr, n_fft)
    fb = np.zeros((12, len(freqs)))
    valid = (freqs >= fmin) & (freqs <= fmax)
    midi = 69 + 12 * np.log2(np.maximum(freqs[valid], EPS) / 440.0)
    for pc in range(12):
        d = ((midi - pc) % 12)
        d = np.minimum(d, 12 - d)
        fb[pc, valid] = np.exp(-0.5 * (d / 0.35) ** 2)
    return fb


def chroma(mag, sr, n_fft, fmin=55.0, fmax=5000.0):
    # power (not log) so fundamentals outweigh overtones: a saw's 3rd harmonic is a fifth up, and log
    # compression makes C–E–G look like G–B–D (measured: 96% vs 74% chord accuracy on synthetic material)
    c = chroma_filterbank(sr, n_fft, fmin, fmax) @ (mag ** 2)
    return c / (np.linalg.norm(c, axis=0, keepdims=True) + EPS)


def onset_envelope(mag, sr, n_fft, fmin=30.0, fmax=None, n_mels=64):
    """Spectral flux on log-mel, optionally restricted to a frequency range."""
    fb = mel_filterbank(sr, n_fft, n_mels)
    if fmin > 30.0 or fmax:
        centers = _mel_to_hz(np.linspace(_hz_to_mel(30.0), _hz_to_mel(sr / 2), n_mels + 2))[1:-1]
        keep = (centers >= fmin) & (centers <= (fmax or sr / 2))
        fb = fb[keep]
    lm = 10 * np.log10(fb @ (mag ** 2) + EPS)
    flux = np.maximum(0, np.diff(lm, axis=1)).mean(axis=0)
    flux = np.concatenate([[0], flux])
    flux -= signal.medfilt(flux, 15) if len(flux) >= 15 else 0
    return np.maximum(flux, 0)


# ---------------------------------------------------------------- descriptors

def band_energies_db(mag, sr, n_fft):
    """Energy per named band in dB relative to total (so a balanced mix reads as its shape, not level)."""
    freqs = fft_freqs(sr, n_fft)
    power = (mag ** 2).mean(axis=1)
    total = power.sum() + EPS
    out = {}
    for name, lo, hi in BANDS:
        sel = (freqs >= lo) & (freqs < hi)
        out[name] = round(float(10 * np.log10(power[sel].sum() / total + EPS)), 1)
    return out


def spectral_stats(mag, sr, n_fft):
    freqs = fft_freqs(sr, n_fft)[:, None]
    p = mag ** 2
    w = p.sum(axis=0) + EPS
    loud = w > w.max() * 1e-3  # ignore near-silent frames
    if not loud.any():
        loud = np.ones_like(w, dtype=bool)
    centroid = (freqs * p).sum(axis=0) / w
    cum = np.cumsum(p, axis=0) / w
    rolloff = freqs[np.argmax(cum >= 0.85, axis=0), 0]
    flatness = np.exp(np.mean(np.log(mag + EPS), axis=0)) / (mag.mean(axis=0) + EPS)
    return {
        "centroid_hz": round(float(np.median(centroid[loud])), 1),
        "rolloff85_hz": round(float(np.median(rolloff[loud])), 1),
        "flatness": round(float(np.median(flatness[loud])), 4),
    }


def _k_weighting(sr):
    """BS.1770 K-weighting as two biquads, designed for any sample rate."""
    # High-shelf (pre-filter)
    f0, g, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = np.tan(np.pi * f0 / sr)
    vh = 10 ** (g / 20)
    vb = vh ** 0.4996667741545416
    a0 = 1 + k / q + k * k
    b1 = [(vh + vb * k / q + k * k) / a0, 2 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0]
    a1 = [1, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0]
    # High-pass (RLB)
    f0, q = 38.13547087602444, 0.5003270373238773
    k = np.tan(np.pi * f0 / sr)
    a0 = 1 + k / q + k * k
    b2 = [1, -2, 1]
    a2 = [1, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0]
    return (b1, a1), (b2, a2)


def loudness(x, sr):
    """Integrated loudness (LUFS, BS.1770 gating), short-term max, sample peak, crest factor."""
    x = x if x.ndim == 2 else x[:, None]
    (b1, a1), (b2, a2) = _k_weighting(sr)
    y = signal.lfilter(b2, a2, signal.lfilter(b1, a1, x, axis=0), axis=0)
    block, step = int(0.4 * sr), int(0.1 * sr)
    if len(y) < block:
        y = np.pad(y, ((0, block - len(y)), (0, 0)))
    starts = range(0, len(y) - block + 1, step)
    ms = np.array([np.mean(y[s:s + block] ** 2, axis=0).sum() for s in starts])
    lk = -0.691 + 10 * np.log10(ms + EPS)
    gated = ms[lk > -70]
    if len(gated) == 0:
        integrated = -70.0
    else:
        rel = -0.691 + 10 * np.log10(gated.mean() + EPS) - 10
        g2 = ms[(lk > -70) & (lk > rel)]
        integrated = float(-0.691 + 10 * np.log10(g2.mean() + EPS)) if len(g2) else -70.0
    st_block = int(3 * sr)
    st = [np.mean(y[s:s + st_block] ** 2, axis=0).sum() for s in range(0, max(1, len(y) - st_block + 1), step * 5)]
    peak = float(np.abs(x).max())
    rms = float(np.sqrt(np.mean(x ** 2)) + EPS)
    return {
        "integrated_lufs": round(integrated, 1),
        "short_term_max_lufs": round(float(-0.691 + 10 * np.log10(max(st) + EPS)), 1),
        "peak_dbfs": round(20 * np.log10(peak + EPS), 1),
        "crest_db": round(20 * np.log10((peak + EPS) / rms), 1),
    }


def stereo_stats(x):
    if x.ndim != 2 or x.shape[1] < 2:
        return {"correlation": 1.0, "side_to_mid_db": -100.0}
    l, r = x[:, 0], x[:, 1]
    mid, side = (l + r) / 2, (l - r) / 2
    corr = float(np.sum(l * r) / (np.sqrt(np.sum(l * l) * np.sum(r * r)) + EPS))
    return {"correlation": round(corr, 3),
            "side_to_mid_db": round(float(10 * np.log10((np.mean(side ** 2) + EPS) / (np.mean(mid ** 2) + EPS))), 1)}


def envelope_db(x, sr, hop_s=0.005):
    x = to_mono(x)
    hop = max(1, int(hop_s * sr))
    n = len(x) // hop
    if n == 0:
        return np.array([-100.0])
    frames = x[:n * hop].reshape(n, hop)
    return 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + EPS)


def trim_to_onset(x, sr, threshold_db=-40.0, pre_s=0.005):
    """Drop leading silence (relative to the peak) — used to align recordings with references."""
    env = envelope_db(x, sr)
    above = np.nonzero(env > env.max() + threshold_db)[0]
    if len(above) == 0:
        return x
    start = max(0, int(above[0] * 0.005 * sr - pre_s * sr))
    return x[start:]


def yin_f0(x, sr, fmin=40.0, fmax=1500.0, threshold=0.15):
    """Median fundamental frequency of a (mostly) monophonic sound via YIN, or None."""
    x = to_mono(x)
    frame, hop = 2048, 512
    if len(x) < frame * 2:
        x = np.pad(x, (0, frame * 2 - len(x)))
    tau_min, tau_max = int(sr / fmax), min(int(sr / fmin), frame - 1)
    f0s = []
    for s in range(0, len(x) - frame * 2, hop):
        seg = x[s:s + frame * 2]
        if np.sqrt(np.mean(seg ** 2)) < 1e-3:
            continue
        d = np.array([np.sum((seg[:frame] - seg[t:t + frame]) ** 2) for t in range(tau_max + 1)])
        cmnd = d[1:] * np.arange(1, tau_max + 1) / (np.cumsum(d[1:]) + EPS)
        cmnd = np.concatenate([[1], cmnd])
        cand = np.nonzero(cmnd[tau_min:] < threshold)[0]
        if len(cand) == 0:
            continue
        t = cand[0] + tau_min
        while t + 1 <= tau_max and cmnd[t + 1] < cmnd[t]:
            t += 1
        if 1 <= t < tau_max:
            a, b, c = cmnd[t - 1], cmnd[t], cmnd[t + 1]
            t = t + 0.5 * (a - c) / (a - 2 * b + c + EPS)
        f0s.append(sr / t)
    return float(np.median(f0s)) if f0s else None


def hz_to_midi(f):
    return 69 + 12 * np.log2(f / 440.0)
