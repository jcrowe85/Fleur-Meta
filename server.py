#!/usr/bin/env python3
"""Local web server that serves the Meta Ads dashboard with a refresh button.

Endpoints:
  GET  /              → dashboard.html
  GET  /api/status    → current refresh state + cooldown
  POST /api/refresh   → trigger a refresh (single-flight, cooldown-throttled)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_file

BASE = Path(__file__).parent.resolve()
HTML = BASE / "dashboard.html"
TIKTOK_HTML = BASE / "tiktok_ad.html"
SCRIPT = BASE / "dashboard.py"

load_dotenv(BASE / ".env")

import tiktok_ad  # noqa: E402  — must come after load_dotenv so env reads see the file

app = Flask(__name__)


@app.before_request
def _require_basic_auth():
    """If APP_USERNAME + APP_PASSWORD are set in env, gate every route behind Basic Auth.
    Local dev (no env vars set) stays open. Production sets both in Railway."""
    user = os.environ.get("APP_USERNAME") or ""
    pwd = os.environ.get("APP_PASSWORD") or ""
    if not user or not pwd:
        return None
    auth = request.authorization
    if auth and auth.username == user and auth.password == pwd:
        return None
    return Response(
        "Authentication required.",
        401,
        {"WWW-Authenticate": 'Basic realm="fleur-meta"'},
    )


# job_id -> {"video_path": Path, "transcript": str, "primary_text": str, "headline": str, "description": str}
tiktok_jobs: dict[str, dict] = {}

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


@app.get("/tiktok-ad")
def tiktok_ad_page():
    if not TIKTOK_HTML.exists():
        return "tiktok_ad.html missing", 500
    return send_file(TIKTOK_HTML, max_age=0)


@app.get("/api/tiktok-ad/env")
def tiktok_env_check():
    return jsonify({"missing": tiktok_ad.check_env()})


@app.get("/api/tiktok-ad/video/<job_id>")
def tiktok_video_download(job_id: str):
    job = tiktok_jobs.get(job_id)
    if not job:
        return "unknown job_id", 404
    video_path = job.get("video_path")
    if not video_path or not Path(video_path).exists():
        return "video file missing", 404
    return send_file(video_path, as_attachment=True, download_name=Path(video_path).name)


@app.get("/api/tiktok-ad/adsets")
def tiktok_list_adsets():
    force = request.args.get("force") in ("1", "true", "yes")
    try:
        return jsonify({"adsets": tiktok_ad.list_adsets(force=force)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/tiktok-ad/campaigns")
def tiktok_list_campaigns():
    force = request.args.get("force") in ("1", "true", "yes")
    try:
        return jsonify({"campaigns": tiktok_ad.list_campaigns(force=force)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/tiktok-ad/generate")
def tiktok_generate():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400
    missing = tiktok_ad.check_env()
    if missing:
        return jsonify({"error": f"Missing env vars: {', '.join(missing)}"}), 500
    try:
        video_path = tiktok_ad.download_tiktok(url)
        audio_path = tiktok_ad.extract_audio(video_path)
        transcript = tiktok_ad.transcribe(audio_path)
        copy = tiktok_ad.generate_copy(transcript)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    job_id = uuid.uuid4().hex
    tiktok_jobs[job_id] = {
        "video_path": video_path,
        "transcript": transcript,
        **copy,
    }
    return jsonify({
        "job_id": job_id,
        "transcript": transcript,
        "primary_text": copy["primary_text"],
        "headline": copy["headline"],
        "description": copy["description"],
        "video_filename": video_path.name,
    })


def _resolve_target_adset(body: dict) -> tuple[str, str | None]:
    """Returns (adset_id, new_adset_id_if_created). Raises ValueError on bad input."""
    mode = (body.get("mode") or "existing_adset").strip()
    if mode == "existing_adset":
        adset_id = (body.get("adset_id") or "").strip()
        if not adset_id:
            raise ValueError("adset_id is required")
        return adset_id, None
    if mode in ("new_adset_cloned", "new_adset_fresh"):
        campaign_id = (body.get("campaign_id") or "").strip()
        new_name = (body.get("new_adset_name") or "").strip()
        budget_cents = body.get("new_adset_daily_budget_cents")
        if not campaign_id:
            raise ValueError("campaign_id is required")
        if not new_name:
            raise ValueError("new_adset_name is required")
        try:
            budget_cents = int(budget_cents)
        except (TypeError, ValueError):
            raise ValueError("new_adset_daily_budget_cents must be an integer (cents)")
        if budget_cents < 100:
            raise ValueError("new_adset_daily_budget_cents must be at least 100 (= $1.00)")
        if mode == "new_adset_cloned":
            new_id = tiktok_ad.create_adset_from_template(
                campaign_id=campaign_id, name=new_name, daily_budget_cents=budget_cents,
            )
            return new_id, new_id
        # fresh: caller supplies full fields
        targeting_raw = body.get("targeting") or {}
        if isinstance(targeting_raw, str):
            try:
                targeting = json.loads(targeting_raw)
            except json.JSONDecodeError as e:
                raise ValueError(f"targeting is not valid JSON: {e}")
        else:
            targeting = targeting_raw
        if not isinstance(targeting, dict) or not targeting:
            raise ValueError("targeting (JSON object) is required for new_adset_fresh")
        optimization_goal = (body.get("optimization_goal") or "").strip()
        billing_event = (body.get("billing_event") or "IMPRESSIONS").strip()
        bid_strategy = (body.get("bid_strategy") or "LOWEST_COST_WITHOUT_CAP").strip()
        if not optimization_goal:
            raise ValueError("optimization_goal is required for new_adset_fresh")
        promoted_object = body.get("promoted_object") or None
        if isinstance(promoted_object, str) and promoted_object.strip():
            try:
                promoted_object = json.loads(promoted_object)
            except json.JSONDecodeError as e:
                raise ValueError(f"promoted_object is not valid JSON: {e}")
        new_id = tiktok_ad.create_adset(
            name=new_name,
            campaign_id=campaign_id,
            daily_budget_cents=budget_cents,
            targeting=targeting,
            optimization_goal=optimization_goal,
            billing_event=billing_event,
            bid_strategy=bid_strategy,
            promoted_object=promoted_object if isinstance(promoted_object, dict) else None,
            start_time=(body.get("start_time") or None),
            end_time=(body.get("end_time") or None),
        )
        return new_id, new_id
    raise ValueError(f"unknown mode: {mode}")


@app.post("/api/tiktok-ad/publish")
def tiktok_publish():
    body = request.get_json(silent=True) or {}
    job_id = body.get("job_id")
    job = tiktok_jobs.get(job_id) if job_id else None
    if not job:
        return jsonify({"error": "unknown or expired job_id — re-run Analyze"}), 400
    primary_text = (body.get("primary_text") or "").strip()
    headline = (body.get("headline") or "").strip()
    description = (body.get("description") or "").strip() or None
    ad_name = (body.get("ad_name") or "").strip()
    destination_url = (body.get("destination_url") or "").strip()
    cta_type = (body.get("cta_type") or "SHOP_NOW").strip()
    for field, val in (("primary_text", primary_text), ("headline", headline),
                       ("ad_name", ad_name), ("destination_url", destination_url)):
        if not val:
            return jsonify({"error": f"{field} is required"}), 400
    try:
        adset_id, created_adset_id = _resolve_target_adset(body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    try:
        video_id = tiktok_ad.upload_video(job["video_path"], name=ad_name)
        tiktok_ad.wait_video_ready(video_id)
        thumb_path = tiktok_ad.extract_thumbnail(job["video_path"])
        image_hash = tiktok_ad.upload_image(thumb_path)
        creative_id = tiktok_ad.create_creative(
            name=ad_name,
            video_id=video_id,
            primary_text=primary_text,
            headline=headline,
            destination_url=destination_url,
            cta_type=cta_type,
            description=description,
            image_hash=image_hash,
        )
        ad_id = tiktok_ad.create_ad(adset_id=adset_id, creative_id=creative_id, name=ad_name)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    tiktok_ad.cleanup_video(job["video_path"])
    tiktok_jobs.pop(job_id, None)
    return jsonify({
        "ad_id": ad_id,
        "creative_id": creative_id,
        "video_id": video_id,
        "adset_id": adset_id,
        "created_adset_id": created_adset_id,
        "ads_manager_url": tiktok_ad.ads_manager_url(ad_id),
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
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
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
