import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = os.environ.get("TIDAL_RADIO_CONFIG", "/etc/tidal-radio/config.yaml")


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path
    generated_shows: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        p = Path(path or DEFAULT_CONFIG_PATH)
        with open(p) as f:
            raw = yaml.safe_load(f) or {}
        cfg = cls(raw=raw, path=p)
        gen = p.parent / "shows.generated.yaml"
        if gen.exists():
            with open(gen) as f:
                cfg.generated_shows = (yaml.safe_load(f) or {}).get("shows", [])
        return cfg

    def get(self, dotted: str, default=None):
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def data_dir(self) -> Path:
        return Path(self.get("paths.data", "/var/lib/tidal-radio"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "radio.db"

    @property
    def session_path(self) -> Path:
        return self.data_dir / "tidal-session.json"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def breaks_dir(self) -> Path:
        return self.data_dir / "breaks"

    @property
    def voices_dir(self) -> Path:
        return self.data_dir / "voices"

    @property
    def shows(self) -> list[dict]:
        return (self.raw.get("shows") or []) + self.generated_shows
