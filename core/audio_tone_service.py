"""Audio guidance tone synthesizer for camera focus adjustment.

Provides real-time, non-piercing acoustic feedback (Radar Ping Mode) based on
focus sharpness using GStreamer appsrc. Frequency scales gently in the mellow
tenor range (300–360 Hz) with an organic exponential decay envelope, accelerating
in tempo as focus sharpens, and playing a soothing harmonic chord (330 Hz + 495 Hz)
at peak focus (>= 95%).
"""

import threading
import time
import numpy as np
from core.logger import get_logger

logger = get_logger("audio_tone")

try:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    GST_AVAILABLE = True
except Exception:
    logger.exception("Failed to import or initialize GStreamer")
    GST_AVAILABLE = False


SAMPLE_RATE = 44100


class AudioToneService:
    def __init__(self, muted: bool = True):
        self._muted: bool = muted
        self._running: bool = False
        self._pipeline = None
        self._appsrc = None
        self._lock = threading.Lock()
        self._score: float = 0.0
        self._stop_event = threading.Event()
        self._pulse_thread = None

        # Precompute ping buffers across 11 pitch buckets (300 Hz - 360 Hz)
        # and 1 harmonious lock chord buffer (330 Hz + 495 Hz, E4 + B4)
        self._ping_buffers = [self._synth_ping(300.0 + i * 6.0) for i in range(11)]
        self._chord_buffer = self._synth_chord()

    @staticmethod
    def _synth_ping(freq: float, duration: float = 0.12, vol: float = 0.22) -> bytes:
        num_samples = int(duration * SAMPLE_RATE)
        t = np.arange(num_samples) / SAMPLE_RATE
        decay = np.exp(-t / 0.03)
        val = np.sin(2.0 * np.pi * freq * t) * (vol * decay)
        return np.clip(val * 32767, -32767, 32767).astype("<i2").tobytes()

    @staticmethod
    def _synth_chord(duration: float = 1.2, vol: float = 0.20) -> bytes:
        num_samples = int(duration * SAMPLE_RATE)
        t = np.arange(num_samples) / SAMPLE_RATE
        decay = np.exp(-t / 0.35)
        val = (np.sin(2.0 * np.pi * 330.0 * t) + 0.6 * np.sin(2.0 * np.pi * 495.0 * t)) * (vol * decay)
        return np.clip(val * 32767, -32767, 32767).astype("<i2").tobytes()

    def score_to_interval(self, score: float) -> float:
        """Convert a 0–100 sharpness score into pulse interval (0.80s down to 0.12s)."""
        clamped = max(40.0, min(95.0, float(score)))
        norm = (clamped - 40.0) / (95.0 - 40.0)
        return round(0.80 - (norm * 0.68), 3)

    def score_to_freq(self, score: float) -> float:
        """Convert a 0–100 sharpness score into mellow radar ping frequency (300–360 Hz)."""
        clamped = max(40.0, min(95.0, float(score)))
        norm = (clamped - 40.0) / (95.0 - 40.0)
        return round(300.0 + (norm * 60.0), 1)

    def start(self) -> bool:
        """Start the audio guidance pipeline and radar pulse loop."""
        if not GST_AVAILABLE:
            logger.warning("GStreamer not available, audio tone service disabled")
            return False

        with self._lock:
            if self._running:
                return True

            for sink in ["autoaudiosink", "pulsesink", "alsasink", "fakesink"]:
                try:
                    desc = (
                        f"appsrc name=src is-live=true format=time do-timestamp=true "
                        f"caps=audio/x-raw,format=S16LE,layout=interleaved,rate={SAMPLE_RATE},channels=1 "
                        f"! audioconvert ! audioresample ! {sink}"
                    )
                    self._pipeline = Gst.parse_launch(desc)
                    self._appsrc = self._pipeline.get_by_name("src")

                    ret = self._pipeline.set_state(Gst.State.PLAYING)
                    if ret != Gst.StateChangeReturn.FAILURE:
                        self._running = True
                        self._stop_event.clear()
                        self._pulse_thread = threading.Thread(
                            target=self._pulse_loop,
                            name="audio_radar_pulse",
                            daemon=True,
                        )
                        self._pulse_thread.start()
                        logger.info("Audio radar ping guidance started using sink: %s", sink)
                        return True
                    else:
                        self._pipeline.set_state(Gst.State.NULL)
                except Exception:
                    logger.debug("Failed to initialize audio with sink %s", sink)

            logger.warning("Could not initialize any audio sink for radar tone guidance")
            self._running = False
            return False

    def _pulse_loop(self) -> None:
        """Background thread executing soft radar ping pulse timing and chord playback."""
        while not self._stop_event.is_set():
            with self._lock:
                muted = self._muted
                running = self._running
                score = self._score
                appsrc = self._appsrc

            if not running or muted or score < 40.0 or not appsrc:
                self._stop_event.wait(0.05)
                continue

            # Sweet Spot (>= 95.0%): Harmonious Lock Chord (330 Hz + 495 Hz)
            if score >= 95.0:
                with self._lock:
                    if self._running and self._appsrc and not self._muted:
                        try:
                            buf = Gst.Buffer.new_wrapped(self._chord_buffer)
                            self._appsrc.emit("push-buffer", buf)
                        except Exception:
                            pass

                chord_deadline = time.monotonic() + 1.2
                while time.monotonic() < chord_deadline and not self._stop_event.is_set():
                    with self._lock:
                        curr_score = self._score
                        curr_muted = self._muted

                    if curr_muted or curr_score < 95.0:
                        break
                    self._stop_event.wait(0.05)
                continue

            # Pulsed zone (40.0 <= score < 95.0)
            interval = self.score_to_interval(score)
            norm = max(0.0, min(1.0, (score - 40.0) / (95.0 - 40.0)))
            bucket = min(10, max(0, int(round(norm * 10))))
            ping_data = self._ping_buffers[bucket]

            with self._lock:
                if self._running and self._appsrc and not self._muted:
                    try:
                        buf = Gst.Buffer.new_wrapped(ping_data)
                        self._appsrc.emit("push-buffer", buf)
                    except Exception:
                        pass

            deadline = time.monotonic() + interval
            snapshot_score = score
            while time.monotonic() < deadline and not self._stop_event.is_set():
                with self._lock:
                    curr_score = self._score
                    curr_muted = self._muted

                if curr_muted or curr_score >= 95.0 or curr_score < 40.0:
                    break
                if abs(curr_score - snapshot_score) > 6.0:
                    break
                self._stop_event.wait(0.02)

    def set_score(self, score: float) -> None:
        """Update sharpness score for the radar guidance tone."""
        with self._lock:
            self._score = float(score)

    def play_lock_chime(self) -> None:
        """Play lock chime (maintained for API compatibility; sweet spot plays harmonic chord)."""
        pass

    def set_muted(self, muted: bool) -> None:
        """Mute or unmute the audio tone."""
        with self._lock:
            self._muted = bool(muted)

    def is_muted(self) -> bool:
        with self._lock:
            return self._muted

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def stop(self) -> None:
        """Shut down the audio pipeline and pulse worker cleanly."""
        self._stop_event.set()
        if self._pulse_thread and self._pulse_thread.is_alive():
            if threading.current_thread() != self._pulse_thread:
                self._pulse_thread.join(timeout=1.0)
        self._pulse_thread = None

        with self._lock:
            self._running = False
            if self._pipeline:
                try:
                    self._pipeline.set_state(Gst.State.NULL)
                except Exception:
                    logger.exception("Error stopping GStreamer pipeline")
                self._pipeline = None
                self._appsrc = None
