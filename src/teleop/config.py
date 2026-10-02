"""Configuration parameters for the Dalek WebRTC Teleoperation Service."""

import os
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = BASE_DIR / "static"
CERTS_DIR = BASE_DIR / "certs"
CERT_FILE = Path(os.environ.get("DALEK_SSL_CERT", CERTS_DIR / "cert.pem"))
KEY_FILE = Path(os.environ.get("DALEK_SSL_KEY", CERTS_DIR / "key.pem"))

# Explicitly ensure XDG_RUNTIME_DIR is set for PulseAudio/PipeWire user socket access
if "XDG_RUNTIME_DIR" not in os.environ and hasattr(os, "getuid"):
    os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"

# Network & Server
HOST = os.environ.get("DALEK_HOST", "0.0.0.0")
PORT = int(os.environ.get("DALEK_PORT", "8443"))
USE_SSL = os.environ.get("DALEK_USE_SSL", "true").lower() in ("true", "1", "yes")

# Security / Authentication
PASSWORD = os.environ.get("DALEK_PASSWORD", "communicate")
SESSION_SECRET = os.environ.get("DALEK_SESSION_SECRET", "dalek-teleop-secret-salt-key-2026")
COOKIE_NAME = "dalek_session"
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 30

# Audio / DSP
SAMPLE_RATE = 48000
FRAME_MS = 20
FRAME_SAMPLES = int(SAMPLE_RATE * (FRAME_MS / 1000.0))  # 960 samples per frame

# Audio Modulation Bypass (disabled by default so audio is clean passthrough; davros handles its own modulation)
ENABLE_MODULATION = os.environ.get("DALEK_ENABLE_MODULATION", "false").lower() in ("true", "1", "yes")

# Effect Defaults (matched to davros phone.py / dalek.py)
MOD_FREQ = float(os.environ.get("DALEK_MOD_FREQ", "30.0"))     # Ring modulator carrier in Hz
MIX = float(os.environ.get("DALEK_MOD_MIX", "1.0"))            # 1.0 = fully wet
DRIVE = float(os.environ.get("DALEK_MOD_DRIVE", "5.0"))        # Overdrive distortion
GAIN = float(os.environ.get("DALEK_MOD_GAIN", "1.0"))          # Pre-gain
PRESENCE = float(os.environ.get("DALEK_MOD_PRESENCE", "8.0"))  # Peaking EQ boost at 2.8kHz in dB

# WebRTC STUN/TURN
DEFAULT_ICE_SERVERS = [
    {"urls": "stun:stun.l.google.com:19302"},
    {"urls": "stun:stun1.l.google.com:19302"},
]
