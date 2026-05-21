"""TikTok URL -> Meta paused-ad pipeline.

Pipeline:
  download_tiktok  -> RapidAPI -> mp4 on disk
  extract_audio    -> ffmpeg   -> mp3
  transcribe       -> OpenAI Whisper -> text
  generate_copy    -> Claude -> {primary_text, headline, description}
  upload_video     -> Meta /act_<id>/advideos
  wait_video_ready -> poll until status.video_status == ready
  create_creative  -> Meta /act_<id>/adcreatives (object_story_spec.video_data)
  create_ad        -> Meta /act_<id>/ads (status=PAUSED)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import anthropic
import requests

BASE = Path(__file__).parent.resolve()
CACHE_DIR = BASE / ".cache" / "tiktok"
GRAPH_VERSION = "v20.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
RAPID_HOST = os.environ.get("RAPIDAPI_TIKTOK_HOST", "tiktok-video-no-watermark2.p.rapidapi.com")
CLAUDE_MODEL = "claude-sonnet-4-6"

_adsets_cache: dict = {"at": 0.0, "data": None}
_campaigns_cache: dict = {"at": 0.0, "data": None}
_ADSETS_TTL_S = 300

# Fields cloned from a template ad set when creating a new ad set in the same campaign.
_TEMPLATE_ADSET_FIELDS = (
    "id,name,campaign_id,targeting,optimization_goal,billing_event,bid_strategy,"
    "promoted_object,attribution_spec,destination_type,is_dynamic_creative,"
    "configured_status,updated_time"
)


def _env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env var: {name}. Set it in .env.")
    return val


def _meta_token() -> str:
    return _env("ACCESS_TOKEN")


def _ad_account() -> str:
    return _env("AD_ACCOUNT_ID")


def _page_id() -> str:
    return _env("PAGE_ID")


_page_token_cache: dict = {"token": None}


def _page_token() -> str:
    """Page-scoped access token, minted via the System User token. Cached for the process."""
    if _page_token_cache["token"]:
        return _page_token_cache["token"]
    r = requests.get(
        f"{GRAPH}/{_page_id()}",
        params={"fields": "access_token", "access_token": _meta_token()},
        timeout=20,
    )
    if r.status_code != 200:
        raise RuntimeError(f"page token mint failed {r.status_code}: {r.text[:300]}")
    tok = (r.json() or {}).get("access_token")
    if not tok:
        raise RuntimeError(
            "page returned no access_token — System User may not be admin on this page"
        )
    _page_token_cache["token"] = tok
    return tok


# ── 1. Download TikTok ───────────────────────────────────────────────────────

def _extract_download_url(payload: dict) -> str:
    """tiktok-video-no-watermark2 returns {'code':0,'data':{'play':<url>,'hdplay':<url>,...}}."""
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected RapidAPI response: {payload!r}")
    if payload.get("code") not in (0, None):
        raise RuntimeError(f"RapidAPI error: {payload.get('msg') or payload}")
    data = payload.get("data") or {}
    for key in ("hdplay", "play", "wmplay"):
        url = data.get(key)
        if url:
            return url
    raise RuntimeError(f"no playable URL in RapidAPI response: {payload}")


def download_tiktok(url: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    api_key = _env("RAPIDAPI_KEY")
    r = requests.get(
        f"https://{RAPID_HOST}/",
        headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": RAPID_HOST},
        params={"url": url, "hd": "1"},
        timeout=60,
    )
    r.raise_for_status()
    play_url = _extract_download_url(r.json())
    out = CACHE_DIR / f"{uuid.uuid4().hex}.mp4"
    with requests.get(play_url, stream=True, timeout=120) as dl:
        dl.raise_for_status()
        with open(out, "wb") as f:
            for chunk in dl.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
    if out.stat().st_size < 1024:
        raise RuntimeError(f"downloaded video is suspiciously small: {out.stat().st_size} bytes")
    return out


# ── 2. Extract audio ─────────────────────────────────────────────────────────

def extract_audio(video_path: Path) -> Path:
    out = video_path.with_suffix(".mp3")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path),
         "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", str(out)],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-400:]}")
    return out


def extract_thumbnail(video_path: Path) -> Path:
    out = video_path.with_suffix(".jpg")
    # Try 1s in; fall back to first frame if the video is shorter.
    for ss in ("1", "0"):
        proc = subprocess.run(
            ["ffmpeg", "-y", "-ss", ss, "-i", str(video_path),
             "-frames:v", "1", "-q:v", "3", str(out)],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 1024:
            return out
    raise RuntimeError(f"ffmpeg thumbnail failed: {proc.stderr[-400:] if proc else 'unknown'}")


# ── 3. Transcribe ────────────────────────────────────────────────────────────

def transcribe(audio_path: Path) -> str:
    api_key = _env("OPENAI_API_KEY")
    with open(audio_path, "rb") as f:
        r = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            data={"model": "whisper-1", "response_format": "json"},
            files={"file": (audio_path.name, f, "audio/mpeg")},
            timeout=180,
        )
    if r.status_code != 200:
        raise RuntimeError(f"Whisper error {r.status_code}: {r.text[:400]}")
    return (r.json().get("text") or "").strip()


# ── 4. Generate copy ─────────────────────────────────────────────────────────

_COPY_SYSTEM = (
    "You write Facebook/Instagram ad copy for a direct-response brand. "
    "Given a transcript of a TikTok video, produce ad copy that matches the "
    "voice and hook of the video while being optimized for Meta Ads. "
    "Constraints: primary_text <= 125 chars (no emojis unless the transcript used them), "
    "headline <= 40 chars, description <= 30 chars. "
    "If the transcript is empty or non-verbal, infer reasonable copy from the brand context "
    "(haircare / scalp serum) and note 'visual-only' in the description. "
    "Reply with ONLY a JSON object, no prose, with keys: primary_text, headline, description."
)


def _parse_copy(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise RuntimeError(f"Claude did not return JSON: {text[:400]}")
    obj = json.loads(m.group(0))
    for k in ("primary_text", "headline", "description"):
        obj.setdefault(k, "")
        obj[k] = str(obj[k]).strip()
    return obj


def generate_copy(transcript: str) -> dict:
    _env("ANTHROPIC_API_KEY")  # validate presence; SDK reads it from env
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=512,
        system=_COPY_SYSTEM,
        messages=[{"role": "user", "content": f"Transcript:\n\n{transcript or '(no speech detected)'}"}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return _parse_copy(text)


# ── 5. Meta: list adsets ─────────────────────────────────────────────────────

def list_campaigns(force: bool = False) -> list[dict]:
    now = time.time()
    if not force and _campaigns_cache["data"] is not None and now - _campaigns_cache["at"] < _ADSETS_TTL_S:
        return _campaigns_cache["data"]
    out: list[dict] = []
    url = f"{GRAPH}/act_{_ad_account()}/campaigns"
    params = {
        "fields": "id,name,objective,status,effective_status",
        "limit": 200,
        "access_token": _meta_token(),
    }
    while url:
        r = requests.get(url, params=params, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"campaigns fetch failed {r.status_code}: {r.text[:300]}")
        body = r.json()
        for c in body.get("data", []):
            out.append({
                "id": c["id"],
                "name": c.get("name", ""),
                "objective": c.get("objective"),
                "status": c.get("status"),
                "effective_status": c.get("effective_status"),
            })
        url = (body.get("paging") or {}).get("next")
        params = None
    out.sort(key=lambda x: x.get("name") or "")
    _campaigns_cache["data"] = out
    _campaigns_cache["at"] = now
    return out


def get_template_adset(campaign_id: str) -> dict | None:
    """Most-recently-updated ad set in the given campaign, with cloneable fields populated."""
    r = requests.get(
        f"{GRAPH}/{campaign_id}/adsets",
        params={
            "fields": _TEMPLATE_ADSET_FIELDS,
            "limit": 50,
            "access_token": _meta_token(),
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"template adset fetch failed {r.status_code}: {r.text[:300]}")
    data = (r.json() or {}).get("data") or []
    if not data:
        return None
    data.sort(key=lambda a: a.get("updated_time") or "", reverse=True)
    return data[0]


def create_adset(*, name: str, campaign_id: str, daily_budget_cents: int,
                 targeting: dict, optimization_goal: str, billing_event: str,
                 bid_strategy: str = "LOWEST_COST_WITHOUT_CAP",
                 promoted_object: dict | None = None,
                 start_time: str | None = None,
                 end_time: str | None = None,
                 attribution_spec: list | None = None,
                 destination_type: str | None = None) -> str:
    data = {
        "name": name,
        "campaign_id": campaign_id,
        "daily_budget": str(int(daily_budget_cents)),
        "billing_event": billing_event,
        "optimization_goal": optimization_goal,
        "bid_strategy": bid_strategy,
        "targeting": json.dumps(targeting),
        "status": "ACTIVE",
        "access_token": _meta_token(),
    }
    if promoted_object:
        data["promoted_object"] = json.dumps(promoted_object)
    if start_time:
        data["start_time"] = start_time
    if end_time:
        data["end_time"] = end_time
    if attribution_spec:
        data["attribution_spec"] = json.dumps(attribution_spec)
    if destination_type:
        data["destination_type"] = destination_type
    r = requests.post(f"{GRAPH}/act_{_ad_account()}/adsets", data=data, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"adset create failed {r.status_code}: {r.text[:500]}")
    return r.json()["id"]


def create_adset_from_template(*, campaign_id: str, name: str,
                               daily_budget_cents: int) -> str:
    """Clone targeting/optimization/etc. from the most recent ad set in the campaign."""
    tmpl = get_template_adset(campaign_id)
    if not tmpl:
        raise RuntimeError(
            "Campaign has no existing ad sets to clone from. "
            "Provide full ad set details, or create one ad set manually in Ads Manager first."
        )
    targeting = tmpl.get("targeting")
    if not targeting:
        raise RuntimeError(f"Template ad set {tmpl.get('id')} has no targeting; cannot clone.")
    return create_adset(
        name=name,
        campaign_id=campaign_id,
        daily_budget_cents=daily_budget_cents,
        targeting=targeting,
        optimization_goal=tmpl.get("optimization_goal") or "OFFSITE_CONVERSIONS",
        billing_event=tmpl.get("billing_event") or "IMPRESSIONS",
        bid_strategy=tmpl.get("bid_strategy") or "LOWEST_COST_WITHOUT_CAP",
        promoted_object=tmpl.get("promoted_object"),
        attribution_spec=tmpl.get("attribution_spec"),
        destination_type=tmpl.get("destination_type"),
    )


def list_adsets(force: bool = False) -> list[dict]:
    now = time.time()
    if not force and _adsets_cache["data"] is not None and now - _adsets_cache["at"] < _ADSETS_TTL_S:
        return _adsets_cache["data"]
    out: list[dict] = []
    url = f"{GRAPH}/act_{_ad_account()}/adsets"
    params = {
        "fields": "id,name,status,effective_status,campaign{id,name}",
        "limit": 200,
        "access_token": _meta_token(),
    }
    while url:
        r = requests.get(url, params=params, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"adsets fetch failed {r.status_code}: {r.text[:300]}")
        body = r.json()
        for a in body.get("data", []):
            camp = a.get("campaign") or {}
            out.append({
                "id": a["id"],
                "name": a.get("name", ""),
                "status": a.get("status"),
                "effective_status": a.get("effective_status"),
                "campaign_id": camp.get("id"),
                "campaign_name": camp.get("name"),
            })
        url = (body.get("paging") or {}).get("next")
        params = None
    out.sort(key=lambda x: (x.get("campaign_name") or "", x.get("name") or ""))
    _adsets_cache["data"] = out
    _adsets_cache["at"] = now
    return out


# ── 6. Meta: upload video ────────────────────────────────────────────────────

def upload_video_as_page_post(
    video_path: Path,
    *,
    title: str,
    description: str,
    destination_url: str,
    cta_type: str = "SHOP_NOW",
    thumb_path: Path | None = None,
    timeout: float = 240.0,
) -> tuple[str, str]:
    """Upload video to the page as an unpublished post.

    Returns (video_id, post_id). post_id is the full <page_id>_<post_id> form
    suitable for use as object_story_id on an ad creative — this path avoids
    the dev-mode block on new dark posts created via object_story_spec.
    """
    page_id = _page_id()
    url = f"{GRAPH}/{page_id}/videos"
    data = {
        "title": title,
        "description": description,
        "published": "false",
        "call_to_action": json.dumps(
            {"type": cta_type, "value": {"link": destination_url}}
        ),
        "access_token": _page_token(),
    }
    sources: list = []
    sources.append(("source", open(video_path, "rb")))
    if thumb_path:
        sources.append(("thumb", open(thumb_path, "rb")))
    files = {
        "source": (video_path.name, sources[0][1], "video/mp4"),
    }
    if thumb_path:
        files["thumb"] = (thumb_path.name, sources[1][1], "image/jpeg")
    try:
        r = requests.post(url, data=data, files=files, timeout=300)
    finally:
        for _, fh in sources:
            try:
                fh.close()
            except Exception:
                pass
    if r.status_code != 200:
        raise RuntimeError(f"page video upload failed {r.status_code}: {r.text[:500]}")
    video_id = r.json().get("id")
    if not video_id:
        raise RuntimeError(f"page video upload returned no id: {r.text[:300]}")
    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        rr = requests.get(
            f"{GRAPH}/{video_id}",
            params={"fields": "post_id,status", "access_token": _page_token()},
            timeout=30,
        )
        if rr.status_code == 200:
            d = rr.json()
            post_id = d.get("post_id")
            status = (d.get("status") or {}).get("video_status") or ""
            last_status = status or last_status
            if post_id:
                return video_id, post_id
            if status == "error":
                raise RuntimeError(f"page video processing failed: {d}")
        time.sleep(3)
    raise RuntimeError(
        f"page video processed but no post_id within {timeout:.0f}s (last status: {last_status})"
    )


def create_creative_from_post(*, name: str, post_id: str) -> str:
    r = requests.post(
        f"{GRAPH}/act_{_ad_account()}/adcreatives",
        data={
            "name": name,
            "object_story_id": post_id,
            "access_token": _meta_token(),
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"creative create failed {r.status_code}: {r.text[:500]}")
    return r.json()["id"]


def create_creative_asset_feed(
    *,
    name: str,
    video_id: str,
    image_hash: str,
    primary_text: str,
    headline: str,
    destination_url: str,
    cta_type: str = "SHOP_NOW",
    description: str | None = None,
) -> str:
    """Asset-feed creative — bundles video + copy as separate assets.

    Different content model from object_story_spec.video_data: Meta combines
    the assets at delivery time, which usually sidesteps the dev-mode
    new-dark-post-by-app block (error subcode 1885183).
    """
    asset_feed_spec = {
        "videos": [{"video_id": video_id, "thumbnail_hash": image_hash}],
        "bodies": [{"text": primary_text}],
        "titles": [{"text": headline}],
        "link_urls": [{"website_url": destination_url}],
        "call_to_action_types": [cta_type],
        "ad_formats": ["SINGLE_VIDEO"],
    }
    if description:
        asset_feed_spec["descriptions"] = [{"text": description}]
    data = {
        "name": name,
        "object_story_spec": json.dumps({"page_id": _page_id()}),
        "asset_feed_spec": json.dumps(asset_feed_spec),
        "access_token": _meta_token(),
    }
    r = requests.post(f"{GRAPH}/act_{_ad_account()}/adcreatives", data=data, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"creative create failed {r.status_code}: {r.text[:500]}")
    return r.json()["id"]


def upload_image(image_path: Path) -> str:
    """Upload an image to ad account's image library. Returns image_hash."""
    url = f"{GRAPH}/act_{_ad_account()}/adimages"
    with open(image_path, "rb") as f:
        r = requests.post(
            url,
            data={"access_token": _meta_token()},
            files={"filename": (image_path.name, f, "image/jpeg")},
            timeout=120,
        )
    if r.status_code != 200:
        raise RuntimeError(f"image upload failed {r.status_code}: {r.text[:400]}")
    images = (r.json() or {}).get("images") or {}
    if not images:
        raise RuntimeError(f"unexpected adimages response: {r.text[:300]}")
    first = next(iter(images.values()))
    h = first.get("hash")
    if not h:
        raise RuntimeError(f"no hash in adimages response: {first}")
    return h


def upload_video(video_path: Path, name: str) -> str:
    """Chunked (3-phase) upload via /act_<id>/advideos. Handles any file size."""
    url = f"{GRAPH}/act_{_ad_account()}/advideos"
    token = _meta_token()
    file_size = video_path.stat().st_size

    r = requests.post(
        url,
        data={"upload_phase": "start", "file_size": str(file_size), "access_token": token},
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"video upload start failed {r.status_code}: {r.text[:400]}")
    start = r.json()
    upload_session_id = start["upload_session_id"]
    video_id = start["video_id"]
    start_offset = int(start["start_offset"])
    end_offset = int(start["end_offset"])

    with open(video_path, "rb") as f:
        while start_offset < end_offset:
            f.seek(start_offset)
            chunk = f.read(end_offset - start_offset)
            r = requests.post(
                url,
                data={
                    "upload_phase": "transfer",
                    "upload_session_id": upload_session_id,
                    "start_offset": str(start_offset),
                    "access_token": token,
                },
                files={"video_file_chunk": ("chunk", chunk, "application/octet-stream")},
                timeout=300,
            )
            if r.status_code != 200:
                raise RuntimeError(f"video upload transfer failed {r.status_code}: {r.text[:400]}")
            d = r.json()
            start_offset = int(d["start_offset"])
            end_offset = int(d["end_offset"])

    r = requests.post(
        url,
        data={
            "upload_phase": "finish",
            "upload_session_id": upload_session_id,
            "title": name,
            "access_token": token,
        },
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"video upload finish failed {r.status_code}: {r.text[:400]}")
    if not r.json().get("success"):
        raise RuntimeError(f"video upload finish returned non-success: {r.text[:400]}")
    return video_id


def wait_video_ready(video_id: str, timeout: float = 180.0) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        r = requests.get(
            f"{GRAPH}/{video_id}",
            params={"fields": "status", "access_token": _meta_token()},
            timeout=30,
        )
        if r.status_code == 200:
            status = (r.json().get("status") or {})
            vs = status.get("video_status")
            last = vs or str(status)
            if vs == "ready":
                return
            if vs == "error":
                raise RuntimeError(f"video processing failed: {status}")
        time.sleep(3)
    raise RuntimeError(f"video not ready within {timeout:.0f}s (last status: {last})")


# ── 7. Meta: create creative + ad ────────────────────────────────────────────

def create_creative(*, name: str, video_id: str, primary_text: str, headline: str,
                    destination_url: str, cta_type: str = "SHOP_NOW",
                    description: str | None = None,
                    image_hash: str | None = None,
                    image_url: str | None = None) -> str:
    video_data = {
        "video_id": video_id,
        "title": headline,
        "message": primary_text,
        "call_to_action": {"type": cta_type, "value": {"link": destination_url}},
    }
    if image_hash:
        video_data["image_hash"] = image_hash
    elif image_url:
        video_data["image_url"] = image_url
    if description:
        video_data["link_description"] = description
    object_story_spec = {"page_id": _page_id(), "video_data": video_data}
    r = requests.post(
        f"{GRAPH}/act_{_ad_account()}/adcreatives",
        data={
            "name": name,
            "object_story_spec": json.dumps(object_story_spec),
            "access_token": _meta_token(),
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"creative create failed {r.status_code}: {r.text[:400]}")
    return r.json()["id"]


def create_ad(*, adset_id: str, creative_id: str, name: str) -> str:
    r = requests.post(
        f"{GRAPH}/act_{_ad_account()}/ads",
        data={
            "name": name,
            "adset_id": adset_id,
            "creative": json.dumps({"creative_id": creative_id}),
            "status": "ACTIVE",
            "access_token": _meta_token(),
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"ad create failed {r.status_code}: {r.text[:400]}")
    return r.json()["id"]


def ads_manager_url(ad_id: str) -> str:
    return (
        "https://adsmanager.facebook.com/adsmanager/manage/ads"
        f"?act={_ad_account()}&selected_ad_ids={ad_id}"
    )


def cleanup_video(video_path: Path) -> None:
    for suffix in (".mp4", ".mp3", ".jpg"):
        p = video_path.with_suffix(suffix)
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


def check_env() -> list[str]:
    """Return list of missing required env vars."""
    missing = []
    for k in ("ACCESS_TOKEN", "AD_ACCOUNT_ID", "PAGE_ID",
              "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "RAPIDAPI_KEY"):
        if not (os.environ.get(k) or "").strip():
            missing.append(k)
    return missing


def _shutil_check() -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found on PATH")
