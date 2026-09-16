"""
Quidian - Flat playlist inspector and sequential batch runner.
Developed by Kamran Ashraf
"""

import yt_dlp

from .downloader import download_media

MAX_PLAYLIST_ITEMS = 250


def inspect_playlist(url, limit=MAX_PLAYLIST_ITEMS):
    """
    Extract metadata for every item in a playlist or channel without
    downloading. Returns title, uploader, item count and per-item entries.
    """
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = MAX_PLAYLIST_ITEMS

    opts = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "extractor_retries": 2,
        "playlistend": limit,
        # Keep going when a single entry is private or region-blocked instead
        # of failing the whole inspection.
        "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(url, download=False)

    if not data:
        raise ValueError("Could not extract playlist information from that URL.")

    entries = []
    raw_entries = data.get("entries")
    if raw_entries is None:
        raw_entries = [data]
    elif not isinstance(raw_entries, list):
        try:
            raw_entries = list(raw_entries)
        except Exception:
            raw_entries = []

    for idx, item in enumerate(raw_entries, 1):
        if not item:
            # ignoreerrors leaves a None placeholder for unavailable items.
            continue
        vid = item.get("id")
        entry_url = item.get("url") or item.get("webpage_url")
        if not entry_url and vid and (item.get("ie_key") in (None, "Youtube")):
            entry_url = f"https://www.youtube.com/watch?v={vid}"
        if not entry_url:
            continue

        thumb = item.get("thumbnail")
        if not thumb:
            thumbs = item.get("thumbnails") or []
            if thumbs:
                thumb = thumbs[-1].get("url")
        if not thumb and vid and "youtube" in str(entry_url).lower():
            thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

        is_upcoming = item.get("live_status") == "is_upcoming"

        entries.append({
            "index": len(entries) + 1,
            "id": vid,
            "title": item.get("title") or f"Item #{idx}",
            "url": entry_url,
            "duration": item.get("duration"),
            "uploader": item.get("uploader") or item.get("channel"),
            "thumbnail": thumb,
            "is_upcoming": is_upcoming,
        })

    if not entries:
        raise ValueError("That playlist or channel contains no downloadable items.")

    return {
        "title": data.get("title") or "Playlist / Channel",
        "uploader": data.get("uploader") or data.get("channel") or "Unknown Creator",
        "count": len(entries),
        "total_items": len(entries),
        "playlist_total": data.get("playlist_count") or len(entries),
        "truncated": bool(data.get("playlist_count") and data["playlist_count"] > len(entries)),
        "entries": entries,
    }


def download_batch(urls, output_path, quality="4k", on_item_start=None,
                   on_item_finish=None, use_stealth=True, cancel_check=None):
    """Process a list of URLs sequentially, reporting progress per item."""
    results = []
    total = len(urls)
    for i, url in enumerate(urls, 1):
        if cancel_check and cancel_check():
            for rem_url in urls[i - 1:]:
                results.append({"url": rem_url, "status": "cancelled"})
            break
        if on_item_start:
            on_item_start(i, total, url)
        try:
            info = download_media(
                url,
                output_path=output_path,
                quality=quality,
                use_stealth=use_stealth,
                cancel_check=cancel_check,
            )
            results.append({"url": url, "status": "success", "title": (info or {}).get("title")})
            if on_item_finish:
                on_item_finish(i, total, url, True)
        except Exception as e:
            results.append({"url": url, "status": "error", "error": str(e)})
            if on_item_finish:
                on_item_finish(i, total, url, False)
    return results
