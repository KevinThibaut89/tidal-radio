import logging
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import requests
import tidalapi

from .config import Config
from .db import Database

log = logging.getLogger(__name__)


class TidalClient:
    """Owns the Tidal session, library sync, and a bounded local audio cache."""

    # Highest → lowest. A device-link (non-PKCE) token is only authorised for
    # HIGH and below; asking for LOSSLESS with one yields 401 on the stream
    # endpoints, so we walk down this ladder until something plays.
    QUALITY_LADDER = ["HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW"]

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = tidalapi.Session()
        self._link: dict | None = None       # device-link state for the UI
        self._link_lock = threading.Lock()
        wanted = cfg.get("tidal.quality", "HIGH").upper()
        if wanted not in self.QUALITY_LADDER:
            wanted = "HIGH"
        self.quality = wanted
        self._apply_quality(wanted)
        # request pacing — Tidal returns 429 quickly if you retry in a tight loop
        self._backoff_until = 0.0
        self._backoff_step = 0
        self._track_cooldown: dict[int, float] = {}
        self._last_http_status: int | None = None
        self.last_error: str | None = None

    def _apply_quality(self, name: str) -> None:
        try:
            self.session.audio_quality = {
                "LOW": tidalapi.Quality.low_96k,
                "HIGH": tidalapi.Quality.low_320k,
                "LOSSLESS": tidalapi.Quality.high_lossless,
                "HI_RES_LOSSLESS": tidalapi.Quality.hi_res_lossless,
            }[name]
        except (AttributeError, KeyError):   # older tidalapi enum names
            pass

    # ── auth ──────────────────────────────────────────────────────────────
    def login_interactive(self, pkce: bool = False) -> bool:
        """Device-link login; prints the link.tidal.com URL. Persists session.

        `pkce=True` uses the browser redirect flow instead, which is the only
        login that authorises LOSSLESS / HI_RES streaming. It asks you to paste
        back the URL your browser lands on, so it needs a terminal.

        A stale session file must never block re-linking: tidalapi raises
        AuthenticationError from the token refresh while *loading* it, so the
        load is attempted defensively and a dead session is moved aside before
        starting a fresh device-link login.
        """
        path = self.cfg.session_path
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists():
            # Reusing a valid session is right — unless PKCE was asked for and
            # the stored one is a device link, which is exactly the upgrade the
            # user is trying to perform.
            reusable = self._try_restore()
            if reusable and pkce and not getattr(self.session, "is_pkce", False):
                log.info("Existing session is a device link — re-linking with PKCE")
                reusable = False
            if reusable:
                log.info("Existing Tidal session is still valid")
                return True
            backup = path.with_name(path.name + ".bak")
            log.warning("Existing session unusable — moving it to %s", backup.name)
            try:
                path.replace(backup)
            except OSError:
                path.unlink(missing_ok=True)
            self.session = tidalapi.Session()  # discard the poisoned state

        if pkce:
            self.session.login_pkce(fn_print=print)      # asks for pasted URL
        else:
            self.session.login_oauth_simple(fn_print=print)  # prints link, blocks
        if self._checked_login():
            self.session.save_session_to_file(path)
            user = getattr(self.session, "user", None)
            log.info("Tidal linked (user id %s)", getattr(user, "id", "?"))
            return True
        log.error("Tidal login did not complete")
        return False

    def ensure_login(self) -> bool:
        """Non-interactive: restore the persisted session. Never prompts —
        this runs inside the service, where an interactive login would hang."""
        if not self.cfg.session_path.exists():
            log.error("No Tidal session found — run `tidal-radio auth` first")
            return False
        if not self._try_restore():
            log.error("Tidal session is not valid — re-run `tidal-radio auth`")
            return False
        # persist any tokens refreshed during the restore
        try:
            self.session.save_session_to_file(self.cfg.session_path)
        except Exception:
            pass
        return True

    def _try_restore(self) -> bool:
        """Load the session file and confirm it works. False on any failure."""
        try:
            self.session.load_session_from_file(self.cfg.session_path)
        except Exception as e:  # expired refresh token, revoked access, …
            log.warning("Tidal session restore failed: %s", e)
            return False
        return self._checked_login()

    def _checked_login(self) -> bool:
        try:
            return bool(self.session.check_login())
        except Exception as e:
            log.warning("Tidal login check failed: %s", e)
            return False

    # ── device link driven from the control UI ───────────────────────────
    def start_device_link(self) -> dict:
        """Begin a device-link login and return the URL for the user to open.

        The wait runs in a background thread so the HTTP request returns at
        once; poll link_status() for the outcome.
        """
        with self._link_lock:
            if self._link and self._link.get("status") == "pending":
                return self._link                      # reuse the live link
            self.session = tidalapi.Session()          # always start clean
            login, future = self.session.login_oauth()
            url = login.verification_uri_complete
            if not url.startswith("http"):
                url = f"https://{url}"
            self._link = {"status": "pending", "url": url,
                          "expires_in": getattr(login, "expires_in", None)}

        def _wait():
            try:
                future.result()
                if self._checked_login():
                    self.session.save_session_to_file(self.cfg.session_path)
                    log.info("Tidal linked from the control UI")
                    self._set_link({"status": "linked", "url": url})
                else:
                    self._set_link({"status": "failed", "url": url,
                                    "error": "login did not complete"})
            except Exception as e:
                log.error("Device link failed: %s", e)
                self._set_link({"status": "failed", "url": url, "error": str(e)})

        threading.Thread(target=_wait, daemon=True, name="tidal-link").start()
        return self._link

    def _set_link(self, state: dict):
        with self._link_lock:
            self._link = state

    def link_status(self) -> dict:
        with self._link_lock:
            state = dict(self._link) if self._link else {"status": "idle"}
        if state.get("status") != "pending" and self.cfg.session_path.exists():
            state.setdefault("status", "linked")
        return state

    def is_linked(self) -> bool:
        return self.cfg.session_path.exists()

    # ── diagnostics ──────────────────────────────────────────────────────
    def diagnose(self, track_id: int | None = None) -> list[tuple[str, str]]:
        """One request per check — reports what Tidal actually allows.

        Deliberately minimal: a single stream attempt at a single quality, so
        running it while rate-limited doesn't make things worse.
        """
        out: list[tuple[str, str]] = []
        add = lambda k, v: out.append((k, str(v)))  # noqa: E731

        add("session file", self.cfg.session_path if self.is_linked() else "MISSING")
        if not self.is_linked():
            return out
        add("session restored", "yes" if self._try_restore() else "NO — re-run auth")

        user = getattr(self.session, "user", None)
        add("user id", getattr(user, "id", "?"))
        add("country code", getattr(self.session, "country_code", "?"))
        add("session is PKCE", getattr(self.session, "is_pkce", False))
        add("configured quality", self.quality)

        try:
            sub = self.session.request.request(
                "GET", f"users/{user.id}/subscription").json()
            add("subscription", sub.get("subscription", {}).get("type", sub))
            add("sound quality", sub.get("highestSoundQuality", "?"))
        except Exception as e:
            add("subscription", f"lookup failed: {e}")

        if track_id is None:
            row = None
            try:
                from .db import Database
                rows = Database(self.cfg.db_path).query("SELECT id FROM tracks LIMIT 1")
                row = rows[0]["id"] if rows else None
            except Exception:
                pass
            track_id = row
        if track_id is None:
            add("stream test", "skipped — library is empty, run `tidal-radio sync`")
            return out

        self._last_http_status = None
        try:
            urls = self._stream_urls(self.session.track(track_id))
            add("stream test", f"OK at {self.quality}" if urls
                else f"FAILED (HTTP {self._last_http_status}) at {self.quality}")
        except Exception as e:
            add("stream test", f"FAILED: {e}")
        if self._last_http_status == 401 and not getattr(self.session, "is_pkce", False):
            add("hint", "401 with a device-link login — try `tidal-radio auth --pkce`, "
                        "or check the subscription covers streaming in your region")
        elif self._last_http_status == 429:
            add("hint", "429 means Tidal is rate-limiting this account right now; "
                        "wait a few minutes before retrying")
        return out

    # ── library sync ──────────────────────────────────────────────────────
    def sync_favorites(self, db: Database) -> int:
        """Import everything playable we can reach: favourited tracks, plus the
        tracks inside favourited albums and the user's playlists.

        Favourited *tracks* alone are a poor library — most people organise by
        playlist or album, and a handful of tracks makes the station loop.
        """
        seen: set[int] = set()

        def store(tracks, favorite: bool) -> None:
            for t in tracks or []:
                tid = getattr(t, "id", None)
                if tid is None or tid in seen:
                    continue
                seen.add(tid)
                db.upsert_track(
                    tid, t.name, t.artist.name if getattr(t, "artist", None) else "Unknown",
                    t.album.name if getattr(t, "album", None) else None,
                    getattr(t, "duration", None), favorite=favorite,
                    cover_url=self._cover_url(t), source="tidal",
                )

        favs = self.session.user.favorites
        store(self._paged(favs.tracks), favorite=True)
        log.info("Synced %d favourite tracks", len(seen))

        if self.cfg.get("sync.albums", True):
            before = len(seen)
            for album in self._paged(favs.albums):
                try:
                    store(album.tracks(), favorite=False)
                except Exception as e:
                    log.debug("Album %s unreadable: %s", getattr(album, "name", "?"), e)
            log.info("Added %d tracks from favourite albums", len(seen) - before)

        if self.cfg.get("sync.playlists", True):
            before = len(seen)
            for pl in self._playlists():
                try:
                    store(self._paged(pl.tracks), favorite=False)
                except Exception as e:
                    log.debug("Playlist %s unreadable: %s", getattr(pl, "name", "?"), e)
            log.info("Added %d tracks from playlists", len(seen) - before)

        log.info("Library sync finished: %d unique tracks", len(seen))
        return len(seen)

    def _playlists(self) -> list:
        """User playlists across tidalapi versions (own + followed)."""
        out = []
        for getter in ("playlists", "playlist_and_favorite_playlists"):
            fn = getattr(self.session.user, getter, None)
            if fn is None:
                continue
            try:
                items = fn()
                # some versions return (playlist, kind) tuples
                out.extend(i[0] if isinstance(i, tuple) else i for i in items or [])
                break
            except Exception as e:
                log.debug("Playlist listing via %s failed: %s", getter, e)
        try:
            out.extend(self._paged(self.session.user.favorites.playlists))
        except Exception:
            pass
        return out

    @staticmethod
    def _paged(fn, page_size: int = 100) -> list:
        """Drain a paginated tidalapi listing; tolerate versions without kwargs."""
        items: list = []
        offset = 0
        while True:
            try:
                page = fn(limit=page_size, offset=offset)
            except TypeError:
                return list(fn() or [])
            except Exception as e:
                log.debug("Paged listing failed at offset %d: %s", offset, e)
                break
            if not page:
                break
            items.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)
        return items

    @staticmethod
    def _cover_url(track) -> str | None:
        """Album art URL, if this tidalapi version exposes one."""
        album = getattr(track, "album", None)
        if album is None:
            return None
        try:
            return album.image(320)
        except Exception:
            return None

    # ── audio cache ───────────────────────────────────────────────────────
    def cached_path(self, track_id: int) -> Path:
        return self.cfg.cache_dir / f"{track_id}.flac"

    # ── request pacing ───────────────────────────────────────────────────
    def throttled_for(self) -> float:
        """Seconds until the next Tidal request is allowed (0 = go ahead)."""
        return max(0.0, self._backoff_until - time.time())

    def _note_failure(self, track_id: int, status: int | None):
        """Back off globally on rate limits, cool down the track otherwise."""
        now = time.time()
        if status == 429:
            self._backoff_step = min(self._backoff_step + 1, 6)
            wait = min(60 * (2 ** (self._backoff_step - 1)), 1800)
            self._backoff_until = now + wait
            self.last_error = f"Tidal rate-limited us (429) — pausing {int(wait)}s"
            log.warning(self.last_error)
        else:
            self._track_cooldown[track_id] = now + 900
            if status == 401:
                self.last_error = ("Tidal refused playback (401) — the account or "
                                   "login is not authorised to stream this track")

    def _note_success(self):
        self._backoff_step = 0
        self._backoff_until = 0.0
        self.last_error = None

    def fetch_track(self, track_id: int) -> Path | None:
        """Return a local audio file for the track, downloading into the cache
        if needed. Returns None on failure (skip the track)."""
        out = self.cached_path(track_id)
        if out.exists():
            out.touch()  # bump LRU
            return out
        if self.throttled_for() > 0:
            return None
        if self._track_cooldown.get(track_id, 0) > time.time():
            return None
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            urls = self._stream_urls_with_fallback(track_id)
            if not urls:
                self._note_failure(track_id, self._last_http_status)
                return None
            with tempfile.NamedTemporaryFile(dir=self.cfg.cache_dir, suffix=".dl",
                                             delete=False) as tmp:
                for url in urls:
                    with requests.get(url, stream=True, timeout=60) as r:
                        r.raise_for_status()
                        for chunk in r.iter_content(chunk_size=1 << 16):
                            tmp.write(chunk)
                tmp_path = Path(tmp.name)
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
            log.error("Fetch failed for track %s: %s", track_id, e)
            self._note_failure(track_id, self._http_status(e))
            return None

    def _stream_urls_with_fallback(self, track_id: int) -> list[str]:
        """Fetch stream URLs, stepping the quality down if the account/token
        isn't authorised for the requested level (401 on the stream endpoints).

        The working level is remembered, so the ladder is walked once — not on
        every track.
        """
        start = self.QUALITY_LADDER.index(self.quality)
        for name in self.QUALITY_LADDER[start:]:
            if name != self.quality:
                self._apply_quality(name)
            self._last_http_status = None
            urls = self._stream_urls(self.session.track(track_id))
            if urls:
                if name != self.quality:
                    log.warning(
                        "Stream quality reduced to %s — a device-link login is not "
                        "authorised for %s. Re-link with `tidal-radio auth --pkce` "
                        "for lossless.", name, self.quality)
                    self.quality = name
                return urls
            if self._last_http_status == 429:
                # Walking the ladder now would just burn more requests.
                break
        self._apply_quality(self.quality)   # restore for the next attempt
        return []

    @staticmethod
    def _http_status(exc: Exception) -> int | None:
        return getattr(getattr(exc, "response", None), "status_code", None)

    def _stream_urls(self, track) -> list[str]:
        """Get playable URL(s) across tidalapi versions."""
        try:
            stream = track.get_stream()
            manifest = stream.get_stream_manifest()
            urls = manifest.get_urls()
            if isinstance(urls, str):
                urls = [urls]
            return list(urls)
        except Exception as e:
            self._last_http_status = self._http_status(e) or self._last_http_status
        try:  # legacy API
            return [track.get_url()]
        except Exception as e:
            self._last_http_status = self._http_status(e) or self._last_http_status
            # Not fatal on its own — the caller may retry at a lower quality.
            log.debug("No stream URL for track %s at %s: %s", track.id, self.quality, e)
            return []

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
