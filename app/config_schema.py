"""Declarative description of everything the control UI may change.

Edits are stored as *overrides* in the runtime settings store rather than
rewritten into config.yaml — that keeps the user's commented YAML intact and,
because most code calls Config.get() at the point of use, lets the majority of
settings apply live without a restart.

`live: False` marks a setting that is only read at startup (or by another
process), so the UI must tell the user a restart is needed.
"""

# Model ids offered per provider. Anthropic ids are exact — never date-suffixed.
ANTHROPIC_MODELS = [
    ("claude-opus-5", "Claude Opus 5 — best all-round"),
    ("claude-sonnet-5", "Claude Sonnet 5 — faster, cheaper"),
    ("claude-haiku-4-5", "Claude Haiku 4.5 — fastest, cheapest"),
    ("claude-fable-5", "Claude Fable 5 — most capable, premium price"),
]
# Small/cheap tiers — discovery only needs a list of names, not prose.
ANTHROPIC_CHEAP = [
    ("claude-haiku-4-5", "Claude Haiku 4.5 — cheapest"),
    ("claude-sonnet-5", "Claude Sonnet 5 — broader knowledge"),
]
OPENAI_CHEAP = [
    ("gpt-4.1-nano", "GPT-4.1 nano — cheapest"),
    ("gpt-4o-mini", "GPT-4o mini"),
    ("gpt-4.1-mini", "GPT-4.1 mini"),
]
OPENAI_MODELS = [
    ("gpt-4o-mini", "GPT-4o mini — fast and cheap"),
    ("gpt-4o", "GPT-4o — stronger writing"),
    ("gpt-4.1", "GPT-4.1"),
    ("gpt-4.1-mini", "GPT-4.1 mini"),
]

# group -> list of settings
SCHEMA: list[dict] = [
    {"group": "Station", "icon": "📻", "settings": [
        {"key": "station.name", "label": "Station name", "control": "text",
         "help": "The DJ says this on air.", "live": True},
        {"key": "station.tagline", "label": "Tagline", "control": "text", "live": True},
        {"key": "station.timezone", "label": "Timezone", "control": "text",
         "help": "IANA name, e.g. Europe/Brussels. Drives show schedules and the DJ clock.",
         "live": True},
    ]},
    {"group": "Music source", "icon": "🎵", "settings": [
        {"key": "source.provider", "label": "Source", "control": "select",
         "options": [("tidal", "Tidal"), ("spotify", "Spotify")],
         "help": "Where tracks and library come from.", "live": False},
        {"key": "tidal.quality", "label": "Tidal quality", "control": "select",
         "options": [("LOW", "Low (96k)"), ("HIGH", "High (320k)"),
                     ("LOSSLESS", "Lossless — needs PKCE login"),
                     ("HI_RES_LOSSLESS", "Hi-Res — needs PKCE login")],
         "help": "Device-link logins are only authorised up to High.", "live": True},
        {"key": "cache.max_gb", "label": "Audio cache (GB)", "control": "number",
         "min": 1, "max": 500, "step": 1,
         "help": "Least-recently-played files are evicted above this.", "live": True},
    ]},
    {"group": "Sequencing", "icon": "🎚", "settings": [
        {"key": "engine.bpm_walk", "label": "Max BPM step", "control": "slider",
         "min": 2, "max": 40, "step": 1,
         "help": "How far tempo may jump between tracks. Lower = smoother sets.", "live": True},
        {"key": "engine.harmonic_weight", "label": "Harmonic mixing", "control": "slider",
         "min": 0, "max": 2, "step": 0.1,
         "help": "Weight given to key compatibility on the Camelot wheel.", "live": True},
        {"key": "engine.tempo_weight", "label": "Tempo continuity", "control": "slider",
         "min": 0, "max": 2, "step": 0.1, "live": True},
        {"key": "engine.taste_weight", "label": "Taste weighting", "control": "slider",
         "min": 0, "max": 2, "step": 0.1,
         "help": "Favours your favourites and under-played tracks.", "live": True},
        {"key": "engine.randomness", "label": "Randomness", "control": "slider",
         "min": 0, "max": 1, "step": 0.05,
         "help": "0 = predictable, 1 = chaotic.", "live": True},
        {"key": "engine.artist_separation", "label": "Tracks between same artist",
         "control": "number", "min": 0, "max": 50, "step": 1, "live": True},
        {"key": "engine.track_cooldown_hours", "label": "Track cooldown (hours)",
         "control": "number", "min": 0, "max": 336, "step": 1, "live": True},
        {"key": "engine.queue_ahead", "label": "Queue depth", "control": "number",
         "min": 1, "max": 10, "step": 1,
         "help": "How many items to keep queued ahead of the stream.", "live": True},
    ]},
    {"group": "Discovery", "icon": "🧭", "settings": [
        {"key": "discovery.enabled", "label": "Find new music", "control": "toggle",
         "help": "Keeps stocking the station with tracks outside your library.",
         "live": True},
        {"key": "engine.discovery_ratio", "label": "How much is new", "control": "slider",
         "min": 0, "max": 1, "step": 0.05,
         "help": "0 = only your library, 1 = only discoveries. 0.35 is a good radio feel.",
         "live": True},
        {"key": "discovery.ai_enabled", "label": "AI suggestions", "control": "toggle",
         "help": "Ask a cheap model for lateral picks Tidal's own radios miss.",
         "live": True},
        {"key": "discovery.ai_provider", "label": "Suggested by", "control": "select",
         "options": [("auto", "Auto — whichever key is set"), ("anthropic", "Claude"),
                     ("openai", "OpenAI")], "live": True},
        {"key": "discovery.ai_model_anthropic", "label": "Claude model (discovery)",
         "control": "select", "options": ANTHROPIC_CHEAP, "allow_custom": True,
         "help": "A small model is plenty — it's returning a list of names.", "live": True},
        {"key": "discovery.ai_model_openai", "label": "OpenAI model (discovery)",
         "control": "select", "options": OPENAI_CHEAP, "allow_custom": True,
         "live": True},
        {"key": "discovery.ai_suggestions", "label": "Suggestions per run",
         "control": "number", "min": 5, "max": 100, "step": 5, "live": True},
        {"key": "discovery.refresh_hours", "label": "Look for more every (hours)",
         "control": "number", "min": 1, "max": 168, "step": 1, "live": True},
        {"key": "discovery.min_tracks", "label": "Keep at least (tracks)",
         "control": "number", "min": 0, "max": 5000, "step": 50, "live": True},
    ]},
    {"group": "AI DJ", "icon": "🎙", "settings": [
        {"key": "dj.enabled", "label": "DJ breaks", "control": "toggle", "live": True},
        {"key": "dj.every_n_tracks", "label": "Break every N tracks", "control": "number",
         "min": 1, "max": 30, "step": 1, "live": True},
        {"key": "dj.provider", "label": "Script writer", "control": "select",
         "options": [("auto", "Auto — whichever key is set"), ("anthropic", "Claude (Anthropic)"),
                     ("openai", "OpenAI"), ("template", "Templates only (no AI)")],
         "live": True},
        {"key": "dj.llm.model", "label": "Claude model", "control": "select",
         "options": ANTHROPIC_MODELS, "allow_custom": True, "live": True},
        {"key": "dj.openai.model", "label": "OpenAI model", "control": "select",
         "options": OPENAI_MODELS, "allow_custom": True, "live": True},
        {"key": "dj.persona", "label": "DJ persona", "control": "textarea",
         "help": "Describe the voice: tone, style, what they talk about.", "live": True},
        {"key": "dj.llm.max_tokens", "label": "Max script length (tokens)",
         "control": "number", "min": 100, "max": 4000, "step": 50, "live": True},
    ]},
    {"group": "Voice", "icon": "🗣", "settings": [
        {"key": "tts.piper.voice", "label": "Piper voice", "control": "text",
         "help": "Model name in the voices directory.", "live": True},
        {"key": "tts.piper.length_scale", "label": "Speaking speed", "control": "slider",
         "min": 0.6, "max": 1.6, "step": 0.05,
         "help": "Higher is slower. ~1.05 sounds like radio delivery.", "live": True},
    ]},
    {"group": "Weather & news", "icon": "🌤", "settings": [
        {"key": "weather.latitude", "label": "Latitude", "control": "number",
         "min": -90, "max": 90, "step": 0.01, "live": True},
        {"key": "weather.longitude", "label": "Longitude", "control": "number",
         "min": -180, "max": 180, "step": 0.01, "live": True},
        {"key": "news.feeds", "label": "RSS feeds", "control": "list",
         "help": "One URL per line. Headlines are read in news breaks.", "live": True},
        {"key": "news.max_headlines", "label": "Headlines per break", "control": "number",
         "min": 1, "max": 10, "step": 1, "live": True},
    ]},
    {"group": "Analysis", "icon": "📊", "settings": [
        {"key": "analysis.max_seconds", "label": "Seconds analysed per track",
         "control": "number", "min": 30, "max": 600, "step": 10,
         "help": "More is more accurate and much slower.", "live": True},
        {"key": "analysis.background", "label": "Analyse in background",
         "control": "toggle", "live": False},
    ]},
]

# dotted key -> setting definition
BY_KEY: dict[str, dict] = {
    s["key"]: {**s, "group": g["group"]}
    for g in SCHEMA for s in g["settings"]
}


def coerce(key: str, value):
    """Validate and coerce an incoming value against the schema."""
    spec = BY_KEY.get(key)
    if spec is None:
        raise KeyError(f"unknown setting: {key}")
    control = spec["control"]

    if control == "toggle":
        return bool(value)
    if control in ("number", "slider"):
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number")
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and num < lo:
            raise ValueError(f"{key} must be at least {lo}")
        if hi is not None and num > hi:
            raise ValueError(f"{key} must be at most {hi}")
        return int(num) if float(spec.get("step", 1)).is_integer() else num
    if control == "list":
        if isinstance(value, str):
            value = [line.strip() for line in value.splitlines()]
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list")
        return [str(v).strip() for v in value if str(v).strip()]
    if control == "select" and not spec.get("allow_custom"):
        valid = [opt[0] for opt in spec.get("options", [])]
        if valid and str(value) not in valid:
            raise ValueError(f"{key} must be one of: {', '.join(valid)}")
    return str(value)


def describe() -> list[dict]:
    """Schema for the UI, with option tuples flattened to objects."""
    out = []
    for group in SCHEMA:
        settings = []
        for s in group["settings"]:
            item = {k: v for k, v in s.items() if k != "options"}
            if "options" in s:
                item["options"] = [{"value": v, "label": lbl} for v, lbl in s["options"]]
            settings.append(item)
        out.append({"group": group["group"], "icon": group.get("icon"),
                    "settings": settings})
    return out
