"""Export the analyzed arrangement as editable MIDI, without touching FL."""
from pathlib import Path
import math

import mido

from .audio.song import chord_to_notes
from .errors import FLError

PPQ = 480
DRUMS = {'kick': 36, 'snare': 38, 'hat': 42}


def arrangement_notes(analysis, labels=None):
    parts = {p['label']: p for p in analysis['parts']}
    if labels is not None and (not labels or set(labels) - parts.keys()):
        raise FLError('Select one or more existing section labels')
    selected = set(parts) if labels is None else set(labels)
    notes = {role: [] for role in ('kick', 'snare', 'hat', 'chords', 'bass')}
    sections = []
    for section in analysis['sections']:
        if section['label'] not in selected:
            continue
        part = parts[section['label']]
        offset, length = section['start_bar'] * 4, section['bars'] * 4
        if offset < 0 or length <= 0 or part['bars'] <= 0:
            raise FLError('Invalid section length or position')
        sections.append(section)
        for role, pitch in DRUMS.items():
            if 'drum_events' in section:
                for note in section['drum_events'].get(role, []):
                    start = note['start']
                    duration = min(note['length'], length-start)
                    if 0 <= start < length and duration > 0:
                        notes[role].append(dict(note, pitch=pitch, start=offset+start, length=duration))
                continue
            groove = ''.join(c for c in part['drums'].get(role, '') if not c.isspace() and c != '|')
            if groove and (len(groove) != 16 or any(c not in 'xXo.' for c in groove)):
                raise FLError(f'Expected a 16-step drum groove for {role}')
            for bar in range(section['bars']):
                for step, hit in enumerate(groove):
                    if hit != '.':
                        notes[role].append({'pitch': pitch, 'start': offset+bar*4+step/4,
                                            'length': .125, 'velocity': {'x': 100, 'X': 127, 'o': 55}[hit]/127})
        # Each occurrence has its own chord events; do not copy the first occurrence blindly.
        chords = section.get('chords', part['chords'])
        for index, event in enumerate(chords):
            start = event['bar']*4 + event['beat']
            end = (chords[index+1]['bar']*4 + chords[index+1]['beat']) if index+1 < len(chords) else length
            end = min(end, length)
            if end <= start or start < 0:
                continue
            for pitch in chord_to_notes(event['chord']):
                notes['chords'].append({'pitch': pitch, 'start': offset+start,
                                        'length': end-start, 'velocity': .7})
        # Bass is estimated for one representative part. Repeat or trim it to the occurrence length.
        cycle = length if 'bass' in section else part['bars']*4
        for repeat in range(math.ceil(length/cycle)):
            for note in section.get('bass', part['bass']):
                start = repeat*cycle + note['start']
                duration = min(note['length'], length-start, (repeat+1)*cycle-start)
                if 0 <= start < length and duration > 0:
                    notes['bass'].append(dict(note, start=offset+start, length=duration))
    return notes, sections


def export_arrangement(analysis, destination, labels=None):
    notes, sections = arrangement_notes(analysis, labels)
    bpm = float(analysis['tempo_bpm'])
    if not math.isfinite(bpm) or not 10 <= bpm <= 500:
        raise FLError('Invalid tempo for MIDI export')
    midi = mido.MidiFile(type=1, ticks_per_beat=PPQ, charset='utf8')
    conductor = mido.MidiTrack()
    midi.tracks.append(conductor)
    conductor.extend([mido.MetaMessage('track_name', name='Tempo and sections'),
                      mido.MetaMessage('set_tempo', tempo=mido.bpm2tempo(bpm)),
                      mido.MetaMessage('time_signature', numerator=4, denominator=4)])
    previous = 0
    for section in sorted(sections, key=lambda s: s['start_bar']):
        tick = round(section['start_bar']*4*PPQ)
        conductor.append(mido.MetaMessage('marker', text=f"{section['label']} {section['name']}", time=tick-previous))
        previous = tick
    end_tick = round(max((s['start_bar']+s['bars'] for s in sections), default=0)*4*PPQ)
    conductor.append(mido.MetaMessage('end_of_track', time=max(0, end_tick-previous)))
    counts = {}
    for role, events in notes.items():
        track = mido.MidiTrack()
        midi.tracks.append(track)
        track.append(mido.MetaMessage('track_name', name=role.capitalize()))
        channel = 9 if role in DRUMS else 0 if role == 'chords' else 1
        timed = []
        for note in events:
            pitch, start, length = int(note['pitch']), float(note['start']), float(note['length'])
            if not 0 <= pitch <= 127 or not math.isfinite(start+length) or start < 0 or length <= 0:
                raise FLError('Invalid note in analyzed arrangement')
            on, off = round(start*PPQ), round((start+length)*PPQ)
            velocity = max(1, min(127, round(note.get('velocity', .8)*127)))
            timed.append((on, 1, mido.Message('note_on', channel=channel, note=pitch, velocity=velocity)))
            timed.append((max(on+1, off), 0, mido.Message('note_off', channel=channel, note=pitch, velocity=0)))
        previous = 0
        for tick, _, event in sorted(timed, key=lambda item: (item[0], item[1])):
            track.append(event.copy(time=tick-previous))
            previous = tick
        counts[role] = len(events)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    midi.save(str(destination))
    return {'midi': str(destination), 'tempo_bpm': bpm, 'tracks': counts,
            'sections': [{'label': s['label'], 'bar': s['start_bar'], 'bars': s['bars']} for s in sections],
            'alignment': 'First detected downbeat is bar 0; initial audio lead-in is omitted.',
            'limitations': ['4/4 is assumed; chords are triads.',
                           'New analyses preserve occurrence-specific drum timing/velocity and bass; legacy analyses use repeated grooves.',
                           'This contains MIDI notes and structure, not separated stems or original instrument presets.'],
            'next': 'Import the MIDI in FL Studio and assign your drum samples, chord instrument and bass plugin.'}
