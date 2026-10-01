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
from typing import Optional
import miniaudio
import numpy as np

from teleop import config

logger = logging.getLogger("teleop.audio")


class AudioPlayer:
    """Streams 48kHz float32 audio frames out to the default speaker sink."""

    def __init__(self, sample_rate: int = config.SAMPLE_RATE, max_buffer_frames: int = 8):
        self.sample_rate = sample_rate
        self.buffer = collections.deque(maxlen=max_buffer_frames)
        self.device: Optional[miniaudio.PlaybackDevice] = None
        self._running = False
        self._silence_int16 = bytes(config.FRAME_SAMPLES * 2)  # 16-bit mono silence

    def _stream_generator(self):
        """Generator yielding PCM 16-bit bytes to miniaudio playback device."""
        # Prime generator for first .send(num_frames)
        num_frames = yield b""
        while self._running:
            needed_bytes = (num_frames if num_frames else config.FRAME_SAMPLES) * 2
            if self.buffer:
                frame_bytes = self.buffer.popleft()
                if len(frame_bytes) < needed_bytes:
                    frame_bytes = frame_bytes + bytes(needed_bytes - len(frame_bytes))
                elif len(frame_bytes) > needed_bytes:
                    # Put remainder back
                    remainder = frame_bytes[needed_bytes:]
                    frame_bytes = frame_bytes[:needed_bytes]
                    self.buffer.appendleft(remainder)
                num_frames = yield frame_bytes
            else:
                num_frames = yield bytes(needed_bytes)

    def start(self):
        if self._running:
            return
        logger.info("Starting miniaudio AudioPlayer at %d Hz...", self.sample_rate)
        try:
            self.device = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=1,
                sample_rate=self.sample_rate,
            )
            self._running = True
            gen = self._stream_generator()
            next(gen)  # Prime generator
            self.device.start(gen)
            logger.info("AudioPlayer playback device active via miniaudio.")
        except Exception as e:
            logger.warning("Could not initialize miniaudio playback device (%s). Playback will be mocked/logged.", e)
            self._running = True

    def write(self, frame: np.ndarray):
        """Queue a 20 ms float32 frame in [-1.0, 1.0] for output."""
        if not self._running:
            return
        if frame.ndim > 1:
            frame = frame.flatten()
        # Convert float32 in [-1.0, 1.0] to signed 16-bit PCM bytes
        pcm_int16 = (np.clip(frame, -1.0, 1.0) * 32767.0).astype(np.int16)
        self.buffer.append(pcm_int16.tobytes())

    def stop(self):
        self._running = False
        if self.device is not None:
            try:
                self.device.stop()
                self.device.close()
            except Exception:
                pass
            self.device = None
        self.buffer.clear()


class AudioCapture:
    """Captures 48kHz audio from local microphone in 20 ms frames for WebRTC."""

    def __init__(self, sample_rate: int = config.SAMPLE_RATE):
        self.sample_rate = sample_rate
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
                bytes_per_frame = self._chunk_samples * 2  # 16-bit mono

                while len(self._partial_buffer) >= bytes_per_frame:
                    frame_bytes = bytes(self._partial_buffer[:bytes_per_frame])
                    del self._partial_buffer[:bytes_per_frame]

                    pcm_int16 = np.frombuffer(frame_bytes, dtype=np.int16)
                    pcm_float = pcm_int16.astype(np.float32) / 32768.0

                    if self._loop and self._loop.is_running():
                        for queue in list(self._subscribers):
                            try:
                                self._loop.call_soon_threadsafe(queue.put_nowait, pcm_float)
                            except asyncio.QueueFull:
                                try:
                                    queue.get_nowait()
                                    queue.put_nowait(pcm_float)
                                except Exception:
                                    pass
            data = yield

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        if self._running:
            return
        self._loop = loop or asyncio.get_event_loop()
        logger.info("Starting miniaudio AudioCapture at %d Hz...", self.sample_rate)
        try:
            self.device = miniaudio.CaptureDevice(
                input_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=1,
                sample_rate=self.sample_rate,
            )
            self._running = True
            gen = self._capture_generator()
            next(gen)  # Prime generator
            self.device.start(gen)
            logger.info("AudioCapture recording active via miniaudio.")
        except Exception as e:
            logger.warning("Could not initialize miniaudio capture device (%s). Using synthetic silence.", e)
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
