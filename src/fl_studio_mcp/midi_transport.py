"""
SysEx transport to the FL Studio MCP Bridge, via WinMM (ctypes, no dependencies).

  this process -> MIDI out "FL Agent"       -> FL (bridge script, OnSysEx)
  FL           -> MIDI out "FL Agent Reply" -> this process's MIDI in

Frame format and 7-bit packing must match fl/device_FLStudioMCP.py.
"""

import ctypes
import json
import random
import time
from collections import deque
from ctypes import wintypes

DEFAULT_OUT_PORT = "FL Agent"
DEFAULT_IN_PORT = "FL Agent Reply"
HEADER = bytes([0xF0, 0x7D, 0x46, 0x42])  # 'FB': protocol v5 (with client id)
KIND_REQUEST, KIND_REPLY = 1, 2
CHUNK_RAW = 512
IN_BUFFERS, IN_BUFFER_SIZE = 16, 8192

MHDR_DONE = 0x01
MMSYSERR_NOERROR = 0

winmm = ctypes.windll.winmm


class MIDIHDR(ctypes.Structure):
    pass


MIDIHDR._fields_ = [
    ("lpData", ctypes.c_void_p),
    ("dwBufferLength", wintypes.DWORD),
    ("dwBytesRecorded", wintypes.DWORD),
    ("dwUser", ctypes.c_size_t),
    ("dwFlags", wintypes.DWORD),
    ("lpNext", ctypes.POINTER(MIDIHDR)),
    ("reserved", ctypes.c_size_t),
    ("dwOffset", wintypes.DWORD),
    ("dwReserved", ctypes.c_size_t * 8),
]


class MIDIINCAPSW(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("vDriverVersion", wintypes.UINT),
                ("szPname", wintypes.WCHAR * 32), ("dwSupport", wintypes.DWORD)]


class MIDIOUTCAPSW(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("vDriverVersion", wintypes.UINT),
                ("szPname", wintypes.WCHAR * 32), ("wTechnology", wintypes.WORD), ("wVoices", wintypes.WORD),
                ("wNotes", wintypes.WORD), ("wChannelMask", wintypes.WORD), ("dwSupport", wintypes.DWORD)]


def list_ports():
    ins, outs = [], []
    for i in range(winmm.midiInGetNumDevs()):
        c = MIDIINCAPSW()
        winmm.midiInGetDevCapsW(i, ctypes.byref(c), ctypes.sizeof(c))
        ins.append(c.szPname)
    for i in range(winmm.midiOutGetNumDevs()):
        c = MIDIOUTCAPSW()
        winmm.midiOutGetDevCapsW(i, ctypes.byref(c), ctypes.sizeof(c))
        outs.append(c.szPname)
    return ins, outs


def _check(rc, what):
    if rc != MMSYSERR_NOERROR:
        raise OSError(f"{what} failed (MMSYSERR {rc})")


def pack7(data):
    out = bytearray()
    for i in range(0, len(data), 7):
        group = data[i:i + 7]
        out.append(sum(1 << j for j, b in enumerate(group) if b & 0x80))
        out.extend(b & 0x7F for b in group)
    return bytes(out)


def unpack7(data):
    out = bytearray()
    for i in range(0, len(data), 8):
        msb = data[i]
        out.extend(b | (0x80 if msb & (1 << j) else 0) for j, b in enumerate(data[i + 1:i + 8]))
    return bytes(out)


def fix_mojibake(s):
    """FL returns non-ASCII strings as UTF-8 bytes mis-decoded as cp1252 (e.g. 'éŸ³é‡'); undo that."""
    if not isinstance(s, str) or s.isascii():
        return s
    raw = bytearray()
    for c in s:
        try:
            raw.extend(c.encode("cp1252"))
        except UnicodeEncodeError:
            if ord(c) > 0xFF:
                return s
            raw.append(ord(c))
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return s


def _fix_strings(obj):
    if isinstance(obj, str):
        return fix_mojibake(obj)
    if isinstance(obj, list):
        return [_fix_strings(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _fix_strings(v) for k, v in obj.items()}
    return obj


def _b14(v):
    return bytes([v & 0x7F, (v >> 7) & 0x7F])


def _u14(lo, hi):
    return lo | (hi << 7)


class FLMidiBridge:
    def __init__(self, out_port=DEFAULT_OUT_PORT, in_port=DEFAULT_IN_PORT):
        self.out_port, self.in_port = out_port, in_port
        ins, outs = list_ports()
        if out_port not in outs or in_port not in ins:
            raise ConnectionError(
                f"缺少 loopMIDI 端口。需要输出 {out_port!r} 和输入 {in_port!r}。\n"
                f"  当前输入: {ins}\n  当前输出: {outs}")
        self.hout = wintypes.HANDLE()
        self.hin = wintypes.HANDLE()
        _check(winmm.midiOutOpen(ctypes.byref(self.hout), outs.index(out_port), 0, 0, 0), f"open MIDI out {out_port!r}")
        rc = winmm.midiInOpen(ctypes.byref(self.hin), ins.index(in_port), 0, 0, 0)
        if rc != MMSYSERR_NOERROR:
            winmm.midiOutClose(self.hout)
            raise OSError(f"cannot open MIDI in {in_port!r} (MMSYSERR {rc}). It is probably in use: either FL has "
                          f"this input enabled (disable it in FL's MIDI settings) or another fl-studio-mcp "
                          f"process is running (only one can connect at a time; use --transport http to share)")
        self.buffers = deque()
        for _ in range(IN_BUFFERS):
            buf = ctypes.create_string_buffer(IN_BUFFER_SIZE)
            hdr = MIDIHDR(lpData=ctypes.cast(buf, ctypes.c_void_p), dwBufferLength=IN_BUFFER_SIZE)
            _check(winmm.midiInPrepareHeader(self.hin, ctypes.byref(hdr), ctypes.sizeof(hdr)), "midiInPrepareHeader")
            _check(winmm.midiInAddBuffer(self.hin, ctypes.byref(hdr), ctypes.sizeof(hdr)), "midiInAddBuffer")
            self.buffers.append((buf, hdr))
        _check(winmm.midiInStart(self.hin), "midiInStart")
        self.stream = bytearray()
        self.partial = {}
        # several server instances may share the ports (e.g. one per client session): FL echoes this id back so
        # each instance only picks up its own replies
        self.client_id = random.randint(1, 127)
        self.next_id = random.randint(1, 16000)

    # -------------------------------------------------- send
    def _send_sysex(self, frame):
        buf = ctypes.create_string_buffer(frame, len(frame))
        hdr = MIDIHDR(lpData=ctypes.cast(buf, ctypes.c_void_p), dwBufferLength=len(frame))
        _check(winmm.midiOutPrepareHeader(self.hout, ctypes.byref(hdr), ctypes.sizeof(hdr)), "midiOutPrepareHeader")
        _check(winmm.midiOutLongMsg(self.hout, ctypes.byref(hdr), ctypes.sizeof(hdr)), "midiOutLongMsg")
        while not hdr.dwFlags & MHDR_DONE:
            time.sleep(0.001)
        winmm.midiOutUnprepareHeader(self.hout, ctypes.byref(hdr), ctypes.sizeof(hdr))

    def _send(self, msg_id, data):
        chunks = [data[i:i + CHUNK_RAW] for i in range(0, len(data), CHUNK_RAW)] or [b""]
        for idx, chunk in enumerate(chunks):
            self._send_sysex(HEADER + bytes([KIND_REQUEST, self.client_id]) + _b14(msg_id) + _b14(idx) + _b14(len(chunks))
                             + pack7(chunk) + b"\xF7")

    # -------------------------------------------------- receive (poll filled input buffers)
    def _drain_buffers(self):
        while self.buffers and self.buffers[0][1].dwFlags & MHDR_DONE:
            buf, hdr = self.buffers.popleft()
            self.stream.extend(buf.raw[:hdr.dwBytesRecorded])
            winmm.midiInUnprepareHeader(self.hin, ctypes.byref(hdr), ctypes.sizeof(hdr))
            hdr.dwFlags = 0
            hdr.dwBytesRecorded = 0
            winmm.midiInPrepareHeader(self.hin, ctypes.byref(hdr), ctypes.sizeof(hdr))
            winmm.midiInAddBuffer(self.hin, ctypes.byref(hdr), ctypes.sizeof(hdr))
            self.buffers.append((buf, hdr))

    def _pop_frames(self):
        frames = []
        while True:
            start = self.stream.find(b"\xF0")
            if start < 0:
                self.stream.clear()
                break
            end = self.stream.find(b"\xF7", start)
            if end < 0:
                del self.stream[:start]
                break
            frames.append(bytes(self.stream[start:end + 1]))
            del self.stream[:end + 1]
        return frames

    def _receive(self, msg_id, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._drain_buffers()
            for f in self._pop_frames():
                if len(f) < 13 or f[:4] != HEADER or f[4] != KIND_REPLY or f[5] != self.client_id:
                    continue
                mid, idx, total = _u14(f[6], f[7]), _u14(f[8], f[9]), _u14(f[10], f[11])
                parts = self.partial.setdefault(mid, {})
                parts[idx] = unpack7(f[12:-1])
                if len(parts) == total:
                    del self.partial[mid]
                    if mid == msg_id:
                        return b"".join(parts[i] for i in range(total))
            time.sleep(0.002)
        raise TimeoutError(
            f"no reply from FL within {timeout}s. Check that FL is running with the 'FL Studio MCP Bridge' "
            f"controller script on input {self.out_port!r} and that FL's MIDI output {self.in_port!r} has the same "
            f"port number. If it worked before, the script in FL is outdated or stopped: run `fl-studio-mcp install`, "
            f"then in FL use View → Script output → Reload script (or restart FL)")

    # -------------------------------------------------- public API
    def call(self, cmd, timeout=5.0, **args):
        msg_id = self.next_id
        self.next_id = self.next_id % 16000 + 1
        t0 = time.perf_counter()
        self._send(msg_id, json.dumps({"id": msg_id, "cmd": cmd, "args": args}).encode("utf-8"))
        resp = _fix_strings(json.loads(self._receive(msg_id, timeout).decode("utf-8")))
        resp["_rtt_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return resp

    def close(self):
        winmm.midiInStop(self.hin)
        winmm.midiInReset(self.hin)
        for _, hdr in self.buffers:
            winmm.midiInUnprepareHeader(self.hin, ctypes.byref(hdr), ctypes.sizeof(hdr))
        winmm.midiInClose(self.hin)
        winmm.midiOutClose(self.hout)
