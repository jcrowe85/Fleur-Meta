#!/usr/bin/env python3
"""Fix single-ad VIDEO adsets by adding the missing URL variant ad.

Strategy:
1. Build {post_id → video_id} and video_post_map from final_ads.json (no API calls)
2. For each single-ad adset in new campaign, get existing ad's effective_object_story_id
3. Match eosi → video_id via the map built in step 1
4. Find partner post from video_post_map, create creative + ad
"""
from __future__ import annotations
import json, os, sys, time, urllib.request, urllib.parse
from collections import defaultdict

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TOKEN   = os.environ.get("ACCESS_TOKEN") or ""
ACCOUNT = os.environ.get("AD_ACCOUNT_ID") or ""
if not TOKEN or not ACCOUNT:
    sys.exit("ACCESS_TOKEN and AD_ACCOUNT_ID must be set (see .env).")

CAMPAIGN_ID = "120245553692860479"
HOME_URL = "https://tryfleur.com/"
PROD_URL = "https://tryfleur.com/products/bloom-hair-scalp-serum-longform"

def api_get(path, params, retries=4):
    params["access_token"] = TOKEN
    url = f"https://graph.facebook.com/v20.0/{path}?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            err = json.loads(body).get("error", {})
            if err.get("code") == 80004:  # rate limit
                wait = 60 * (attempt + 1)
                print(f"  Rate limited, waiting {wait}s…")
                time.sleep(wait)
            else:
                print(f"  API error: {body[:200]}")
                return {}
    return {}

def api_post(path, data, retries=4):
    data["access_token"] = TOKEN
    encoded = urllib.parse.urlencode(data).encode()
    url = f"https://graph.facebook.com/v20.0/{path}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=encoded, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            err = json.loads(body).get("error", {})
            if err.get("code") == 80004:
                wait = 60 * (attempt + 1)
                print(f"  Rate limited, waiting {wait}s…")
                time.sleep(wait)
            else:
                return {"error": json.loads(body).get("error", {})}
    return {"error": {"message": "max retries hit"}}

# ── Step 1: build maps from final_ads.json (no API calls) ───────────────────
with open("/tmp/final_ads.json") as f:
    qualifying = json.load(f)

# post_id → video_id (only for ads that have video_id)
post_to_video: dict[str, str] = {}
# video_id → {"home": post_id, "product": post_id}
video_post_map: dict[str, dict[str, str]] = defaultdict(dict)

for a in qualifying:
    vid = a.get("video_id")
    post = a.get("post_id")
    link = (a.get("original_link") or "").strip().rstrip("/")
    if vid and post:
        post_to_video[post] = vid
        home_stripped = HOME_URL.rstrip("/")
        if link == home_stripped or link == home_stripped + "/":
            video_post_map[vid]["home"] = post
        elif "products" in link:
            video_post_map[vid]["product"] = post

home_count = sum(1 for v in video_post_map.values() if "home" in v and "product" in v)
print(f"post→video map: {len(post_to_video)} entries")
print(f"video→posts map: {len(video_post_map)} videos, {home_count} with both URLs")

# ── Step 2: get all adsets and find single-ad ones ──────────────────────────
print("\nFetching adsets…")
result = api_get(f"{CAMPAIGN_ID}/adsets", {"fields": "id,name", "limit": 100})
adsets = result.get("data", [])
print(f"  {len(adsets)} adsets found")

single_adsets = []
for adset in adsets:
    time.sleep(0.5)  # gentle pacing
    ads_r = api_get(f"{adset['id']}/ads", {
        "fields": "id,name,creative{id,effective_object_story_id}",
        "limit": 10,
    })
    ads = ads_r.get("data", [])
    if len(ads) == 1:
        single_adsets.append({
            "adset_id": adset["id"],
            "adset_name": adset["name"],
            "ad": ads[0],
        })
    time.sleep(0.5)

print(f"  {len(single_adsets)} single-ad adsets")

# ── Step 3: fix each single-ad adset ────────────────────────────────────────
print("\nFixing…")
fixed = 0
skipped = []

for item in single_adsets:
    adset_id   = item["adset_id"]
    adset_name = item["adset_name"]
    ad         = item["ad"]
    cre        = ad.get("creative", {})
    eosi       = cre.get("effective_object_story_id", "")
    ad_name    = ad.get("name", "")

    vid = post_to_video.get(eosi)
    print(f"  [{adset_name[:40]}] ad='{ad_name}' eosi={eosi} vid={vid}")

    if not vid:
        skipped.append(f"{adset_name} (eosi={eosi}, no video_id)")
        continue

    # Determine which URL the existing ad has, find the partner
    link = ""
    for a in qualifying:
        if a.get("post_id") == eosi:
            link = (a.get("original_link") or "").strip().rstrip("/")
            break

    home_stripped = HOME_URL.rstrip("/")
    if link == home_stripped or link + "/" == HOME_URL:
        # existing is home, need product
        partner_post = video_post_map[vid].get("product")
        new_name = "product page"
    elif "products" in link:
        # existing is product, need home
        partner_post = video_post_map[vid].get("home")
        new_name = "home page"
    else:
        print(f"    ⚠ unknown link: {link}")
        skipped.append(f"{adset_name} (unknown link)")
        continue

    if not partner_post:
        print(f"    ⚠ no partner post found for vid={vid}")
        skipped.append(f"{adset_name} (no partner post for vid={vid})")
        continue

    print(f"    partner_post={partner_post} → creating creative…")

    # Create creative from partner post
    cre_r = api_post(f"act_{ACCOUNT}/adcreatives", {
        "name": f"{adset_name} | {new_name}",
        "object_story_id": partner_post,
    })
    time.sleep(1)

    if "error" in cre_r:
        print(f"    ✗ creative error: {cre_r['error'].get('message','')[:120]}")
        skipped.append(f"{adset_name} (creative error: {cre_r['error'].get('message','')[:80]})")
        continue

    new_cre_id = cre_r["id"]
    print(f"    creative={new_cre_id}, creating ad…")

    # Create ad
    ad_r = api_post(f"act_{ACCOUNT}/ads", {
        "name": new_name,
        "adset_id": adset_id,
        "creative": json.dumps({"creative_id": new_cre_id}),
        "status": "PAUSED",
    })
    time.sleep(1)

    if "error" in ad_r:
        print(f"    ✗ ad error: {ad_r['error'].get('message','')[:120]}")
        skipped.append(f"{adset_name} (ad error: {ad_r['error'].get('message','')[:80]})")
    else:
        print(f"    ✓ ad={ad_r['id']}")
        fixed += 1

print(f"\nFixed: {fixed}/{len(single_adsets)}")
if skipped:
    print(f"Skipped ({len(skipped)}):")
    for s in skipped:
        print(f"  - {s}")
