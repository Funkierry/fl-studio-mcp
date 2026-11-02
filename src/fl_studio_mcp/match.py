"""
Sound matching: adjust a plugin's parameters until a note it plays sounds like a reference recording.

Black-box optimization on what FL actually outputs: set parameters → play a note → record → compare descriptors.
Phase 1 sweeps each parameter on a coarse grid (cheap, finds the big moves); phase 2 refines jointly with a
(1+1) evolution strategy whose step size shrinks. The best setting found is applied at the end, and the starting
values are returned so the change can be reverted.
"""

import random
import time
import math
import re

from .audio import timbre
from .errors import AudioError, FLError

KEYWORDS = ["cutoff", "filter", "freq", "reso", "attack", "decay", "sustain", "release", "env", "amount",
            "wave", "shape", "osc", "detune", "unison", "spread", "drive", "dist", "tone", "bright", "noise", "sub",
            "lfo", "mix", "滤波", "起音", "衰减", "延音", "释音", "包络", "波形", "共振", "截止"]
EXCLUDE = ["volume", "vol", "gain", "pan", "master", "output", "level", "音量", "声像", "midi", "program", "bypass"]


def suggest_params(params, limit=10):
    """Pick timbre-relevant parameters by name when the caller doesn't specify any."""
    scored = []
    for p in params:
        name = str(p["name"]).lower()
        envelope = any(k in name for k in ("envelope", "attack", "decay", "sustain", "release", "包络"))
        if any(x in name for x in EXCLUDE) and not envelope:
            continue
        score = sum(1 for k in KEYWORDS if k in name) + bool(re.search(r"\bq\b", name))
        if score:
            scored.append((score, p))
    scored.sort(key=lambda s: -s[0])
    return [p for _, p in scored[:limit]]


def run_match(session, listener, channel, target, params, pitch, note_len_s=0.8, budget=40, report=None, cancel=None,
              seed=0, reference_path=None):
    rng = random.Random(seed)
    report = report or (lambda p, m: None)
    names = {p["param"]: p["name"] for p in params}
    initial = {int(p["param"]): float(p["value"]) for p in params}
    if not initial or any(not math.isfinite(v) or not 0 <= v <= 1 for v in initial.values()):
        raise FLError("matching requires finite normalized parameter values")
    history = []
    best_audio = before_audio = None
    best_sr = before_sr = None

    def set_values(values, undo=False):
        session._call("set_params", channel=channel, undo=undo,
                      changes=[{"param": k, "value": v} for k, v in values.items()])

    def evaluate(values, label):
        if session.project().get("playing"):
            raise FLError("FL playback started during matching; stop transport before retrying")
        set_values(values)
        time.sleep(0.05)
        audio, sr = listener.render_note(channel, pitch, note_len_s)
        try:
            desc = timbre.describe(audio, sr)
            dist, parts = timbre.distance(target, desc)
            if not math.isfinite(dist):
                raise AudioError("non-finite sound distance")
        except AudioError:  # silent render (e.g. filter fully closed)
            desc, dist, parts = None, 1e9, {}
        history.append({"step": len(history), "phase": label,
                        "distance": round(dist, 3) if dist < 1e9 else None,
                        "values": dict(values)})
        return dist, parts, desc, audio, sr

    keys, best_vals = list(initial), dict(initial)
    try:
        # One undo point for the whole search; restoring after an exception is explicit.
        set_values(initial, undo=True)
        best, best_parts, best_desc, before_audio, before_sr = evaluate(best_vals, "start")
        best_audio, best_sr = before_audio, before_sr
        start_dist = best
        evals = 1

        def done():
            return evals >= budget or (cancel is not None and cancel.is_set())

        def consider(trial, label):
            nonlocal best, best_vals, best_parts, best_desc, best_audio, best_sr, evals
            d, parts, desc, audio, sr = evaluate(trial, label)
            evals += 1
            if d < best:
                best, best_vals, best_parts, best_desc = d, trial, parts, desc
                best_audio, best_sr = audio, sr
            report(evals / budget, f"{evals}/{budget} evaluations, best distance {best:.2f} (start {start_dist:.2f})")
            return d

        grid = [0.0, 0.25, 0.5, 0.75, 1.0]
        sweep_limit = min(budget, max(2, int(budget * .65)))
        candidates = {k: sorted([v for v in grid if abs(v - initial[k]) > 1e-6],
                                key=lambda v: abs(v - initial[k])) for k in keys}
        # Round-robin gives every parameter a chance before spending the coarse budget.
        for pos in range(len(grid)):
            for k in keys:
                if done() or evals >= sweep_limit:
                    break
                if pos < len(candidates[k]):
                    trial = dict(best_vals)
                    trial[k] = candidates[k][pos]  # integer parameter indexes are not keyword arguments
                    consider(trial, f"sweep {names[k]}")

        sigma = .15
        while not done():
            trial, previous_best = dict(best_vals), best
            for k in rng.sample(keys, max(1, len(keys) // 3)):
                trial[k] = min(1.0, max(0.0, trial[k] + rng.gauss(0, sigma)))
            consider(trial, "refine")
            sigma = min(.3, sigma * 1.3) if best < previous_best else max(.02, sigma * .85)
        if best_desc is None:
            raise FLError("all matching renders were silent; check the plugin and audio routing")
        set_values(best_vals)
        audio_files = {"before": listener.store.save_audio(before_audio, before_sr, "match_before"),
                       "best": listener.store.save_audio(best_audio, best_sr, "match_best")}
        if reference_path:
            audio_files["reference"] = reference_path
    except Exception as error:
        try:
            set_values(initial)
        except Exception as restore_error:
            raise FLError(f"Matching failed: {error}. Original settings could not be confirmed restored: "
                          f"{restore_error}. Restore values: {initial}") from error
        raise FLError(f"Matching failed; original settings restored: {error}") from error
    result = {
        "channel": channel,
        "pitch": pitch,
        "note_length_s": note_len_s,
        "target": timbre.public(target),
        "evaluations": evals,
        "start_distance": round(start_dist, 3),
        "best_distance": round(best, 3),
        "improvement": f"{(1 - best / start_dist) * 100:.0f}%" if 0 < start_dist < 1e9 else None,
        "distance_parts": best_parts,
        "remaining_differences": timbre.compare(target, best_desc) if best_desc else ["render was silent"],
        "changed": [{"param": k, "name": names[k], "from": round(initial[k], 3), "to": round(best_vals[k], 3)}
                    for k in keys if abs(best_vals[k] - initial[k]) > 1e-3],
        "revert": [{"param": k, "value": v} for k, v in initial.items()],
        "best_settings": [{"param": k, "value": v} for k, v in best_vals.items()],
        "audio_files": audio_files,
        "cancelled": bool(cancel is not None and cancel.is_set()),
        "metric_note": "Distance reduction is an objective feature comparison, not a perceptual similarity percentage. Listen to reference/before/best.",
        "history": history,
        "note": "Applied the best setting. Parameters outside the searched set, oscillator waveforms that are not "
                "exposed as parameters, and effects can limit how close it gets.",
    }
    try:
        result['match_id'] = listener.store.put('match', dict(result))
    except OSError as error:
        result['persistence_error'] = str(error)
    return result
