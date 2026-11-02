"""
fl-studio-mcp — MCP server that lets any MCP-capable assistant arrange in a running FL Studio.

  fl-studio-mcp setup                one-time setup: loopMIDI ports, FL scripts + MIDI settings, MCP clients
  fl-studio-mcp                      run the MCP server over stdio (what MCP clients launch)
  fl-studio-mcp serve --transport http [--port 47821]
  fl-studio-mcp install              copy the FL-side scripts into FL Studio (part of setup)
  fl-studio-mcp doctor               check ports, scripts and the connection to FL
  fl-studio-mcp ports                list MIDI ports
"""

import argparse
import json
import os
import sys

from . import __version__, paths
from .midi_transport import DEFAULT_IN_PORT, DEFAULT_OUT_PORT, list_ports


def _port_args(p):
    p.add_argument("--out-port", default=os.environ.get("FLSTUDIO_MCP_OUT_PORT", DEFAULT_OUT_PORT),
                   help=f"loopMIDI port FL listens on (default {DEFAULT_OUT_PORT!r}, env FLSTUDIO_MCP_OUT_PORT)")
    p.add_argument("--in-port", default=os.environ.get("FLSTUDIO_MCP_IN_PORT", DEFAULT_IN_PORT),
                   help=f"loopMIDI port FL replies on (default {DEFAULT_IN_PORT!r}, env FLSTUDIO_MCP_IN_PORT)")


def cmd_serve(a):
    from .server import McpServer, run_stdio
    from .tools import FLSession

    session = FLSession(a.out_port, a.in_port)
    server = McpServer(session)
    try:
        if a.transport == "http":
            from .http_transport import run_http
            run_http(server, a.host, a.port)
        else:
            run_stdio(server)
    finally:
        session.close()


def cmd_setup(a):
    from .setup_wizard import run

    ok = run(clients=a.clients, assume_yes=a.yes, dry_run=a.dry_run, out_port=a.out_port, in_port=a.in_port,
             port_number=a.port_number, skip_fl=a.skip_fl)
    sys.exit(0 if ok else 1)


def cmd_install(a):
    from .installer import install

    for line in install():
        print(line)
    print(f"\nIn FL Studio: Options → MIDI settings, select input {a.out_port!r}, set Controller type to "
          f"'{paths.BRIDGE_NAME}', enable it, and give it the same port number as output {a.in_port!r}. "
          f"If it was already selected, reload the script (View → Script output → Reload script).")


def cmd_ports(a):
    ins, outs = list_ports()
    print("MIDI inputs: ", ins)
    print("MIDI outputs:", outs)


def cmd_doctor(a):
    from .tools import FLSession
    session = FLSession(a.out_port, a.in_port)
    try:
        report = session.status()
    finally:
        session.close()
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"fl-studio-mcp {report['server']['version']} — {report['server']['executable']}")
        for item in report["checks"]:
            print(f"[{'ok' if item['ok'] else '!!'}] {item['name']}" +
                  (f" — {item['detail']}" if item['detail'] else ""))
        print(f"Connection: {report['connection']['state']}")
        for action in report["next_steps"]:
            print(f"  → {action}")
        for warning in report["saved_midi_settings"]["warnings"]:
            print(f"[saved settings, possibly stale] {warning}")
    sys.exit(0 if report["ok"] else 1)


def cmd_call(a):
    """Debug helper: call one MCP tool and print the result."""
    from .server import McpServer
    from .tools import FLSession

    session = FLSession(a.out_port, a.in_port)
    try:
        resp = McpServer(session).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                          "params": {"name": a.tool, "arguments": json.loads(a.arguments)}})
        result = resp["result"]
        text = result["content"][0]["text"]
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except ValueError:
            pass
        print(text)
        sys.exit(1 if result.get("isError") else 0)
    finally:
        session.close()


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="fl-studio-mcp", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"fl-studio-mcp {__version__}")
    sub = ap.add_subparsers(dest="command")

    p = sub.add_parser('recreation-worker', help=argparse.SUPPRESS)
    p.add_argument('folder')
    p.set_defaults(fn=lambda a: __import__('fl_studio_mcp.recreation', fromlist=['worker_main']).worker_main(a.folder))

    p = sub.add_parser("serve", help="run the MCP server (default)")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=47821)
    _port_args(p)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("setup", help="one-time setup (loopMIDI ports, FL scripts and MIDI settings, MCP clients)")
    p.add_argument("--clients", default="ask",
                   help="ask (default), all, none, or a comma list of: claude-desktop,cursor,windsurf,vscode,cline,codex,claude-code")
    p.add_argument("--yes", "-y", action="store_true", help="answer yes to every question")
    p.add_argument("--dry-run", action="store_true", help="only report what would be done")
    p.add_argument("--skip-fl", action="store_true", help="don't touch FL's MIDI settings")
    p.add_argument("--port-number", type=int, default=10, help="FL MIDI port number to pair input/output on (default 10)")
    _port_args(p)
    p.set_defaults(fn=cmd_setup)

    for name, fn, help_ in [("install", cmd_install, "install the FL-side scripts"),
                            ("doctor", cmd_doctor, "check the setup"),
                            ("ports", cmd_ports, "list MIDI ports")]:
        p = sub.add_parser(name, help=help_)
        _port_args(p)
        if name == "doctor":
            p.add_argument("--json", action="store_true", help="print a structured diagnostic report")
        p.set_defaults(fn=fn)

    p = sub.add_parser("call", help="call one tool, e.g. call fl_get_project")
    p.add_argument("tool")
    p.add_argument("arguments", nargs="?", default="{}", help="JSON arguments")
    _port_args(p)
    p.set_defaults(fn=cmd_call)

    a = ap.parse_args(argv)
    if a.command is None:
        a = ap.parse_args(["serve"] + list(argv or sys.argv[1:]))
    a.fn(a)


if __name__ == "__main__":
    main()
