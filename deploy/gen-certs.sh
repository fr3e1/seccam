#!/usr/bin/env bash
set -euo pipefail

# Generate local CA and server certificate for SecCam
# Usage: gen-certs.sh <output_dir>
# Creates: ca.key, ca.crt, server.key, server.crt, server.san

DIR="${1:-}"
if [[ -z "$DIR" ]]; then
    echo "Usage: $0 <output_dir>" >&2
    exit 1
fi

mkdir -p "$DIR"
chmod 750 "$DIR"

HOSTNAME=$(hostname)
mapfile -t IPS < <( { hostname -I 2>/dev/null || ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1; } | tr " " "\n" | grep -E "^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$" )
SAN_ENTRIES=("DNS:$HOSTNAME" "DNS:$HOSTNAME.local" "DNS:localhost" "IP:127.0.0.1")
for ip in "${IPS[@]}"; do
    SAN_ENTRIES+=("IP:$ip")
done
SAN_STRING=$(IFS=,; echo "${SAN_ENTRIES[*]}")

# Check if we need to regenerate server cert
REGEN_SERVER=0
if [[ ! -f "$DIR/server.crt" ]] || [[ ! -f "$DIR/server.key" ]]; then
    REGEN_SERVER=1
    echo "Server certificate missing, will generate"
else
    # Check expiry (within 30 days)
    if ! openssl x509 -checkend $((30*86400)) -noout -in "$DIR/server.crt" >/dev/null 2>&1; then
        REGEN_SERVER=1
        echo "Server certificate expiring within 30 days, will regenerate"
    elif [[ -f "$DIR/server.san" ]]; then
        OLD_SAN=$(cat "$DIR/server.san")
        if [[ "$OLD_SAN" != "$SAN_STRING" ]]; then
            REGEN_SERVER=1
            echo "SAN changed (was: $OLD_SAN), will regenerate server certificate"
        fi
    else
        REGEN_SERVER=1
        echo "No SAN record found, will regenerate server certificate"
    fi
fi

# Generate CA if missing or expiring within 30 days
REGEN_CA=0
if [[ ! -f "$DIR/ca.crt" ]] || [[ ! -f "$DIR/ca.key" ]]; then
    REGEN_CA=1
    echo "CA certificate missing, will generate"
else
    if ! openssl x509 -checkend $((30*86400)) -noout -in "$DIR/ca.crt" >/dev/null 2>&1; then
        REGEN_CA=1
        echo "CA certificate expiring within 30 days, will regenerate"
    else
        echo "CA certificate exists and is valid, keeping existing"
    fi
fi

if [[ $REGEN_CA -eq 1 ]]; then
    echo "Generating CA certificate..."
    openssl ecparam -name prime256v1 -genkey -noout -out "$DIR/ca.key"
    chmod 600 "$DIR/ca.key"
    chown root:root "$DIR/ca.key"

    NC_ENTRIES="permitted;DNS:$HOSTNAME,permitted;DNS:$HOSTNAME.local,permitted;DNS:localhost,permitted;IP:127.0.0.1/255.255.255.255"
    for ip in "${IPS[@]}"; do
        NC_ENTRIES+=",permitted;IP:$ip/255.255.255.255"
    done

    openssl req -x509 -new -nodes -key "$DIR/ca.key" -sha256 -days 3650 \
        -subj "/CN=SecCam Local CA" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -addext "nameConstraints=critical,$NC_ENTRIES" \
        -addext "subjectKeyIdentifier=hash" \
        -out "$DIR/ca.crt"
    chmod 644 "$DIR/ca.crt"
    chown root:seccam "$DIR/ca.crt" 2>/dev/null || true
    echo "CA certificate generated: $DIR/ca.crt"
fi

# Generate server certificate if needed
if [[ $REGEN_SERVER -eq 1 ]]; then
    echo "Generating server certificate..."
    openssl ecparam -name prime256v1 -genkey -noout -out "$DIR/server.key"
    chmod 640 "$DIR/server.key"
    chown root:seccam "$DIR/server.key" 2>/dev/null || true

    # Create CSR with SAN
    cat > "$DIR/server.csr.cnf" <<EOF
[req]
default_bits = 256
prompt = no
default_md = sha256
distinguished_name = dn
req_extensions = req_ext

[dn]
CN = $HOSTNAME

[req_ext]
subjectAltName = @alt_names

[alt_names]
EOF
    i=1
    for entry in "${SAN_ENTRIES[@]}"; do
        type=${entry%%:*}
        value=${entry#*:}
        if [[ "$type" == "DNS" ]]; then
            echo "DNS.$i = $value" >> "$DIR/server.csr.cnf"
        elif [[ "$type" == "IP" ]]; then
            echo "IP.$i = $value" >> "$DIR/server.csr.cnf"
        fi
        ((i++))
    done

    openssl req -new -key "$DIR/server.key" -out "$DIR/server.csr" -config "$DIR/server.csr.cnf"

    # Create extfile for signing
    cat > "$DIR/server.ext" <<EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyAgreement
extendedKeyUsage=serverAuth
subjectAltName=@alt_names

[alt_names]
EOF
    i=1
    for entry in "${SAN_ENTRIES[@]}"; do
        type=${entry%%:*}
        value=${entry#*:}
        if [[ "$type" == "DNS" ]]; then
            echo "DNS.$i = $value" >> "$DIR/server.ext"
        elif [[ "$type" == "IP" ]]; then
            echo "IP.$i = $value" >> "$DIR/server.ext"
        fi
        ((i++))
    done

    openssl x509 -req -in "$DIR/server.csr" -CA "$DIR/ca.crt" -CAkey "$DIR/ca.key" \
        -CAcreateserial -out "$DIR/server.crt" -days 397 -sha256 \
        -extfile "$DIR/server.ext"

    chmod 644 "$DIR/server.crt"
    chown root:seccam "$DIR/server.crt" 2>/dev/null || true

    # Store SAN for future comparison
    echo "$SAN_STRING" > "$DIR/server.san"
    chmod 644 "$DIR/server.san"
    chown root:seccam "$DIR/server.san" 2>/dev/null || true

    # Cleanup
    rm -f "$DIR/server.csr" "$DIR/server.csr.cnf" "$DIR/server.ext"

    echo "Server certificate generated: $DIR/server.crt"
else
    echo "Server certificate is up to date"
fi

# Set permissions on keys
# The CA key is root-only (never readable by the service user); the server key is readable by the service.
chmod 600 "$DIR/ca.key"
chown root:root "$DIR/ca.key"
chmod 640 "$DIR/server.key" 2>/dev/null || true
chown root:seccam "$DIR/server.key" 2>/dev/null || true

echo "Certificate generation complete"
echo "CA:      $DIR/ca.crt"
echo "Server:  $DIR/server.crt"
echo "Key:     $DIR/server.key"
