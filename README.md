# SecCam

Self-hosted AI security camera. An iPhone acts as the camera (on-device TensorFlow.js detection with server-side YOLOX-Nano fallback). Viewers watch live events from any browser.

## How it works

```
iPhone Safari (TF.js on-device AI) --wss--> SecCam Server (FastAPI + YOLOX-Nano fallback) --> Browser Dashboard
```

## Features

- On-device detection via TensorFlow.js COCO-SSD (lite_mobilenet_v2); the server only receives low-rate preview frames, detections and event snapshots, never runs AI
- Server fallback: streams JPEG frames; server runs YOLOX-Nano (ONNX, 416×416)
- Auto mode: phone tries local first, falls back if model fails to load or avg inference > 1.2 s over first 5 runs
- Event recording: snapshots with bounding boxes, thumbnails, SQLite index, configurable retention
- Multi-viewer: any number of browsers watch live preview + events
- Low resource: targets 1 vCPU, 1 GB RAM LXC; ~60 MB RAM idle, ~120 MB with server AI
- Secure by default: PIN auth, HttpOnly Secure cookies, local CA for TLS, systemd hardening
- First-run setup wizard: choose PIN and preferences in browser (no CLI required)

## Requirements

- Proxmox VE 8+/9 or any Debian 12 / Ubuntu 22.04+ machine
- iPhone with iOS 16.4+ (Safari, WebGL support)
- Same LAN (no port-forwarding needed or recommended)

## Quick start (Proxmox)

Run **one command** in the Proxmox host shell:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/fr3e1/seccam/main/scripts/proxmox-install.sh)"
```

Then open `https://<container-ip>/` in a browser → accept the certificate warning → the setup page asks for a PIN and preferences.

## Quick start (any Debian 12 / Ubuntu 22.04+ machine)

```bash
git clone https://github.com/fr3e1/seccam.git
cd seccam
sudo ./deploy/install.sh
```

## Connect your iPhone

1. Open `http://<server-ip>/ca.crt` in **Safari** → Allow → "Profile Downloaded"
2. Settings → General → VPN & Device Management → tap "SecCam Local CA" → Install
3. Settings → General → About → Certificate Trust Settings → enable "SecCam Local CA"
4. Open `https://<server-ip>/camera` in Safari, log in with your PIN, tap **Start**
5. **Tip**: Settings → Display & Brightness → Auto-Lock → Never; keep Safari in front; use a charger

## Using the dashboard

- **Live view**: real-time preview frames from all cameras with latest detections overlaid
- **Events**: chronological list with thumbnails; filter by label and camera
- **Notifications**: browser notifications for new events while the dashboard is open (bell icon)
- **Settings**: detection labels, confidence threshold, cooldown, preview/detect FPS, AI mode, retention
- **Change PIN**: Settings → Security → Change PIN (invalidates all other sessions)

## AI modes

| Mode | Description | Models |
|------|-------------|--------|
| `auto` (default) | Phone loads TF.js + COCO-SSD. Falls back to server if load fails or avg inference > 1200 ms over first 5 runs. | TF.js COCO-SSD (lite_mobilenet_v2) → YOLOX-Nano ONNX |
| `local` | Forces on-phone detection. Errors shown in UI. | TF.js COCO-SSD only |
| `server` | Forces server-side detection. Phone sends JPEG frames at `detect_fps`. | YOLOX-Nano ONNX only |

Resource use: ~60 MB RAM idle, ~120 MB with server AI, 1 vCPU fine (ONNX Runtime single-threaded).

## Configuration

All settings via `/etc/seccam/seccam.env` (see `deploy/seccam.env.example`) or in the web UI (first-run wizard at `/welcome`, then the Settings drawer).

| Variable | Default | Description |
|----------|---------|-------------|
| `SECCAM_PIN` | *(optional, legacy)* | Login PIN (4–12 digits). If unset, first-run wizard creates it. |
| `SECCAM_SECRET` | auto-generated | Session signing key (generated once, stored in `DATA_DIR/secret`). |
| `SECCAM_DATA_DIR` | `/var/lib/seccam` | Data directory (events, snapshots, secret). |
| `SECCAM_RETENTION_DAYS` | `14` | Event retention (also editable in UI). |
| `SECCAM_MODEL_PATH` | `/opt/seccam/models/yolox_nano.onnx` | ONNX model path. |
| `SECCAM_HOST` | `0.0.0.0` | Bind address. |
| `SECCAM_HTTPS_PORT` | `443` | HTTPS port. |
| `SECCAM_HTTP_PORT` | `80` | HTTP redirect port (serves `/ca.crt`, `/setup`, redirects rest). |
| `SECCAM_CERT` | `/etc/seccam/tls/server.crt` | TLS certificate. |
| `SECCAM_KEY` | `/etc/seccam/tls/server.key` | TLS private key. |
| `SECCAM_CA_CERT` | `/etc/seccam/tls/ca.crt` | CA certificate (served at `/ca.crt`). |
| `SECCAM_STATIC_DIR` | `/opt/seccam/static` | Static files directory. |

### Detection settings (via UI / API)

| Setting | Range | Default | Description |
|---------|-------|---------|-------------|
| `labels` | subset of `ALL_LABELS` | person, car, truck, bus, motorcycle, bicycle, cat, dog, bird | Which classes trigger events |
| `min_score` | 0.1–0.95 | 0.55 | Minimum confidence |
| `cooldown_s` | 0–3600 | 30 | Seconds between events per camera |
| `preview_fps` | 0.2–10 | 2 | Preview frame rate (phone → server/viewers) |
| `detect_fps` | 0.2–10 | 2 | Server detection rate (server mode) |
| `mode` | auto\|local\|server | auto | Detection mode |
| `retention_days` | 1–365 | 14 | Event retention (also in env) |

## Updating

Inside the container (or on the host for bare-metal):

```bash
cd /opt/seccam-src && git pull && ./deploy/install.sh
```

Preserves `/var/lib/seccam` data, updates code, restarts service.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
python -m server.main   # serves plain HTTP on :8443 (dev mode, no certs)
```

Open `http://localhost:8443/` in a browser.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Certificate not trusted" on iPhone | iOS doesn't trust local CA | Open `https://<server>/ca.crt` → Settings → VPN & Device Management → Trust "SecCam Local CA" |
| Camera stops after screen locks | iOS Auto-Lock | Settings → Display & Brightness → Auto-Lock → Never |
| Camera stops when switching apps | iOS suspends background tabs | Keep camera tab foreground; add to Home Screen |
| "Model load failed" on phone | Old iOS / no WebGL | Falls back to server mode automatically; or force `server` mode in Settings |
| High CPU on server | YOLOX-Nano running | Normal during detection; limited to 1 thread, `detect_fps` max 10 |
| No events recorded | Labels / score / cooldown | Check Settings: enabled labels, `min_score`, `cooldown_s` |
| Viewer shows stale frames | WebSocket backpressure | Server drops slow viewers (2 s timeout); refresh page |
| IP changed | DHCP lease expired | Re-run installer to reissue cert; recommend DHCP reservation |

## Security notes

- **LAN only** — do not port-forward 443/80 to the internet
- Change the default PIN (via `--pin` on install or Settings → Security)
- The local CA is generated per-host; install `ca.crt` only on trusted devices
- Sessions are signed cookies (HMAC-SHA256), 30-day TTL, HttpOnly, Secure, SameSite=Lax
- Login rate-limited: 5 failures / 5 min per IP → 5 min lockout
- Systemd hardening: `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, memory/CPU limits, minimal capabilities (`CAP_NET_BIND_SERVICE` only for ports 80/443)
- No external CDNs — all JS/CSS served locally

## License

MIT — see [LICENSE](LICENSE)

## Third-party notices

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
