"""Exercise FL's load-then-apply lifecycle, including isolated interpreters."""
import builtins
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from fl_studio_mcp import paths


class PianoLifecycleTests(unittest.TestCase):
    def setUp(self):
        class Note:
            def __init__(self):
                self.number, self.time, self.length = 0, 0, 0
                self.velocity, self.selected = 0.0, False

        class Score:
            PPQ = 96
            def __init__(self):
                self.notes = []
            @property
            def noteCount(self):
                return len(self.notes)
            def getNote(self, index):
                return self.notes[index]
            def addNote(self, note):
                self.notes.append(note)
            def clearNotes(self, *args):
                self.notes.clear()

        self.messages, self.logs = [], []
        self.fake = types.ModuleType('flpianoroll')
        self.fake.Note, self.fake.score = Note, Score()
        self.fake.Utils = types.SimpleNamespace(log=self.logs.append, ShowMessage=self.messages.append)
        for patcher in (patch.dict(sys.modules, {'flpianoroll': self.fake}),
                        patch.object(builtins, '_flagent_applied_stamps', set(), create=True),
                        patch.object(builtins, '_flagent_pr_report', None, create=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertFalse(hasattr(builtins, '_flagent_midi_alive'))
        self.payload = {'stamp': 123, 'mode': 'merge', 'notes': [
            {'pitch': pitch, 'start': i, 'length': length, 'velocity': .65}
            for i, (pitch, length) in enumerate([(45, .75), (48, .75), (52, 1.5)])]}

    def load(self):
        source = paths.packaged_script('MCP - Apply notes.pyscript')
        source = source.replace('DATA = None', f'DATA = {self.payload!r}')
        script = {}
        exec(compile(source, 'test.pyscript', 'exec'), script)
        return script

    def test_load_does_not_write_until_fl_calls_apply(self):
        script = self.load()
        self.assertEqual(self.fake.score.noteCount, 0)
        self.assertEqual(self.messages, [])
        self.assertIsNone(builtins._flagent_pr_report)
        script['apply']()
        self.assertEqual([(n.number, n.time, n.length) for n in self.fake.score.notes],
                         [(45, 0, 72), (48, 96, 72), (52, 192, 144)])

    def test_zero_or_one_argument_callbacks_and_rerun_do_not_duplicate(self):
        self.load()['apply']()
        self.load()['apply'](None)
        self.assertEqual(self.fake.score.noteCount, 3)
        self.assertEqual(builtins._flagent_pr_report['added'], 0)
        self.assertIsNone(builtins._flagent_pr_report['source'])

    def test_fresh_interpreter_deduplicates_and_reports_actual_score(self):
        self.load()['apply']()
        builtins._flagent_applied_stamps = set()
        self.load()['apply']()
        report = json.loads(self.logs[-1].removeprefix('FL MCP REPORT: '))
        self.assertEqual(report['added'], 0)
        self.assertEqual(report['skipped_duplicates'], 3)
        self.assertEqual(report['note_count'], 3)
        self.assertFalse(report['shared'])
        self.assertIn('MIDI pitches: 45, 48, 52', self.messages[-1])
        self.assertIn('readback is unavailable', self.messages[-1])


if __name__ == '__main__':
    unittest.main()
