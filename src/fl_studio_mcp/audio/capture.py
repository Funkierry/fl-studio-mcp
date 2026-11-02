"""
Record what FL Studio plays.

Primary: WASAPI process loopback (Microsoft documents build 20348+) — captures only FL64.exe and its child processes, so a video
playing in the browser doesn't end up in the analysis. Implemented with ctypes; no extra dependencies.
Fallback: system loopback of the default output device via the `soundcard` package (captures every app).

Packets are placed on a timeline by their QPC timestamps, and time.perf_counter() uses the same clock on Windows,
so callers can line recordings up with the moment they sent MIDI to FL.
"""

import ctypes
import threading
import time
from ctypes import POINTER, WINFUNCTYPE, byref, c_long, c_ubyte, c_uint32, c_uint64, c_ulong, c_ushort, c_void_p, wintypes

import numpy as np

SAMPLE_RATE = 48000
CHANNELS = 2

S_OK = 0
E_NOINTERFACE = ctypes.c_long(0x80004002).value
VT_BLOB = 65
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
WAVE_FORMAT_PCM = 1
VAD_PROCESS_LOOPBACK = "VAD\\Process_Loopback"


class CaptureError(RuntimeError):
    pass


class GUID(ctypes.Structure):
    _fields_ = [("d1", c_ulong), ("d2", c_ushort), ("d3", c_ushort), ("d4", c_ubyte * 8)]

    @classmethod
    def parse(cls, s):
        import uuid
        return cls.from_buffer_copy(uuid.UUID(s).bytes_le)

    def __eq__(self, other):
        return bytes(self) == bytes(other)


IID_IUnknown = GUID.parse("{00000000-0000-0000-C000-000000000046}")
IID_IAgileObject = GUID.parse("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
IID_IActivateAudioInterfaceCompletionHandler = GUID.parse("{41D949AB-9862-444A-80F6-C261334DA5EB}")
IID_IAudioClient = GUID.parse("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = GUID.parse("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")


class PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", ctypes.c_int)]


class ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [("ActivationType", ctypes.c_int), ("ProcessLoopbackParams", PROCESS_LOOPBACK_PARAMS)]


class BLOB(ctypes.Structure):
    _fields_ = [("cbSize", c_ulong), ("pBlobData", c_void_p)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort), ("blob", BLOB)]


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD), ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD), ("nBlockAlign", wintypes.WORD),
                ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD)]


def _vcall(obj, index, argtypes, *args):
    """Call method `index` of the COM vtable of `obj`; raise on failure HRESULT."""
    vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
    fn = WINFUNCTYPE(c_long, c_void_p, *argtypes)(vtbl[index])
    hr = fn(obj, *args)
    if hr < 0:
        raise CaptureError(f"COM call #{index} failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}")
    return hr


def _release(obj):
    if obj:
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
        WINFUNCTYPE(c_ulong, c_void_p)(vtbl[2])(obj)


# ---------------------------------------------------------------- process lookup

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def find_process(names=("FL64.exe", "FL.exe", "FL64 (scaled).exe")):
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    entry = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
    wanted = {n.lower() for n in names}
    try:
        ok = k32.Process32FirstW(snap, byref(entry))
        while ok:
            if entry.szExeFile.lower() in wanted:
                return entry.th32ProcessID
            ok = k32.Process32NextW(snap, byref(entry))
    finally:
        k32.CloseHandle(snap)
    return None


# ---------------------------------------------------------------- activation completion handler (a tiny COM object)

QI_T = WINFUNCTYPE(c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))
REF_T = WINFUNCTYPE(c_ulong, c_void_p)
DONE_T = WINFUNCTYPE(c_long, c_void_p, c_void_p)


class _HandlerVtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", QI_T), ("AddRef", REF_T), ("Release", REF_T), ("ActivateCompleted", DONE_T)]


class _Handler(ctypes.Structure):
    _fields_ = [("lpVtbl", POINTER(_HandlerVtbl))]


class _CompletionHandler:
    def __init__(self):
        self.done = threading.Event()
        self.operation = None

        def qi(this, riid, ppv):
            if riid[0] in (IID_IUnknown, IID_IAgileObject, IID_IActivateAudioInterfaceCompletionHandler):
                ppv[0] = this
                return S_OK
            ppv[0] = None
            return E_NOINTERFACE

        def completed(this, operation):
            self.operation = operation
            self.done.set()
            return S_OK

        self._fns = (QI_T(qi), REF_T(lambda this: 1), REF_T(lambda this: 1), DONE_T(completed))
        self._vtbl = _HandlerVtbl(*self._fns)
        self.obj = _Handler(ctypes.pointer(self._vtbl))

    @property
    def pointer(self):
        return ctypes.addressof(self.obj)


# ---------------------------------------------------------------- recorders

class ProcessLoopbackRecorder:
    """Record one process tree's audio output. Use as a context manager, then read .audio."""

    kind = "process"

    def __init__(self, pid, samplerate=SAMPLE_RATE, channels=CHANNELS):
        self.pid, self.sr, self.ch = pid, samplerate, channels
        self._thread = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error = None
        self._packets = []      # (qpc_100ns, int16 array)
        self.t_start = self.t_stop = None
        self.audio = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise CaptureError("audio capture did not start within 5 s")
        if self._error:
            raise self._error
        return self

    def __exit__(self, *exc):
        self.stop()

    def stop(self):
        if self._thread is None:
            return self.audio
        self.t_stop = time.perf_counter()
        self._stop.set()
        self._thread.join(5)
        self._thread = None
        if self._error:
            raise self._error
        self.audio = self._assemble()
        return self.audio

    def _assemble(self):
        n = max(1, int((self.t_stop - self.t_start) * self.sr))
        out = np.zeros((n, self.ch), dtype=np.float32)
        t0 = self.t_start * 1e7
        for qpc, data in self._packets:
            start = int(round((qpc - t0) * self.sr / 1e7))
            frames = data.reshape(-1, self.ch).astype(np.float32) / 32768.0
            if start < 0:
                frames, start = frames[-start:], 0
            end = min(n, start + len(frames))
            if end > start:
                out[start:end] = frames[:end - start]
        return out

    def _run(self):
        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx(None, 0)  # MTA
        client = capture = op = None
        event = None
        try:
            params = ACTIVATION_PARAMS(AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
                                       PROCESS_LOOPBACK_PARAMS(self.pid, PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE))
            pv = PROPVARIANT(vt=VT_BLOB, blob=BLOB(ctypes.sizeof(params), ctypes.cast(byref(params), c_void_p)))
            handler = _CompletionHandler()
            op_ptr = c_void_p()
            mmdevapi = ctypes.windll.Mmdevapi
            mmdevapi.ActivateAudioInterfaceAsync.restype = c_long
            mmdevapi.ActivateAudioInterfaceAsync.argtypes = [wintypes.LPCWSTR, POINTER(GUID), POINTER(PROPVARIANT),
                                                             c_void_p, POINTER(c_void_p)]
            hr = mmdevapi.ActivateAudioInterfaceAsync(VAD_PROCESS_LOOPBACK, byref(IID_IAudioClient), byref(pv),
                                                      handler.pointer, byref(op_ptr))
            if hr < 0:
                raise CaptureError(f"ActivateAudioInterfaceAsync failed: 0x{hr & 0xFFFFFFFF:08X} "
                                   f"(process loopback needs Windows 10 2004 or later)")
            if not handler.done.wait(5):
                raise CaptureError("audio interface activation timed out")
            op = op_ptr.value
            hr_act, unk = c_long(), c_void_p()
            _vcall(op, 3, [POINTER(c_long), POINTER(c_void_p)], byref(hr_act), byref(unk))
            if hr_act.value < 0:
                raise CaptureError(f"process loopback activation failed: 0x{hr_act.value & 0xFFFFFFFF:08X}")
            client = unk.value

            fmt = WAVEFORMATEX(WAVE_FORMAT_PCM, self.ch, self.sr, self.sr * self.ch * 2, self.ch * 2, 16, 0)
            flags = (AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                     | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY)
            _vcall(client, 3, [ctypes.c_int, wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong,
                               POINTER(WAVEFORMATEX), c_void_p], 0, flags, 2_000_000, 0, byref(fmt), None)
            k32 = ctypes.windll.kernel32
            k32.CreateEventW.restype = wintypes.HANDLE
            event = k32.CreateEventW(None, False, False, None)
            _vcall(client, 13, [wintypes.HANDLE], event)
            cap = c_void_p()
            _vcall(client, 14, [POINTER(GUID), POINTER(c_void_p)], byref(IID_IAudioCaptureClient), byref(cap))
            capture = cap.value
            _vcall(client, 10, [])  # Start
            self.t_start = time.perf_counter()
            self._ready.set()

            data_p, frames, bflags, devpos, qpc = c_void_p(), c_uint32(), wintypes.DWORD(), c_uint64(), c_uint64()
            next_size = c_uint32()
            while not self._stop.is_set():
                k32.WaitForSingleObject(event, 50)
                while True:
                    _vcall(capture, 5, [POINTER(c_uint32)], byref(next_size))
                    if next_size.value == 0:
                        break
                    _vcall(capture, 3, [POINTER(c_void_p), POINTER(c_uint32), POINTER(wintypes.DWORD),
                                        POINTER(c_uint64), POINTER(c_uint64)],
                           byref(data_p), byref(frames), byref(bflags), byref(devpos), byref(qpc))
                    n = frames.value * self.ch
                    if bflags.value & AUDCLNT_BUFFERFLAGS_SILENT or not data_p.value:
                        chunk = np.zeros(n, dtype=np.int16)
                    else:
                        chunk = np.ctypeslib.as_array(ctypes.cast(data_p, POINTER(ctypes.c_int16)), (n,)).copy()
                    self._packets.append((qpc.value, chunk))
                    _vcall(capture, 4, [c_uint32], frames.value)
            _vcall(client, 11, [])  # Stop
        except CaptureError as e:
            self._error = e
        except Exception as e:  # noqa: BLE001 — surface anything from the capture thread
            self._error = CaptureError(f"{type(e).__name__}: {e}")
        finally:
            self._ready.set()
            _release(capture)
            _release(client)
            _release(op)
            if event:
                ctypes.windll.kernel32.CloseHandle(event)
            ole32.CoUninitialize()


class SystemLoopbackRecorder:
    """Fallback: everything the default output device plays (needs the `soundcard` package)."""

    kind = "system"

    def __init__(self, samplerate=SAMPLE_RATE, channels=CHANNELS):
        try:
            import soundcard
        except ImportError as e:
            raise CaptureError("system loopback needs `pip install soundcard`") from e
        speaker = soundcard.default_speaker()
        self._mic = soundcard.get_microphone(id=str(speaker.name), include_loopback=True)
        self.sr, self.ch = samplerate, channels
        self._chunks, self._stop, self._thread = [], threading.Event(), None
        self.t_start = self.t_stop = None
        self.audio = None

    def __enter__(self):
        started = threading.Event()

        def run():
            with self._mic.recorder(samplerate=self.sr, channels=self.ch) as r:
                self.t_start = time.perf_counter()
                started.set()
                while not self._stop.is_set():
                    self._chunks.append(r.record(numframes=self.sr // 20))

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        started.wait(5)
        return self

    def __exit__(self, *exc):
        self.stop()

    def stop(self):
        if self._thread is not None:
            self.t_stop = time.perf_counter()
            self._stop.set()
            self._thread.join(5)
            self._thread = None
            self.audio = np.concatenate(self._chunks).astype(np.float32) if self._chunks else np.zeros((1, self.ch), np.float32)
        return self.audio


def fl_recorder(samplerate=SAMPLE_RATE, allow_system_fallback=True):
    """A recorder for FL's audio: process loopback if possible, else (optionally) system loopback."""
    pid = find_process()
    if pid is None:
        raise CaptureError("FL Studio (FL64.exe) is not running")
    try:
        rec = ProcessLoopbackRecorder(pid, samplerate)
        rec.__enter__()
        return rec
    except CaptureError:
        if not allow_system_fallback:
            raise
    rec = SystemLoopbackRecorder(samplerate)
    rec.__enter__()
    return rec
