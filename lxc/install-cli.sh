#!/usr/bin/env bash
# Install the `tidal-radio` CLI shim. Shared by setup.sh and update.sh.
#
# The shim drops to the `radio` service user when invoked as root, so state it
# creates (Tidal session, database, audio cache) stays owned by the user the
# systemd service runs as — otherwise `tidal-radio auth` as root writes a
# session the service cannot read, and the service crash-loops.
set -euo pipefail

VENV=/opt/tidal-radio/venv
ETC=/etc/tidal-radio
DATA=/var/lib/tidal-radio

cat > /usr/local/bin/tidal-radio <<EOF
#!/usr/bin/env bash
set -a; [ -f $ETC/secrets.env ] && . $ETC/secrets.env 2>/dev/null; set +a
if [ "\$(id -u)" -eq 0 ]; then
  exec runuser -u radio -- $VENV/bin/python -m app "\$@"
fi
exec $VENV/bin/python -m app "\$@"
EOF
chmod +x /usr/local/bin/tidal-radio

# /usr/local/bin is missing from the PATH that `pct exec` / `pct enter` provide.
ln -sf /usr/local/bin/tidal-radio /usr/bin/tidal-radio

# Repair ownership of anything a previous root-run CLI left behind.
[ -d "$DATA" ] && chown -R radio:radio "$DATA"
