# Dalek WebRTC Teleoperation Service (`dalek-teleop`)

A standalone WebRTC audio and teleoperation service for the Dalek robot.

## Features
- **Two-way Audio**: Stream voice from your mobile phone browser to the Dalek with real-time Dalek ring modulation (30 Hz carrier, biquad EQ, and tanh overdrive).
- **Return Audio**: Stream Pi microphone (webcam/lavalier) back to your phone.
- **Ear Dome Light Sync**: Output feeds standard system audio sinks (PulseAudio/PipeWire/ALSA) which triggers the Dalek's ear peak detector in `davros`.
- **Password Protected**: Mobile web UI and WebSocket signaling require authentication.
- **HTTPS / WebRTC Ready**: Built-in certificate generation for seamless mobile browser media permissions.

## Quick Start on Laptop or Raspberry Pi

### 1. Install dependencies
Using `uv`:
```bash
cd dalek-teleop
~/.local/bin/uv venv
source .venv/bin/activate
uv pip install -e .
```

### 2. Generate SSL certificates
Mobile browsers require HTTPS to grant microphone permissions:
```bash
python generate_certs.py
```

### 3. Start the service
Set your desired operator password (defaults to `exterminate`):
```bash
export DALEK_PASSWORD="your-secure-password"
python -m teleop.server
```

Open `https://<ip-address>:8443` on your smartphone browser.
