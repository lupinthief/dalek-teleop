"""Test camera manager and video frame generation."""

import numpy as np
from teleop.camera import CameraManager


def test_camera_manager_frame_generation():
    camera = CameraManager(device_index=999)  # Non-existent camera index to test graceful fallback
    camera.start()
    frame = camera.get_frame()
    assert isinstance(frame, np.ndarray)
    assert frame.shape == (480, 640, 3)
    assert frame.dtype == np.uint8
    camera.stop()
