# Security Policy

## Reporting a vulnerability

Report issues privately through **GitHub Security Advisories**:
https://github.com/fr3e1/seccam/security/advisories/new

Do not open public issues for vulnerabilities.

## Threat model

SecCam is designed for a home LAN and, with a TLS reverse proxy, for the internet. Assumed attacker: anyone who can reach
the HTTP port, a stolen camera device, or a compromised web process. Not in scope: an attacker with root on the host, or
compromise of the GitHub release account.

## What is protected

- Accounts: scrypt password hashing, optional TOTP and recovery codes, login rate limiting and lockout, first-run setup
  gated by a one-time setup token read from the data directory
- Sessions: opaque server-side session tokens in HttpOnly SameSite cookies (Secure when served over HTTPS), configurable TTL, revocable
- Devices: cameras and agents use revocable pairing tokens, never user passwords
- Network: origin checks on WebSockets and state-changing requests against `SECCAM_PUBLIC_ORIGIN`; `X-Forwarded-*` is trusted
  only from `SECCAM_TRUSTED_PROXIES`; no external CDNs; strict content security policy
- Process: the service runs as the unprivileged `seccam` user with `NoNewPrivileges`, `ProtectSystem=strict`, read-only code
  directory, memory and CPU limits
- Updates: the web process can only write a request file. A root unit downloads over HTTPS from allowlisted GitHub hosts,
  verifies the `SHA256SUMS` entry, validates every tarball member, installs wheels only, keeps backups and its journal in a
  root-only directory, and rolls back on a failed health check. Note that updates trust the GitHub repository's releases.
- Admin CLI: refuses to run as root; with `--allow-root` it drops to the data directory owner

## Operator checklist

- Behind a proxy, set `SECCAM_PUBLIC_ORIGIN`, `SECCAM_TRUSTED_PROXIES` and `SECCAM_COOKIE_SECURE=true`; bind to `127.0.0.1`
- Enable TOTP for admin accounts; revoke lost devices; install the local CA only on devices you trust
- Set `SECCAM_UPDATE_MODE=off` if you do not want the root updater
