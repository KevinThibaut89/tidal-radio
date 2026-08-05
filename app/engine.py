"""Track selection: tempo walk + harmonic mixing + taste + no-repeat rules."""
import logging
import random
import time

from .analysis import camelot_distance
from .config import Config
from .db import Database

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.recent_artists: list[str] = []   # most recent last
        self.last_features: dict | None = None

    def pick_next(self, show: dict) -> dict | None:
        """Pick the next track as a dict row, honoring show constraints."""
        candidates = [dict(r) for r in self.db.candidates()]
        if not candidates:
            return None

        now = time.time()
        cooldown = float(self.cfg.get("engine.track_cooldown_hours", 20)) * 3600
        sep = int(self.cfg.get("engine.artist_separation", 6))
        blocked_artists = set(a.lower() for a in self.recent_artists[-sep:])

        bpm_lo, bpm_hi = show.get("bpm", [0, 999])

        pool = []
        for c in candidates:
            last = self.db.last_played_at(c["id"])
            if last and now - last < cooldown:
                continue
            if c["artist"].lower() in blocked_artists:
                continue
            if c["bpm"] is not None and not (bpm_lo - 8 <= c["bpm"] <= bpm_hi + 8):
                continue
            pool.append(c)

        if not pool:
            # Constraints can't be met — usually a small library. Relax to the
            # least-recently-played half rather than anything at all, so a tiny
            # library still rotates instead of repeating the same few tracks.
            ordered = sorted(candidates, key=lambda c: self.db.last_played_at(c["id"]) or 0)
            pool = ordered[: max(1, len(ordered) // 2)]
            if len(candidates) < 25:
                log.warning(
                    "Only %d tracks available — the station will repeat. "
                    "Run `tidal-radio sync` to import playlists and albums.",
                    len(candidates))

        scored = [(self._score(c, show), c) for c in pool]
        scored.sort(key=lambda x: x[0], reverse=True)
        # weighted pick among the top few so the station isn't deterministic
        top = scored[: max(3, len(scored) // 10)]
        weights = [max(s, 0.01) for s, _ in top]
        choice = random.choices([c for _, c in top], weights=weights, k=1)[0]
        return choice

    def _score(self, c: dict, show: dict) -> float:
        w_tempo = float(self.cfg.get("engine.tempo_weight", 1.0))
        w_key = float(self.cfg.get("engine.harmonic_weight", 1.0))
        w_taste = float(self.cfg.get("engine.taste_weight", 0.8))
        jitter = float(self.cfg.get("engine.randomness", 0.35))
        walk = float(self.cfg.get("engine.bpm_walk", 12))

        score = 0.0

        # tempo continuity relative to what's playing (with energy direction)
        if self.last_features and self.last_features.get("bpm") and c.get("bpm"):
            target = self.last_features["bpm"]
            energy = show.get("energy", "flat")
            if energy == "rising":
                target += walk * 0.4
            elif energy == "falling":
                target -= walk * 0.4
            diff = abs(c["bpm"] - target)
            score += w_tempo * max(0.0, 1.0 - diff / max(walk, 1))

        # harmonic compatibility on the Camelot wheel
        if self.last_features:
            d = camelot_distance(self.last_features.get("camelot"), c.get("camelot"))
            score += w_key * max(0.0, 1.0 - d / 4.0)

        # taste: favorites, mild boost for un(der)played tracks
        if c.get("favorite"):
            score += w_taste * 0.5
        plays = self.db.play_count(c["id"])
        score += w_taste * 0.5 / (1 + plays)

        score += random.uniform(0, jitter)
        return score

    def note_played(self, track: dict):
        self.recent_artists.append(track["artist"])
        self.recent_artists = self.recent_artists[-40:]
        if track.get("bpm") is not None:
            self.last_features = {"bpm": track["bpm"], "camelot": track.get("camelot")}
