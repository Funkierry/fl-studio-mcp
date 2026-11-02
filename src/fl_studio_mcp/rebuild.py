"""
Rebuild an analyzed song's skeleton in FL.

Automatic: tempo, one pattern per distinct section (named like "B Verse"), drum grooves written into each pattern's
step sequencer, and playlist markers at every section start. Chords and bass go into the piano roll, which FL only
lets a piano roll script write — those become a queue the user steps through with one click each.

FL's scripting API cannot place pattern clips in the playlist, so the markers show where each pattern goes.
"""

from .audio.song import chord_to_notes
from .errors import FLError

DRUM_KEYS = {"kick": ["kick", "bd", "底鼓", "大鼓"], "snare": ["snare", "clap", "sd", "军鼓", "拍手"],
             "hat": ["hat", "hh", "hihat", "镲", "踩镲"]}


def auto_drum_channels(channels):
    found = {}
    for role, keys in DRUM_KEYS.items():
        for ch in channels:
            if any(k in str(ch["name"]).lower() for k in keys):
                found[role] = ch["index"]
                break
    return found


def chord_notes_for_part(part):
    """Close-voiced chords spanning the part, in beats from the pattern start."""
    events = part["chords"]
    total = part["bars"] * 4
    notes = []
    for i, ev in enumerate(events):
        start = ev["bar"] * 4 + ev["beat"]
        end = events[i + 1]["bar"] * 4 + events[i + 1]["beat"] if i + 1 < len(events) else total
        if end <= start:
            continue
        for p in chord_to_notes(ev["chord"]):
            notes.append({"pitch": p, "start": start, "length": end - start, "velocity": 0.7})
    return notes


def build_plan(analysis, drum_channels, chord_channel, bass_channel, labels=None):
    known = {p['label'] for p in analysis['parts']}
    if labels is not None and (not labels or set(labels) - known):
        raise FLError('Select one or more existing section labels')
    parts = [p for p in analysis["parts"] if not labels or p["label"] in labels]
    steps = []
    for p in parts:
        if chord_channel is not None and p["chords"]:
            steps.append({"label": p["label"], "pattern_name": f"{p['label']} {p['name']}", "channel": chord_channel,
                          "what": "chords", "notes": chord_notes_for_part(p)})
        if bass_channel is not None and p["bass"]:
            steps.append({"label": p["label"], "pattern_name": f"{p['label']} {p['name']}", "channel": bass_channel,
                          "what": "bass", "notes": p["bass"]})
    return parts, steps


def validate_channels(project, drums, chord_channel, bass_channel):
    by_index = {ch['index']: ch for ch in project['channels']}
    if set(drums) - set(DRUM_KEYS):
        raise FLError('Drum roles must be kick, snare or hat')
    assigned = list(drums.values()) + [c for c in (chord_channel, bass_channel) if c is not None]
    if len(set(assigned)) != len(assigned):
        raise FLError('Use distinct channels for each drum role, chords and bass')
    for channel in assigned:
        if channel not in by_index:
            raise FLError(f'No channel {channel} in this project')


def run_automatic(session, analysis, parts, drum_channels, set_tempo=True, add_markers=True):
    log = []
    if set_tempo:
        r = session.set_tempo(round(analysis["tempo_bpm"], 2))
        log.append(f"tempo → {r.get('tempo')}")
    pattern_of = {}
    for p in parts:
        r = session.patterns("new", name=f"{p['label']} {p['name']}")
        idx = r["current_pattern"]
        pattern_of[p["label"]] = idx
        if drum_channels:
            tracks = []
            for role, ch in drum_channels.items():
                bar = p["drums"].get(role, "")
                if bar and set(bar) != {"."}:
                    tracks.append({"channel": ch, "steps": bar * p["bars"]})
            if tracks:
                r = session.write_drums(tracks)
                lengths = {t["channel"]: len(t["readback"].rstrip(".")) for t in r["tracks"]}
                log.append(f"pattern {idx} '{p['label']} {p['name']}': {p['bars']} bars, drums on "
                           + ", ".join(f"{role}→ch{ch}" for role, ch in drum_channels.items()))
                expected = p["bars"] * 16
                short = [ch for ch, n in lengths.items() if 0 < n < expected - 16]
                if short:
                    log.append(f"  note: step sequencer kept fewer steps than {expected} on channels {short}")
            else:
                log.append(f"pattern {idx} '{p['label']} {p['name']}': no drums detected")
        else:
            log.append(f"pattern {idx} '{p['label']} {p['name']}' created")
    markers = []
    if add_markers:
        labels = {p['label'] for p in parts}
        markers = [{"bar": s["start_bar"], "name": f"{s['name']} ({s['label']})"} for s in analysis["sections"]
                   if s['label'] in labels]
        session._call("add_markers", markers=markers)
        log.append(f"{len(markers)} playlist markers added")
    return pattern_of, log, markers
