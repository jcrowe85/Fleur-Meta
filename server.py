#!/usr/bin/env python3
"""Local web server that serves the Meta Ads dashboard with a refresh button.

Endpoints:
  GET  /              → dashboard.html
  GET  /api/status    → current refresh state + cooldown
  POST /api/refresh   → trigger a refresh (single-flight, cooldown-throttled)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, send_file

BASE = Path(__file__).parent.resolve()
HTML = BASE / "dashboard.html"
SCRIPT = BASE / "dashboard.py"

app = Flask(__name__)

config = {
    "cooldown_s": 90,
    "gen_args": [],
}

state = {
    "running": False,
    "last_started": None,
    "last_finished": None,
    "last_error": None,
    "last_duration": None,
    "last_log_tail": "",
}
lock = threading.Lock()


def run_generation() -> None:
    state["last_started"] = time.time()
    state["last_error"] = None
    state["last_log_tail"] = ""
    start = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *config["gen_args"]],
            cwd=BASE,
            capture_output=True,
            text=True,
            check=False,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        state["last_log_tail"] = out.strip()[-800:]
        if proc.returncode != 0:
            state["last_error"] = (proc.stderr or proc.stdout).strip()[-500:] or f"exit {proc.returncode}"
    except Exception as e:
        state["last_error"] = str(e)
    finally:
        state["last_duration"] = time.time() - start
        state["last_finished"] = time.time()
        state["running"] = False


@app.get("/")
def root():
    if not HTML.exists():
        return ("Dashboard not generated yet. POST /api/refresh, or run "
                "<code>python3 dashboard.py --include-ads</code>."), 503
    return send_file(HTML, max_age=0)


@app.get("/api/status")
def status():
    now = time.time()
    cooldown_remaining = 0
    if state["last_finished"]:
        cooldown_remaining = max(0, int(config["cooldown_s"] - (now - state["last_finished"])))
    return jsonify({
        "running": state["running"],
        "last_started": state["last_started"],
        "last_finished": state["last_finished"],
        "last_error": state["last_error"],
        "last_duration": state["last_duration"],
        "last_log_tail": state["last_log_tail"],
        "cooldown_remaining": cooldown_remaining,
        "cooldown_total": config["cooldown_s"],
        "html_mtime": HTML.stat().st_mtime if HTML.exists() else None,
        "now": now,
    })


@app.post("/api/refresh")
def refresh():
    now = time.time()
    with lock:
        if state["running"]:
            return jsonify({"status": "running", "message": "Already refreshing"}), 202
        if state["last_finished"] and (now - state["last_finished"] < config["cooldown_s"]):
            wait = int(config["cooldown_s"] - (now - state["last_finished"]))
            return jsonify({
                "status": "throttled",
                "message": f"Cooldown active — wait {wait}s",
                "cooldown_remaining": wait,
            }), 429
        state["running"] = True
        threading.Thread(target=run_generation, daemon=True).start()
    return jsonify({"status": "started"}), 202


def main() -> int:
    parser = argparse.ArgumentParser(description="Local dashboard server with refresh button")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--cooldown", type=int, default=90, help="Seconds between allowed refreshes")
    parser.add_argument("--date-preset", default="last_30d")
    parser.add_argument("--top-ads", type=int, default=30)
    parser.add_argument("--no-ads", action="store_true", help="Skip ad-level fetching")
    import datetime as _dt
    default_ads_since = (_dt.date.today() - _dt.timedelta(days=36 * 30)).isoformat()
    parser.add_argument("--ads-since", default=default_ads_since,
                        help=f"Start date for ad-level insights (default = {default_ads_since}, "
                             f"~36mo back; Meta caps history at 37mo)")
    parser.add_argument("--ads-until", default=None)
    parser.add_argument("--ads-match-window", action="store_true",
                        help="Use same window for ads as for the rest of the dashboard")
    parser.add_argument("--ads-top-campaigns", type=int, default=10,
                        help="Only scan ads in top N campaigns by spend (keeps API calls down)")
    args = parser.parse_args()

    config["cooldown_s"] = args.cooldown
    gen = ["--date-preset", args.date_preset]
    if not args.no_ads:
        gen += ["--include-ads", "--top-ads", str(args.top_ads),
                "--ads-top-campaigns", str(args.ads_top_campaigns)]
        if args.ads_match_window:
            gen.append("--ads-match-window")
        else:
            gen += ["--ads-since", args.ads_since]
            if args.ads_until:
                gen += ["--ads-until", args.ads_until]
    config["gen_args"] = gen

    url = f"http://{args.host}:{args.port}/"
    print(f"→ Dashboard server on {url}")
    print(f"  Cooldown: {args.cooldown}s")
    print(f"  Refresh args: {' '.join(gen)}")
    if not HTML.exists():
        print(f"  Note: {HTML.name} not found yet — click 'Refresh' once the page loads.")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
