"""Audio analysis: tempo, musical key (Krumhansl-Schmuckler), Camelot code.

librosa is imported lazily — it is heavy and only needed on the analysis path.
"""
import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# Krumhansl-Kessler key profiles
MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Camelot wheel: (pitch class, mode) -> code. mode 1 = major (B side), 0 = minor (A side)
CAMELOT_MAJOR = {0: "8B", 1: "3B", 2: "10B", 3: "5B", 4: "12B", 5: "7B",
                 6: "2B", 7: "9B", 8: "4B", 9: "11B", 10: "6B", 11: "1B"}
CAMELOT_MINOR = {0: "5A", 1: "12A", 2: "7A", 3: "2A", 4: "9A", 5: "4A",
                 6: "11A", 7: "6A", 8: "1A", 9: "8A", 10: "3A", 11: "10A"}


def analyze_file(path: Path, max_seconds: int = 120) -> dict | None:
    """Return {'bpm', 'key_idx', 'mode', 'camelot', 'energy'} or None."""
    try:
        import librosa
    except ImportError:
        log.error("librosa not installed — cannot analyze")
        return None
    try:
        y, sr = librosa.load(path, sr=22050, mono=True, duration=max_seconds)
        if y.size == 0:
            return None

        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(np.atleast_1d(tempo)[0])
        # Fold implausible values into the common radio range.
        while bpm and bpm < 60:
            bpm *= 2
        while bpm > 190:
            bpm /= 2

        chroma = librosa.feature.chroma_cqt(y=y, sr=sr).mean(axis=1)
        key_idx, mode = _estimate_key(chroma)
        camelot = (CAMELOT_MAJOR if mode else CAMELOT_MINOR)[key_idx]

        rms = librosa.feature.rms(y=y).mean()
        energy = float(np.clip(rms * 10, 0.0, 1.0))

        return {"bpm": round(bpm, 1), "key_idx": key_idx, "mode": mode,
                "camelot": camelot, "energy": round(energy, 3)}
    except Exception as e:
        log.error("Analysis failed for %s: %s", path, e)
        return None


def _estimate_key(chroma: np.ndarray) -> tuple[int, int]:
    best = (-2.0, 0, 1)
    for shift in range(12):
        rotated = np.roll(chroma, -shift)
        for mode, profile in ((1, MAJOR_PROFILE), (0, MINOR_PROFILE)):
            r = np.corrcoef(rotated, profile)[0, 1]
            if r > best[0]:
                best = (r, shift, mode)
    return best[1], best[2]


def key_name(key_idx: int, mode: int) -> str:
    return f"{NOTE_NAMES[key_idx]} {'major' if mode else 'minor'}"


def camelot_distance(a: str | None, b: str | None) -> int:
    """0 = same key, 1 = mixable neighbor (±1 hour or relative maj/min),
    larger = clashier. Unknown keys get a neutral middle distance."""
    if not a or not b:
        return 3
    ha, sa = int(a[:-1]), a[-1]
    hb, sb = int(b[:-1]), b[-1]
    hour_dist = min(abs(ha - hb), 12 - abs(ha - hb))
    if sa == sb:
        return hour_dist
    return 1 if hour_dist == 0 else hour_dist + 2
