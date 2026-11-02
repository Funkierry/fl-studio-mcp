"""Offline regressions: input validation, transactional staging, and rebuild retries.

    python tests/test_regressions.py

Uses temporary script files and simulated FL modules; never connects to FL Studio.
"""
import builtins
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from fl_studio_mcp import tools
from fl_studio_mcp.ears import Ears
from fl_studio_mcp.errors import FLError
from fl_studio_mcp.server import McpServer

NOTE = {"pitch": 60, "start": 0, "length": 1, "velocity": 0.8}


class RecordingSession(tools.FLSession):
    def __init__(self, fail_at=None):
        super().__init__()
        self.commands = []
        self.fail_at = fail_at

    def _call(self, cmd, **args):
        self.commands.append((cmd, args))
        if cmd == self.fail_at:
            raise FLError("simulated bridge failure")
        if cmd == "open_piano_roll":
            return {"selected_channel": args["channel"]}
        return {}


class ValidationTests(unittest.TestCase):
    def test_valid_pitch_boundaries_and_accidentals(self):
        for value, expected in [(0, 0), (127, 127), (60.0, 60), ("C-1", 0), ("G9", 127),
                                ("C#4", 61), ("Db4", 61), ("D♭4", 61), ("60", 60)]:
            with self.subTest(value=value):
                self.assertEqual(tools.parse_pitch(value), expected)

    def test_invalid_pitch_rejected(self):
        for pitch in [-1, 128, 60.5, True, float("nan"), float("inf"), "B-2", "G#9", "H4"]:
            with self.subTest(pitch=pitch), self.assertRaises(FLError):
                tools.parse_pitch(pitch)

    def test_invalid_times_and_velocity_rejected_before_dispatch(self):
        cases = [("start", -1), ("length", 0), ("length", -1), ("velocity", -1), ("velocity", 128)]
        cases += [(field, bad) for field in ("start", "length", "velocity")
                  for bad in (float("nan"), float("inf"), -float("inf"), True, None)]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                session = RecordingSession()
                with self.assertRaises(FLError):
                    session.audition(0, [dict(NOTE, **{field: value})], bpm=120)
                self.assertEqual(session.commands, [])

    def test_end_must_remain_finite(self):
        with self.assertRaises(FLError):
            tools.normalize_notes([dict(NOTE, start=1e308, length=1e308)])

    def test_velocity_formats_and_defaults_preserved(self):
        for value, expected in [(0, 0), (.8, .8), (1, 1), (100, round(100 / 127, 3)), (127, 1)]:
            with self.subTest(value=value):
                self.assertEqual(tools.normalize_notes([dict(NOTE, velocity=value)])[0]["velocity"], expected)
        self.assertEqual(tools.normalize_notes([{"pitch": "C4", "start": 0}]), [dict(NOTE)])

    def test_invalid_note_object_rejected(self):
        with self.assertRaises(FLError):
            tools.normalize_notes([None])

    def test_invalid_mode_rejected_before_dispatch(self):
        session = RecordingSession()
        with self.assertRaises(FLError):
            session.write_notes(0, [NOTE], mode="invalid")
        self.assertEqual(session.commands, [])

    def test_mcp_returns_tool_error_for_invalid_note(self):
        session = RecordingSession()
        result = McpServer(session).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": "fl_write_notes", "arguments": {
                                               "channel": 0, "notes": [dict(NOTE, pitch=128)]}}})
        self.assertTrue(result["result"]["isError"])
        self.assertIn("0–127", result["result"]["content"][0]["text"])
        self.assertEqual(session.commands, [])


class StagingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="fl-mcp-regression-")
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.path = self.folder / "MCP - Apply notes.pyscript"
        self.previous = b"# previous payload\r\nDATA = None\r\n"
        self.path.write_bytes(self.previous)
        patcher = patch.object(tools.paths, "piano_roll_script_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_previous_preserved(self):
        self.assertEqual(self.path.read_bytes(), self.previous)
        self.assertEqual(list(self.folder.iterdir()), [self.path])

    def test_disconnected_bridge_preserves_script(self):
        with self.assertRaises(FLError):
            RecordingSession("open_piano_roll").write_notes(0, [NOTE], mode="replace")
        self.assert_previous_preserved()

    def test_hint_failure_preserves_script(self):
        with self.assertRaises(FLError):
            RecordingSession("hint").write_notes(0, [NOTE], mode="replace")
        self.assert_previous_preserved()

    def test_failure_does_not_create_new_pending_script(self):
        self.path.unlink()
        with self.assertRaises(FLError):
            RecordingSession("open_piano_roll").write_notes(0, [NOTE])
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_atomic_replace_failure_preserves_file_and_cleans_temporary(self):
        session = RecordingSession()
        with patch.object(tools.os, "replace", side_effect=PermissionError("simulated locked file")):
            with self.assertRaises(PermissionError):
                session.write_notes(0, [NOTE])
        self.assert_previous_preserved()
        self.assertFalse(any(cmd == "stage_notes" for cmd, _ in session.commands))

    def test_partial_disk_write_preserves_file_and_cleans_temporary(self):
        real_temporary_file = tools.tempfile.NamedTemporaryFile

        class BrokenWrite:
            def __init__(self, **kwargs):
                self.file = real_temporary_file(**kwargs)
                self.name = self.file.name

            def __enter__(self):
                return self

            def write(self, content):
                self.file.write(content[:20])
                raise OSError("simulated full disk")

            def __exit__(self, *args):
                self.file.close()

        with patch.object(tools.tempfile, "NamedTemporaryFile", BrokenWrite):
            with self.assertRaises(OSError):
                RecordingSession().write_notes(0, [NOTE])
        self.assert_previous_preserved()

    def test_success_embeds_notes_and_requires_manual_apply(self):
        session = RecordingSession()
        result = session.write_notes(2, [NOTE], label="中文测试")
        source = self.path.read_text(encoding="utf-8")
        compile(source, str(self.path), "exec")
        self.assertIn("中文测试", source)
        self.assertIn("'pitch': 60", source)
        self.assertEqual(result["staged"], 1)
        self.assertEqual(result["piano_roll"]["selected_channel"], 2)
        self.assertIn("not in the project until", result["user_action_required"])
        self.assertEqual(list(self.folder.iterdir()), [self.path])
        self.assertFalse(any(cmd == "stage_notes" for cmd, _ in session.commands))

    def test_invalid_note_leaves_script_untouched(self):
        session = RecordingSession()
        with self.assertRaises(FLError):
            session.write_notes(0, [dict(NOTE, pitch=-1)])
        self.assert_previous_preserved()
        self.assertEqual(session.commands, [])


class RebuildTests(unittest.TestCase):
    def make_ears(self, fail_at=None):
        class Session:
            def __init__(self):
                self.fail_at = fail_at
                self.written = []

            def patterns(self, *args, **kwargs):
                if self.fail_at == "select":
                    raise FLError("simulated selection failure")

            def write_notes(self, channel, notes, **kwargs):
                if self.fail_at == "write":
                    raise FLError("simulated staging failure")
                self.written.append((channel, notes))
                return {"user_action_required": "Apply notes manually"}

        ears = Ears.__new__(Ears)
        ears.session = Session()
        ears.rebuild_queue = [{"pattern": 2, "channel": ch, "notes": [dict(NOTE)],
                               "what": "bass", "pattern_name": "Test"} for ch in (1, 2)]
        return ears

    def test_failed_selection_keeps_queue(self):
        ears = self.make_ears("select")
        before = list(ears.rebuild_queue)
        with self.assertRaises(FLError):
            ears.rebuild_next()
        self.assertEqual(ears.rebuild_queue, before)

    def test_failed_staging_keeps_queue_and_retry_uses_same_part(self):
        ears = self.make_ears("write")
        before = list(ears.rebuild_queue)
        with self.assertRaises(FLError):
            ears.rebuild_next()
        self.assertEqual(ears.rebuild_queue, before)
        ears.session.fail_at = None
        result = ears.rebuild_next()
        self.assertEqual(result["remaining"], 1)
        self.assertEqual(ears.session.written, [(1, [NOTE])])
        self.assertEqual(ears.rebuild_queue, before)
        ears.session.fail_at = 'write'
        with self.assertRaises(FLError):
            ears.rebuild_next(applied=True)
        self.assertEqual(ears.rebuild_queue, before[1:])

    def test_success_consumes_each_part_once(self):
        ears = self.make_ears()
        self.assertEqual(ears.rebuild_next()["remaining"], 1)
        self.assertEqual(ears.rebuild_next()["remaining"], 1)
        self.assertEqual(len(ears.session.written), 1)
        self.assertEqual(ears.rebuild_next(applied=True)["remaining"], 0)
        self.assertEqual(ears.rebuild_next(applied=True)["remaining"], 0)
        self.assertEqual(ears.rebuild_next()["remaining"], 0)
        self.assertEqual([ch for ch, _ in ears.session.written], [1, 2])


class PianoRollTests(unittest.TestCase):
    def test_embedded_payload_does_not_replay_old_midi_payload(self):
        class Note:
            def __init__(self):
                self.number, self.time, self.length, self.velocity, self.selected = 0, 0, 0, 0.0, False

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

        fake = types.ModuleType("flpianoroll")
        fake.Note, fake.score = Note, Score()
        fake.Utils = types.SimpleNamespace(log=lambda msg: None, ShowMessage=lambda msg: None)
        old = {"stamp": 1, "mode": "replace", "notes": [dict(NOTE, pitch=40)]}
        template = tools.paths.packaged_script("MCP - Apply notes.pyscript")
        payload = {"stamp": 2, "mode": "merge", "notes": [dict(NOTE)]}
        source = template.replace("DATA = None", f"DATA = {payload!r}")
        with patch.dict(sys.modules, {"flpianoroll": fake}), \
                patch.object(builtins, "_flagent_pending", old, create=True), \
                patch.object(builtins, "_flagent_applied_stamps", set(), create=True), \
                patch.object(builtins, "_flagent_pr_report", None, create=True):
            script = {}
            exec(compile(source, "test.pyscript", "exec"), script)
            self.assertEqual(fake.score.notes, [])
            self.assertIsNone(builtins._flagent_pr_report)
            script["apply"]()
            self.assertEqual([n.number for n in fake.score.notes], [60])
            self.assertEqual(builtins._flagent_pr_report["added"], 1)
            script = {}
            exec(compile(source, "test.pyscript", "exec"), script)
            script["apply"](None)
            self.assertEqual([n.number for n in fake.score.notes], [60])
            self.assertIsNone(builtins._flagent_pr_report["source"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
