#!/usr/bin/env bash
# Create the tidal-radio container on an LXD or Incus host.
# Usage: ./create-lxd.sh [name]
set -euo pipefail

NAME="${1:-tidal-radio}"
LXC_BIN="$(command -v incus || command -v lxc)"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

"$LXC_BIN" launch ubuntu:24.04 "$NAME" \
  -c limits.cpu=2 \
  -c limits.memory=2GiB

echo "Waiting for network..."
sleep 8

"$LXC_BIN" exec "$NAME" -- mkdir -p /opt/tidal-radio/src
tar -C "$REPO_DIR" -cf - . | "$LXC_BIN" exec "$NAME" -- tar -C /opt/tidal-radio/src -xf -
"$LXC_BIN" exec "$NAME" -- bash /opt/tidal-radio/src/lxc/setup.sh

IP=$("$LXC_BIN" exec "$NAME" -- hostname -I | awk '{print $1}')
cat <<EOF

Done. Next steps ($LXC_BIN exec $NAME -- bash):
  1. tidal-radio auth                # link your Tidal account
  2. tidal-radio sync
  3. systemctl start icecast2 liquidsoap-radio tidal-radio

Stream:  http://${IP}:8000/radio.mp3
Control: http://${IP}:8080/
EOF
