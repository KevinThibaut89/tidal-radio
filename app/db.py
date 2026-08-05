import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id          TEXT PRIMARY KEY,             -- numeric for Tidal, base62 for Spotify
    title       TEXT NOT NULL,
    artist      TEXT NOT NULL,
    album       TEXT,
    duration    INTEGER,                      -- seconds
    favorite    INTEGER DEFAULT 1,
    added_at    REAL
);
CREATE TABLE IF NOT EXISTS features (
    track_id    TEXT PRIMARY KEY REFERENCES tracks(id),
    bpm         REAL,
    key_idx     INTEGER,                      -- 0=C .. 11=B
    mode        INTEGER,                      -- 1=major 0=minor
    camelot     TEXT,                         -- e.g. "8A"
    energy      REAL,                         -- 0..1 rough loudness proxy
    analyzed_at REAL
);
CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id    TEXT REFERENCES tracks(id),
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
            for column, decl in (("cover_url", "TEXT"), ("source", "TEXT DEFAULT 'tidal'"),
                                 ("origin", "TEXT DEFAULT 'library'")):
                try:
                    self._conn.execute(f"ALTER TABLE tracks ADD COLUMN {column} {decl}")
                except sqlite3.OperationalError:
                    pass          # already present
            self._widen_ids()
            self._conn.commit()

    def _widen_ids(self):
        """Track ids must be TEXT: Tidal's are numeric but Spotify's are base62.

        SQLite can't ALTER a PRIMARY KEY, and an INTEGER PRIMARY KEY is a rowid
        alias that rejects a string outright, so the tables are rebuilt once.
        Existing integer ids survive as their text form.
        """
        row = self._conn.execute(
            "SELECT type FROM pragma_table_info('tracks') WHERE name='id'").fetchone()
        if row is None or (row[0] or "").upper() == "TEXT":
            return                                   # fresh db, or already migrated
        self._conn.executescript("""
            PRAGMA foreign_keys=off;
            CREATE TABLE tracks_new (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, artist TEXT NOT NULL,
                album TEXT, duration INTEGER, favorite INTEGER DEFAULT 1,
                added_at REAL, cover_url TEXT, source TEXT DEFAULT 'tidal',
                origin TEXT DEFAULT 'library');
            INSERT INTO tracks_new SELECT CAST(id AS TEXT), title, artist, album,
                duration, favorite, added_at, cover_url, source, origin FROM tracks;
            DROP TABLE tracks;
            ALTER TABLE tracks_new RENAME TO tracks;

            CREATE TABLE features_new (
                track_id TEXT PRIMARY KEY REFERENCES tracks(id), bpm REAL,
                key_idx INTEGER, mode INTEGER, camelot TEXT, energy REAL,
                analyzed_at REAL);
            INSERT INTO features_new SELECT CAST(track_id AS TEXT), bpm, key_idx,
                mode, camelot, energy, analyzed_at FROM features;
            DROP TABLE features;
            ALTER TABLE features_new RENAME TO features;

            CREATE TABLE history_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT, track_id TEXT REFERENCES tracks(id),
                played_at REAL, show_id TEXT);
            INSERT INTO history_new (id, track_id, played_at, show_id)
                SELECT id, CAST(track_id AS TEXT), played_at, show_id FROM history;
            DROP TABLE history;
            ALTER TABLE history_new RENAME TO history;
            CREATE INDEX IF NOT EXISTS idx_history_track ON history(track_id, played_at);
            PRAGMA foreign_keys=on;
        """)
        log.info("Migrated track ids to TEXT so non-numeric sources can be stored")

    def execute(self, sql: str, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ── tracks ────────────────────────────────────────────────────────────
    def upsert_track(self, tid: str, title: str, artist: str, album: str | None,
                     duration: int | None, favorite: bool = True,
                     cover_url: str | None = None, source: str = "tidal",
                     origin: str = "library"):
        self.execute(
            """INSERT INTO tracks (id, title, artist, album, duration, favorite,
                                   added_at, cover_url, source, origin)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title, artist=excluded.artist,
                 album=excluded.album, duration=excluded.duration,
                 favorite=excluded.favorite,
                 cover_url=COALESCE(excluded.cover_url, tracks.cover_url),
                 source=excluded.source,
                 -- a track that turns up in your library outranks a discovery
                 origin=CASE WHEN excluded.origin='library' THEN 'library'
                             ELSE tracks.origin END""",
            (tid, title, artist, album, duration, int(favorite), time.time(),
             cover_url, source, origin),
        )

    def tracks_without_features(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.query(
            """SELECT t.* FROM tracks t LEFT JOIN features f ON f.track_id = t.id
               WHERE f.track_id IS NULL ORDER BY RANDOM() LIMIT ?""",
            (limit,),
        )

    def save_features(self, track_id: str, bpm: float, key_idx: int, mode: int,
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
    def record_play(self, track_id: str, show_id: str | None):
        self.execute("INSERT INTO history (track_id, played_at, show_id) VALUES (?,?,?)",
                     (track_id, time.time(), show_id))

    def recent_plays(self, n: int = 50) -> list[sqlite3.Row]:
        return self.query(
            """SELECT h.*, t.title, t.artist, t.album, t.cover_url FROM history h
               JOIN tracks t ON t.id = h.track_id
               ORDER BY h.played_at DESC LIMIT ?""", (n,))

    def last_played_at(self, track_id: str) -> float | None:
        rows = self.query("SELECT MAX(played_at) AS ts FROM history WHERE track_id=?",
                          (track_id,))
        return rows[0]["ts"] if rows and rows[0]["ts"] else None

    def play_count(self, track_id: str) -> int:
        return self.query("SELECT COUNT(*) AS c FROM history WHERE track_id=?",
                          (track_id,))[0]["c"]
