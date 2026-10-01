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
            self.device_index = int(os.environ.get("DALEK_CAMERA_INDEX", "0"))
        else:
            self.device_index = device_index
        self.width = width
        self.height = height
        self.fps = fps

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
        self._running = True
        self._thread = threading.Thread(target=self._capture_worker, name="camera-capture-thread", daemon=True)
        self._thread.start()
        logger.info("CameraManager started background capture on device index %d.", self.device_index)

    def _capture_worker(self):
        """Dedicated thread continuously pulling latest frames from camera."""
        while self._running:
            try:
                if self.cap is None or not self.cap.isOpened():
                    logger.info("Opening video capture device %s...", self.device_index)
                    cap = cv2.VideoCapture(self.device_index)
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                    cap.set(cv2.CAP_PROP_FPS, self.fps)

                    if not cap.isOpened():
                        logger.warning("Camera device %s could not be opened. Using standby test pattern.", self.device_index)
                        with self._lock:
                            self._latest_frame = self._create_standby_frame("DALEK OPTICAL SENSOR", "STANDBY / NO CAMERA")
                        time.sleep(2.0)
                        continue
                    self.cap = cap
                    logger.info("Camera device %s opened successfully (%dx%d @ %d FPS).", self.device_index, self.width, self.height, self.fps)

                ret, frame = self.cap.read()
                if ret and frame is not None:
                    # Ensure correct dimensions if camera returned different size
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
                time.sleep(1.0)

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
