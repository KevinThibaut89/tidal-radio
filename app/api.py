"""Control API + mini web player."""
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

if TYPE_CHECKING:  # avoid pulling the analysis stack in just for a type hint
    from .orchestrator import Orchestrator

PLAYER_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name}</title>
<style>
 body{{font-family:system-ui,sans-serif;background:#111;color:#eee;max-width:520px;
      margin:40px auto;padding:0 16px}}
 h1{{font-size:1.4rem}} .np{{font-size:1.1rem;margin:12px 0}} .dim{{color:#999}}
 button{{background:#333;color:#eee;border:1px solid #555;border-radius:8px;
        padding:8px 16px;margin-right:8px;cursor:pointer}}
 audio{{width:100%;margin:16px 0}}
 li{{margin:4px 0}}
 .err{{background:#3a1414;border:1px solid #7a2b2b;border-radius:8px;
      padding:10px 14px;margin:12px 0;display:none}}
 code{{background:#222;padding:2px 6px;border-radius:4px}}
</style></head><body>
<h1>📻 {name}</h1>
<div class="err" id="err"></div>
<audio controls id="stream"></audio>
<div class="np" id="np">…</div>
<div><button onclick="fetch('/skip',{{method:'POST'}})">⏭ Skip</button>
<button onclick="fetch('/break',{{method:'POST'}})">🎙 DJ break</button></div>
<h3>Recently played</h3><ul id="recent"></ul>
<script>
document.getElementById('stream').src =
  location.protocol + '//' + location.hostname + ':8000/radio.mp3';
async function tick(){{
  const s = await (await fetch('/status')).json();
  const eb = document.getElementById('err');
  if (s.error) {{
    eb.style.display = 'block';
    eb.innerHTML = `<b>Station not playing:</b> ${{s.error}}`
      + (s.tidal_linked ? '' : '<br>Run <code>tidal-radio auth</code> then '
                              + '<code>tidal-radio sync</code> in the container.');
  }} else {{ eb.style.display = 'none'; }}
  const np = s.now_playing;
  document.getElementById('np').innerHTML = np
    ? (np.kind==='break' ? '🎙 <i>DJ break</i>'
       : `▶ <b>${{np.title}}</b> — ${{np.artist}}`
         + (np.bpm?` <span class="dim">(${{np.bpm}} bpm · ${{np.camelot}})</span>`:''))
    : '<span class="dim">warming up…</span>';
  document.getElementById('np').innerHTML +=
    `<div class="dim">show: ${{s.show.name}}</div>`;
  document.getElementById('recent').innerHTML =
    s.recent.map(r=>`<li>${{r.title}} <span class="dim">— ${{r.artist}}</span></li>`).join('');
}}
tick(); setInterval(tick, 10000);
</script></body></html>"""


def create_app(orch: "Orchestrator") -> FastAPI:
    app = FastAPI(title="tidal-radio", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def player():
        name = orch.cfg.get("station.name", "Tidal Radio")
        return PLAYER_HTML.format(name=name)

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
