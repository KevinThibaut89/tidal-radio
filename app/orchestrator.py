"""The station brain: keeps liquidsoap's queue fed with music + DJ breaks."""
import logging
import re
import threading
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from .analysis import analyze_file
from .config import Config
from .db import Database
from .dj import DJ
from .engine import Engine
from .liquidsoap_client import Liquidsoap
from .settings_store import SettingsStore
from .shows import current_show
from .tidal_client import TidalClient
from .tts import TTS

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.tidal = TidalClient(cfg)
        self.engine = Engine(cfg, self.db)
        self.settings = SettingsStore(cfg.data_dir / "settings.json")
        cfg.bind_overrides(self.settings)   # UI edits win over config.yaml
        self.dj = DJ(cfg, self.settings)
        self.tts = TTS(cfg)
        self.ls = Liquidsoap(cfg.get("liquidsoap.host", "127.0.0.1"),
                             int(cfg.get("liquidsoap.port", 1234)))

        self.pushed: deque[dict] = deque(maxlen=20)  # items sent to liquidsoap
        self.tracks_since_break = 0
        self.last_news_hour: int | None = None
        self.break_requested = False
        self.forced_show: dict | None = None
        self.running = False
        self.fatal_error: str | None = None
        self._fetch_failures = 0

    # ── public state for the API ─────────────────────────────────────────
    def status(self) -> dict:
        show = current_show(self.cfg, self.forced_show)
        return {
            "station": self.cfg.get("station.name"),
            "tagline": self.cfg.get("station.tagline"),
            "error": self.fatal_error,
            "tidal_linked": self.tidal.is_linked(),
            "dj_provider": self.dj.active_provider(),
            "quality": self.tidal.quality,
            "tidal_error": self.tidal.last_error,
            "throttled_for": round(self.tidal.throttled_for()),
            "library_tracks": self.db.query("SELECT COUNT(*) AS c FROM tracks")[0]["c"],
            "show": {"id": show.get("id"), "name": show.get("name")},
            "now_playing": self._now_playing(),
            "queue": [self._item_public(i) for i in self.pushed
                      if not i.get("done")][-5:],
            "recent": [dict(r) for r in self.db.recent_plays(10)],
            "cache_gb": round(self.tidal.cache_usage_gb(), 2),
            "liquidsoap": self.ls.alive(),
        }

    def skip(self) -> bool:
        return self.ls.skip()

    def request_break(self):
        self.break_requested = True

    def sync_library_async(self) -> bool:
        """Kick off a favorites sync in the background (used after UI linking)."""
        if not self.tidal.is_linked():
            return False

        def _run():
            try:
                if self.tidal.ensure_login():
                    n = self.tidal.sync_favorites(self.db)
                    log.info("Library sync finished: %d tracks", n)
            except Exception:
                log.exception("Library sync failed")

        threading.Thread(target=_run, daemon=True, name="sync").start()
        return True

    def force_show(self, show_id: str | None) -> bool:
        if show_id is None:
            self.forced_show = None
            return True
        for s in self.cfg.shows:
            if s.get("id") == show_id:
                self.forced_show = s
                return True
        return False

    # ── main loop ────────────────────────────────────────────────────────
    def run(self):
        if not self.tidal.ensure_login():
            raise RuntimeError("Tidal not linked — run `tidal-radio auth` in the container")
        if not self.db.query("SELECT 1 FROM tracks LIMIT 1"):
            log.info("Library empty — running initial sync")
            self.tidal.sync_favorites(self.db)

        if self.cfg.get("analysis.background", True):
            threading.Thread(target=self._analysis_worker, daemon=True,
                             name="analysis").start()

        self.running = True
        queue_ahead = int(self.cfg.get("engine.queue_ahead", 2))
        log.info("Station is on the air")
        while self.running:
            try:
                self._mark_started_items()
                qlen = self.ls.queue_length()
                if qlen < 0:
                    time.sleep(5)   # liquidsoap not up yet
                    continue
                if qlen < queue_ahead:
                    self._enqueue_next()
                else:
                    time.sleep(5)
            except Exception:
                log.exception("Loop error")
                time.sleep(10)

    def _enqueue_next(self):
        show = current_show(self.cfg, self.forced_show)
        track = self.engine.pick_next(show)
        if not track:
            log.warning("No track available — is the library synced?")
            time.sleep(30)
            return

        path = self.tidal.fetch_track(track["id"])
        if path is None:
            # Don't spin: honour any rate-limit backoff, and escalate our own
            # wait so a systemic failure can't turn into a request flood.
            wait = self.tidal.throttled_for()
            if wait <= 0:
                self._fetch_failures += 1
                wait = min(5 * self._fetch_failures, 60)
            else:
                log.info("Tidal backoff active — waiting %.0fs", wait)
            time.sleep(wait)
            return
        self._fetch_failures = 0

        # analyze on demand so sequencing improves as the station runs
        if track.get("bpm") is None:
            feats = analyze_file(path, int(self.cfg.get("analysis.max_seconds", 120)))
            if feats:
                self.db.save_features(track["id"], **feats)
                track.update(feats)

        if self._break_due():
            last_music = next((i for i in reversed(self.pushed)
                               if i["kind"] == "music"), None)
            self._push_break(last_music, track, show)

        if self.ls.push(str(path)):
            self.pushed.append({"kind": "music", "path": str(path), **track})
            self.db.record_play(track["id"], show.get("id"))
            self.engine.note_played(track)
            self.tracks_since_break += 1
            log.info("Queued: %s — %s (%s bpm, %s)", track["artist"], track["title"],
                     track.get("bpm"), track.get("camelot"))
        else:
            time.sleep(5)

    def _break_due(self) -> bool:
        if not self.cfg.get("dj.enabled", True):
            return False
        if self.break_requested:
            return True
        every = int(self.cfg.get("dj.every_n_tracks", 4))
        if self.tracks_since_break >= every:
            return True
        # top-of-hour news break
        tz = ZoneInfo(self.cfg.get("station.timezone", "UTC"))
        now = datetime.now(tz)
        news_minutes = self.cfg.get("dj.news_minutes", [0]) or []
        if any(abs(now.minute - m) <= 2 for m in news_minutes) \
                and self.last_news_hour != now.hour and self.tracks_since_break >= 1:
            return True
        return False

    def _push_break(self, just_played, up_next, show):
        tz = ZoneInfo(self.cfg.get("station.timezone", "UTC"))
        now = datetime.now(tz)
        news_minutes = self.cfg.get("dj.news_minutes", [0]) or []
        include_news = any(abs(now.minute - m) <= 5 for m in news_minutes) \
            and self.last_news_hour != now.hour

        script = self.dj.write_break(just_played, up_next, show, include_news)
        log.info("DJ break%s: %s", " (news)" if include_news else "", script[:120])
        wav = self.tts.synthesize(script)
        if wav and self.ls.push(str(wav)):
            self.pushed.append({"kind": "break", "path": str(wav),
                                "title": "DJ break", "artist": self.cfg.get("station.name"),
                                "script": script})
            if include_news:
                self.last_news_hour = now.hour
        self.tracks_since_break = 0
        self.break_requested = False

    # ── now-playing tracking ─────────────────────────────────────────────
    def _mark_started_items(self):
        """Ask liquidsoap what's on air and mark our pushed items accordingly."""
        uri = self._on_air_uri()
        if not uri:
            return
        seen = False
        for item in reversed(self.pushed):
            if item["path"] in uri and not seen:
                item["on_air"] = True
                seen = True
            elif seen:
                item["on_air"] = False
                item["done"] = True
            else:
                item["on_air"] = False

    def _on_air_uri(self) -> str | None:
        try:
            rids = self.ls._command("request.on_air")
            rid = rids.split()[0] if rids.split() else None
            if not rid:
                return None
            meta = self.ls._command(f"request.metadata {rid}")
            m = re.search(r'(?:initial_uri|filename)="([^"]+)"', meta)
            return m.group(1) if m else None
        except Exception:
            return None

    def _now_playing(self) -> dict | None:
        for item in reversed(self.pushed):
            if item.get("on_air"):
                return self._item_public(item)
        return None

    @staticmethod
    def _item_public(item: dict) -> dict:
        keys = ("kind", "title", "artist", "album", "bpm", "camelot", "script",
                "cover_url", "duration")
        return {k: item.get(k) for k in keys if item.get(k) is not None}

    # ── background analysis ──────────────────────────────────────────────
    def _analysis_worker(self):
        max_s = int(self.cfg.get("analysis.max_seconds", 120))
        while True:
            # Never compete with playback for Tidal's rate limit.
            wait = self.tidal.throttled_for()
            if wait > 0:
                time.sleep(wait + 5)
                continue
            rows = self.db.tracks_without_features(limit=1)
            if not rows:
                time.sleep(300)
                continue
            t = rows[0]
            path = self.tidal.fetch_track(t["id"])
            if path:
                feats = analyze_file(path, max_s)
                if feats:
                    self.db.save_features(t["id"], **feats)
                    log.info("Analyzed %s — %s: %s bpm %s", t["artist"], t["title"],
                             feats["bpm"], feats["camelot"])
                else:
                    # mark as attempted so we don't loop on a broken file
                    self.db.save_features(t["id"], bpm=None, key_idx=None, mode=None,
                                          camelot=None, energy=None)
            time.sleep(10)  # be gentle on CPU and on Tidal
