import yt_dlp
from .downloader import download_media


def inspect_playlist(url):
    """
    Extract metadata for all items in a playlist or channel without downloading.
    Returns title, uploader, item count, and individual track/video entries.
    """
    opts = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "playlistend": 250,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(url, download=False)

    if not data:
        raise ValueError("Could not extract playlist information from URL.")

    entries = []
    raw_entries = data.get("entries") or [data]
    for idx, item in enumerate(raw_entries, 1):
        if not item:
            continue
        vid = item.get("id")
        entry_url = item.get("url")
        if not entry_url and vid:
            entry_url = f"https://www.youtube.com/watch?v={vid}"

        thumb = item.get("thumbnail")
        if not thumb and vid:
            thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

        entries.append({
            "index": idx,
            "id": vid,
            "title": item.get("title") or f"Item #{idx}",
            "url": entry_url,
            "duration": item.get("duration"),
            "uploader": item.get("uploader") or item.get("channel"),
            "thumbnail": thumb,
        })

    return {
        "title": data.get("title") or "Playlist / Channel",
        "uploader": data.get("uploader") or data.get("channel") or "Unknown Creator",
        "count": len(entries),
        "total_items": len(entries),
        "entries": entries,
    }


def download_batch(urls, output_path, quality="4k", on_item_start=None, on_item_finish=None, use_stealth=True):
    """
    Sequentially process a list of URLs with progress notifications and stealth protection.
    """
    results = []
    total = len(urls)
    for i, url in enumerate(urls, 1):
        if on_item_start:
            on_item_start(i, total, url)
        try:
            info = download_media(url, output_path=output_path, quality=quality, use_stealth=use_stealth)
            results.append({"url": url, "status": "success", "title": (info or {}).get("title")})
            if on_item_finish:
                on_item_finish(i, total, url, True)
        except Exception as e:
            results.append({"url": url, "status": "error", "error": str(e)})
            if on_item_finish:
                on_item_finish(i, total, url, False)
    return results

