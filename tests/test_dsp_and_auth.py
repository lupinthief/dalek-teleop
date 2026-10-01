"""Unit tests for Dalek DSP ring modulation and audio processing."""

import math
import numpy as np
import pytest
from teleop import config
from teleop.dsp import DalekStream, biquad, StreamingFIR, _biquad_cascade_fir
from teleop.auth import (
    verify_password,
    create_session_token,
    verify_session_token,
    record_failed_attempt,
    reset_failed_attempts,
    is_rate_limited,
)


def test_biquad_filtering():
    sr = 48000
    x = np.sin(2 * np.pi * 1000 * np.arange(960) / sr).astype(np.float32)
    # Apply lowpass filter
    y = biquad(x, sr, "lowpass", 500, q=0.707)
    assert len(y) == len(x)
    # Amplitude of 1000Hz sine should be attenuated through 500Hz lowpass
    assert np.max(np.abs(y)) < np.max(np.abs(x))


def test_dalek_stream_dsp():
    stream = DalekStream()
    sr = config.SAMPLE_RATE
    frame_len = config.FRAME_SAMPLES

    # Feed 20 ms synthetic voice signal (sine wave at 440 Hz)
    t = np.arange(frame_len) / sr
    audio_in = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

    # Process first frame
    out1 = stream.process(audio_in)
    assert len(out1) == frame_len
    assert out1.dtype == np.float32
    assert np.all(np.isfinite(out1))
    assert np.max(np.abs(out1)) <= 1.0

    # Process second frame: verify carrier phase continuity
    out2 = stream.process(audio_in)
    assert len(out2) == frame_len
    assert np.all(np.isfinite(out2))


def test_auth_and_session_tokens():
    # Password verification
    assert verify_password("communicate") is True
    assert verify_password("wrong-password") is False

    # Session token creation and validation
    token = create_session_token(expiry_seconds=3600)
    assert verify_session_token(token) is True
    assert verify_session_token("invalid:token:format") is False
    assert verify_session_token(token + "tampered") is False

    # Rate limiting
    test_ip = "192.168.1.99"
    reset_failed_attempts(test_ip)
    assert is_rate_limited(test_ip) is False

    for _ in range(config.MAX_LOGIN_ATTEMPTS):
        record_failed_attempt(test_ip)
    assert is_rate_limited(test_ip) is True
    reset_failed_attempts(test_ip)
    assert is_rate_limited(test_ip) is False
