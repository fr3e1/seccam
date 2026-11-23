#!/usr/bin/env bash
# Install the SecCam camera agent (run as root). Does not start the service until a token exists.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "run as root (sudo $0)" >&2
    exit 1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/opt/seccam-agent
STATE=/var/lib/seccam-agent
ENV_FILE=/etc/seccam-agent.env

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ffmpeg python3 python3-websockets

if ! id -u seccam-agent >/dev/null 2>&1; then
    useradd --system --home-dir "${STATE}" --shell /usr/sbin/nologin --groups video seccam-agent
else
    usermod -aG video seccam-agent
fi

install -d -m 0755 "${DEST}"
install -m 0644 "${SRC}/seccam_agent.py" "${DEST}/seccam_agent.py"
install -d -m 0700 -o seccam-agent -g seccam-agent "${STATE}"

if [[ ! -f "${ENV_FILE}" ]]; then
    cat > "${ENV_FILE}" <<'ENVEOF'
# SecCam agent settings. Edit SECCAM_AGENT_SERVER, then pair (see README).
SECCAM_AGENT_SERVER=https://seccam.example:8443
SECCAM_AGENT_TOKEN_FILE=/var/lib/seccam-agent/token
SECCAM_AGENT_DEVICE=/dev/video0
ENVEOF
    chmod 0644 "${ENV_FILE}"
fi

install -m 0644 "${SRC}/seccam-agent.service" /etc/systemd/system/seccam-agent.service
systemctl daemon-reload

if [[ -s "${STATE}/token" ]]; then
    systemctl enable --now seccam-agent.service
    echo "seccam-agent started."
else
    systemctl enable seccam-agent.service
    cat <<MSG
Installed. No token yet, so the service is not started. Pair it, then start it:
  sudo -u seccam-agent python3 ${DEST}/seccam_agent.py pair --server https://HOST:8443 \\
      --code XXXX-XXXX --name "Garage" --token-file ${STATE}/token [--ca-file ca.crt]
  sudo systemctl start seccam-agent
MSG
fi
