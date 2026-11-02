import copy
import sys
import tempfile
import unittest
from pathlib import Path

import mido

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from fl_studio_mcp import midi_export, rebuild
from fl_studio_mcp.errors import FLError


def analysis():
    return {'tempo_bpm': 120, 'parts': [{'label': 'A', 'name': 'Verse', 'bars': 1,
        'drums': {'kick': 'X...x...x...x...', 'snare': '....x.......x...', 'hat': '..o...x...o...x.'},
        'chords': [{'bar': 0, 'beat': 0, 'chord': 'Am'}],
        'bass': [{'pitch': 45, 'start': 0, 'length': 1, 'velocity': .8}]}],
        'sections': [{'label': 'A', 'name': 'Verse', 'start_bar': 0, 'bars': 1,
                       'chords': [{'bar': 0, 'beat': 0, 'chord': 'Am'}]},
                     {'label': 'A', 'name': 'Verse', 'start_bar': 2, 'bars': 2,
                       'chords': [{'bar': 0, 'beat': 0, 'chord': 'C'}]}]}


class MidiRecreationTests(unittest.TestCase):
    def test_occurrence_lengths_and_chord_variations_preserved(self):
        notes, sections = midi_export.arrangement_notes(analysis())
        self.assertEqual([n['start'] for n in notes['bass']], [0, 8, 12])
        self.assertEqual(len(notes['kick']), 12)
        self.assertEqual(sorted({n['pitch'] for n in notes['chords'] if n['start'] == 8}),
                         sorted(rebuild.chord_to_notes('C')))
        self.assertEqual({n['length'] for n in notes['chords'] if n['start'] == 8}, {8})

    def test_written_midi_has_correct_tempo_markers_notes_and_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            report = midi_export.export_arrangement(analysis(), Path(folder)/'arrangement.mid')
            midi = mido.MidiFile(report['midi'], charset='utf8')
            self.assertEqual(midi.type, 1)
            self.assertEqual(len(midi.tracks), 6)
            self.assertAlmostEqual(midi.length, 8)  # 4 bars at 120 BPM, including the gap
            self.assertEqual(next(m.tempo for m in midi.tracks[0] if m.type == 'set_tempo'), 500000)
            tick, markers = 0, []
            for message in midi.tracks[0]:
                tick += message.time
                if message.type == 'marker':
                    markers.append(tick)
            self.assertEqual(markers, [0, 8*480])
            for track in midi.tracks[1:]:
                self.assertEqual(sum(m.type == 'note_on' for m in track), report['tracks'][track.name.lower()])
                on = [m for m in track if m.type == 'note_on']
                off = [m for m in track if m.type == 'note_off']
                self.assertEqual(len(on), len(off))
                self.assertTrue(all(m.time >= 0 for m in track))
            self.assertTrue(all(m.channel == 9 and m.note == 36 for m in midi.tracks[1] if m.type == 'note_on'))

    def test_unknown_selection_fails_before_writing(self):
        with self.assertRaises(FLError):
            midi_export.arrangement_notes(analysis(), ['Z'])
        with self.assertRaises(FLError):
            rebuild.build_plan(analysis(), {}, 0, 1, [])

    def test_bass_notes_clipped_to_section_end(self):
        a = analysis()
        a['parts'][0]['bass'] = [{'pitch': 45, 'start': 3.5, 'length': 4}]
        notes, _ = midi_export.arrangement_notes(a)
        self.assertEqual([n['length'] for n in notes['bass']], [.5, .5, .5])


class RebuildPreflightTests(unittest.TestCase):
    def test_invalid_or_overlapping_channels_rejected(self):
        project = {'channels': [{'index': 0}, {'index': 1}, {'index': 2}]}
        for args in (({'kick': 0, 'hat': 0}, 1, 2), ({'kick': 0}, 1, 1), ({'kick': 8}, 1, 2)):
            with self.subTest(args=args), self.assertRaises(FLError):
                rebuild.validate_channels(project, *args)

    def test_markers_only_include_selected_parts(self):
        a = analysis()
        a['sections'].append({'label': 'B', 'name': 'Other', 'start_bar': 4, 'bars': 1})
        class Session:
            def patterns(self, *args, **kwargs):
                return {'current_pattern': 4}
            def _call(self, command, **args):
                self.markers = args['markers']
        session = Session()
        rebuild.run_automatic(session, a, a['parts'], {}, set_tempo=False, add_markers=True)
        self.assertEqual(len(session.markers), 2)
        self.assertTrue(all('(A)' in marker['name'] for marker in session.markers))


if __name__ == '__main__':
    unittest.main()
