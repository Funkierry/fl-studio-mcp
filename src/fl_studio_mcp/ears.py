"""
The listening features: hear the project, diagnose vague complaints, analyze recordings, rebuild songs, match sounds.
"""

from . import match, rebuild
from .audio import dsp, perception, song, timbre
from .listen import Listener
from .store import Jobs, Store
from .tools import FLError, _finite_number, parse_pitch
import uuid

SONG_MIN_SECONDS = 20.0


class Ears:
    def __init__(self, session):
        self.session = session
        self.store = Store()
        self.jobs = Jobs()
        self.listener = Listener(session, self.store)
        self.rebuild_queue = []

    # ------------------------------------------------------------------ helpers
    def _channel(self, channel):
        proj = self.session.project()
        for ch in proj["channels"]:
            if ch["index"] == channel:
                return ch
        raise FLError(f"no channel {channel}")

    def _record(self, mode, seconds, channel=None):
        if self.jobs.active():
            raise FLError("Wait for the sound matching job to finish before recording playback")
        audio, sr, info = self.listener.record_playback(seconds, mode, channel)
        if info.get("silent"):
            where = f"channel {channel}" if channel is not None else "the project"
            raise FLError(f"{where} was silent while playing the current {mode}. If the part lives in another pattern, "
                          f"select it first (fl_patterns select) or listen in song mode.")
        return audio, sr, info

    # ------------------------------------------------------------------ listening & diagnosis
    def listen(self, mode="pattern", seconds=8.0, channel=None, role=None):
        audio, sr, info = self._record(mode, seconds, channel)
        m = perception.metrics(audio, sr)
        if channel is not None:
            ch = self._channel(channel)
            role = role or perception.guess_role(ch["name"], ch.get("plugin") or "")
            info["channel_name"] = ch["name"]
        role = role or "mix"
        return {"recording": info, "role_assumed": role, "metrics": m,
                "possible_issues": perception.auto_findings(m, role),
                "note": "Heuristic ranges are rules of thumb; compare against a reference track for a firmer verdict."}

    def diagnose(self, description="", channel=None, role=None, concerns=None, reference_path=None,
                 mode="pattern", seconds=8.0):
        ids = list(concerns or []) or perception.match_descriptors(description)
        unknown = [i for i in ids if i not in perception.BY_ID]
        if unknown:
            raise FLError(f"unknown concerns {unknown}; known: {sorted(perception.BY_ID)}")
        target_audio, sr, info = self._record(mode, seconds, channel)
        target = perception.metrics(target_audio, sr)
        result = {"description": description, "recording": info}
        mix = None
        ch = None
        if channel is not None:
            ch = self._channel(channel)
            role = role or perception.guess_role(ch["name"], ch.get("plugin") or "")
            mix_audio, mix_sr, mix_info = self._record(mode, seconds)
            mix = perception.metrics(mix_audio, mix_sr)
            result["mix_recording"] = mix_info
        role = role or "mix"
        reference = None
        if reference_path:
            ref_audio, ref_sr = dsp.load_audio(reference_path)
            reference = perception.metrics(ref_audio, ref_sr)
            result["reference_band_difference_db"] = perception.compare_bands(target, reference)
        result["role_assumed"] = role
        if not ids:
            result["interpreted_as"] = []
            result["findings"] = perception.auto_findings(target, role, reference)
            result["note"] = ("The description didn't map to a known quality; these are the measurable issues found. "
                              f"Known qualities: {sorted(perception.BY_ID)} — pass them as `concerns` to be explicit.")
        else:
            result["interpreted_as"] = [{"id": i, "meaning": perception.BY_ID[i]["meaning"]} for i in ids]
            result["findings"] = perception.diagnose(target, ids, role, reference, mix)
        result["metrics"] = target
        if ch is not None:
            result["fix_targets"] = self._fix_targets(ch, result["findings"])
        return result

    def _fix_targets(self, ch, findings):
        """Concrete knobs for the suggested fixes: matching plugin parameters and the channel's mixer track."""
        out = {"channel": ch["index"], "plugin": ch.get("plugin")}
        keywords = set()
        for f in findings:
            for fix in f["fixes"]:
                if fix["type"] == "plugin_param" and fix["target"]:
                    keywords.update(fix["target"])
        if ch.get("plugin") and keywords:
            try:
                params = self.session.list_params(ch["index"], limit=5000)["params"]
                out["plugin_params"] = [p for p in params if any(k in str(p["name"]).lower() for k in keywords)][:12]
            except FLError as e:
                out["plugin_params_error"] = str(e)
        try:
            mx = self.session.mixer()
            track = next((r["mixer_track"] for r in mx["routing"] if r["channel"] == ch["index"]), None)
            out["mixer_track"] = track
            out["mixer"] = next((t for t in mx["tracks"] if t["track"] == track), None) if isinstance(track, int) else None
            out["eq_api"] = mx.get("eq_api")
        except FLError as e:
            out["mixer_error"] = str(e)
        return out

    # ------------------------------------------------------------------ files
    def analyze_audio(self, path, start=None, end=None, kind="auto", tempo_hint=None):
        if kind not in ('auto', 'sound', 'song'):
            raise FLError("kind must be auto, sound or song")
        self._validate_region(start, end)
        audio, sr = dsp.load_audio(path, start=start, end=end)
        dur = len(audio) / sr
        if kind == "auto":
            kind = "song" if dur >= SONG_MIN_SECONDS else "sound"
        if kind == "song":
            result = song.analyze_song(audio, sr, tempo_hint=tempo_hint)
            result["source"] = str(path)
            aid = self.store.put("song", result)
            return {"analysis_id": aid, "kind": "song", **result,
                    "accuracy_note": "Automatic analysis: tempo/key/sections are usually solid; chords are triads only; "
                                     "drums are read from a full mix and may miss or invent hits; bass lines can have "
                                     "octave errors. Review before relying on details."}
        desc = timbre.describe(audio, sr)
        reference_wav = self.store.save_audio(audio, sr, 'reference_sound')
        aid = self.store.put("sound", timbre.public(desc), private={"descriptor": desc, "source": str(path),
                             "audio_path": reference_wav, "start": start, "end": end})
        return {"analysis_id": aid, "kind": "sound", "duration_s": round(dur, 2), **timbre.public(desc)}

    @staticmethod
    def _validate_region(start, end):
        a = 0 if start is None else _finite_number(start, 'start')
        b = None if end is None else _finite_number(end, 'end')
        if a < 0 or (b is not None and b <= a):
            raise FLError('Audio region requires 0 <= start < end')

    # ------------------------------------------------------------------ rebuild
    def plan_rebuild(self, analysis_id, drum_channels=None, chord_channel=None, bass_channel=None, parts=None):
        rec = self.store.get(analysis_id)
        if rec['kind'] != 'song':
            raise FLError('Expected a song analysis')
        project = self.session.project()
        drums = rebuild.auto_drum_channels(project['channels']) if drum_channels is None else drum_channels
        drums = {role: int(channel) for role, channel in drums.items()}
        rebuild.validate_channels(project, drums, chord_channel, bass_channel)
        selected, steps = rebuild.build_plan(rec['data'], drums, chord_channel, bass_channel, parts)
        return {'analysis_id': analysis_id, 'tempo_bpm': rec['data']['tempo_bpm'], 'key': rec['data']['key'],
                'drum_channels': drums, 'chord_channel': chord_channel, 'bass_channel': bass_channel,
                'available_channels': project['channels'],
                'patterns': [{'label': p['label'], 'name': p['name'], 'bars': p['bars']} for p in selected],
                'piano_roll_steps': [{'part': s['label'], 'channel': s['channel'], 'what': s['what'],
                                      'note_count': len(s['notes'])} for s in steps],
                'changes_project': False,
                'limits': ['Approximate drum/chord/bass skeleton; no vocal or lead transcription.',
                           'Piano-roll application and playlist placement require UI actions.',
                           'Instrument selection and sound matching are separate steps.']}

    def export_rebuild(self, analysis_id, parts=None):
        from .midi_export import export_arrangement
        rec = self.store.get(analysis_id)
        if rec['kind'] != 'song':
            raise FLError('Expected a song analysis')
        path = self.store.root / 'exports' / f'arrangement-{uuid.uuid4().hex[:8]}.mid'
        return export_arrangement(rec['data'], path, parts)

    def rebuild_song(self, analysis_id, drum_channels=None, chord_channel=None, bass_channel=None, parts=None,
                     set_tempo=True, add_markers=True):
        if self.rebuild_queue:
            raise FLError('Finish the pending rebuild queue before creating another arrangement')
        rec = self.store.get(analysis_id)
        if rec["kind"] != "song":
            raise FLError(f"{analysis_id} is a {rec['kind']} analysis, not a song")
        analysis = rec["data"]
        proj = self.session.project()
        if drum_channels is None:
            drum_channels = rebuild.auto_drum_channels(proj["channels"])
        drum_channels = {k: int(v) for k, v in (drum_channels or {}).items()}
        rebuild.validate_channels(proj, drum_channels, chord_channel, bass_channel)
        sel, steps = rebuild.build_plan(analysis, drum_channels, chord_channel, bass_channel, parts)
        pattern_of, log, markers = rebuild.run_automatic(self.session, analysis, sel, drum_channels, set_tempo, add_markers)
        for st in steps:
            st["pattern"] = pattern_of[st["label"]]
        self.rebuild_queue = steps
        return {
            "done": log,
            "patterns": [{"label": l, "pattern": i} for l, i in pattern_of.items()],
            "arrangement": [{"bar": s["start_bar"], "bars": s["bars"], "section": s["name"], "pattern": pattern_of.get(s["label"])}
                            for s in analysis["sections"]],
            "drum_channels_used": drum_channels,
            "piano_roll_steps_queued": [{"pattern": s["pattern"], "channel": s["channel"], "what": s["what"],
                                         "notes": len(s["notes"])} for s in steps],
            "next": ("Call fl_rebuild_next to open the first piano-roll step." if steps else
                     "No chord/bass channel given, so nothing to write into piano rolls."),
            "playlist": "FL's script API can't place clips: paint each pattern into the playlist at its marker.",
        }

    def rebuild_next(self, applied=False):
        pending = getattr(self, '_rebuild_pending', None)
        if pending and not applied:
            return dict(pending, awaiting_application=True,
                        then='Run MCP - Apply notes and verify its result, then call fl_rebuild_next(applied=true).')
        if pending and applied:
            self.rebuild_queue.pop(0)
            self._rebuild_pending = None
        if not self.rebuild_queue:
            return {"remaining": 0, "message": "Rebuild queue is empty — all piano-roll steps have been staged."}
        st = self.rebuild_queue[0]
        self.session.patterns("select", index=st["pattern"])
        r = self.session.write_notes(st["channel"], st["notes"], mode="merge",
                                     label=f"{st['pattern_name']} {st['what']}")
        user_action = r["user_action_required"]
        self._rebuild_pending = {"staged": f"{st['what']} for pattern {st['pattern']} '{st['pattern_name']}' on channel {st['channel']}",
                "notes": len(st["notes"]), "remaining": len(self.rebuild_queue)-1,
                "user_action_required": user_action,
                "awaiting_application": True,
                "then": "After running the script and verifying its result, call fl_rebuild_next(applied=true)."}
        return dict(self._rebuild_pending)

    # ------------------------------------------------------------------ sound matching
    def match_sound(self, reference, channel, params=None, budget=40, start=None, end=None, pitch=None, note_len_s=None):
        self._validate_region(start, end)
        if self.jobs.active():
            raise FLError('A sound matching job is already running; wait or cancel it first')
        if self.session.project().get('playing'):
            raise FLError('Stop FL playback before matching a single sound')
        if str(reference).startswith("sound-"):
            if start is not None or end is not None:
                raise FLError('A sound analysis already fixes its region; analyze a new slice to change it')
            rec = self.store.get(reference)
            target = rec["private"]["descriptor"]
            reference_path = rec['private'].get('audio_path')
        else:
            audio, sr = dsp.load_audio(reference, start=start, end=end)
            target = timbre.describe(audio, sr)
            reference_path = self.store.save_audio(audio, sr, 'reference_sound')
        ch = self._channel(channel)
        if not ch.get("plugin"):
            raise FLError(f"channel {channel} has no plugin to adjust")
        all_params = self.session.list_params(channel, limit=5000)["params"]
        if params is not None:
            wanted = {int(p) for p in params}
            chosen = [p for p in all_params if p["param"] in wanted]
            missing = wanted - {p['param'] for p in chosen}
            if missing:
                raise FLError(f'Unknown plugin parameter indexes: {sorted(missing)}')
        else:
            chosen = match.suggest_params(all_params)
        if not chosen:
            raise FLError("no parameters to search: pass `params` (indices from fl_list_params)")
        # list_params rounds values for display; retain full precision for restoration.
        values = self.session._call('get_param_values', channel=channel,
                                    params=[p['param'] for p in chosen])['values']
        chosen = [dict(p, value=values[str(p['param'])]) for p in chosen]
        detected = target.get('midi_note')
        pitch = parse_pitch(pitch if pitch is not None else detected if detected is not None else 60)
        note_len = (_finite_number(note_len_s, 'note_len_s') if note_len_s is not None
                    else min(1.5, max(0.3, target['decay_to_-20db_s'] * .8)))
        if not .05 <= note_len <= 5:
            raise FLError('note_len_s must be between 0.05 and 5 seconds')
        numeric_budget = _finite_number(budget, 'budget')
        if not numeric_budget.is_integer() or not 2 <= numeric_budget <= 200:
            raise FLError('budget must be an integer between 2 and 200')
        budget = int(numeric_budget)
        jid = self.jobs.start("match", match.run_match, self.session, self.listener, channel, target, chosen, pitch,
                              note_len, budget, reference_path=reference_path)
        return {"job_id": jid, "channel": channel, "plugin": ch["plugin"], "pitch": pitch, "budget": budget,
                "searching": [{"param": p["param"], "name": p["name"]} for p in chosen],
                "target": timbre.public(target),
                "estimated_seconds": round(budget * (note_len + 1.1)),
                "next": "Poll fl_job with this job_id. Keep FL's transport stopped and don't play other sounds in FL meanwhile."}

    def job(self, job_id, cancel=False):
        return self.jobs.status(job_id, cancel)

    def match_result(self, match_id):
        rec = self.store.get(match_id)
        if rec['kind'] != 'match':
            raise FLError('Expected a match result id')
        return dict(rec['data'], match_id=rec['id'])
