#!/usr/bin/env python3
"""Generate an HTML dashboard from Meta Ads CLI data."""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

META = shutil.which("meta") or "meta"
INSIGHT_FIELDS = "spend,impressions,clicks,ctr,cpc,reach,actions,action_values,purchase_roas,frequency"
CACHE_DIR = Path(__file__).parent / ".cache"


def run_meta(args: list[str], retries: int = 6) -> dict | list:
    delay = 8.0
    last_err = ""
    for attempt in range(retries):
        proc = subprocess.run(
            [META, "-o", "json", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        last_err = proc.stderr.strip()
        if "too many calls" in last_err.lower() or "rate" in last_err.lower() or "(17)" in last_err or "(80004)" in last_err:
            time.sleep(delay)
            delay = min(delay * 1.7, 90)
            continue
        break
    raise RuntimeError(f"meta {' '.join(args)} failed: {last_err}")


def parse_embedded(value):
    """The CLI stringifies nested objects as '<TypeName> {...json...}'. Parse back to dict."""
    if not isinstance(value, str):
        return value
    m = re.match(r"^<[^>]+>\s*(\{.*\})\s*$", value, re.DOTALL)
    if not m:
        return value
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return value


def cache_load(name: str) -> dict:
    p = CACHE_DIR / name
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def cache_save(name: str, data: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    (CACHE_DIR / name).write_text(json.dumps(data))


def list_campaigns() -> list[dict]:
    return run_meta(["ads", "campaign", "list"])


def list_ads(limit: int = 500) -> list[dict]:
    return run_meta(["ads", "ad", "list", "--limit", str(limit)])


FETCH_FAILED = {"__fetch_failed__": True}


def _window_args(date_preset: str | None, since: str | None, until: str | None) -> list[str]:
    if since:
        return ["--since", since, "--until", until or dt.date.today().isoformat()]
    return ["--date-preset", date_preset or "last_30d"]


def get_insights(campaign_id: str, date_preset: str | None = None,
                 since: str | None = None, until: str | None = None,
                 mark_failures: bool = False) -> dict | None:
    """Returns insight dict, None for 'no data', or FETCH_FAILED on error (if mark_failures)."""
    try:
        resp = run_meta([
            "ads", "insights", "get",
            *_window_args(date_preset, since, until),
            "--campaign-id", campaign_id,
            "--fields", INSIGHT_FIELDS,
        ])
    except RuntimeError:
        return FETCH_FAILED if mark_failures else None
    data = resp.get("data") or []
    return data[0] if data else None


def get_ad_insights(ad_id: str, date_preset: str | None = None,
                    since: str | None = None, until: str | None = None) -> dict | None:
    """Returns the insight dict, None for 'no data in window', or FETCH_FAILED on error."""
    try:
        resp = run_meta([
            "ads", "insights", "get",
            *_window_args(date_preset, since, until),
            "--ad-id", ad_id,
            "--fields", INSIGHT_FIELDS,
        ])
    except RuntimeError:
        return FETCH_FAILED
    data = resp.get("data") or []
    return data[0] if data else None


def get_creative(creative_id: str) -> dict | None:
    try:
        resp = run_meta(["ads", "creative", "get", creative_id])
    except RuntimeError:
        return None
    if isinstance(resp, list):
        return resp[0] if resp else None
    return resp


def action_value(items: list[dict] | None, key: str) -> float:
    if not items:
        return 0.0
    for item in items:
        if item.get("action_type") == key:
            try:
                return float(item.get("value") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def merge(campaign: dict, insight: dict | None) -> dict:
    row = {
        "id": campaign["id"],
        "name": campaign.get("name", "(unnamed)"),
        "status": campaign.get("effective_status", ""),
        "objective": campaign.get("objective", ""),
        "daily_budget_cents": int(campaign.get("daily_budget") or 0),
        "lifetime_budget_cents": int(campaign.get("lifetime_budget") or 0),
        "start_time": campaign.get("start_time", ""),
        "spend": 0.0,
        "impressions": 0,
        "clicks": 0,
        "ctr": 0.0,
        "cpc": 0.0,
        "reach": 0,
        "frequency": 0.0,
        "purchases": 0,
        "purchase_value": 0.0,
        "roas": 0.0,
        "cpa": 0.0,
        "has_data": False,
    }
    if not insight:
        return row
    row["has_data"] = True
    row["spend"] = float(insight.get("spend") or 0)
    row["impressions"] = int(insight.get("impressions") or 0)
    row["clicks"] = int(insight.get("clicks") or 0)
    row["ctr"] = float(insight.get("ctr") or 0)
    row["cpc"] = float(insight.get("cpc") or 0)
    row["reach"] = int(insight.get("reach") or 0)
    row["frequency"] = float(insight.get("frequency") or 0)
    actions = insight.get("actions") or []
    values = insight.get("action_values") or []
    row["purchases"] = int(action_value(actions, "purchase"))
    row["purchase_value"] = action_value(values, "purchase")
    if row["spend"] > 0:
        row["roas"] = row["purchase_value"] / row["spend"]
        if row["purchases"]:
            row["cpa"] = row["spend"] / row["purchases"]
    return row


CREATIVE_ID_RE = re.compile(r'"id"\s*:\s*"(\d+)"')


def extract_creative_id(creative_field) -> str | None:
    if isinstance(creative_field, dict):
        return creative_field.get("id")
    if isinstance(creative_field, str):
        m = CREATIVE_ID_RE.search(creative_field)
        if m:
            return m.group(1)
    return None


def merge_ad(ad: dict, insight: dict | None, creative: dict | None, campaign_name: str) -> dict:
    spend = float((insight or {}).get("spend") or 0)
    impressions = int((insight or {}).get("impressions") or 0)
    clicks = int((insight or {}).get("clicks") or 0)
    actions = (insight or {}).get("actions") or []
    values = (insight or {}).get("action_values") or []
    purchases = int(action_value(actions, "purchase"))
    revenue = action_value(values, "purchase")
    roas = revenue / spend if spend > 0 else 0
    cpa = spend / purchases if purchases > 0 else 0
    ctr = float((insight or {}).get("ctr") or 0)

    thumb = ""
    story_id = ""
    body_text = ""
    title_text = ""
    video_ids: list[str] = []
    if creative:
        thumb = creative.get("thumbnail_url") or ""
        story_id = creative.get("effective_object_story_id") or ""
        spec = parse_embedded(creative.get("asset_feed_spec"))
        if isinstance(spec, dict):
            bodies = spec.get("bodies") or []
            if bodies and isinstance(bodies[0], dict):
                body_text = bodies[0].get("text", "")
            titles = spec.get("titles") or []
            if titles and isinstance(titles[0], dict):
                title_text = titles[0].get("text", "")
            for v in (spec.get("videos") or []):
                if isinstance(v, dict) and v.get("video_id"):
                    video_ids.append(v["video_id"])
                    if not thumb:
                        thumb = v.get("thumbnail_url", "")

    fb_url = ""
    if story_id and "_" in story_id:
        page_id, post_id = story_id.split("_", 1)
        fb_url = f"https://www.facebook.com/{page_id}/posts/{post_id}"

    return {
        "id": ad["id"],
        "name": ad.get("name", "(unnamed)"),
        "status": ad.get("effective_status", ""),
        "campaign_id": ad.get("campaign_id", ""),
        "campaign_name": campaign_name,
        "thumbnail_url": thumb,
        "fb_url": fb_url,
        "title": title_text,
        "body": body_text,
        "video_ids": video_ids,
        "spend": spend,
        "impressions": impressions,
        "clicks": clicks,
        "ctr": ctr,
        "purchases": purchases,
        "purchase_value": revenue,
        "roas": roas,
        "cpa": cpa,
    }


def fmt_money(v: float) -> str:
    return f"${v:,.2f}"


def fmt_int(v: int | float) -> str:
    return f"{int(v):,}"


def fmt_pct(v: float) -> str:
    return f"{v:.2f}%"


def fmt_roas(v: float) -> str:
    return f"{v:.2f}x" if v else "—"


def status_badge(status: str) -> str:
    s = status or "UNKNOWN"
    cls = {
        "ACTIVE": "ok",
        "PAUSED": "muted",
        "DELETED": "bad",
        "ARCHIVED": "muted",
        "WITH_ISSUES": "warn",
        "CAMPAIGN_PAUSED": "muted",
        "ADSET_PAUSED": "muted",
    }.get(s, "muted")
    return f'<span class="badge {cls}">{html.escape(s)}</span>'


def render_row(row: dict) -> str:
    daily = row["daily_budget_cents"] / 100 if row["daily_budget_cents"] else 0
    daily_str = fmt_money(daily) if daily else "—"
    return (
        "<tr>"
        f'<td class="name" title="{html.escape(row["id"])}">{html.escape(row["name"])}</td>'
        f"<td>{status_badge(row['status'])}</td>"
        f"<td class='num'>{fmt_money(row['spend'])}</td>"
        f"<td class='num'>{fmt_int(row['impressions'])}</td>"
        f"<td class='num'>{fmt_int(row['clicks'])}</td>"
        f"<td class='num'>{fmt_pct(row['ctr'])}</td>"
        f"<td class='num'>{fmt_money(row['cpc']) if row['cpc'] else '—'}</td>"
        f"<td class='num'>{fmt_int(row['purchases'])}</td>"
        f"<td class='num'>{fmt_money(row['purchase_value'])}</td>"
        f"<td class='num roas'>{fmt_roas(row['roas'])}</td>"
        f"<td class='num'>{fmt_money(row['cpa']) if row['cpa'] else '—'}</td>"
        f"<td class='num'>{daily_str}</td>"
        "</tr>"
    )


def render_top_card(row: dict, rank: int) -> str:
    return f"""
    <div class="top-card">
      <div class="rank">#{rank}</div>
      <div class="top-name" title="{html.escape(row['id'])}">{html.escape(row['name'])}</div>
      <div class="top-roas">{fmt_roas(row['roas'])}</div>
      <div class="top-stats">
        <div><span>Spend</span><strong>{fmt_money(row['spend'])}</strong></div>
        <div><span>Revenue</span><strong>{fmt_money(row['purchase_value'])}</strong></div>
        <div><span>Purchases</span><strong>{fmt_int(row['purchases'])}</strong></div>
        <div><span>CPA</span><strong>{fmt_money(row['cpa']) if row['cpa'] else '—'}</strong></div>
      </div>
      {status_badge(row['status'])}
    </div>
    """


def render_ad_card(ad: dict, rank: int) -> str:
    thumb = html.escape(ad["thumbnail_url"]) if ad["thumbnail_url"] else ""
    has_video = bool(ad["video_ids"])
    video_badge = '<div class="ad-video-badge">▶ Video</div>' if has_video else ""
    if thumb:
        media = f'<div class="ad-thumb"><img src="{thumb}" loading="lazy" alt="" />{video_badge}</div>'
    else:
        media = '<div class="ad-thumb ad-thumb-empty"><span>No preview</span></div>'
    roas_class = "ok" if ad["roas"] >= 1 else ("warn" if ad["roas"] > 0 else "muted")
    data_attrs = (
        f'data-id="{html.escape(ad["id"])}" '
        f'data-name="{html.escape(ad["name"])}" '
        f'data-title="{html.escape(ad["title"])}" '
        f'data-body="{html.escape(ad["body"])}" '
        f'data-thumb="{thumb}" '
        f'data-fb="{html.escape(ad["fb_url"])}" '
        f'data-campaign="{html.escape(ad["campaign_name"])}" '
        f'data-spend="{ad["spend"]:.2f}" '
        f'data-revenue="{ad["purchase_value"]:.2f}" '
        f'data-roas="{ad["roas"]:.3f}" '
        f'data-purchases="{ad["purchases"]}" '
        f'data-impressions="{ad["impressions"]}" '
        f'data-clicks="{ad["clicks"]}" '
        f'data-ctr="{ad["ctr"]:.2f}" '
        f'data-cpa="{ad["cpa"]:.2f}"'
    )
    return f"""
    <div class="ad-card" {data_attrs}>
      <div class="ad-rank">#{rank}</div>
      {media}
      <div class="ad-meta">
        <div class="ad-name" title="{html.escape(ad['name'])}">{html.escape(ad['name'] or '(unnamed)')}</div>
        <div class="ad-campaign" title="{html.escape(ad['campaign_name'])}">{html.escape(ad['campaign_name'])}</div>
        <div class="ad-stats">
          <div class="ad-roas {roas_class}">{fmt_roas(ad['roas'])}</div>
          <div class="ad-secondary">
            <span>{fmt_money(ad['spend'])} spend</span>
            <span>{fmt_int(ad['purchases'])} purch</span>
          </div>
        </div>
      </div>
    </div>
    """


def build_html(rows: list[dict], date_preset: str, generated_at: str,
               ads: list[dict] | None = None, ads_window_label: str = "",
               ads_quality: dict | None = None) -> str:
    active = [r for r in rows if r["has_data"] and r["spend"] > 0]
    totals = {
        "spend": sum(r["spend"] for r in active),
        "revenue": sum(r["purchase_value"] for r in active),
        "purchases": sum(r["purchases"] for r in active),
        "impressions": sum(r["impressions"] for r in active),
        "clicks": sum(r["clicks"] for r in active),
    }
    total_roas = totals["revenue"] / totals["spend"] if totals["spend"] else 0
    total_ctr = (totals["clicks"] / totals["impressions"] * 100) if totals["impressions"] else 0
    total_cpa = totals["spend"] / totals["purchases"] if totals["purchases"] else 0

    top = sorted(
        [r for r in active if r["purchases"] > 0],
        key=lambda r: (r["roas"], r["purchase_value"]),
        reverse=True,
    )[:6]

    rows_sorted = sorted(rows, key=lambda r: (r["spend"], r["purchase_value"]), reverse=True)
    table_rows = "\n".join(render_row(r) for r in rows_sorted)
    top_cards = "\n".join(render_top_card(r, i + 1) for i, r in enumerate(top)) or \
        '<div class="empty">No campaigns with purchases in this window.</div>'

    chart_labels = [r["name"][:40] for r in active[:15]]
    chart_spend = [round(r["spend"], 2) for r in active[:15]]
    chart_revenue = [round(r["purchase_value"], 2) for r in active[:15]]

    if ads:
        ads_sorted = sorted(ads, key=lambda a: a["spend"], reverse=True)
        ads_with_thumb = sum(1 for a in ads_sorted if a["thumbnail_url"])
        ad_cards = "\n".join(render_ad_card(a, i + 1) for i, a in enumerate(ads_sorted))
        window_html = (f' <span style="font-size:11px;color:var(--accent);background:rgba(91,141,239,0.12);'
                       f'padding:3px 8px;border-radius:999px;text-transform:none;letter-spacing:0;'
                       f'margin-left:6px">{html.escape(ads_window_label)}</span>') if ads_window_label else ""
        quality_banner = ""
        if ads_quality:
            msgs = []
            if ads_quality.get("rank_total") and ads_quality.get("rank_failures", 0) / ads_quality["rank_total"] > 0.20:
                msgs.append(f'{ads_quality["rank_failures"]} of {ads_quality["rank_total"]} '
                            f'campaign-rank calls failed')
            if ads_quality.get("total") and ads_quality["failed"] / ads_quality["total"] > 0.20:
                msgs.append(f'{ads_quality["failed"]} of {ads_quality["total"]} '
                            f'ad-insights calls failed')
            if msgs:
                quality_banner = (
                    f'<div style="background:rgba(240,180,0,0.12);border:1px solid var(--warn);'
                    f'color:var(--warn);padding:10px 14px;border-radius:8px;font-size:13px;'
                    f'margin-bottom:12px">'
                    f'⚠ Incomplete data: {"; ".join(msgs)} (likely rate-limited). '
                    f'Some top performers may be missing — wait a few minutes and refresh.'
                    f'</div>')
        ads_section = f"""
  <section>
    <h2>Top {len(ads_sorted)} Ads by Spend{window_html} &middot; <span style="text-transform:none;color:var(--muted);font-weight:400">{ads_with_thumb} with previews</span></h2>
    {quality_banner}
    <input type="text" class="search" id="adSearch" placeholder="Filter ads…" />
    <div class="ad-grid" id="adGrid">
      {ad_cards}
    </div>
  </section>
        """
    else:
        ads_section = ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Meta Ads Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #0b0f17;
    --panel: #131a26;
    --panel-2: #1a2333;
    --border: #243049;
    --text: #e6ecf5;
    --muted: #8a98b2;
    --accent: #5b8def;
    --ok: #36c98b;
    --warn: #f0b400;
    --bad: #ef5a5a;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    -webkit-font-smoothing: antialiased;
  }}
  header {{
    padding: 28px 40px 16px;
    border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: end; gap: 24px;
  }}
  header h1 {{ margin: 0; font-size: 22px; letter-spacing: -0.01em; }}
  header .meta {{ color: var(--muted); font-size: 13px; }}
  main {{ padding: 24px 40px 60px; max-width: 1500px; margin: 0 auto; }}
  section {{ margin-bottom: 36px; }}
  h2 {{
    font-size: 13px; text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--muted); margin: 0 0 14px; font-weight: 600;
  }}
  .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }}
  .stat {{
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    padding: 16px 18px;
  }}
  .stat .label {{ font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }}
  .stat .value {{ font-size: 24px; font-weight: 600; margin-top: 6px; letter-spacing: -0.01em; }}
  .stat .sub {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
  .stat.hero .value {{ color: var(--accent); }}

  .top-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px; }}
  .top-card {{
    background: linear-gradient(180deg, var(--panel-2), var(--panel));
    border: 1px solid var(--border); border-radius: 14px; padding: 18px;
    position: relative; overflow: hidden;
  }}
  .top-card .rank {{
    position: absolute; top: 14px; right: 16px;
    color: var(--accent); font-weight: 700; font-size: 13px;
    background: rgba(91,141,239,0.12); padding: 4px 10px; border-radius: 999px;
  }}
  .top-name {{
    font-weight: 600; font-size: 14px; margin-bottom: 14px;
    padding-right: 50px; line-height: 1.35;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }}
  .top-roas {{
    font-size: 36px; font-weight: 700; letter-spacing: -0.02em;
    color: var(--ok); margin-bottom: 14px;
  }}
  .top-stats {{
    display: grid; grid-template-columns: 1fr 1fr; gap: 10px 16px;
    padding: 12px 0; border-top: 1px solid var(--border); margin-bottom: 12px;
  }}
  .top-stats > div {{ display: flex; flex-direction: column; }}
  .top-stats span {{ color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }}
  .top-stats strong {{ font-size: 14px; font-weight: 600; margin-top: 2px; }}

  .panel {{ background: var(--panel); border: 1px solid var(--border); border-radius: 14px; padding: 18px; }}
  .chart-wrap {{ height: 360px; }}

  table {{
    width: 100%; border-collapse: collapse; font-size: 13px;
    background: var(--panel); border: 1px solid var(--border); border-radius: 14px; overflow: hidden;
  }}
  thead th {{
    text-align: left; font-weight: 600; font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.06em; color: var(--muted);
    padding: 12px 14px; background: var(--panel-2); border-bottom: 1px solid var(--border);
    position: sticky; top: 0; cursor: pointer; user-select: none;
  }}
  thead th.num {{ text-align: right; }}
  thead th:hover {{ color: var(--text); }}
  tbody td {{ padding: 11px 14px; border-bottom: 1px solid var(--border); }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tbody tr:hover {{ background: rgba(91,141,239,0.05); }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.name {{ max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  td.roas {{ font-weight: 600; color: var(--ok); }}

  .badge {{
    display: inline-block; font-size: 10px; padding: 3px 8px; border-radius: 999px;
    text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600;
  }}
  .badge.ok {{ background: rgba(54,201,139,0.15); color: var(--ok); }}
  .badge.warn {{ background: rgba(240,180,0,0.15); color: var(--warn); }}
  .badge.bad {{ background: rgba(239,90,90,0.15); color: var(--bad); }}
  .badge.muted {{ background: rgba(138,152,178,0.15); color: var(--muted); }}

  .empty {{ color: var(--muted); padding: 24px; text-align: center; }}
  .search {{
    width: 100%; max-width: 320px; padding: 9px 12px; border-radius: 8px;
    background: var(--panel-2); border: 1px solid var(--border); color: var(--text);
    font-size: 13px; margin-bottom: 12px;
  }}
  .search:focus {{ outline: none; border-color: var(--accent); }}

  /* Refresh button */
  .refresh-wrap {{ display: flex; align-items: center; gap: 12px; }}
  .refresh-btn {{
    background: var(--accent); color: #fff; border: none; border-radius: 8px;
    padding: 9px 16px; font-size: 13px; font-weight: 600; cursor: pointer;
    display: inline-flex; align-items: center; gap: 8px;
    transition: background 0.12s, opacity 0.12s;
  }}
  .refresh-btn:hover:not(:disabled) {{ background: #4a7adb; }}
  .refresh-btn:disabled {{ opacity: 0.55; cursor: not-allowed; }}
  .refresh-btn .spinner {{
    width: 12px; height: 12px; border: 2px solid rgba(255,255,255,0.4);
    border-top-color: #fff; border-radius: 50%; display: none;
    animation: spin 0.8s linear infinite;
  }}
  .refresh-btn.is-running .spinner {{ display: inline-block; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  .refresh-status {{ font-size: 12px; color: var(--muted); }}
  .refresh-status.error {{ color: var(--bad); }}
  .refresh-toast {{
    position: fixed; bottom: 24px; right: 24px; z-index: 200;
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 12px 16px; font-size: 13px; box-shadow: 0 8px 24px rgba(0,0,0,0.4);
    display: none; max-width: 380px;
  }}
  .refresh-toast.show {{ display: block; }}
  .refresh-toast.warn {{ border-color: var(--warn); }}
  .refresh-toast.bad {{ border-color: var(--bad); }}

  /* Ad grid */
  .ad-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 14px; }}
  .ad-card {{
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    overflow: hidden; cursor: pointer; transition: transform 0.12s, border-color 0.12s;
    position: relative; display: flex; flex-direction: column;
  }}
  .ad-card:hover {{ transform: translateY(-2px); border-color: var(--accent); }}
  .ad-rank {{
    position: absolute; top: 8px; left: 8px; z-index: 2;
    background: rgba(0,0,0,0.7); color: #fff; font-size: 11px; font-weight: 700;
    padding: 3px 8px; border-radius: 999px;
  }}
  .ad-thumb {{
    position: relative; aspect-ratio: 1 / 1; background: #000;
    display: flex; align-items: center; justify-content: center; overflow: hidden;
  }}
  .ad-thumb img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
  .ad-thumb-empty {{ color: var(--muted); font-size: 12px; background: var(--panel-2); }}
  .ad-video-badge {{
    position: absolute; bottom: 8px; right: 8px;
    background: rgba(0,0,0,0.75); color: #fff; font-size: 11px; font-weight: 600;
    padding: 3px 8px; border-radius: 4px;
  }}
  .ad-meta {{ padding: 12px 14px; flex: 1; display: flex; flex-direction: column; gap: 6px; }}
  .ad-name {{
    font-size: 13px; font-weight: 600; line-height: 1.3;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }}
  .ad-campaign {{
    font-size: 11px; color: var(--muted); white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
  }}
  .ad-stats {{
    display: flex; justify-content: space-between; align-items: center;
    margin-top: auto; padding-top: 8px; border-top: 1px solid var(--border);
  }}
  .ad-roas {{ font-size: 18px; font-weight: 700; letter-spacing: -0.01em; }}
  .ad-roas.ok {{ color: var(--ok); }}
  .ad-roas.warn {{ color: var(--warn); }}
  .ad-roas.muted {{ color: var(--muted); }}
  .ad-secondary {{ display: flex; flex-direction: column; align-items: flex-end; gap: 2px;
    font-size: 11px; color: var(--muted); }}

  /* Modal */
  .modal-overlay {{
    position: fixed; inset: 0; background: rgba(0,0,0,0.75); z-index: 100;
    display: none; align-items: center; justify-content: center; padding: 20px;
    backdrop-filter: blur(4px);
  }}
  .modal-overlay.open {{ display: flex; }}
  .modal {{
    background: var(--panel); border: 1px solid var(--border); border-radius: 16px;
    max-width: 920px; width: 100%; max-height: 90vh; overflow: auto;
    display: grid; grid-template-columns: 1fr 1fr;
  }}
  @media (max-width: 720px) {{ .modal {{ grid-template-columns: 1fr; }} }}
  .modal-media {{
    background: #000; min-height: 320px;
    display: flex; align-items: center; justify-content: center; position: relative;
  }}
  .modal-media img, .modal-media video {{ width: 100%; height: 100%; max-height: 90vh; object-fit: contain; }}
  .modal-body {{ padding: 24px; display: flex; flex-direction: column; gap: 14px; overflow: auto; }}
  .modal-close {{
    position: absolute; top: 12px; right: 16px; background: rgba(0,0,0,0.5); color: #fff;
    border: none; border-radius: 999px; width: 32px; height: 32px; font-size: 18px;
    cursor: pointer; z-index: 2;
  }}
  .modal h3 {{ margin: 0; font-size: 16px; line-height: 1.4; }}
  .modal .modal-title {{ font-size: 14px; font-weight: 600; color: var(--accent); }}
  .modal .modal-copy {{ font-size: 13px; color: var(--text); line-height: 1.5;
    background: var(--panel-2); padding: 12px; border-radius: 8px;
    max-height: 200px; overflow: auto; }}
  .modal-stats {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }}
  .modal-stat {{ background: var(--panel-2); padding: 10px 12px; border-radius: 8px; }}
  .modal-stat .label {{ font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }}
  .modal-stat .value {{ font-size: 16px; font-weight: 600; margin-top: 2px; }}
  .modal-actions {{ display: flex; gap: 8px; margin-top: auto; padding-top: 12px; }}
  .modal-actions a {{
    background: var(--accent); color: #fff; text-decoration: none;
    padding: 9px 14px; border-radius: 8px; font-size: 13px; font-weight: 600;
  }}
  .modal-actions a.secondary {{ background: var(--panel-2); color: var(--text); border: 1px solid var(--border); }}
</style>
</head>
<body>
<header>
  <div>
    <h1>Meta Ads Dashboard</h1>
    <div class="meta">Window: {date_preset.replace('_', ' ')} &middot; {len(active)} campaigns with spend &middot; {len(rows)} total</div>
  </div>
  <div class="refresh-wrap">
    <a href="/tiktok-ad" style="color:#4f8cff;text-decoration:none;font-size:13px;margin-right:14px;">+ Create ad from TikTok</a>
    <a href="/logout" style="color:#8a93a6;text-decoration:none;font-size:13px;margin-right:14px;">Sign out</a>
    <div class="refresh-status" id="refreshStatus">Generated {generated_at}</div>
    <button class="refresh-btn" id="refreshBtn" style="display:none">
      <span class="spinner"></span>
      <span class="label">Refresh</span>
    </button>
  </div>
</header>
<div class="refresh-toast" id="refreshToast"></div>

<main>
  <section>
    <h2>Summary</h2>
    <div class="summary">
      <div class="stat hero">
        <div class="label">ROAS</div>
        <div class="value">{fmt_roas(total_roas)}</div>
        <div class="sub">Revenue / Spend</div>
      </div>
      <div class="stat">
        <div class="label">Spend</div>
        <div class="value">{fmt_money(totals['spend'])}</div>
      </div>
      <div class="stat">
        <div class="label">Revenue</div>
        <div class="value">{fmt_money(totals['revenue'])}</div>
      </div>
      <div class="stat">
        <div class="label">Purchases</div>
        <div class="value">{fmt_int(totals['purchases'])}</div>
        <div class="sub">CPA {fmt_money(total_cpa) if total_cpa else '—'}</div>
      </div>
      <div class="stat">
        <div class="label">Impressions</div>
        <div class="value">{fmt_int(totals['impressions'])}</div>
        <div class="sub">CTR {fmt_pct(total_ctr)}</div>
      </div>
      <div class="stat">
        <div class="label">Clicks</div>
        <div class="value">{fmt_int(totals['clicks'])}</div>
      </div>
    </div>
  </section>

  <section>
    <h2>Top Performers — Ranked by ROAS</h2>
    <div class="top-grid">
      {top_cards}
    </div>
  </section>

  {ads_section}

  <section>
    <h2>Spend vs. Revenue (Top 15 by Spend)</h2>
    <div class="panel">
      <div class="chart-wrap"><canvas id="spendChart"></canvas></div>
    </div>
  </section>

  <section>
    <h2>All Campaigns</h2>
    <input type="text" class="search" id="search" placeholder="Filter campaigns…" />
    <table id="campaigns">
      <thead>
        <tr>
          <th data-key="name">Campaign</th>
          <th data-key="status">Status</th>
          <th class="num" data-key="spend">Spend</th>
          <th class="num" data-key="impressions">Impr.</th>
          <th class="num" data-key="clicks">Clicks</th>
          <th class="num" data-key="ctr">CTR</th>
          <th class="num" data-key="cpc">CPC</th>
          <th class="num" data-key="purchases">Purch.</th>
          <th class="num" data-key="revenue">Revenue</th>
          <th class="num" data-key="roas">ROAS</th>
          <th class="num" data-key="cpa">CPA</th>
          <th class="num" data-key="budget">Daily Budget</th>
        </tr>
      </thead>
      <tbody>
        {table_rows}
      </tbody>
    </table>
  </section>
</main>

<div class="modal-overlay" id="modal">
  <div class="modal">
    <div class="modal-media" id="modalMedia">
      <button class="modal-close" id="modalClose">×</button>
    </div>
    <div class="modal-body">
      <div class="modal-title" id="mTitle"></div>
      <h3 id="mName"></h3>
      <div class="modal-copy" id="mBody" style="display:none"></div>
      <div class="modal-stats">
        <div class="modal-stat"><div class="label">ROAS</div><div class="value" id="mRoas"></div></div>
        <div class="modal-stat"><div class="label">Spend</div><div class="value" id="mSpend"></div></div>
        <div class="modal-stat"><div class="label">Revenue</div><div class="value" id="mRevenue"></div></div>
        <div class="modal-stat"><div class="label">Purchases</div><div class="value" id="mPurch"></div></div>
        <div class="modal-stat"><div class="label">Clicks / CTR</div><div class="value" id="mClicks"></div></div>
        <div class="modal-stat"><div class="label">CPA</div><div class="value" id="mCpa"></div></div>
      </div>
      <div style="font-size:11px;color:var(--muted)">Campaign: <span id="mCampaign"></span></div>
      <div class="modal-actions">
        <a href="#" id="mFb" target="_blank" rel="noopener">View on Facebook</a>
      </div>
    </div>
  </div>
</div>

<script>
  const ctx = document.getElementById('spendChart').getContext('2d');
  new Chart(ctx, {{
    type: 'bar',
    data: {{
      labels: {json.dumps(chart_labels)},
      datasets: [
        {{ label: 'Spend', data: {json.dumps(chart_spend)}, backgroundColor: '#5b8def' }},
        {{ label: 'Revenue', data: {json.dumps(chart_revenue)}, backgroundColor: '#36c98b' }}
      ]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ labels: {{ color: '#e6ecf5' }} }} }},
      scales: {{
        x: {{ ticks: {{ color: '#8a98b2', maxRotation: 60, minRotation: 45 }}, grid: {{ color: '#243049' }} }},
        y: {{ ticks: {{ color: '#8a98b2', callback: v => '$' + v.toLocaleString() }}, grid: {{ color: '#243049' }} }}
      }}
    }}
  }});

  // Search filter
  const search = document.getElementById('search');
  const tbody = document.querySelector('#campaigns tbody');
  search.addEventListener('input', () => {{
    const q = search.value.toLowerCase();
    [...tbody.rows].forEach(r => {{
      r.style.display = r.cells[0].textContent.toLowerCase().includes(q) ? '' : 'none';
    }});
  }});

  // Sortable columns
  document.querySelectorAll('th[data-key]').forEach((th, idx) => {{
    let asc = false;
    th.addEventListener('click', () => {{
      asc = !asc;
      const rows = [...tbody.rows];
      const numeric = th.classList.contains('num');
      rows.sort((a, b) => {{
        const av = a.cells[idx].textContent.replace(/[$,x%]/g, '').trim();
        const bv = b.cells[idx].textContent.replace(/[$,x%]/g, '').trim();
        if (numeric) {{
          const an = parseFloat(av) || 0, bn = parseFloat(bv) || 0;
          return asc ? an - bn : bn - an;
        }}
        return asc ? av.localeCompare(bv) : bv.localeCompare(av);
      }});
      rows.forEach(r => tbody.appendChild(r));
    }});
  }});

  // Ad search
  const adSearch = document.getElementById('adSearch');
  if (adSearch) {{
    const cards = document.querySelectorAll('#adGrid .ad-card');
    adSearch.addEventListener('input', () => {{
      const q = adSearch.value.toLowerCase();
      cards.forEach(c => {{
        const text = (c.dataset.name + ' ' + c.dataset.campaign + ' ' + c.dataset.title + ' ' + c.dataset.body).toLowerCase();
        c.style.display = text.includes(q) ? '' : 'none';
      }});
    }});
  }}

  // Modal
  const modal = document.getElementById('modal');
  const modalMedia = document.getElementById('modalMedia');
  const fmtMoney = v => '$' + Number(v).toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
  const fmtInt = v => Number(v).toLocaleString();

  document.querySelectorAll('.ad-card').forEach(card => {{
    card.addEventListener('click', () => {{
      const d = card.dataset;
      document.getElementById('mName').textContent = d.name || '(unnamed)';
      document.getElementById('mTitle').textContent = d.title || '';
      const body = document.getElementById('mBody');
      if (d.body) {{ body.textContent = d.body; body.style.display = 'block'; }}
      else {{ body.style.display = 'none'; }}
      const roas = parseFloat(d.roas) || 0;
      document.getElementById('mRoas').textContent = roas > 0 ? roas.toFixed(2) + 'x' : '—';
      document.getElementById('mSpend').textContent = fmtMoney(d.spend);
      document.getElementById('mRevenue').textContent = fmtMoney(d.revenue);
      document.getElementById('mPurch').textContent = fmtInt(d.purchases);
      document.getElementById('mClicks').textContent = fmtInt(d.clicks) + ' · ' + Number(d.ctr).toFixed(2) + '%';
      const cpa = parseFloat(d.cpa) || 0;
      document.getElementById('mCpa').textContent = cpa > 0 ? fmtMoney(cpa) : '—';
      document.getElementById('mCampaign').textContent = d.campaign;
      const fb = document.getElementById('mFb');
      if (d.fb) {{ fb.href = d.fb; fb.style.display = 'inline-block'; }} else {{ fb.style.display = 'none'; }}

      // Media: use thumbnail. (Video source URLs require extra API calls; we link to FB instead.)
      modalMedia.querySelectorAll('img,video').forEach(n => n.remove());
      if (d.thumb) {{
        const img = document.createElement('img');
        img.src = d.thumb;
        modalMedia.appendChild(img);
      }}
      modal.classList.add('open');
    }});
  }});
  document.getElementById('modalClose').addEventListener('click', () => modal.classList.remove('open'));
  modal.addEventListener('click', e => {{ if (e.target === modal) modal.classList.remove('open'); }});
  document.addEventListener('keydown', e => {{ if (e.key === 'Escape') modal.classList.remove('open'); }});

  // ------- Refresh button (only active when served by server.py) -------
  const btn = document.getElementById('refreshBtn');
  const statusEl = document.getElementById('refreshStatus');
  const toast = document.getElementById('refreshToast');
  const label = btn.querySelector('.label');
  let pollTimer = null;
  let countdownTimer = null;
  let initialMtime = null;

  const showToast = (msg, kind = '') => {{
    toast.textContent = msg;
    toast.className = 'refresh-toast show ' + kind;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => toast.classList.remove('show'), 4500);
  }};

  const relTime = (ts) => {{
    if (!ts) return '';
    const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    return Math.floor(s / 3600) + 'h ago';
  }};

  const setRunning = (running) => {{
    btn.classList.toggle('is-running', running);
    btn.disabled = running;
    label.textContent = running ? 'Refreshing…' : 'Refresh';
  }};

  const setCooldown = (remaining) => {{
    clearInterval(countdownTimer);
    if (remaining <= 0) {{
      btn.disabled = false;
      label.textContent = 'Refresh';
      return;
    }}
    btn.disabled = true;
    let r = remaining;
    const tick = () => {{
      label.textContent = `Wait ${{r}}s`;
      if (--r < 0) {{ clearInterval(countdownTimer); btn.disabled = false; label.textContent = 'Refresh'; }}
    }};
    tick();
    countdownTimer = setInterval(tick, 1000);
  }};

  const applyStatus = (s, fromRefresh = false) => {{
    if (s.last_finished) {{
      statusEl.classList.toggle('error', !!s.last_error);
      statusEl.textContent = s.last_error
        ? `Last refresh failed (${{relTime(s.last_finished)}})`
        : `Refreshed ${{relTime(s.last_finished)}}`;
    }}
    if (s.running) {{
      setRunning(true);
      const elapsed = s.last_started ? Math.floor(s.now - s.last_started) : 0;
      label.textContent = `Refreshing… ${{elapsed}}s`;
      return;
    }}
    setRunning(false);
    if (s.cooldown_remaining > 0) setCooldown(s.cooldown_remaining);
    if (fromRefresh && !s.last_error && s.html_mtime && initialMtime && s.html_mtime > initialMtime) {{
      showToast('Dashboard refreshed — reloading…', '');
      setTimeout(() => location.reload(), 700);
    }} else if (fromRefresh && s.last_error) {{
      showToast('Refresh failed: ' + s.last_error.split('\\n').pop(), 'bad');
    }}
  }};

  const poll = (fromRefresh = false) => {{
    fetch('/api/status', {{ cache: 'no-store' }})
      .then(r => r.json())
      .then(s => {{
        if (initialMtime === null) initialMtime = s.html_mtime;
        applyStatus(s, fromRefresh);
        if (s.running) {{
          clearTimeout(pollTimer);
          pollTimer = setTimeout(() => poll(true), 2500);
        }}
      }})
      .catch(() => {{}});
  }};

  // Probe server availability; if absent (file://), keep button hidden.
  fetch('/api/status', {{ cache: 'no-store' }}).then(r => {{
    if (!r.ok) throw new Error('no api');
    return r.json();
  }}).then(s => {{
    btn.style.display = 'inline-flex';
    initialMtime = s.html_mtime;
    applyStatus(s);
    // Background ticker to keep "Refreshed Xm ago" fresh
    setInterval(() => {{
      if (!btn.classList.contains('is-running') && s.last_finished) {{
        statusEl.textContent = s.last_error
          ? `Last refresh failed (${{relTime(s.last_finished)}})`
          : `Refreshed ${{relTime(s.last_finished)}}`;
      }}
    }}, 15000);
  }}).catch(() => {{ /* served as file:// — button stays hidden */ }});

  btn.addEventListener('click', () => {{
    if (btn.disabled) return;
    setRunning(true);
    label.textContent = 'Starting…';
    fetch('/api/refresh', {{ method: 'POST' }})
      .then(r => r.json().then(j => ({{ ok: r.ok, status: r.status, body: j }})))
      .then(({{ ok, status, body }}) => {{
        if (status === 429) {{
          showToast(body.message || 'Cooldown active', 'warn');
          setRunning(false);
          setCooldown(body.cooldown_remaining || 0);
          return;
        }}
        if (status === 202) {{
          showToast(body.status === 'running' ? 'Already refreshing…' : 'Refresh started', '');
          poll(true);
          return;
        }}
        showToast('Unexpected response (' + status + ')', 'bad');
        setRunning(false);
      }})
      .catch(err => {{
        showToast('Refresh request failed: ' + err.message, 'bad');
        setRunning(false);
      }});
  }});
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Meta Ads HTML dashboard")
    parser.add_argument("--date-preset", default="last_30d",
                        choices=["today", "yesterday", "last_3d", "last_7d", "last_14d",
                                 "last_30d", "last_90d", "this_month", "last_month"])
    parser.add_argument("--output", "-o", default="dashboard.html")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--include-ads", action="store_true",
                        help="Also fetch individual ads + creative thumbnails (slower, more API calls)")
    parser.add_argument("--top-ads", type=int, default=30,
                        help="When --include-ads, number of top-spending ads to fetch creatives for")
    parser.add_argument("--ad-limit", type=int, default=500,
                        help="Max ads to pull from the account before filtering")
    default_ads_since = (dt.date.today() - dt.timedelta(days=36 * 30)).isoformat()
    parser.add_argument("--ads-since", default=default_ads_since,
                        help=f"Start date (YYYY-MM-DD) for ad-level insights. "
                             f"Default = {default_ads_since} (~36 months back — Meta caps at 37).")
    parser.add_argument("--ads-until", default=None,
                        help="End date for ad-level insights. Default = today.")
    parser.add_argument("--ads-match-window", action="store_true",
                        help="Use the same date range as the rest of the dashboard for ads "
                             "(instead of the default all-time window).")
    parser.add_argument("--ads-top-campaigns", type=int, default=10,
                        help="Only scan ads in the top N campaigns by spend over the ads window. "
                             "Keeps the number of API calls manageable.")
    parser.add_argument("--ranking-ttl-hours", type=int, default=12,
                        help="Reuse the cached campaign-spend ranking if it's newer than this. "
                             "Set to 0 to always refetch.")
    parser.add_argument("--refresh-ranking", action="store_true",
                        help="Force re-fetch of the campaign-spend ranking, ignoring cache.")
    args = parser.parse_args()

    print(f"→ Fetching campaign list…", flush=True)
    campaigns = list_campaigns()
    print(f"  {len(campaigns)} campaigns", flush=True)

    print(f"→ Fetching insights ({args.date_preset}) for each campaign…", flush=True)
    rows: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(get_insights, c["id"], args.date_preset): c for c in campaigns}
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            campaign = futures[fut]
            insight = fut.result()
            rows.append(merge(campaign, insight))
            done += 1
            print(f"  [{done}/{len(campaigns)}] {campaign['name'][:60]}", flush=True)

    ads_data: list[dict] = []
    ads_window_label = ""
    ads_data_quality: dict | None = None
    if args.include_ads:
        campaign_name_by_id = {c["id"]: c.get("name", "") for c in campaigns}

        if args.ads_match_window:
            ads_since: str | None = None
            ads_until: str | None = None
            ads_preset: str | None = args.date_preset
            ads_window_label = args.date_preset.replace("_", " ")
            campaign_spend = {r["id"]: r["spend"] for r in rows}
            rank_failures = 0
        else:
            ads_since = args.ads_since
            ads_until = args.ads_until
            ads_preset = None
            ads_window_label = f"since {ads_since}"
            cutoff_36mo = (dt.date.today() - dt.timedelta(days=36 * 30)).isoformat()
            if ads_since <= cutoff_36mo:
                ads_window_label = "max history (~36mo)"
            cache_key = f"campaign_spend_{ads_since}_{ads_until or 'today'}.json"
            rank_cache = cache_load(cache_key)
            ttl_s = args.ranking_ttl_hours * 3600
            now_ts = time.time()
            campaign_spend = {}
            rank_failures = 0
            fresh = (not args.refresh_ranking
                     and rank_cache.get("fetched_at")
                     and now_ts - rank_cache["fetched_at"] < ttl_s
                     and rank_cache.get("spend"))
            if fresh:
                campaign_spend = {k: float(v) for k, v in rank_cache["spend"].items()}
                age_min = int((now_ts - rank_cache["fetched_at"]) / 60)
                print(f"\n→ Using cached campaign ranking (window: {ads_window_label}, "
                      f"age: {age_min}m)", flush=True)
            else:
                print(f"\n→ Fetching campaign insights (window: {ads_window_label}) "
                      f"to rank campaigns for ad scanning…", flush=True)
                with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                    futures = {pool.submit(get_insights, c["id"], None, ads_since, ads_until, True): c
                               for c in campaigns}
                    done = 0
                    for fut in concurrent.futures.as_completed(futures):
                        c = futures[fut]
                        res = fut.result()
                        if res is FETCH_FAILED:
                            rank_failures += 1
                            campaign_spend[c["id"]] = 0.0
                        else:
                            campaign_spend[c["id"]] = float((res or {}).get("spend") or 0)
                        done += 1
                        if done % 10 == 0 or done == len(campaigns):
                            print(f"  campaign-rank [{done}/{len(campaigns)}] "
                                  f"(failed: {rank_failures})", flush=True)
                if rank_failures / max(len(campaigns), 1) <= 0.20:
                    cache_save(cache_key, {"fetched_at": now_ts, "spend": campaign_spend})
                else:
                    print(f"  ! ranking pass too unreliable ({rank_failures}/{len(campaigns)} "
                          f"failed) — not caching", flush=True)

        ranked_campaign_ids = [cid for cid, sp in sorted(
            campaign_spend.items(), key=lambda kv: kv[1], reverse=True) if sp > 0]
        top_campaign_ids = set(ranked_campaign_ids[: args.ads_top_campaigns])

        if not top_campaign_ids:
            print(f"  ! no spending campaigns identified ({rank_failures} ranking failures). "
                  f"Skipping ad scan to avoid making things worse.", flush=True)
            ads_data_quality = {"total": 0, "failed": 0, "ok": 0,
                                "rank_failures": rank_failures, "rank_total": len(campaigns)}
        else:
            print(f"  {len(ranked_campaign_ids)} campaigns with spend; scanning ads "
                  f"in top {len(top_campaign_ids)}", flush=True)

            print(f"\n→ Fetching ad list (limit {args.ad_limit})…", flush=True)
            all_ads = list_ads(limit=args.ad_limit)
            print(f"  {len(all_ads)} ads in account", flush=True)
            relevant_ads = [a for a in all_ads if a.get("campaign_id") in top_campaign_ids]
            print(f"  {len(relevant_ads)} ads to scan — fetching ad-level insights "
                  f"({ads_window_label})…", flush=True)

            insights_by_ad: dict[str, dict | None] = {}
            failed_ad_ids: set[str] = set()
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(get_ad_insights, a["id"], ads_preset, ads_since, ads_until): a
                           for a in relevant_ads}
                done = 0
                for fut in concurrent.futures.as_completed(futures):
                    ad = futures[fut]
                    result = fut.result()
                    if result is FETCH_FAILED:
                        failed_ad_ids.add(ad["id"])
                        insights_by_ad[ad["id"]] = None
                    else:
                        insights_by_ad[ad["id"]] = result
                    done += 1
                    if done % 10 == 0 or done == len(relevant_ads):
                        print(f"  ad-insights [{done}/{len(relevant_ads)}] "
                              f"(failed so far: {len(failed_ad_ids)})", flush=True)
            ads_data_quality = {
                "total": len(relevant_ads),
                "failed": len(failed_ad_ids),
                "ok": len(relevant_ads) - len(failed_ad_ids),
                "rank_failures": rank_failures,
                "rank_total": len(campaigns),
            }
            if relevant_ads and len(failed_ad_ids) / len(relevant_ads) > 0.20:
                pct = round(len(failed_ad_ids) / len(relevant_ads) * 100)
                print(f"  ⚠ {pct}% of ad-insights calls failed — likely rate limited", flush=True)

            ranked = sorted(
                relevant_ads,
                key=lambda a: float((insights_by_ad.get(a["id"]) or {}).get("spend") or 0),
                reverse=True,
            )
            spending_ads = [a for a in ranked if float((insights_by_ad.get(a["id"]) or {}).get("spend") or 0) > 0]
            top_ads = spending_ads[: args.top_ads]
            print(f"\n→ Fetching creatives for top {len(top_ads)} ads…", flush=True)

            creative_cache = cache_load("creatives.json")
            creative_ids = {extract_creative_id(a.get("creative")) for a in top_ads}
            creative_ids.discard(None)
            to_fetch = [cid for cid in creative_ids if cid not in creative_cache]
            print(f"  {len(creative_cache)} cached, {len(to_fetch)} to fetch", flush=True)
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(get_creative, cid): cid for cid in to_fetch}
                done = 0
                for fut in concurrent.futures.as_completed(futures):
                    cid = futures[fut]
                    creative_cache[cid] = fut.result() or {}
                    done += 1
                    if done % 5 == 0 or done == len(to_fetch):
                        print(f"  creatives [{done}/{len(to_fetch)}]", flush=True)
            cache_save("creatives.json", creative_cache)

            for ad in top_ads:
                cid = extract_creative_id(ad.get("creative"))
                creative = creative_cache.get(cid) if cid else None
                cname = campaign_name_by_id.get(ad.get("campaign_id", ""), "")
                ads_data.append(merge_ad(ad, insights_by_ad.get(ad["id"]), creative, cname))

    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    html_doc = build_html(rows, args.date_preset, generated,
                          ads_data or None, ads_window_label, ads_data_quality)
    out_path = Path(args.output).resolve()
    out_path.write_text(html_doc, encoding="utf-8")
    print(f"\n✔ Dashboard written to {out_path}")
    print(f"  Open: file://{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
