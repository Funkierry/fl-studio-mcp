# name=FL Studio MCP Bridge
# url=
"""
FL Studio MCP Bridge（MIDI 控制器脚本，由 fl-studio-mcp 安装）

FL 2024 的脚本沙盒禁止 socket 和文件读写，所以通信走 SysEx：
  外部程序 → loopMIDI 端口 "FL Agent"       → 本脚本（OnSysEx 收）
  本脚本   → loopMIDI 端口 "FL Agent Reply" → 外部程序（device.midiOutSysex 发）
FL 设置：输入 "FL Agent" 和输出 "FL Agent Reply" 设成相同端口号。

消息格式：F0 7D 46 42 <kind> <client> <id lo> <id hi> <chunk lo> <chunk hi> <total lo> <total hi> <7-bit 打包的 JSON> F7
kind: 1 = 请求（外部→FL），2 = 回复（FL→外部）。client：每个外部进程随机选的编号，回复原样带回，
这样多个 MCP 服务器实例同时连接也不会串消息。

收到的请求先排队，在 OnIdle（FL 主线程）里执行。
"""

print("[FL MCP] script loading...")

import builtins
import json
import os
import sys
import time

import channels
import device
import general
import midi
import mixer
import patterns
import plugins
import transport
import ui

VERSION = 7
DATA_DIR = os.path.join(os.path.expanduser("~"), "Documents", "FLAgent")
HEADER = bytes([0xF0, 0x7D, 0x46, 0x42])  # 'FB': protocol v5 (with client id)
PARTIAL_TTL_S = 10.0
KIND_REQUEST, KIND_REPLY = 1, 2
CHUNK_RAW = 512
PROBE_MODULES = ["socket", "selectors", "threading", "json", "os", "struct", "time"]
STEP_VELOCITY = {"x": 100, "X": 127, "o": 55}

_partial = {}      # (client, msg_id) -> [first_seen, {chunk_idx: bytes}]
_queue = []        # [(client, msg_id, json_bytes)]
_events = []       # 试听事件 [(due_time, global_channel_index, pitch, velocity)]，velocity 0 = note-off


# ---------------------------------------------------------------- 工具函数

def _log(msg):
    print("[FL MCP] " + str(msg))


def _safe(fn, *args, default=None):
    """调用一个 FL API，失败时返回错误字符串而不是抛异常。"""
    try:
        return fn(*args)
    except Exception as e:
        return default if default is not None else "ERR %s: %s" % (type(e).__name__, e)


def _try(fn):
    try:
        return {"ok": True, "value": fn()}
    except BaseException as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def _probe_imports():
    out = {}
    for name in PROBE_MODULES:
        try:
            __import__(name)
            out[name] = "ok"
        except Exception as e:
            out[name] = "%s: %s" % (type(e).__name__, e)
    return out


def _probe_file_io():
    path = os.path.join(DATA_DIR, "caps_test.txt")

    def write():
        with open(path, "w", encoding="utf-8") as f:
            f.write("hello from FL")
        return path

    return {"open_write": _try(write)}


def _tempo():
    t = mixer.getCurrentTempo()
    return t / 1000.0 if t > 1000 else float(t)


# ---------------------------------------------------------------- 7-bit 打包

def pack7(data):
    out = bytearray()
    for i in range(0, len(data), 7):
        group = data[i:i + 7]
        msb = 0
        for j, b in enumerate(group):
            if b & 0x80:
                msb |= 1 << j
        out.append(msb)
        out.extend(b & 0x7F for b in group)
    return bytes(out)


def unpack7(data):
    out = bytearray()
    for i in range(0, len(data), 8):
        msb = data[i]
        for j, b in enumerate(data[i + 1:i + 8]):
            out.append(b | (0x80 if msb & (1 << j) else 0))
    return bytes(out)


def _u14(lo, hi):
    return lo | (hi << 7)


def _b14(v):
    return [v & 0x7F, (v >> 7) & 0x7F]


# ---------------------------------------------------------------- 命令：读取

def cmd_ping(args):
    return {"pong": True, "time": time.time(), "bridge_version": VERSION}


def _channel_list():
    out = []
    for i in range(channels.channelCount()):
        valid = bool(_safe(plugins.isValid, i, default=False))
        out.append({
            "index": i,
            "name": _safe(channels.getChannelName, i),
            "plugin": _safe(plugins.getPluginName, i) if valid else None,
            "selected": bool(_safe(channels.isChannelSelected, i, default=False)),
        })
    return out


def _pattern_list():
    out = []
    current = patterns.patternNumber()
    # patternCount is the number of used patterns, not the highest used index.
    # Gaps are legal; scanning only 1..count silently loses later patterns.
    for i in range(1, patterns.patternMax() + 1):
        if i != current and patterns.isPatternDefault(i):
            continue
        out.append({"index": i, "name": _safe(patterns.getPatternName, i),
                    # FL 24.2.1 returns sixteenth-note steps despite the API doc's unit label.
                    "length_steps": _safe(patterns.getPatternLength, i),
                    "length_beats": _safe(lambda: patterns.getPatternLength(i) / 4.0)})
    return out


def cmd_info(args):
    return {
        "fl_version": _safe(ui.getVersion),
        "api_version": _safe(general.getVersion),
        "bridge_version": VERSION,
        "python": sys.version,
        "tempo": _safe(_tempo),
        "ppq": _safe(general.getRecPPQ),
        "playing": _safe(transport.isPlaying),
        "pattern": _safe(patterns.patternNumber),
        "pattern_count": _safe(patterns.patternCount),
        "channel_count": _safe(channels.channelCount),
        "selected_channel": _safe(channels.selectedChannel),
        "device_name": _safe(device.getName),
        "device_port": _safe(device.getPortNumber),
        "device_output_assigned": _safe(device.isAssigned),
    }


def cmd_project(args):
    return {
        "tempo": _safe(_tempo),
        "playing": _safe(transport.isPlaying),
        "song_mode": _safe(transport.getLoopMode) == 1,
        "current_pattern": _safe(patterns.patternNumber),
        "selected_channel": _safe(channels.selectedChannel),
        "channels": _channel_list(),
        "patterns": _pattern_list(),
    }


def cmd_caps(args):
    return {
        "imports": _probe_imports(),
        "file_io": _probe_file_io(),
        "interpreter_id": id(builtins),
        "pr_report": getattr(builtins, "_flagent_pr_report", None),
    }


def cmd_channels(args):
    out = _channel_list()
    for ch in out:
        ch["param_count"] = _safe(plugins.getParamCount, ch["index"]) if ch["plugin"] else 0
        ch["plugin_valid"] = ch["plugin"] is not None
        ch["plugin_name"] = ch["plugin"]
    return {"channels": out}


def _target(args):
    """Plugin address: a channel's instrument, or an effect in a mixer track slot."""
    if args.get("mixer_track") is not None:
        return int(args["mixer_track"]), int(args.get("slot", 0))
    return int(args["channel"]), -1


def cmd_params(args):
    idx, slot = _target(args)
    limit = int(args.get("limit", 300))
    include_empty = bool(args.get("include_empty", False))
    if not plugins.isValid(idx, slot):
        raise ValueError("no plugin at %s" % (("mixer track %d slot %d" % (idx, slot)) if slot >= 0 else ("channel %d" % idx)))
    total = plugins.getParamCount(idx, slot)
    named, empty = [], 0
    for p in range(total):
        name = _safe(plugins.getParamName, p, idx, slot)
        if not include_empty and (not name or not str(name).strip()):
            empty += 1
            continue
        if len(named) < limit:
            named.append({
                "param": p,
                "name": name,
                "value": _safe(plugins.getParamValue, p, idx, slot),
                "value_str": _safe(plugins.getParamValueString, p, idx, slot),
            })
    return {"plugin": plugins.getPluginName(idx, slot), "total": total, "empty_names": empty, "params": named}


def cmd_get_steps(args):
    ch = int(args["channel"])
    n = int(args.get("length", 16))
    return {"channel": ch, "steps": "".join("x" if channels.getGridBit(ch, p) else "." for p in range(n))}


def cmd_pr_report(args):
    return {"shared_interpreter_report": getattr(builtins, "_flagent_pr_report", None)}


# ---------------------------------------------------------------- 命令：写入（都先 saveUndo）

def cmd_set_param(args):
    ch, p, value = int(args["channel"]), int(args["param"]), float(args["value"])
    before = plugins.getParamValue(p, ch)
    general.saveUndo("MCP: set param", midi.UF_Plugin)
    plugins.setParamValue(value, p, ch)
    return {
        "name": plugins.getParamName(p, ch),
        "before": before,
        "after": plugins.getParamValue(p, ch),
        "after_str": _safe(plugins.getParamValueString, p, ch),
    }


def cmd_set_params(args):
    idx, slot = _target(args)
    if args.get("undo", True):
        general.saveUndo("MCP: set params", midi.UF_Plugin)
    out = []
    for c in args["changes"]:
        p, value = int(c["param"]), max(0.0, min(1.0, float(c["value"])))
        before = _safe(plugins.getParamValueString, p, idx, slot)
        plugins.setParamValue(value, p, idx, slot)
        out.append({"param": p, "name": _safe(plugins.getParamName, p, idx, slot),
                    "before": before, "after": _safe(plugins.getParamValueString, p, idx, slot),
                    "value": _safe(plugins.getParamValue, p, idx, slot)})
    return {"changes": out}


def cmd_get_param_values(args):
    idx, slot = _target(args)
    return {"values": {str(p): _safe(plugins.getParamValue, int(p), idx, slot) for p in args["params"]}}


# ---------------------------------------------------------------- mixer, solo, playback, markers

EQ_FUNCS = ("getEqGain", "setEqGain", "getEqFrequency", "setEqFrequency", "getEqBandwidth", "setEqBandwidth")


def _track_info(t):
    info = {"track": t, "name": _safe(mixer.getTrackName, t), "volume": _safe(mixer.getTrackVolume, t),
            "effects": []}
    if hasattr(mixer, "getEqGain"):
        info["eq"] = [{"band": b, "gain": _safe(mixer.getEqGain, t, b),
                       "freq": _safe(mixer.getEqFrequency, t, b) if hasattr(mixer, "getEqFrequency") else None}
                      for b in range(3)]
    for slot in range(10):
        if _safe(plugins.isValid, t, slot, default=False) is True:
            info["effects"].append({"slot": slot, "plugin": _safe(plugins.getPluginName, t, slot),
                                    "param_count": _safe(plugins.getParamCount, t, slot)})
    return info


def cmd_mixer(args):
    routes = []
    for i in range(channels.channelCount()):
        routes.append({"channel": i, "name": _safe(channels.getChannelName, i),
                       "mixer_track": _safe(channels.getTargetFxTrack, i)})
    tracks = sorted({r["mixer_track"] for r in routes if isinstance(r["mixer_track"], int)} | {0})
    if args.get("tracks"):
        tracks = [int(t) for t in args["tracks"]]
    return {"routing": routes, "tracks": [_track_info(t) for t in tracks],
            "eq_api": [f for f in EQ_FUNCS if hasattr(mixer, f)]}


def cmd_mixer_set(args):
    t = int(args["track"])
    if args.get("undo", True):
        general.saveUndo("MCP: mixer", midi.UF_None)
    before = _track_info(t)
    if args.get("volume") is not None:
        mixer.setTrackVolume(t, float(args["volume"]))
    for eq in args.get("eq", []):
        b = int(eq["band"])
        if eq.get("gain") is not None:
            mixer.setEqGain(t, b, float(eq["gain"]))
        if eq.get("freq") is not None and hasattr(mixer, "setEqFrequency"):
            mixer.setEqFrequency(t, b, float(eq["freq"]))
        if eq.get("bandwidth") is not None and hasattr(mixer, "setEqBandwidth"):
            mixer.setEqBandwidth(t, b, float(eq["bandwidth"]))
    return {"before": before, "after": _track_info(t)}


def cmd_solo(args):
    ch, on = int(args["channel"]), bool(args.get("on", True))
    if bool(channels.isChannelSolo(ch)) != on:
        channels.soloChannel(ch)
    return {"channel": ch, "solo": bool(channels.isChannelSolo(ch))}


def cmd_listen_play(args):
    """Start playback for a listening pass: pattern or song, from the start. Returns the previous state."""
    prev = {"playing": bool(transport.isPlaying()), "song_mode": transport.getLoopMode() == 1}
    if transport.isPlaying():
        transport.stop()
    want_song = args.get("mode", "pattern") == "song"
    if (transport.getLoopMode() == 1) != want_song:
        transport.setLoopMode()
    _safe(transport.setSongPos, 0.0)
    transport.start()
    return {"previous": prev, "song_mode": transport.getLoopMode() == 1}


def cmd_listen_stop(args):
    transport.stop()
    restore = args.get("restore_song_mode")
    if restore is not None and (transport.getLoopMode() == 1) != bool(restore):
        transport.setLoopMode()
    return {"playing": bool(transport.isPlaying())}


def cmd_add_markers(args):
    import arrangement
    ppq = general.getRecPPQ()
    beats_per_bar = int(args.get("beats_per_bar", 4))
    done = []
    for m in args["markers"]:
        ticks = int(round(float(m["bar"]) * beats_per_bar * ppq))
        r = _safe(arrangement.addAutoTimeMarker, ticks, str(m["name"]))
        done.append({"bar": m["bar"], "name": m["name"], "ticks": ticks, "result": r})
    return {"markers": done}


def cmd_steps(args):
    ch = int(args["channel"])
    steps = [1 if s else 0 for s in args["steps"]]
    general.saveUndo("MCP: steps", midi.UF_SS)
    for pos, bit in enumerate(steps):
        channels.setGridBit(ch, pos, bit)
    readback = [channels.getGridBit(ch, pos) for pos in range(len(steps))]
    return {"written": steps, "readback": readback, "match": readback == steps}


def cmd_drums(args):
    """tracks: [{channel, steps}]，steps 字符：x 普通、X 重音、o 轻音（ghost），其他字符 = 空。"""
    pat = patterns.patternNumber()
    clear_to = int(args.get("clear_to", 0))
    general.saveUndo("MCP: drums", midi.UF_SS)
    out = []
    for t in args["tracks"]:
        ch, steps = int(t["channel"]), str(t["steps"])
        vel_errors = 0
        for pos in range(max(len(steps), clear_to)):
            c = steps[pos] if pos < len(steps) else "."
            on = c in STEP_VELOCITY
            channels.setGridBit(ch, pos, 1 if on else 0)
            if on:
                r = _safe(channels.setStepParameterByIndex, ch, pat, pos, midi.pVelocity, STEP_VELOCITY[c])
                if isinstance(r, str) and r.startswith("ERR"):
                    vel_errors += 1
        readback = "".join("x" if channels.getGridBit(ch, p) else "." for p in range(len(steps)))
        out.append({"channel": ch, "name": _safe(channels.getChannelName, ch), "readback": readback,
                    "velocity_errors": vel_errors})
    return {"pattern": pat, "tracks": out}


def cmd_play(args):
    """试听：events = [{pitch, at_ms, len_ms, velocity(0-127)}]，相对现在的时间。"""
    ch = int(args["channel"])
    g = channels.getChannelIndex(ch)
    now = time.time()
    for e in args["events"]:
        on = now + float(e["at_ms"]) / 1000.0
        _events.append((on, g, int(e["pitch"]), max(1, min(127, int(e.get("velocity", 100))))))
        _events.append((on + float(e["len_ms"]) / 1000.0, g, int(e["pitch"]), 0))
    _events.sort(key=lambda x: (x[0], x[3] > 0))
    return {"scheduled": len(args["events"]), "global_index": g}


def cmd_note(args):
    return cmd_play({"channel": args["channel"], "events": [{
        "pitch": args.get("pitch", 60), "at_ms": 0, "len_ms": args.get("length_ms", 400),
        "velocity": args.get("velocity", 100)}]})


def cmd_stop_audition(args):
    offs = [e for e in _events if e[3] == 0]
    del _events[:]
    for _, g, pitch, _v in offs:
        _safe(channels.midiNoteOn, g, pitch, 0)
    return {"stopped": len(offs)}


def cmd_stage_notes(args):
    """供钢琴卷帘脚本读取（如果两边共用解释器）。外部程序同时会把音符嵌进脚本文件。"""
    payload = dict(args)
    builtins._flagent_pending = payload
    builtins._flagent_pr_report = None
    return {"builtins": True, "count": len(payload.get("notes", []))}


def cmd_open_piano_roll(args):
    ch = int(args["channel"])
    channels.selectOneChannel(ch)
    # showWindow only raises the previous editor; it does not retarget its channel.
    ui.openEventEditor(channels.getRecEventId(ch) + midi.REC_Chan_PianoRoll, midi.EE_PR)
    ui.setFocused(midi.widPianoRoll)
    return {"selected_channel": channels.selectedChannel(), "focused_caption": _safe(ui.getFocusedFormCaption)}


def cmd_select_channel(args):
    ch = int(args["channel"])
    channels.selectOneChannel(ch)
    return {"selected_channel": channels.selectedChannel()}


def cmd_rename_channel(args):
    ch = int(args["channel"])
    channels.setChannelName(ch, str(args["name"]))
    return {"channel": ch, "name": channels.getChannelName(ch)}


def cmd_patterns(args):
    action = args.get("action", "list")
    if action == "select":
        patterns.jumpToPattern(int(args["index"]))
    elif action == "new":
        current = patterns.patternNumber()
        maximum = patterns.patternMax()
        used = [i for i in range(1, maximum + 1) if not patterns.isPatternDefault(i)]
        first = max(used + [current]) + 1
        candidates = list(range(first, maximum + 1)) + list(range(1, min(first, maximum + 1)))
        n = next((i for i in candidates if i != current and patterns.isPatternDefault(i)), None)
        if n is None:
            raise ValueError("no unused pattern slot is available")
        patterns.jumpToPattern(n)
        if args.get("name"):
            patterns.setPatternName(n, str(args["name"]))
    elif action == "rename":
        patterns.setPatternName(int(args["index"]), str(args["name"]))
    return {"current_pattern": patterns.patternNumber(), "patterns": _pattern_list()}


def cmd_set_tempo(args):
    bpm = float(args["bpm"])
    general.processRECEvent(midi.REC_Tempo, int(round(bpm * 1000)), midi.REC_Control | midi.REC_UpdateControl)
    return {"tempo": _tempo()}


def cmd_transport(args):
    action = args.get("action", "toggle")
    if action == "play" and not transport.isPlaying():
        transport.start()
    elif action == "stop":
        transport.stop()
    elif action == "toggle":
        transport.start()
    elif action == "song_mode":
        if transport.getLoopMode() != 1:
            transport.setLoopMode()
    elif action == "pattern_mode":
        if transport.getLoopMode() != 0:
            transport.setLoopMode()
    return {"playing": transport.isPlaying(), "song_mode": transport.getLoopMode() == 1}


def cmd_undo(args):
    general.undoUp()
    return {"ok": True}


def cmd_hint(args):
    ui.setHintMsg(str(args.get("text", "FL Studio MCP")))
    return {"ok": True}


COMMANDS = {
    "ping": cmd_ping,
    "info": cmd_info,
    "project": cmd_project,
    "caps": cmd_caps,
    "channels": cmd_channels,
    "params": cmd_params,
    "get_steps": cmd_get_steps,
    "pr_report": cmd_pr_report,
    "set_param": cmd_set_param,
    "set_params": cmd_set_params,
    "get_param_values": cmd_get_param_values,
    "mixer": cmd_mixer,
    "mixer_set": cmd_mixer_set,
    "solo": cmd_solo,
    "listen_play": cmd_listen_play,
    "listen_stop": cmd_listen_stop,
    "add_markers": cmd_add_markers,
    "steps": cmd_steps,
    "drums": cmd_drums,
    "play": cmd_play,
    "note": cmd_note,
    "stop_audition": cmd_stop_audition,
    "stage_notes": cmd_stage_notes,
    "open_piano_roll": cmd_open_piano_roll,
    "select_channel": cmd_select_channel,
    "rename_channel": cmd_rename_channel,
    "patterns": cmd_patterns,
    "set_tempo": cmd_set_tempo,
    "transport": cmd_transport,
    "undo": cmd_undo,
    "hint": cmd_hint,
}


def _handle(raw):
    try:
        req = json.loads(raw.decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": "bad json: %s" % e}
    rid = req.get("id")
    fn = COMMANDS.get(req.get("cmd"))
    if fn is None:
        return {"id": rid, "ok": False, "error": "unknown cmd %r（FL 里的桥脚本可能是旧版，请重新加载脚本）" % req.get("cmd")}
    try:
        return {"id": rid, "ok": True, "result": fn(req.get("args") or {})}
    except Exception as e:
        return {"id": rid, "ok": False, "error": "%s: %s" % (type(e).__name__, e)}


# ---------------------------------------------------------------- SysEx 收发

def _send_reply(client, msg_id, obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    chunks = [data[i:i + CHUNK_RAW] for i in range(0, len(data), CHUNK_RAW)] or [b""]
    for idx, chunk in enumerate(chunks):
        frame = HEADER + bytes([KIND_REPLY, client] + _b14(msg_id) + _b14(idx) + _b14(len(chunks))) + pack7(chunk) + b"\xF7"
        device.midiOutSysex(frame)


def OnSysEx(event):
    # never let a malformed or interleaved message take the script down: FL stops answering if this raises
    try:
        data = bytes(event.sysex)
        if len(data) < 13 or data[:4] != HEADER or data[4] != KIND_REQUEST:
            return
        event.handled = True
        client = data[5]
        msg_id, idx, total = _u14(data[6], data[7]), _u14(data[8], data[9]), _u14(data[10], data[11])
        if total < 1 or idx >= total:
            return
        entry = _partial.setdefault((client, msg_id), [time.time(), {}])
        entry[1][idx] = unpack7(data[12:-1])
        if len(entry[1]) == total:
            del _partial[(client, msg_id)]
            _queue.append((client, msg_id, b"".join(entry[1][i] for i in range(total))))
    except Exception as e:
        _log("OnSysEx error: %s: %s" % (type(e).__name__, e))


def _process_queue():
    while _queue:
        client, msg_id, raw = _queue.pop(0)
        resp = _handle(raw)
        if not _safe(device.isAssigned, default=False):
            _log("收到请求 #%d，但没有分配输出端口，无法回复：在 MIDI 设置里给回复用的输出端口设相同端口号" % msg_id)
        try:
            _send_reply(client, msg_id, resp)
        except Exception as e:
            _log("reply failed: %s: %s" % (type(e).__name__, e))
    # drop half-received messages whose sender went away
    now = time.time()
    for key in [k for k, v in _partial.items() if now - v[0] > PARTIAL_TTL_S]:
        del _partial[key]


def _poll_events():
    if not _events:
        return
    now = time.time()
    while _events and _events[0][0] <= now:
        _, g, pitch, vel = _events.pop(0)
        _safe(channels.midiNoteOn, g, pitch, vel)


# ---------------------------------------------------------------- FL 回调

def OnInit():
    builtins._flagent_midi_alive = time.time()
    _log("v%d device=%s port=%s output_assigned=%s" % (
        VERSION, _safe(device.getName), _safe(device.getPortNumber), _safe(device.isAssigned)))
    _safe(ui.setHintMsg, "FL Studio MCP Bridge v%d ready" % VERSION)


def OnDeInit():
    cmd_stop_audition({})


def OnIdle():
    try:
        _process_queue()
        _poll_events()
    except Exception as e:
        _log("OnIdle error: %s: %s" % (type(e).__name__, e))
