"""Audio Input and Output handling for Dalek Teleoperation using miniaudio.

miniaudio embeds its own backends (ALSA, PulseAudio, PipeWire, CoreAudio, WASAPI)
with zero external shared library dependencies (no PortAudio / libportaudio needed).

Handles:
1. Speaker Playback: Receives modulated audio frames from the mobile client
   and streams them to the system speaker sink (ALSA/PulseAudio/PipeWire).
   Peak outputs are reflected in PulseAudio/PipeWire for Dalek ear flashing.
2. Microphone Capture: Captures 48kHz audio from the local microphone
   (laptop mic, or webcam/lavalier on Pi) and pushes it to an async queue
   for the outbound WebRTC audio track back to the phone.
"""

import asyncio
import collections
import logging
import threading
from typing import Optional
import miniaudio
import numpy as np

from teleop import config

logger = logging.getLogger("teleop.audio")


class AudioPlayer:
    """Streams 48kHz audio frames out to the default speaker sink with continuous buffering."""

    def __init__(self, sample_rate: int = config.SAMPLE_RATE, max_buffer_msec: int = 250):
        self.sample_rate = sample_rate
        self.nchannels = 1
        self.max_buffer_bytes = int(sample_rate * (max_buffer_msec / 1000.0) * 2)  # 250ms = 24,000 bytes
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.device: Optional[miniaudio.PlaybackDevice] = None
        self._running = False

    def _stream_generator(self):
        """Generator yielding continuous PCM 16-bit bytes to miniaudio playback device."""
        num_frames = yield b""
        while self._running:
            needed_bytes = (num_frames if num_frames else config.FRAME_SAMPLES) * 2 * self.nchannels
            with self.lock:
                avail = len(self.buffer)
                if avail >= needed_bytes:
                    chunk = bytes(self.buffer[:needed_bytes])
                    del self.buffer[:needed_bytes]
                elif avail > 0:
                    chunk = bytes(self.buffer) + bytes(needed_bytes - avail)
                    self.buffer.clear()
                else:
                    chunk = bytes(needed_bytes)
            num_frames = yield chunk

    def start(self):
        if self._running:
            return
        logger.info("Starting AudioPlayer output stream (target sr=%d Hz)...", self.sample_rate)

        # Ensure runtime environment variables are set so PulseAudio/PipeWire user sockets connect cleanly
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        if "XDG_RUNTIME_DIR" not in os.environ:
            os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
        if "PULSE_SERVER" not in os.environ and os.path.exists(f"/run/user/{uid}/pulse/native"):
            os.environ["PULSE_SERVER"] = f"unix:/run/user/{uid}/pulse/native"

        # Candidate backends: PulseAudio first, ALSA second, Default third
        backend_candidates = [
            ("PulseAudio", [miniaudio.Backend.PULSEAUDIO]),
            ("ALSA", [miniaudio.Backend.ALSA]),
            ("Default", None),
        ]

        device = None
        bound_ch = 1
        for name, backends in backend_candidates:
            # Try stereo first (broadest hardware compatibility on Pi/ALSA), then mono
            for ch in [2, 1]:
                try:
                    d = miniaudio.PlaybackDevice(
                        output_format=miniaudio.SampleFormat.SIGNED16,
                        nchannels=ch,
                        sample_rate=self.sample_rate,
                        buffersize_msec=50,
                        backends=backends,
                    )
                    device = d
                    bound_ch = ch
                    logger.info("AudioPlayer initialized via %s (%s, %d ch, %d Hz).", name, d.backend, ch, self.sample_rate)
                    break
                except Exception as e:
                    logger.debug("Playback attempt %s (%d ch) failed: %s", name, ch, e)
            if device is not None:
                break

        if device is not None:
            self.device = device
            self.nchannels = bound_ch
            self.max_buffer_bytes = int(self.sample_rate * (250 / 1000.0) * 2 * self.nchannels)
            self._running = True
            gen = self._stream_generator()
            next(gen)  # Prime generator
            self.device.start(gen)
            logger.info("AudioPlayer playback device active.")
        else:
            logger.warning("Could not initialize audio playback device. Playback will be mocked/logged.")
            self._running = True

    def write(self, frame: np.ndarray):
        """Queue audio frame for continuous output."""
        if not self._running:
            return
        if frame.ndim > 1:
            frame = frame.flatten()
        if frame.dtype != np.int16:
            # float in [-1.0, 1.0] -> signed 16-bit PCM
            pcm_int16 = (np.clip(frame, -1.0, 1.0) * 32767.0).astype(np.int16)
        else:
            pcm_int16 = frame

        # If audio device opened in stereo, duplicate mono to both channels
        if self.nchannels == 2:
            pcm_int16 = np.repeat(pcm_int16, 2)

        data = pcm_int16.tobytes()

        with self.lock:
            # If buffer exceeds max limit (250ms), drop oldest data to avoid lag
            if len(self.buffer) + len(data) > self.max_buffer_bytes:
                excess = (len(self.buffer) + len(data)) - self.max_buffer_bytes
                del self.buffer[:excess]
            self.buffer.extend(data)

    def stop(self):
        self._running = False
        if self.device is not None:
            try:
                self.device.stop()
                self.device.close()
            except Exception:
                pass
            self.device = None
        with self.lock:
            self.buffer.clear()


class AudioCapture:
    """Captures 48kHz audio from local microphone in 20 ms frames for WebRTC."""

    def __init__(self, sample_rate: int = config.SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.nchannels = 1
        self.device: Optional[miniaudio.CaptureDevice] = None
        self._running = False
        self._subscribers = set()
        self._loop = None
        self._chunk_samples = config.FRAME_SAMPLES
        self._partial_buffer = bytearray()

    def _capture_generator(self):
        """Generator receiving captured audio bytes via .send(data)."""
        data = yield  # Prime generator
        while self._running:
            if data:
                self._partial_buffer.extend(data)
                bytes_per_frame = self._chunk_samples * 2 * self.nchannels  # 16-bit mono or stereo

                while len(self._partial_buffer) >= bytes_per_frame:
                    frame_bytes = bytes(self._partial_buffer[:bytes_per_frame])
                    del self._partial_buffer[:bytes_per_frame]

                    pcm_int16 = np.frombuffer(frame_bytes, dtype=np.int16)
                    # If captured in stereo, average channels to mono for WebRTC transmission
                    if self.nchannels == 2:
                        pcm_int16 = ((pcm_int16[0::2].astype(np.int32) + pcm_int16[1::2].astype(np.int32)) // 2).astype(np.int16)

                    pcm_float = pcm_int16.astype(np.float32) / 32768.0

                    if self._loop and self._loop.is_running():
                        def _push_to_queues(frame):
                            for queue in list(self._subscribers):
                                try:
                                    queue.put_nowait(frame)
                                except asyncio.QueueFull:
                                    try:
                                        queue.get_nowait()
                                        queue.put_nowait(frame)
                                    except Exception:
                                        pass

                        self._loop.call_soon_threadsafe(_push_to_queues, pcm_float)
            data = yield

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        if self._running:
            return
        self._loop = loop or asyncio.get_event_loop()
        logger.info("Starting AudioCapture microphone stream (target sr=%d Hz)...", self.sample_rate)

        uid = os.getuid() if hasattr(os, "getuid") else 1000
        if "XDG_RUNTIME_DIR" not in os.environ:
            os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
        if "PULSE_SERVER" not in os.environ and os.path.exists(f"/run/user/{uid}/pulse/native"):
            os.environ["PULSE_SERVER"] = f"unix:/run/user/{uid}/pulse/native"

        backend_candidates = [
            ("PulseAudio", [miniaudio.Backend.PULSEAUDIO]),
            ("ALSA", [miniaudio.Backend.ALSA]),
            ("Default", None),
        ]

        device = None
        bound_ch = 1
        for name, backends in backend_candidates:
            # Try stereo first (many USB/webcam mics only negotiate 2ch), then mono
            for ch in [2, 1]:
                try:
                    d = miniaudio.CaptureDevice(
                        input_format=miniaudio.SampleFormat.SIGNED16,
                        nchannels=ch,
                        sample_rate=self.sample_rate,
                        buffersize_msec=50,
                        backends=backends,
                    )
                    device = d
                    bound_ch = ch
                    logger.info("AudioCapture initialized via %s (%s, %d ch, %d Hz).", name, d.backend, ch, self.sample_rate)
                    break
                except Exception as e:
                    logger.debug("Capture attempt %s (%d ch) failed: %s", name, ch, e)
            if device is not None:
                break

        if device is not None:
            self.device = device
            self.nchannels = bound_ch
            self._running = True
            gen = self._capture_generator()
            next(gen)  # Prime generator
            self.device.start(gen)
            logger.info("AudioCapture recording active.")
        else:
            logger.warning("Could not initialize audio capture device. Using synthetic silence.")
            self._running = True
            self._loop.create_task(self._synthetic_silence_feed())

    async def _synthetic_silence_feed(self):
        silence = np.zeros(config.FRAME_SAMPLES, dtype=np.float32)
        while self._running:
            for queue in list(self._subscribers):
                try:
                    queue.put_nowait(silence)
                except asyncio.QueueFull:
                    pass
            await asyncio.sleep(config.FRAME_MS / 1000.0)

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=15)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers.discard(q)

    def stop(self):
        self._running = False
        if self.device is not None:
            try:
                self.device.stop()
                self.device.close()
            except Exception:
                pass
            self.device = None
        self._subscribers.clear()
        self._partial_buffer.clear()
