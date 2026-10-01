"""Authentication and session management for Dalek Teleoperation."""

import hashlib
import hmac
import time
from typing import Dict, Tuple

from teleop import config

# In-memory IP failed attempt tracking: ip -> (failed_count, lockout_until)
_FAILED_ATTEMPTS: Dict[str, Tuple[int, float]] = {}


def is_rate_limited(ip: str) -> bool:
    now = time.time()
    if ip in _FAILED_ATTEMPTS:
        count, lockout = _FAILED_ATTEMPTS[ip]
        if now < lockout:
            return True
        if now >= lockout and count >= config.MAX_LOGIN_ATTEMPTS:
            # Lockout expired, reset
            del _FAILED_ATTEMPTS[ip]
    return False


def record_failed_attempt(ip: str) -> int:
    now = time.time()
    count, _ = _FAILED_ATTEMPTS.get(ip, (0, 0.0))
    count += 1
    lockout = 0.0
    if count >= config.MAX_LOGIN_ATTEMPTS:
        lockout = now + config.LOCKOUT_SECONDS
    _FAILED_ATTEMPTS[ip] = (count, lockout)
    return count


def reset_failed_attempts(ip: str) -> None:
    _FAILED_ATTEMPTS.pop(ip, None)


def verify_password(provided_password: str) -> bool:
    """Constant-time comparison against configured password."""
    if not isinstance(provided_password, str):
        return False
    return hmac.compare_digest(
        provided_password.encode("utf-8"),
        config.PASSWORD.encode("utf-8")
    )


def create_session_token(expiry_seconds: int = 86400) -> str:
    """Create a tamper-evident timestamped session token."""
    expires_at = int(time.time()) + expiry_seconds
    payload = f"operator:{expires_at}"
    signature = hmac.new(
        config.SESSION_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return f"{payload}:{signature}"


def verify_session_token(token: str) -> bool:
    """Verify validity and signature of session token."""
    if not token or not isinstance(token, str):
        return False
    parts = token.split(":")
    if len(parts) != 3:
        return False
    role, expires_at_str, signature = parts
    try:
        expires_at = int(expires_at_str)
    except ValueError:
        return False

    if time.time() > expires_at:
        return False

    payload = f"{role}:{expires_at_str}"
    expected_sig = hmac.new(
        config.SESSION_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(signature, expected_sig)
