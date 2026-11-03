"""Read-only connection diagnostics, shared by MCP and the CLI."""
import os
import sys
from pathlib import Path

from . import __version__, paths
from .errors import FLError
from .midi_transport import list_ports

MIN_BRIDGE_VERSION = 8


def saved_midi_settings(out_port, in_port):
    """Saved registry settings are advisory: FL may have unsaved live changes."""
    import winreg

    def values(key_path, names):
        result = {}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                for name in names:
                    try:
                        result[name] = winreg.QueryValueEx(key, name)[0]
                    except OSError:
                        pass
        except OSError:
            pass
        return result

    def subkeys(key_path):
        names = []
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    names.append(winreg.EnumKey(key, i))
        except OSError:
            pass
        return names

    root = r"Software\Image-Line"
    result = []
    for name in subkeys(root):
        if not name.startswith("FL Studio ") or not name[10:].isdigit():
            continue
        devices = f"{root}\\{name}\\Devices"
        result.append({
            "version": name,
            "input": values(f"{devices}\\MIDI input\\{out_port}", ("Port", "Enabled", "ScriptFolder")),
            "reply_input": values(f"{devices}\\MIDI input\\{in_port}", ("Enabled",)),
            "outputs": {n: values(f"{devices}\\MIDI output\\{n}", ("Port",))
                        for n in subkeys(f"{devices}\\MIDI output")},
        })
    return result


def routing_issues(settings, out_port, in_port):
    issues = []
    for entry in settings:
        port = str(entry["input"].get("Port", "-1"))
        if port in ("-1", ""):
            continue
        outputs = entry["outputs"]
        if str(outputs.get(in_port, {}).get("Port", "-1")) != port:
            issues.append(f"{entry['version']}: input {out_port!r} and output {in_port!r} need the same port number.")
        conflicting = [name for name, value in outputs.items()
                       if name != in_port and str(value.get("Port", "-1")) == port]
        if conflicting:
            issues.append(f"{entry['version']}: output port {port} is also assigned to {conflicting!r}; "
                          f"leave only {in_port!r} on that output port to avoid misrouted replies.")
        if str(entry.get("reply_input", {}).get("Enabled", "0")) == "1":
            issues.append(f"{entry['version']}: disable input {in_port!r} in FL; the MCP server receives that port.")
    return issues


def collect_status(session, probe=True):
    checks = []
    actions = []

    def check(name, passed, detail=""):
        checks.append({"name": name, "ok": bool(passed), "detail": detail})

    result = {
        "server": {"version": __version__, "pid": os.getpid(), "executable": sys.executable,
                   "packaged": bool(getattr(sys, "frozen", False)), "module": str(Path(__file__).resolve())},
        "checks": checks, "next_steps": actions,
        "connection": {"state": "not_probed", "out_port": session.out_port, "in_port": session.in_port},
    }
    try:
        ins, outs = list_ports()
        result["ports"] = {"inputs": ins, "outputs": outs}
        check("midi_output", session.out_port in outs, session.out_port)
        check("midi_input", session.in_port in ins, session.in_port)
        have_ports = session.out_port in outs and session.in_port in ins
        if not have_ports:
            result["connection"]["state"] = "missing_ports"
            actions.append(f"Start loopMIDI and create both {session.out_port!r} and {session.in_port!r}; keep loopMIDI running.")
    except OSError as exc:
        have_ports = False
        check("midi_ports", False, str(exc))
        result["connection"]["state"] = "port_error"

    for name, path in [("fl_user_directory", paths.fl_user_dir()),
                       ("bridge_script", paths.bridge_script_path()),
                       ("piano_roll_script", paths.piano_roll_script_path())]:
        present = path.is_dir() if name == "fl_user_directory" else path.is_file()
        check(name, present, str(path))
        if not present:
            actions.append(f"Missing {path}; check FLSTUDIO_USER_DIR and run fl-studio-mcp install.")

    settings = saved_midi_settings(session.out_port, session.in_port)
    result["saved_midi_settings"] = {"source": "registry; may differ from the running FL settings",
                                     "versions": settings,
                                     "warnings": routing_issues(settings, session.out_port, session.in_port)}
    if probe and have_ports:
        try:
            info = session._call("info", timeout=2.0)
            result["fl"] = info
            check("fl_responds", True)
            compatible = int(info.get("bridge_version", 1)) >= MIN_BRIDGE_VERSION
            check("bridge_version", compatible, str(info.get("bridge_version", 1)))
            result["connection"]["state"] = "connected" if compatible else "outdated_bridge"
            if not compatible:
                actions.append("Run fl-studio-mcp install, then reload the bridge script inside FL Studio.")
        except (FLError, ValueError, TypeError) as exc:
            check("fl_responds", False, str(exc))
            result["connection"].update(state="unreachable", error=str(exc))
            actions.extend(result["saved_midi_settings"]["warnings"])
            actions.append(f"Open FL Studio. In MIDI settings enable input {session.out_port!r} with "
                           f"{paths.BRIDGE_NAME!r}; pair its port with output {session.in_port!r}. "
                           "Ensure no other output uses that port, then check View > Script output.")
    result["ok"] = result["connection"]["state"] == "connected" and all(c["ok"] for c in checks)
    return result
