"""Runtime settings written from the control UI.

Kept separate from /etc/tidal-radio/secrets.env because that file is read by
systemd at service start (a change needs a restart) and is root-owned, while
this store is owned by the `radio` service user and re-read on every use — so
pasting an API key in the UI takes effect immediately.

Environment variables still win, so a key set in secrets.env keeps working.
"""
import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# setting name -> environment variable it shadows
ENV_FALLBACK = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
}
SECRET_KEYS = set(ENV_FALLBACK)


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text())
        except Exception as e:
            log.warning("Could not read %s: %s", self.path, e)
            self._data = {}

    def get(self, key: str, default=None):
        with self._lock:
            value = self._data.get(key)
        if value:
            return value
        env = ENV_FALLBACK.get(key)
        if env and os.environ.get(env):
            return os.environ[env]
        return default

    def set_many(self, values: dict):
        with self._lock:
            for k, v in values.items():
                if v is None or v == "":
                    self._data.pop(k, None)   # empty clears the setting
                else:
                    self._data[k] = v
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            tmp.chmod(0o600)                  # secrets live here
            tmp.replace(self.path)

    # ── config overrides (set from the control UI) ───────────────────────
    def config_override(self, dotted: str):
        with self._lock:
            return (self._data.get("config_overrides") or {}).get(dotted)

    def all_config_overrides(self) -> dict:
        with self._lock:
            return dict(self._data.get("config_overrides") or {})

    def set_config_overrides(self, values: dict):
        with self._lock:
            current = dict(self._data.get("config_overrides") or {})
            for k, v in values.items():
                if v is None:
                    current.pop(k, None)   # None resets to the config.yaml value
                else:
                    current[k] = v
            self._data["config_overrides"] = current
        self.set_many({})                  # persist through the same atomic write

    def public(self) -> dict:
        """Settings safe to show in the UI — secrets reduced to a set/unset flag."""
        with self._lock:
            out = {k: v for k, v in self._data.items() if k not in SECRET_KEYS}
        for key, env in ENV_FALLBACK.items():
            from_env = bool(os.environ.get(env))
            out[f"{key}_set"] = bool(self.get(key))
            out[f"{key}_from_env"] = from_env and not self._data.get(key)
        return out
