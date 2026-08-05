import argparse
import logging
import sys
import threading

from .config import Config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tidal-radio")


def main():
    parser = argparse.ArgumentParser(prog="tidal-radio",
                                     description="Personal AI radio from your Tidal library")
    parser.add_argument("--config", help="path to config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("auth", help="link your Tidal account (device flow)")
    sub.add_parser("sync", help="sync Tidal favorites into the local DB")
    p_an = sub.add_parser("analyze", help="analyze tempo/key of unanalyzed tracks")
    p_an.add_argument("--limit", type=int, default=25)
    p_shows = sub.add_parser("shows", help="show tools")
    p_shows.add_argument("action", choices=["generate", "list"])
    sub.add_parser("run", help="run the station (service entrypoint)")
    sub.add_parser("status", help="print now playing + queue")

    args = parser.parse_args()
    cfg = Config.load(args.config)

    if args.cmd == "auth":
        from .tidal_client import TidalClient
        ok = TidalClient(cfg).login_interactive()
        sys.exit(0 if ok else 1)

    if args.cmd == "sync":
        from .db import Database
        from .tidal_client import TidalClient
        tc = TidalClient(cfg)
        if not tc.ensure_login():
            sys.exit(1)
        n = tc.sync_favorites(Database(cfg.db_path))
        print(f"Synced {n} tracks.")
        return

    if args.cmd == "analyze":
        from .analysis import analyze_file
        from .db import Database
        from .tidal_client import TidalClient
        tc = TidalClient(cfg)
        if not tc.ensure_login():
            sys.exit(1)
        db = Database(cfg.db_path)
        rows = db.tracks_without_features(limit=args.limit)
        if not rows:
            print("Everything is analyzed.")
            return
        for i, t in enumerate(rows, 1):
            path = tc.fetch_track(t["id"])
            if not path:
                print(f"[{i}/{len(rows)}] SKIP {t['artist']} — {t['title']} (fetch failed)")
                continue
            feats = analyze_file(path, int(cfg.get("analysis.max_seconds", 120)))
            if feats:
                db.save_features(t["id"], **feats)
                print(f"[{i}/{len(rows)}] {t['artist']} — {t['title']}: "
                      f"{feats['bpm']} bpm, {feats['camelot']}")
            else:
                print(f"[{i}/{len(rows)}] FAIL {t['artist']} — {t['title']}")
        return

    if args.cmd == "shows":
        from .db import Database
        if args.action == "generate":
            from .shows import generate_shows
            shows = generate_shows(cfg, Database(cfg.db_path))
            for s in shows:
                print(f"  {s['id']}: {s['name']}  ({'/'.join(s['days'])} "
                      f"{s['start']}-{s['end']}, {s['bpm'][0]}-{s['bpm'][1]} bpm)")
        else:
            for s in cfg.shows:
                print(f"  {s.get('id')}: {s.get('name')}")
        return

    if args.cmd == "status":
        import json
        import urllib.request
        port = cfg.get("api.port", 8080)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=5) as r:
            print(json.dumps(json.load(r), indent=2))
        return

    if args.cmd == "run":
        import uvicorn

        from .api import create_app
        from .orchestrator import Orchestrator

        orch = Orchestrator(cfg)
        app = create_app(orch)
        api_thread = threading.Thread(
            target=uvicorn.run,
            kwargs={"app": app, "host": cfg.get("api.host", "0.0.0.0"),
                    "port": int(cfg.get("api.port", 8080)), "log_level": "warning"},
            daemon=True, name="api",
        )
        api_thread.start()

        # Keep the control UI reachable even when the brain can't run (e.g.
        # Tidal not linked yet) — it's where the problem gets reported. Retry
        # so the station self-heals once the cause is fixed.
        import time
        while True:
            try:
                orch.fatal_error = None
                orch.run()
            except Exception as e:
                orch.fatal_error = str(e)
                log.error("Station stopped: %s — retrying in 30s "
                          "(control UI stays up at :%s)", e,
                          cfg.get("api.port", 8080))
                time.sleep(30)


if __name__ == "__main__":
    main()
