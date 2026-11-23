import base64
import hashlib
import os
import stat
import threading

import pytest

from server import auth
from server.auth import LoginLimiter, PairingCodes


# --- legacy PIN helpers ---

def test_check_pin():
    assert auth.check_pin("1234", "1234") is True
    assert auth.check_pin("1234", "12345") is False


def test_check_pin_non_ascii_returns_false():
    assert auth.check_pin("1234", "12\u00e94") is False
    assert auth.check_pin("12\u00e94", "12\u00e94") is True


def test_hash_pin_roundtrip():
    rec = auth.hash_pin("123456")
    assert auth.verify_pin_hash("123456", rec) is True
    assert auth.verify_pin_hash("654321", rec) is False
    assert auth.verify_pin_hash("123456", {"salt": "zz"}) is False


def test_removed_token_helpers():
    for name in ("make_token", "verify_token", "derive_token_secret"):
        assert not hasattr(auth, name)


# --- usernames / passwords ---

@pytest.mark.parametrize("bad", ["ab", "x" * 33, "bad name", "ünï", "", None, 5, "a/b"])
def test_validate_username_rejects(bad):
    with pytest.raises(ValueError, match="username must be 3-32 chars"):
        auth.validate_username(bad)


def test_validate_username_strips():
    assert auth.validate_username("  Alice_1  ") == "Alice_1"


def test_validate_password():
    auth.validate_password("correct horse", "alice")
    auth.validate_password("ab" * 128)
    for bad in ("short", "x" * 257, "aaaaaaaaaaaa", 123, None):
        with pytest.raises(ValueError):
            auth.validate_password(bad, "alice")
    with pytest.raises(ValueError):
        auth.validate_password("AliceAlice", "aliceALICE")


def test_password_hash_format_and_verify():
    enc = auth.hash_password("hunter2hunter2")
    parts = enc.split("$")
    assert parts[:4] == ["scrypt", "32768", "8", "1"]
    assert len(base64.urlsafe_b64decode(parts[4] + "==")) == 16
    assert len(base64.urlsafe_b64decode(parts[5] + "==")) == 32
    assert "=" not in parts[4] + parts[5]
    assert auth.verify_password("hunter2hunter2", enc) is True
    assert auth.verify_password("hunter2hunter3", enc) is False
    assert auth.hash_password("hunter2hunter2") != enc
    assert auth.needs_rehash(enc) is False


@pytest.mark.parametrize("bad", ["", "garbage", "scrypt$1$2", "scrypt$x$8$1$aa$bb", "md5$1$1$1$aa$bb",
                                 "scrypt$32768$8$1$!!$!!", "scrypt$3$8$1$aa$bb", "scrypt$32768$8$1$$"])
def test_verify_password_malformed(bad):
    assert auth.verify_password("x", bad) is False


def test_needs_rehash_on_different_params():
    assert auth.needs_rehash("scrypt$16384$8$1$AAAA$AAAA") is True
    assert auth.needs_rehash("nonsense") is True


def test_dummy_hash_valid():
    assert auth.DUMMY_HASH.startswith("scrypt$32768$8$1$")
    assert auth.verify_password("anything", auth.DUMMY_HASH) is False


# --- TOTP ---

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")


@pytest.mark.parametrize("t,expected", [(59, "94287082"), (1111111109, "07081804"),
                                        (1111111111, "14050471"), (1234567890, "89005924"),
                                        (2000000000, "69279037"), (20000000000, "65353130")])
def test_totp_rfc6238_vectors(t, expected):
    assert auth.totp_code(RFC_SECRET, t // 30, digits=8) == expected


def test_totp_six_digits_and_secret_shape():
    s = auth.new_totp_secret()
    assert len(s) == 32 and "=" not in s
    code = auth.totp_code(s, 5)
    assert len(code) == 6 and code.isdigit()


def test_verify_totp_window_and_replay():
    now = 1_700_000_000.0
    step = int(now // 30)
    for offset in (-1, 0, 1):
        assert auth.verify_totp(RFC_SECRET, auth.totp_code(RFC_SECRET, step + offset), 0, now=now) == step + offset
    assert auth.verify_totp(RFC_SECRET, auth.totp_code(RFC_SECRET, step + 2), 0, now=now) is None
    assert auth.verify_totp(RFC_SECRET, auth.totp_code(RFC_SECRET, step - 2), 0, now=now) is None
    code = auth.totp_code(RFC_SECRET, step)
    assert auth.verify_totp(RFC_SECRET, code, step, now=now) is None
    assert auth.verify_totp(RFC_SECRET, code, step - 1, now=now) == step


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "abcdef", "12 456", "١٢٣٤٥٦"])
def test_verify_totp_rejects_malformed(bad):
    assert auth.verify_totp(RFC_SECRET, bad, 0, now=1e9) is None


def test_otpauth_uri():
    uri = auth.otpauth_uri("ABC234", "al ice")
    assert uri == ("otpauth://totp/SecCam:al%20ice?secret=ABC234&issuer=SecCam"
                   "&algorithm=SHA1&digits=6&period=30")


# --- recovery codes ---

def test_recovery_codes():
    codes, hashes = auth.new_recovery_codes()
    assert len(codes) == len(hashes) == 8 and len(set(codes)) == 8
    for c, h in zip(codes, hashes):
        assert len(c) == 9 and c[4] == "-"
        assert set(c.replace("-", "")) <= set("abcdefghijkmnpqrstuvwxyz23456789")
        assert h == hashlib.sha256(c.encode()).hexdigest()
    rest = auth.use_recovery_code("  " + codes[3].upper() + " ", hashes)
    assert rest == hashes[:3] + hashes[4:]
    assert auth.use_recovery_code("zzzz-zzzz", hashes) is None
    assert auth.use_recovery_code(codes[0], rest if codes[0] != codes[3] else []) is not None
    assert auth.use_recovery_code(codes[3], rest) is None


# --- tokens ---

def test_session_and_device_tokens():
    tok, h = auth.new_session_token()
    assert h == auth.hash_token(tok) and len(h) == 64
    assert len(auth.new_csrf_token()) >= 43
    did = auth.new_device_id()
    assert len(did) == 12 and int(did, 16) >= 0
    token, thash = auth.new_device_token(did)
    assert token.startswith(f"scd_{did}.")
    assert auth.parse_device_token(token) == (did, token.split(".", 1)[1])
    assert thash == auth.hash_token(token.split(".", 1)[1])
    assert auth.parse_device_token("Bearer " + token) == auth.parse_device_token(token)
    assert auth.parse_device_token("bearer  " + token) is not None


@pytest.mark.parametrize("bad", [None, "", "abc", "scd_xyz.abc", "scd_0123456789ab.", "scd_0123456789AB.aaaaaaaaaaaaaaaaaaaa",
                                 "scd_0123456789ab.a b", 7])
def test_parse_device_token_rejects(bad):
    assert auth.parse_device_token(bad) is None


# --- pairing codes ---

def test_pairing_code_format_and_normalize():
    code = auth.new_pairing_code()
    assert len(code) == 9 and code[4] == "-"
    assert set(code.replace("-", "")) <= set(auth.PAIRING_ALPHABET)
    assert auth.normalize_pairing_code(" abcd efgh ") == "ABCD-EFGH"
    assert auth.normalize_pairing_code("abcd-efgh") == "ABCD-EFGH"
    for bad in ("ABCD-EFG", "ABCD-EFGHJ", "ABCD-EFG0", "ABCD-EFGO", "ABCD-EFG1", None, 12):
        assert auth.normalize_pairing_code(bad) is None


def test_pairing_codes_issue_consume():
    pc = PairingCodes(ttl_s=600, max_live=2)
    c1, exp = pc.issue("Porch", now=1000.0)
    assert exp == 1_600_000
    assert pc.consume(c1.lower().replace("-", " "), now=1001.0) == {"name": "Porch"}
    assert pc.consume(c1, now=1001.0) is None
    assert pc.consume("garbage") is None
    c2, _ = pc.issue(now=1000.0)
    pc.issue(now=1000.0)
    with pytest.raises(ValueError, match="too many codes"):
        pc.issue(now=1000.0)
    assert pc.consume(c2, now=1000.0) == {"name": None}


def test_pairing_codes_expire_and_free_slots():
    pc = PairingCodes(ttl_s=10, max_live=1)
    c, _ = pc.issue(now=0.0)
    assert pc.consume(c, now=10.0) is None
    c, _ = pc.issue(now=0.0)
    c, _ = pc.issue(now=11.0)  # the expired one no longer counts
    assert pc.consume(c, now=11.5) is not None


def test_pairing_codes_thread_safe():
    pc = PairingCodes(max_live=1000)
    out = []
    ts = [threading.Thread(target=lambda: out.append(pc.issue()[0])) for _ in range(50)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(set(out)) == len(out) == 50


# --- LoginLimiter ---

@pytest.fixture
def clock(monkeypatch):
    state = {"t": 1000.0}
    monkeypatch.setattr(auth.time, "monotonic", lambda: state["t"])
    return state


def test_limiter_locks_after_max_failures(clock):
    lim = LoginLimiter()
    for _ in range(4):
        lim.failure("k")
    assert lim.allowed("k") and lim.retry_after("k") == 0
    lim.failure("k")
    assert not lim.allowed("k")
    assert lim.retry_after("k") == 300
    clock["t"] += 100.5
    assert lim.retry_after("k") == 200
    clock["t"] += 200
    assert lim.allowed("k") and lim.retry_after("k") == 0
    lim.failure("k")  # fresh count after the lock expired
    assert lim.allowed("k")


def test_limiter_custom_params_and_keys_independent(clock):
    lim = LoginLimiter(max_failures=2, window_s=10, lock_s=5)
    lim.failure("a")
    clock["t"] += 11  # outside the window
    lim.failure("a")
    assert lim.allowed("a")
    lim.failure("a")
    assert not lim.allowed("a") and lim.allowed("b")
    assert lim.retry_after("a") == 5


def test_limiter_success_resets_but_total_persists(clock):
    lim = LoginLimiter()
    for _ in range(3):
        lim.failure("k")
    lim.success("k")
    for _ in range(4):
        lim.failure("k")
    assert lim.allowed("k")
    assert lim.total_failures("k") == 7
    clock["t"] += 10_000
    assert lim.allowed("k") and lim.total_failures("k") == 7
    assert lim.total_failures("never") == 0


def test_limiter_long_keys_hashed(clock):
    lim = LoginLimiter(max_failures=1)
    long_a, long_b = "x" * 200 + "a", "x" * 200 + "b"
    lim.failure(long_a)
    assert not lim.allowed(long_a) and lim.allowed(long_b)
    assert all(len(k) <= 128 for k in lim._records)
    assert lim.total_failures(long_a) == 1


def test_limiter_evicts_oldest(clock):
    lim = LoginLimiter(max_keys=3)
    for i in range(5):
        clock["t"] += 1
        lim.failure(f"k{i}")
    assert len(lim._records) == 3
    assert "k0" not in lim._records and "k4" in lim._records


def test_limiter_prunes_expired(clock):
    lim = LoginLimiter()
    lim.failure("old")
    clock["t"] += 301
    lim.allowed("other")
    assert "old" not in lim._records


# --- setup token ---

def test_setup_token_lifecycle(tmp_path):
    assert auth.read_setup_token(tmp_path) is None
    tok = auth.ensure_setup_token(tmp_path)
    assert len(tok) >= 20
    assert auth.ensure_setup_token(tmp_path) == tok
    assert auth.read_setup_token(tmp_path) == tok
    path = tmp_path / "setup-token"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    auth.clear_setup_token(tmp_path)
    assert auth.read_setup_token(tmp_path) is None
    auth.clear_setup_token(tmp_path)  # idempotent
    new = auth.ensure_setup_token(tmp_path)
    assert new != tok


def test_setup_token_empty_file_replaced(tmp_path):
    (tmp_path / "setup-token").write_text("")
    assert auth.ensure_setup_token(tmp_path)


def test_limiter_eviction_never_drops_locked_record(clock):
    lim = LoginLimiter(max_failures=1, max_keys=3)
    lim.failure("locked")
    assert not lim.allowed("locked")
    for i in range(50):
        lim.failure(f"flood{i}")
    assert not lim.allowed("locked")
    assert len(lim._records) <= 3


def test_limiter_all_locked_new_key_not_stored(clock):
    lim = LoginLimiter(max_failures=1, max_keys=2)
    lim.failure("a")
    lim.failure("b")
    lim.failure("c")
    assert not lim.allowed("a") and not lim.allowed("b")
    assert len(lim._records) == 2
    assert lim.total_failures("c") == 1


def test_setup_token_refuses_symlink(tmp_path):
    target = tmp_path / "victim"
    target.write_text("keep")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "setup-token").symlink_to(target)
    with pytest.raises(OSError):
        auth.ensure_setup_token(tmp_path / "data")
    assert target.read_text() == "keep"
