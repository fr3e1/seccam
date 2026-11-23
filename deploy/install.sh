#!/usr/bin/env bash
set -euo pipefail

# SecCam installer for Debian 12 / Ubuntu 22.04+ (Python 3.11+).
# Usage: install.sh [--retention DAYS] [--trusted-proxies LIST --public-origin URL] [--pin PIN]
# Run as root. Idempotent: safe for first installs, re-runs, and migration of a v1 flat install.
#
# Layout: /opt/seccam/releases/<VERSION>, /opt/seccam/venvs/<hash>, /opt/seccam/current -> releases/<VERSION>

PIN=""
RETENTION_DAYS=""
TRUSTED_PROXIES=""
PUBLIC_ORIGIN=""

usage() {
    echo "Usage: $0 [--retention DAYS] [--trusted-proxies LIST --public-origin URL] [--pin PIN]" >&2
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --pin) PIN="${2:-}"; shift 2 ;;
        --retention) RETENTION_DAYS="${2:-}"; shift 2 ;;
        --trusted-proxies) TRUSTED_PROXIES="${2:-}"; shift 2 ;;
        --public-origin) PUBLIC_ORIGIN="${2:-}"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
INSTALL_DIR="/opt/seccam"
DATA_DIR="/var/lib/seccam"
CONFIG_DIR="/etc/seccam"
TLS_DIR="$CONFIG_DIR/tls"
ENV_FILE="$CONFIG_DIR/seccam.env"
SYSTEMD_DIR="/etc/systemd/system"

VERSION="$(tr -d '[:space:]' < "$PROJECT_ROOT/VERSION")"
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Invalid VERSION file: $VERSION" >&2
    exit 1
fi
if [[ -n "$PIN" ]] && ! [[ "$PIN" =~ ^[0-9]{4,12}$ ]]; then
    echo "PIN must be 4-12 digits" >&2
    exit 1
fi
if [[ -n "$RETENTION_DAYS" ]] && ! [[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]]; then
    echo "retention must be a number of days" >&2
    exit 1
fi
if [[ -n "$TRUSTED_PROXIES" && -z "$PUBLIC_ORIGIN" ]]; then
    echo "--public-origin is mandatory together with --trusted-proxies" >&2
    exit 1
fi
if [[ -n "$PUBLIC_ORIGIN" ]] && ! [[ "$PUBLIC_ORIGIN" =~ ^https?://[A-Za-z0-9.\-\[\]:]+$ ]]; then
    echo "--public-origin must look like https://cam.example.com (no path)" >&2
    exit 1
fi
if [[ -n "$TRUSTED_PROXIES" ]] && ! [[ "$TRUSTED_PROXIES" =~ ^[0-9A-Fa-f.:/,\ ]+$ ]]; then
    echo "--trusted-proxies must be a comma-separated list of IPs/CIDRs" >&2
    exit 1
fi

echo "=== SecCam installation $VERSION ==="

echo "Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-venv openssl ca-certificates curl \
    >/dev/null

if ! id -u seccam >/dev/null 2>&1; then
    echo "Creating system user 'seccam'..."
    useradd --system --no-create-home --home-dir "$DATA_DIR" --shell /usr/sbin/nologin seccam
fi

mkdir -p "$DATA_DIR" "$CONFIG_DIR" "$TLS_DIR" "$INSTALL_DIR/releases" "$INSTALL_DIR/venvs"
chown seccam:seccam "$DATA_DIR"
chmod 750 "$DATA_DIR" "$CONFIG_DIR" "$TLS_DIR"
chown root:seccam "$CONFIG_DIR" "$TLS_DIR"
chmod 755 "$INSTALL_DIR"

# --- env file helpers (never source the file: it may hold secrets) ---
env_get() { # env_get KEY -> value or empty
    [[ -f "$ENV_FILE" ]] || return 0
    { grep -E "^$1=" "$ENV_FILE" || true; } | tail -1 | cut -d= -f2-
}
env_del() { # env_del KEY
    [[ -f "$ENV_FILE" ]] || return 0
    local tmp
    tmp="$(mktemp)"
    { grep -Ev "^$1=" "$ENV_FILE" || true; } > "$tmp"
    cat "$tmp" > "$ENV_FILE"
    rm -f "$tmp"
}
env_set() { # env_set KEY VALUE
    env_del "$1"
    printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
}

# --- release directory ---
TARGET="$INSTALL_DIR/releases/$VERSION"
if [[ "$(readlink -f "$PROJECT_ROOT")" != "$(readlink -f "$TARGET")" ]]; then
    echo "Installing release $VERSION..."
    rm -rf "$INSTALL_DIR/releases/.tmp-$VERSION" "$TARGET"
    mkdir -p "$INSTALL_DIR/releases/.tmp-$VERSION"
    for item in server static models agent deploy VERSION requirements.txt LICENSE; do
        if [[ -e "$PROJECT_ROOT/$item" ]]; then
            cp -a "$PROJECT_ROOT/$item" "$INSTALL_DIR/releases/.tmp-$VERSION/"
        fi
    done
    find "$INSTALL_DIR/releases/.tmp-$VERSION" -name "__pycache__" -prune -exec rm -rf {} +
    chown -R root:root "$INSTALL_DIR/releases/.tmp-$VERSION"
    chmod -R go+rX,go-w "$INSTALL_DIR/releases/.tmp-$VERSION"
    mv "$INSTALL_DIR/releases/.tmp-$VERSION" "$TARGET"
fi

# --- hashed venv ---
KEY="$(sha256sum "$TARGET/requirements.txt" | cut -c1-16)"
VENV="$INSTALL_DIR/venvs/$KEY"
if [[ ! -x "$VENV/bin/python" ]]; then
    echo "Creating Python virtual environment..."
    rm -rf "$VENV"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --no-cache-dir --only-binary=:all: --no-input -r "$TARGET/requirements.txt" -q
fi
ln -sfn "../../venvs/$KEY" "$TARGET/venv"

# --- TLS (skipped behind a reverse proxy) ---
PROXY_MODE=0
[[ -n "$TRUSTED_PROXIES" ]] && PROXY_MODE=1
if [[ $PROXY_MODE -eq 0 && ! -f "$ENV_FILE" ]] || [[ $PROXY_MODE -eq 0 && -n "$(env_get SECCAM_CERT)" ]]; then
    echo "Generating TLS certificates..."
    "$SCRIPT_DIR/gen-certs.sh" "$TLS_DIR"
fi

# Repair existing installs: the CA key must be root-only
if [[ -f "$TLS_DIR/ca.key" ]]; then
    chown root:root "$TLS_DIR/ca.key"
    chmod 600 "$TLS_DIR/ca.key"
fi

# --- env file: create on first install, rewrite in place otherwise ---
echo "Writing environment file: $ENV_FILE"
if [[ ! -f "$ENV_FILE" ]]; then
    umask 027
    {
        echo "# SecCam environment configuration (see deploy/seccam.env.example for every variable)"
        echo "SECCAM_DATA_DIR=$DATA_DIR"
    } > "$ENV_FILE"
    if [[ $PROXY_MODE -eq 1 ]]; then
        env_set SECCAM_HOST 127.0.0.1
        env_set SECCAM_HTTPS_PORT 8443
        env_set SECCAM_COOKIE_SECURE true
    else
        env_set SECCAM_HOST 0.0.0.0
        env_set SECCAM_HTTPS_PORT 443
        env_set SECCAM_HTTP_PORT 80
        env_set SECCAM_CERT "$TLS_DIR/server.crt"
        env_set SECCAM_KEY "$TLS_DIR/server.key"
        env_set SECCAM_CA_CERT "$TLS_DIR/ca.crt"
    fi
fi
for var in SECCAM_STATIC_DIR SECCAM_MODEL_PATH; do
    case "$(env_get "$var")" in
        "$INSTALL_DIR"/*) env_del "$var" ;;
    esac
done
env_set SECCAM_UPDATE_MODE systemd
[[ -n "$PIN" ]] && env_set SECCAM_PIN "$PIN"
[[ -n "$RETENTION_DAYS" ]] && env_set SECCAM_RETENTION_DAYS "$RETENTION_DAYS"
[[ -n "$TRUSTED_PROXIES" ]] && env_set SECCAM_TRUSTED_PROXIES "$TRUSTED_PROXIES"
[[ -n "$PUBLIC_ORIGIN" ]] && env_set SECCAM_PUBLIC_ORIGIN "$PUBLIC_ORIGIN"
chown root:seccam "$ENV_FILE"
chmod 640 "$ENV_FILE"

# --- flip current, install units ---
ln -sfn "releases/$VERSION" "$INSTALL_DIR/current.new"
mv -T "$INSTALL_DIR/current.new" "$INSTALL_DIR/current"

echo "Installing systemd units..."
for unit in seccam.service seccam-update.service seccam-update.path; do
    cp "$TARGET/deploy/$unit" "$SYSTEMD_DIR/$unit"
done
systemctl daemon-reload
systemctl enable seccam.service seccam-update.path >/dev/null
systemctl restart seccam.service
systemctl start seccam-update.path

# --- health check ---
PORT="$(env_get SECCAM_HTTPS_PORT)"
PORT="${PORT:-8443}"
SCHEME=http
[[ -n "$(env_get SECCAM_CERT)" ]] && SCHEME=https
HEALTH_URL="$SCHEME://127.0.0.1:$PORT/api/health"
echo "Waiting for service to become healthy..."
HEALTHY=0
HEALTH_BODY=""
for _ in {1..30}; do
    if HEALTH_BODY="$(curl -k -s -f "$HEALTH_URL" 2>/dev/null)"; then
        HEALTHY=1
        break
    fi
    sleep 1
done
if [[ $HEALTHY -eq 0 ]]; then
    echo "ERROR: service did not become healthy within 30 seconds" >&2
    journalctl -u seccam -n 50 --no-pager >&2 || true
    exit 1
fi

# --- v1 migration cleanup (only after the new layout is healthy) ---
for old in server static models venv requirements.txt; do
    if [[ -e "$INSTALL_DIR/$old" || -L "$INSTALL_DIR/$old" ]]; then
        echo "Removing v1 file: $INSTALL_DIR/$old"
        rm -rf "${INSTALL_DIR:?}/$old"
    fi
done

PRIMARY_IP="$( { hostname -I 2>/dev/null || true; } | tr ' ' '\n' | grep -E '^[0-9.]+$' | head -1 || true)"
echo ""
echo "=== Installation Complete ==="
if [[ -n "$PUBLIC_ORIGIN" ]]; then
    echo "Open: $PUBLIC_ORIGIN/"
else
    echo "Open: $SCHEME://${PRIMARY_IP:-<server-ip>}/"
fi
if grep -Eq '"setup_required" *: *true' <<<"$HEALTH_BODY"; then
    TOKEN="$(cat "$DATA_DIR/setup-token" 2>/dev/null || true)"
    echo "First-run setup is required."
    if [[ -n "$TOKEN" ]]; then
        echo "Setup token: $TOKEN"
    else
        echo "Setup token: run  sudo -u seccam $INSTALL_DIR/current/venv/bin/python -m server.manage setup-token"
    fi
fi
echo "Data directory: $DATA_DIR"
echo "Re-run this script to change settings; updates are applied from Settings in the web UI."
