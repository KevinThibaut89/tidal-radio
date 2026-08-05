import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id          INTEGER PRIMARY KEY,          -- Tidal track id
    title       TEXT NOT NULL,
    artist      TEXT NOT NULL,
    album       TEXT,
    duration    INTEGER,                      -- seconds
    favorite    INTEGER DEFAULT 1,
    added_at    REAL
);
CREATE TABLE IF NOT EXISTS features (
    track_id    INTEGER PRIMARY KEY REFERENCES tracks(id),
    bpm         REAL,
    key_idx     INTEGER,                      -- 0=C .. 11=B
    mode        INTEGER,                      -- 1=major 0=minor
    camelot     TEXT,                         -- e.g. "8A"
    energy      REAL,                         -- 0..1 rough loudness proxy
    analyzed_at REAL
);
CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id    INTEGER REFERENCES tracks(id),
    played_at   REAL,
    show_id     TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_track ON history(track_id, played_at);
"""


class Database:
    """Thin thread-safe wrapper around SQLite."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            # additive migrations — safe to run against an existing database
            for column, decl in (("cover_url", "TEXT"), ("source", "TEXT DEFAULT 'tidal'")):
                try:
                    self._conn.execute(f"ALTER TABLE tracks ADD COLUMN {column} {decl}")
                except sqlite3.OperationalError:
                    pass          # already present
            self._conn.commit()

    def execute(self, sql: str, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ── tracks ────────────────────────────────────────────────────────────
    def upsert_track(self, tid: int, title: str, artist: str, album: str | None,
                     duration: int | None, favorite: bool = True,
                     cover_url: str | None = None, source: str = "tidal"):
        self.execute(
            """INSERT INTO tracks (id, title, artist, album, duration, favorite,
                                   added_at, cover_url, source)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title, artist=excluded.artist,
                 album=excluded.album, duration=excluded.duration,
                 favorite=excluded.favorite,
                 cover_url=COALESCE(excluded.cover_url, tracks.cover_url),
                 source=excluded.source""",
            (tid, title, artist, album, duration, int(favorite), time.time(),
             cover_url, source),
        )

    def tracks_without_features(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.query(
            """SELECT t.* FROM tracks t LEFT JOIN features f ON f.track_id = t.id
               WHERE f.track_id IS NULL ORDER BY RANDOM() LIMIT ?""",
            (limit,),
        )

    def save_features(self, track_id: int, bpm: float, key_idx: int, mode: int,
                      camelot: str, energy: float):
        self.execute(
            """INSERT INTO features (track_id, bpm, key_idx, mode, camelot, energy, analyzed_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(track_id) DO UPDATE SET
                 bpm=excluded.bpm, key_idx=excluded.key_idx, mode=excluded.mode,
                 camelot=excluded.camelot, energy=excluded.energy,
                 analyzed_at=excluded.analyzed_at""",
            (track_id, bpm, key_idx, mode, camelot, energy, time.time()),
        )

    def candidates(self) -> list[sqlite3.Row]:
        """All tracks joined with features (features may be NULL)."""
        return self.query(
            """SELECT t.*, f.bpm, f.key_idx, f.mode, f.camelot, f.energy
               FROM tracks t LEFT JOIN features f ON f.track_id = t.id"""
        )

    # ── history ───────────────────────────────────────────────────────────
    def record_play(self, track_id: int, show_id: str | None):
        self.execute("INSERT INTO history (track_id, played_at, show_id) VALUES (?,?,?)",
                     (track_id, time.time(), show_id))

    def recent_plays(self, n: int = 50) -> list[sqlite3.Row]:
        return self.query(
            """SELECT h.*, t.title, t.artist, t.album, t.cover_url FROM history h
               JOIN tracks t ON t.id = h.track_id
               ORDER BY h.played_at DESC LIMIT ?""", (n,))

    def last_played_at(self, track_id: int) -> float | None:
        rows = self.query("SELECT MAX(played_at) AS ts FROM history WHERE track_id=?",
                          (track_id,))
        return rows[0]["ts"] if rows and rows[0]["ts"] else None

    def play_count(self, track_id: int) -> int:
        return self.query("SELECT COUNT(*) AS c FROM history WHERE track_id=?",
                          (track_id,))[0]["c"]
