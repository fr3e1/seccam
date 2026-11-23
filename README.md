# SecCam

Self-hosted AI security camera. Any phone, laptop or small Linux box becomes a camera; detection runs on the device
(TensorFlow.js) or on the server (YOLOX-Nano ONNX); viewers watch live previews and recorded events from any browser.

```
Android Chrome / iPhone Safari / Linux browser (TF.js) --wss-->  SecCam server (FastAPI)  -->  browser dashboard
Headless agent (ffmpeg + V4L2 USB/Pi camera)        --wss-->  (YOLOX-Nano fallback)
```

## Features

- Cameras: Android Chrome, iPhone Safari, any Linux desktop browser, and a headless agent for USB and Raspberry Pi cameras
- On-device detection (COCO-SSD) with server-side YOLOX-Nano fallback; modes `auto`, `local`, `server`
- Event recording: snapshots with boxes, thumbnails, SQLite index, configurable retention
- Admin account with optional TOTP and recovery codes, device pairing, session management
- Eight dark and light themes, one server-wide setting, remembered in each browser
- One-click web updater with checksum verification, health check and automatic rollback
- Runs on 1 vCPU / 1 GB RAM; no external CDNs; systemd hardening

## Requirements

- Debian 12 (Python 3.11.2) or Ubuntu 22.04+, or a Proxmox VE 8+/9 host (the installer creates a container)
- A browser with `getUserMedia` for camera pages (HTTPS is required by browsers; see the certificate or proxy sections)

## Install

Proxmox, one command on the host shell:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/fr3e1/seccam/main/scripts/proxmox-install.sh)"
```

Any Debian/Ubuntu machine:

```bash
git clone https://github.com/fr3e1/seccam.git && cd seccam
sudo ./deploy/install.sh
```

The installer creates the `seccam` user, installs the release under `/opt/seccam/releases/<version>` with a
virtualenv in `/opt/seccam/venvs/`, points `/opt/seccam/current` at it, writes `/etc/seccam/seccam.env`, starts
`seccam.service`, enables `seccam-update.path` and prints a **setup token**. Open the printed URL, enter the token and
create the admin account. Lost the token? Run `sudo -u seccam /opt/seccam/current/venv/bin/python -m server.manage setup-token`.

Options: `--retention DAYS`, `--trusted-proxies LIST --public-origin URL` (reverse-proxy mode, below), `--pin PIN`
(legacy v1 credential, only used to migrate a v1 install).

### Migrating from v1

Run `sudo ./deploy/install.sh` from a v2 checkout. It converts the flat `/opt/seccam` layout to `releases/` + `current`,
keeps `/var/lib/seccam` and your env file, drops `SECCAM_STATIC_DIR`/`SECCAM_MODEL_PATH` lines that point into
`/opt/seccam/`, sets `SECCAM_UPDATE_MODE=systemd`, and removes the old flat files only after the new layout answers
`/api/health`. Enter the v1 PIN on the first-run setup page to create the admin account.

## Cameras

### Android Chrome
1. Open `https://<server>/camera` in Chrome and sign in as the admin, or pair the browser instead: open `/pair` and enter a code from Settings > Devices. HTTPS is required; either install the local CA (served at
   `/ca.crt`) or use a real certificate through a reverse proxy.
2. Tap **Start**, allow camera access. The page keeps the screen awake (Wake Lock) while running.
3. Use "Add to Home screen" for a full-screen app, and keep the phone charging.

### iPhone Safari
1. Open `http://<server>/ca.crt` in Safari, allow the profile download, install it under Settings > General > VPN & Device
   Management, then enable it under Settings > General > About > Certificate Trust Settings.
2. Open `https://<server>/camera`, sign in (or pair at `/pair` with a code from Settings > Devices), tap **Start**. Set Auto-Lock to Never and keep Safari in front.

### Linux browser
Open `https://<server>/camera` in Firefox or Chrome on any Linux desktop or laptop with a webcam. Trust the CA
(`/ca.crt`) in the browser, or use a proxy with a public certificate.

### Headless agent (USB and Raspberry Pi cameras)
`agent/` holds a single-file agent that streams a V4L2 camera through `ffmpeg`:

```bash
sudo ./agent/install-agent.sh              # on the camera machine
```

In the web UI open **Devices** and create a pairing code (valid 10 minutes), then pair and start:

```bash
sudo -u seccam-agent python3 /opt/seccam-agent/seccam_agent.py pair --server https://<server> \
    --code XXXX-XXXX --name "Garage" --token-file /var/lib/seccam-agent/token --ca-file /path/to/ca.crt
sudo systemctl start seccam-agent
```

See [agent/README.md](agent/README.md) for options. Revoke a device in the UI or with `manage revoke-device`.

## Hosting on the internet (reverse proxy)

Do not expose the built-in listener directly. Put Caddy or nginx in front, terminate TLS there, and run SecCam on
loopback. Examples: [deploy/Caddyfile.example](deploy/Caddyfile.example), [deploy/nginx.conf.example](deploy/nginx.conf.example).

1. Install in proxy mode (first install), or edit `/etc/seccam/seccam.env` and restart:
   ```bash
   sudo ./deploy/install.sh --trusted-proxies 127.0.0.1 --public-origin https://cam.example.com
   ```
   The proxy flags only write `SECCAM_HOST`, the port and the missing cert lines on a **first** install. On an existing
   install they add just `SECCAM_TRUSTED_PROXIES` and `SECCAM_PUBLIC_ORIGIN`; edit the env file by hand for the rest. A
   first install sets `SECCAM_HOST=127.0.0.1`, `SECCAM_HTTPS_PORT=8443`, no `SECCAM_CERT`/`SECCAM_KEY` (the main listener serves plain
   HTTP on `SECCAM_HTTPS_PORT` when no certificate is set), `SECCAM_TRUSTED_PROXIES=127.0.0.1`,
   `SECCAM_PUBLIC_ORIGIN=https://cam.example.com` and `SECCAM_COOKIE_SECURE=true`.
2. **`SECCAM_PUBLIC_ORIGIN` is mandatory behind a proxy.** It is the exact origin browsers use; WebSocket and API origin
   checks compare against it.
3. The proxy must keep the original `Host` header, set `X-Forwarded-For`/`X-Forwarded-Proto`, and upgrade WebSockets on `/ws/`.
   The example files do all of this.
4. Enable TOTP for every admin account, use strong passwords, and keep the web updater on (see SECURITY.md).

## Updates

With `SECCAM_UPDATE_MODE=systemd` (set by the installer), admins see new GitHub releases under Settings > Update and can
click **Update**. The server only writes a request file; a root `seccam-update.service` triggered by `seccam-update.path`
downloads the release tarball over HTTPS from the allowed GitHub hosts, verifies it against `SHA256SUMS`, extracts it into
`releases/<version>`, builds a hashed venv with wheels only, backs up the database, switches `current`, restarts, and waits for
`/api/health` to report the new version. If the check fails it rolls back the code and the database. An interrupted update
is rolled back at the next service start. If a release changes unit files the status says to re-run `deploy/install.sh`.

Without the web updater: `git pull && sudo ./deploy/install.sh`.

## Themes

Pick a theme in Settings > Appearance (one server-wide setting, also remembered in the browser's localStorage): `dracula`, `nord`, `gruvbox-dark`, `tokyo-night`,
`catppuccin-mocha`, `solarized-dark`, `solarized-light`, `one-dark`.

## Admin CLI

Run it as the service user, not as root:

```bash
sudo -u seccam /opt/seccam/current/venv/bin/python -m server.manage <command>
```

Commands: `create-admin`, `reset-password` (`--username`, `--password-stdin`), `disable-totp`, `revoke-sessions`,
`list-devices`, `revoke-device ID`, `setup-token`. As root it refuses unless `--allow-root` is given, and then drops
privileges to the data directory owner before touching anything.

## Configuration

Settings live in `/etc/seccam/seccam.env` ([example](deploy/seccam.env.example)) and in the web UI.

| Variable | Default | Description |
|----------|---------|-------------|
| `SECCAM_DATA_DIR` | `./data` (`/var/lib/seccam` installed) | Database, snapshots, secret |
| `SECCAM_RETENTION_DAYS` | `14` | Event retention |
| `SECCAM_MODEL_PATH` | `<release>/models/yolox_nano.onnx` | ONNX model |
| `SECCAM_STATIC_DIR` | `<release>/static` | Static files |
| `SECCAM_HOST` | `0.0.0.0` | Bind address |
| `SECCAM_HTTPS_PORT` | `8443` (`443` installed) | Main listener: TLS with a cert, plain HTTP without |
| `SECCAM_HTTP_PORT` | `8080` (`80` installed) | Redirect listener (serves `/ca.crt`) |
| `SECCAM_CERT`, `SECCAM_KEY`, `SECCAM_CA_CERT` | empty | TLS files |
| `SECCAM_TRUSTED_PROXIES` | empty | IPs/CIDRs allowed to set `X-Forwarded-*` |
| `SECCAM_PUBLIC_ORIGIN` | empty | Public origin; mandatory behind a proxy |
| `SECCAM_COOKIE_SECURE` | `auto` | `auto`, `true` or `false` |
| `SECCAM_SESSION_TTL_DAYS` | `30` | Session lifetime (1-365) |
| `SECCAM_UPDATE_MODE` | `off` (`systemd` installed) | Web updater |
| `SECCAM_UPDATE_REPO` | `fr3e1/seccam` | GitHub `owner/repo` for updates |
| `SECCAM_SECRET` | auto | Reserved |
| `SECCAM_PIN` | empty | Legacy v1 credential for migration |

## Development

Python 3.11+ (Debian 12's 3.11.2 is supported) and Node 22 for the JS tests.

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q --timeout 60
node --test tests/js/*.test.mjs
.venv/bin/python -m server.main     # plain HTTP on :8443
```

Releases: bump `VERSION`, push a tag `vX.Y.Z`; the release workflow tests, runs `scripts/make-release.sh` and publishes
`seccam-X.Y.Z.tar.gz` with `SHA256SUMS`.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Browser refuses the camera | Camera needs HTTPS: trust `/ca.crt` or use a reverse proxy with a real certificate |
| Login or WebSocket fails behind a proxy | Check `SECCAM_PUBLIC_ORIGIN`, `SECCAM_TRUSTED_PROXIES` and that `Host` is preserved |
| Camera stops when the screen locks | Keep the page in front, disable auto-lock, use a charger; or use the headless agent |
| Update shows `rolled_back` | See `journalctl -u seccam-update`; the previous version is running |
| Lost setup token | `sudo -u seccam ... python -m server.manage setup-token` |

## License

MIT, see [LICENSE](LICENSE). Third-party notices: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
