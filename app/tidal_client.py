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
            if self._try_restore():
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

    # ── library sync ──────────────────────────────────────────────────────
    def sync_favorites(self, db: Database) -> int:
        favs = self.session.user.favorites
        count = 0
        offset = 0
        while True:
            try:
                page = favs.tracks(limit=100, offset=offset)
            except TypeError:  # older tidalapi without pagination kwargs
                page = favs.tracks()
            if not page:
                break
            for t in page:
                db.upsert_track(
                    t.id, t.name, t.artist.name if t.artist else "Unknown",
                    t.album.name if t.album else None, t.duration, favorite=True,
                )
                count += 1
            if len(page) < 100:
                break
            offset += len(page)
        log.info("Synced %d favorite tracks", count)
        return count

    # ── audio cache ───────────────────────────────────────────────────────
    def cached_path(self, track_id: int) -> Path:
        return self.cfg.cache_dir / f"{track_id}.flac"

    def fetch_track(self, track_id: int) -> Path | None:
        """Return a local audio file for the track, downloading into the cache
        if needed. Returns None on failure (skip the track)."""
        out = self.cached_path(track_id)
        if out.exists():
            out.touch()  # bump LRU
            return out
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            urls = self._stream_urls_with_fallback(track_id)
            if not urls:
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
            return out
        except Exception as e:
            log.error("Fetch failed for track %s: %s", track_id, e)
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
            urls = self._stream_urls(self.session.track(track_id))
            if urls:
                if name != self.quality:
                    log.warning(
                        "Stream quality reduced to %s — a device-link login is not "
                        "authorised for %s. Re-link with `tidal-radio auth --pkce` "
                        "for lossless.", name, self.quality)
                    self.quality = name
                return urls
        self._apply_quality(self.quality)   # restore for the next attempt
        return []

    def _stream_urls(self, track) -> list[str]:
        """Get playable URL(s) across tidalapi versions."""
        try:
            stream = track.get_stream()
            manifest = stream.get_stream_manifest()
            urls = manifest.get_urls()
            if isinstance(urls, str):
                urls = [urls]
            return list(urls)
        except Exception:
            pass
        try:  # legacy API
            return [track.get_url()]
        except Exception as e:
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
