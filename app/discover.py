"""Discovery: keep the station stocked with music you don't own yet.

A station that only replays your own library is a shuffle, not a radio. This
worker seeds Tidal's recommendation endpoints with your actual taste — your
most-played and favourited artists and tracks — and files the results in the
same database with a non-library `origin`, so the engine can blend them in.

Everything here is bounded and slow on purpose: discovery is a background
nicety, and starving the playback loop of Tidal's rate limit would be a much
worse outcome than a smaller discovery pool.
"""
import logging
import random
import time

from .db import Database

log = logging.getLogger(__name__)

# origins that are *not* the user's own library
DISCOVERED = ("radio", "mix", "similar", "ai")

SUGGESTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["suggestions"],
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["artist", "title", "why"],
                "properties": {
                    "artist": {"type": "string"},
                    "title": {"type": "string"},
                    "why": {"type": "string", "description": "one short clause"},
                },
            },
        }
    },
}


class Discovery:
    def __init__(self, cfg, source, db: Database, settings=None):
        self.cfg = cfg
        self.source = source          # a MusicSource (TidalClient today)
        self.db = db
        self.settings = settings      # SettingsStore, for API keys
        self.last_run = 0.0
        self.last_added = 0
        self.last_ai_note: str | None = None

    # ── public ───────────────────────────────────────────────────────────
    def pool_size(self) -> int:
        placeholders = ",".join("?" * len(DISCOVERED))
        rows = self.db.query(
            f"SELECT COUNT(*) AS c FROM tracks WHERE origin IN ({placeholders})",
            DISCOVERED)
        return rows[0]["c"] if rows else 0

    def due(self) -> bool:
        if not self.cfg.get("discovery.enabled", True):
            return False
        hours = float(self.cfg.get("discovery.refresh_hours", 12))
        if self.pool_size() < int(self.cfg.get("discovery.min_tracks", 150)):
            return True
        return time.time() - self.last_run > hours * 3600

    def run(self) -> int:
        """Expand the discovery pool. Returns how many new tracks were added."""
        if not self.cfg.get("discovery.enabled", True):
            return 0
        self.last_run = time.time()
        seeds = self._seeds()
        if not seeds["artists"] and not seeds["tracks"]:
            log.info("Discovery: no taste to seed from yet — sync your library first")
            return 0

        budget = int(self.cfg.get("discovery.max_new_per_run", 300))
        added = 0
        for fetch in (self._from_ai, self._from_mixes, self._from_artist_radio,
                      self._from_track_radio, self._from_similar_artists):
            if added >= budget:
                break
            try:
                added += fetch(seeds, budget - added)
            except Exception:
                log.exception("Discovery step %s failed", fetch.__name__)
        self.last_added = added
        log.info("Discovery added %d new tracks (pool now %d)", added, self.pool_size())
        return added

    # ── seeds: what you actually listen to ───────────────────────────────
    def _seeds(self) -> dict:
        n = int(self.cfg.get("discovery.seeds", 6))
        artists = self.db.query(
            """SELECT t.artist, COUNT(h.id) AS plays, MAX(t.favorite) AS fav
               FROM tracks t LEFT JOIN history h ON h.track_id = t.id
               WHERE t.origin = 'library' OR t.origin IS NULL
               GROUP BY t.artist ORDER BY fav DESC, plays DESC, RANDOM() LIMIT ?""",
            (n * 3,))
        tracks = self.db.query(
            """SELECT id, title, artist FROM tracks
               WHERE favorite = 1 ORDER BY RANDOM() LIMIT ?""", (n,))
        picked = [a["artist"] for a in artists]
        random.shuffle(picked)
        return {"artists": picked[:n], "tracks": [dict(t) for t in tracks]}

    # ── sources of new music ─────────────────────────────────────────────
    def _from_mixes(self, seeds, budget: int) -> int:
        """Tidal's own personalised mixes — the closest thing to Discover Weekly."""
        session = getattr(self.source, "session", None)
        user = getattr(session, "user", None)
        if user is None or not hasattr(user, "mixes"):
            return 0
        added = 0
        for mix in (self._safe(user.mixes) or [])[:4]:
            if added >= budget:
                break
            title = getattr(mix, "title", "mix")
            items = self._safe(mix.items) or []
            added += self._store(items, "mix", budget - added)
            log.info("Discovery: %s → %d tracks so far", title, added)
            self._breathe()
        return added

    def _from_artist_radio(self, seeds, budget: int) -> int:
        added = 0
        for name in seeds["artists"]:
            if added >= budget:
                break
            artist = self._find_artist(name)
            if artist is None:
                continue
            added += self._store(self._safe(artist.get_radio) or [], "radio", budget - added)
            self._breathe()
        return added

    def _from_track_radio(self, seeds, budget: int) -> int:
        added = 0
        for t in seeds["tracks"]:
            if added >= budget:
                break
            try:
                track = self.source.session.track(t["id"])
                items = self._safe(track.get_track_radio) or []
            except Exception as e:
                log.debug("Track radio for %s failed: %s", t["title"], e)
                continue
            added += self._store(items, "radio", budget - added)
            self._breathe()
        return added

    def _from_similar_artists(self, seeds, budget: int) -> int:
        added = 0
        for name in seeds["artists"]:
            if added >= budget:
                break
            artist = self._find_artist(name)
            if artist is None:
                continue
            for similar in (self._safe(artist.get_similar) or [])[:3]:
                if added >= budget:
                    break
                top = self._safe(lambda: similar.get_top_tracks(10)) or []
                added += self._store(top, "similar", budget - added)
                self._breathe()
        return added

    # ── AI suggestions ───────────────────────────────────────────────────
    def _from_ai(self, seeds, budget: int) -> int:
        """Ask a cheap model for lateral recommendations, then find them on Tidal.

        Tidal's own radios stay close to what you already play; a language model
        will reach across scenes and eras, which is the point of discovery. Runs
        on the small/cheap tier — this is a list of names, not an essay.
        """
        if not self.cfg.get("discovery.ai_enabled", True):
            return 0
        want = min(int(self.cfg.get("discovery.ai_suggestions", 25)), budget)
        if want <= 0:
            return 0

        profile = self._taste_profile()
        if not profile["artists"]:
            return 0
        suggestions = self._ask_model(profile, want)
        if not suggestions:
            return 0

        # Deep cuts count as discovery too: an unheard track by an artist you
        # already love is exactly what a good radio station digs out. Only the
        # tracks you already have are skipped, and _store does that by id.
        deep_cuts = bool(self.cfg.get("discovery.deep_cuts", True))
        known = {r["artist"].lower() for r in
                 self.db.query("SELECT DISTINCT artist FROM tracks")}
        added = 0
        for s in suggestions:
            if added >= budget:
                break
            if not deep_cuts and s["artist"].lower() in known:
                continue
            track = self._find_track(s["artist"], s["title"])
            if track is None:
                continue
            added += self._store([track], "ai", budget - added)
            self._breathe()
        log.info("Discovery: AI suggested %d, %d new tracks found on Tidal",
                 len(suggestions), added)
        return added

    def _taste_profile(self) -> dict:
        rows = self.db.query(
            """SELECT t.artist, COUNT(h.id) AS plays
               FROM tracks t LEFT JOIN history h ON h.track_id = t.id
               WHERE t.origin = 'library' OR t.origin IS NULL
               GROUP BY t.artist ORDER BY plays DESC, RANDOM() LIMIT 25""")
        tempo = self.db.query(
            "SELECT ROUND(AVG(bpm)) AS avg_bpm FROM features WHERE bpm IS NOT NULL")
        disliked = self.db.query(
            """SELECT DISTINCT artist FROM tracks
               WHERE origin IN ('ai','radio','similar','mix') LIMIT 60""")
        return {"artists": [r["artist"] for r in rows],
                "avg_bpm": tempo[0]["avg_bpm"] if tempo and tempo[0]["avg_bpm"] else None,
                "already_suggested": [r["artist"] for r in disliked]}

    def _ask_model(self, profile: dict, want: int) -> list[dict]:
        deep_share = (float(self.cfg.get("discovery.deep_cut_share", 0.3))
                      if self.cfg.get("discovery.deep_cuts", True) else 0.0)
        provider, key, model = self._model_choice()
        if provider is None:
            self.last_ai_note = "no API key set — AI discovery skipped"
            return []
        prompt = (
            f"This listener's favourite artists: {', '.join(profile['artists'])}.\n"
            + (f"Their library averages {profile['avg_bpm']:.0f} BPM.\n"
               if profile["avg_bpm"] else "")
            + (f"Already suggested, do not repeat: {', '.join(profile['already_suggested'][:40])}.\n"
               if profile["already_suggested"] else "")
            + f"\nSuggest {want} specific tracks they would likely love but probably "
              "haven't heard.\n"
            + (f"Roughly {int(deep_share * 100)}% should be deep cuts by artists they "
               "already love — the album tracks and B-sides, not the hits they "
               "obviously know. The rest should be artists new to them.\n"
               if deep_share > 0 else "Favour artists NOT in the lists above.\n")
            + "Reach across scenes, eras and countries rather than picking the obvious "
              "neighbours, but keep the sensibility recognisably theirs. Real, findable "
              "recordings only — never invent a title."
        )
        system = ("You are a music director for a personal radio station with deep "
                  "catalogue knowledge. You suggest real recordings, never invented ones.")
        try:
            if provider == "anthropic":
                import anthropic
                r = anthropic.Anthropic(api_key=key).messages.create(
                    model=model, max_tokens=2000, system=system,
                    output_config={"format": {"type": "json_schema",
                                              "schema": SUGGESTION_SCHEMA}},
                    messages=[{"role": "user", "content": prompt}],
                )
                if r.stop_reason == "refusal":
                    return []
                import json
                text = "".join(b.text for b in r.content if b.type == "text")
                data = json.loads(text)
            else:
                from openai import OpenAI
                r = OpenAI(api_key=key).chat.completions.create(
                    model=model, max_tokens=2000,
                    response_format={"type": "json_schema", "json_schema": {
                        "name": "suggestions", "strict": True,
                        "schema": SUGGESTION_SCHEMA}},
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": prompt}],
                )
                import json
                data = json.loads(r.choices[0].message.content or "{}")
            out = [s for s in data.get("suggestions", [])
                   if s.get("artist") and s.get("title")]
            self.last_ai_note = f"{provider}/{model}: {len(out)} suggestions"
            return out
        except Exception as e:
            log.warning("AI discovery failed (%s/%s): %s", provider, model, e)
            self.last_ai_note = f"failed: {e}"
            return []

    def _model_choice(self) -> tuple[str | None, str | None, str | None]:
        """Pick the cheap model to use, preferring whatever key exists."""
        get = (self.settings.get if self.settings else (lambda k, d=None: None))
        pref = self.cfg.get("discovery.ai_provider", "auto")
        anth, oai = get("anthropic_api_key"), get("openai_api_key")
        if pref == "anthropic" or (pref == "auto" and anth):
            if anth:
                return "anthropic", anth, self.cfg.get("discovery.ai_model_anthropic",
                                                       "claude-haiku-4-5")
        if pref in ("openai", "auto") and oai:
            return "openai", oai, self.cfg.get("discovery.ai_model_openai", "gpt-4.1-nano")
        return None, None, None

    def _find_track(self, artist: str, title: str):
        """Resolve a suggested 'artist – title' to a real track on the service."""
        for query in (f"{artist} {title}", title):
            try:
                results = self.source.session.search(query)
            except Exception as e:
                log.debug("Search failed for %s: %s", query, e)
                return None
            tracks = results.get("tracks") if isinstance(results, dict) else None
            for t in (tracks or [])[:5]:
                got = (getattr(getattr(t, "artist", None), "name", "") or "").lower()
                if artist.lower() in got or got in artist.lower():
                    return t
            self._breathe()
        return None

    # ── helpers ──────────────────────────────────────────────────────────
    def _find_artist(self, name: str):
        try:
            results = self.source.session.search(name, models=None)
        except Exception:
            try:
                results = self.source.session.search(name)
            except Exception as e:
                log.debug("Artist search for %s failed: %s", name, e)
                return None
        artists = (results or {}).get("artists") if isinstance(results, dict) else None
        return artists[0] if artists else None

    def _store(self, tracks, origin: str, budget: int) -> int:
        added = 0
        for t in tracks or []:
            if added >= budget:
                break
            tid = getattr(t, "id", None)
            if tid is None or not getattr(t, "name", None):
                continue
            if self.db.query("SELECT 1 FROM tracks WHERE id = ?", (tid,)):
                continue                      # already known — don't downgrade it
            self.db.upsert_track(
                tid, t.name,
                t.artist.name if getattr(t, "artist", None) else "Unknown",
                t.album.name if getattr(t, "album", None) else None,
                getattr(t, "duration", None), favorite=False,
                cover_url=self.source._cover_url(t), source="tidal", origin=origin)
            added += 1
        return added

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception as e:
            log.debug("Discovery call failed: %s", e)
            return None

    def _breathe(self):
        """Stay well clear of the rate limit that playback depends on."""
        wait = self.source.throttled_for()
        time.sleep(max(wait, float(self.cfg.get("discovery.pause_seconds", 1.5))))
