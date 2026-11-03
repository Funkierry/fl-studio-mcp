"""MCP protocol core: tool/prompt definitions and JSON-RPC dispatch, independent of transport."""

import json
import sys
import traceback

from . import __version__, paths
from .errors import AudioError, FLError
from .tools import parse_pitch

SUPPORTED_PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

GUIDE = f"""\
You are operating the user's running FL Studio project through the fl-studio MCP server, helping them arrange.

Workflow
1. Call fl_get_project first: tempo, channels (index, name, plugin) and patterns. Always address channels by index.
   If it cannot connect, call fl_get_status for server version, MIDI ports, bridge health and recovery steps.
2. Before writing, say what you plan (key, chords, rhythm, register, which channel). For bigger changes,
   play it with fl_audition first and let the user approve.
3. Melodies, chords, bass: fl_write_notes. It stages the notes and opens the piano roll, but the user must run
   the piano roll script "{paths.PIANO_ROLL_SCRIPT}" (piano roll menu → Tools → Scripting) for them to be written.
   Remind the user every time.
4. Drums: fl_write_drums writes the step sequencer of the current pattern directly.
5. Sound design: fl_list_params (filter by name) + fl_set_params. Values are normalized 0–1.
6. Every write can be undone with fl_undo or Ctrl+Z in FL. Ask before replacing or clearing the user's material.

Conventions
- Time is in beats (quarter notes) from the start of the pattern; one 4/4 bar = 4 beats.
- Pitch is a MIDI number or a note name with C4 = 60. FL's UI labels that note C5; mention this when naming notes.
- Velocity is 0–1 (default 0.8).
- Drum step strings: one character per 16th step — x normal, X accent, o ghost, . rest; spaces and | are ignored.
- Plugins such as Kontakt may only expose generic automation slots; if parameter names are unclear, ask the user.
- To read what the user played/wrote, ask them to open that piano roll and run the script once (it writes nothing
  when no notes are staged), then call fl_read_piano_roll.
- Reply in the user's language.

Listening (you can hear FL's actual output)
- When the user describes a sound vaguely ("不够亮", "muddy", "没力"), don't guess: call fl_diagnose with their words
  and the channel. Report what the measurement confirms or contradicts, in plain terms, then propose the fix from
  fix_targets (plugin parameter or mixer EQ/volume). After changing something, call fl_diagnose or fl_listen again
  and tell the user what changed (A/B). Measurements without a reference use rules of thumb — say so.
- Whole-file reconstruction: prefer fl_recreate_music(path, kind="song") after fl_recreation_config is configured.
  It creates a new editable FLP with instruments and playlist, MIDI, native FL WAV, verification and A/B audio.
  reference_mix=true also creates mixed.wav and mix-report.json; offline DSP is not embedded in the FLP.
  Poll fl_recreation_status; the task survives disconnects. If waiting_fl_close, save and close FL then resume=true.
  Never report a task complete unless its status is done. render=false deliberately skips audio rendering.
  Use fl_recreate_music(kind="sound", channel=...) for persistent live plugin matching; FL must stay open for this.
  The current whole-song engine assumes 4/4 and rebuilds drums/chords/bass, not vocals or independent leads.
  Template instruments approximate the source; a finished workflow is not a claim of 1:1 musical similarity.
- Reference mixing: fl_match_mix(reference_path, source_path) processes an existing rendered audio file.
  Omit source_path to optimize actual FL mixer controls; inspect fl_get_mixer/fl_list_params first.
  Live matching needs bridge v8, a matching excerpt, and no concurrent playback/parameter edits.
  Poll fl_recreation_status; before/best/reference audio and exact mixer-revert.json are saved.
  On cancellation/error live mixing restores the original values; successful best settings remain applied.
- For incremental rebuilding inside an already-open project: fl_analyze_audio(path) → summarize tempo/key/sections/chords → confirm the channel mapping
  (drums, a chord instrument, a bass instrument) → fl_plan_rebuild → fl_rebuild_song → fl_rebuild_next.
  After applying and verifying each piano-roll script, acknowledge with fl_rebuild_next(applied=true).
  fl_export_rebuild exports the entire editable MIDI arrangement without changing FL.
  If the tempo looks half/double, re-run with tempo_hint. Tell the user that playlist placement is manual (markers).
- "Make this sound": fl_analyze_audio(path, kind=sound, or start/end for a slice) → fl_match_sound on a channel whose
  plugin can plausibly make it → poll fl_job → report how close it got and what still differs. The result includes
  `revert` values if the user prefers the old sound. Compare reference/before/best WAVs; distance reduction
  is not perceptual similarity. fl_match_result reads saved results after reconnecting.
"""

NOTE = {
    "type": "object",
    "properties": {
        "pitch": {"type": ["integer", "string"], "minimum": 0, "maximum": 127,
                  "description": "MIDI number or note name (C4 = 60), e.g. 45 or 'A2'"},
        "start": {"type": "number", "minimum": 0, "description": "Start in beats from pattern start"},
        "length": {"type": "number", "exclusiveMinimum": 0, "description": "Length in beats (0.5 = eighth note)"},
        "velocity": {"type": "number", "minimum": 0, "maximum": 127,
                     "description": "0–1, default 0.8; also accepts MIDI velocity 0–127"},
    },
    "required": ["pitch", "start", "length"],
}
CHANNEL = {"type": "integer", "description": "Channel rack index (from fl_get_project)"}
ROLE = {"type": "string", "enum": ["lead", "pad", "pluck", "bass", "drums", "vocal", "mix"],
        "description": "What the part is, for judging against typical ranges (guessed from the name if omitted)"}
CONCERNS = ["dull", "harsh", "muddy", "boomy", "thin", "narrow", "weak", "quiet", "loud"]


def _obj(props=None, required=()):
    return {"type": "object", "properties": props or {}, "required": list(required)}


TOOLS = [
    ('fl_match_mix', 'Persistent reference mix matching. With source_path: process an existing audio file using bounded EQ, '
     'compression, stereo width/balance, optional synthetic ambience and a -1 dBTP peak guard. Without source_path: '
     'capture FL playback and optimize existing mixer/FX controls; bridge v8 required. Returns task_id; poll fl_recreation_status. '
     'Live best settings stay applied; errors/cancellation restore the original, with a durable mixer-revert.json. '
     'Use the same musical excerpt in reference and playback. Offline DSP is not embedded into FLP.',
     _obj({'reference_path': {'type':'string'}, 'source_path': {'type':'string'}, 'output_dir': {'type':'string'},
           'start': {'type':'number','minimum':0}, 'end': {'type':'number','exclusiveMinimum':0},
           'budget': {'type':'integer','minimum':2,'maximum':200,'default':12},
           'seconds': {'type':'number','minimum':1,'maximum':30,'default':8},
           'mode': {'type':'string','enum':['song','pattern'],'default':'song'},
           'controls': {'type':'array','minItems':1,'maxItems':16,'items':_obj({
               'track':{'type':'integer','minimum':0,'maximum':125},
               'kind':{'type':'string','enum':['volume','pan','stereo','eq','effect']},
               'band':{'type':'integer','minimum':0,'maximum':2}, 'slot':{'type':'integer','minimum':0,'maximum':9},
               'param':{'type':'integer','minimum':0}, 'min':{'type':'number'}, 'max':{'type':'number'},
               'step':{'type':'number','exclusiveMinimum':0,'maximum':.25}}, ['track','kind'])}}, ['reference_path']),
     lambda s,a:s.recreation.match_mix(**a)),
    ("fl_recreation_config", "Read or configure the complete reconstruction workflow. Register a saved FL Studio 24 "
     "instrument template with five untrimmed playlist clips and one instrument per pattern. Default pattern names: "
     "Full Kick, Full Snare, Full Hat, Full Chords, Full Bass. The template is copied, never modified. No arguments reads configuration.",
     _obj({'template_path':{'type':'string'}, 'role_patterns':{'type':'object','properties':{role:{'type':'integer','minimum':1} for role in ('kick','snare','hat','chords','bass')}},
           'fl_executable':{'type':'string'}, 'output_root':{'type':'string'}}),
     lambda s,a:s.recreation.configure(**a)),
    ("fl_recreate_music", "Complete persistent reconstruction job. Input local audio; kind=auto chooses song at 20+ seconds, "
     "otherwise sound. Song: analyze, create editable FLP/MIDI with template instruments and playlist, render through FL, "
     "validate WAV and make reference/recreated comparison. FL must be closed for rendering; waiting_fl_close is resumable. "
     "Sound: keep FL open, supply channel; searches live plugin parameters and saves before/best/reference WAVs and revert settings. "
     "Returns task_id immediately; poll fl_recreation_status. Approximate drums/chords/bass only; no vocal/lead transcription or fidelity guarantee.",
     _obj({'path':{'type':'string'}, 'kind':{'type':'string','enum':['auto','song','sound'],'default':'auto'},
           'output_dir':{'type':'string','description':'Absolute parent folder; each task gets its own new subfolder'},
           'tempo_hint':{'type':'number','minimum':30,'maximum':300}, 'render':{'type':'boolean','default':True},
           'reference_mix':{'type':'boolean','default':True,'description':'After rendering, match reference mix to mixed.wav; dry WAV and FLP retained. Offline DSP is not embedded in FLP.'},
           'start':{'type':'number','minimum':0}, 'end':{'type':'number','exclusiveMinimum':0},
           'channel':CHANNEL, 'params':{'type':'array','items':{'type':'integer','minimum':0}},
           'budget':{'type':'integer','minimum':2,'maximum':200,'default':40},
           'pitch':{'type':['integer','string']}, 'note_len_s':{'type':'number','minimum':.05,'maximum':5}},['path']),
     lambda s,a:s.recreation.start(**a)),
    ("fl_recreation_status", "Read persistent progress and artifact paths after reconnecting. cancel=true requests cancellation; "
     "completed files are kept, sound matching keeps its best parameters. resume=true continues a waiting_fl_close song task "
     "after FL has been saved and closed; never recreates notes or overwrites the input. Only status=done means the requested workflow completed.",
     _obj({'task_id':{'type':'string'},'cancel':{'type':'boolean','default':False},'resume':{'type':'boolean','default':False}},['task_id']),
     lambda s,a:s.recreation.status(**a)),
    ("fl_get_status", "Read-only diagnostics: running server version/path, MIDI ports, installed scripts, "
     "saved routing conflicts and a live FL bridge check. Works even when FL is disconnected. "
     "probe=false only checks local configuration and does not contact FL.",
     _obj({"probe": {"type": "boolean", "default": True}}), lambda s, a: s.status(a.get("probe", True))),
    ("fl_get_project", "Read the current project: tempo, play state, channels (index, name, plugin), patterns.",
     _obj(), lambda s, a: s.project()),
    ("fl_list_params", "List a plugin's parameters (index, name, value): a channel's instrument, or an effect in a "
     "mixer track slot (mixer_track + slot, see fl_get_mixer). Filter by name with query, e.g. 'cutoff'.",
     _obj({"channel": CHANNEL, "mixer_track": {"type": "integer"}, "slot": {"type": "integer"},
           "query": {"type": "string"}, "limit": {"type": "integer", "default": 80}}),
     lambda s, a: s.list_params(a.get("channel"), a.get("query"), a.get("limit", 80), a.get("mixer_track"), a.get("slot"))),
    ("fl_set_params", "Set plugin parameters (normalized 0–1) on a channel instrument or a mixer effect slot. "
     "Returns displayed values before/after.",
     _obj({"channel": CHANNEL, "mixer_track": {"type": "integer"}, "slot": {"type": "integer"},
           "changes": {"type": "array", "items": _obj(
               {"param": {"type": "integer"}, "value": {"type": "number"}}, ["param", "value"])}}, ["changes"]),
     lambda s, a: s.set_params(a.get("channel"), a["changes"], a.get("mixer_track"), a.get("slot"))),
    ("fl_audition", "Play notes live through a channel's plugin without writing them. Uses project tempo unless bpm is given.",
     _obj({"channel": CHANNEL, "notes": {"type": "array", "items": NOTE}, "bpm": {"type": "number"}}, ["channel", "notes"]),
     lambda s, a: s.audition(a["channel"], a["notes"], a.get("bpm"))),
    ("fl_stop_audition", "Stop an audition in progress.", _obj(), lambda s, a: s.stop_audition()),
    ("fl_write_notes",
     f"Write notes into a channel's piano roll (current pattern). Opens the piano roll and stages the notes; the user "
     f"must then run the piano roll script '{paths.PIANO_ROLL_SCRIPT}'. mode=merge (default) adds notes and skips exact "
     f"duplicates; replace clears the piano roll first.",
     _obj({"channel": CHANNEL, "notes": {"type": "array", "items": NOTE},
           "mode": {"type": "string", "enum": ["merge", "replace"], "default": "merge"},
           "label": {"type": "string", "description": "Short description, e.g. 'verse bass'"}}, ["channel", "notes"]),
     lambda s, a: s.write_notes(a["channel"], a["notes"], a.get("mode", "merge"), a.get("label"))),
    ("fl_read_piano_roll", "Read the notes in the piano roll as of the last time the user ran the piano roll script, plus what it wrote.",
     _obj(), lambda s, a: s.piano_roll_report()),
    ("fl_write_drums",
     "Write the current pattern's step sequencer. tracks: [{channel, steps}], one char per 16th step: x normal, "
     "X accent, o ghost, . rest — e.g. 'x...x...x...x...'. clear_to clears steps up to that index.",
     _obj({"tracks": {"type": "array", "items": _obj({"channel": CHANNEL, "steps": {"type": "string"}}, ["channel", "steps"])},
           "clear_to": {"type": "integer", "default": 0}}, ["tracks"]),
     lambda s, a: s.write_drums(a["tracks"], a.get("clear_to", 0))),
    ("fl_get_steps", "Read a channel's step sequencer in the current pattern.",
     _obj({"channel": CHANNEL, "length": {"type": "integer", "default": 16}}, ["channel"]),
     lambda s, a: s.get_steps(a["channel"], a.get("length", 16))),
    ("fl_patterns", "Patterns: list, select (index), new (optional name), rename (index, name).",
     _obj({"action": {"type": "string", "enum": ["list", "select", "new", "rename"]},
           "index": {"type": "integer"}, "name": {"type": "string"}}, ["action"]),
     lambda s, a: s.patterns(a["action"], a.get("index"), a.get("name"))),
    ("fl_set_tempo", "Set the project tempo in BPM.", _obj({"bpm": {"type": "number"}}, ["bpm"]),
     lambda s, a: s.set_tempo(a["bpm"])),
    ("fl_transport", "Transport: play, stop, toggle, song_mode (play arrangement), pattern_mode (loop current pattern).",
     _obj({"action": {"type": "string", "enum": ["play", "stop", "toggle", "song_mode", "pattern_mode"]}}, ["action"]),
     lambda s, a: s.transport(a["action"])),
    ("fl_select_channel", "Select a channel in the channel rack.", _obj({"channel": CHANNEL}, ["channel"]),
     lambda s, a: s.select_channel(a["channel"])),
    ("fl_rename_channel", "Rename a channel.", _obj({"channel": CHANNEL, "name": {"type": "string"}}, ["channel", "name"]),
     lambda s, a: s.rename_channel(a["channel"], a["name"])),
    ("fl_undo", "Undo the last action in FL (same as Ctrl+Z).", _obj(), lambda s, a: s.undo()),
    ("fl_get_mixer", "Mixer: which track each channel is routed to, track names/volumes, built-in EQ, effect slots.",
     _obj({"tracks": {"type": "array", "items": {"type": "integer"}}}), lambda s, a: s.mixer(a.get("tracks"))),
    ("fl_set_mixer", "Set a mixer track's volume (0–1, FL's fader scale) and/or built-in EQ bands "
     "(band 0 low, 1 mid, 2 high; values in FL's normalized units — read fl_get_mixer first and change gradually).",
     _obj({"track": {"type": "integer"}, "volume": {"type": "number", "minimum":0,"maximum":1},
           "pan": {"type":"number","minimum":-1,"maximum":1},
           "stereo_separation": {"type":"number","minimum":-1,"maximum":1},
           "eq": {"type": "array", "items": _obj({"band": {"type": "integer"}, "gain": {"type": "number"},
                                                   "freq": {"type": "number"}, "bandwidth": {"type": "number"}}, ["band"])}},
          ["track"]),
     lambda s, a: s.set_mixer(a["track"], a.get("volume"), a.get("eq"), a.get("pan"), a.get("stereo_separation"))),

    # ---- listening -------------------------------------------------------------------------------------------
    ("fl_listen",
     "Play the current pattern (or the song) in FL, record exactly what FL outputs (other apps are excluded), and "
     "measure it: loudness, band balance, brightness, stereo width, dynamics, plus issues that stand out. Pass a "
     "channel to hear it soloed.",
     _obj({"mode": {"type": "string", "enum": ["pattern", "song"], "default": "pattern"},
           "seconds": {"type": "number", "default": 8}, "channel": CHANNEL, "role": ROLE}),
     lambda s, a: s.ears.listen(a.get("mode", "pattern"), a.get("seconds", 8), a.get("channel"), a.get("role"))),
    ("fl_diagnose",
     "Check a vague listening complaint by measuring it. Pass the user's own words as description (any language, "
     "e.g. '这个音色不够亮', 'the mix is muddy') and/or explicit concerns. Records the channel soloed and the full mix "
     "for context (or compares to a reference file), then returns which complaints the measurements confirm, by how "
     "much, and concrete fix targets (matching plugin parameters, the channel's mixer track and EQ).",
     _obj({"description": {"type": "string"}, "channel": CHANNEL,
           "concerns": {"type": "array", "items": {"type": "string", "enum": CONCERNS}}, "role": ROLE,
           "reference_path": {"type": "string", "description": "Audio file to compare against (WAV, or any format with ffmpeg)"},
           "mode": {"type": "string", "enum": ["pattern", "song"], "default": "pattern"},
           "seconds": {"type": "number", "default": 8}}),
     lambda s, a: s.ears.diagnose(a.get("description", ""), a.get("channel"), a.get("role"), a.get("concerns"),
                                  a.get("reference_path"), a.get("mode", "pattern"), a.get("seconds", 8))),
    ("fl_analyze_audio",
     "Analyze an audio file. Songs (>= 20 s, or kind=song): tempo, key, sections with names, chords per bar, drum "
     "grooves and bass lines per section — feed the analysis_id to fl_rebuild_song. Short sounds (or kind=sound): "
     "pitch, envelope, brightness, band balance — feed the analysis_id to fl_match_sound. start/end in seconds cut a "
     "region (e.g. one bass note out of a song).",
     _obj({"path": {"type": "string"}, "start": {"type": "number"}, "end": {"type": "number"},
           "kind": {"type": "string", "enum": ["auto", "song", "sound"], "default": "auto"},
           "tempo_hint": {"type": "number", "description": "Known BPM, if the detected tempo is half/double"}}, ["path"]),
     lambda s, a: s.ears.analyze_audio(a["path"], a.get("start"), a.get("end"), a.get("kind", "auto"), a.get("tempo_hint"))),
    ("fl_rebuild_song",
     "Recreate an analyzed song's skeleton in FL: tempo, one named pattern per distinct section with its drum groove "
     "in the step sequencer, and playlist markers at each section. Chords and bass are queued for the piano roll "
     "(fl_rebuild_next). Drum channels are found by name (kick/snare/clap/hat) unless given.",
     _obj({"analysis_id": {"type": "string"},
           "drum_channels": {"type": "object", "properties": {"kick": CHANNEL, "snare": CHANNEL, "hat": CHANNEL}},
           "chord_channel": CHANNEL, "bass_channel": CHANNEL,
           "parts": {"type": "array", "items": {"type": "string"}, "description": "Section labels to build (default all)"},
           "set_tempo": {"type": "boolean", "default": True}, "add_markers": {"type": "boolean", "default": True}},
          ["analysis_id"]),
     lambda s, a: s.ears.rebuild_song(a["analysis_id"], a.get("drum_channels"), a.get("chord_channel"),
                                      a.get("bass_channel"), a.get("parts"), a.get("set_tempo", True),
                                      a.get("add_markers", True))),
    ("fl_plan_rebuild", "Preview song reconstruction and validate instrument channel mapping without changing FL.",
     _obj({"analysis_id": {"type": "string"}, "drum_channels": {"type": "object"},
           "chord_channel": CHANNEL, "bass_channel": CHANNEL,
           "parts": {"type": "array", "items": {"type": "string"}}}, ["analysis_id"]),
     lambda s, a: s.ears.plan_rebuild(a['analysis_id'], a.get('drum_channels'), a.get('chord_channel'),
                                     a.get('bass_channel'), a.get('parts'))),
    ("fl_export_rebuild", "Export the analyzed song as a complete editable MIDI arrangement with drum, chord, bass, tempo and marker tracks. Does not change FL. No vocals or lead transcription.",
     _obj({"analysis_id": {"type": "string"}, "parts": {"type": "array", "items": {"type": "string"}}}, ['analysis_id']),
     lambda s, a: s.ears.export_rebuild(a['analysis_id'], a.get('parts'))),
    ("fl_rebuild_next", f"Stage the next queued chord/bass part. Run '{paths.PIANO_ROLL_SCRIPT}' and verify it, then "
     "call with applied=true to acknowledge and advance. Without acknowledgement it returns the same pending step.",
     _obj({'applied': {'type': 'boolean', 'default': False}}), lambda s, a: s.ears.rebuild_next(a.get('applied', False))),
    ("fl_match_sound",
     "Recreate a reference sound on a channel's plugin: plays notes through the plugin, records them, and tunes the "
     "plugin's parameters to minimize the difference (runs in the background — poll fl_job). reference is an audio "
     "file path or a 'sound-...' analysis_id. params: parameter indices to search (default: timbre-related ones picked "
     "by name). Returns reference/before/best WAVs, saved match_id and revert values. "
     "Distance reduction is not a perceptual similarity percentage. budget: number of test renders.",
     _obj({"reference": {"type": "string"}, "channel": CHANNEL,
           "params": {"type": "array", "items": {"type": "integer"}}, "budget": {"type": "integer", "minimum": 2, "maximum": 200, "default": 40},
           "start": {"type": "number"}, "end": {"type": "number"},
           "pitch": {"type": ["integer", "string"], "description": "Note to play (default: detected from the reference)"},
           "note_len_s": {"type": "number"}}, ["reference", "channel"]),
     lambda s, a: s.ears.match_sound(a["reference"], a["channel"], a.get("params"), a.get("budget", 40),
                                     a.get("start"), a.get("end"),
                                     parse_pitch(a["pitch"]) if a.get("pitch") is not None else None, a.get("note_len_s"))),
    ("fl_job", "Status/result of a background job (fl_match_sound). cancel=true stops it (the best setting so far is kept).",
     _obj({"job_id": {"type": "string"}, "cancel": {"type": "boolean", "default": False}}, ["job_id"]),
     lambda s, a: s.ears.job(a["job_id"], a.get("cancel", False))),
    ("fl_match_result", "Read a saved sound-match result after reconnecting, including reference/before/best WAVs and revert settings.",
     _obj({"match_id": {"type": "string"}}, ["match_id"]), lambda s, a: s.ears.match_result(a['match_id'])),
]
TOOL_LIST = [{"name": n, "description": d, "inputSchema": schema} for n, d, schema, _ in TOOLS]
TOOL_RUN = {n: run for n, _, _, run in TOOLS}

PROMPTS = [{"name": "fl_arranging_guide",
            "description": "How to work with the fl-studio tools: workflow, units, and conventions.",
            "arguments": []}]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class McpServer:
    def __init__(self, session):
        self.session = session

    def handle(self, msg):
        """Handle one JSON-RPC message (or a batch list). Returns the response, or None for notifications."""
        if isinstance(msg, list):
            out = [r for r in (self.handle(m) for m in msg) if r is not None]
            return out or None
        if not isinstance(msg, dict) or "method" not in msg:
            return None
        rid = msg.get("id")
        try:
            result = self._dispatch(msg["method"], msg.get("params") or {})
        except LookupError:
            return None if rid is None else self._error(rid, -32601, f"method not found: {msg['method']}")
        except Exception as e:
            log(traceback.format_exc())
            return None if rid is None else self._error(rid, -32603, f"{type(e).__name__}: {e}")
        return None if rid is None else {"jsonrpc": "2.0", "id": rid, "result": result}

    @staticmethod
    def _error(rid, code, message):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    def _dispatch(self, method, params):
        if method == "initialize":
            requested = params.get("protocolVersion")
            return {
                "protocolVersion": requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {}, "prompts": {}},
                "serverInfo": {"name": "fl-studio-mcp", "version": __version__},
                "instructions": GUIDE,
            }
        if method.startswith("notifications/"):
            return {}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOL_LIST}
        if method == "tools/call":
            return self._call_tool(params.get("name"), params.get("arguments") or {})
        if method == "prompts/list":
            return {"prompts": PROMPTS}
        if method == "prompts/get":
            if params.get("name") != "fl_arranging_guide":
                raise ValueError(f"unknown prompt {params.get('name')!r}")
            return {"description": PROMPTS[0]["description"],
                    "messages": [{"role": "user", "content": {"type": "text", "text": GUIDE}}]}
        raise LookupError(method)

    def _call_tool(self, name, args):
        run = TOOL_RUN.get(name)
        if run is None:
            return {"content": [{"type": "text", "text": f"unknown tool {name!r}"}], "isError": True}
        try:
            result = run(self.session, args)
        except (FLError, AudioError, KeyError, ValueError, TypeError, OSError) as e:
            return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}


def run_stdio(server):
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    log(f"fl-studio-mcp {__version__} ready (stdio)")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            resp = server.handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
