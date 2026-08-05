"""Spotify source for the station, built on librespot-python.

Mirrors TidalClient's surface so the orchestrator can drive either one:

    is_linked() / ensure_login() / fetch_track() / sync_favorites()
    throttled_for() / quality / last_error / cache_usage_gb() / diagnose()

Track ids here are Spotify base62 strings, not ints, and are stored in the
library as "spotify:<base62>" so both sources can share one tracks table.

Audio: librespot decrypts Spotify's OGG Vorbis CDN stream and hands us a plain
seekable byte stream, *faster than real time*. We write it to a temp .ogg and
transcode to FLAC with ffmpeg, exactly like TidalClient does, so liquidsoap and
librosa see one uniform format.

IMPORTANT (deployment): librespot's generated *_pb2.py are protoc-3.20 era and
raise "Descriptors cannot be created directly" under protobuf >= 4, which is
what onnxruntime (piper-tts) requires. They coexist only if the process runs
with PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python — set it in the systemd unit
and in the auth CLI. We check it before touching librespot so the failure mode
is one clear sentence instead of a descriptor traceback.
"""
import logging
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from .config import Config
from .db import Database

log = logging.getLogger(__name__)

SPOTIFY_API = "https://api.spotify.com/v1"
OGG_CHUNK = 1 << 16

# Spotify only redirects to a URI registered against the client id, and the one
# librespot is registered for is a loopback address. That address resolves on
# whichever machine opened the browser — never on this container — so the code
# comes back through the UI instead. Neither value may be substituted.
OAUTH_REDIRECT = "http://127.0.0.1:5588/login"


class SpotifyClient:
    """Owns the librespot session, library sync, and shares the audio cache."""

    name = "spotify"

    # Highest → lowest. Spotify Connect (and therefore librespot) is not served
    # the lossless tier, so this ladder is Vorbis-only by design; asking for
    # anything else just yields no playable file.
    QUALITY_LADDER = ["VERY_HIGH", "HIGH", "NORMAL"]

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = None
        self._session_lock = threading.Lock()    # guards swapping self.session
        # librespot's chunked streams share one class-level condition variable,
        # so two threads downloading at once wait on each other's chunks.
        self._fetch_lock = threading.Lock()
        self._oauth = None                       # live OAuth exchange, if any
        self._link: dict | None = None           # link state for the UI
        self._link_lock = threading.Lock()
        self._username: str | None = None
        wanted = str(cfg.get("spotify.quality", "VERY_HIGH")).upper()
        if wanted not in self.QUALITY_LADDER:
            wanted = "VERY_HIGH"
        self.quality = wanted
        self.served_quality: str | None = None   # what Spotify actually gave us
        # request pacing — the Web API returns 429 with a Retry-After
        self._backoff_until = 0.0
        self._backoff_step = 0
        self._track_cooldown: dict[str, float] = {}
        self.last_error: str | None = None

    # ── paths ─────────────────────────────────────────────────────────────
    @property
    def credentials_path(self) -> Path:
        return self.cfg.data_dir / "spotify-credentials.json"

    def cached_path(self, track_id: str) -> Path:
        # Namespaced so a base62 id cannot collide with Tidal's numeric one,
        # while the shared LRU evictor (one *.flac budget) still sees both.
        return self.cfg.cache_dir / f"sp_{self._base62(track_id)}.flac"

    @staticmethod
    def _base62(track_id: str) -> str:
        """Accept '4uLU…', 'spotify:4uLU…' (our db key) or a full track URI."""
        return str(track_id).rsplit(":", 1)[-1]

    # ── auth ──────────────────────────────────────────────────────────────
    def _guard_protobuf(self) -> None:
        """librespot's stubs predate protobuf 4 and only load under the pure
        Python backend. Checked here because the traceback it raises otherwise
        names descriptors, not the environment variable that fixes it."""
        from google.protobuf import __version__ as protobuf_version
        from google.protobuf.internal import api_implementation

        backend = api_implementation.Type()
        if int(protobuf_version.split(".")[0]) >= 4 and backend != "python":
            raise RuntimeError(
                "Set PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python — librespot's "
                f"protobuf stubs cannot load under the {backend} backend of "
                f"protobuf {protobuf_version}")

    def _configuration(self):
        from librespot.core import Session
        return (Session.Configuration.Builder()
                .set_store_credentials(True)
                .set_stored_credential_file(str(self.credentials_path))
                .set_cache_enabled(False)        # we keep our own FLAC cache
                .build())

    def _new_oauth(self):
        from librespot.mercury import MercuryRequests
        from librespot.oauth import OAuth
        # No url callback: we hand the URL to the caller instead of printing it.
        return OAuth(MercuryRequests.keymaster_client_id, OAUTH_REDIRECT, None)

    def start_link(self) -> dict:
        """Begin an OAuth login and return the URL for the user to approve.

        Stepwise on purpose: librespot's own builder.oauth() waits on a
        callback listener *inside this container*, which the browser on the
        user's laptop can never reach. The user's browser is left on a page
        that fails to load, and its address bar carries the code back to
        submit_code().
        """
        self._guard_protobuf()
        with self._link_lock:
            if self._link and self._link.get("status") == "pending":
                return dict(self._link)          # reuse the live link
            self._oauth = self._new_oauth()      # always start clean
            self._link = {"status": "pending", "needs_code": True,
                          "url": self._oauth.get_auth_url()}
            return dict(self._link)

    def submit_code(self, code: str) -> dict:
        """Finish the login with the code (or whole redirect URL) pasted back."""
        with self._link_lock:
            oauth = self._oauth
            url = (self._link or {}).get("url")
        if oauth is None:
            return self._set_link({"status": "failed",
                                   "error": "no link in progress — start one first"})
        code = self._extract_code(code)
        if not code:
            return self._set_link({"status": "failed", "url": url,
                                   "error": "that address carries no ?code= — "
                                            "approve the login and paste the "
                                            "page your browser landed on"})
        try:
            self._guard_protobuf()
            oauth.set_code(code)
            oauth.request_token()
            if not self._open_session(oauth.get_credentials()):
                raise RuntimeError(self.last_error or "login did not complete")
        except Exception as e:
            log.error("Spotify link failed: %s", e)
            return self._set_link({"status": "failed", "url": url, "error": str(e)})
        with self._link_lock:
            self._oauth = None                   # the code is single-use
        log.info("Spotify linked from the control UI (user %s)", self._username)
        return self._set_link({"status": "linked"})

    @staticmethod
    def _extract_code(pasted: str) -> str:
        """The whole redirect URL is easier to copy than the code inside it.

        Anything with a query string is treated as that URL, so a refused login
        (…?error=access_denied) yields no code rather than being sent to Spotify
        as if it were one.
        """
        pasted = (pasted or "").strip()
        if "?" not in pasted:
            return pasted
        query = urlparse(pasted).query or pasted.split("?", 1)[1]
        return parse_qs(query).get("code", [""])[0]

    def _set_link(self, state: dict) -> dict:
        with self._link_lock:
            self._link = state
        return dict(state)

    def link_status(self) -> dict:
        with self._link_lock:
            state = dict(self._link) if self._link else {"status": "idle"}
        if state["status"] == "idle" and self.is_linked():
            state["status"] = "linked"
        return state

    def login_interactive(self) -> bool:
        """Terminal login — the same stepwise flow the control UI drives.

        Reuses valid stored credentials, so re-running it is cheap.
        """
        if self.is_linked() and self.ensure_login():
            log.info("Existing Spotify credentials are still valid")
            return True
        state = self.start_link()
        print(f"\nOpen this in your browser and approve:\n\n  {state['url']}\n")
        print(f"Your browser will then land on {OAUTH_REDIRECT}?code=… and fail "
              "to load —\nthat is expected. Copy the whole address and paste it "
              "here.\n")
        result = self.submit_code(input("Redirect URL (or just the code): "))
        if result.get("status") == "linked":
            log.info("Spotify linked (user %s)", self._username)
            return True
        log.error("Spotify login did not complete: %s", result.get("error"))
        return False

    def ensure_login(self) -> bool:
        """Non-interactive: reuse the stored credentials. Never prompts —
        this runs inside the service, where an interactive login would hang."""
        with self._session_lock:
            session = self.session
        if session is not None and self._session_valid(session):
            return True
        if not self.is_linked():
            log.error("No Spotify credentials — run `tidal-radio spotify-auth` first")
            return False
        try:
            self._guard_protobuf()
        except Exception as e:
            self.last_error = str(e)
            log.error(self.last_error)
            return False
        return self._open_session()

    def _open_session(self, credentials=None) -> bool:
        """Build (or rebuild) the librespot session.

        `credentials` comes from a completed OAuth exchange; without it the
        stored credentials file is used, which librespot rewrites on every
        successful authentication. Assigning login_credentials directly is
        deliberate — Builder.oauth() would start a loopback listener here.
        """
        try:
            from librespot.core import Session
            self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
            builder = Session.Builder(self._configuration())
            builder.set_device_name(str(self.cfg.get("station.name", "tidal-radio")))
            if credentials is not None:
                builder.login_credentials = credentials
            else:
                builder.stored_file(str(self.credentials_path))
                if builder.login_credentials is None:
                    raise RuntimeError("stored credentials are unreadable — re-link "
                                       "the account")
            session = builder.create()
        except Exception as e:
            self.last_error = f"Spotify login failed: {e}"
            log.error(self.last_error)
            return False
        self._replace_session(session)
        try:
            self._username = session.username()
        except Exception:
            self._username = None
        self.last_error = None
        return True

    def _replace_session(self, session=None) -> None:
        """Swap in a new session — or none — and close the one it replaced."""
        with self._session_lock:
            old, self.session = self.session, session
        if old is None:
            return
        try:
            old.close()
        except Exception:
            pass                                 # already dead, nothing to do

    @staticmethod
    def _session_valid(session) -> bool:
        try:
            return bool(session.is_valid())
        except Exception:                        # closed, or half-dead socket
            return False

    def is_linked(self) -> bool:
        return self.credentials_path.exists()

    # ── diagnostics ──────────────────────────────────────────────────────
    def diagnose(self, track_id: str | None = None) -> list[tuple[str, str]]:
        """One request per check — reports what Spotify actually allows.

        Deliberately minimal: a single stream attempt, so running it while
        rate-limited doesn't make things worse.
        """
        out: list[tuple[str, str]] = []
        add = lambda k, v: out.append((k, str(v)))  # noqa: E731

        add("credentials file",
            self.credentials_path if self.is_linked() else "MISSING")
        try:
            self._guard_protobuf()
            add("protobuf backend", "pure python — OK")
        except Exception as e:
            add("protobuf backend", f"WRONG — {e}")
            return out
        if not self.is_linked():
            return out

        restored = self.ensure_login()
        add("session", "restored" if restored else f"FAILED — {self.last_error}")
        with self._session_lock:
            session = self.session
        if not restored or session is None:
            return out
        add("user", self._username or "?")
        add("account type", session.get_user_attribute("type", "?"))
        add("country code", session.get_user_attribute("country", "?"))
        add("configured quality", self.quality)

        if track_id is None:
            try:
                rows = Database(self.cfg.db_path).query(
                    "SELECT id FROM tracks WHERE source='spotify' LIMIT 1")
                track_id = rows[0]["id"] if rows else None
            except Exception:
                pass
        if track_id is None:
            add("stream test", "skipped — no Spotify tracks yet, run `tidal-radio sync`")
            return out

        try:
            from librespot.metadata import TrackId
            picker = self._picker(self.quality)
            with self._fetch_lock:
                loaded = session.content_feeder().load(
                    TrackId.from_base62(self._base62(track_id)), picker, False, None)
                loaded.input_stream.stream().close()
            add("stream test", f"OK at {self._picked_quality(picker) or self.quality}")
        except Exception as e:
            add("stream test", f"FAILED: {e}")
            add("hint", "librespot only streams for Premium accounts — a free one "
                        "is refused the audio key for every track")
        return out

    # ── library sync ──────────────────────────────────────────────────────
    def _web_token(self, scope: str = "user-library-read") -> str:
        """Web API token minted through librespot's Login5 — no Spotify developer
        app registration needed. librespot caches it and re-mints it on expiry,
        so this is cheap enough to call per request (and a sync of a large
        library outlives the one-hour token).

        If this ever stops working, swap in a normal registered-app
        Authorization-Code-with-PKCE token; nothing else changes.
        """
        with self._session_lock:
            session = self.session
        if session is None:
            raise RuntimeError("Spotify session is not open")
        try:
            token = session.tokens().get(scope)
        except AttributeError:      # Login5 refused; librespot returns no token
            token = None
        if not token:
            raise RuntimeError("Spotify would not issue a Web API token")
        return token

    def sync_favorites(self, db: Database) -> int:
        if not self.ensure_login():
            return 0
        url = f"{SPOTIFY_API}/me/tracks?limit=50"
        count = 0
        attempts = 0
        while url:
            r = requests.get(url, timeout=30,
                             headers={"Authorization": f"Bearer {self._web_token()}"})
            if r.status_code == 429:
                attempts += 1
                if attempts > 5:
                    # Persistent throttling: stop and let the global backoff
                    # keep playback from making it worse.
                    self._note_failure(None, 429)
                    break
                wait = min(int(r.headers.get("Retry-After", "5")) + 1, 60)
                log.warning("Spotify Web API 429 — sleeping %ds", wait)
                time.sleep(wait)
                continue
            attempts = 0
            r.raise_for_status()
            page = r.json()
            for item in page.get("items", []):
                t = item.get("track") or {}
                if not t.get("id") or t.get("is_local"):
                    continue                     # local files have no stream
                album = t.get("album") or {}
                db.upsert_track(
                    f"spotify:{t['id']}",
                    t.get("name") or "Unknown",
                    (t.get("artists") or [{}])[0].get("name") or "Unknown",
                    album.get("name"), round((t.get("duration_ms") or 0) / 1000),
                    favorite=True, cover_url=self._cover_url(album),
                    source="spotify",
                )
                count += 1
            url = page.get("next")
        log.info("Synced %d favorite tracks", count)
        return count

    @staticmethod
    def _cover_url(album: dict) -> str | None:
        """Album art nearest 320px, to match what the Tidal source stores."""
        images = album.get("images") or []
        if not images:
            return None
        return min(images, key=lambda i: abs((i.get("width") or 0) - 320)).get("url")

    # ── request pacing (same shape as TidalClient) ────────────────────────
    def throttled_for(self) -> float:
        """Seconds until the next Spotify request is allowed (0 = go ahead)."""
        return max(0.0, self._backoff_until - time.time())

    def _note_failure(self, track_id: str | None, status: int | None):
        """Back off globally on rate limits, cool down the track otherwise."""
        now = time.time()
        if status == 429:
            self._backoff_step = min(self._backoff_step + 1, 6)
            wait = min(60 * (2 ** (self._backoff_step - 1)), 1800)
            self._backoff_until = now + wait
            self.last_error = f"Spotify rate-limited us (429) — pausing {int(wait)}s"
            log.warning(self.last_error)
        elif track_id is not None:
            # Region-restricted, unplayable, or an audio-key refusal: park it.
            self._track_cooldown[self._base62(track_id)] = now + 900
            if status in (401, 403):
                self.last_error = ("Spotify refused playback — the account is not "
                                   "authorised to stream (Premium is required)")

    def _note_success(self):
        self._backoff_step = 0
        self._backoff_until = 0.0
        self.last_error = None

    @staticmethod
    def _http_status(exc: Exception) -> int | None:
        """The HTTP status behind a failure, whether it surfaced through
        requests or through librespot's own StatusCodeException."""
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return status if status is not None else getattr(exc, "code", None)

    # ── audio ─────────────────────────────────────────────────────────────
    def fetch_track(self, track_id: str) -> Path | None:
        """Return a local audio file for the track, downloading into the cache
        if needed. Returns None on failure (skip the track)."""
        out = self.cached_path(track_id)
        if out.exists():
            out.touch()  # bump LRU
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
            from librespot.metadata import TrackId

            picker = self._picker(self.quality)
            tid = TrackId.from_base62(self._base62(track_id))
            with self._fetch_lock:
                with self._session_lock:
                    session = self.session
                loaded = session.content_feeder().load(tid, picker, False, None)
                # Spotify's 0xA7-byte header is already skipped, so what's left
                # is a plain Ogg file.
                stream = loaded.input_stream.stream()
                try:
                    with tempfile.NamedTemporaryFile(dir=self.cfg.cache_dir,
                                                     suffix=".ogg",
                                                     delete=False) as tmp:
                        tmp_path = Path(tmp.name)
                        size = stream.size()
                        # Read to the known length: a read *at* the end walks off
                        # librespot's chunk table instead of returning empty.
                        while stream.pos() < size:
                            chunk = stream.read(OGG_CHUNK)
                            if not chunk:
                                break
                            tmp.write(chunk)
                finally:
                    stream.close()
            self._note_quality(picker)
            if tmp_path.stat().st_size < 4096:
                raise IOError("stream produced no audio")

            # Normalize container to FLAC so liquidsoap/librosa handle it uniformly.
            proc = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp_path),
                 "-vn", "-c:a", "flac", str(out)],
                capture_output=True, text=True,
            )
            tmp_path.unlink(missing_ok=True)
            if proc.returncode != 0 or not out.exists():
                log.error("ffmpeg failed for track %s: %s", track_id, proc.stderr[-400:])
                out.unlink(missing_ok=True)
                return None
            self._evict_cache()
            self._note_success()
            return out
        except Exception as e:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            log.error("Fetch failed for track %s: %s", track_id, e)
            self._note_failure(track_id, self._http_status(e))
            # A dropped socket surfaces as an arbitrary error deep inside
            # librespot, so ask the session itself rather than read the message.
            with self._session_lock:
                session = self.session
            if session is not None and not self._session_valid(session):
                self._replace_session()  # the next attempt rebuilds from the file
            return None

    def _picker(self, preferred: str):
        """Vorbis-only quality picker that remembers what it settled on."""
        from librespot.audio.decoders import AudioQuality, VorbisOnlyAudioQuality

        class Probe(VorbisOnlyAudioQuality):
            chosen = None

            def get_file(self, files):
                self.chosen = super().get_file(files)
                return self.chosen

        return Probe(AudioQuality[preferred])

    @staticmethod
    def _picked_quality(picker) -> str | None:
        from librespot.audio.decoders import AudioQuality
        chosen = getattr(picker, "chosen", None)
        try:
            return AudioQuality.get_quality(chosen.format).name
        except Exception:                        # no file, or an unknown format
            return None

    def _note_quality(self, picker) -> None:
        """Record what Spotify actually served, and say so once.

        Unlike Tidal — where a lower tier means the token isn't authorised for
        the higher one, account-wide — Spotify's picker degrades per file. So
        the requested quality deliberately stays put: one 160k track must not
        pin the whole station down to 160k.
        """
        served = self._picked_quality(picker)
        if served is None or served == self.served_quality:
            return
        self.served_quality = served
        if served != self.quality:
            log.warning("Spotify served %s, not the requested %s — this account "
                        "is not offered %s for these tracks (Connect clients are "
                        "never served lossless).", served, self.quality, self.quality)

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


# ── optional: run both sources at once ────────────────────────────────────
class SourceRouter:
    """Dispatches on the db key: a bare Tidal id, or 'spotify:<base62>'.

    Only needed if the library holds both sources at once — with one source
    selected in the config, either client can be used on its own.
    """

    def __init__(self, **sources):
        self.sources = sources               # {"tidal": TidalClient, "spotify": SpotifyClient}

    def _pick(self, key: str):
        name = key.split(":", 1)[0] if ":" in str(key) else "tidal"
        return self.sources.get(name)

    def fetch_track(self, key: str) -> Path | None:
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
