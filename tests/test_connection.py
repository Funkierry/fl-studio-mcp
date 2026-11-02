"""Connection failures must never replay an uncertain write in FL Studio."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from fl_studio_mcp.tools import FLSession, FLError


class RetryTests(unittest.TestCase):
    def test_lost_write_reply_is_not_replayed(self):
        for command, args in [("undo", {}), ("patterns", {"action": "new"}),
                              ("transport", {"action": "toggle"}), ("play", {}),
                              ("future_write", {})]:
            with self.subTest(command=command):
                first = Mock()
                first.call.side_effect = TimeoutError("reply lost after FL executed the command")
                second = Mock()
                second.call.return_value = {"ok": True, "result": {}}
                with patch("fl_studio_mcp.tools.FLMidiBridge", side_effect=[first, second]) as factory:
                    session = FLSession()
                    with self.assertRaisesRegex(FLError, "may already have run"):
                        session._call(command, **args)
                    self.assertEqual(factory.call_count, 1)
                    first.call.assert_called_once()
                    first.close.assert_called_once()
                    self.assertIsNone(session.bridge)

    def test_read_reconnects_after_lost_reply(self):
        for command, args in [("project", {}), ("info", {}), ("patterns", {"action": "list"})]:
            with self.subTest(command=command):
                first, second = Mock(), Mock()
                first.call.side_effect = TimeoutError("lost")
                second.call.return_value = {"ok": True, "result": {"retried": True}}
                with patch("fl_studio_mcp.tools.FLMidiBridge", side_effect=[first, second]):
                    session = FLSession()
                    self.assertEqual(session._call(command, **args), {"retried": True})
                    session.close()

    def test_open_failure_can_retry_before_a_write_is_sent(self):
        bridge = Mock()
        bridge.call.return_value = {"ok": True, "result": {"once": True}}
        with patch("fl_studio_mcp.tools.FLMidiBridge", side_effect=[OSError("open failed"), bridge]):
            session = FLSession()
            self.assertEqual(session.undo(), {"once": True})
            bridge.call.assert_called_once()
            session.close()

    def test_read_retry_is_bounded(self):
        bridge = Mock()
        bridge.call.side_effect = TimeoutError("offline")
        with patch("fl_studio_mcp.tools.FLMidiBridge", return_value=bridge) as factory:
            with self.assertRaises(FLError):
                FLSession().project()
            self.assertEqual(factory.call_count, 2)

    def test_fl_error_is_not_retried(self):
        bridge = Mock()
        bridge.call.return_value = {"ok": False, "error": "invalid channel"}
        with patch("fl_studio_mcp.tools.FLMidiBridge", return_value=bridge):
            session = FLSession()
            with self.assertRaisesRegex(FLError, "invalid channel"):
                session.undo()
            bridge.call.assert_called_once()
            session.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
