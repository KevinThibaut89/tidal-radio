"""Spotify source for the station, built on librespot-python.

Mirrors TidalClient's surface so both can sit behind one MusicSource protocol:

    fetch_track(track_id: str) -> Path | None
    sync_favorites(db: Database) -> int
    ensure_login() / is_linked() / throttled_for() / cache_usage_gb()

Audio: librespot decrypts Spotify's OGG Vorbis 320 (VERY_HIGH) CDN stream and
hands us a plain seekable byte stream, *faster than real time*. We write it to
a temp .ogg and transcode to FLAC with ffmpeg, exactly like TidalClient does,
so liquidsoap and librosa see one uniform format.

IMPORTANT (deployment): librespot's generated *_pb2.py are protoc-3.20 era and
raise "Descriptors cannot be created directly" under protobuf >= 4. The station
also needs protobuf >= 4.25.8 for onnxruntime (piper-tts). Both coexist only if
the process runs with PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python — set it in
the systemd unit and in the auth CLI. We assert it here so the failure mode is
a clear message instead of an import traceback.
"""
import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import requests

from .config import Config
from .db import Database

log = logging.getLogger(__name__)

SPOTIFY_API = "https://api.spotify.com/v1"
OGG_CHUNK = 1 << 16


class SpotifyClient:
    """Owns the librespot session, library sync, and shares the audio cache."""

    # Highest → lowest. Spotify Connect (and therefore librespot) is *not*
    # served the Lossless/FLAC tier as of 2026 — the picker silently falls back
    # to whatever Vorbis file exists, so this ladder is short by design.
    QUALITY_LADDER = ["VERY_HIGH", "HIGH", "NORMAL"]

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = None
        self._session_lock = threading.RLock()   # librespot session is not thread-safe
        self._link: dict | None = None
        self._link_lock = threading.Lock()
        wanted = str(cfg.get("spotify.quality", "VERY_HIGH")).upper()
        self.quality = wanted if wanted in self.QUALITY_LADDER else "VERY_HIGH"
        self._backoff_until = 0.0
        self._backoff_step = 0
        self._track_cooldown: dict[str, float] = {}
        self.last_error: str | None = None

    # ── paths ─────────────────────────────────────────────────────────────
    @property
    def credentials_path(self) -> Path:
        return self.cfg.data_dir / "spotify-credentials.json"

    def cached_path(self, track_id: str) -> Path:
        # Namespaced so it cannot collide with Tidal's numeric ids, and so the
        # shared LRU evictor (one *.flac budget) sees both sources.
        return self.cfg.cache_dir / f"sp_{self._base62(track_id)}.flac"

    @staticmethod
    def _base62(track_id: str) -> str:
        """Accept '4uLU…', 'spotify:track:4uLU…', 'spotify:4uLU…' (our db key)."""
        return track_id.rsplit(":", 1)[-1]

    # ── auth ──────────────────────────────────────────────────────────────
    def _conf(self):
        from librespot.core import Session
        return (Session.Configuration.Builder()
                .set_store_credentials(True)
                .set_stored_credential_file(str(self.credentials_path))
                .set_cache_enabled(False)        # we keep our own FLAC cache
                .build())

    def _guard_protobuf(self):
        if os.environ.get("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION") != "python":
            raise RuntimeError(
                "Set PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python — librespot's "
                "protobuf stubs cannot load under the C++ backend of protobuf>=4")

    def login_interactive(self) -> bool:
        """OAuth (PKCE) login. Headless-friendly: librespot-python listens on
        127.0.0.1:5588 *inside the container*, so run this over
        `ssh -L 5588:127.0.0.1:5588 <container>` and open the printed URL in
        the browser on your laptop. Credentials are then reusable forever
        (until revoked) from spotify-credentials.json."""
        self._guard_protobuf()
        from librespot.core import Session
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            builder = Session.Builder(self._conf())
            if self.credentials_path.exists():
                builder.stored_file(str(self.credentials_path))
            else:
                builder.oauth(lambda url: print(f"\nOpen in your browser:\n{url}\n"))
            with self._session_lock:
                self.session = builder.create()
            log.info("Spotify linked (user %s)", self.session.username())
            return True
        except Exception as e:
            log.error("Spotify login failed: %s", e)
            return False

    def ensure_login(self) -> bool:
        """Non-interactive: reuse stored credentials. Never prompts."""
        with self._session_lock:
            if self.session is not None and self.session.is_valid():
                return True
            if not self.credentials_path.exists():
                log.error("No Spotify credentials — run `tidal-radio spotify-auth`")
                return False
            self._guard_protobuf()
            from librespot.core import Session
            try:
                self.session = (Session.Builder(self._conf())
                                .stored_file(str(self.credentials_path))
                                .create())
                return True
            except Exception as e:
                self.last_error = f"Spotify login failed: {e}"
                log.error(self.last_error)
                self.session = None
                return False

    def is_linked(self) -> bool:
        return self.credentials_path.exists()

    # ── request pacing (same shape as TidalClient) ────────────────────────
    def throttled_for(self) -> float:
        return max(0.0, self._backoff_until - time.time())

    def _note_failure(self, track_id: str, rate_limited: bool = False):
        now = time.time()
        if rate_limited:
            self._backoff_step = min(self._backoff_step + 1, 6)
            wait = min(60 * (2 ** (self._backoff_step - 1)), 1800)
            self._backoff_until = now + wait
            self.last_error = f"Spotify rate-limited us — pausing {int(wait)}s"
            log.warning(self.last_error)
        else:
            # Region-restricted / unplayable / audio-key refusal: park the track.
            self._track_cooldown[self._base62(track_id)] = now + 900

    def _note_success(self):
        self._backoff_step = 0
        self._backoff_until = 0.0
        self.last_error = None

    # ── audio ─────────────────────────────────────────────────────────────
    def fetch_track(self, track_id: str) -> Path | None:
        """Return a local FLAC for the track, downloading it if needed.
        None on failure (caller skips the track) — same contract as Tidal."""
        out = self.cached_path(track_id)
        if out.exists():
            out.touch()                       # bump LRU
            return out
        if self.throttled_for() > 0:
            return None
        if self._track_cooldown.get(self._base62(track_id), 0) > time.time():
            return None
        if not self.ensure_login():
            return None
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)

        tmp_path: Path | None = None
        try:
            from librespot.audio.decoders import AudioQuality, VorbisOnlyAudioQuality
            from librespot.metadata import TrackId

            tid = TrackId.from_base62(self._base62(track_id))
            picker = VorbisOnlyAudioQuality(getattr(AudioQuality, self.quality))
            with self._session_lock:
                if not self.session.is_valid():
                    self.session.reconnect()
                loaded = self.session.content_feeder().load(tid, picker, False, None)
                stream = loaded.input_stream.stream()   # 0xA7 Spotify header already skipped

            with tempfile.NamedTemporaryFile(dir=self.cfg.cache_dir, suffix=".ogg",
                                             delete=False) as tmp:
                while True:
                    chunk = stream.read(OGG_CHUNK)
                    if not chunk:
                        break
                    tmp.write(chunk)
                tmp_path = Path(tmp.name)
            stream.close()
            if tmp_path.stat().st_size < 4096:
                raise IOError("stream produced no audio")

            proc = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp_path),
                 "-vn", "-c:a", "flac", str(out)],
                capture_output=True, text=True,
            )
            tmp_path.unlink(missing_ok=True)
            if proc.returncode != 0 or not out.exists():
                log.error("ffmpeg failed for spotify:%s: %s", track_id, proc.stderr[-400:])
                out.unlink(missing_ok=True)
                return None
            self._evict_cache()
            self._note_success()
            return out
        except Exception as e:
            if tmp_path:
                tmp_path.unlink(missing_ok=True)
            msg = str(e).lower()
            rate_limited = "429" in msg or "rate" in msg
            log.error("Spotify fetch failed for %s: %s", track_id, e)
            self._note_failure(track_id, rate_limited)
            # An audio-key failure usually means the session went stale; drop it
            # so the next call rebuilds from stored credentials.
            if "audio key" in msg or "connection" in msg:
                with self._session_lock:
                    self.session = None
            return None

    # ── library sync ──────────────────────────────────────────────────────
    def _web_token(self, scope: str = "user-library-read") -> str:
        """Web API token minted through librespot's Login5 — no Spotify developer
        app registration needed. If this ever stops working, swap in a normal
        registered-app Authorization-Code-with-PKCE token; nothing else changes."""
        with self._session_lock:
            return self.session.tokens().get(scope)

    def sync_favorites(self, db: Database) -> int:
        if not self.ensure_login():
            return 0
        token = self._web_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{SPOTIFY_API}/me/tracks?limit=50"
        count = 0
        while url:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "5")) + 1
                log.warning("Spotify Web API 429 — sleeping %ss", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            page = r.json()
            for item in page.get("items", []):
                t = item.get("track") or {}
                if not t.get("id") or t.get("is_local"):
                    continue
                db.upsert_track(
                    f"spotify:{t['id']}",
                    t.get("name") or "Unknown",
                    (t.get("artists") or [{}])[0].get("name") or "Unknown",
                    (t.get("album") or {}).get("name"),
                    int((t.get("duration_ms") or 0) / 1000),
                    favorite=True,
                    source="spotify",
                )
                count += 1
            url = page.get("next")
        log.info("Synced %d Spotify favorites", count)
        return count

    # ── shared cache (one budget across sources) ──────────────────────────
    def _evict_cache(self):
        max_bytes = float(self.cfg.get("cache.max_gb", 6)) * (1 << 30)
        files = sorted(self.cfg.cache_dir.glob("*.flac"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        while total > max_bytes and len(files) > 1:
            victim = files.pop(0)
            total -= victim.stat().st_size
            victim.unlink(missing_ok=True)
            log.info("Cache evicted %s", victim.name)

    def cache_usage_gb(self) -> float:
        if not self.cfg.cache_dir.exists():
            return 0.0
        return sum(p.stat().st_size for p in self.cfg.cache_dir.glob("*.flac")) / (1 << 30)


# ── the abstraction both sources sit behind ───────────────────────────────
class SourceRouter:
    """Dispatches on the db key prefix: 'tidal:<int>' / 'spotify:<base62>'.

    Orchestrator changes are then one-liners:
        path = self.sources.fetch_track(track["id"])
        wait = self.sources.throttled_for(track["id"])
    """

    def __init__(self, **sources):
        self.sources = sources               # {"tidal": TidalClient, "spotify": SpotifyClient}

    def _pick(self, key: str):
        name = key.split(":", 1)[0] if ":" in str(key) else "tidal"
        return self.sources.get(name)

    def fetch_track(self, key: str):
        src = self._pick(key)
        if src is None:
            return None
        raw = str(key).split(":", 1)[1] if ":" in str(key) else key
        return src.fetch_track(int(raw) if str(raw).isdigit() else raw)

    def throttled_for(self, key: str) -> float:
        src = self._pick(key)
        return src.throttled_for() if src else 0.0

    def sync_all(self, db: Database) -> int:
        total = 0
        for name, src in self.sources.items():
            try:
                if src.is_linked():
                    total += src.sync_favorites(db)
            except Exception:
                log.exception("%s sync failed", name)
        return total
