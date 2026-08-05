"""Control API + mini web player."""
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from . import config_schema

if TYPE_CHECKING:  # avoid pulling the analysis stack in just for a type hint
    from .orchestrator import Orchestrator

TEMPLATE = Path(__file__).parent / "templates" / "index.html"


def create_app(orch: "Orchestrator") -> FastAPI:
    app = FastAPI(title="tidal-radio", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def player():
        name = orch.cfg.get("station.name", "Tidal Radio")
        return TEMPLATE.read_text().replace("__NAME__", name)

    @app.get("/status")
    def status():
        return orch.status()

    @app.post("/skip")
    def skip():
        if not orch.skip():
            raise HTTPException(502, "liquidsoap unreachable")
        return {"ok": True}

    @app.post("/break")
    def dj_break():
        orch.request_break()
        return {"ok": True, "note": "break will play after the current track"}

    @app.get("/shows")
    def shows():
        return {"shows": orch.cfg.shows, "forced": orch.forced_show}

    # ── setup: Tidal linking ─────────────────────────────────────────────
    @app.post("/tidal/link")
    def tidal_link():
        """Start a device-link login; returns the URL to open and approve."""
        try:
            return orch.tidal.start_device_link()
        except Exception as e:
            raise HTTPException(502, f"could not start Tidal linking: {e}")

    @app.get("/tidal/status")
    def tidal_status():
        state = orch.tidal.link_status()
        state["linked"] = orch.tidal.is_linked()
        return state

    @app.post("/tidal/sync")
    def tidal_sync():
        if not orch.sync_library_async():
            raise HTTPException(409, "Tidal is not linked yet")
        return {"ok": True, "note": "sync running in the background"}

    # ── configuration ────────────────────────────────────────────────────
    @app.get("/config")
    def get_config():
        """Editable settings: schema, effective values, and what's overridden."""
        overrides = orch.settings.all_config_overrides()
        groups = config_schema.describe()
        for group in groups:
            for item in group["settings"]:
                key = item["key"]
                item["value"] = orch.cfg.get(key)
                item["file_value"] = orch.cfg.file_value(key)
                item["overridden"] = key in overrides
        return {"groups": groups,
                "restart_pending": any(not config_schema.BY_KEY[k].get("live", True)
                                       for k in overrides
                                       if k in config_schema.BY_KEY)}

    @app.patch("/config")
    def patch_config(body: dict):
        """Set (or reset, with null) any number of settings at once."""
        coerced, needs_restart = {}, []
        for key, value in body.items():
            try:
                coerced[key] = None if value is None else config_schema.coerce(key, value)
            except KeyError as e:
                raise HTTPException(400, str(e))
            except ValueError as e:
                raise HTTPException(422, str(e))
            if value is not None and not config_schema.BY_KEY[key].get("live", True):
                needs_restart.append(key)
        orch.settings.set_config_overrides(coerced)
        return {"ok": True, "updated": sorted(coerced),
                "restart_required": needs_restart}

    # ── setup: AI DJ credentials ─────────────────────────────────────────
    @app.get("/settings")
    def get_settings():
        s = orch.settings.public()
        s["dj_provider_active"] = orch.dj.active_provider()
        return s

    @app.post("/settings")
    def post_settings(body: dict):
        allowed = {"anthropic_api_key", "openai_api_key", "llm_provider"}
        unknown = set(body) - allowed
        if unknown:
            raise HTTPException(400, f"unknown settings: {', '.join(sorted(unknown))}")
        provider = body.get("llm_provider")
        if provider and provider not in ("auto", "anthropic", "openai", "template"):
            raise HTTPException(400, f"invalid llm_provider: {provider}")
        orch.settings.set_many({k: (v.strip() if isinstance(v, str) else v)
                                for k, v in body.items()})
        return {"ok": True, "dj_provider_active": orch.dj.active_provider()}

    @app.post("/shows/{show_id}/start")
    def start_show(show_id: str):
        if not orch.force_show(show_id):
            raise HTTPException(404, f"unknown show: {show_id}")
        return {"ok": True}

    @app.post("/shows/resume-schedule")
    def resume_schedule():
        orch.force_show(None)
        return {"ok": True}

    return app
