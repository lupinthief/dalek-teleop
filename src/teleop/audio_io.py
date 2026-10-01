"""Audio Input and Output handling for Dalek Teleoperation.

Handles:
1. Speaker Playback: Receives modulated audio frames from the mobile client
   and streams them to the system speaker via sounddevice / ALSA / PulseAudio.
   Peak outputs are naturally reflected in PulseAudio/PipeWire for Dalek ear flashing.
2. Microphone Capture: Captures 48kHz audio from the local microphone
   (laptop mic, or webcam/lavalier on Pi) and pushes it to an async queue
   for the outbound WebRTC audio track back to the phone.
"""

import asyncio
import collections
import logging
from typing import Optional
import numpy as np
import sounddevice as sd

from teleop import config

logger = logging.getLogger("teleop.audio")


class AudioPlayer:
    """Streams 48kHz float32 audio frames out to the default speaker sink."""

    def __init__(self, sample_rate: int = config.SAMPLE_RATE, max_buffer_frames: int = 8):
        self.sample_rate = sample_rate
        # Bounded frame buffer: drop oldest frames if speaker clock lags behind network
        self.buffer = collections.deque(maxlen=max_buffer_frames)
        self.stream: Optional[sd.OutputStream] = None
        self._running = False
        self._silence = np.zeros(config.FRAME_SAMPLES, dtype=np.float32)

    def _callback(self, outdata, frames, time_info, status):
        if status:
            logger.debug("Speaker stream status: %s", status)
        try:
            if self.buffer:
                frame = self.buffer.popleft()
                if len(frame) == frames:
                    outdata[:, 0] = frame
                    return
                elif len(frame) > frames:
                    outdata[:, 0] = frame[:frames]
                    return
                else:
                    outdata[:len(frame), 0] = frame
                    outdata[len(frame):, 0] = 0.0
                    return
            outdata.fill(0.0)
        except Exception as e:
            logger.error("Audio playback callback error: %s", e)
            outdata.fill(0.0)

    def start(self):
        if self._running:
            return
        logger.info("Starting AudioPlayer output stream at %d Hz...", self.sample_rate)
        try:
            self.stream = sd.OutputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=config.FRAME_SAMPLES,
                callback=self._callback,
            )
            self.stream.start()
            self._running = True
        except Exception as e:
            logger.warning("Could not open real sounddevice output stream (%s). Audio output will be mocked/logged.", e)
            self._running = True

    def write(self, frame: np.ndarray):
        """Queue a 20 ms float32 frame for output."""
        if not self._running:
            return
        if frame.ndim > 1:
            frame = frame.flatten()
        self.buffer.append(frame.astype(np.float32))

    def stop(self):
        self._running = False
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.buffer.clear()


class AudioCapture:
    """Captures 48kHz audio from local microphone in 20 ms frames for WebRTC."""

    def __init__(self, sample_rate: int = config.SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.stream: Optional[sd.InputStream] = None
        self._running = False
        self._subscribers = set()
        self._loop = None

    def _callback(self, indata, frames, time_info, status):
        if status:
            logger.debug("Microphone stream status: %s", status)
        frame = indata[:, 0].copy()
        if self._loop and self._loop.is_running():
            for queue in list(self._subscribers):
                try:
                    self._loop.call_soon_threadsafe(queue.put_nowait, frame)
                except asyncio.QueueFull:
                    # Drop oldest to avoid lag
                    try:
                        queue.get_nowait()
                        queue.put_nowait(frame)
                    except Exception:
                        pass

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        if self._running:
            return
        self._loop = loop or asyncio.get_event_loop()
        logger.info("Starting AudioCapture microphone stream at %d Hz...", self.sample_rate)
        try:
            self.stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=config.FRAME_SAMPLES,
                callback=self._callback,
            )
            self.stream.start()
            self._running = True
        except Exception as e:
            logger.warning("Could not open real sounddevice input stream (%s). Generating silence generator.", e)
            self._running = True
            # Spawn a task to feed periodic silence if no hardware mic
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
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self._subscribers.clear()
