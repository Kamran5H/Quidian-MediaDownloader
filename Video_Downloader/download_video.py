import warnings
warnings.filterwarnings("ignore")

import os
import sys
import re
import html
import time
import uuid
import shutil
import tempfile
import urllib.parse

try:
    import yt_dlp
except ImportError:
    print("[X] Error: 'yt_dlp' is not installed.")
    print("    Please install it by running: pip install -U yt-dlp")
    sys.exit(1)

try:
    import requests
except ImportError:
    requests = None

try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None

# Raised from a progress hook to abort a running download (used for "Cancel").
DownloadCancelled = getattr(yt_dlp.utils, "DownloadCancelled", None)


# ---------------------------------------------------------------------------
# Quality profiles
# ---------------------------------------------------------------------------
# yt-dlp picks the BEST stream that satisfies the constraint, then falls back.
# "4k" means: give me 4K if it exists, otherwise the next best (1440p, 1080p...).
# This is exactly the "4K if possible, otherwise 1080p (or best available)" behaviour.
QUALITY_FORMATS = {
    "best":  "bestvideo*+bestaudio/best",
    "4k":    "bestvideo[height<=2160]+bestaudio/best[height<=2160]/bestvideo*+bestaudio/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720p":  "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "audio": "bestaudio/best",
}


def _ensure_environment():
    """Add Deno to PATH if present so yt-dlp can use it natively (JS challenges)."""
    deno_path = os.path.expanduser(r"~\.deno\bin")
    if os.path.exists(deno_path) and deno_path not in os.environ.get("PATH", ""):
        os.environ["PATH"] += os.pathsep + deno_path


def _temp_dir():
    """System temp dir for intermediate files (avoids OneDrive file locks / WinError 32)."""
    temp_dir = os.path.join(tempfile.gettempdir(), "media_downloader_temp", uuid.uuid4().hex[:10])
    os.makedirs(temp_dir, exist_ok=True)
    return temp_dir


def _staging_dir():
    """A fresh LOCAL folder to download+merge into, away from OneDrive sync locks."""
    d = os.path.join(tempfile.gettempdir(), "media_downloader_stage", uuid.uuid4().hex[:10])
    os.makedirs(d, exist_ok=True)
    return d


def _move_into(output_path, stage_dir, primary_name=None, attempts=8):
    """
    Move every finished file from the local stage dir into the real output dir,
    retrying on Windows file locks (OneDrive sync holds the destination briefly).
    Returns the final path of the primary (main media) file.
    """
    os.makedirs(output_path, exist_ok=True)
    final_primary = None
    for name in os.listdir(stage_dir):
        src = os.path.join(stage_dir, name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(output_path, name)
        moved = _move_one_with_retry(src, dst, attempts)
        if primary_name and name == primary_name:
            final_primary = moved
    # Clean up the (now empty) staging folder.
    try:
        shutil.rmtree(stage_dir, ignore_errors=True)
    except Exception:
        pass
    return final_primary


def _get_unique_path(dst):
    """Ensure we never overwrite existing files; add (1), (2) instead."""
    if not os.path.exists(dst):
        return dst
    base, ext = os.path.splitext(dst)
    counter = 1
    while os.path.exists(f"{base} ({counter}){ext}"):
        counter += 1
    return f"{base} ({counter}){ext}"


def _move_one_with_retry(src, dst, attempts=8):
    dst = _get_unique_path(dst)
    for i in range(attempts):
        try:
            shutil.move(src, dst)
            return dst
        except (PermissionError, OSError):
            if i < attempts - 1:
                time.sleep(0.8)         # wait out a transient OneDrive lock
                continue
            # Last resort: save under timestamped unique name
            base, ext = os.path.splitext(dst)
            alt = f"{base} ({int(time.time())}){ext}"
            try:
                shutil.move(src, alt)
                return alt
            except Exception:
                return None


def build_ydl_opts(output_path="Downloads", quality="4k", progress_hook=None,
                   postprocessor_hook=None, cookies_from_browser=None):
    """Construct a robust yt-dlp options dict that works across most sites."""
    os.makedirs(output_path, exist_ok=True)
    _ensure_environment()

    quality = (quality or "4k").lower()
    fmt = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["4k"])
    audio_only = quality == "audio"

    ydl_opts = {
        "format": fmt,
        "paths": {"home": output_path, "temp": _temp_dir()},
        "outtmpl": {"default": "%(title).200B [%(id)s].%(ext)s"},
        "noplaylist": True,          # a single item, never a whole playlist/mix
        "windowsfilenames": True,    # strip characters Windows forbids in names
        "restrictfilenames": False,
        "ignoreerrors": False,
        "no_warnings": True,
        "quiet": True,
        "noprogress": True,
        "concurrent_fragment_downloads": 5,   # faster HLS/DASH fetches
        "retries": 10,
        "fragment_retries": 10,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }

    if audio_only:
        # Extract the best audio and transcode to a high-bitrate MP3 (great for songs).
        ydl_opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ]
        ydl_opts["writethumbnail"] = True
    else:
        # Merge video+audio into MP4 for broad device compatibility; embed subs/metadata.
        ydl_opts["merge_output_format"] = "mp4"
        ydl_opts["postprocessors"] = [
            {"key": "FFmpegMetadata"},
            {"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False},
        ]
        ydl_opts["writesubtitles"] = True
        ydl_opts["writeautomaticsub"] = False
        ydl_opts["subtitleslangs"] = ["en.*", "en"]

    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]
    if postprocessor_hook:
        ydl_opts["postprocessor_hooks"] = [postprocessor_hook]

    # Optionally reuse a logged-in browser's cookies (for private / age-gated media).
    if cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_from_browser,)

    return ydl_opts


def download_video(url, output_path="Downloads", quality="4k", progress_hook=None,
                   postprocessor_hook=None, cookies_from_browser=None, use_stealth=True):
    if not (url.startswith("http://") or url.startswith("https://")):
        print(f"\n[*] Treating input as search query: {url}")
        url = f"ytsearch1:{url}"
        
    print(f"\n[*] Preparing download: {url}  (quality={quality})")

    # Download + merge into a LOCAL staging folder, then move into the real
    # output dir. This keeps yt-dlp entirely off OneDrive (no WinError 32),
    # and only the final move touches OneDrive — with lock retries.
    os.makedirs(output_path, exist_ok=True)
    stage_dir = _staging_dir()
    ydl_opts = build_ydl_opts(stage_dir, quality, progress_hook,
                              postprocessor_hook, cookies_from_browser)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        staged_path = resolve_filepath(info)
        primary_name = os.path.basename(staged_path) if staged_path else None
        final_path = _move_into(output_path, stage_dir, primary_name)

        # Point the returned info at the real, final location.
        if final_path and isinstance(info, dict):
            downloads = info.get("requested_downloads") or []
            if downloads:
                downloads[0]["filepath"] = final_path
            else:
                info["filepath"] = final_path

        print(f"\n[OK] Download completed! Saved in '{output_path}'.")
        return info
    except Exception as e:
        shutil.rmtree(stage_dir, ignore_errors=True)
        # Let an intentional cancel bubble up unchanged.
        if DownloadCancelled and isinstance(e, DownloadCancelled):
            raise
        msg = str(e)
        print(f"\n[X] Download failed: {msg}")

        # Stealth Lock Bypass fallback
        if use_stealth and (url.startswith("http://") or url.startswith("https://")):
            err_str = msg.lower()
            if any(k in err_str for k in ["403", "forbidden", "cloudflare", "bot", "protected", "drm", "login"]):
                try:
                    _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    if _base not in sys.path:
                        sys.path.append(_base)
                    from engines.stealth_sniffer import StealthStreamInterceptor
                    print("[*] Engaging Stealth Stream Interceptor (bypassing platform protection)...")
                    interceptor = StealthStreamInterceptor(output_path=output_path)
                    return interceptor.bypass_and_extract(url, quality=quality)
                except Exception as s_err:
                    print(f"[*] Stealth bypass attempt: {s_err}")

        if isinstance(e, yt_dlp.utils.DownloadError):
            raise RuntimeError(_friendly_error(msg))
        raise RuntimeError(f"Unexpected error: {msg}")


def resolve_filepath(info):
    """Best-effort extraction of the final saved file path from an info dict."""
    if not isinstance(info, dict):
        return None
    downloads = info.get("requested_downloads") or []
    if downloads:
        return downloads[0].get("filepath") or downloads[0].get("_filename")
    return info.get("filepath") or info.get("_filename")


def _friendly_error(msg):
    """Translate common yt-dlp errors into plain guidance."""
    low = msg.lower()
    if "unsupported url" in low:
        return "This site/link isn't supported for direct extraction. Try the Search tab instead."
    if "private" in low or "login" in low or "sign in" in low:
        return "This media is private or requires login. Enable browser cookies and try again."
    if "geo" in low or "not available in your country" in low:
        return "This media is geo-blocked in your region."
    if "ffmpeg" in low:
        return "FFmpeg is required to merge/convert this media. Please install FFmpeg."
    if "http error 429" in low or "too many requests" in low:
        return "The site is rate-limiting requests. Wait a bit and try again."
    return f"Download failed: {msg}"


# ---------------------------------------------------------------------------
# Search-by-name (feature 2): find media by title, no link required
#
# Raw YouTube search order is full of junk for a bare name — reactions, reviews,
# "10 mistakes in...", explainers, low-res reuploads. We fetch a larger pool,
# detect intent (song / movie / trailer / general), filter obvious junk and
# unavailable items, then rank by relevance + official-source + popularity so
# the ACTUAL content lands on top.
# ---------------------------------------------------------------------------
import math

# Terms that signal commentary/derivative content rather than the real media.
_JUNK_TERMS = (
    "reaction", "reacts", "reacting", "review", "reviews", "reviewed",
    "explained", "explmain", "explanation", "explaination", "breakdown",
    "break down", "mistakes", "things you missed", "things you didn't",
    "easter egg", "easter eggs", "analysis", "analyzed", "recap", "recapped",
    "ending explained", "explained in", "behind the scenes", "making of",
    "bloopers", "blooper", "deleted scene", "deleted scenes", "tier list",
    "ranked", "ranking", "honest trailer", "everything wrong", "cinemasins",
    "first time watching", "commentary", "parody", "spoof", "summary",
    "summarize", "spoiler", "spoilers", "fan edit", "fan made", "fanmade",
    "fan-made", "amv", "whatsapp status", "ringtone", "8d audio", "nightcore",
    "mashup", "how to download", "download link", "movie recap", "film recap",
    "story explained", "plot explained", "top 10", "top 5", "best scenes",
    "best moments", "funny moments", "compilation", "explain",
)

_OFFICIAL_TERMS = (
    "official video", "official music video", "official audio", "official trailer",
    "official lyric", "official teaser", "official mv", "official movie",
)


def _norm_tokens(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _detect_intent(query):
    q = query.lower()
    if any(w in q for w in ("full movie", "full film", "full hd movie", "pelicula", "complete movie")):
        return "movie"
    if "movie" in q or "film" in q:
        return "movie"
    if any(w in q for w in ("trailer", "teaser")):
        return "trailer"
    if any(w in q for w in ("song", "audio", "lyrics", "lyric", "soundtrack", "ost",
                            "full album", "album", "music video", "mp3", "nasheed",
                            "naat", "qawwali", " (music", "official music")):
        return "music"
    return "general"


def _score_entry(entry, query, intent):
    title = entry.get("title") or ""
    tl = title.lower()
    uploader = (entry.get("uploader") or entry.get("channel") or "").lower()
    q = query.lower().strip()
    qtokens = _norm_tokens(query)
    ttokens = _norm_tokens(title)

    score = 0.0

    # --- Relevance: how much of the query is reflected in the title ---
    if qtokens:
        overlap = len(qtokens & ttokens) / len(qtokens)
        score += overlap * 45
        if q and q in tl:
            score += 15            # exact phrase present

    # --- Junk penalties (unless the user explicitly asked for that term) ---
    for j in _JUNK_TERMS:
        if j in tl and j.strip() not in q:
            score -= 22

    # --- Official / trusted source boosts ---
    if uploader.endswith("- topic"):
        score += 28               # auto-generated official audio channel
    if "vevo" in uploader:
        score += 24
    if any(o in tl for o in _OFFICIAL_TERMS):
        score += 16
    elif "official" in tl and "official" not in q:
        score += 6
    if entry.get("channel_is_verified") or entry.get("uploader_verified"):
        score += 8

    # --- Popularity (log views): the real thing is usually the most-watched ---
    vc = entry.get("view_count") or 0
    if vc > 0:
        score += min(22.0, math.log10(vc + 1) * 4.2)

    # --- Duration shaping by intent ---
    dur = entry.get("duration") or 0
    if intent == "music":
        if 60 <= dur <= 600:
            score += 14
        elif dur > 1800:
            score -= 16
        elif 0 < dur < 45:
            score -= 12
    elif intent == "movie":
        if dur >= 3600:
            score += 26
        elif 2400 <= dur < 3600:
            score += 10
        elif 0 < dur < 900:
            score -= 18
        if ("trailer" in tl or "teaser" in tl) and "trailer" not in q:
            score -= 16
    elif intent == "trailer":
        if "trailer" in tl or "teaser" in tl:
            score += 14
        if 30 <= dur <= 240:
            score += 8
    else:  # general
        if 0 < dur < 30:
            score -= 10
        if "trailer" in tl and "trailer" not in q and 0 < dur < 200:
            score -= 4

    return score


def _entry_badge(entry):
    uploader = (entry.get("uploader") or entry.get("channel") or "").lower()
    tl = (entry.get("title") or "").lower()
    if uploader.endswith("- topic") or "vevo" in uploader:
        return "Official Audio"
    if any(o in tl for o in _OFFICIAL_TERMS):
        return "Official"
    if entry.get("channel_is_verified") or entry.get("uploader_verified"):
        return "Verified"
    return None


def search_videos(query, count=8, provider="youtube", use_stealth=True):
    """
    Search YouTube for media by name and return RANKED candidates (no download).
    Equipped with anti-bot stealth evasion and TLS browser impersonation.
    """
    try:
        _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _base not in sys.path:
            sys.path.append(_base)
        from engines.search import search_videos as _sv
        return _sv(query, count=count, provider=provider, use_stealth=use_stealth)
    except Exception:
        pass

    _ensure_environment()

    intent = _detect_intent(query)
    pool = max(int(count) * 3, 18)
    pool = min(pool, 40)
    search_term = f"ytsearch{pool}:{query}"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,   # metadata only — fast, no per-video extraction
        "skip_download": True,
        "default_search": "ytsearch",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_term, download=False)
    except Exception as e:
        raise RuntimeError(f"Search failed: {e}")

    scored = []
    for entry in (info or {}).get("entries", []) or []:
        if not entry:
            continue
        vid = entry.get("id")
        url = entry.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else None)
        if not url or not entry.get("title"):
            continue

        # Drop items that can't be downloaded normally.
        if entry.get("live_status") in ("is_live", "is_upcoming"):
            continue
        if entry.get("availability") in ("private", "premium_only", "subscriber_only", "needs_auth"):
            continue

        thumb = entry.get("thumbnail")
        thumbs = entry.get("thumbnails") or []
        if not thumb and thumbs:
            thumb = thumbs[-1].get("url")
        if not thumb and vid:
            thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

        score = _score_entry(entry, query, intent)
        scored.append((score, {
            "title": entry.get("title") or "Untitled",
            "url": url,
            "id": vid,
            "duration": entry.get("duration"),
            "uploader": entry.get("uploader") or entry.get("channel"),
            "channel": entry.get("channel") or entry.get("uploader"),
            "thumbnail": thumb,
            "view_count": entry.get("view_count"),
            "is_official": bool(_entry_badge(entry)),
            "verified": bool(entry.get("channel_is_verified")),
            "badge": _entry_badge(entry),
            "score": round(score, 1),
            "source": "youtube",
        }))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _s, item in scored[:int(count)]]


# ---------------------------------------------------------------------------
# General web-search fallback: find media on ANY site when YouTube has no
# good match (e.g. full movies). Uses DuckDuckGo (no API key), returns page
# URLs; yt-dlp's generic extractor handles the actual download.
# ---------------------------------------------------------------------------
# Known video hosts yt-dlp handles well — bubble these to the top of results.
_VIDEO_HOSTS = (
    "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "facebook.com",
    "instagram.com", "tiktok.com", "twitter.com", "x.com", "ok.ru", "rumble.com",
    "bitchute.com", "archive.org", "streamable.com", "twitch.tv", "soundcloud.com",
    "bandcamp.com", "reddit.com", "9gag.com", "bilibili.com", "ted.com",
)
# Pages that are almost never directly downloadable — push down / drop.
_SKIP_HOSTS = ("wikipedia.org", "duckduckgo.com", "bing.com", "google.com")


def search_web(query, count=8, use_stealth=True):
    """
    Search the open web for downloadable media matching `query`.
    Returns dicts shaped like search_videos() results, with source='web'.
    """
    try:
        _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _base not in sys.path:
            sys.path.append(_base)
        from engines.search import search_web as _sw
        return _sw(query, count=count, use_stealth=use_stealth)
    except Exception:
        pass

    if DDGS is None:
        raise RuntimeError("The 'duckduckgo-search' package is required for web search. pip install duckduckgo-search")

    items = []
    try:
        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=20)
            for r in results:
                items.append((r.get('href'), r.get('title')))
    except Exception as e:
        print(f"DDGS search failed: {e}")

    # Dedupe by URL, drop noise hosts, bubble known video hosts to the top.
    seen, cleaned = set(), []
    for url, title in items:
        if not url:
            continue
        domain = urllib.parse.urlparse(url).netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        if url in seen or any(s in domain for s in _SKIP_HOSTS):
            continue
        seen.add(url)
        cleaned.append({
            "title": title,
            "url": url,
            "id": None,
            "duration": None,
            "uploader": domain,
            "thumbnail": None,
            "view_count": None,
            "source": "web",
        })

    cleaned.sort(key=lambda x: 0 if any(h in x["uploader"] for h in _VIDEO_HOSTS) else 1)
    return cleaned[:int(count)]


if __name__ == "__main__":
    print("=" * 60)
    print("              UNIVERSAL HD MEDIA DOWNLOADER              ")
    print("=" * 60)
    try:
        url = input("\nPaste any video/audio link (or leave blank to search): ").strip()
        if url:
            q = input("Quality [best/4k/1080p/720p/audio] (default 4k): ").strip() or "4k"
            download_video(url, quality=q)
        else:
            term = input("Search by name: ").strip()
            if term:
                for i, r in enumerate(search_videos(term), 1):
                    dur = r.get("duration")
                    print(f"  {i}. {r['title']}  [{dur}s]  {r['url']}")
    except KeyboardInterrupt:
        print("\n\n[!] Interrupted. Exiting.")
        sys.exit(0)
