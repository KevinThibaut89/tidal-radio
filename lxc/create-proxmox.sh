#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Tidal Radio LXC — Proxmox installer wizard
#  Community-scripts-style guided setup: Default / Advanced settings,
#  station configuration, and optional Tidal account linking.
#
#  Run from a repo checkout:   ./radio/lxc/create-proxmox.sh
#  Or as a one-liner:          bash -c "$(curl -fsSL <raw-url-of-this-script>)"
#    (one-liner mode fetches the repo tarball; override with RADIO_REPO_TARBALL)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── look & feel ──────────────────────────────────────────────────────────────
YW=$'\033[33m'; GN=$'\033[1;92m'; RD=$'\033[01;31m'; BL=$'\033[36m'
CL=$'\033[m'; BOLD=$'\033[1m'
LOG=/tmp/tidal-radio-install.log
: > "$LOG"

msg_info()  { echo -ne "\r\033[K ${YW}⏳ ${1}...${CL}"; }
msg_ok()    { echo -e "\r\033[K ${GN}✔${CL} ${1}"; }
msg_error() { echo -e "\r\033[K ${RD}✖${CL} ${1}"; }

header() {
  clear
  cat <<EOF
${BL}${BOLD}
   ______   __     __        __   ____             __ _
  /_  __/  (_)___/ /___ _  / /  / __ \____ _ ____/ /(_)___
   / /    / // _  // __ \`/ / /  / /_/ / __ \`// __  // // __ \\
  / /    / // /_/ // /_/ / / /  / _, _/ /_/ // /_/ // // /_/ /
 /_/    /_/ \__,_/ \__,_/ /_/  /_/ |_|\__,_/ \__,_//_/ \____/
${CL}
   Personal AI radio from your Tidal library — LXC installer
─────────────────────────────────────────────────────────────────
EOF
}

CT_CREATED=""
on_error() {
  local line=$1
  msg_error "Installation failed (line ${line}). Log: ${LOG}"
  tail -5 "$LOG" 2>/dev/null | sed 's/^/    /'
  if [ -n "$CT_CREATED" ]; then
    read -r -p " Remove partially created container ${CT_CREATED}? [y/N] " ans
    if [[ "${ans,,}" == y* ]]; then
      pct stop "$CT_CREATED" &>/dev/null || true
      pct destroy "$CT_CREATED" &>/dev/null || true
      msg_ok "Container ${CT_CREATED} removed"
    fi
  fi
  exit 1
}
trap 'on_error $LINENO' ERR

# ── guards ───────────────────────────────────────────────────────────────────
header
[ "$(id -u)" -eq 0 ] || { msg_error "Run as root on the Proxmox host."; exit 1; }
command -v pveversion &>/dev/null || {
  msg_error "This wizard must run on a Proxmox VE host (pveversion not found)."
  msg_error "For LXD/Incus hosts use radio/lxc/create-lxd.sh instead."
  exit 1
}
command -v whiptail &>/dev/null || { msg_error "whiptail not found."; exit 1; }

WT() { whiptail --backtitle "Tidal Radio LXC installer" --title "$1" "${@:2}" 3>&1 1>&2 2>&3; }

# ── mode selection ───────────────────────────────────────────────────────────
MODE=$(WT "Tidal Radio" --menu "\nThis wizard creates an LXC container running your personal\nAI radio station (Tidal + tempo/key DJ + AI voice).\n\nChoose setup mode:" 16 62 2 \
  "default"  "Default settings  (recommended)" \
  "advanced" "Advanced settings (pick everything)") || exit 0

# container defaults
CTID=$(pvesh get /cluster/nextid 2>>"$LOG")
HOSTNAME="tidal-radio"
DISK=12
CORES=2
RAM=2048
BRIDGE="vmbr0"
NET="ip=dhcp"
UNPRIV=1
STORAGE=$(pvesm status -content rootdir 2>>"$LOG" | awk 'NR==2 {print $1}')
TPL_STORAGE=$(pvesm status -content vztmpl 2>>"$LOG" | awk 'NR==2 {print $1}')
TPL_STORAGE=${TPL_STORAGE:-local}

if [ "$MODE" = "advanced" ]; then
  CTID=$(WT "Container ID" --inputbox "Container ID:" 10 58 "$CTID")
  HOSTNAME=$(WT "Hostname" --inputbox "Hostname:" 10 58 "$HOSTNAME")
  DISK=$(WT "Disk" --inputbox "Disk size (GB) — audio cache lives here:" 10 58 "$DISK")
  CORES=$(WT "CPU" --inputbox "CPU cores (2+ recommended, analysis is CPU-heavy):" 10 58 "$CORES")
  RAM=$(WT "Memory" --inputbox "RAM (MB):" 10 58 "$RAM")

  mapfile -t STORAGES < <(pvesm status -content rootdir | awk 'NR>1 {print $1" "$2}')
  SITEMS=(); for s in "${STORAGES[@]}"; do SITEMS+=($s "off"); done
  SITEMS[2]="on"   # radiolist triplets are (tag, item, status) — preselect first
  STORAGE=$(WT "Storage" --radiolist "Container storage:" 16 58 6 "${SITEMS[@]}")

  mapfile -t BRIDGES < <(ls /sys/class/net | grep -E '^vmbr' || echo vmbr0)
  BITEMS=(); for b in "${BRIDGES[@]}"; do BITEMS+=("$b" "" "off"); done
  BITEMS[2]="on"
  BRIDGE=$(WT "Network" --radiolist "Bridge:" 14 58 5 "${BITEMS[@]}")

  if WT "Network" --yesno "Use DHCP?\n(Choose No for a static IP)" 10 58; then
    NET="ip=dhcp"
  else
    SIP=$(WT "Static IP" --inputbox "IP address with CIDR (e.g. 192.168.1.50/24):" 10 58 "")
    SGW=$(WT "Gateway" --inputbox "Gateway (e.g. 192.168.1.1):" 10 58 "")
    NET="ip=${SIP},gw=${SGW}"
  fi

  WT "Privileges" --yesno "Create as unprivileged container? (recommended)" 10 58 \
    && UNPRIV=1 || UNPRIV=0
fi

if pct status "$CTID" &>/dev/null; then
  msg_error "Container ID ${CTID} is already in use — aborting."
  exit 1
fi

# ── station settings ─────────────────────────────────────────────────────────
HOST_TZ=$(cat /etc/timezone 2>/dev/null || echo "Europe/Brussels")
STATION_NAME=$(WT "Station" --inputbox "Station name (the DJ says this on air):" 10 58 "Kevin FM")
STATION_TZ=$(WT "Station" --inputbox "Timezone (shows + DJ clock):" 10 58 "$HOST_TZ")
WLAT=$(WT "Weather" --inputbox "Weather location — latitude:" 10 58 "50.85")
WLON=$(WT "Weather" --inputbox "Weather location — longitude:" 10 58 "4.35")
ANTHROPIC_KEY=$(WT "AI DJ (optional)" --passwordbox "\nAnthropic API key for Claude-written DJ breaks.\nLeave empty to use the built-in template DJ.\n(You can add it later in /etc/tidal-radio/secrets.env)" 13 62 "") || ANTHROPIC_KEY=""

# ── summary ──────────────────────────────────────────────────────────────────
WT "Summary" --yesno "\
Container : ${CTID} (${HOSTNAME}) — Ubuntu 24.04, unprivileged=${UNPRIV}
Resources : ${CORES} cores / ${RAM} MB RAM / ${DISK} GB on ${STORAGE}
Network   : ${BRIDGE}, ${NET}
Station   : ${STATION_NAME} (${STATION_TZ})
Weather   : ${WLAT}, ${WLON}
AI DJ     : $([ -n "$ANTHROPIC_KEY" ] && echo "Claude (key set)" || echo "template (no key)")

Create the container now?" 18 66 || exit 0

header
echo -e " ${BOLD}Installing ${STATION_NAME}${CL} (CT ${CTID})\n"

# ── template ─────────────────────────────────────────────────────────────────
msg_info "Locating Ubuntu 24.04 template"
pveam update >>"$LOG" 2>&1 || true
TEMPLATE=$(pveam available --section system 2>>"$LOG" | awk '/ubuntu-24.04-standard/ {print $2}' | sort | tail -1)
[ -n "$TEMPLATE" ] || { msg_error "No ubuntu-24.04 template available (pveam update failed?)"; exit 1; }
if ! pveam list "$TPL_STORAGE" 2>>"$LOG" | grep -q "$TEMPLATE"; then
  msg_info "Downloading template ${TEMPLATE}"
  pveam download "$TPL_STORAGE" "$TEMPLATE" >>"$LOG" 2>&1
fi
msg_ok "Template ready: ${TEMPLATE}"

# ── create container ─────────────────────────────────────────────────────────
msg_info "Creating container ${CTID}"
pct create "$CTID" "${TPL_STORAGE}:vztmpl/${TEMPLATE}" \
  --hostname "$HOSTNAME" --cores "$CORES" --memory "$RAM" \
  --rootfs "${STORAGE}:${DISK}" \
  --net0 "name=eth0,bridge=${BRIDGE},${NET}" \
  --features nesting=1 --unprivileged "$UNPRIV" \
  --onboot 1 --start 1 >>"$LOG" 2>&1
CT_CREATED="$CTID"
msg_ok "Container ${CTID} created and started"

msg_info "Waiting for network"
for _ in $(seq 1 30); do
  IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}') || IP=""
  [ -n "$IP" ] && pct exec "$CTID" -- ping -c1 -W2 deb.debian.org >>"$LOG" 2>&1 && break
  sleep 2
done
[ -n "${IP:-}" ] || { msg_error "Container has no network"; exit 1; }
msg_ok "Network up (${IP})"

# ── deliver code ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-/dev/null}")" 2>/dev/null && pwd || echo /nonexistent)"
msg_info "Copying tidal-radio code into the container"
pct exec "$CTID" -- mkdir -p /opt/tidal-radio/src
if [ -d "$SCRIPT_DIR/../app" ]; then
  tar -C "$SCRIPT_DIR/.." -cf - . | pct exec "$CTID" -- tar -C /opt/tidal-radio/src -xf -
else
  TARBALL="${RADIO_REPO_TARBALL:-https://github.com/KevinThibaut89/tidal-radio/archive/refs/heads/main.tar.gz}"
  TMPD=$(mktemp -d)
  if ! curl -fsSL "$TARBALL" -o "$TMPD/repo.tar.gz" 2>>"$LOG"; then
    msg_error "Could not download ${TARBALL}"
    msg_error "If the repo is unreachable, clone it and run this script from the checkout,"
    msg_error "or set RADIO_REPO_TARBALL to a reachable tarball URL."
    exit 1
  fi
  tar -C "$TMPD" -xzf "$TMPD/repo.tar.gz"
  # locate the code root regardless of tarball layout (repo root or radio/ subdir)
  SETUP=$(find "$TMPD" -maxdepth 4 -type f -path '*/lxc/setup.sh' | head -1)
  [ -n "$SETUP" ] || { msg_error "No lxc/setup.sh in the tarball"; exit 1; }
  RADIO_DIR=$(dirname "$(dirname "$SETUP")")
  tar -C "$RADIO_DIR" -cf - . | pct exec "$CTID" -- tar -C /opt/tidal-radio/src -xf -
  rm -rf "$TMPD"
fi
msg_ok "Code delivered"

# ── provision inside the container ──────────────────────────────────────────
msg_info "Provisioning (packages, Piper voice, services — takes a few minutes)"
pct exec "$CTID" -- bash /opt/tidal-radio/src/lxc/setup.sh >>"$LOG" 2>&1
msg_ok "Container provisioned"

msg_info "Applying station settings"
pct exec "$CTID" -- sed -i \
  -e "s|^  name: .*|  name: \"${STATION_NAME}\"|" \
  -e "s|^  timezone: .*|  timezone: \"${STATION_TZ}\"|" \
  -e "s|^  latitude: .*|  latitude: ${WLAT}|" \
  -e "s|^  longitude: .*|  longitude: ${WLON}|" \
  /etc/tidal-radio/config.yaml
if [ -n "$ANTHROPIC_KEY" ]; then
  pct exec "$CTID" -- bash -c \
    "sed -i '/^#\\?ANTHROPIC_API_KEY=/d' /etc/tidal-radio/secrets.env && \
     echo 'ANTHROPIC_API_KEY=${ANTHROPIC_KEY}' >> /etc/tidal-radio/secrets.env"
fi
msg_ok "Station configured"

# ── finish line: link Tidal, sync, go live ──────────────────────────────────
AUTH_DONE=0
if WT "Link Tidal" --yesno "\nLink your Tidal account now?\n\nA link.tidal.com URL will be printed — open it on your\nphone or laptop and approve. The wizard waits for you." 13 62; then
  echo
  # absolute path: lxc-attach's minimal PATH excludes /usr/local/bin
  if pct exec "$CTID" -- /usr/local/bin/tidal-radio auth; then
    AUTH_DONE=1
    msg_ok "Tidal account linked"
    msg_info "Syncing your favorites"
    pct exec "$CTID" -- /usr/local/bin/tidal-radio sync >>"$LOG" 2>&1 || true
    msg_ok "Library synced"
  else
    msg_error "Tidal linking failed or was cancelled — run 'tidal-radio auth' later"
  fi
fi

msg_info "Starting services"
pct exec "$CTID" -- systemctl enable --now icecast2 liquidsoap-radio >>"$LOG" 2>&1
if [ "$AUTH_DONE" = 1 ]; then
  pct exec "$CTID" -- systemctl enable --now tidal-radio >>"$LOG" 2>&1
  msg_ok "Station is on the air"
else
  pct exec "$CTID" -- systemctl enable icecast2 liquidsoap-radio tidal-radio >>"$LOG" 2>&1 || true
  msg_ok "Services enabled (station starts once Tidal is linked)"
fi

# ── done ─────────────────────────────────────────────────────────────────────
echo -e "
─────────────────────────────────────────────────────────────────
 ${GN}${BOLD}✔ ${STATION_NAME} is installed${CL}   (container ${CTID}, ${IP})

   ${BOLD}Stream${CL}   http://${IP}:8000/radio.mp3
   ${BOLD}Control${CL}  http://${IP}:8080/
"
if [ "$AUTH_DONE" != 1 ]; then
  echo -e "   ${YW}Still to do inside the container (pct enter ${CTID}):${CL}
     tidal-radio auth && tidal-radio sync
     systemctl start tidal-radio
"
fi
echo -e "   Config: /etc/tidal-radio/config.yaml · Logs: journalctl -u tidal-radio -f
─────────────────────────────────────────────────────────────────"
