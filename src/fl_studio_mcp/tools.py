"""
High-level FL operations: turns assistant-friendly arguments (note names, beats, 0–1 velocity,
drum step strings) into bridge commands. Used by the MCP server and the CLI.
"""

import math
import os
import re
import tempfile
import threading
import time

from . import paths
from .midi_transport import DEFAULT_IN_PORT, DEFAULT_OUT_PORT, FLMidiBridge

DATA_MARKER = "# @@FLAGENT_DATA@@"
NOTE_NAMES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
NAMES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


from .errors import FLError  # noqa: F401  (re-exported)


def parse_pitch(p):
    """Note name or MIDI number → MIDI number. C4 = 60 (scientific pitch; FL's UI labels the same note C5)."""
    if isinstance(p, bool):
        raise FLError("pitch must be an integer MIDI number from 0 to 127, or a note name")
    if isinstance(p, (int, float)):
        if isinstance(p, float) and (not math.isfinite(p) or not p.is_integer()):
            raise FLError(f"pitch must be a finite integer MIDI number: {p!r}")
        pitch = int(p)
    else:
        s = str(p).strip()
        if s.lstrip("-").isdigit():
            pitch = int(s)
        else:
            m = re.fullmatch(r"([A-Ga-g])([#b♯♭]*)(-?\d+)", s)
            if not m:
                raise FLError(f"cannot parse pitch {p!r}; use a MIDI number or a name like 'C#3'")
            letter, acc, octave = m.groups()
            n = NOTE_NAMES[letter.upper()] + acc.count("#") + acc.count("♯") - acc.count("b") - acc.count("♭")
            pitch = (int(octave) + 1) * 12 + n
    if not 0 <= pitch <= 127:
        raise FLError(f"pitch must be in MIDI range 0–127: {p!r}")
    return pitch


def pitch_name(n):
    return f"{NAMES_SHARP[n % 12]}{n // 12 - 1}"


def _finite_number(value, name):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as e:
        raise FLError(f"{name} must be a finite number: {value!r}") from e
    if isinstance(value, bool) or not math.isfinite(number):
        raise FLError(f"{name} must be a finite number: {value!r}")
    return number


def normalize_notes(notes):
    out = []
    for n in notes:
        if not isinstance(n, dict):
            raise FLError(f"each note must be an object: {n!r}")
        length = _finite_number(n.get("length", 1.0), "note length")
        if length <= 0:
            raise FLError(f"note length must be > 0: {n}")
        start = _finite_number(n["start"], "note start")
        if start < 0 or not math.isfinite(start + length):
            raise FLError(f"note start must be >= 0 and its end must be finite: {n}")
        vel = _finite_number(n.get("velocity", 0.8), "note velocity")
        if not 0 <= vel <= 127:
            raise FLError(f"note velocity must be 0–1 or a MIDI velocity from 0–127: {n}")
        if vel > 1.0:  # also accept 0–127
            vel /= 127.0
        out.append({"pitch": parse_pitch(n["pitch"]), "start": start, "length": length,
                    "velocity": round(vel, 3)})
    return out


def clean_steps(s):
    return re.sub(r"[\s|]", "", str(s))


def render_piano_roll_script(payload):
    """FL's sandbox can't read files, so the notes are embedded into the piano roll script itself."""
    template = paths.packaged_script(f"{paths.PIANO_ROLL_SCRIPT}.pyscript")
    line = next(l for l in template.splitlines() if DATA_MARKER in l)
    path = paths.piano_roll_script_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    source = template.replace(line, f"DATA = {payload!r}  {DATA_MARKER}")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as f:
            temporary = f.name
            f.write(source)
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
    return path


class FLSession:
    """One connection to FL, opened lazily and reopened after errors. Thread-safe."""

    def __init__(self, out_port=DEFAULT_OUT_PORT, in_port=DEFAULT_IN_PORT):
        self.out_port, self.in_port = out_port, in_port
        self.bridge = None
        self.lock = threading.Lock()
        self._ears = None
        self._recreation = None

    @property
    def recreation(self):
        if self._recreation is None:
            from .recreation import Recreation
            self._recreation = Recreation(self)
        return self._recreation

    @property
    def ears(self):
        """Listening features (recording, analysis, matching), created on first use."""
        if self._ears is None:
            from .ears import Ears
            self._ears = Ears(self)
        return self._ears

    def _call(self, cmd, timeout=8.0, **args):
        retry_read = cmd in {"ping", "info", "project", "channels", "params", "get_steps",
                             "pr_report", "get_param_values", "mixer"}
        retry_read |= cmd == "patterns" and args.get("action", "list") == "list"
        with self.lock:
            for attempt in (1, 2):
                dispatched = False
                try:
                    if self.bridge is None:
                        self.bridge = FLMidiBridge(self.out_port, self.in_port)
                    dispatched = True
                    resp = self.bridge.call(cmd, timeout=timeout, **args)
                    break
                except (OSError, ConnectionError, TimeoutError) as e:
                    self._close()
                    if dispatched and not retry_read:
                        raise FLError(
                            f"FL command {cmd!r} may already have run; its result could not be confirmed. "
                            f"It was not retried. Inspect FL's current state before repeating it. Cause: {e}") from e
                    if attempt == 2:
                        raise FLError(f"cannot reach FL Studio: {e}") from e
        if not resp.get("ok"):
            err = str(resp.get("error"))
            if err.startswith("unknown cmd"):
                err += " — the bridge script in FL is outdated: run `fl-studio-mcp install`, then reload the script in FL"
            raise FLError(err)
        return resp["result"]

    def _close(self):
        if self.bridge is not None:
            try:
                self.bridge.close()
            except Exception:
                pass
            self.bridge = None

    def close(self):
        with self.lock:
            self._close()

    # ---------------------------------------------------------------- read
    def status(self, probe=True):
        from .diagnostics import collect_status
        return collect_status(self, probe=probe)

    def info(self):
        return self._call("info")

    def project(self):
        return self._call("project")

    def tempo(self):
        return float(self._call("info")["tempo"])

    @staticmethod
    def _target(channel=None, mixer_track=None, slot=None):
        if mixer_track is not None:
            return {"mixer_track": int(mixer_track), "slot": int(slot or 0)}
        if channel is None:
            raise FLError("give a channel, or a mixer_track and slot")
        return {"channel": int(channel)}

    def list_params(self, channel=None, query=None, limit=80, mixer_track=None, slot=None):
        r = self._call("params", timeout=20, limit=5000, **self._target(channel, mixer_track, slot))
        params = r["params"]
        if query:
            q = query.lower()
            params = [p for p in params if q in str(p["name"]).lower()]
        r["params"] = [{"param": p["param"], "name": p["name"], "value": round(p["value"], 4),
                        "display": p["value_str"]} for p in params[:limit]]
        r["shown"] = len(r["params"])
        return r

    def get_steps(self, channel, length=16):
        return self._call("get_steps", channel=channel, length=length)

    def piano_roll_report(self):
        rep = self._call("pr_report")["shared_interpreter_report"]
        if rep is None:
            return {"available": False,
                    "hint": f"The '{paths.PIANO_ROLL_SCRIPT}' piano roll script has not run since the bridge "
                            f"loaded, or FL runs it in a separate interpreter so it cannot report back. "
                            f"After running it, check its result dialog and the 'FL MCP REPORT' debug log "
                            f"for the actual note count; unavailable does not mean the write failed."}
        rep = dict(rep, available=True)
        rep["notes"] = [dict(n, name=pitch_name(n["pitch"])) for n in rep.get("notes", [])]
        return rep

    # ---------------------------------------------------------------- write
    def set_params(self, channel=None, changes=(), mixer_track=None, slot=None, undo=True):
        return self._call("set_params", changes=list(changes), undo=undo, **self._target(channel, mixer_track, slot))

    def mixer(self, tracks=None):
        return self._call("mixer", tracks=tracks) if tracks else self._call("mixer")

    def set_mixer(self, track, volume=None, eq=None):
        return self._call("mixer_set", track=track, volume=volume, eq=eq or [])

    def audition(self, channel, notes, bpm=None):
        notes = normalize_notes(notes)
        bpm = bpm or self.tempo()
        ms = 60000.0 / bpm
        events = [{"pitch": n["pitch"], "at_ms": n["start"] * ms, "len_ms": n["length"] * ms * 0.95,
                   "velocity": round(n["velocity"] * 127)} for n in notes]
        r = self._call("play", channel=channel, events=events)
        r["duration_s"] = round(max(n["start"] + n["length"] for n in notes) * ms / 1000, 2) if notes else 0
        return r

    def stop_audition(self):
        return self._call("stop_audition")

    def write_notes(self, channel, notes, mode="merge", label=None):
        notes = normalize_notes(notes)
        if mode not in ("merge", "replace"):
            raise FLError(f"unknown note mode {mode!r}; use 'merge' or 'replace'")
        payload = {"stamp": time.time(), "mode": mode, "label": label or f"{len(notes)} notes", "notes": notes}
        pr = self._call("open_piano_roll", channel=channel)
        self._call("hint", text=f"Piano roll → Tools → Scripting → {paths.PIANO_ROLL_SCRIPT} ({len(notes)} notes)")
        # Commit only after FL has confirmed the target. The embedded file is the sole
        # payload: a second MIDI-side staging write could succeed even if its reply is lost.
        script = render_piano_roll_script(payload)
        return {
            "staged": len(notes),
            "span_beats": max(n["start"] + n["length"] for n in notes) if notes else 0,
            "mode": mode,
            "script": str(script),
            "piano_roll": pr,
            "user_action_required": (
                f"Ask the user to run the piano roll script '{paths.PIANO_ROLL_SCRIPT}' "
                f"(piano roll menu → Tools → Scripting) in the piano roll that just opened. "
                f"The notes are not in the project until they do. Running it twice does not duplicate notes."),
        }

    def write_drums(self, tracks, clear_to=0):
        tracks = [{"channel": int(t["channel"]), "steps": clean_steps(t["steps"])} for t in tracks]
        return self._call("drums", tracks=tracks, clear_to=clear_to)

    def patterns(self, action="list", index=None, name=None):
        args = {"action": action}
        if index is not None:
            args["index"] = index
        if name is not None:
            args["name"] = name
        return self._call("patterns", **args)

    def set_tempo(self, bpm):
        return self._call("set_tempo", bpm=bpm)

    def transport(self, action):
        return self._call("transport", action=action)

    def select_channel(self, channel):
        return self._call("select_channel", channel=channel)

    def rename_channel(self, channel, name):
        return self._call("rename_channel", channel=channel, name=name)

    def undo(self):
        return self._call("undo")
