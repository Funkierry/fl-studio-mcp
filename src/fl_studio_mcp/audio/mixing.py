"""Reference-guided, bounded audio mixing. No source separation or plugin-state inference.

All processing is deterministic and the selected recipe can be replayed. Selection uses
distributed excerpts; the final result is checked on the complete audio before acceptance.
"""
from pathlib import Path

import numpy as np
from scipy import ndimage, signal

from . import dsp
from ..errors import FLError

CENTERS = np.array([40, 80, 160, 315, 630, 1250, 2500, 5000, 10000, 16000.])


def stereo(audio):
    x = np.asarray(audio, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    if x.ndim != 2 or x.shape[1] not in (1, 2) or not len(x) or not np.isfinite(x).all():
        raise FLError('Mixing requires finite, nonempty mono or stereo audio')
    return np.repeat(x, 2, axis=1) if x.shape[1] == 1 else x.copy()


def profile(audio, sr):
    x = stereo(audio)
    if len(x) < int(.1*sr) or np.max(np.abs(x)) < 1e-5:
        raise FLError('Mix profile requires at least 0.1 s of audible audio')
    # Sum channel powers, not mono samples: anti-phase material must remain audible.
    f, psd = signal.welch(x, sr, nperseg=min(4096, len(x)), axis=0)
    power = psd.mean(axis=1)
    edges = np.r_[20., np.sqrt(CENTERS[:-1]*CENTERS[1:]), sr/2]
    bands = np.array([power[(f >= a) & (f < b)].sum() for a, b in zip(edges[:-1], edges[1:])])
    relative = 10*np.log10((bands+1e-20)/(bands.sum()+1e-20))
    mid, side = (x[:, 0]+x[:, 1])/2, (x[:, 0]-x[:, 1])/2
    rms = float(np.sqrt(np.mean(x*x)))
    hop = max(1, round(.01*sr))
    frames = x[:len(x)//hop*hop].reshape(-1, hop, 2)
    env = np.sqrt(np.mean(frames*frames, axis=(1, 2)))
    active = env[env > max(env.max()*.01, 1e-7)]
    # A decay proxy, not RT60: musical articulation and overlap also affect it.
    peaks, _ = signal.find_peaks(env, distance=25, prominence=max(env.max()*.15, 1e-6))
    tails = [np.mean(env[p+8:p+24])/(env[p]+1e-12) for p in peaks if p+24 < len(env)]
    return {'bands_db': relative.tolist(), 'centers_hz': CENTERS.tolist(),
            'lufs': dsp.loudness(x, sr)['integrated_lufs'],
            'crest_db': float(20*np.log10((np.abs(x).max()+1e-12)/(rms+1e-12))),
            'dynamic_range_db': float(20*np.log10((np.percentile(active, 90)+1e-12)/(np.percentile(active, 10)+1e-12))),
            'width_db': float(np.clip(10*np.log10((np.mean(side*side)+1e-15)/(np.mean(mid*mid)+1e-15)), -60, 30)),
            'balance_db': float(np.clip(10*np.log10((np.mean(x[:, 1]**2)+1e-15)/(np.mean(x[:, 0]**2)+1e-15)), -24, 24)),
            'decay_proxy': float(np.median(tails)) if tails else 0.,
            'peak_dbfs': float(20*np.log10(np.abs(x).max()+1e-12))}


def distance(actual, reference):
    a, b = np.array(actual['bands_db']), np.array(reference['bands_db'])
    relevant = (a > -55) | (b > -55)
    terms = {'tone': float(np.mean(np.minimum(np.abs(a[relevant]-b[relevant]), 24)))/6,
             'loudness': abs(actual['lufs']-reference['lufs'])/6,
             'crest': abs(actual['crest_db']-reference['crest_db'])/8,
             'dynamics': abs(actual['dynamic_range_db']-reference['dynamic_range_db'])/12,
             'width': abs(actual['width_db']-reference['width_db'])/15,
             'balance': abs(actual['balance_db']-reference['balance_db'])/8,
             'decay_proxy': abs(actual['decay_proxy']-reference['decay_proxy'])/2}
    return float(sum(terms.values())), terms


def equalize(x, sr, gains):
    # Broad log-frequency curve, FIR with compensated group delay. No empty-band boosts.
    freq = np.linspace(0, sr/2, 1025)
    curve = np.interp(np.log2(np.maximum(freq, 20)), np.log2(CENTERS), gains)
    taps = signal.firwin2(1025, freq, 10**(curve/20), fs=sr)
    return signal.fftconvolve(x, taps[:, None], mode='same', axes=0)


def compress(x, sr, ratio=1., threshold_db=-18., attack_ms=15., release_ms=150.):
    if ratio <= 1:
        return x.copy()
    hop = max(1, round(sr*.001))
    padded = np.pad(np.max(np.abs(x), axis=1), (0, (-len(x)) % hop))
    peak_db = 20*np.log10(padded.reshape(-1, hop).max(axis=1)+1e-12)
    desired = np.maximum(peak_db-threshold_db, 0)*(1-1/ratio)
    attack, release = np.exp(-hop/(sr*attack_ms/1000)), np.exp(-hop/(sr*release_ms/1000))
    reduction, previous = np.empty(len(desired)), 0.
    for i, value in enumerate(desired):
        coefficient = attack if value > previous else release
        previous = coefficient*previous+(1-coefficient)*value
        reduction[i] = previous
    gains = 10**(-np.interp(np.arange(len(x)), np.arange(len(desired))*hop, reduction)/20)
    return x*gains[:, None]


def ambience(x, sr, wet, decay_s):
    if wet <= 0:
        return x.copy()
    # Deterministic diffused stereo taps. Keep the timeline and tail length unchanged.
    rng = np.random.default_rng(7401)
    n = max(2, round(decay_s*2*sr))
    impulse = np.zeros((n, 2))
    times = np.arange(.023, decay_s*2, .019)
    for channel in range(2):
        positions = np.minimum(n-1, np.round((times+rng.uniform(0, .008, len(times)))*sr).astype(int))
        impulse[positions, channel] = rng.choice([-1., 1.], len(times))*np.exp(-times*3/decay_s)
        impulse[:, channel] /= max(1, np.sum(np.abs(impulse[:, channel])))
    mono = x.mean(axis=1)
    tail = np.stack([signal.fftconvolve(mono, impulse[:, c])[:len(x)] for c in range(2)], axis=1)
    return (1-wet)*x+wet*tail


def process(audio, sr, recipe, finalize=True):
    x = stereo(audio)
    gains = recipe.get('eq_gains_db', [0.]*len(CENTERS))
    if np.max(np.abs(gains)) > 1e-8:
        x = equalize(x, sr, gains)
    x = compress(x, sr, **recipe.get('compressor', {}))
    x = ambience(x, sr, **recipe.get('ambience', {'wet': 0., 'decay_s': .3}))
    width = recipe.get('width', 1.)
    mid, side = (x[:, 0]+x[:, 1])/2, (x[:, 0]-x[:, 1])/2*width
    x = np.stack([mid+side, mid-side], axis=1)
    balance = recipe.get('balance_db', 0.)
    x *= [10**(-balance/40), 10**(balance/40)]
    if finalize:
        x *= 10**(recipe.get('gain_db', 0.)/20)
        # Oversampled peak guard is transparent gain attenuation, not hard clipping.
        peak = float(np.max(np.abs(signal.resample_poly(x, 4, 1, axis=0))))
        x *= min(1., 10**(recipe.get('ceiling_dbtp', -1.)/20)/max(peak, 1e-12))
    return x


def excerpts(x, sr):
    size = min(len(x), 6*sr)
    starts = sorted(set([0, max(0, (len(x)-size)//2), max(0, len(x)-size)]))
    return np.concatenate([x[start:start+size] for start in starts])


def fit(reference, source, sr, progress=None, cancelled=None):
    reference, source = stereo(reference), stereo(source)
    if not 8000 <= sr <= 192000:
        raise FLError('Mixing supports sample rates from 8000 to 192000 Hz')
    ref_full, before = profile(reference, sr), profile(source, sr)
    ref, dry = excerpts(reference, sr), excerpts(source, sr)
    target, original = profile(ref, sr), profile(dry, sr)
    gain_curve = ndimage.gaussian_filter1d(np.clip(np.array(target['bands_db'])-original['bands_db'], -6, 6), .6)
    gain_curve[np.array(original['bands_db']) < -55] = np.minimum(gain_curve[np.array(original['bands_db']) < -55], 0)
    width = float(np.clip(10**((target['width_db']-original['width_db'])/20), .25, 2.))
    balance = float(np.clip(target['balance_db']-original['balance_db'], -4, 4))
    recipe = {'eq_centers_hz': CENTERS.tolist(), 'eq_gains_db': [0.]*len(CENTERS), 'width': 1., 'balance_db': 0.,
              'compressor': {'ratio': 1., 'threshold_db': -18., 'attack_ms': 15., 'release_ms': 150.},
              'ambience': {'wet': 0., 'decay_s': .3}, 'gain_db': 0., 'ceiling_dbtp': -1.}
    trials = []

    def evaluate(candidate):
        if cancelled:
            cancelled()
        y = process(dry, sr, candidate, finalize=False)
        candidate = dict(candidate, gain_db=float(np.clip(target['lufs']-dsp.loudness(y, sr)['integrated_lufs'], -12, 12)))
        y = process(dry, sr, candidate)
        score, terms = distance(profile(y, sr), target)
        trials.append({'score': score, 'recipe': candidate, 'terms': terms})
        if progress:
            progress(len(trials), score)
        return score, candidate

    best_score, recipe = evaluate(recipe)
    # Coordinate descent: no candidate can displace a better measured candidate.
    groups = [[{'eq_gains_db': (gain_curve*scale).tolist()} for scale in (.5, 1.)],
              [{'width': width, 'balance_db': balance}],
              [{'compressor': dict(recipe['compressor'], ratio=r)} for r in (1.5, 2.5)]
              if target['crest_db'] < original['crest_db']-1 else [],
              [{'ambience': {'wet': wet, 'decay_s': decay}} for wet, decay in ((.10, .25), (.20, .5), (.28, .9))]
              if target['decay_proxy'] > original['decay_proxy']+.03 or target['width_db'] > original['width_db']+3 else []]
    for group in groups:
        base = recipe
        for change in group:
            score, candidate = evaluate(dict(base, **change))
            if score < best_score:
                best_score, recipe = score, candidate
    # Re-fit gain on the complete recording, then reject full-song regressions.
    shaped = process(source, sr, recipe, finalize=False)
    recipe['gain_db'] = float(np.clip(ref_full['lufs']-dsp.loudness(shaped, sr)['integrated_lufs'], -12, 12))
    mixed = process(source, sr, recipe)
    after = profile(mixed, sr)
    score_before, terms_before = distance(before, ref_full)
    score_after, terms_after = distance(after, ref_full)
    accepted = score_after < score_before
    if not accepted:
        recipe = dict(recipe, eq_gains_db=[0.]*len(CENTERS), width=1., balance_db=0.,
                      compressor=dict(recipe['compressor'], ratio=1.), ambience={'wet': 0., 'decay_s': .3}, gain_db=0.)
        mixed = process(source, sr, recipe)
        after = profile(mixed, sr)
        score_after, terms_after = distance(after, ref_full)
    report = {'mode': 'offline', 'accepted_processing': accepted, 'recipe': recipe,
              'reference': ref_full, 'before': before, 'after': after,
              'distance_before': score_before, 'distance_after': score_after,
              'terms_before': terms_before, 'terms_after': terms_after, 'trials': trials,
              'true_peak_dbtp': float(20*np.log10(np.abs(signal.resample_poly(mixed, 4, 1, axis=0)).max()+1e-12)),
              'limitations': ['Descriptor distance is not a perceptual similarity percentage.',
                              'Master processing cannot recover missing instruments, vocals, notes or original effect settings.',
                              'Ambience is a bounded synthetic approximation, not an estimated room impulse response.',
                              'Offline DSP is saved as audio and recipe; it is not embedded into the FLP mixer.',
                              'A peak ceiling can prevent reaching the reference loudness.']}
    return mixed, report


def save_comparison(reference, before, after, sr, destination):
    clips = [stereo(x)[:16*sr] for x in (reference, before, after)]
    level = min(float(np.sqrt(np.mean(x*x))) for x in clips)
    clips = [x*min(1., level/(np.sqrt(np.mean(x*x))+1e-12)) for x in clips]
    silence = np.zeros((round(.75*sr), 2))
    ab = np.concatenate([clips[0], silence, clips[1], silence, clips[2]])
    ab *= min(1., .98/(np.abs(ab).max()+1e-12))
    dsp.save_wav(Path(destination), ab, sr)
    return {'path': str(destination), 'order': ['reference', 'before', 'after'],
            'seconds_each_max': 16, 'gap_seconds': .75, 'level_match': 'RMS attenuation only'}
