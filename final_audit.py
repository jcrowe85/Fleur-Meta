#!/usr/bin/env python3
import json, os, sys, urllib.request, urllib.parse, time

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TOKEN = os.environ.get("ACCESS_TOKEN") or ""
if not TOKEN:
    sys.exit("ACCESS_TOKEN must be set (see .env).")
CAMPAIGN_ID = "120245553692860479"

def api_get(path, params, retries=6):
    params["access_token"] = TOKEN
    url = f"https://graph.facebook.com/v20.0/{path}?" + urllib.parse.urlencode(params)
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            err = json.loads(e.read().decode()).get("error", {})
            if err.get("code") in (17, 80004):
                wait = 60 * (i + 1)
                print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
            else:
                print("Error:", err)
                return {}
    return {}

r = api_get(f"{CAMPAIGN_ID}/adsets", {"fields": "id,name", "limit": 100})
adsets = r.get("data", [])
print(f"=== Campaign: ABO | Winners | 5.20.26 (ID: {CAMPAIGN_ID}) ===")
print(f"Adsets: {len(adsets)}")

two = one = zero = 0
partial_names = []
for adset in adsets:
    time.sleep(0.5)
    ads_r = api_get(f"{adset['id']}/ads", {"fields": "id", "limit": 10})
    n = len(ads_r.get("data", []))
    if n == 2:
        two += 1
    elif n == 1:
        one += 1
        partial_names.append(adset["name"])
    else:
        zero += 1

print(f"Complete (2 ads): {two}")
print(f"Partial  (1 ad):  {one}")
print(f"Empty    (0 ads): {zero}")
if partial_names:
    print("Partial adsets:", partial_names)
print("\nAll adsets are PAUSED. $20/day budget each.")
