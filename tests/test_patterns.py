"""FL's pattern count is not the maximum index; preserve sparse projects."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parent.parent / "src/fl_studio_mcp/fl/device_FLStudioMCP.py"


class PatternTests(unittest.TestCase):
    def setUp(self):
        self.used = {3: "Verse", 7: "Chorus"}
        self.current = 7
        self.maximum = 10
        modules = {name: types.ModuleType(name) for name in (
            "channels", "device", "general", "midi", "mixer", "patterns", "plugins", "transport", "ui")}
        patterns = modules["patterns"]
        patterns.patternNumber = lambda: self.current
        patterns.patternCount = lambda: len(self.used)
        patterns.patternMax = lambda: self.maximum
        patterns.isPatternDefault = lambda i: i not in self.used
        patterns.getPatternName = lambda i: self.used.get(i, f"Pattern {i}")
        patterns.getPatternLength = lambda i: 4
        patterns.jumpToPattern = lambda i: setattr(self, "current", i)
        patterns.setPatternName = lambda i, name: self.used.__setitem__(i, name)
        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location("pattern_test_bridge", SCRIPT)
            self.bridge = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.bridge)

    def test_sparse_list_contains_real_indices(self):
        self.assertEqual([p["index"] for p in self.bridge._pattern_list()], [3, 7])

    def test_length_reports_quarter_note_beats_and_native_steps(self):
        item=self.bridge._pattern_list()[0]
        self.assertEqual(item['length_steps'],4)
        self.assertEqual(item['length_beats'],1)

    def test_piano_roll_opens_editor_for_requested_channel(self):
        from unittest.mock import Mock
        b=self.bridge
        b.channels.selectOneChannel=Mock()
        b.channels.getRecEventId=lambda ch:1000+ch*100
        b.channels.selectedChannel=lambda:3
        b.midi.REC_Chan_PianoRoll=7
        b.midi.EE_PR=2
        b.midi.widPianoRoll=3
        b.ui.openEventEditor=Mock()
        b.ui.setFocused=Mock()
        b.ui.getFocusedFormCaption=lambda:'Piano roll - Snare'
        b.cmd_open_piano_roll({'channel':3})
        b.ui.openEventEditor.assert_called_once_with(1307,2)

    def test_current_empty_pattern_is_visible(self):
        self.current = 1
        self.assertEqual([p["index"] for p in self.bridge._pattern_list()], [1, 3, 7])

    def test_new_does_not_overwrite_pattern_at_count_plus_one(self):
        self.bridge.cmd_patterns({"action": "new", "name": "New"})
        self.assertEqual(self.current, 8)
        self.assertEqual(self.used[3], "Verse")
        self.assertEqual(self.used[7], "Chorus")

    def test_new_finds_a_free_gap_when_end_is_full(self):
        self.current = 10
        self.used[10] = "End"
        self.bridge.cmd_patterns({"action": "new", "name": "New"})
        self.assertEqual(self.current, 1)
        self.assertEqual(self.used[10], "End")

    def test_full_project_fails_without_replacing_any_pattern(self):
        self.used = {i: str(i) for i in range(1, self.maximum + 1)}
        before = self.used.copy()
        with self.assertRaisesRegex(ValueError, "no unused"):
            self.bridge.cmd_patterns({"action": "new", "name": "New"})
        self.assertEqual(self.used, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
