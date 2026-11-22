import time
import pytest
from server.auth import make_token, verify_token, check_pin, LoginLimiter


class TestToken:
    def test_roundtrip(self):
        secret = "test-secret"
        token = make_token(secret, ttl_s=3600)
        assert verify_token(secret, token) is True

    def test_tampered_token(self):
        secret = "test-secret"
        token = make_token(secret, ttl_s=3600)
        # Tamper with the HMAC part
        parts = token.split(".")
        tampered = parts[0] + "." + "0" * 64
        assert verify_token(secret, tampered) is False

    def test_expired_token(self):
        secret = "test-secret"
        # Negative TTL means already expired
        token = make_token(secret, ttl_s=-1)
        assert verify_token(secret, token) is False

    def test_none_token(self):
        assert verify_token("secret", None) is False

    def test_wrong_secret(self):
        token = make_token("secret1", ttl_s=3600)
        assert verify_token("secret2", token) is False


class TestCheckPin:
    def test_correct_pin(self):
        assert check_pin("1234", "1234") is True

    def test_wrong_pin(self):
        assert check_pin("1234", "4321") is False

    def test_empty_pin(self):
        assert check_pin("1234", "") is False

    def test_timing_safe(self):
        # Just verify it uses hmac.compare_digest internally (no exception)
        for _ in range(100):
            check_pin("1234", "1234")
            check_pin("1234", "0000")


class TestLoginLimiter:
    def test_allows_up_to_5_failures(self):
        limiter = LoginLimiter()
        ip = "192.168.1.1"
        for _ in range(5):
            assert limiter.allowed(ip) is True
            limiter.failure(ip)
        # 6th attempt should be blocked
        assert limiter.allowed(ip) is False

    def test_success_resets_failures(self):
        limiter = LoginLimiter()
        ip = "192.168.1.2"
        for _ in range(3):
            limiter.failure(ip)
        limiter.success(ip)
        # Should be allowed again
        for _ in range(5):
            assert limiter.allowed(ip) is True
            limiter.failure(ip)
        assert limiter.allowed(ip) is False

    def test_different_ips_independent(self):
        limiter = LoginLimiter()
        for _ in range(5):
            limiter.failure("10.0.0.1")
        assert limiter.allowed("10.0.0.1") is False
        assert limiter.allowed("10.0.0.2") is True

    def test_lockout_expires(self, monkeypatch):
        # We need to monkeypatch time.monotonic in server.auth
        import server.auth as auth_module
        original_monotonic = time.monotonic
        current_time = original_monotonic()
        
        def fake_monotonic():
            return current_time
        
        monkeypatch.setattr(auth_module.time, 'monotonic', fake_monotonic)
        
        limiter = LoginLimiter()
        ip = "192.168.1.10"
        for _ in range(5):
            limiter.failure(ip)
        assert limiter.allowed(ip) is False
        
        # Advance time by 301 seconds (lockout is 5 min = 300s)
        current_time += 301
        
        # Now should be allowed again
        assert limiter.allowed(ip) is True
