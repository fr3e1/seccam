# Security Policy

## Reporting a vulnerability

Please report security issues privately via **GitHub Security Advisories**:
https://github.com/fr3e1/seccam/security/advisories/new

Do not open public issues for security vulnerabilities.

## Design principles

- LAN-only deployment — no internet exposure intended or supported
- Local CA per host; certificates not shared across installations
- PIN-based auth with PBKDF2-HMAC-SHA256 (200,000 iterations), 16-byte salt
- Session cookies: HMAC-SHA256, 30-day TTL, HttpOnly, Secure, SameSite=LaX
- Login rate limiting: 5 failures / 5 min per IP → 5 min lockout
- Systemd hardening: `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, memory/CPU limits, `CAP_NET_BIND_SERVICE` only
- No external dependencies at runtime (all JS/CSS vendored)
