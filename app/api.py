"""Control API + mini web player."""
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

if TYPE_CHECKING:  # avoid pulling the analysis stack in just for a type hint
    from .orchestrator import Orchestrator

# Substituted with __TOKENS__ rather than str.format, so the JavaScript braces
# below don't all need doubling.
PLAYER_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__NAME__</title>
<style>
 :root{color-scheme:dark}
 body{font-family:system-ui,-apple-system,sans-serif;background:#111;color:#eee;
      max-width:600px;margin:32px auto;padding:0 16px;line-height:1.5}
 h1{font-size:1.5rem;margin-bottom:4px} h3{margin:22px 0 8px;font-size:1rem;
      text-transform:uppercase;letter-spacing:.05em;color:#aaa}
 .dim{color:#999} .np{font-size:1.15rem;margin:14px 0}
 button{background:#2a2a2a;color:#eee;border:1px solid #555;border-radius:8px;
        padding:8px 14px;margin:0 8px 8px 0;cursor:pointer;font-size:.95rem}
 button:hover{background:#383838} button.primary{background:#1d4ed8;border-color:#1d4ed8}
 button:disabled{opacity:.5;cursor:default}
 audio{width:100%;margin:14px 0}
 li{margin:4px 0} ul{padding-left:20px}
 input,select{background:#1c1c1c;color:#eee;border:1px solid #444;border-radius:8px;
        padding:8px 10px;font-size:.95rem;width:100%;box-sizing:border-box;margin:4px 0 10px}
 code{background:#222;padding:2px 6px;border-radius:4px;font-size:.9em}
 .card{background:#191919;border:1px solid #2e2e2e;border-radius:12px;
        padding:14px 16px;margin:14px 0}
 .err{background:#3a1414;border-color:#7a2b2b;display:none}
 .ok{color:#4ade80} .warn{color:#fbbf24}
 .step{display:flex;align-items:center;gap:8px;margin:6px 0}
 details summary{cursor:pointer;color:#aaa;margin-top:20px}
 a{color:#7aa2f7}
</style></head><body>
<h1>📻 __NAME__</h1>
<div class="dim" id="sub">…</div>

<div class="card err" id="err"></div>

<div class="card" id="setup" style="display:none">
  <h3 style="margin-top:0">Setup</h3>

  <div class="step"><span id="t-icon">○</span><b>1. Tidal account</b>
      <span class="dim" id="t-state"></span></div>
  <div id="t-actions">
    <button class="primary" id="btn-link" onclick="startLink()">Link Tidal account</button>
    <button id="btn-sync" onclick="syncNow()">Sync favorites</button>
  </div>
  <div id="t-link" style="display:none">
    <p>Open this link and approve, then come back — this page updates itself:</p>
    <p><a id="t-url" target="_blank" rel="noopener"></a></p>
  </div>

  <div class="step" style="margin-top:16px"><span id="k-icon">○</span><b>2. AI DJ voice-writing</b>
      <span class="dim" id="k-state"></span></div>
  <p class="dim">Optional. Without a key the DJ uses built-in templates.
     Keys are stored on the container only.</p>
  <label class="dim">Provider</label>
  <select id="provider">
    <option value="auto">Auto — use whichever key is set</option>
    <option value="anthropic">Claude (Anthropic)</option>
    <option value="openai">OpenAI</option>
    <option value="template">Templates only (no AI)</option>
  </select>
  <label class="dim">Anthropic API key</label>
  <input type="password" id="anthropic_api_key" placeholder="sk-ant-… (leave blank to keep)">
  <label class="dim">OpenAI API key</label>
  <input type="password" id="openai_api_key" placeholder="sk-… (leave blank to keep)">
  <button class="primary" onclick="saveSettings()">Save</button>
  <span id="save-state" class="dim"></span>
</div>

<audio controls id="stream"></audio>
<div class="np" id="np">…</div>
<div>
  <button onclick="post('/skip')">⏭ Skip</button>
  <button onclick="post('/break')">🎙 DJ break</button>
  <button onclick="toggleSetup()">⚙ Setup</button>
</div>

<h3>Recently played</h3><ul id="recent"></ul>

<script>
const $ = id => document.getElementById(id);
$('stream').src = location.protocol + '//' + location.hostname + ':8000/radio.mp3';
let setupPinned = false, linkPolling = false;

const post = async (path, body) => fetch(path, {
  method: 'POST',
  headers: body ? {'Content-Type': 'application/json'} : undefined,
  body: body ? JSON.stringify(body) : undefined,
});

function toggleSetup(){ setupPinned = !setupPinned; refresh(); }

async function startLink(){
  $('btn-link').disabled = true;
  const r = await post('/tidal/link');
  if (!r.ok){ $('t-state').textContent = 'could not start linking'; $('btn-link').disabled = false; return; }
  const d = await r.json();
  $('t-url').href = d.url; $('t-url').textContent = d.url;
  $('t-link').style.display = 'block';
  $('t-state').innerHTML = '<span class="warn">waiting for approval…</span>';
  if (!linkPolling){ linkPolling = true; pollLink(); }
}

async function pollLink(){
  const d = await (await fetch('/tidal/status')).json();
  if (d.linked || d.status === 'linked'){
    linkPolling = false;
    $('t-link').style.display = 'none';
    $('t-state').innerHTML = '<span class="ok">linked — syncing your library…</span>';
    await post('/tidal/sync');
    refresh();
    return;
  }
  if (d.status === 'failed'){
    linkPolling = false;
    $('t-state').innerHTML = '<span class="warn">linking failed: ' + (d.error||'') + '</span>';
    $('btn-link').disabled = false;
    return;
  }
  setTimeout(pollLink, 3000);
}

async function syncNow(){
  $('btn-sync').disabled = true;
  await post('/tidal/sync');
  $('btn-sync').textContent = 'Syncing…';
  setTimeout(() => { $('btn-sync').disabled = false; $('btn-sync').textContent = 'Sync favorites'; }, 8000);
}

async function saveSettings(){
  const body = {llm_provider: $('provider').value};
  for (const k of ['anthropic_api_key','openai_api_key']){
    if ($(k).value.trim()) body[k] = $(k).value.trim();
  }
  const r = await post('/settings', body);
  const d = await r.json().catch(() => ({}));
  $('save-state').textContent = r.ok ? 'saved — DJ now uses: ' + d.dj_provider_active
                                     : 'save failed';
  $('anthropic_api_key').value = ''; $('openai_api_key').value = '';
  refresh();
}

async function refresh(){
  const s = await (await fetch('/status')).json();
  const cfg = await (await fetch('/settings')).json();

  const problem = s.error || s.tidal_error;
  $('err').style.display = problem ? 'block' : 'none';
  if (problem) $('err').innerHTML = '<b>' + (s.error ? 'Station not playing:' : 'Tidal:')
    + '</b> ' + problem
    + (s.throttled_for ? ' <span class="dim">(retrying in ' + s.throttled_for + 's)</span>' : '')
    + '<br><span class="dim">Run <code>tidal-radio diagnose</code> in the container '
    + 'for details.</span>';

  const needsSetup = !s.tidal_linked;
  $('setup').style.display = (needsSetup || setupPinned) ? 'block' : 'none';

  $('t-icon').textContent = s.tidal_linked ? '✅' : '○';
  if (!linkPolling)
    $('t-state').textContent = s.tidal_linked
      ? (s.library_tracks + ' tracks in your library') : 'not linked yet';
  $('btn-link').textContent = s.tidal_linked ? 'Re-link account' : 'Link Tidal account';
  $('btn-link').disabled = false;
  $('btn-sync').style.display = s.tidal_linked ? '' : 'none';

  const hasKey = cfg.anthropic_api_key_set || cfg.openai_api_key_set;
  $('k-icon').textContent = hasKey ? '✅' : '○';
  $('k-state').textContent = 'DJ writes with: ' + s.dj_provider;
  if (cfg.llm_provider) $('provider').value = cfg.llm_provider;
  $('anthropic_api_key').placeholder = cfg.anthropic_api_key_set
      ? (cfg.anthropic_api_key_from_env ? 'set via secrets.env' : 'saved — type to replace')
      : 'sk-ant-…';
  $('openai_api_key').placeholder = cfg.openai_api_key_set
      ? (cfg.openai_api_key_from_env ? 'set via secrets.env' : 'saved — type to replace')
      : 'sk-…';

  const np = s.now_playing;
  $('np').innerHTML = np
    ? (np.kind === 'break' ? '🎙 <i>DJ break</i>'
       : '▶ <b>' + np.title + '</b> — ' + np.artist
         + (np.bpm ? ' <span class="dim">(' + np.bpm + ' bpm · ' + np.camelot + ')</span>' : ''))
    : '<span class="dim">' + (s.tidal_linked ? 'warming up…' : 'waiting for setup') + '</span>';
  $('sub').textContent = s.show.name + (s.quality ? ' · ' + s.quality : '')
    + (s.liquidsoap ? '' : ' · audio pipeline down');
  $('recent').innerHTML = s.recent
    .map(r => '<li>' + r.title + ' <span class="dim">— ' + r.artist + '</span></li>').join('');
}
refresh(); setInterval(refresh, 10000);
</script></body></html>"""


def create_app(orch: "Orchestrator") -> FastAPI:
    app = FastAPI(title="tidal-radio", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def player():
        name = orch.cfg.get("station.name", "Tidal Radio")
        return PLAYER_HTML.replace("__NAME__", name)

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
