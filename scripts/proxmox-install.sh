#!/usr/bin/env bash
set -euo pipefail

# Proxmox VE one-command SecCam container installer
# Runs on Proxmox host as root. Creates an unprivileged CT, installs SecCam inside it.
# Usage: [env vars] bash scripts/proxmox-install.sh
# Env overrides: CTID, CT_HOSTNAME, CT_CORES, CT_RAM, CT_SWAP, CT_DISK, CT_BRIDGE, CT_STORAGE,
#                TEMPLATE_STORAGE, REPO_URL, REPO_REF

# Colors (only if stdout is a tty)
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    NC=''
fi

log_info() { echo -e "${BLUE}[INFO]${NC} $*"; }
log_ok() { echo -e "${GREEN}[OK]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_err() { echo -e "${RED}[ERR]${NC} $*"; }

# Trap errors and print helpful message
FAILED_STEP=""
trap 'on_error' ERR
on_error() {
    local exit_code=$?
    log_err "Installation failed at step: ${FAILED_STEP:-unknown} (exit code $exit_code)"
    if [[ -n "${CTID:-}" ]] && pct status "$CTID" &>/dev/null; then
        log_warn "Container $CTID left for inspection. You can:"
        log_warn "  pct enter $CTID          # enter container"
        log_warn "  pct destroy $CTID        # remove container"
    fi
    exit $exit_code
}

# --- Pre-flight checks ---
FAILED_STEP="pre-flight checks"
if [[ $EUID -ne 0 ]]; then
    log_err "Must run as root"
    exit 1
fi
for cmd in pct pveam pvesh pvesm; do
    if ! command -v "$cmd" &>/dev/null; then
        log_err "Command '$cmd' not found. Are you on a Proxmox VE host?"
        exit 1
    fi
done
log_ok "Running on Proxmox VE as root"

# --- Configuration with defaults ---
FAILED_STEP="configuration"
CTID="${CTID:-}"
CT_HOSTNAME="${CT_HOSTNAME:-seccam}"
CT_CORES="${CT_CORES:-1}"
CT_RAM="${CT_RAM:-1024}"
CT_SWAP="${CT_SWAP:-512}"
CT_DISK="${CT_DISK:-8}"
CT_BRIDGE="${CT_BRIDGE:-vmbr0}"
CT_STORAGE="${CT_STORAGE:-}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
REPO_URL="${REPO_URL:-https://github.com/fr3e1/seccam.git}"
REPO_REF="${REPO_REF:-main}"

# Auto-assign CTID if not provided
if [[ -z "$CTID" ]]; then
    CTID=$(pvesh get /cluster/nextid 2>/dev/null | tr -dc '0-9')
    if [[ -z "$CTID" ]]; then
        log_err "Could not determine next CTID"
        exit 1
    fi
fi
log_info "Using CTID: $CTID"

# Auto-select storage for CT rootfs if not provided
if [[ -z "$CT_STORAGE" ]]; then
    # active storages that can hold container root disks; prefer local-lvm, then local-zfs, then anything
    ROOTDIR_STORAGES=$(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active" {print $1}')
    for pref in local-lvm local-zfs; do
        if grep -qx "$pref" <<<"$ROOTDIR_STORAGES"; then CT_STORAGE="$pref"; break; fi
    done
    if [[ -z "$CT_STORAGE" ]]; then
        CT_STORAGE=$(head -1 <<<"$ROOTDIR_STORAGES")
    fi
    if [[ -z "$CT_STORAGE" ]]; then
        log_err "No suitable storage found for CT rootfs"
        exit 1
    fi
fi
log_info "Using CT storage: $CT_STORAGE"

# --- Download Debian 12 template ---
FAILED_STEP="template download"
log_info "Updating template list..."
pveam update >/dev/null
TEMPLATE=$(pveam available --section system 2>/dev/null | awk '{print $2}' \
    | grep -E '^debian-12-standard_.*\.tar\.(zst|gz|xz)$' | sort -V | tail -1 || true)
if [[ -z "$TEMPLATE" ]]; then
    log_err "No debian-12-standard template found"
    exit 1
fi
log_info "Found template: $TEMPLATE"
if ! pveam list "$TEMPLATE_STORAGE" | grep -q "$TEMPLATE"; then
    log_info "Downloading template to $TEMPLATE_STORAGE..."
    pveam download "$TEMPLATE_STORAGE" "$TEMPLATE" >/dev/null
else
    log_ok "Template already present on $TEMPLATE_STORAGE"
fi

# --- Create container ---
FAILED_STEP="container creation"
ROOT_PASS=$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)
log_info "Creating container $CTID ($CT_HOSTNAME) on $CT_STORAGE..."
pct create "$CTID" "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}" \
    --hostname "$CT_HOSTNAME" \
    --cores "$CT_CORES" \
    --memory "$CT_RAM" \
    --swap "$CT_SWAP" \
    --rootfs "${CT_STORAGE}:${CT_DISK}" \
    --net0 "name=eth0,bridge=${CT_BRIDGE},ip=dhcp" \
    --unprivileged 1 \
    --nesting 1 \
    --onboot 1 \
    --tags "seccam" \
    --password "$ROOT_PASS" \
    --start 1 \
    >/dev/null
log_ok "Container created and started"

# --- Wait for network ---
FAILED_STEP="network wait"
log_info "Waiting for container to get IPv4 address (up to 90s)..."
CT_IP=""
for i in {1..90}; do
    CT_IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -1 || true)
    if [[ -n "$CT_IP" ]]; then
        break
    fi
    sleep 1
done
if [[ -z "$CT_IP" ]]; then
    log_err "Container did not get an IPv4 address within 90s"
    exit 1
fi
log_ok "Container IP: $CT_IP"

# --- Wait for DNS/apt to work ---
FAILED_STEP="apt update"
log_info "Waiting for DNS and network (apt-get update)..."
for i in {1..30}; do
    if pct exec "$CTID" -- apt-get update -qq >/dev/null 2>&1; then
        log_ok "Network and DNS working"
        break
    fi
    sleep 2
    if [[ $i -eq 30 ]]; then
        log_err "apt-get update failed after 60s"
        exit 1
    fi
done

# --- Install git and clone repo inside CT ---
FAILED_STEP="git clone"
log_info "Installing git and ca-certificates in container..."
pct exec "$CTID" -- apt-get install -y -qq git ca-certificates >/dev/null
log_info "Cloning SecCam repository ($REPO_REF)..."
pct exec "$CTID" -- git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" /opt/seccam-src >/dev/null
log_ok "Repository cloned"

# --- Run install.sh inside CT ---
FAILED_STEP="install.sh"
log_info "Running SecCam install.sh inside container..."
pct exec "$CTID" -- bash /opt/seccam-src/deploy/install.sh >/dev/null
log_ok "SecCam installed successfully"

# --- Summary ---
echo ""
echo -e "${GREEN}=== SecCam Container Ready ===${NC}"
echo -e "Container ID:    ${BLUE}$CTID${NC}"
echo -e "Hostname:        ${BLUE}$CT_HOSTNAME${NC}"
echo -e "IP Address:      ${BLUE}$CT_IP${NC}"
echo -e "Root password:   ${YELLOW}(random, not shown)${NC}"
echo ""
echo -e "Open in browser: ${GREEN}https://$CT_IP/${NC}  (first-run setup, choose your PIN)"
echo -e "iPhone cert:     ${GREEN}http://$CT_IP/ca.crt${NC}  (install on iPhone for camera access)"
echo ""
echo -e "Manage container:"
echo -e "  pct enter $CTID          # shell inside container"
echo -e "  pct reboot $CTID         # restart container"
echo -e "  pct destroy $CTID        # remove container"
echo ""
