"""Control API + mini web player."""
import html
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from . import config_schema
from .auth import MIN_LENGTH, PASSWORD_KEY, Auth

if TYPE_CHECKING:  # avoid pulling the analysis stack in just for a type hint
    from .orchestrator import Orchestrator

TEMPLATE = Path(__file__).parent / "templates" / "index.html"

# Reachable without a token: the page itself, which renders the login form, and
# the handshake it needs. Everything else is gated once a password is set.
OPEN_ROUTES = {("GET", "/"), ("POST", "/auth/login"), ("GET", "/auth/status")}


def create_app(orch: "Orchestrator") -> FastAPI:
    auth = Auth(orch.settings)

    def bearer(request: Request) -> str:
        scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
        return token.strip() if scheme.lower() == "bearer" else ""

    def guard(request: Request):
        if not auth.is_enabled() or (request.method, request.url.path) in OPEN_ROUTES:
            return
        if not auth.verify_token(bearer(request)):
            raise HTTPException(401, "login required")

    app = FastAPI(title="tidal-radio", docs_url=None, redoc_url=None,
                  openapi_url=None,   # plain route — the guard would not apply
                  dependencies=[Depends(guard)])

    @app.get("/", response_class=HTMLResponse)
    def player():
        name = orch.cfg.get("station.name", "Tidal Radio")
        return TEMPLATE.read_text().replace("__NAME__", html.escape(name))

    @app.get("/status")
    def status():
        return orch.status()

    # ── access control ───────────────────────────────────────────────────
    @app.get("/auth/status")
    def auth_status(request: Request):
        enabled = auth.is_enabled()
        return {"enabled": enabled,
                "required": enabled and not auth.verify_token(bearer(request))}

    @app.post("/auth/login")
    def auth_login(body: dict, request: Request):
        who = (request.client.host if request.client else "?")
        wait = auth.locked_for(who)
        if wait > 0:
            raise HTTPException(429, f"too many attempts — try again in {int(wait) + 1}s")
        if not auth.verify_password(str(body.get("password") or "")):
            auth.note_failure(who)
            raise HTTPException(401, "wrong password")
        auth.note_success(who)
        return {"token": auth.issue_token()}

    @app.post("/auth/password")
    def auth_password(body: dict, request: Request):
        """Set, change, or (with an empty new password) remove protection.

        `guard` has already demanded a valid token when a password exists, so
        current_password is an optional second proof rather than the gate.
        """
        # Re-authentication is mandatory once a password exists — an absent
        # field must not short-circuit the check.
        if auth.is_enabled():
            who = (request.client.host if request.client else "?")
            wait = auth.locked_for(who)
            if wait > 0:
                raise HTTPException(429, f"too many attempts — try again in {int(wait) + 1}s")
            if not auth.verify_password(str(body.get("current_password") or "")):
                auth.note_failure(who)
                raise HTTPException(401, "wrong current password")
        new = str(body.get("new_password") or "")
        if new and len(new) < MIN_LENGTH:
            raise HTTPException(422, f"password must be at least {MIN_LENGTH} characters")
        auth.set_password(new)
        # Setting one drops every session including the caller's, so hand back a
        # fresh token — otherwise the UI locks out the person who just set it.
        return {"ok": True, "enabled": auth.is_enabled(),
                "token": auth.issue_token() if new else None}

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

    # ── setup: Spotify linking (paste-a-code flow) ───────────────────────
    def _spotify():
        src = orch.source
        if not hasattr(src, "submit_code"):
            raise HTTPException(409, "Spotify is not the active source — "
                                     "switch it in Settings and restart")
        return src

    @app.post("/spotify/link")
    def spotify_link():
        try:
            return _spotify().start_link()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"could not start Spotify linking: {e}")

    @app.post("/spotify/link/code")
    def spotify_code(body: dict):
        code = str((body or {}).get("code") or "").strip()
        if not code:
            raise HTTPException(422, "paste the code or redirect URL first")
        return _spotify().submit_code(code)

    @app.get("/spotify/status")
    def spotify_status():
        src = orch.source
        if not hasattr(src, "link_status"):
            return {"status": "idle", "linked": False}
        state = src.link_status()
        state["linked"] = src.is_linked()
        return state

    # ── voices ───────────────────────────────────────────────────────────
    @app.get("/voices")
    def voices():
        return {"voices": orch.tts.list_voices(),
                "current": orch.cfg.get("tts.piper.voice")}

    @app.post("/voices/preview")
    def voice_preview(body: dict | None = None):
        body = body or {}
        station = orch.cfg.get("station.name", "the radio")
        text = (body.get("text") or
                f"You're listening to {station}. Here's one you might not know yet.")
        path = orch.tts.preview(text[:300], voice=body.get("voice"))
        if path is None:
            raise HTTPException(500, "could not render that voice")
        return FileResponse(path, media_type="audio/wav", filename="preview.wav")

    # ── discovery ────────────────────────────────────────────────────────
    @app.get("/discovery")
    def discovery():
        return orch.discovery_stats()

    @app.post("/discovery/run")
    def discovery_run():
        if not orch.discover_now():
            raise HTTPException(409, "link a music service first")
        return {"ok": True, "note": "looking for new music in the background"}

    # ── configuration ────────────────────────────────────────────────────
    @app.get("/config")
    def get_config():
        """Editable settings: schema, effective values, and what's overridden."""
        overrides = orch.settings.all_config_overrides()
        groups = config_schema.describe()
        for group in groups:
            for item in group["settings"]:
                key = item["key"]
                if key == "tts.piper.voice":
                    installed = orch.tts.list_voices()
                    if installed:      # only offer voices that exist on disk
                        item["control"] = "select"
                        item["options"] = installed
                        item["preview"] = "voice"
                # fall back to the schema default so a config.yaml written
                # before a setting existed doesn't render as empty/off
                item["value"] = orch.cfg.get(key, item.get("default"))
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
        s.pop(PASSWORD_KEY, None)          # the hash never leaves the container
        s["password_set"] = auth.is_enabled()
        s["dj_provider_active"] = orch.dj.active_provider()
        s["password_set"] = s.get("password_set", False)
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
