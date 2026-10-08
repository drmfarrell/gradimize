#!/usr/bin/env bash
# Generate a self-signed TLS certificate for Gradimize's hardened mode.
#
# A self-signed cert encrypts traffic but browsers will show a one-time
# warning until the cert (certs/gradimize.crt) is trusted. The clean fix is
# to ask campus IT for a certificate from the university's internal CA for
# this host — drop the files in as certs/gradimize.{crt,key} and nothing
# else changes.
#
# Usage: ./scripts/make_cert.sh [hostname-or-ip ...]
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p certs

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
CN="${1:-${HOST_IP:-localhost}}"

SAN="DNS:localhost,IP:127.0.0.1"
[ -n "$HOST_IP" ] && SAN="$SAN,IP:$HOST_IP"
for extra in "$@"; do
    if [[ "$extra" =~ ^[0-9.]+$ ]]; then SAN="$SAN,IP:$extra"; else SAN="$SAN,DNS:$extra"; fi
done

openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 825 \
    -keyout certs/gradimize.key -out certs/gradimize.crt \
    -subj "/CN=$CN/O=Gradimize" \
    -addext "subjectAltName=$SAN"
chmod 600 certs/gradimize.key

echo
echo "Created certs/gradimize.crt + certs/gradimize.key (SAN: $SAN)"
echo "Fingerprint (share so users can verify the browser warning is THIS cert):"
openssl x509 -in certs/gradimize.crt -noout -fingerprint -sha256
