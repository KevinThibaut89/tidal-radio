# Tidal Radio — personal AI radio station in an LXC container

A self-hosted radio station that plays **your own Tidal library**, sequenced like a
real DJ set (tempo + harmonic key mixing), organized into **thematic shows based on
your taste**, with an **AI voice DJ** that talks between tracks — station IDs,
weather, and news headlines — so it feels like a real station.

The whole thing runs headless inside one LXC container and exposes:

- an **audio stream**: `http://<container-ip>:8000/radio.mp3` (play in VLC, Sonos, browser, car…)
- a **control API + mini player**: `http://<container-ip>:8080/`

> ⚠️ Personal use only. You need an **active Tidal subscription**; audio is fetched
> through your own account for playback and kept only in a bounded, transient cache
> (like any player's buffer). Don't redistribute the stream outside your household.

## Architecture

```
┌────────────────────────────── LXC container ──────────────────────────────┐
│                                                                           │
│  tidal-radio (Python "brain")                                             │
│   ├─ tidal_client   OAuth link to your Tidal account, library sync,       │
│   │                 fetches audio into a bounded cache                    │
│   ├─ analysis       librosa: BPM + musical key (→ Camelot) per track      │
│   ├─ engine         picks the next track: tempo walk + harmonic mixing    │
│   │                 + taste weighting + no-repeat rules                   │
│   ├─ shows          thematic shows (config + auto-generated from taste)   │
│   ├─ dj             writes DJ breaks (Claude API or templates)            │
│   │                 + weather (Open-Meteo) + news (RSS)                   │
│   ├─ tts            Piper local voice (offline, free)                     │
│   └─ api            FastAPI: status / skip / shows / mini web player      │
│            │ pushes files + voice breaks via telnet                       │
│            ▼                                                              │
│  liquidsoap  ── crossfades, gapless queue ──►  icecast2 ──► radio.mp3     │
└───────────────────────────────────────────────────────────────────────────┘
```

## Quick start (Proxmox — installer wizard)

Run the wizard as root on your Proxmox host, community-scripts style:

```bash
# one-liner (fetches the code from the public mirror repo):
bash -c "$(curl -fsSL https://raw.githubusercontent.com/KevinThibaut89/tidal-radio/main/lxc/create-proxmox.sh)"

# or from a checkout of this repo on the PVE host:
./lxc/create-proxmox.sh
```


The wizard walks you through everything:

1. **Default settings** (next free CTID, 2 cores / 2 GB RAM / 12 GB disk, vmbr0
   DHCP, unprivileged) or **Advanced settings** (pick CTID, resources, storage,
   bridge, static IP, privilege level).
2. **Station settings** — station name, timezone, weather location, and an
   optional AI DJ key (skippable — you can paste one in the control UI later,
   and the template DJ is used until you do).
3. Creates + provisions the container (Ubuntu 24.04 template, icecast2,
   liquidsoap, ffmpeg, Python deps, Piper voice, systemd services).
4. Offers to **link your Tidal account right there** (you can also do this from
   the control UI afterwards) — it prints a
   link.tidal.com URL, you approve on your phone, it syncs your favorites and
   puts the station on the air.

At the end it prints your URLs:

```
Stream   http://<container-ip>:8000/radio.mp3
Control  http://<container-ip>:8080/
```

If you skipped the Tidal step, finish later inside the container
(`pct enter <CTID>`): `tidal-radio auth && tidal-radio sync`, then
`systemctl start tidal-radio`.

### Audio quality (and why lossless needs a different login)

Tidal's device-link login — the convenient "open this URL and approve" flow —
issues a token that is **only authorised to stream up to `HIGH` (320 kbps)**.
Requesting `LOSSLESS` with it makes the stream endpoints return `401`, so the
station falls back down the quality ladder automatically, logs the downgrade
once, and keeps playing.

For genuine lossless, re-link using the browser redirect (PKCE) flow, which is
the only login Tidal authorises for it — it needs a terminal because you paste
the redirect URL back:

```bash
tidal-radio auth --pkce            # then set quality: LOSSLESS in config.yaml
systemctl restart tidal-radio
```

### When tracks won't play

```bash
tidal-radio diagnose        # one request per check — safe to run when rate-limited
```

It reports the session state, your account's country and subscription tier, the
active quality, and a single stream attempt with the exact HTTP status.

- **401** — Tidal refuses playback for this login. Try `tidal-radio auth --pkce`;
  if that still fails, the subscription or its region likely doesn't cover
  streaming for the account (`country code` in the output is the one Tidal uses).
- **429** — rate limited. The station backs off automatically (60s, doubling up
  to 30 min) and the analysis worker pauses too. Just wait; don't restart in a
  loop, which is what earns the limit in the first place.

### Other hosts

- **LXD/Incus**: `./lxc/create-lxd.sh tidal-radio` (simple non-wizard script).
- **Manual**: any Ubuntu 24.04 container — copy `radio/` to `/opt/tidal-radio/src`,
  run `bash /opt/tidal-radio/src/lxc/setup.sh`, then `tidal-radio auth`,
  `tidal-radio sync`, and start the three services.

### Updating an existing container

```bash
pct exec <CTID> -- bash /opt/tidal-radio/src/lxc/update.sh
```

Pulls the latest published code, reinstalls dependencies only if they changed,
and restarts the services. Your config, secrets, Tidal session, database and
audio cache live outside the code directory and are left untouched.

### Useful afterwards

```bash
nano /etc/tidal-radio/config.yaml       # shows, DJ persona, news feeds, engine tuning
tidal-radio analyze --limit 50          # batch BPM/key analysis (also runs in background)
tidal-radio shows generate              # build thematic shows from your taste
```

Analysis is incremental — the radio starts immediately; unanalyzed tracks are
sequenced with less precision until the background worker catches up.

## CLI

| Command | What it does |
|---|---|
| `tidal-radio auth` | Link Tidal account (device flow) |
| `tidal-radio sync` | Sync favorites/playlists into local DB |
| `tidal-radio analyze [--limit N]` | Analyze tempo/key of unanalyzed tracks |
| `tidal-radio shows generate` | Auto-generate thematic shows from your taste |
| `tidal-radio run` | Run the station (what the systemd service runs) |
| `tidal-radio status` | Print now-playing + queue |
| `tidal-radio diagnose` | Check what Tidal allows: session, subscription, region, one stream test |

## Control UI

`http://<container-ip>:8080/` is the all-in-one panel: player, now playing,
controls, **and setup**. Nothing else is needed to get the station running —

- **Link your Tidal account** — click *Link Tidal account*, open the
  link.tidal.com URL it shows, approve it, and the page picks up the result on
  its own and starts syncing your favorites. No shell needed.
- **AI DJ keys** — paste an **Anthropic** or **OpenAI** API key under ⚙ Setup
  and pick a provider (`auto` uses whichever key is set). Keys are saved to
  `/var/lib/tidal-radio/settings.json` (owner-only, `0600`) and take effect on
  the next DJ break — no restart. Leave both empty to use the template DJ.
  A key in `secrets.env` still works and is shown as "set via secrets.env".
- The setup card appears automatically until Tidal is linked, and is always
  reachable with the ⚙ Setup button.

### Protecting the control UI

The UI ships **unprotected** so you can reach it straight after install — which
also means anyone on your network can, until you set a password. Do it early:
**Settings → Setup → Password**. Notes:

- The first password can be set by anyone who reaches the page, so set it as
  soon as the station is up.
- Sessions live in memory only, so restarting the service signs everyone out.
- It is plain HTTP on your LAN — fine at home, not something to expose to the
  internet. For remote access use an SSH tunnel or a VPN rather than a port
  forward, or set `api.host: 127.0.0.1`.

| Endpoint | Description |
|---|---|
| `GET /` | Control UI (player, status, setup) |
| `GET /status` | JSON: now playing, show, queue, DJ provider, library size |
| `POST /skip` | Skip current track |
| `POST /break` | Trigger a DJ break after the current track |
| `GET /shows` · `POST /shows/{id}/start` | List shows / force-start one |
| `POST /tidal/link` · `GET /tidal/status` | Start device linking / poll it |
| `POST /tidal/sync` | Sync favorites in the background |
| `GET /settings` · `POST /settings` | Read (keys masked) / set DJ provider + keys |
| `GET /config` · `PATCH /config` | Every editable setting: schema, values, live updates |
| `GET /discovery` · `POST /discovery/run` | Discovery pool stats / find more now |
| `GET /voices` · `POST /voices/preview` | Installed Piper voices / audition one |
| `POST /auth/login` · `POST /auth/password` | Sign in / set-change-remove the password |
| `POST /spotify/link` · `POST /spotify/link/code` | Spotify OAuth: get URL / submit pasted code |

## How the radio thinks

**Track sequencing** — every candidate is scored against the current track:
tempo proximity (a configurable BPM walk, so energy moves gradually), harmonic
compatibility on the **Camelot wheel** (same key, ±1 hour, or relative major/minor
score highest), taste weight (favorites + play history), and a repeat penalty
(no artist within N tracks, no track within M hours). A little randomness keeps
it from being deterministic.

**Thematic shows** — defined in `config.yaml` with time windows and constraints
(BPM range, energy curve, seed artists/genres). `tidal-radio shows generate`
also clusters your favorites (artist affinity × tempo bands × your listening
habits) into a weekly lineup — with Claude naming the shows if an API key is
set ("Low-End Theory Tuesdays"), or sensible generated names otherwise.

**The DJ** — every N tracks (and at the top of the hour) the brain writes a short
break: back/forward announcements, time, station ID, weather from Open-Meteo, and
headlines from your RSS feeds. With an **Anthropic** or **OpenAI** key set (in
the control UI, or `secrets.env`) the script is written by that model in your
configured DJ persona; otherwise a template engine is used.
The script is voiced with Piper (fully local, no cloud dependency) and slotted
between tracks with a crossfade.

## Files & paths inside the container

| Path | Purpose |
|---|---|
| `/opt/tidal-radio/src` | this directory (code) |
| `/opt/tidal-radio/venv` | Python virtualenv |
| `/etc/tidal-radio/config.yaml` | main config |
| `/etc/tidal-radio/secrets.env` | API keys + icecast password (systemd EnvironmentFile) |
| `/var/lib/tidal-radio/tidal-session.json` | persisted Tidal OAuth session |
| `/var/lib/tidal-radio/radio.db` | SQLite: tracks, features, history |
| `/var/lib/tidal-radio/cache/` | bounded audio cache (LRU-evicted) |
| `/var/lib/tidal-radio/voices/` | Piper voice models |
| `/var/lib/tidal-radio/breaks/` | generated DJ break audio (transient) |

## Troubleshooting

- `journalctl -u tidal-radio -f` — the brain's logs (auth, selection, DJ breaks)
- `journalctl -u liquidsoap-radio -f` — audio pipeline
- No sound but stream connects → check the brain queued tracks (`tidal-radio status`)
  and that `tidal-radio auth` has been completed.
- Analysis is CPU-heavy (librosa). Give the container 2+ cores or lower
  `analysis.max_seconds` in config.
