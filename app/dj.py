"""The AI DJ: writes talk breaks (Claude API or templates) with weather + news."""
import logging
import os
import random
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from .config import Config

log = logging.getLogger(__name__)

WEATHER_CODES = {
    0: "clear skies", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
    81: "rain showers", 82: "violent rain showers", 95: "a thunderstorm",
    96: "a thunderstorm with hail", 99: "a thunderstorm with hail",
}


class DJ:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ── context gathering ────────────────────────────────────────────────
    def get_weather(self) -> str | None:
        lat = self.cfg.get("weather.latitude")
        lon = self.cfg.get("weather.longitude")
        if lat is None or lon is None:
            return None
        try:
            r = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={"latitude": lat, "longitude": lon,
                        "current": "temperature_2m,weather_code",
                        "daily": "temperature_2m_max,precipitation_probability_max",
                        "forecast_days": 1, "timezone": "auto"},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            cur = data["current"]
            cond = WEATHER_CODES.get(cur.get("weather_code"), "changeable weather")
            txt = f"{round(cur['temperature_2m'])} degrees and {cond}"
            daily = data.get("daily", {})
            if daily.get("temperature_2m_max"):
                txt += f", heading for a high of {round(daily['temperature_2m_max'][0])}"
            if daily.get("precipitation_probability_max"):
                p = daily["precipitation_probability_max"][0]
                if p and p >= 40:
                    txt += f", with a {p} percent chance of rain"
            return txt
        except Exception as e:
            log.warning("Weather fetch failed: %s", e)
            return None

    def get_headlines(self) -> list[str]:
        feeds = self.cfg.get("news.feeds", []) or []
        limit = int(self.cfg.get("news.max_headlines", 3))
        headlines: list[str] = []
        try:
            import feedparser
            for url in feeds:
                parsed = feedparser.parse(url)
                for entry in parsed.entries[:limit]:
                    headlines.append(entry.title)
                if len(headlines) >= limit:
                    break
        except Exception as e:
            log.warning("News fetch failed: %s", e)
        return headlines[:limit]

    # ── script writing ───────────────────────────────────────────────────
    def write_break(self, just_played: dict | None, up_next: dict | None,
                    show: dict, include_news: bool) -> str:
        tz = ZoneInfo(self.cfg.get("station.timezone", "UTC"))
        now = datetime.now(tz)
        weather = self.get_weather()
        headlines = self.get_headlines() if include_news else []

        if os.environ.get("ANTHROPIC_API_KEY"):
            script = self._write_with_claude(just_played, up_next, show, now,
                                             weather, headlines)
            if script:
                return script
        return self._write_template(just_played, up_next, show, now, weather, headlines)

    def _write_with_claude(self, just_played, up_next, show, now,
                           weather, headlines) -> str | None:
        try:
            import anthropic
            client = anthropic.Anthropic()
            station = self.cfg.get("station.name", "the radio")
            persona = self.cfg.get("dj.persona", "A friendly radio host.")
            parts = [f"Station: {station}", f"Show: {show.get('name')}",
                     f"Local time: {now.strftime('%A %H:%M')}"]
            if just_played:
                parts.append(f"Just played: {just_played['title']} by {just_played['artist']}")
            if up_next:
                parts.append(f"Up next: {up_next['title']} by {up_next['artist']}")
            if weather:
                parts.append(f"Weather: {weather}")
            if headlines:
                parts.append("Headlines: " + " | ".join(headlines))

            response = client.messages.create(
                model=self.cfg.get("dj.llm.model", "claude-opus-5"),
                max_tokens=int(self.cfg.get("dj.llm.max_tokens", 600)),
                system=(
                    f"You are the on-air voice of a real radio station. Persona: {persona}\n"
                    "Write ONLY the words the DJ speaks — no stage directions, no quotes, "
                    "no markdown, no emoji. 3 to 6 short sentences, ~20 seconds of speech. "
                    "Mention the station name once. If headlines are given, read them "
                    "briefly and neutrally like a news minute; if weather is given, work it "
                    "in naturally. Always land on introducing the next track."
                ),
                messages=[{"role": "user", "content": "\n".join(parts)}],
            )
            if response.stop_reason == "refusal":
                return None
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            return text or None
        except Exception as e:
            log.warning("Claude DJ script failed, using template: %s", e)
            return None

    def _write_template(self, just_played, up_next, show, now,
                        weather, headlines) -> str:
        station = self.cfg.get("station.name", "the radio")
        bits = []
        if just_played:
            bits.append(random.choice([
                f"That was {just_played['title']} by {just_played['artist']}.",
                f"{just_played['artist']} there, with {just_played['title']}.",
            ]))
        bits.append(f"You're listening to {station}. "
                    f"It's {now.strftime('%H:%M')} on this {now.strftime('%A')}.")
        if weather:
            bits.append(f"Outside right now: {weather}.")
        if headlines:
            bits.append("In the news: " + ". ".join(headlines) + ".")
        if up_next:
            bits.append(random.choice([
                f"Coming up next: {up_next['title']} by {up_next['artist']}.",
                f"Here's {up_next['artist']} with {up_next['title']}.",
            ]))
        return " ".join(bits)
