import hmac
import hashlib
import time
import threading
import re
import os
from typing import Dict, Tuple, Optional


PIN_RE = re.compile(r"^[0-9]{4,12}$")


def hash_pin(pin: str, salt: bytes | None = None, iterations: int = 200_000) -> dict:
    if salt is None:
        salt = os.urandom(16)
    pin_hash = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations)
    return {
        "pin_hash": pin_hash.hex(),
        "salt": salt.hex(),
        "iterations": iterations,
    }


def verify_pin_hash(pin: str, rec: dict) -> bool:
    try:
        salt = bytes.fromhex(rec["salt"])
        iterations = rec.get("iterations", 200_000)
        expected_hash = bytes.fromhex(rec["pin_hash"])
    except (KeyError, ValueError):
        return False
    computed = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations)
    return hmac.compare_digest(computed, expected_hash)


def make_token(secret: str, ttl_s: int = 30 * 86400) -> str:
    expiry = int(time.time()) + ttl_s
    msg = str(expiry).encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def verify_token(secret: str, token: str | None) -> bool:
    if not token:
        return False
    try:
        expiry_str, sig = token.split(".", 1)
        expiry = int(expiry_str)
    except (ValueError, AttributeError):
        return False

    if expiry < int(time.time()):
        return False

    expected_sig = hmac.new(secret.encode(), str(expiry).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected_sig)


def check_pin(cfg_pin: str, pin: str) -> bool:
    return hmac.compare_digest(cfg_pin, pin)


def derive_token_secret(base_secret: str, pin_component: str) -> str:
    """Derive token HMAC key from base secret and PIN component (hash or env PIN)."""
    return f"{base_secret}:{pin_component}"


class LoginLimiter:
    def __init__(self):
        self._lock = threading.Lock()
        # ip -> (failure_count, first_failure_time, locked_until)
        self._records: Dict[str, Tuple[int, float, float]] = {}

    def _prune(self, now: float) -> None:
        # Remove entries where lock expired and no recent failures
        to_delete = []
        for ip, (count, first_fail, locked_until) in self._records.items():
            if locked_until <= now and (now - first_fail) > 300:  # 5 min window
                to_delete.append(ip)
        for ip in to_delete:
            del self._records[ip]

    def allowed(self, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            record = self._records.get(ip)
            if not record:
                return True
            count, first_fail, locked_until = record
            if locked_until > now:
                return False
            return count < 5

    def failure(self, ip: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            record = self._records.get(ip)
            if not record:
                self._records[ip] = (1, now, 0)
            else:
                count, first_fail, locked_until = record
                new_count = count + 1
                if new_count >= 5:
                    locked_until = now + 300  # 5 min lock
                self._records[ip] = (new_count, first_fail, locked_until)

    def success(self, ip: str) -> None:
        with self._lock:
            if ip in self._records:
                del self._records[ip]
