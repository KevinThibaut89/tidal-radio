import logging
import subprocess
import tempfile
import time
from pathlib import Path

import requests
import tidalapi

from .config import Config
from .db import Database

log = logging.getLogger(__name__)


class TidalClient:
    """Owns the Tidal session, library sync, and a bounded local audio cache."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = tidalapi.Session()
        quality = cfg.get("tidal.quality", "LOSSLESS").upper()
        try:
            self.session.audio_quality = {
                "LOW": tidalapi.Quality.low_96k,
                "HIGH": tidalapi.Quality.low_320k,
                "LOSSLESS": tidalapi.Quality.high_lossless,
            }.get(quality, tidalapi.Quality.high_lossless)
        except AttributeError:  # older tidalapi enum names
            pass

    # ── auth ──────────────────────────────────────────────────────────────
    def login_interactive(self) -> bool:
        """Device-link login; prints the link.tidal.com URL. Persists session.

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
            track = self.session.track(track_id)
            urls = self._stream_urls(track)
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
            log.error("No stream URL for track %s: %s", track.id, e)
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
