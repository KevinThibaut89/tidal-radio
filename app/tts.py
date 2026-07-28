"""Text-to-speech for DJ breaks via Piper (local, offline)."""
import logging
import shutil
import subprocess
import time
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)


class TTS:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.breaks_dir.mkdir(parents=True, exist_ok=True)

    def synthesize(self, text: str) -> Path | None:
        """Render text to a normalized WAV, return the path (None on failure)."""
        raw = self.cfg.breaks_dir / f"break-{int(time.time())}.raw.wav"
        out = self.cfg.breaks_dir / f"break-{int(time.time())}.wav"
        try:
            if not self._piper(text, raw):
                return None
            # Loudness-normalize so breaks sit at the same level as music.
            proc = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
                 "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100",
                 str(out)],
                capture_output=True, text=True,
            )
            raw.unlink(missing_ok=True)
            if proc.returncode != 0:
                log.error("loudnorm failed: %s", proc.stderr[-300:])
                return None
            self._cleanup_old()
            return out
        except Exception as e:
            log.error("TTS failed: %s", e)
            return None

    def _piper(self, text: str, out: Path) -> bool:
        voice = self.cfg.get("tts.piper.voice", "en_US-lessac-medium")
        model = self.cfg.voices_dir / f"{voice}.onnx"
        if not model.exists():
            log.error("Piper voice model missing: %s", model)
            return False
        piper_bin = shutil.which("piper") or "/opt/tidal-radio/venv/bin/piper"
        proc = subprocess.run(
            [piper_bin, "--model", str(model), "--output_file", str(out),
             "--length_scale", str(self.cfg.get("tts.piper.length_scale", 1.05))],
            input=text, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            log.error("piper failed: %s", proc.stderr[-300:])
            return False
        return out.exists()

    def _cleanup_old(self, keep: int = 20):
        files = sorted(self.cfg.breaks_dir.glob("break-*.wav"),
                       key=lambda p: p.stat().st_mtime)
        for f in files[:-keep]:
            f.unlink(missing_ok=True)
