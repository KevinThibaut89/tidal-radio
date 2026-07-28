"""Thematic shows: schedule resolution + auto-generation from taste."""
import logging
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml

from .config import Config
from .db import Database

log = logging.getLogger(__name__)

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# name, bpm range, preferred day/time slot for generated shows
TEMPO_BANDS = [
    ("slow", 0, 95),
    ("mid", 95, 120),
    ("upbeat", 120, 145),
    ("fast", 145, 999),
]


def current_show(cfg: Config, forced: dict | None = None) -> dict:
    """Resolve the show active right now (or the forced one)."""
    if forced:
        return forced
    tz = ZoneInfo(cfg.get("station.timezone", "UTC"))
    now = datetime.now(tz)
    day = DAY_KEYS[now.weekday()]
    minutes = now.hour * 60 + now.minute

    for show in cfg.shows:
        days = show.get("days", DAY_KEYS)
        if day not in days:
            continue
        start = _to_minutes(show.get("start", "00:00"))
        end = _to_minutes(show.get("end", "24:00"))
        if start <= end:
            if start <= minutes < end:
                return show
        else:  # window crosses midnight (e.g. 22:00-01:00)
            if minutes >= start or minutes < end:
                return show

    ff = cfg.get("freeform", {}) or {}
    return {"id": "freeform", "name": ff.get("name", "Freeform"),
            "bpm": ff.get("bpm", [60, 160]), "energy": ff.get("energy", "flat")}


def _to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def generate_shows(cfg: Config, db: Database) -> list[dict]:
    """Cluster the library into thematic shows and write shows.generated.yaml.

    Clustering is deliberately simple and transparent: top artists × tempo
    bands. If ANTHROPIC_API_KEY is set, Claude names the shows; otherwise
    template names are used.
    """
    rows = [dict(r) for r in db.candidates()]
    if not rows:
        log.warning("Library empty — sync first")
        return []

    bands: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("bpm") is None:
            continue
        for name, lo, hi in TEMPO_BANDS:
            if lo <= r["bpm"] < hi:
                bands[name].append(r)
                break

    shows: list[dict] = []
    slots = [(["sat"], "10:00", "13:00"), (["sun"], "16:00", "19:00"),
             (["wed"], "20:00", "22:00"), (["fri"], "18:00", "20:00")]
    for i, (band, tracks) in enumerate(sorted(bands.items(), key=lambda kv: -len(kv[1]))):
        if len(tracks) < 10:
            continue
        top_artists = [a for a, _ in Counter(t["artist"] for t in tracks).most_common(5)]
        lo = min(t["bpm"] for t in tracks)
        hi = max(t["bpm"] for t in tracks)
        days, start, end = slots[i % len(slots)]
        shows.append({
            "id": f"gen-{band}",
            "name": f"{band.title()} Selections",   # renamed by LLM below if possible
            "days": days, "start": start, "end": end,
            "bpm": [int(lo), int(hi) + 1],
            "energy": "flat",
            "seed_artists": top_artists,
            "generated": True,
        })

    _name_shows_with_llm(cfg, shows)

    out = cfg.path.parent / "shows.generated.yaml"
    with open(out, "w") as f:
        yaml.safe_dump({"shows": shows}, f, sort_keys=False, allow_unicode=True)
    log.info("Wrote %d generated shows to %s", len(shows), out)
    return shows


def _name_shows_with_llm(cfg: Config, shows: list[dict]):
    import os
    if not os.environ.get("ANTHROPIC_API_KEY") or not shows:
        return
    try:
        import anthropic
        client = anthropic.Anthropic()
        desc = "\n".join(
            f"- id={s['id']}: bpm {s['bpm'][0]}-{s['bpm'][1]}, "
            f"top artists: {', '.join(s.get('seed_artists', [])[:5])}, "
            f"slot: {'/'.join(s['days'])} {s['start']}-{s['end']}"
            for s in shows
        )
        response = client.messages.create(
            model=cfg.get("dj.llm.model", "claude-opus-5"),
            max_tokens=1000,
            system="You name radio shows. Reply with one line per show, "
                   "format `id: Show Name`, nothing else. Names should be witty, "
                   "specific to the music described, and radio-worthy.",
            messages=[{"role": "user", "content": f"Name these shows for a personal "
                       f"radio station:\n{desc}"}],
        )
        if response.stop_reason == "refusal":
            return
        text = "".join(b.text for b in response.content if b.type == "text")
        names = {}
        for line in text.strip().splitlines():
            if ":" in line:
                sid, name = line.split(":", 1)
                names[sid.strip().lstrip("-• ")] = name.strip()
        for s in shows:
            if s["id"] in names:
                s["name"] = names[s["id"]]
    except Exception as e:
        log.warning("LLM show naming skipped: %s", e)
