"""HTTP, WebSocket signaling, and static asset server for Dalek Teleoperation."""

import asyncio
import json
import logging
import re
import ssl
import sys
from typing import Optional
from aiohttp import web, WSMsgType
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer

from teleop import config
from teleop.auth import (
    verify_password,
    verify_session_token,
    create_session_token,
    is_rate_limited,
    record_failed_attempt,
    reset_failed_attempts,
)
from teleop.audio_io import AudioPlayer, AudioCapture
from teleop.camera import CameraManager
from teleop.rtc import PeerSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("teleop.server")

# Global resources
player = AudioPlayer()
capture = AudioCapture()
camera = CameraManager()
active_sessions: set[PeerSession] = set()

# Control datagram socket for forwarding virtual joystick commands to dalek.py
cmd_sock = None
try:
    import socket
    cmd_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
except Exception as e:
    logger.warning("Could not initialize command socket: %s", e)

def forward_joystick_cmd(rx: float, ry: float):
    global cmd_sock
    # Dynamically duck the Dalek room mic sensitivity when head/eye motors are active
    if abs(rx) > 0.05 or abs(ry) > 0.05:
        # Heavily attenuate or mute mic during head motor movement
        capture.set_duck_factor(0.0)
    else:
        capture.set_duck_factor(1.0)

    if not cmd_sock:
        return
    import os
    if not os.path.exists(config.TELEOP_CMD_SOCKET_PATH):
        return
    try:
        payload = json.dumps({"rx": float(rx), "ry": float(ry)}).encode("utf-8")
        cmd_sock.sendto(payload, config.TELEOP_CMD_SOCKET_PATH)
    except Exception as e:
        logger.debug("Failed sending joystick cmd to %s: %s", config.TELEOP_CMD_SOCKET_PATH, e)


# ------------------------------------------------------------------ System Volume Helpers
def get_pi_volume() -> dict:
    """Retrieve master speaker volume and mute status via pactl."""
    try:
        import subprocess
        out = subprocess.check_output(["pactl", "get-sink-volume", "@DEFAULT_SINK@"], text=True)
        m = re.search(r"(\d+)%", out)
        vol = int(m.group(1)) if m else 100
        mute_out = subprocess.check_output(["pactl", "get-sink-mute", "@DEFAULT_SINK@"], text=True)
        is_muted = "yes" in mute_out.lower()
        return {"volume": vol, "muted": is_muted}
    except Exception as e:
        logger.debug("Failed reading system volume: %s", e)
        return {"volume": 100, "muted": False}


def set_pi_volume(volume: Optional[int] = None, mute: Optional[bool] = None) -> dict:
    """Set master speaker volume and/or mute status via pactl."""
    try:
        import subprocess
        if volume is not None:
            v = max(0, min(100, int(volume)))
            subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{v}%"], check=True)
        if mute is not None:
            m = "1" if mute else "0"
            subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", m], check=True)
    except Exception as e:
        logger.warning("Failed setting system volume: %s", e)
    return get_pi_volume()


# ------------------------------------------------------------------ Auth Middleware
@web.middleware
async def auth_middleware(request: web.Request, handler):
    path = request.path

    # Public paths
    if path in ("/api/login", "/api/status") or path.startswith("/static/"):
        return await handler(request)

    # Allow manifest and favicon
    if path in ("/manifest.json", "/favicon.ico"):
        return await handler(request)

    # Check session cookie or Authorization header
    token = request.cookies.get(config.COOKIE_NAME)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()

    is_authenticated = verify_session_token(token) if token else False

    # Web page requests: if not authenticated, redirect or allow login form
    if path == "/" or path == "/index.html":
        # Pass authentication status to handler / template
        request["is_authenticated"] = is_authenticated
        return await handler(request)

    # API and WebSocket routes require valid auth
    if not is_authenticated:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return web.Response(status=401, text="Unauthorized WebSocket connection")
        return web.json_response({"error": "Unauthorized", "authenticated": False}, status=401)

    return await handler(request)


# ------------------------------------------------------------------ Route Handlers
async def handle_index(request: web.Request):
    return web.FileResponse(config.STATIC_DIR / "index.html")


async def handle_login(request: web.Request):
    ip = request.remote or "unknown"
    if is_rate_limited(ip):
        return web.json_response(
            {"error": "Too many failed attempts. Try again in 30 seconds."},
            status=429
        )

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    password = data.get("password", "")
    if verify_password(password):
        reset_failed_attempts(ip)
        token = create_session_token()
        response = web.json_response({"success": True, "token": token})
        response.set_cookie(
            config.COOKIE_NAME,
            token,
            max_age=86400,
            httponly=True,
            secure=config.USE_SSL,
            samesite="Lax",
        )
        logger.info("Successful operator login from IP %s", ip)
        return response
    else:
        attempts = record_failed_attempt(ip)
        logger.warning("Failed login attempt (%d) from IP %s", attempts, ip)
        return web.json_response(
            {"error": "Invalid password", "attempts": attempts},
            status=401
        )


async def handle_logout(request: web.Request):
    response = web.json_response({"success": True})
    response.del_cookie(config.COOKIE_NAME)
    return response


async def handle_status(request: web.Request):
    token = request.cookies.get(config.COOKIE_NAME)
    is_auth = verify_session_token(token) if token else False
    vol_info = get_pi_volume()
    return web.json_response({
        "status": "online",
        "service": "dalek-teleop",
        "authenticated": is_auth,
        "active_calls": len(active_sessions),
        "volume": vol_info.get("volume", 100),
        "muted": vol_info.get("muted", False),
    })


async def handle_volume(request: web.Request):
    token = request.cookies.get(config.COOKIE_NAME)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    if not (token and verify_session_token(token)):
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    volume = data.get("volume")
    muted = data.get("muted")
    res = set_pi_volume(volume=volume, mute=muted)
    return web.json_response(res)


async def handle_signaling_ws(request: web.Request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    logger.info("Signaling WebSocket connected from %s", request.remote)

    ice_servers = [RTCIceServer(urls=s["urls"]) for s in config.DEFAULT_ICE_SERVERS]
    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))
    session = PeerSession(pc, player, capture, camera)
    active_sessions.add(session)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                action = data.get("action")

                if action == "offer":
                    offer = RTCSessionDescription(sdp=data["sdp"], type=data["type"])
                    await pc.setRemoteDescription(offer)
                    answer = await pc.createAnswer()
                    await pc.setLocalDescription(answer)
                    await ws.send_json({
                        "action": "answer",
                        "sdp": pc.localDescription.sdp,
                        "type": pc.localDescription.type,
                    })

                elif action == "candidate":
                    candidate_info = data.get("candidate")
                    if candidate_info:
                        # Optional trickle ICE candidate support
                        pass

                elif action == "joystick":
                    rx = data.get("rx", 0.0)
                    ry = data.get("ry", 0.0)
                    forward_joystick_cmd(rx, ry)

                elif action == "volume":
                    volume = data.get("volume")
                    muted = data.get("muted")
                    res = set_pi_volume(volume=volume, mute=muted)
                    await ws.send_json({
                        "action": "volume_update",
                        "volume": res.get("volume", 100),
                        "muted": res.get("muted", False),
                    })

                elif action == "hangup":
                    await session.close()
                    break

            elif msg.type == WSMsgType.ERROR:
                logger.error("Signaling WS error: %s", ws.exception())
    finally:
        active_sessions.discard(session)
        await session.close()
        logger.info("Signaling WebSocket disconnected from %s", request.remote)

    return ws


# ------------------------------------------------------------------ App Setup & Lifecycle
async def on_startup(app: web.Application):
    logger.info("Initializing audio and camera hardware I/O...")
    loop = asyncio.get_event_loop()
    player.start()
    capture.start(loop)
    camera.start()


async def on_cleanup(app: web.Application):
    logger.info("Cleaning up active teleop sessions, camera, and audio devices...")
    for session in list(active_sessions):
        await session.close()
    active_sessions.clear()
    player.stop()
    capture.stop()
    camera.stop()


def create_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/", handle_index)
    app.router.add_post("/api/login", handle_login)
    app.router.add_post("/api/logout", handle_logout)
    app.router.add_get("/api/status", handle_status)
    app.router.add_post("/api/volume", handle_volume)
    app.router.add_get("/ws", handle_signaling_ws)
    app.router.add_static("/static/", path=config.STATIC_DIR, name="static")

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    app = create_app()

    ssl_context = None
    if config.USE_SSL:
        if not config.CERT_FILE.exists() or not config.KEY_FILE.exists():
            from generate_certs import generate_certificates
            generate_certificates(config.CERT_FILE, config.KEY_FILE)

        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(str(config.CERT_FILE), str(config.KEY_FILE))
        proto = "https"
    else:
        proto = "http"

    logger.info("=" * 60)
    logger.info("DALEK TELEOP SERVICE STARTING")
    logger.info("Server bound to %s://%s:%d", proto, config.HOST, config.PORT)
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        logger.info(">>> OPEN ON PHONE: %s://%s:%d <<<", proto, local_ip, config.PORT)
        logger.info("(IMPORTANT: Must include %s:// in your mobile browser address)", proto)
        logger.info("(Tap 'Advanced' -> 'Proceed' when self-signed certificate warning appears)")
    except Exception:
        logger.info("Open on smartphone browser: %s://<your-ip>:%d", proto, config.PORT)
    logger.info("=" * 60)

    web.run_app(
        app,
        host=config.HOST,
        port=config.PORT,
        ssl_context=ssl_context,
        access_log=None,
    )


if __name__ == "__main__":
    main()
