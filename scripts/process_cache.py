#!/usr/bin/env python3
"""
process_cache.py
Merges channels + streams + guides into lean JSON files for the static frontend.
Run by GitHub Actions after fetching raw data from iptv-org.
"""

import json
import os
import re

CACHE_DIR = "cache"


def load(filename):
    path = os.path.join(CACHE_DIR, filename)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save(filename, data):
    path = os.path.join(CACHE_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
    print(f"  Saved {filename} ({len(data)} records, {os.path.getsize(path)//1024} KB)")


def main():
    print("Loading raw data...")
    channels_raw = load("channels_raw.json")
    streams_raw  = load("streams_raw.json")
    guides_raw   = load("guides_raw.json")

    # ── Build stream index: channel_id → list of streams ────────────────────
    stream_index = {}
    for s in streams_raw:
        cid = s.get("channel")
        if not cid:
            continue
        if cid not in stream_index:
            stream_index[cid] = []
        # Keep only essential fields
        stream_index[cid].append({
            "url":    s.get("url", ""),
            "status": s.get("status", "unknown"),
            "http_referrer": s.get("http_referrer"),
            "user_agent":    s.get("user_agent"),
        })

    # ── Build guide index: channel_id → guide info ───────────────────────────
    guide_index = {}
    for g in guides_raw:
        for cid in g.get("channel", []):
            if cid not in guide_index:
                guide_index[cid] = []
            guide_index[cid].append({
                "site":     g.get("site", ""),
                "site_id":  g.get("site_id", ""),
                "lang":     g.get("lang", ""),
            })

    # ── Process channels ─────────────────────────────────────────────────────
    channels_out = []
    for ch in channels_raw:
        cid = ch.get("id", "")

        # Skip NSFW
        if ch.get("is_nsfw"):
            continue
        # Skip closed channels
        if ch.get("closed"):
            continue

        streams   = stream_index.get(cid, [])
        has_stream = len(streams) > 0

        # Skip channels with no streams at all
        if not has_stream:
            continue

        guides    = guide_index.get(cid, [])

        # Build logo URL from iptv-org CDN
        logo_url = f"https://iptv-org.github.io/iptv/logo/{cid}.png"

        channels_out.append({
            "id":         cid,
            "name":       ch.get("name", ""),
            "alt_names":  ch.get("alt_names", []),
            "country":    ch.get("country", ""),
            "categories": ch.get("categories", []),
            "logo":       logo_url,
            "website":    ch.get("website"),
            "network":    ch.get("network"),
            "streams":    streams,
            "guides":     guides[:2],  # max 2 guide sources
            "has_stream": has_stream,
        })

    # Sort: channels with streams first, then alphabetically
    channels_out.sort(key=lambda c: (not c["has_stream"], c["name"].lower()))

    # ── Save processed channels ──────────────────────────────────────────────
    save("channels.json", channels_out)

    # ── Save a slim index for initial page load (no stream/guide details) ────
    channels_slim = []
    for ch in channels_out:
        channels_slim.append({
            "id":         ch["id"],
            "name":       ch["name"],
            "country":    ch["country"],
            "categories": ch["categories"],
            "logo":       ch["logo"],
            "has_stream": ch["has_stream"],
        })
    save("channels_slim.json", channels_slim)

    # ── Build per-country index ──────────────────────────────────────────────
    country_index = {}
    for ch in channels_slim:
        cc = ch["country"]
        if cc not in country_index:
            country_index[cc] = []
        country_index[cc].append(ch["id"])

    save("country_index.json", country_index)

    # ── Build per-category index ─────────────────────────────────────────────
    cat_index = {}
    for ch in channels_slim:
        for cat in ch["categories"]:
            if cat not in cat_index:
                cat_index[cat] = []
            cat_index[cat].append(ch["id"])

    save("category_index.json", cat_index)

    # ── Save streams separately (large, lazy-loaded) ────────────────────────
    save("streams.json", [s for s in streams_raw if not s.get("is_nsfw")])

    print(f"\nDone. {len(channels_out)} channels processed ({sum(1 for c in channels_out if c['has_stream'])} with streams).")


if __name__ == "__main__":
    main()
