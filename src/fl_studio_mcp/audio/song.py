"""
Whole-song analysis: tempo, beats, downbeats, key, chords, sections, drum grooves and bass lines.

Pipeline: STFT → harmonic/percussive split (median filtering) → onset envelope → tempo (autocorrelation with a
tempo prior) → beat tracking (dynamic programming) → downbeats (kick-weighted phase) → beat-synchronous chroma
→ key (Krumhansl profiles) and chords (templates + Viterbi) → bar features → self-similarity novelty → sections
→ per-section drum steps (percussive band onsets on a 16th grid) and bass notes (low-band pitch salience).
Assumes 4/4.
"""

import warnings

import numpy as np
from scipy import ndimage, signal

from . import dsp

SR = 22050
N_FFT, HOP = 2048, 512
N_FFT_LOW = 8192
FPS = SR / HOP
# STFT frames are not centered (frame t spans [t*HOP, t*HOP + N_FFT)); onsets register about half a window early
TIME_OFFSET = N_FFT / 2 / SR

PITCH_CLASSES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
KK_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KK_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
CHORD_TYPES = {"": [0, 4, 7], "m": [0, 3, 7]}
DRUM_BANDS = {"kick": (30, 130), "snare": (180, 2500), "hat": (6000, 11000)}


# ---------------------------------------------------------------- helpers

def hpss(mag, kernel=17):
    h = ndimage.median_filter(mag, size=(1, kernel))
    p = ndimage.median_filter(mag, size=(kernel, 1))
    h2, p2 = h ** 2, p ** 2
    total = h2 + p2 + dsp.EPS
    return mag * (h2 / total), mag * (p2 / total)


def deleak(bands):
    """Subtract cross-band leakage (kick click in the snare band, snare body in the kick band, hat noise below its
    cutoff). Leak ratios are estimated where the source band peaks, using a low percentile so that genuinely
    simultaneous hits don't inflate them."""
    def clean(target, source):
        peaks = source > np.percentile(source, 97)
        if peaks.sum() < 5:
            return target
        ratio = float(np.percentile(target[peaks] / (source[peaks] + dsp.EPS), 30))
        return np.maximum(0, target - min(ratio, 1.0) * source)

    # snare first (it picks up both neighbours), then clean kick and hat against the *cleaned* snare —
    # the raw snare band contains the kick itself, and subtracting it would erase real kicks
    snare = clean(clean(bands["snare"], bands["kick"]), bands["hat"])
    kick = clean(bands["kick"], snare)
    hat = clean(bands["hat"], snare)
    return {"kick": _normalize(kick), "snare": _normalize(snare), "hat": _normalize(hat)}


def _normalize(x):
    x = x - x.min()
    return x / (np.percentile(x, 99) + dsp.EPS)


def estimate_tempo(oenv, bpm_min=60, bpm_max=200, prior_bpm=120.0):
    x = ndimage.gaussian_filter1d(oenv, 1.5)  # widen onset peaks so fractional lags aren't missed
    x = x - x.mean()
    ac = signal.fftconvolve(x, x[::-1], mode="full")[len(x) - 1:]
    ac /= ac[0] + dsp.EPS
    lags = np.arange(len(ac))
    with np.errstate(divide="ignore"):
        bpm = 60 * FPS / lags
    ok = (bpm >= bpm_min) & (bpm <= bpm_max)
    prior = np.exp(-0.5 * (np.log2(bpm[ok] / prior_bpm) / 1.0) ** 2)
    score = ac[ok] * prior
    i = int(np.argmax(score))
    lag = float(lags[ok][i])

    def ac_at(l):
        j = int(round(l))
        return float(ac[max(0, j - 1):j + 2].max()) if 0 < j < len(ac) - 1 else 0.0

    # metrical-level check: a slow pulse whose half-period is also strongly periodic is really the faster tempo
    if 60 * FPS / (lag / 2) <= bpm_max * 1.0 and 60 * FPS / lag < 78 and ac_at(lag / 2) > 0.45 * ac_at(lag):
        lag /= 2
    j = int(round(lag))
    if 1 <= j < len(ac) - 1:  # parabolic refinement
        a, b, c = ac[j - 1], ac[j], ac[j + 1]
        lag = j + 0.5 * (a - c) / (a - 2 * b + c + dsp.EPS)
    tempo = 60 * FPS / lag
    sorted_scores = np.sort(score)
    confidence = float(1 - sorted_scores[-2] / (sorted_scores[-1] + dsp.EPS)) if len(score) > 1 else 0.0
    return float(tempo), confidence


def track_beats(oenv, tempo, tightness=100.0):
    period = 60 * FPS / tempo
    win = np.arange(-int(period), int(period) + 1)
    local = np.convolve(oenv, np.exp(-0.5 * (win * 32.0 / period) ** 2), mode="same")
    n = len(local)
    cum = np.zeros(n)
    back = -np.ones(n, dtype=int)
    lo, hi = int(round(period / 2)), int(round(2 * period))
    offsets = np.arange(lo, hi + 1)
    penalty = -tightness * np.log(offsets / period) ** 2
    for t in range(n):
        prev = t - offsets
        valid = prev >= 0
        if valid.any():
            cand = cum[prev[valid]] + penalty[valid]
            j = int(np.argmax(cand))
            cum[t] = local[t] + max(cand[j], 0)
            back[t] = prev[valid][j] if cand[j] > 0 else -1
        else:
            cum[t] = local[t]
    # end on the strongest beat within the last period
    tail = max(0, n - int(period))
    t = tail + int(np.argmax(cum[tail:]))
    beats = []
    while t >= 0:
        beats.append(t)
        t = back[t]
    beats = np.array(beats[::-1])
    # trim weak beats at the edges (silence before/after the music)
    strong = local[beats] > 0.03 * np.percentile(local[beats], 95)
    if strong.any():
        first, last = np.argmax(strong), len(strong) - np.argmax(strong[::-1])
        beats = beats[first:last]
    return beats


def extend_beats(beats, tempo, mag, silence_db=-50.0):
    """Extrapolate the beat grid over quiet stretches with no clear onsets (e.g. a pad-only intro or outro)."""
    period = 60 * FPS / tempo
    level = 20 * np.log10(np.sqrt((mag ** 2).mean(axis=0)) + dsp.EPS)
    audible = np.nonzero(level > level.max() + silence_db)[0]
    if len(audible) == 0:
        return beats
    first, last = audible[0], audible[-1]
    head = []
    t = beats[0] - period
    while t >= first - period * 0.6:  # frame times run early, so a beat at t=0 can land slightly before frame 0
        head.append(int(round(max(t, 0))))
        t -= period
    tail = []
    t = beats[-1] + period
    while t <= last:
        tail.append(int(round(t)))
        t += period
    return np.array(head[::-1] + list(beats) + tail)


def _sync(feat, frames):
    """Average feature columns between consecutive frame indices."""
    out = []
    bounds = list(frames) + [feat.shape[1]]
    for a, b in zip(bounds[:-1], bounds[1:]):
        b = max(b, a + 1)
        out.append(feat[:, a:b].mean(axis=1))
    return np.array(out).T


def estimate_key(chroma_vec, cadence_roots=None):
    """Krumhansl key estimate. `cadence_roots` (chord/bass roots at section starts and the ending) breaks the tie
    between relative major/minor keys, which share all their notes (e.g. Am–F–C–G)."""
    best = []
    for mode, prof in (("major", KK_MAJOR), ("minor", KK_MINOR)):
        for tonic in range(12):
            r = np.corrcoef(chroma_vec, np.roll(prof, tonic))[0, 1]
            best.append((r, tonic, mode))
    best.sort(reverse=True)
    r, tonic, mode = best[0]
    note = None
    if cadence_roots:
        rel_tonic, rel_mode = ((tonic + 9) % 12, "minor") if mode == "major" else ((tonic + 3) % 12, "major")
        rel_r = next(x[0] for x in best if x[1] == rel_tonic and x[2] == rel_mode)
        votes = {pc: cadence_roots.count(pc) for pc in (tonic, rel_tonic)}
        if r - rel_r < 0.15 and votes[rel_tonic] > votes[tonic]:
            note = f"relative {PITCH_CLASSES[tonic]} {mode} also fits; phrases start/end on {PITCH_CLASSES[rel_tonic]}"
            r, tonic, mode = rel_r, rel_tonic, rel_mode
    second = next(x[0] for x in best if (x[1], x[2]) != (tonic, mode))
    out = {"tonic": PITCH_CLASSES[tonic], "mode": mode, "name": f"{PITCH_CLASSES[tonic]} {mode}",
           "confidence": round(float(r - second), 3), "tonic_pc": tonic}
    if note:
        out["note"] = note
    return out


def _chord_templates():
    names, temps, roots = [], [], []
    for root in range(12):
        for suffix, ivs in CHORD_TYPES.items():
            t = np.zeros(12)
            t[[(root + i) % 12 for i in ivs]] = [1.0, 0.8, 0.9]
            names.append(PITCH_CLASSES[root] + suffix)
            temps.append(t / np.linalg.norm(t))
            roots.append(root)
    return names, np.array(temps), np.array(roots)


def recognize_chords(beat_chroma, beat_bass, stay=0.7):
    """Per-beat chord labels with Viterbi smoothing. Bass pitch class nudges the root."""
    names, temps, roots = _chord_templates()
    c = beat_chroma / (np.linalg.norm(beat_chroma, axis=0, keepdims=True) + dsp.EPS)
    b = beat_bass / (beat_bass.max(axis=0, keepdims=True) + dsp.EPS)
    emis = temps @ c + 0.35 * b[roots]
    k, n = emis.shape
    log_e = emis * 8.0
    trans = np.full((k, k), np.log((1 - stay) / (k - 1)))
    np.fill_diagonal(trans, np.log(stay))
    score = log_e[:, 0].copy()
    back = np.zeros((k, n), dtype=int)
    for t in range(1, n):
        m = score[:, None] + trans
        back[:, t] = np.argmax(m, axis=0)
        score = m[back[:, t], np.arange(k)] + log_e[:, t]
    path = [int(np.argmax(score))]
    for t in range(n - 1, 0, -1):
        path.append(back[path[-1], t])
    path = path[::-1]
    return [names[i] for i in path], [int(roots[i]) for i in path]


def chord_to_notes(name, low=52):
    """Close voicing of a chord symbol (root position, root at or above `low`)."""
    suffix = "m" if name.endswith("m") else ""
    root = PITCH_CLASSES.index(name[:-1] if suffix else name)
    base = low + ((root - low) % 12)
    return [base + i for i in CHORD_TYPES[suffix]]


def novelty(ssm, k=4):
    """Foote checkerboard novelty along the diagonal of a bar-level self-similarity matrix."""
    n = len(ssm)
    g = signal.windows.gaussian(2 * k, std=k / 2)
    kern = np.outer(g, g) * np.kron([[1, -1], [-1, 1]], np.ones((k, k)))
    pad = np.pad(ssm, k, mode="edge")
    return np.array([np.sum(pad[i:i + 2 * k, i:i + 2 * k] * kern) for i in range(n)])


def segment_bars(bar_feats, min_len=4, k=4):
    n = bar_feats.shape[1]
    f = bar_feats / (np.linalg.norm(bar_feats, axis=0, keepdims=True) + dsp.EPS)
    ssm = f.T @ f
    if n < 2 * min_len:
        return [0, n], ssm
    nov = novelty(ssm, k)
    nov[:min_len] = nov[n - min_len + 1:] = 0
    # adaptive peak picking: a local maximum that stands out from its neighbourhood. A single global threshold
    # gets dominated by the big intro/outro changes and misses verse→chorus boundaries.
    floor = 0.15 * nov.max()
    peaks = []
    for i in range(min_len, n - min_len + 1):
        if nov[i] < nov[max(0, i - 2):min(n, i + 3)].max() or nov[i] < floor:
            continue
        if nov[i] > 1.6 * np.median(nov[max(0, i - 8):min(n, i + 9)]):
            peaks.append(i)
    bounds = [0]
    for i in sorted(peaks, key=lambda i: -nov[i]):
        if all(abs(i - b) >= min_len for b in bounds + [n]):
            bounds.append(int(i))
    bounds = sorted(set(bounds + [n]))
    # snap to 4-bar phrases when close
    snapped = [0]
    for b in bounds[1:-1]:
        s = int(round(b / 4) * 4)
        b = s if abs(s - b) <= 1 and 0 < s < n else b
        if b - snapped[-1] >= min_len and n - b >= min_len // 2:
            snapped.append(b)
    return snapped + [n], ssm


def label_segments(bounds, ssm, bar_feats, threshold=0.88):
    segs = list(zip(bounds[:-1], bounds[1:]))
    labels, reps = [], []
    for a, b in segs:
        mean = bar_feats[:, a:b].mean(axis=1)
        best, best_sim = None, -1.0
        for lab, (ra, rb, rmean) in enumerate(reps):
            cos = float(mean @ rmean / (np.linalg.norm(mean) * np.linalg.norm(rmean) + dsp.EPS))
            m = min(b - a, rb - ra)
            diag = float(np.mean([ssm[a + i, ra + i] for i in range(m)]))
            sim = 0.4 * cos + 0.6 * diag
            if sim > best_sim:
                best, best_sim = lab, sim
        if best is not None and best_sim >= threshold:
            labels.append(best)
        else:
            reps.append((a, b, mean))
            labels.append(len(reps) - 1)
    return [chr(ord("A") + l) for l in labels]


def name_sections(sections):
    """Guess conventional section names from repetition, position and energy."""
    by_label = {}
    for s in sections:
        by_label.setdefault(s["label"], []).append(s)
    energy = {lab: np.mean([s["energy_db"] for s in ss]) for lab, ss in by_label.items()}
    repeated = [lab for lab, ss in by_label.items() if len(ss) >= 2]
    names = {}
    if repeated:
        chorus = max(repeated, key=lambda l: energy[l])
        names[chorus] = "Chorus"
        others = sorted((l for l in repeated if l != chorus), key=lambda l: -len(by_label[l]))
        if others:
            names[others[0]] = "Verse"
            for i, l in enumerate(others[1:]):
                names[l] = "Pre-Chorus" if np.mean([s["bars"] for s in by_label[l]]) <= 4 else f"Part {chr(ord('C') + i)}"
    first, last = sections[0], sections[-1]
    for s in sections:
        lab = s["label"]
        if lab in names:
            s["name"] = names[lab]
        elif s is first:
            s["name"] = "Intro"
        elif s is last:
            s["name"] = "Outro"
        elif len(by_label[lab]) == 1:
            s["name"] = "Bridge" if energy[lab] < max(energy.values()) - 3 else "Break"
        else:
            s["name"] = f"Section {lab}"
    return sections


def _steps(env_frames, bar_start, bar_end, n_steps=16):
    """Max onset strength around each 16th-note position of a bar."""
    pos = np.linspace(bar_start, bar_end, n_steps + 1)[:-1]
    w = max(1, int((bar_end - bar_start) / n_steps / 3))
    return np.array([env_frames[max(0, int(p) - w):int(p) + w + 1].max(initial=0) for p in pos])


def drum_pattern(bands, bar_frames, bars):
    """One 16-step string per drum band for the given bars (x = hit, X = accent, o = ghost)."""
    out = {}
    for name, env in bands.items():
        hits = np.array([_steps(env, bar_frames[b], bar_frames[b + 1]) for b in bars])
        # relative to this section's strongest hits too: bands leak into each other (kick click in the snare
        # band, hat noise below its cutoff), and leakage is weaker than the real hits
        thr = max(0.3, 0.45 * np.percentile(hits, 95))
        rate = (hits > thr).mean(axis=0)
        strength = np.where(hits > thr, hits, np.nan)
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            avg = np.nan_to_num(np.nanmean(strength, axis=0))
        chars = []
        for r, s in zip(rate, avg):
            if r < 0.5:
                chars.append(".")
            elif s > 0.75:
                chars.append("X")
            elif s < 0.4:
                chars.append("o")
            else:
                chars.append("x")
        pattern = "".join(chars)
        # a single hit on the downbeat and nothing else is almost always a pad/chord attack leaking into the
        # percussive part (drumless intros/outros), not a drum
        if pattern.count(".") == len(pattern) - 1 and pattern[0] != ".":
            pattern = "." * len(pattern)
        out[name] = pattern
    return out


def bass_salience(mag_low, sr, n_fft, midi_lo=28, midi_hi=55):
    freqs = dsp.fft_freqs(sr, n_fft)
    midis = np.arange(midi_lo, midi_hi + 1)
    sal = np.zeros((len(midis), mag_low.shape[1]))
    df = freqs[1] - freqs[0]
    for i, m in enumerate(midis):
        f0 = 440 * 2 ** ((m - 69) / 12)
        partial = []
        for h, w in ((1, 1.0), (2, 0.6), (3, 0.4), (4, 0.25)):
            idx = int(round(f0 * h / df))
            p = mag_low[idx - 1:idx + 2].max(axis=0) if idx < len(freqs) - 1 else np.zeros(mag_low.shape[1])
            partial.append(p)
            sal[i] += w * p
        # the fundamental itself must be present, or the sub-octave steals notes from the real pitch
        sal[i] *= np.minimum(1.0, partial[0] / (0.5 * partial[1] + dsp.EPS))
    return midis, sal


def bass_notes(midis, sal, bass_onset, beat_frames, first_beat, n_beats, threshold):
    """Eighth-note pitch changes and re-attacks, measured separately for each occurrence.

    The 371 ms pitch window cannot reliably resolve sixteenths in a full mix;
    finer grids increased false notes in independent hit/pitch benchmarks.
    """
    slots = []
    for bi in range(first_beat, min(first_beat + n_beats, len(beat_frames) - 1)):
        a, b = beat_frames[bi], beat_frames[bi + 1]
        for half in range(2):
            s = a + (b - a) * half / 2
            e = a + (b - a) * (half + 1) / 2
            seg = sal[:, int(s):max(int(e), int(s) + 1)]
            strength = float(seg.max(axis=0).mean()) if seg.size else 0.0
            slots.append((bi, half, s, e, seg, strength))
    if not slots:
        return []
    # threshold relative to this section as well as the song: quiet verses still have a bass line
    rel = 0.35 * np.percentile([sl[5] for sl in slots], 90)
    reference = max(np.percentile([sl[5] for sl in slots], 95), dsp.EPS)
    peaks, _ = signal.find_peaks(bass_onset, distance=max(1, int(FPS*.10)), prominence=.18)
    notes = []
    for bi, half, s, e, seg, strength in slots:
        t = (bi - first_beat) + half / 2
        if strength < min(threshold, rel) or strength < 0.15 * threshold:
            continue
        pitch = int(midis[int(np.argmax(seg.mean(axis=1)))])
        w = max(1, (e-s)*.4)
        onset = bool(np.any(np.abs(peaks-s) <= w))
        if notes and notes[-1]["pitch"] == pitch and not onset and abs(notes[-1]["start"] + notes[-1]["length"] - t) < 1e-6:
            notes[-1]["length"] += 0.5
        else:
            note_strength = seg.mean(axis=1)
            ranked = np.sort(note_strength)
            confidence = float(np.clip(1-ranked[-2]/(ranked[-1]+dsp.EPS), 0, 1))
            notes.append({"pitch": pitch, "start": t, "length": 0.5,
                          "velocity": round(float(np.clip(.85*np.sqrt(strength/reference), .25, 1)), 3),
                          "confidence": round(confidence, 3)})
    for n in notes:
        n["length"] = round(n["length"] * 0.95, 3)
    return notes


def drum_events(bands, bar_frames, bars, flux_delay=0.):
    """Occurrence-local transients; keep fills, accents and sub-grid timing.

    Thresholds are measured per section, but each onset is exported independently.
    The regular grid is used only to express time, not to average or quantize events.
    """
    out = {}
    for role, env in bands.items():
        strengths = np.concatenate([_steps(env, bar_frames[b], bar_frames[b+1]) for b in bars])
        threshold = max(.3, .45*np.percentile(strengths, 95))
        step_frames = np.median(np.diff(bar_frames))/16
        peaks, properties = signal.find_peaks(np.r_[0., env, 0.], height=threshold,
                                              distance=max(1, int(step_frames*.65)), prominence=threshold*.35)
        peaks = peaks-1
        events = []
        reference = max(float(np.percentile(strengths, 95)), dsp.EPS)
        for bar in bars:
            a, b = bar_frames[bar:bar+2]
            for peak in peaks[(peaks >= a-step_frames*.35) & (peaks < b-step_frames*.35)]:
                # Sub-frame parabolic interpolation of the onset envelope peak.
                delta = 0.
                if 0 < peak < len(env)-1:
                    left, center, right = env[peak-1:peak+2]
                    delta = float(np.clip(.5*(left-right)/(left-2*center+right+dsp.EPS), -.5, .5))
                start = max(0., (bar-bars[0])*4+(peak+delta-flux_delay-a)/(b-a)*4)
                if 0 <= start < len(bars)*4:
                    strength = float(env[peak])
                    events.append({'start': round(start, 4), 'length': .125,
                                   'velocity': round(float(np.clip(.85*np.sqrt(strength/reference), .2, 1)), 3),
                                   'confidence': round(float(np.clip((strength-threshold)/reference, 0, 1)), 3)})
        # Keep the existing conservative rejection of isolated pad attacks.
        if events and len(events) <= len(bars) and all(min(n['start'] % 4, 4-n['start'] % 4) < .15 for n in events):
            events = []
        out[role] = events
    return out


# ---------------------------------------------------------------- main entry

def backbeat_score(beats, bands):
    """How much the beats look like 'kick on 1 & 3, snare on 2 & 4' (either parity). Positive = plausible level."""
    if len(beats) < 8:
        return -1.0
    k, s = bands["kick"][beats], bands["snare"][beats]
    alt = max((k[0::2].mean() - k[1::2].mean()) + (s[1::2].mean() - s[0::2].mean()),
              (k[1::2].mean() - k[0::2].mean()) + (s[0::2].mean() - s[1::2].mean()))
    mids = ((beats[:-1] + beats[1:]) // 2)
    on_beat = (k + s).mean() - (bands["kick"][mids] + bands["snare"][mids]).mean()
    return float(alt + 0.5 * on_beat)


def choose_tempo(oenv, bands, tempo_hint=None):
    """Pick the metrical level (half / as-is / double) whose beat grid has the most convincing backbeat."""
    if tempo_hint:
        cands = [float(tempo_hint)]
    else:
        base, _ = estimate_tempo(oenv)
        cands = [t for t in (base / 2, base, base * 2) if 55 <= t <= 210]
    scored = []
    for t in cands:
        beats = track_beats(oenv, t)
        if len(beats) < 8:
            continue
        refined = float(60 * FPS / np.polyfit(np.arange(len(beats)), beats, 1)[0])
        prior = np.exp(-0.5 * (np.log2(refined / 120.0) / 1.2) ** 2)
        scored.append((backbeat_score(beats, bands) + 0.15 * prior, refined, beats))
    if not scored:
        raise dsp.AudioError("not enough rhythmic content to analyze (need a few bars with a beat)")
    scored.sort(key=lambda s: -s[0])
    best = scored[0]
    margin = best[0] - scored[1][0] if len(scored) > 1 else 1.0
    return best[1], best[2], round(float(margin), 3), sorted(round(s[1], 2) for s in scored[1:])


def analyze_song(audio, sr, tempo_hint=None):
    x = dsp.to_mono(audio)
    if sr != SR:
        x = dsp.resample(x, sr, SR)
    duration = len(x) / SR
    mag = dsp.stft_mag(x, N_FFT, HOP)
    harm, perc = hpss(mag)

    bands = {name: _normalize(dsp.onset_envelope(perc, SR, N_FFT, lo, hi)) for name, (lo, hi) in DRUM_BANDS.items()}
    # equal-weight drum bands + harmonic onsets: hats and snares count as much as the kick
    oenv = _normalize(sum(bands.values()) + 0.5 * _normalize(dsp.onset_envelope(harm, SR, N_FFT)))
    tempo, beats, tempo_conf, tempo_alternatives = choose_tempo(oenv, bands, tempo_hint)
    # Linear spectral flux preserves the level difference between real hits and
    # quiet harmonic leakage; log-mel whitening promoted leakage to full hits.
    frequency = dsp.fft_freqs(SR, N_FFT)
    detail_bands = {}
    for name, (lo, hi) in {'kick':(30,130), 'snare':(1400,6000), 'hat':(6500,11000)}.items():
        spectrum = perc[(frequency >= lo) & (frequency < hi)]
        flux = np.maximum(0, np.diff(spectrum, axis=1, prepend=spectrum[:, :1])).sum(axis=0)
        detail_bands[name] = _normalize(flux)
    detail_bands = deleak(detail_bands)
    bands = deleak(bands)  # keep robust structure/tempo features independent of hit classification
    beats = extend_beats(beats, tempo, mag)

    # downbeat phase: where kicks land and chords change
    # Equal frame centers for both resolutions. Padding on the right alone shifted
    # bass/chord evidence 139 ms earlier than the drum and beat grid.
    pad = (N_FFT_LOW-N_FFT)//2
    mag_low = dsp.stft_mag(np.pad(x, (pad, pad)), N_FFT_LOW, HOP)[:, :mag.shape[1]]
    if mag_low.shape[1] < mag.shape[1]:
        mag_low = np.pad(mag_low, ((0, 0), (0, mag.shape[1] - mag_low.shape[1])))
    # chroma from the high-resolution spectrum (2.7 Hz bins resolve semitones down to the bass register),
    # harmonic part only, cropped to 2 kHz to keep the median filtering cheap
    top = int(2000 / (SR / N_FFT_LOW))
    harm_low, _ = hpss(mag_low[:top])
    harm_low = np.pad(harm_low, ((0, mag_low.shape[0] - top), (0, 0)))
    harm_chroma = dsp.chroma(harm_low, SR, N_FFT_LOW, fmin=110, fmax=2000)
    bass_chroma = dsp.chroma(harm_low, SR, N_FFT_LOW, fmin=35, fmax=200)
    beat_chroma = _sync(harm_chroma, beats)
    beat_bass = _sync(bass_chroma, beats)
    change = np.concatenate([[0], np.linalg.norm(np.diff(beat_chroma, axis=1), axis=0)])
    kick_at = bands["kick"][beats]
    phase = int(np.argmax([kick_at[p::4].mean() + 0.5 * change[p::4].mean() for p in range(4)]))
    lead_in = phase  # beats before the first downbeat
    beats_db = beats[phase:]
    n_bars = len(beats_db) // 4
    bar_frames = list(beats_db[:n_bars * 4:4]) + [beats_db[n_bars * 4] if len(beats_db) > n_bars * 4 else min(mag.shape[1], round(beats_db[-1]+60*FPS/tempo))]
    bar_frames = np.array(bar_frames)

    chord_names, chord_roots = recognize_chords(beat_chroma, beat_bass)

    # bar-level features for structure
    mf = dsp.mfcc(mag, SR, N_FFT)
    mf = (mf - mf.mean(axis=1, keepdims=True)) / (mf.std(axis=1, keepdims=True) + dsp.EPS)
    rms_db = 20 * np.log10(np.sqrt((mag ** 2).mean(axis=0)) + dsp.EPS)
    bar_chroma = _sync(harm_chroma, bar_frames[:-1])[:, :n_bars]
    bar_mfcc = _sync(mf, bar_frames[:-1])[:, :n_bars]
    bar_rms = _sync(rms_db[None, :], bar_frames[:-1])[0, :n_bars]
    # groove per bar: 16-step onset strengths of each drum band — what usually separates verse from chorus
    bar_rhythm = np.array([np.concatenate([_steps(bands[k], bar_frames[b], bar_frames[b + 1]) for k in bands])
                           for b in range(n_bars)]).T
    energy = (bar_rms - bar_rms.mean()) / (bar_rms.std() + dsp.EPS)
    # novelty: groove, timbre, loudness, and harmony smoothed over 4 bars (single bars change chord constantly)
    chroma_4 = ndimage.uniform_filter1d(bar_chroma, 4, axis=1, mode="nearest")
    nov_feats = np.vstack([bar_rhythm * 0.6, bar_mfcc * 0.3, energy[None, :] * 0.8, chroma_4 * 0.8])
    bounds, _ = segment_bars(nov_feats)
    # labeling compares sections bar by bar, so chord order matters here
    lab_feats = np.vstack([bar_rhythm * 0.6, bar_mfcc * 0.3, energy[None, :] * 0.8, bar_chroma * 1.0])
    fl = lab_feats / (np.linalg.norm(lab_feats, axis=0, keepdims=True) + dsp.EPS)
    letters = label_segments(bounds, fl.T @ fl, lab_feats)

    sal_midis, sal = bass_salience(mag_low, SR, N_FFT_LOW)
    sal_thr = np.percentile(sal.max(axis=0), 40)
    # onsets on the harmonic part: the kick lives in the same band and would mask bass re-attacks
    bass_onset = _normalize(dsp.onset_envelope(harm_low, SR, N_FFT_LOW, 35, 260))

    sections = []
    for (a, b), lab in zip(zip(bounds[:-1], bounds[1:]), letters):
        first_beat = phase + a * 4
        sections.append({
            "label": lab, "start_bar": int(a), "bars": int(b - a),
            "start_s": round(float(bar_frames[a] / FPS + TIME_OFFSET), 2),
            "energy_db": round(float(bar_rms[a:b].mean()), 1),
            "chords": _bar_chords(chord_names, phase, a, b),
            "_bars": list(range(a, b)), "_first_beat": first_beat,
        })
    name_sections(sections)
    cadence = [chord_roots[min(len(chord_roots) - 1, s["_first_beat"])] for s in sections]
    cadence += [chord_roots[-1]] * 2  # the ending counts double
    key = estimate_key(beat_chroma.sum(axis=1), cadence)

    # per unique label: drums from all occurrences, bass from the first
    parts = {}
    for s in sections:
        p = parts.setdefault(s["label"], {"label": s["label"], "name": s["name"], "bars": s["bars"],
                                          "occurrences": 0, "_bars": [], "first": s})
        p["occurrences"] += 1
        p["_bars"] += s["_bars"]
    # sub-120 Hz energy per bar: a section without it has no bass part (low pad notes would otherwise pass as bass)
    df = SR / N_FFT_LOW
    low_db = 10 * np.log10((harm_low[int(35 / df):int(120 / df)] ** 2).sum(axis=0) + dsp.EPS)
    bar_low = _sync(low_db[None, :], bar_frames[:-1])[0, :n_bars]
    low_ref = np.percentile(bar_low, 90)
    for p in parts.values():
        first = p.pop("first")
        bars_of = p.pop("_bars")
        p["drums"] = drum_pattern(bands, bar_frames, bars_of)
        p["chords"] = first["chords"]
        has_bass = np.mean(bar_low[bars_of]) > low_ref - 15
        p["bass"] = bass_notes(sal_midis, sal, bass_onset, beats, first["_first_beat"], first["bars"] * 4, sal_thr) if has_bass else []
    for s in sections:
        bars_of = s['_bars']
        s['drum_events'] = drum_events(detail_bands, bar_frames, bars_of, flux_delay=1.)
        s['bass'] = bass_notes(sal_midis, sal, bass_onset, beats, s['_first_beat'], s['bars']*4, sal_thr) if np.mean(bar_low[bars_of]) > low_ref-15 else []
        s.pop("_bars")
        s.pop("_first_beat")

    return {
        "duration_s": round(duration, 2),
        "tempo_bpm": round(tempo, 2),
        "tempo_confidence": tempo_conf,
        "tempo_alternatives": tempo_alternatives,
        "time_signature": "4/4 (assumed)",
        "key": {k: v for k, v in key.items() if k != "tonic_pc"},
        "first_downbeat_s": round(float(bar_frames[0] / FPS + TIME_OFFSET), 3),
        "lead_in_beats": int(lead_in),
        "bars": int(n_bars),
        "sections": sections,
        "parts": list(parts.values()),
        "detail_version": 2,
        "detail_notes": "Each section occurrence retains drum onset timing/velocity and its own eighth-resolution bass; confidence values are heuristic, not calibrated probabilities.",
    }


def _bar_chords(chord_names, phase, a, b):
    """Chords of bars [a, b) as [{bar, beat, chord}] with bar relative to the section start."""
    out = []
    for bar in range(a, b):
        for beat in range(4):
            i = phase + bar * 4 + beat
            if i >= len(chord_names):
                return out
            ch = chord_names[i]
            if not out or out[-1]["chord"] != ch:
                out.append({"bar": bar - a, "beat": beat, "chord": ch})
    return out
