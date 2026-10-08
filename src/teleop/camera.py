"""Camera capture manager for Dalek Teleoperation.

Continuously captures frames from the video camera (OpenCV / V4L2) in a background
thread so the asyncio event loop is never blocked and the hardware buffer never lags.
Provides a graceful test/standby frame if no camera hardware is connected.
"""

import logging
import os
import threading
import time
import cv2
import numpy as np

logger = logging.getLogger("teleop.camera")


class CameraManager:
    """Manages a single shared camera stream for WebRTC video tracks."""

    def __init__(self, device_index: int = None, width: int = 640, height: int = 480, fps: int = 30):
        if device_index is None:
            env_val = os.environ.get("DALEK_CAMERA_INDEX", "auto")
            if env_val.lower() == "auto":
                self.device_index = "auto"
            else:
                try:
                    self.device_index = int(env_val)
                except ValueError:
                    self.device_index = "auto"
        else:
            self.device_index = device_index
        self.width = width
        self.height = height
        self.fps = fps
        self.flip_180 = os.environ.get("DALEK_CAMERA_FLIP_180", "true").lower() in ("true", "1", "yes")

        self.cap: cv2.VideoCapture | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest_frame = self._create_standby_frame("DALEK OPTICAL SENSOR", "INITIALIZING...")

    def _create_standby_frame(self, title: str, subtitle: str) -> np.ndarray:
        """Create a clean standby test pattern frame when no camera is active."""
        img = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        # Subtle grid background
        img[::40, :, :] = (18, 20, 24)
        img[:, ::40, :] = (18, 20, 24)

        # Crosshairs
        cx, cy = self.width // 2, self.height // 2
        cv2.drawMarker(img, (cx, cy), (40, 45, 55), markerType=cv2.MARKER_CROSS, markerSize=30, thickness=1)

        # Text
        cv2.putText(img, title, (cx - 160, cy - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 210, 255), 2)
        cv2.putText(img, subtitle, (cx - 120, cy + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (139, 146, 158), 1)
        return img

    def start(self):
        if self._running:
            return
        if self.device_index == -1:
            logger.info("Camera disabled via DALEK_CAMERA_INDEX=-1. Using test pattern.")
            self._latest_frame = self._create_standby_frame("DALEK OPTICAL SENSOR", "CAMERA DISABLED")
            return
        self._running = True
        self._thread = threading.Thread(target=self._capture_worker, name="camera-capture-thread", daemon=True)
        self._thread.start()
        logger.info("CameraManager started background capture (device: %s).", self.device_index)

    def _probe_camera(self) -> tuple[cv2.VideoCapture | None, int | None]:
        """Try opening specified or auto-discovered camera index."""
        candidates = []
        if self.device_index == "auto":
            # On Pi with dalek-video-splitter, video3 is DalekTeleop loopback, falling back to 0, 1, 2, 4
            candidates = [3, 0, 1, 2, 4]
        else:
            candidates = [self.device_index]

        for idx in candidates:
            try:
                cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
                if not cap.isOpened():
                    cap = cv2.VideoCapture(idx)
                if cap.isOpened():
                    ret, test_frame = cap.read()
                    if ret and test_frame is not None:
                        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                        cap.set(cv2.CAP_PROP_FPS, self.fps)
                        return cap, idx
                    cap.release()
            except Exception:
                pass
        return None, None

    def _capture_worker(self):
        """Dedicated thread continuously pulling latest frames from camera."""
        consecutive_failures = 0
        while self._running:
            try:
                if self.cap is None or not self.cap.isOpened():
                    cap, bound_idx = self._probe_camera()
                    if cap is None:
                        consecutive_failures += 1
                        if consecutive_failures == 1:
                            logger.warning("No working video capture device found. Using standby test pattern.")
                        with self._lock:
                            self._latest_frame = self._create_standby_frame("DALEK OPTICAL SENSOR", "STANDBY / NO CAMERA")
                        time.sleep(5.0)  # Back off retry interval
                        continue

                    self.cap = cap
                    consecutive_failures = 0
                    logger.info("Camera device %s opened successfully (%dx%d @ %d FPS).", bound_idx, self.width, self.height, self.fps)

                ret, frame = self.cap.read()
                if ret and frame is not None:
                    if self.flip_180:
                        frame = cv2.rotate(frame, cv2.ROTATE_180)
                    if frame.shape[1] != self.width or frame.shape[0] != self.height:
                        frame = cv2.resize(frame, (self.width, self.height))
                    with self._lock:
                        self._latest_frame = frame
                else:
                    time.sleep(0.01)

            except Exception as e:
                logger.error("Camera capture error: %s", e)
                with self._lock:
                    self._latest_frame = self._create_standby_frame("DALEK OPTICAL SENSOR", "CAMERA READ ERROR")
                time.sleep(2.0)

    def get_frame(self) -> np.ndarray:
        """Get copy of the most recently captured BGR frame."""
        with self._lock:
            return self._latest_frame.copy()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        logger.info("CameraManager stopped.")
