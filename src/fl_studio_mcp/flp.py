"""Conservative FL Studio 24 template editor; unknown/plugin events remain opaque.

This is not a general FLP writer. A saved template must contain five named pattern
clips (Full Kick/Snare/Hat/Chords/Bass), each with notes on one instrument channel.
The file format facts were cross-checked against the PyFLP project's public format
research (pattern.py, arrangement.py, project.py); no PyFLP code is vendored.
Only newly created files are written. The input template is never overwritten.
"""
import math
import struct
from pathlib import Path

from .errors import FLError

ROLES = ('kick', 'snare', 'hat', 'chords', 'bass')


def _varint(n):
    out = bytearray()
    while n >= 128:
        out.append((n & 127) | 128)
        n >>= 7
    out.append(n)
    return bytes(out)


def _text(data):
    return data.decode('utf-16le').rstrip('\0')


class Project:
    def __init__(self, path):
        self.path = Path(path).resolve()
        raw = self.path.read_bytes()
        if len(raw) < 22 or raw[:8] != b'FLhd\x06\0\0\0' or raw[14:18] != b'FLdt':
            raise FLError('Not a supported FLP project')
        self.header = raw[:18]
        fmt, self.channels, self.ppq = struct.unpack_from('<HHH', raw, 8)
        if fmt != 0 or not 24 <= self.ppq <= 960 or len(raw)-22 != int.from_bytes(raw[18:22], 'little'):
            raise FLError('Invalid FLP header, length or timebase')
        self.events = []
        offset = 22
        while offset < len(raw):
            eid = raw[offset]
            offset += 1
            if eid < 192:
                size = 1 if eid < 64 else 2 if eid < 128 else 4
            else:
                size = 0
                for shift in range(0, 35, 7):
                    if offset >= len(raw):
                        raise FLError('Truncated FLP event size')
                    byte = raw[offset]
                    offset += 1
                    size |= (byte & 127) << shift
                    if byte < 128:
                        break
                else:
                    raise FLError('Invalid FLP event size')
            if offset+size > len(raw):
                raise FLError('Truncated FLP event')
            self.events.append((eid, raw[offset:offset+size]))
            offset += size
        versions = [d.decode('ascii').rstrip('\0') for e, d in self.events if e == 199]
        if len(versions) != 1 or versions[0].split('.')[0] != '24':
            raise FLError('Template writer currently supports FL Studio 24 projects only')
        self.version = versions[0]

    def patterns(self):
        found = {}
        current = None
        for index, (eid, data) in enumerate(self.events):
            if eid == 65:
                current = int.from_bytes(data, 'little')
                found.setdefault(current, {'index': current, 'notes': b'', 'name': ''})
            elif current is not None and eid == 224:
                if len(data) % 24:
                    raise FLError('Unsupported FLP note record')
                if found[current]['notes']:
                    raise FLError('Ambiguous pattern notes in template')
                found[current].update(notes=data, event_index=index)
            elif current is not None and eid == 193:
                found[current]['name'] = _text(data)
        return found

    def template(self, role_patterns=None):
        patterns = self.patterns()
        if role_patterns is None:
            role_patterns = {}
            for role in ROLES:
                hits = [p['index'] for p in patterns.values() if p['name'].lower() == 'full '+role]
                if len(hits) != 1:
                    raise FLError(f'Template needs exactly one Full {role.title()} pattern, or explicit role_patterns')
                role_patterns[role] = hits[0]
        if set(role_patterns) != set(ROLES) or len(set(role_patterns.values())) != 5:
            raise FLError('Map all five roles to distinct pattern numbers')
        if sum(e == 99 for e, _ in self.events) != 1:
            raise FLError('Use a template with exactly one playlist arrangement')
        playlists = [d for e, d in self.events if e == 233]
        if len(playlists) != 1 or not playlists[0] or len(playlists[0]) % 60:
            raise FLError('Template needs FL 24 playlist pattern clips (60-byte records)')
        if any(e == 223 and d for e, d in self.events):
            raise FLError('Use a clean instrument template without pattern automation')
        roles = {}
        clips = [playlists[0][i:i+60] for i in range(0, len(playlists[0]), 60)]
        for role, number in role_patterns.items():
            if isinstance(number, bool) or not isinstance(number, int):
                raise FLError('Pattern numbers must be integers')
            pattern = patterns.get(number)
            if not pattern or not pattern['notes']:
                raise FLError(f'Template pattern {number} must contain a note for instrument mapping')
            notes = pattern['notes']
            channels = {struct.unpack_from('<H', notes, i+6)[0] for i in range(0, len(notes), 24)}
            if len(channels) != 1:
                raise FLError(f'Template pattern {number} must use exactly one instrument channel')
            matches = [c for c in clips if struct.unpack_from('<HH', c, 4) == (20480, 20480+number)]
            if not matches:
                raise FLError(f'Place template pattern {number} once in the playlist and save it')
            if matches[0][24:32] != b'\xff'*8:
                raise FLError('Template pattern clips must be untrimmed (default start/end offsets)')
            roles[role] = {'pattern': number, 'channel': channels.pop(), 'name': pattern['name'],
                           'note': notes[:24], 'clip': matches[0]}
        if len({r['channel'] for r in roles.values()}) != 5:
            raise FLError('The five roles must use distinct instrument channels')
        return roles

    def write(self, path, events):
        path = Path(path).resolve()
        if path == self.path or path.exists():
            raise FLError('Refusing to overwrite an existing FLP file')
        data = bytearray()
        for eid, payload in events:
            data.append(eid)
            if eid >= 192:
                data.extend(_varint(len(payload)))
            data.extend(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as file:
            file.write(self.header + struct.pack('<I', len(data)) + data)


def inspect_template(path, role_patterns=None):
    project = Project(path)
    roles = project.template(role_patterns)
    return {'fl_version': project.version, 'ppq': project.ppq,
            'roles': {r:{k:v for k,v in item.items() if k not in ('note','clip')} for r,item in roles.items()}}


def build_project(template, destination, analysis, role_patterns=None):
    from .midi_export import arrangement_notes
    project = Project(template)
    roles = project.template(role_patterns)
    notes, sections = arrangement_notes(analysis)
    bpm = float(analysis['tempo_bpm'])
    if not math.isfinite(bpm) or not 10 <= bpm <= 500 or not sections:
        raise FLError('Invalid arrangement tempo or empty sections')
    end_beat = max(s['start_bar']+s['bars'] for s in sections)*4
    end_tick = round(end_beat*project.ppq)
    if not 0 < end_tick < 2**31:
        raise FLError('Arrangement exceeds supported FLP duration')
    new_notes, clips = {}, bytearray()
    for track, role in enumerate(ROLES):
        item = roles[role]
        payload = bytearray()
        for note in sorted(notes[role], key=lambda n:(n['start'], n['pitch'])):
            start, length = float(note['start']), float(note['length'])
            pitch = 60 if role in ('kick', 'snare', 'hat') else int(note['pitch'])
            velocity = float(note.get('velocity', .8))
            if not math.isfinite(start+length+velocity) or start < 0 or length <= 0 or not 0 <= pitch <= 127 or not 0 <= velocity <= 1:
                raise FLError('Invalid note in reconstruction')
            onset, duration = round(start*project.ppq), max(1, round(length*project.ppq))
            if onset+duration > end_tick+1:
                raise FLError('Note extends beyond the arrangement')
            encoded = bytearray(item['note'])
            struct.pack_into('<IHHIHH', encoded, 0, onset, 0, item['channel'], duration, pitch, 0)
            encoded[16:24] = bytes([120, 0, 64, 0, 64, max(1, min(128, round(velocity*128))), 128, 128])
            payload.extend(encoded)
        new_notes[item['pattern']] = bytes(payload)
        clip = bytearray(item['clip'])
        struct.pack_into('<IHHIHH', clip, 0, 0, 20480, 20480+item['pattern'], end_tick, 499-track, 0)
        clip[18:20] = (64).to_bytes(2, 'little')
        clip[24:32] = b'\xff'*8  # Preserve FL's untrimmed pattern-clip sentinel.
        clips.extend(clip)
    events, current, inserted = [], None, set()
    arrangement = False
    for eid, data in project.events:
        if eid == 65:
            current = int.from_bytes(data, 'little')
        if eid == 99:
            arrangement = True
        if eid == 224:
            data = new_notes.get(current, b'')
            inserted.add(current)
        elif eid == 223:
            data = b''
        elif eid == 156:
            data = struct.pack('<I', round(bpm*1000))
        elif eid == 9:
            data = b'\x01'  # Song transport mode in FL 24 template.
        elif eid == 67:
            data = struct.pack('<H', roles['chords']['pattern'])
        elif eid == 217:
            data = b'\xff'*len(data)  # No selected render/time region.
        elif eid == 233:
            data = bytes(clips)
        elif arrangement and eid in (148, 205, 33, 34):
            continue
        elif eid == 241:
            data = 'MCP Reconstruction\0'.encode('utf-16le')
        events.append((eid, data))
        if eid == 233:
            for section in sections:
                events.extend([(148, struct.pack('<I', round(section['start_bar']*4*project.ppq))),
                               (205, (section['name']+'\0').encode('utf-16le'))])
    if not set(new_notes).issubset(inserted):
        raise FLError('Template is missing pattern note events')
    project.write(destination, events)
    check = Project(destination)
    actual = check.patterns()
    counts = {r:len(actual[item['pattern']]['notes'])//24 for r,item in roles.items()}
    if counts != {r:len(notes[r]) for r in ROLES}:
        raise FLError('Generated FLP failed note-count verification')
    return {'project':str(Path(destination).resolve()), 'bars':end_beat/4, 'tempo_bpm':bpm,
            'duration_s':end_beat*60/bpm, 'note_counts':counts, 'sections':sections,
            'instruments':inspect_template(template, role_patterns)['roles']}
