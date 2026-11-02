"""
Bridge protocol test without FL: the FL-side script runs against stub FL modules, two host clients send
interleaved multi-chunk requests, and each must receive only its own reply. Also: garbage must not kill the script.

    python tests/test_protocol.py
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def stub(name, **fns):
    m = types.ModuleType(name)
    m.__dict__.update(fns)
    sys.modules[name] = m


sent = []
stub("channels", channelCount=lambda: 1, getChannelName=lambda i: "Kontakt 8", getChannelIndex=lambda i: i,
     selectedChannel=lambda: 0, isChannelSelected=lambda i: True, selectOneChannel=lambda i: None,
     midiNoteOn=lambda *a: None, setGridBit=lambda *a: None, getGridBit=lambda *a: 0, getTargetFxTrack=lambda i: 1)
stub("plugins", isValid=lambda *a: True, getPluginName=lambda *a: "Kontakt 8", getParamCount=lambda *a: 3000,
     getParamName=lambda p, *a: f"参数 #{p:03d}" if p < 512 else "", getParamValue=lambda *a: 0.5,
     getParamValueString=lambda *a: "50%", setParamValue=lambda *a: None)
stub("device", midiOutSysex=lambda b: sent.append(bytes(b)), getName=lambda: "FL Agent", getPortNumber=lambda: 10,
     isAssigned=lambda: True)
stub("general", getVersion=lambda: 37, getRecPPQ=lambda: 96, saveUndo=lambda *a: None)
stub("mixer", getCurrentTempo=lambda: 140000, getTrackName=lambda t: f"Insert {t}", getTrackVolume=lambda t: 0.8)
stub("patterns", patternNumber=lambda: 1, patternCount=lambda: 1, getPatternName=lambda i: "Pattern 1",
     getPatternLength=lambda i: 4, patternMax=lambda: 999, isPatternDefault=lambda i: i != 1)
stub("transport", isPlaying=lambda: False, getLoopMode=lambda: 0)
stub("ui", getVersion=lambda: "24.2.1", setHintMsg=lambda t: None)
stub("midi", UF_Plugin=4096, UF_SS=2, UF_None=0, widPianoRoll=3)

spec = importlib.util.spec_from_file_location("bridge", ROOT / "src" / "fl_studio_mcp" / "fl" / "device_FLStudioMCP.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

from fl_studio_mcp import midi_transport as M  # noqa: E402


class FakeClient(M.FLMidiBridge):
    def __init__(self, client_id):  # no WinMM: frames are captured instead of sent
        self.out, self.stream, self.partial = [], bytearray(), {}
        self.client_id, self.next_id = client_id, 7  # same message id on purpose

    def _send_sysex(self, frame):
        self.out.append(frame)


def event(frame):
    return types.SimpleNamespace(sysex=frame, handled=False)


a, b = FakeClient(11), FakeClient(42)
big = {"notes": [{"pitch": 45, "start": i * 0.5, "length": 0.45} for i in range(200)]}
a._send(7, json.dumps({"id": 7, "cmd": "stage_notes", "args": big}).encode())
b._send(7, json.dumps({"id": 7, "cmd": "params", "args": {"channel": 0, "limit": 300}}).encode())
assert len(a.out) > 1 and len(b.out) == 1, (len(a.out), len(b.out))

# interleave the two clients' chunks, with garbage in between
frames = []
for i in range(max(len(a.out), len(b.out))):
    if i < len(a.out):
        frames.append(a.out[i])
    if i < len(b.out):
        frames.append(b.out[i])
    frames.append(M.HEADER + bytes([1, 99, 0x7F, 0x7F, 5, 0, 2, 0]) + b"\x01\x02\xF7")  # idx >= total
frames.append(b"\xF0\x7D\x46\x42\xF7")  # truncated
for f in frames:
    bridge.OnSysEx(event(f))
bridge.OnIdle()

replies = b"".join(sent)
for client in (a, b):
    client.stream.extend(replies)
    client._drain_buffers = lambda: None
ra = json.loads(a._receive(7, 1))
rb = json.loads(b._receive(7, 1))
assert ra["ok"] and ra["result"]["count"] == 200, ra
assert rb["ok"] and rb["result"]["plugin"] == "Kontakt 8" and len(rb["result"]["params"]) == 300, str(rb)[:200]
assert not bridge._partial or all(k[0] == 99 for k in bridge._partial), bridge._partial
print(f"OK: {len(frames)} interleaved frames from 2 clients + garbage -> each client got its own reply "
      f"({len(sent)} reply frames); bridge still running")
