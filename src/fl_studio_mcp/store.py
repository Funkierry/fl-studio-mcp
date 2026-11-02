"""Analyses, recordings and background jobs, kept in memory and on disk (%LOCALAPPDATA%/fl-studio-mcp)."""

import json
import os
import threading
import time
import traceback
import uuid
from pathlib import Path

from .audio import dsp


def data_dir():
    base = os.environ.get("FLSTUDIO_MCP_DATA") or os.path.join(os.environ.get("LOCALAPPDATA", str(Path.home())), "fl-studio-mcp")
    path = Path(base)
    (path / "analyses").mkdir(parents=True, exist_ok=True)
    (path / "audio").mkdir(parents=True, exist_ok=True)
    return path


class Store:
    def __init__(self):
        self.root = data_dir()
        self._mem = {}

    def put(self, kind, data, private=None):
        aid = f"{kind}-{time.strftime('%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        record = {"id": aid, "kind": kind, "created": time.time(), "data": data, "private": private or {}}
        self._mem[aid] = record
        (self.root / "analyses" / f"{aid}.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return aid

    def get(self, aid):
        if aid not in self._mem:
            path = self.root / "analyses" / f"{aid}.json"
            if not path.exists():
                raise KeyError(f"unknown analysis id {aid!r}")
            self._mem[aid] = json.loads(path.read_text(encoding="utf-8"))
        return self._mem[aid]

    def save_audio(self, audio, sr, name):
        path = self.root / "audio" / f"{time.strftime('%m%d-%H%M%S')}_{name}_{uuid.uuid4().hex[:6]}.wav"
        dsp.save_wav(path, audio, sr)
        return str(path)


class Jobs:
    """Run long operations (sound matching) in the background so tool calls return immediately."""

    def __init__(self):
        self._jobs = {}
        self._lock = threading.Lock()

    def active(self):
        with self._lock:
            return next((jid for jid, job in self._jobs.items() if job['status'] == 'running'), None)

    def start(self, kind, fn, *args, **kwargs):
        jid = f"{kind}-{uuid.uuid4().hex[:6]}"
        job = {"id": jid, "kind": kind, "status": "running", "progress": 0.0, "message": "starting",
               "started": time.time(), "result": None, "error": None, "cancel": threading.Event()}

        def report(progress, message):
            job["progress"], job["message"] = round(float(progress), 3), message

        def run():
            try:
                job["result"] = fn(*args, report=report, cancel=job["cancel"], **kwargs)
                job["status"] = "cancelled" if job["cancel"].is_set() else "done"
            except Exception as e:  # noqa: BLE001 — surfaced to the client
                job["status"], job["error"] = "error", f"{type(e).__name__}: {e}"
                job["trace"] = traceback.format_exc()[-1500:]
            job["progress"] = 1.0 if job["status"] == "done" else job["progress"]

        with self._lock:
            active = next((j for j in self._jobs.values() if j['status'] == 'running'), None)
            if active:
                raise ValueError(f"Audio matching job {active['id']} is still running; wait or cancel it first")
            self._jobs[jid] = job
        threading.Thread(target=run, daemon=True).start()
        return jid

    def status(self, jid, cancel=False):
        job = self._jobs.get(jid)
        if job is None:
            raise KeyError(f"unknown job {jid!r}")
        if cancel:
            job["cancel"].set()
        out = {k: v for k, v in job.items() if k not in ("cancel", "trace")}
        out["elapsed_s"] = round(time.time() - job["started"], 1)
        return out
