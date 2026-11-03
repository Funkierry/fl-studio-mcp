"""Match the actual FL mixer using measured playback and explicit, reversible controls."""
import time

import numpy as np

from .audio import dsp, mixing
from .errors import FLError
from .recreation import atomic_json
from .tools import _finite_number


def validate_controls(controls):
    if controls is None:
        controls = [{'track': 0, 'kind': 'eq', 'band': b, 'min': .35, 'max': .65, 'step': .06} for b in range(3)]
    if not isinstance(controls, list) or not 1 <= len(controls) <= 16:
        raise FLError('controls must contain 1 to 16 mixer controls')
    result, seen = [], set()
    for control in controls:
        if not isinstance(control, dict):
            raise FLError('Each control must be an object')
        c = dict(control)
        kind = c.get('kind')
        if kind not in ('volume', 'pan', 'stereo', 'eq', 'effect'):
            raise FLError('Control kind must be volume, pan, stereo, eq or effect')
        for field, maximum in [('track', 125)]+([('band', 2)] if kind == 'eq' else [('slot', 9), ('param', 65535)] if kind == 'effect' else []):
            if type(c.get(field)) is not int or not 0 <= c[field] <= maximum:
                raise FLError(f'Invalid mixer control {field}')
        low = -1. if kind in ('pan', 'stereo') else 0.
        c['min'], c['max'] = _finite_number(c.get('min', low), 'min'), _finite_number(c.get('max', 1.), 'max')
        c['step'] = _finite_number(c.get('step', .06), 'step')
        if not low <= c['min'] < c['max'] <= 1 or not 0 < c['step'] <= .25:
            raise FLError('Invalid control bounds or step; step must be >0 and <=.25')
        identity = (c['track'], kind, c.get('band') if kind == 'eq' else c.get('slot'), c.get('param') if kind == 'effect' else None)
        if identity in seen:
            raise FLError('Duplicate mixer control')
        seen.add(identity)
        result.append(c)
    return result


def read_control(session, c):
    if c['kind'] == 'effect':
        return session._call('get_param_values', mixer_track=c['track'], slot=c['slot'], params=[c['param']])['values'][str(c['param'])]
    track = session.mixer([c['track']])['tracks'][0]
    if c['kind'] == 'eq':
        return next(e['gain'] for e in track['eq'] if e['band'] == c['band'])
    return track[{'stereo': 'stereo_separation'}.get(c['kind'], c['kind'])]


def apply_control(session, c, value, undo=False):
    if c['kind'] == 'effect':
        return session.set_params(mixer_track=c['track'], slot=c['slot'], changes=[{'param': c['param'], 'value': value}], undo=undo)
    args = {'eq': [{'band': c['band'], 'gain': value}]} if c['kind'] == 'eq' else {{'stereo': 'stereo_separation'}.get(c['kind'], c['kind']): value}
    return session.set_mixer(c['track'], **args, undo=undo)


def fit_live(session, reference, sr, output, controls=None, budget=12, seconds=8, mode='song', progress=None, cancelled=None):
    controls = validate_controls(controls)
    target = mixing.profile(reference[:round(seconds*sr)], sr)
    if session.info().get('bridge_version', 0) < 8:
        raise FLError('Live mixing requires bridge version 8; install and reload the new bridge')
    original = [read_control(session, c) for c in controls]
    for c, value in zip(controls, original):
        if value is None or not np.isfinite(value) or not c['min'] <= value <= c['max']:
            raise FLError('Current value is unavailable or outside requested bounds; inspect the mixer and widen the bounds')
    revert = [{'control': c, 'value': v} for c, v in zip(controls, original)]
    # Durable before the first FL write, so recovery also works after a crash.
    atomic_json(output/'mixer-revert.json', {'controls': revert, 'status': 'saved_before_changes'})
    best = list(original)
    history = []
    changed = False

    def capture():
        audio, rate, info = session.ears.listener.record_playback(seconds, mode)
        if info.get('capture') == 'system':
            raise FLError('Live mix search requires FL process capture; system loopback may include unrelated audio')
        audio = audio[:round(seconds*rate)]
        if info.get('silent'):
            raise FLError('FL playback was silent; select a populated pattern or arrangement')
        if rate != sr:
            audio = dsp.resample(audio, rate, sr)
        p = mixing.profile(audio, sr)
        score, terms = mixing.distance(p, target)
        # Do not improve spectral descriptors at the cost of a clipped output.
        score += max(0., p['peak_dbfs']+1)*2
        return audio, p, score, terms

    try:
        if cancelled:
            cancelled()
        before_audio, before, best_score, terms = capture()
        best_audio, after = before_audio, before
        baseline = best_score
        dsp.save_wav(output/'before.wav', before_audio, sr)
        history.append({'evaluation': 0, 'score': baseline, 'values': original, 'terms': terms})
        for evaluation in range(1, budget):
            if cancelled:
                cancelled()
            i = ((evaluation-1)//2) % len(controls)
            direction = -1 if evaluation % 2 else 1
            scale = .5**((evaluation-1)//(2*len(controls)))
            c = controls[i]
            candidate = list(best)
            candidate[i] = float(np.clip(best[i]+direction*c['step']*scale, c['min'], c['max']))
            atomic_json(output/'mixer-journal.json', {'phase': 'applying', 'values': candidate, 'revert': revert})
            changed = True  # a timeout may occur after FL applied the command
            apply_control(session, c, candidate[i], undo=evaluation == 1)
            actual = _finite_number(read_control(session, c), 'readback')
            if abs(actual-candidate[i]) > .002:
                raise FLError('FL mixer readback did not match requested value')
            candidate[i] = actual
            time.sleep(.1)
            audio, measured, score, terms = capture()
            history.append({'evaluation': evaluation, 'score': score, 'values': candidate, 'terms': terms})
            if score < best_score:
                best, best_score, best_audio, after = candidate, score, audio, measured
            else:
                apply_control(session, c, best[i])
            if progress:
                progress(evaluation+1, best_score)
        if cancelled:
            cancelled()
        dsp.save_wav(output/'mixed.wav', best_audio, sr)
        settings = [{'control': c, 'value': v} for c, v in zip(controls, best)]
        atomic_json(output/'mixer-journal.json', {'phase': 'best_applied', 'settings': settings, 'revert': revert})
        comparison = mixing.save_comparison(reference, before_audio, best_audio, sr, output/'mix-comparison.wav')
        return {'mode': 'live', 'reference': target, 'before': before, 'after': after,
                'distance_before': baseline, 'distance_after': best_score, 'history': history,
                'best_settings': settings, 'revert': revert, 'comparison': comparison,
                'limitations': ['Uses existing FL mixer/EQ/effect controls only; does not install or identify original effects.',
                                'Selected settings remain applied in FL. Save the project to persist them.',
                                'Measured on playback from the start; choose a matching reference excerpt.',
                                'Distance is not perceptual similarity. Playback and plugin randomness affect measurements.']}
    except BaseException:
        failures = []
        if changed:
            for c, value in zip(controls, original):
                try:
                    apply_control(session, c, value)
                except Exception as exc:
                    failures.append(str(exc))
        atomic_json(output/'mixer-journal.json', {'phase': 'restore_failed' if failures else 'original_restored',
                                                'restore_errors': failures, 'revert': revert})
        if failures:
            raise FLError('Mixer restoration could not be confirmed. Use mixer-revert.json; errors: '+str(failures))
        raise
