"""Dalek real-time audio DSP chain.

Re-implements the proven vectorized filter chain from davros/phone.py:
- Band-limiting biquad cascade (120 Hz highpass, 5 kHz lowpass, 2.8 kHz presence EQ)
- 30 Hz carrier ring modulation
- tanh distortion overdrive
- 6 kHz smoothing lowpass

Vectorized with NumPy for real-time sub-millisecond execution per 20 ms frame.
"""

import math
import numpy as np
from teleop import config


def biquad(x: np.ndarray, sr: int, kind: str, freq: float,
           q: float = 0.707, gain_db: float = 0.0) -> np.ndarray:
    """RBJ cookbook biquad filter (highpass/lowpass/peaking), numpy-only."""
    w0 = 2.0 * math.pi * freq / sr
    alpha = math.sin(w0) / (2.0 * q)
    cos_w0 = math.cos(w0)
    if kind == "highpass":
        b0 = (1.0 + cos_w0) / 2.0
        b1 = -(1.0 + cos_w0)
        b2 = (1.0 + cos_w0) / 2.0
        a0, a1, a2 = 1.0 + alpha, -2.0 * cos_w0, 1.0 - alpha
    elif kind == "lowpass":
        b0 = (1.0 - cos_w0) / 2.0
        b1 = 1.0 - cos_w0
        b2 = (1.0 - cos_w0) / 2.0
        a0, a1, a2 = 1.0 + alpha, -2.0 * cos_w0, 1.0 - alpha
    elif kind == "peaking":
        big_a = 10.0 ** (gain_db / 40.0)
        b0 = 1.0 + alpha * big_a
        b1 = -2.0 * cos_w0
        b2 = 1.0 - alpha * big_a
        a0, a1, a2 = 1.0 + alpha / big_a, -2.0 * cos_w0, 1.0 - alpha / big_a
    else:
        raise ValueError(f"Unknown filter kind: {kind}")

    b0, b1, b2, a1, a2 = b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0

    y = np.empty_like(x)
    x1 = x2 = y1 = y2 = 0.0
    for i in range(len(x)):
        xi = x[i]
        yi = b0 * xi + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1 = x1, xi
        y2, y1 = y1, yi
        y[i] = yi
    return y


def _biquad_cascade_fir(stages: list, ntaps: int = 256, sr: int = config.SAMPLE_RATE) -> np.ndarray:
    """Collapse a cascade of RBJ biquads into one FIR kernel by capturing its impulse response."""
    kernel = np.zeros(ntaps, dtype=np.float32)
    kernel[0] = 1.0
    for kind, freq, q, gain_db in stages:
        kernel = biquad(kernel, sr, kind, freq, q, gain_db)
    return kernel


class StreamingFIR:
    """Causal frame-by-frame convolution, carrying input history across frames."""

    def __init__(self, kernel: np.ndarray) -> None:
        self.kernel = kernel
        self.history = np.zeros(len(kernel) - 1, dtype=np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate([self.history, x])
        self.history = buf[-(len(self.kernel) - 1):]
        return np.convolve(buf, self.kernel, mode="valid")

    def reset(self) -> None:
        self.history.fill(0.0)


class DalekStream:
    """Streaming Dalek chain: band-limiting EQ, ring modulator, tanh drive,

    then a final lowpass so the distortion harmonics stay tinny rather than
    harsh-digital - fed 20 ms frames.
    """

    def __init__(
        self,
        mod_freq: float = config.MOD_FREQ,
        mix: float = config.MIX,
        drive: float = config.DRIVE,
        gain: float = config.GAIN,
        presence: float = config.PRESENCE,
        sample_rate: int = config.SAMPLE_RATE,
    ) -> None:
        self.mod_freq = mod_freq
        self.mix = mix
        self.drive = drive
        self.gain = gain
        self.sample_rate = sample_rate
        self.phase = 0.0  # carrier position in samples

        # Pre-filter: band-limit & presence boost
        pre = [
            ("highpass", 120.0, 0.707, 0.0),
            ("lowpass", 5000.0, 0.707, 0.0),
        ]
        if presence > 0:
            pre.append(("peaking", 2800.0, 1.0, presence))
        self.pre = StreamingFIR(_biquad_cascade_fir(pre, ntaps=256, sr=sample_rate))
        self.post = StreamingFIR(
            _biquad_cascade_fir([("lowpass", 6000.0, 0.707, 0.0)], ntaps=256, sr=sample_rate)
        )

    def process(self, audio: np.ndarray) -> np.ndarray:
        """One frame in, one frame out; float32 in [-1, 1] both ways."""
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        audio = self.pre.process(audio)
        n = len(audio)
        t = (self.phase + np.arange(n)) / self.sample_rate
        carrier = np.sin(2.0 * math.pi * self.mod_freq * t, dtype=np.float32)
        self.phase = (self.phase + n) % (self.sample_rate / self.mod_freq)

        # Ring modulation
        audio = self.mix * audio * carrier + (1.0 - self.mix) * audio

        # Tanh distortion overdrive
        tanh_drive = math.tanh(self.drive) if self.drive != 0 else 1.0
        audio = np.tanh(self.drive * self.gain * audio) / tanh_drive

        # Post-filter smoothing & headroom
        return (self.post.process(audio) * 0.9).astype(np.float32)

    def reset(self) -> None:
        self.phase = 0.0
        self.pre.reset()
        self.post.reset()
