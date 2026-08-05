#!/usr/bin/env bash
# Update an existing tidal-radio container to the latest published code.
#
# Inside the container:   bash /opt/tidal-radio/src/lxc/update.sh
# From the Proxmox host:  pct exec <CTID> -- bash /opt/tidal-radio/src/lxc/update.sh
#
# Config, secrets, the Tidal session, the database and the audio cache all live
# outside the code directory, so they survive the update untouched.
set -euo pipefail

SRC=/opt/tidal-radio/src
VENV=/opt/tidal-radio/venv
TARBALL="${RADIO_REPO_TARBALL:-https://github.com/KevinThibaut89/tidal-radio/archive/refs/heads/main.tar.gz}"

echo "==> Fetching latest code"
TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT
curl -fsSL "$TARBALL" -o "$TMPD/repo.tar.gz"
tar -C "$TMPD" -xzf "$TMPD/repo.tar.gz"
NEW=$(dirname "$(dirname "$(find "$TMPD" -maxdepth 4 -type f -path '*/lxc/setup.sh' | head -1)")")
[ -d "$NEW/app" ] || { echo "ERROR: no app/ directory in the downloaded code" >&2; exit 1; }

echo "==> Replacing code in $SRC"
OLD_REQ=$(sha256sum "$SRC/requirements.txt" 2>/dev/null | cut -d' ' -f1 || echo none)
rm -rf "$SRC.old"
cp -a "$SRC" "$SRC.old"
cp -a "$NEW"/. "$SRC"/
find "$SRC/app" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

NEW_REQ=$(sha256sum "$SRC/requirements.txt" | cut -d' ' -f1)
if [ "$OLD_REQ" != "$NEW_REQ" ]; then
  echo "==> Dependencies changed — updating virtualenv"
  "$VENV/bin/pip" install -q -r "$SRC/requirements.txt"
fi

echo "==> Refreshing CLI + services"
ln -sf /usr/local/bin/tidal-radio /usr/bin/tidal-radio
cp "$SRC/lxc/systemd/liquidsoap-radio.service" /etc/systemd/system/
cp "$SRC/lxc/systemd/tidal-radio.service" /etc/systemd/system/
systemctl daemon-reload
/usr/local/bin/tidal-radio --help >/dev/null || {
  echo "ERROR: updated CLI is not runnable — previous code kept at $SRC.old" >&2; exit 1; }

echo "==> Restarting"
systemctl restart liquidsoap-radio || true
systemctl restart tidal-radio || true
rm -rf "$SRC.old"

echo
echo "Updated. Check it came back up:"
echo "  systemctl status tidal-radio --no-pager | head -5"
echo "  journalctl -u tidal-radio -n 20 --no-pager"
