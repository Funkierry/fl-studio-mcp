"""Play things in FL and record exactly what FL outputs."""

import time

import numpy as np

from .audio import dsp
from .audio.capture import fl_recorder

LATENCY_MARGIN_S = 0.25  # MIDI → FL → audio buffer, measured ~0.11 s; generous margin


class Listener:
    def __init__(self, session, store):
        self.session = session
        self.store = store

    def record_playback(self, seconds=8.0, mode="pattern", channel=None):
        """Solo `channel` (optional), play the pattern/song from the start, record `seconds` of FL's output."""
        seconds = float(max(1.0, min(seconds, 60.0)))
        soloed = False
        state = None
        rec = fl_recorder()
        try:
            if channel is not None:
                self.session._call("solo", channel=channel, on=True)
                soloed = True
            state = self.session._call("listen_play", mode=mode)
            time.sleep(seconds + LATENCY_MARGIN_S)
        finally:
            audio = rec.stop()
            try:
                self.session._call("listen_stop", restore_song_mode=(state or {}).get("previous", {}).get("song_mode"))
            finally:
                if soloed:
                    self.session._call("solo", channel=channel, on=False)
        audio = dsp.trim_to_onset(audio, rec.sr, threshold_db=-50)
        info = {"capture": rec.kind, "seconds": round(len(audio) / rec.sr, 2), "mode": mode, "channel": channel}
        if np.abs(audio).max() < 1e-4:
            info["silent"] = True
        info["wav"] = self.store.save_audio(audio, rec.sr, f"listen_{mode}" + (f"_ch{channel}" if channel is not None else ""))
        return audio, rec.sr, info

    def render_note(self, channel, pitch, length_s=0.8, velocity=100, tail_s=0.7):
        """Play one note through the channel's plugin and record it."""
        rec = fl_recorder()
        try:
            self.session._call("play", channel=channel, events=[
                {"pitch": int(pitch), "at_ms": 0, "len_ms": length_s * 1000, "velocity": int(velocity)}])
            time.sleep(length_s + tail_s + LATENCY_MARGIN_S)
        except BaseException:
            self.session._call("stop_audition")
            raise
        finally:
            audio = rec.stop()
        return audio, rec.sr
