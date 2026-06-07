#!/usr/bin/env python3
"""
process_cache.py
Merges channels + streams, validates stream URLs concurrently,
and writes lean JSON cache files for the static frontend.
Run by GitHub Actions after fetching raw data from iptv-org.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

CACHE_DIR   = "cache"
MAX_WORKERS = 40      # concurrent HEAD requests
TIMEOUT     = 8       # seconds per request
# Only validate this many streams per channel (avoids rate-limiting on huge lists)
MAX_STREAMS_TO_CHECK = 5


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
    size_kb = os.path.getsize(path) // 1024
    count   = len(data) if isinstance(data, (list, dict)) else "—"
    print(f"  ✓ {filename}  ({count} records, {size_kb} KB)")


# ── Stream validation ────────────────────────────────────────────────────────

def check_stream(stream: dict) -> dict:
    """
    Try a HEAD (then GET) request on the stream URL.
    Returns the stream dict with 'status' set to 'online' or 'error'.
    """
    url = stream.get("url", "")
    if not url:
        return {**stream, "status": "error"}

    headers = {
        "User-Agent": stream.get("user_agent") or
                      "Mozilla/5.0 (compatible; StreamGrid/1.0)",
    }
    if stream.get("http_referrer"):
        headers["Referer"] = stream["http_referrer"]

    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                ct   = resp.headers.get("Content-Type", "")
                code = resp.status
                # 200-299 and plausible content-type → online
                if 200 <= code < 300:
                    if any(k in ct for k in ("mpegurl", "octet-stream", "mp2t",
                                             "video", "audio", "x-mpegURL",
                                             "vnd.apple")):
                        return {**stream, "status": "online"}
                    # HEAD sometimes returns wrong Content-Type; accept 200 anyway
                    if method == "GET":
                        return {**stream, "status": "online"}
                # 405 = Method Not Allowed on HEAD → retry with GET
                if code == 405 and method == "HEAD":
                    continue
                return {**stream, "status": "error"}
        except Exception:
            if method == "HEAD":
                continue   # retry with GET
            return {**stream, "status": "error"}

    return {**stream, "status": "error"}


def validate_streams(stream_index: dict) -> dict:
    """
    Validate all streams concurrently.
    Returns updated stream_index with accurate 'status' values.
    Streams already marked 'online' by iptv-org are still re-checked.
    Ordering: online streams float to the top per channel.
    """
    # Flatten into (channel_id, stream) pairs for the thread pool
    tasks = []
    for cid, streams in stream_index.items():
        for s in streams[:MAX_STREAMS_TO_CHECK]:
            tasks.append((cid, s))

    total = len(tasks)
    print(f"  Validating {total} stream URLs with {MAX_WORKERS} workers "
          f"(timeout {TIMEOUT}s)…")

    results: dict[str, list] = {cid: [] for cid in stream_index}
    done    = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_to_cid = {pool.submit(check_stream, s): cid for cid, s in tasks}
        for fut in as_completed(future_to_cid):
            cid = future_to_cid[fut]
            try:
                checked = fut.result()
            except Exception as exc:
                # Shouldn't happen — check_stream catches internally
                checked = {**dict(), "status": "error"}
            results[cid].append(checked)
            done += 1
            if done % 100 == 0 or done == total:
                pct = done * 100 // total
                print(f"    {done}/{total} ({pct}%)", flush=True)

    # Sort: online first, then unknown, then error
    status_order = {"online": 0, "unknown": 1, "error": 2}
    for cid in results:
        results[cid].sort(key=lambda s: status_order.get(s.get("status","error"), 2))

    return results


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    skip_validation = "--no-validate" in sys.argv
    print("Loading raw data…")
    channels_raw = load("channels_raw.json")
    streams_raw  = load("streams_raw.json")
    guides_raw   = load("guides_raw.json")

    if not channels_raw:
        print("ERROR: channels_raw.json is empty or missing.")
        sys.exit(1)

    # ── Build stream index: channel_id → [streams] ───────────────────────────
    stream_index: dict[str, list] = {}
    for s in streams_raw:
        cid = s.get("channel")
        if not cid or s.get("is_nsfw"):
            continue
        stream_index.setdefault(cid, []).append({
            "url":           s.get("url", ""),
            "status":        s.get("status", "unknown"),
            "http_referrer": s.get("http_referrer"),
            "user_agent":    s.get("user_agent"),
        })

    # ── Validate streams ──────────────────────────────────────────────────────
    if skip_validation:
        print("  Skipping stream validation (--no-validate)")
        validated_index = stream_index
    else:
        validated_index = validate_streams(stream_index)

    # Count online after validation
    online_count = sum(
        1 for streams in validated_index.values()
        for s in streams if s.get("status") == "online"
    )
    print(f"  Online streams found: {online_count}")

    # ── Build guide index ─────────────────────────────────────────────────────
    guide_index: dict[str, list] = {}
    for g in guides_raw:
        for cid in g.get("channel", []):
            guide_index.setdefault(cid, []).append({
                "site":    g.get("site", ""),
                "site_id": g.get("site_id", ""),
                "lang":    g.get("lang", ""),
            })

    # ── Process channels ──────────────────────────────────────────────────────
    print("Building channel list…")
    channels_out = []
    for ch in channels_raw:
        cid = ch.get("id", "")
        if ch.get("is_nsfw") or ch.get("closed"):
            continue

        streams = validated_index.get(cid, [])

        if not skip_validation:
            # Only keep channels that have at least one online stream
            live_streams = [s for s in streams if s.get("status") == "online"]
            if not live_streams:
                continue
            streams = live_streams
        else:
            # Without validation, keep any channel with a URL
            if not streams:
                continue

        channels_out.append({
            "id":         cid,
            "name":       ch.get("name", ""),
            "alt_names":  ch.get("alt_names", []),
            "country":    ch.get("country", ""),
            "categories": ch.get("categories", []),
            "logo":       f"https://iptv-org.github.io/iptv/logo/{cid}.png",
            "website":    ch.get("website"),
            "network":    ch.get("network"),
            "streams":    streams,
            "guides":     guide_index.get(cid, [])[:2],
            "has_stream": True,
        })

    channels_out.sort(key=lambda c: c["name"].lower())

    # ── Save outputs ──────────────────────────────────────────────────────────
    print("Saving cache files…")
    save("channels.json", channels_out)

    channels_slim = [{
        "id":         ch["id"],
        "name":       ch["name"],
        "country":    ch["country"],
        "categories": ch["categories"],
        "logo":       ch["logo"],
        "has_stream": True,
    } for ch in channels_out]
    save("channels_slim.json", channels_slim)

    country_index: dict[str, list] = {}
    for ch in channels_slim:
        country_index.setdefault(ch["country"], []).append(ch["id"])
    save("country_index.json", country_index)

    cat_index: dict[str, list] = {}
    for ch in channels_slim:
        for cat in ch["categories"]:
            cat_index.setdefault(cat, []).append(ch["id"])
    save("category_index.json", cat_index)

    print(f"\n✓ Done — {len(channels_out)} channels with verified live streams.")


if __name__ == "__main__":
    main()
