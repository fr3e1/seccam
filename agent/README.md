# SecCam camera agent

A single-file headless agent that streams a V4L2 camera (USB webcam, Raspberry Pi camera) to a SecCam server
through `ffmpeg`. Needs Python 3.11+, `websockets>=13,<16` and `ffmpeg`.

## Install (Debian/Ubuntu/Raspberry Pi OS)
```bash
sudo ./install-agent.sh
```
This installs ffmpeg and python3-websockets, creates the `seccam-agent` user (in the `video` group), copies the agent to
`/opt/seccam-agent`, installs the systemd unit and writes `/etc/seccam-agent.env`. It does not start the service until a
token exists. To run without installing: `pip install -r requirements.txt` and use `python3 seccam_agent.py`.

## Pair
In the SecCam web UI open Devices and create a pairing code (valid 10 minutes), then:
```bash
sudo -u seccam-agent python3 /opt/seccam-agent/seccam_agent.py pair \
    --server https://seccam.example:8443 --code XXXX-XXXX --name "Garage" \
    --token-file /var/lib/seccam-agent/token --ca-file /path/to/ca.crt
```
Use `--ca-file` for a self-signed or private CA (the server serves it at `/ca.crt`). `--insecure` skips TLS
verification and should only be used for testing. The token file is written with mode 0600. Pairing fails with the
server's message if the code is wrong or expired (exit 1).

## Run
```bash
python3 seccam_agent.py list-devices          # /dev/video0<TAB>Camera name
python3 seccam_agent.py run --server https://seccam.example:8443 --device /dev/video0 \
    --width 1280 --height 720 --fps 5 --input-format mjpeg --quality 5
```
Options: `--token-file`, `--name`, `--ffmpeg`, `--ca-file | --insecure`. Environment fallbacks: `SECCAM_AGENT_SERVER`,
`SECCAM_AGENT_TOKEN_FILE`, `SECCAM_AGENT_DEVICE`. The agent follows the server's `preview_fps`/`detect_fps` settings,
capped at `--fps`, and reconnects with jittered backoff.

Exit codes: 0 clean stop, 2 usage error, 3 token rejected (re-pair), 4 ffmpeg missing.

## systemd
Edit `/etc/seccam-agent.env` (set `SECCAM_AGENT_SERVER`, and `SECCAM_AGENT_TOKEN_FILE` if you changed it), pair as above, then:
```bash
sudo systemctl start seccam-agent
journalctl -u seccam-agent -f
```
The unit does not restart after exit code 3 (token revoked). Pair again, then start the service.

## Troubleshooting
- **`ffmpeg not found` (exit 4):** install ffmpeg or pass `--ffmpeg /path/to/ffmpeg`.
- **ffmpeg errors about the device or format:** run `list-devices`; try `--input-format yuyv422` or a lower `--width/--height`.
  Check with `ffmpeg -f v4l2 -list_formats all -i /dev/video0`.
- **Permission denied on /dev/video0:** the user must be in the `video` group.
- **Exit 3 / "token rejected":** the device was revoked in the UI. Delete it there and pair again.
- **TLS errors:** pass `--ca-file` with the server CA, or put the CA in the system trust store.
- **Repeated reconnects:** check the server URL and that the server is reachable; the agent retries with growing delays (max 30 s).
