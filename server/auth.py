"""Auth primitives: passwords, TOTP, recovery codes, tokens, pairing codes, rate limiting, setup token."""
import base64
import binascii
import hashlib
import hmac
import math
import os
import re
import secrets
import struct
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import quote


# --- legacy PIN helpers (kept for v1 migration) ---------------------------------------------------

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


def check_pin(cfg_pin: str, pin: str) -> bool:
    if not isinstance(cfg_pin, str) or not isinstance(pin, str):
        return False
    return hmac.compare_digest(cfg_pin.encode("utf-8"), pin.encode("utf-8"))


# --- usernames and passwords ----------------------------------------------------------------------

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
PASSWORD_MIN = 10
PASSWORD_MAX = 256

_SCRYPT_N = 32768
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def validate_username(username: object) -> str:
    msg = "username must be 3-32 chars: letters, digits, _ . -"
    if not isinstance(username, str):
        raise ValueError(msg)
    username = username.strip()
    if not USERNAME_RE.match(username):
        raise ValueError(msg)
    return username


def validate_password(password: object, username: str = "") -> None:
    if not isinstance(password, str):
        raise ValueError("password must be a string")
    if len(password) < PASSWORD_MIN:
        raise ValueError(f"password must be at least {PASSWORD_MIN} characters")
    if len(password) > PASSWORD_MAX:
        raise ValueError(f"password must be at most {PASSWORD_MAX} characters")
    if username and password.casefold() == username.casefold():
        raise ValueError("password must not equal the username")
    if len(set(password)) == 1:
        raise ValueError("password must not be a single repeated character")


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]*", text):
        raise ValueError("bad base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                        maxmem=_SCRYPT_MAXMEM, dklen=_SCRYPT_DKLEN)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(dk)}"


def _parse_hash(encoded: str) -> tuple[int, int, int, bytes, bytes]:
    scheme, n, r, p, salt, dk = encoded.split("$")
    if scheme != "scrypt":
        raise ValueError("unknown scheme")
    return int(n), int(r), int(p), _b64d(salt), _b64d(dk)


def verify_password(password: str, encoded: str) -> bool:
    try:
        n, r, p, salt, dk = _parse_hash(encoded)
        if not dk or not salt:
            return False
        computed = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                                  maxmem=_SCRYPT_MAXMEM, dklen=len(dk))
    except (ValueError, TypeError, AttributeError, OverflowError, UnicodeError, binascii.Error):
        return False
    return hmac.compare_digest(computed, dk)


def needs_rehash(encoded: str) -> bool:
    try:
        n, r, p, _salt, dk = _parse_hash(encoded)
    except (ValueError, TypeError, AttributeError, binascii.Error):
        return True
    return (n, r, p, len(dk)) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_DKLEN)


DUMMY_HASH: str = hash_password(secrets.token_urlsafe(24))


# --- TOTP (RFC 6238 / 4226, SHA-1) ----------------------------------------------------------------

def new_totp_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")


def totp_code(secret_b32: str, step: int, digits: int = 6) -> str:
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def verify_totp(secret_b32: str, code: str, last_step: int, now: Optional[float] = None,
                window: int = 1) -> Optional[int]:
    if not isinstance(code, str) or len(code) != 6 or not (code.isascii() and code.isdigit()):
        return None
    current = int((time.time() if now is None else now) // 30)
    match: Optional[int] = None
    try:
        for step in range(current - window, current + window + 1):
            if hmac.compare_digest(totp_code(secret_b32, step), code) and step > last_step and match is None:
                match = step
    except (ValueError, binascii.Error):
        return None
    return match


def otpauth_uri(secret_b32: str, username: str, issuer: str = "SecCam") -> str:
    label = quote(f"{issuer}:{username}", safe=":")
    return (f"otpauth://totp/{label}?secret={quote(secret_b32, safe='')}&issuer={quote(issuer, safe='')}"
            "&algorithm=SHA1&digits=6&period=30")


# --- recovery codes -------------------------------------------------------------------------------

_RECOVERY_ALPHABET = "".join(c for c in "abcdefghijklmnopqrstuvwxyz23456789" if c not in "lo")


def new_recovery_codes(n: int = 8) -> tuple[list[str], list[str]]:
    codes = []
    for _ in range(n):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(8))
        codes.append(f"{raw[:4]}-{raw[4:]}")
    return codes, [hash_token(c) for c in codes]


def use_recovery_code(code: str, hashes: list[str]) -> Optional[list[str]]:
    if not isinstance(code, str):
        return None
    digest = hash_token(code.strip().lower())
    index = None
    for i, h in enumerate(hashes):
        if hmac.compare_digest(h, digest) and index is None:
            index = i
    if index is None:
        return None
    return hashes[:index] + hashes[index + 1:]


# --- session / device tokens ----------------------------------------------------------------------

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def new_device_id() -> str:
    return secrets.token_hex(6)


def new_device_token(device_id: str) -> tuple[str, str]:
    secret = secrets.token_urlsafe(32)
    return f"scd_{device_id}.{secret}", hash_token(secret)


_DEVICE_TOKEN_RE = re.compile(r"^scd_([0-9a-f]{12})\.([A-Za-z0-9_-]{16,128})$")


def parse_device_token(token: Optional[str]) -> Optional[tuple[str, str]]:
    if not isinstance(token, str):
        return None
    token = token.strip()
    if token[:7].lower() == "bearer ":
        token = token[7:].strip()
    m = _DEVICE_TOKEN_RE.match(token)
    return (m.group(1), m.group(2)) if m else None


# --- pairing codes --------------------------------------------------------------------------------

PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_pairing_code() -> str:
    raw = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_pairing_code(code: object) -> Optional[str]:
    if not isinstance(code, str):
        return None
    raw = code.upper().replace(" ", "").replace("-", "")
    if len(raw) != 8 or any(c not in PAIRING_ALPHABET for c in raw):
        return None
    return f"{raw[:4]}-{raw[4:]}"


class PairingCodes:
    def __init__(self, ttl_s: int = 600, max_live: int = 5) -> None:
        self._ttl_s = ttl_s
        self._max_live = max_live
        self._lock = threading.Lock()
        self._codes: Dict[str, Tuple[float, Optional[str]]] = {}

    def _prune(self, now: float) -> None:
        for code in [c for c, (exp, _) in self._codes.items() if exp <= now]:
            del self._codes[code]

    def issue(self, name: Optional[str] = None, now: Optional[float] = None) -> tuple[str, int]:
        now = time.time() if now is None else now
        with self._lock:
            self._prune(now)
            if len(self._codes) >= self._max_live:
                raise ValueError("too many codes")
            code = new_pairing_code()
            while code in self._codes:
                code = new_pairing_code()
            expires = now + self._ttl_s
            self._codes[code] = (expires, name)
            return code, int(expires * 1000)

    def consume(self, code: str, now: Optional[float] = None) -> Optional[dict]:
        norm = normalize_pairing_code(code)
        if norm is None:
            return None
        now = time.time() if now is None else now
        with self._lock:
            self._prune(now)
            entry = self._codes.pop(norm, None)
        return None if entry is None else {"name": entry[1]}


# --- login rate limiting --------------------------------------------------------------------------

def norm_limiter_key(key: str) -> str:
    """Keys longer than 128 chars are replaced by "h:" + sha256 hex (bounded memory)."""
    if len(key) > 128:
        return "h:" + hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()
    return key


class LoginLimiter:
    def __init__(self, max_failures: int = 5, window_s: float = 300, lock_s: float = 300,
                 max_keys: int = 10000) -> None:
        self._max_failures = max_failures
        self._window_s = window_s
        self._lock_s = lock_s
        self._max_keys = max_keys
        self._lock = threading.Lock()
        # key -> [failure_count, first_failure_time, locked_until]; ordered by first failure (oldest first)
        self._records: "OrderedDict[str, list]" = OrderedDict()
        self._totals: "OrderedDict[str, int]" = OrderedDict()

    def _prune(self, now: float) -> None:
        for key in [k for k, (_c, first, until) in self._records.items()
                    if until <= now and now - first > self._window_s]:
            del self._records[key]

    def _locked_until(self, key: str, now: float) -> float:
        rec = self._records.get(key)
        if rec is None:
            return 0.0
        if rec[2] > now:
            return rec[2]
        if rec[2] > 0:  # lock expired: start over
            del self._records[key]
        return 0.0

    def allowed(self, key: str) -> bool:
        key = norm_limiter_key(key)
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            return self._locked_until(key, now) <= now

    def failure(self, key: str) -> None:
        key = norm_limiter_key(key)
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            self._totals[key] = self._totals.get(key, 0) + 1
            self._totals.move_to_end(key)
            while len(self._totals) > self._max_keys:
                self._totals.popitem(last=False)
            if self._locked_until(key, now) > now:
                return
            rec = self._records.get(key)
            if rec is None:
                rec = self._records[key] = [0, now, 0.0]
            rec[0] += 1
            if rec[0] >= self._max_failures:
                rec[2] = now + self._lock_s
            while len(self._records) > self._max_keys:
                # never evict a locked record (fail-open); if all are locked, drop the new key's record
                victim = next((k for k, r in self._records.items() if r[2] <= now and k != key), key)
                del self._records[victim]

    def success(self, key: str) -> None:
        key = norm_limiter_key(key)
        with self._lock:
            self._records.pop(key, None)

    def retry_after(self, key: str) -> int:
        key = norm_limiter_key(key)
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            until = self._locked_until(key, now)
        return max(0, math.ceil(until - now)) if until > now else 0

    def total_failures(self, key: str) -> int:
        key = norm_limiter_key(key)
        with self._lock:
            return self._totals.get(key, 0)


# --- setup token ----------------------------------------------------------------------------------

_SETUP_TOKEN_FILE = "setup-token"


def read_setup_token(data_dir: Path) -> Optional[str]:
    try:
        fd = os.open(Path(data_dir) / _SETUP_TOKEN_FILE, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as fh:
            token = fh.read(4096).strip()
    except OSError:
        return None
    return token or None


def ensure_setup_token(data_dir: Path) -> str:
    existing = read_setup_token(data_dir)
    if existing:
        return existing
    path = Path(data_dir) / _SETUP_TOKEN_FILE
    token = secrets.token_urlsafe(18)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        # empty leftover or a concurrent creator: overwrite only if there is no usable token
        existing = read_setup_token(data_dir)
        if existing:
            return existing
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(token + "\n")
    return token


def clear_setup_token(data_dir: Path) -> None:
    try:
        (Path(data_dir) / _SETUP_TOKEN_FILE).unlink()
    except FileNotFoundError:
        pass
