"""WebRTC PeerConnection and MediaTrack implementations using aiortc."""

import asyncio
import fractions
import logging
import time
from typing import Optional, Set
import av
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack, VideoStreamTrack
from aiortc.rtcrtpreceiver import RemoteStreamTrack

from teleop import config
from teleop.dsp import DalekStream
from teleop.audio_io import AudioPlayer, AudioCapture
from teleop.camera import CameraManager

logger = logging.getLogger("teleop.rtc")


class DalekMicrophoneTrack(MediaStreamTrack):
    """AudioStreamTrack that streams the Dalek's physical microphone back to the phone."""

    kind = "audio"

    def __init__(self, capture: AudioCapture):
        super().__init__()
        self.capture = capture
        self.queue = capture.subscribe()
        self._timestamp = 0
        self._time_base = fractions.Fraction(1, config.SAMPLE_RATE)
        self._start_time = None

    async def recv(self):
        if self.readyState != "live":
            raise asyncio.CancelledError

        # Read next 20 ms frame from microphone capture queue
        try:
            pcm_float = await asyncio.wait_for(self.queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            # Fallback to silence if microphone is quiet/stalled
            pcm_float = np.zeros(config.FRAME_SAMPLES, dtype=np.float32)

        # Convert float32 [-1.0, 1.0] to int16 for Opus packing
        pcm_int16 = (np.clip(pcm_float, -1.0, 1.0) * 32767.0).astype(np.int16)
        
        # Package into PyAV AudioFrame
        frame = av.AudioFrame(format="s16", layout="mono", samples=len(pcm_int16))
        frame.planes[0].update(pcm_int16.tobytes())
        frame.sample_rate = config.SAMPLE_RATE
        frame.pts = self._timestamp
        frame.time_base = self._time_base
        self._timestamp += len(pcm_int16)

        return frame

    def stop(self):
        super().stop()
        self.capture.unsubscribe(self.queue)


class DalekCameraTrack(VideoStreamTrack):
    """VideoStreamTrack that streams the Dalek's physical camera back to the phone."""

    kind = "video"

    def __init__(self, camera: CameraManager):
        super().__init__()
        self.camera = camera

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        frame = self.camera.get_frame()
        video_frame = av.VideoFrame.from_ndarray(frame, format="bgr24")
        video_frame.pts = pts
        video_frame.time_base = time_base
        return video_frame


class PeerSession:
    """Manages an active WebRTC PeerConnection session with an operator phone."""

    def __init__(self, pc: RTCPeerConnection, player: AudioPlayer, capture: AudioCapture, camera: CameraManager):
        self.pc = pc
        self.player = player
        self.capture = capture
        self.camera = camera
        self.dsp = DalekStream()
        self.mic_track: Optional[DalekMicrophoneTrack] = None
        self.camera_track: Optional[DalekCameraTrack] = None
        self.connected_at = time.time()
        self._audio_pump_task: Optional[asyncio.Task] = None

        @pc.on("connectionstatechange")
        async def on_state_change():
            logger.info("WebRTC connection state: %s", pc.connectionState)
            if pc.connectionState == "failed":
                await self.close()

        @pc.on("track")
        def on_track(track):
            logger.info("Received incoming WebRTC track: %s (kind=%s)", track.id, track.kind)
            if track.kind == "audio":
                self._audio_pump_task = asyncio.create_task(self._process_incoming_audio(track))

        # Add Dalek camera track first, matching browser transceiver order (video m-line 0)
        self.camera_track = DalekCameraTrack(camera)
        self.pc.addTrack(self.camera_track)

        # Add Dalek mic track second, matching browser transceiver order (audio m-line 1)
        self.mic_track = DalekMicrophoneTrack(capture)
        self.pc.addTrack(self.mic_track)

    async def _process_incoming_audio(self, track: RemoteStreamTrack):
        """Continuously decode incoming phone voice, normalize audio, and stream to speaker."""
        logger.info("Starting incoming phone audio processing pipeline...")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=config.SAMPLE_RATE)
        try:
            while True:
                frame = await track.recv()
                # Resample and normalize to 48kHz mono signed 16-bit PCM
                for resampled_frame in resampler.resample(frame):
                    pcm_int16 = resampled_frame.to_ndarray().flatten()
                    if config.ENABLE_MODULATION:
                        audio_float = pcm_int16.astype(np.float32) / 32768.0
                        modulated = self.dsp.process(audio_float)
                        self.player.write(modulated)
                    else:
                        self.player.write(pcm_int16)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.info("Incoming audio track ended or error: %s", e)

    async def close(self):
        if self._audio_pump_task and not self._audio_pump_task.done():
            self._audio_pump_task.cancel()
        if self.mic_track:
            self.mic_track.stop()
        if self.camera_track:
            self.camera_track.stop()
        await self.pc.close()
        logger.info("PeerSession closed.")
