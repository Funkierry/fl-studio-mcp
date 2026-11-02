"""Status remains useful without FL, missing ports, or a responding bridge."""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from fl_studio_mcp import __version__, diagnostics, cli
from fl_studio_mcp.errors import FLError
from fl_studio_mcp.server import McpServer
from fl_studio_mcp.tools import FLSession


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.ports = stack.enter_context(patch.object(diagnostics, "list_ports", return_value=(
            ["FL Agent Reply"], ["FL Agent"])))
        self.saved = stack.enter_context(patch.object(diagnostics, "saved_midi_settings", return_value=[]))
        for name in ("fl_user_dir", "bridge_script_path", "piano_roll_script_path"):
            stack.enter_context(patch.object(diagnostics.paths, name, return_value=Path(__file__)))
        stack.enter_context(patch.object(Path, "is_dir", return_value=True))
        self.session = FLSession()
        self.session._call = Mock(return_value={"bridge_version": 7, "tempo": 130})

    def test_connected_report_contains_runtime_identity(self):
        status = self.session.status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["connection"]["state"], "connected")
        self.assertEqual(status["server"]["version"], __version__)
        self.assertEqual(status["server"]["executable"], sys.executable)

    def test_missing_ports_does_not_attempt_connection(self):
        self.ports.return_value = ([], [])
        status = self.session.status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["connection"]["state"], "missing_ports")
        self.session._call.assert_not_called()
        self.assertIn("loopMIDI", " ".join(status["next_steps"]))

    def test_probe_false_does_not_claim_a_live_connection(self):
        status = self.session.status(probe=False)
        self.assertEqual(status["connection"]["state"], "not_probed")
        self.assertFalse(status["ok"])
        self.session._call.assert_not_called()

    def test_reply_failure_is_reported_not_raised(self):
        self.session._call.side_effect = FLError("no reply")
        status = self.session.status()
        self.assertEqual(status["connection"]["state"], "unreachable")
        self.assertIn("no reply", status["connection"]["error"])
        self.assertTrue(status["next_steps"])

    def test_outdated_bridge_requires_reload(self):
        self.session._call.return_value = {"bridge_version": 4}
        status = self.session.status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["connection"]["state"], "outdated_bridge")
        self.assertIn("reload", " ".join(status["next_steps"]))

    def test_duplicate_output_and_enabled_reply_input_are_detected(self):
        settings = [{"version": "FL Studio 24", "input": {"Port": "10"},
                     "reply_input": {"Enabled": "1"},
                     "outputs": {"FL Agent": {"Port": "10"}, "FL Agent Reply": {"Port": "10"}}}]
        issues = diagnostics.routing_issues(settings, "FL Agent", "FL Agent Reply")
        self.assertEqual(len(issues), 2)
        self.assertIn("misrouted", issues[0])
        self.saved.return_value = settings
        self.assertTrue(self.session.status()["ok"], "saved registry may differ from live working settings")

    def test_reply_port_mismatch_is_detected(self):
        settings = [{"version": "FL Studio 24", "input": {"Port": "10"},
                     "outputs": {"FL Agent Reply": {"Port": "11"}}}]
        self.assertIn("same port", diagnostics.routing_issues(settings, "FL Agent", "FL Agent Reply")[0])

    def test_mcp_status_works_when_fl_is_missing(self):
        self.ports.return_value = ([], [])
        result = McpServer(self.session).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "fl_get_status", "arguments": {}}})["result"]
        self.assertFalse(result.get("isError", False))
        self.assertEqual(json.loads(result["content"][0]["text"])["connection"]["state"], "missing_ports")

    def test_doctor_json_is_parseable_and_exit_code_matches_health(self):
        for healthy in (True, False):
            with self.subTest(healthy=healthy):
                self.ports.return_value = (["FL Agent Reply"], ["FL Agent"]) if healthy else ([], [])
                output = io.StringIO()
                with patch("fl_studio_mcp.tools.FLSession", return_value=self.session), \
                     contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
                    cli.cmd_doctor(Mock(out_port="FL Agent", in_port="FL Agent Reply", json=True))
                self.assertEqual(stopped.exception.code, 0 if healthy else 1)
                self.assertEqual(json.loads(output.getvalue())["ok"], healthy)


if __name__ == "__main__":
    unittest.main(verbosity=2)
