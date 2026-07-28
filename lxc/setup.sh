#!/usr/bin/env bash
# Provision the tidal-radio stack inside an Ubuntu 24.04 container.
# Idempotent: safe to re-run after pulling code updates.
set -euo pipefail

SRC=/opt/tidal-radio/src
VENV=/opt/tidal-radio/venv
DATA=/var/lib/tidal-radio
ETC=/etc/tidal-radio

export DEBIAN_FRONTEND=noninteractive

echo "==> Installing system packages"
apt-get update
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-dev build-essential \
  icecast2 liquidsoap ffmpeg curl ca-certificates libsndfile1

echo "==> Creating radio user + directories"
id -u radio &>/dev/null || useradd --system --home "$DATA" --shell /usr/sbin/nologin radio
mkdir -p "$DATA"/{cache,voices,breaks} "$ETC"
chown -R radio:radio "$DATA"

echo "==> Python virtualenv"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip wheel >/dev/null
"$VENV/bin/pip" install -r "$SRC/requirements.txt"

# CLI shim
cat > /usr/local/bin/tidal-radio <<EOF
#!/usr/bin/env bash
set -a; [ -f $ETC/secrets.env ] && . $ETC/secrets.env; set +a
exec $VENV/bin/python -m app "\$@"
EOF
chmod +x /usr/local/bin/tidal-radio
# make `python -m app` importable from the src dir
echo "$SRC" > "$($VENV/bin/python -c 'import site;print(site.getsitepackages()[0])')/tidal-radio.pth"

echo "==> Config + secrets"
[ -f "$ETC/config.yaml" ] || cp "$SRC/config.example.yaml" "$ETC/config.yaml"
if [ ! -f "$ETC/secrets.env" ]; then
  ICECAST_PASS=$(tr -dc 'a-zA-Z0-9' </dev/urandom | head -c 24)
  cat > "$ETC/secrets.env" <<EOF
# Sourced by the systemd services and the tidal-radio CLI.
ICECAST_PASS=$ICECAST_PASS
# Optional — AI DJ script writing (template fallback used when unset):
#ANTHROPIC_API_KEY=
EOF
  chmod 640 "$ETC/secrets.env"; chgrp radio "$ETC/secrets.env"
fi
# shellcheck disable=SC1091
. "$ETC/secrets.env"

echo "==> Icecast"
sed -i \
  -e "s|<source-password>.*</source-password>|<source-password>${ICECAST_PASS}</source-password>|" \
  -e "s|<admin-password>.*</admin-password>|<admin-password>${ICECAST_PASS}</admin-password>|" \
  -e "s|<relay-password>.*</relay-password>|<relay-password>${ICECAST_PASS}</relay-password>|" \
  /etc/icecast2/icecast.xml
# listen on all interfaces so the LAN can tune in
sed -i 's|<bind-address>127.0.0.1</bind-address>|<bind-address>0.0.0.0</bind-address>|' /etc/icecast2/icecast.xml || true
sed -i 's/ENABLE=false/ENABLE=true/' /etc/default/icecast2 2>/dev/null || true

echo "==> Piper voice (local TTS)"
VOICE="$DATA/voices/en_US-lessac-medium.onnx"
if [ ! -f "$VOICE" ]; then
  BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium"
  curl -fL -o "$VOICE" "$BASE/en_US-lessac-medium.onnx" \
    && curl -fL -o "$VOICE.json" "$BASE/en_US-lessac-medium.onnx.json" \
    || echo "WARN: Piper voice download failed — re-run setup with network access"
  chown radio:radio "$VOICE"* 2>/dev/null || true
fi

echo "==> systemd services"
cp "$SRC/lxc/systemd/liquidsoap-radio.service" /etc/systemd/system/
cp "$SRC/lxc/systemd/tidal-radio.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable icecast2 liquidsoap-radio tidal-radio >/dev/null

cat <<'EOF'

Provisioning complete.

  1. tidal-radio auth        # link your Tidal account (prints a link.tidal.com URL)
  2. tidal-radio sync        # import your favorites
  3. systemctl start icecast2 liquidsoap-radio tidal-radio

Config:  /etc/tidal-radio/config.yaml
Secrets: /etc/tidal-radio/secrets.env   (add ANTHROPIC_API_KEY for the AI DJ)
EOF
