"""
`fl-studio-mcp setup`: everything needed to go from "installed" to "my assistant can use FL", in one command.

  1. Python dependencies for the listening features (numpy, scipy)
  2. loopMIDI and its two virtual ports (ports are written to loopMIDI's registry key and loopMIDI is (re)started)
  3. FL-side scripts (bridge + piano roll script)
  4. FL's MIDI settings (controller type, port numbers) — written to FL's registry key, so FL must be closed
  5. MCP client configs for the clients found on this machine (backups are kept)
  6. A final check

Every step is idempotent and reports what it did; nothing outside this list is touched.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import winreg
from pathlib import Path

from . import paths
from .midi_transport import DEFAULT_IN_PORT, DEFAULT_OUT_PORT, list_ports

LOOPMIDI_URL = "https://www.tobias-erichsen.de/software/loopmidi.html"
LOOPMIDI_KEY = r"Software\Tobias Erichsen\loopMIDI"
FL_KEY_PREFIX = r"Software\Image-Line"
SERVER_NAME = "fl-studio"
PORT_NUMBER = 10


# ---------------------------------------------------------------- small helpers

def _say(status, text):
    mark = {"ok": "[ok]", "do": "[->]", "warn": "[!!]", "skip": "[--]", "ask": "[??]"}[status]
    print(f"{mark} {text}")


def _confirm(question, assume_yes):
    if assume_yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        return False
    return input(f"[??] {question} [y/N] ").strip().lower() in ("y", "yes", "是", "好")


def _running(exe_name):
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH"], capture_output=True, text=True,
                         errors="replace").stdout
    return exe_name.lower() in out.lower()


def server_command():
    """The command an MCP client should launch, for however this copy is installed."""
    if getattr(sys, "frozen", False):                       # PyInstaller exe
        return sys.executable, []
    script = Path(sys.argv[0]).resolve()
    if script.name.lower().startswith("fl-studio-mcp") and script.suffix.lower() == ".py":  # source checkout
        return sys.executable, [script.as_posix()]
    exe = shutil.which("fl-studio-mcp")                     # pip/pipx/uv tool install
    if exe:
        return exe, []
    return sys.executable, ["-m", "fl_studio_mcp"]


# ---------------------------------------------------------------- 1. dependencies

def check_dependencies():
    missing = []
    for mod in ("numpy", "scipy"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        _say("warn", f"listening features need {', '.join(missing)}: `{Path(sys.executable).name} -m pip install "
                     f"{' '.join(missing)}` (the basic FL control tools work without them)")
        return False
    _say("ok", "numpy and scipy available (listening features enabled)")
    return True


# ---------------------------------------------------------------- 2. loopMIDI

def loopmidi_exe():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base:
            p = Path(base) / "Tobias Erichsen" / "loopMIDI" / "loopMIDI.exe"
            if p.exists():
                return p
    return None


def setup_loopmidi(port_names, assume_yes):
    exe = loopmidi_exe()
    if exe is None:
        _say("warn", f"loopMIDI is not installed. Install it from {LOOPMIDI_URL}, then run setup again.")
        if _confirm("Open the download page now?", False):
            os.startfile(LOOPMIDI_URL)  # noqa: S606 — opens the user's browser
        return False
    ins, outs = list_ports()
    if all(n in ins and n in outs for n in port_names):
        _say("ok", f"loopMIDI ports present: {', '.join(port_names)}")
        return True
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, LOOPMIDI_KEY + r"\Ports")
    for n in port_names:
        winreg.SetValueEx(key, n, 0, winreg.REG_DWORD, 1)
    winreg.CloseKey(key)
    _say("do", f"registered loopMIDI ports: {', '.join(port_names)}")
    if _running("loopMIDI.exe"):
        if not _confirm("loopMIDI must restart to create the ports (programs using its ports will lose them). Restart?",
                        assume_yes):
            _say("warn", "restart loopMIDI yourself to create the ports")
            return False
        subprocess.run(["taskkill", "/IM", "loopMIDI.exe", "/F"], capture_output=True)
        time.sleep(1.0)
    subprocess.Popen([str(exe)], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0), close_fds=True)
    for _ in range(20):
        time.sleep(0.5)
        ins, outs = list_ports()
        if all(n in ins and n in outs for n in port_names):
            _say("ok", "loopMIDI started with the ports")
            return True
    _say("warn", "loopMIDI started but the ports did not appear; check its window")
    return False


# ---------------------------------------------------------------- 3. FL scripts

def setup_scripts():
    from .installer import install
    try:
        for line in install():
            _say("do" if not line.startswith("could not") else "warn", line)
        return True
    except FileNotFoundError as e:
        _say("warn", str(e))
        return False


# ---------------------------------------------------------------- 4. FL MIDI settings

def fl_version_keys():
    """Registry keys of installed FL versions, e.g. 'Software\\Image-Line\\FL Studio 24'."""
    keys = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, FL_KEY_PREFIX)
    except OSError:
        return keys
    i = 0
    while True:
        try:
            name = winreg.EnumKey(root, i)
        except OSError:
            break
        if name.startswith("FL Studio") and name[len("FL Studio"):].strip().isdigit():
            keys.append(f"{FL_KEY_PREFIX}\\{name}")
        i += 1
    return sorted(keys, key=lambda k: int(k.rsplit(" ", 1)[1]))


def _set_strings(subkey, values):
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, subkey)
    for name, value in values.items():
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))
    winreg.CloseKey(key)


def _get_string(subkey, name):
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey)
        value = winreg.QueryValueEx(key, name)[0]
        winreg.CloseKey(key)
        return value
    except OSError:
        return None


def _parking_output_port(dev, out_port, port_number):
    """Keep the unused same-name output off the reply port, including after restart."""
    existing = _get_string(f"{dev}\\MIDI output\\{out_port}", "Port")
    try:
        if 0 <= int(existing) <= 255 and int(existing) != int(port_number):
            return int(existing)
    except (ValueError, TypeError):
        pass
    used = {int(port_number)}
    for direction in ('input', 'output'):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{dev}\\MIDI {direction}") as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    name = winreg.EnumKey(key, i)
                    value = _get_string(f"{dev}\\MIDI {direction}\\{name}", 'Port')
                    try:
                        used.add(int(value))
                    except (ValueError, TypeError):
                        pass
        except FileNotFoundError:
            pass
    candidates = [int(port_number)-1] + list(range(255, -1, -1))
    return next((p for p in candidates if 0 <= p <= 255 and p not in used), None)


def setup_fl_midi(out_port, in_port, port_number, assume_yes):
    keys = fl_version_keys()
    if not keys:
        _say("warn", "FL Studio settings not found in the registry; start FL once, close it, and run setup again")
        return False
    if _running("FL64.exe") or _running("FL.exe"):
        current = [k for k in keys if _get_string(f"{k}\\Devices\\MIDI input\\{out_port}", "ScriptFolder") == paths.BRIDGE_NAME]
        if current:
            _say("ok", f"FL MIDI settings already point at '{paths.BRIDGE_NAME}' (FL is running; not touched)")
            return True
        _say("warn", "FL Studio is running. Save your project, close FL, then run setup again so it can configure FL's "
                     f"MIDI settings — or do it by hand: F10 → input '{out_port}' → controller type "
                     f"'{paths.BRIDGE_NAME}', enable, port {port_number}; output '{in_port}' port {port_number}.")
        return False
    for k in keys:
        dev = f"{k}\\Devices"
        parking = _parking_output_port(dev, out_port, port_number)
        if parking is None:
            _say('warn', 'No unused MIDI port is available for the same-name output; settings not changed')
            return False
        _set_strings(f"{dev}\\MIDI input\\{out_port}", {"Type": "-3", "ScriptFolder": paths.BRIDGE_NAME,
                                                         "IDString": "-", "Port": port_number, "Enabled": "1"})
        _set_strings(f"{dev}\\MIDI input\\{in_port}", {"Type": "0", "ScriptFolder": "", "IDString": "-",
                                                        "Port": "-1", "Enabled": "0"})
        _set_strings(f"{dev}\\MIDI output\\{in_port}", {"Port": port_number, "Sync": "0", "SendAllNotesOff": "1",
                                                         "IDString": "-"})
        # FL 24 can auto-pair an unassigned same-name output back to the input's
        # port on restart. An explicit spare port prevents this reply conflict.
        _set_strings(f"{dev}\\MIDI output\\{out_port}", {"Port": parking, "Sync": "0"})
        _say("do", f"{k.rsplit(chr(92), 1)[1]}: input '{out_port}' → {paths.BRIDGE_NAME} (port {port_number}), "
                   f"output '{in_port}' → port {port_number}; unused output '{out_port}' → spare port {parking}")
    return True


# ---------------------------------------------------------------- 5. MCP clients

def _home():
    return Path.home()


def _appdata():
    return Path(os.environ.get("APPDATA", _home() / "AppData" / "Roaming"))


CLIENTS = [
    # id, display name, config file, format, directory whose existence means "installed"
    ("claude-desktop", "Claude Desktop", lambda: _appdata() / "Claude" / "claude_desktop_config.json", "mcpServers",
     lambda: _appdata() / "Claude"),
    ("cursor", "Cursor", lambda: _home() / ".cursor" / "mcp.json", "mcpServers", lambda: _home() / ".cursor"),
    ("windsurf", "Windsurf", lambda: _home() / ".codeium" / "windsurf" / "mcp_config.json", "mcpServers",
     lambda: _home() / ".codeium" / "windsurf"),
    ("vscode", "VS Code (GitHub Copilot)", lambda: _appdata() / "Code" / "User" / "mcp.json", "servers",
     lambda: _appdata() / "Code" / "User"),
    ("cline", "Cline (VS Code)", lambda: _appdata() / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev"
     / "settings" / "cline_mcp_settings.json", "mcpServers",
     lambda: _appdata() / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev"),
    ("codex", "Codex CLI", lambda: _home() / ".codex" / "config.toml", "toml", lambda: _home() / ".codex"),
    ("claude-code", "Claude Code", None, "claude-cli", lambda: Path(shutil.which("claude") or "/nonexistent")),
]


def _entry(fmt, command, args):
    if fmt == "servers":
        return {"type": "stdio", "command": command, "args": args}
    return {"command": command, "args": args}


def _toml_section(command, args):
    q = lambda s: json.dumps(s)  # TOML basic strings accept JSON-style escaping
    return f"[mcp_servers.{SERVER_NAME}]\ncommand = {q(command)}\nargs = [{', '.join(q(a) for a in args)}]\n"


def client_status(client):
    cid, name, path_fn, fmt, marker = client
    installed = marker().exists()
    configured = False
    if installed:
        if fmt == "claude-cli":
            # run from the home folder so a project-level .mcp.json doesn't count as "configured"
            r = subprocess.run(["claude", "mcp", "get", SERVER_NAME], capture_output=True, text=True, errors="replace",
                               cwd=str(_home()))
            configured = r.returncode == 0
        else:
            p = path_fn()
            if p.exists():
                text = p.read_text(encoding="utf-8", errors="replace")
                if fmt == "toml":
                    configured = f"[mcp_servers.{SERVER_NAME}]" in text
                else:
                    try:
                        configured = SERVER_NAME in (json.loads(text or "{}").get(fmt) or {})
                    except json.JSONDecodeError:
                        configured = False
    return installed, configured


def add_to_client(client, command, args):
    cid, name, path_fn, fmt, _ = client
    if fmt == "claude-cli":
        r = subprocess.run(["claude", "mcp", "add", "--scope", "user", SERVER_NAME, "--", command, *args],
                           capture_output=True, text=True, errors="replace", cwd=str(_home()))
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or r.stdout.strip())
        return "claude mcp add --scope user"
    if fmt == "toml" and shutil.which("codex"):
        # Codex's own command keeps its (app-managed) config.toml consistent
        r = subprocess.run(["codex", "mcp", "add", SERVER_NAME, "--", command, *args],
                           capture_output=True, text=True, errors="replace")
        if r.returncode == 0:
            return "codex mcp add"
    p = path_fn()
    p.parent.mkdir(parents=True, exist_ok=True)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    if p.exists():
        shutil.copy2(p, p.with_name(p.name + ".bak"))
    if fmt == "toml":
        section = _toml_section(command, args)
        p.write_text(text + ("\n" if text and not text.endswith("\n") else "") + "\n" + section, encoding="utf-8")
    else:
        data = json.loads(text) if text.strip() else {}
        data.setdefault(fmt, {})[SERVER_NAME] = _entry(fmt, command, args)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(p)


def setup_clients(selection, assume_yes, dry_run=False):
    command, args = server_command()
    _say("ok", f"server command: {command} {' '.join(args)}".rstrip())
    wanted = None if selection in (None, "ask", "all") else {s.strip() for s in selection.split(",")}
    if selection == "none":
        return True
    for client in CLIENTS:
        cid, name = client[0], client[1]
        if wanted is not None and cid not in wanted:
            continue
        installed, configured = client_status(client)
        if not installed:
            continue
        if configured:
            _say("ok", f"{name}: already configured")
            continue
        if dry_run:
            _say("skip", f"{name}: would add '{SERVER_NAME}'")
            continue
        if selection == "all" or (wanted is not None) or _confirm(f"Add '{SERVER_NAME}' to {name}?", assume_yes):
            try:
                where = add_to_client(client, command, args)
                _say("do", f"{name}: added ({where}); restart {name} to load it")
            except Exception as e:  # noqa: BLE001 — report and continue with the next client
                _say("warn", f"{name}: could not add automatically ({e})")
        else:
            _say("skip", f"{name}: skipped")
    _say("ok", "other clients (e.g. Cherry Studio): add a stdio MCP server with the command above, or run "
               "`fl-studio-mcp serve --transport http` and use http://127.0.0.1:47821/mcp")
    return True


# ---------------------------------------------------------------- entry point

def run(clients="ask", assume_yes=False, dry_run=False, out_port=DEFAULT_OUT_PORT, in_port=DEFAULT_IN_PORT,
        port_number=PORT_NUMBER, skip_fl=False):
    print("fl-studio-mcp setup\n")
    results = {}
    print("1) Python dependencies")
    results["deps"] = check_dependencies()
    print("\n2) loopMIDI")
    if dry_run:
        ins, outs = list_ports()
        have = [n for n in (out_port, in_port) if n in ins and n in outs]
        _say("ok" if len(have) == 2 else "skip", f"installed: {bool(loopmidi_exe())}, running: {_running('loopMIDI.exe')}, "
                                                  f"ports present: {have or 'none'}")
        results["loopmidi"] = True
    else:
        results["loopmidi"] = setup_loopmidi([out_port, in_port], assume_yes)
    print("\n3) FL Studio scripts")
    if dry_run:
        _say("skip", f"would install {paths.bridge_script_path()} and {paths.piano_roll_script_path()}")
        results["scripts"] = True
    else:
        results["scripts"] = setup_scripts()
    print("\n4) FL Studio MIDI settings")
    if dry_run:
        fl_up = _running("FL64.exe") or _running("FL.exe")
        for k in fl_version_keys():
            cur = _get_string(f"{k}\\Devices\\MIDI input\\{out_port}", "ScriptFolder")
            _say("skip", f"{k.rsplit(chr(92), 1)[1]}: input '{out_port}' controller = {cur!r}"
                         + (" (FL is running: setup would ask you to close it)" if fl_up else ""))
        results["fl_midi"] = True
    elif skip_fl:
        _say("skip", "skipped")
        results["fl_midi"] = True
    else:
        results["fl_midi"] = setup_fl_midi(out_port, in_port, port_number, assume_yes)
    print("\n5) MCP clients")
    results["clients"] = setup_clients(clients, assume_yes, dry_run)
    print()
    pending = [k for k, v in results.items() if not v]
    if pending:
        _say("warn", f"finish these and run setup again: {', '.join(pending)}")
    else:
        _say("ok", "done. Start FL Studio (and loopMIDI), restart your AI client, and ask it to look at your project. "
                   "Check anytime with `fl-studio-mcp doctor`.")
    return not pending
