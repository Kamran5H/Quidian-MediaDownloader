import warnings
warnings.filterwarnings("ignore")

import os
import re
import shutil
import tempfile
import uuid
import time
import urllib.parse
import subprocess
import yt_dlp

DownloadCancelled = getattr(yt_dlp.utils, "DownloadCancelled", None)

DRM_SUBSCRIPTION_PLATFORMS = {
    "primevideo.com": "Amazon Prime Video",
    "amazon.com/gp/video": "Amazon Prime Video",
    "netflix.com": "Netflix",
    "disneyplus.com": "Disney+",
    "hulu.com": "Hulu",
    "hbomax.com": "Max / HBO",
    "max.com": "Max / HBO",
    "tv.apple.com": "Apple TV+",
    "peacocktv.com": "Peacock",
    "paramountplus.com": "Paramount+",
}


def resolve_page_title(url):
    """Attempt fast metadata extraction from URL or page HTML via curl_cffi."""
    try:
        from curl_cffi import requests as cffi
        import html
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = cffi.get(url, headers=headers, impersonate="chrome124", timeout=5)
        if r.status_code in (200, 202):
            m = re.search(r'<title>(.*?)</title>', r.text, re.IGNORECASE)
            if m:
                t = html.unescape(m.group(1)).strip()
                t = re.sub(r'^(Watch|Prime Video:)\s*', '', t, flags=re.I)
                t = re.sub(r'\s*\|\s*(Prime Video|Netflix|IMDb|Disney\+?|Hulu|HBO|Apple\s*TV).*$', '', t, flags=re.I)
                t = re.sub(r'\s*-\s*(IMDb|Netflix|Prime Video).*$', '', t, flags=re.I)
                clean_t = t.strip()
                if clean_t and len(clean_t) > 1:
                    return clean_t
    except Exception:
        pass

    # Fallback to parsing URL slug if page fetch is blocked
    try:
        path = urllib.parse.urlparse(url).path
        parts = [p for p in path.split('/') if p and not p.isdigit() and len(p) > 2 and p.lower() not in ('detail', 'title', 'watch', 'gp', 'video')]
        if parts:
            slug = parts[-1].replace('-', ' ').replace('_', ' ').title()
            return slug
    except Exception:
        pass
    return None


QUALITY_FORMATS = {
    "best":  "bestvideo*+bestaudio/best",
    "4k":    "bestvideo[height<=2160]+bestaudio/best[height<=2160]/bestvideo*+bestaudio/best/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/bestvideo+bestaudio/best/best",
    "720p":  "bestvideo[height<=720]+bestaudio/best[height<=720]/bestvideo+bestaudio/best/best",
    "audio": "bestaudio/best",
}


def check_aria2c_installed():
    """Detect if aria2c is available on the system PATH for turbo download speed."""
    return bool(shutil.which("aria2c"))


def _get_unique_path(dst):
    """Ensure duplicate downloads never overwrite existing files; auto-number (1), (2)."""
    if not os.path.exists(dst):
        return dst
    base, ext = os.path.splitext(dst)
    counter = 1
    while os.path.exists(f"{base} ({counter}){ext}"):
        counter += 1
    return f"{base} ({counter}){ext}"


SIDECAR_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".json", ".description",
                ".annotations.xml", ".part", ".ytdl", ".temp"}


def _move_into(output_path, stage_dir, primary_name=None, attempts=8):
    """Safely transfer completed media from isolated staging to the real downloads directory.

    Returns the path of the *primary* media file. When yt-dlp did not tell us
    which file that is, the largest non-sidecar file wins - previously the
    first entry of an unordered listdir() could hand back a thumbnail .jpg.
    """
    os.makedirs(output_path, exist_ok=True)
    moved = []          # (original_name, destination_path, size)
    final_primary = None

    for name in sorted(os.listdir(stage_dir)):
        src = os.path.join(stage_dir, name)
        if not os.path.isfile(src):
            continue
        try:
            size = os.path.getsize(src)
        except OSError:
            size = 0
        dst = _get_unique_path(os.path.join(output_path, name))
        for i in range(attempts):
            try:
                shutil.move(src, dst)
                moved.append((name, dst, size))
                break
            except (PermissionError, OSError):
                if i < attempts - 1:
                    # Windows AV scanners and OneDrive hold brief locks on
                    # freshly written files; back off and retry.
                    time.sleep(0.6)
                    continue
                base, ext = os.path.splitext(dst)
                alt = f"{base}_{int(time.time())}{ext}"
                try:
                    shutil.move(src, alt)
                    moved.append((name, alt, size))
                except Exception:
                    pass

    if primary_name:
        for name, dst, _size in moved:
            if name == primary_name:
                final_primary = dst
                break
    if not final_primary:
        candidates = [m for m in moved if os.path.splitext(m[0])[1].lower() not in SIDECAR_EXTS]
        pool = candidates or moved
        if pool:
            final_primary = max(pool, key=lambda m: m[2])[1]

    shutil.rmtree(stage_dir, ignore_errors=True)
    return final_primary


def build_engine_opts(stage_dir, temp_dir, quality="4k", progress_hook=None,
                      postprocessor_hook=None, use_turbo=True, cookies_from_browser=None,
                      use_stealth=True):
    """Build high-reliability yt-dlp configuration with optional multi-connection aria2c booster and stealth headers."""
    quality = (quality or "4k").lower()
    fmt = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["4k"])
    audio_only = quality == "audio"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    if use_stealth:
        headers.update({
            "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        })

    opts = {
        "format": fmt,
        # Prefer highest resolution/fps, but among equal resolutions prefer the
        # UNIVERSALLY playable H.264 (avc1) + AAC in an MP4 container. yt-dlp's
        # default would pick AV1/VP9, which many phones and older players can't
        # decode ("video won't play"). H.264 tops out at 1080p on YouTube, so 4K
        # still falls back to VP9/AV1 automatically when the user asks for 4K.
        "format_sort": ["res", "fps", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"],
        "paths": {"home": stage_dir, "temp": temp_dir},
        "outtmpl": {"default": "%(title).180s [%(id)s].%(ext)s"},
        "noplaylist": True,
        "windowsfilenames": True,
        "restrictfilenames": False,
        "ignoreerrors": False,
        "no_warnings": True,
        "quiet": True,
        "noprogress": True,
        "retries": 12,
        "fragment_retries": 12,
        "extractor_retries": 3,
        # Without a socket timeout a stalled CDN connection hangs the worker
        # thread forever and the job never resolves.
        "socket_timeout": 30,
        "overwrites": False,
        "continuedl": True,
        "trim_file_name": 180,
        "http_headers": headers,
    }

    # aria2c turbo multi-connection acceleration (16 parallel streams).
    #
    # Trade-off worth knowing: yt-dlp does not call progress hooks while an
    # external downloader owns the transfer, so a cancel request cannot
    # interrupt aria2c mid-stream. It takes effect at the next format boundary
    # or at post-processing. Nothing partial ever reaches the user's folder -
    # the staging directory is wiped in `finally` - but the job can sit in
    # "Cancelling..." until the current stream finishes. Turning turbo off
    # makes cancellation immediate.
    if use_turbo and check_aria2c_installed():
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = [
            "-c", "-j", "16", "-x", "16", "-s", "16", "-k", "1M",
            "--max-tries=5", "--retry-wait=2", "--connect-timeout=20", "--timeout=30",
        ]
    else:
        opts["concurrent_fragment_downloads"] = 8

    if audio_only:
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ]
        opts["writethumbnail"] = True
    else:
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"] = [
            {"key": "FFmpegMetadata"},
        ]

    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    if postprocessor_hook:
        opts["postprocessor_hooks"] = [postprocessor_hook]
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)

    return opts


def normalize_url(raw_input):
    """
    Intelligently detect if raw_input is a URL missing schema (e.g. youtube.com/watch, udemy.com/...)
    or a YouTube Clip that should be resolved to the full original video.
    """
    s = (raw_input or "").strip().strip('"').strip("'")
    if not s:
        return None, None

    # A pasted local file path is not a search query - say so plainly instead
    # of silently running a YouTube search for "C:\Users\...\clip.mp4".
    if os.path.isfile(s):
        raise ValueError(
            "That is a local file path. Use the Audio Studio to convert local files; "
            "the downloader needs a web link or a search phrase."
        )

    # If it's already a full URL
    if s.startswith("http://") or s.startswith("https://"):
        url = s
    else:
        # Check if it looks like a domain / URL without scheme
        domain_pattern = r'^(?:www\.)?[a-zA-Z0-9-]+(?:\.[a-zA-Z]{2,})+(?:/.*)?$'
        if re.match(domain_pattern, s):
            url = f"https://{s}"
        else:
            return None, s  # It's a text query, not a URL

    # Resolve YouTube clip to the full original video so the user gets the complete video
    if "youtube.com/clip/" in url or "youtu.be/clip/" in url:
        try:
            from curl_cffi import requests as cffi
            r = cffi.get(url, impersonate="chrome124", timeout=6)
            if r.status_code == 200:
                m = re.search(r'"originalVideoId"\s*:\s*"([a-zA-Z0-9_-]{11})"', r.text)
                if not m:
                    m = re.search(r'"videoId"\s*:\s*"([a-zA-Z0-9_-]{11})"', r.text)
                if m:
                    full_id = m.group(1)
                    return f"https://www.youtube.com/watch?v={full_id}", None
        except Exception:
            pass

    return url, None


def verify_download_integrity(filepath, is_audio=False):
    """
    Validate that downloaded media file is non-empty, intact, and not damaged.
    Returns (True, None) if sound, or (False, error_message).
    """
    if not filepath or not os.path.isfile(filepath):
        return False, "Output media file was not found on disk."
    
    size = 0
    for attempt in range(5):
        try:
            size = os.path.getsize(filepath)
            break
        except (PermissionError, OSError):
            if attempt < 4:
                time.sleep(0.3)
            else:
                size = 0

    min_bytes = 20 * 1024 if is_audio else 200 * 1024  # At least 200KB for video, 20KB for audio
    if size < min_bytes:
        return False, f"Media file is truncated or corrupted (file size only {size} bytes)."
    
    # Optional FFprobe container verification if FFmpeg/FFprobe is on PATH.
    # A file that has bytes but no decodable stream is the classic "downloaded
    # but won't play" failure, so probe for *any* stream rather than a video one.
    if shutil.which("ffprobe"):
        try:
            res = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                 "-of", "default=noprint_wrappers=1:nokey=1", filepath],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
            stream_types = (res.stdout or b"").decode("utf-8", "replace").split()
            if res.returncode != 0 or not stream_types:
                return False, "File container verification failed (no decodable stream)."
            if not is_audio and "video" not in stream_types and "audio" not in stream_types:
                return False, "File container verification failed (stream unreadable)."
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass

    return True, None


def _score_movie_candidate(c, canonical_title, year=None, cast_names=None):
    """
    Strict scoring algorithm to verify that a candidate video is the authentic
    full-length movie and reject fake re-titled clickbait uploads.
    """
    score = 0
    title_lower = (c.get("title") or "").lower()
    uploader_lower = (c.get("uploader") or c.get("channel") or "").lower()
    dur = c.get("duration") or 0

    # Must be a full-length release (at least 40 minutes if duration is known)
    if 0 < dur < 2400:
        return -1000
    elif dur == 0 and not c.get("longform_source"):
        # Mild uncertainty penalty. Sources that only publish feature-length
        # material (Archive.org) declare longform_source and are exempt rather
        # than being handed a fabricated runtime.
        score -= 20

    # Disqualify common derivative junk (trailers, clips, scenes, reactions) even if duration is unknown
    JUNK_WORDS = [
        "trailer", "teaser", "promo", "reaction", "review", "song", "audio", "jukebox",
        "scene", "scenes", "climax", "clip", "clips", "making of", "behind the scenes",
        "interview", "dialogue", "status", "shorts", "recap", "explained"
    ]
    for jw in JUNK_WORDS:
        if re.search(r'\b' + re.escape(jw) + r'\b', title_lower):
            score -= 400

    clean_canon = re.sub(r'\s*\(\d{4}\)', '', canonical_title).strip().lower()
    tokens = [t for t in re.split(r'[\s\-_:]+', clean_canon) if len(t) > 0]
    for t in tokens:
        # Use word boundaries so 'don' doesn't match 'abandon' and 'war' doesn't match 'towards'
        if re.search(r'\b' + re.escape(t) + r'\b', title_lower):
            score += 25
        else:
            if len(t) >= 3 or t.isdigit():
                score -= 200  # Crucial keyword missing!
            else:
                score -= 40

    # Number / Sequel verification (e.g. '3', '2', '4')
    num_tokens = [t for t in tokens if t.isdigit()]
    for nt in num_tokens:
        if not re.search(r'\b' + re.escape(nt) + r'\b', title_lower):
            score -= 500  # Target number must be present!
    if num_tokens:
        conflicting_nums = {'1', '2', '3', '4', '5', '6', '7', '8', '9'} - set(num_tokens)
        for cn in conflicting_nums:
            for t in tokens:
                if not t.isdigit() and re.search(r'\b' + re.escape(t) + r'\s+' + re.escape(cn) + r'\b', title_lower):
                    score -= 500  # Explicit conflicting sequel phrase!

    # Year Discrepancy Detection: If canonical year is known, reject candidate if it has a conflicting release year
    if year:
        try:
            target_y = int(year)
            candidate_years = [int(y) for y in re.findall(r'\b(19\d\d|20\d\d)\b', title_lower)]
            if candidate_years and not any(abs(cy - target_y) <= 1 for cy in candidate_years):
                score -= 350
        except Exception:
            pass

    # Detect conflicting blockbuster movie titles (e.g. 3 Idiots, Dangal, PK)
    famous_conflicts = [
        "3 idiots", "dangal", "pk", "ghajini", "lagaan", "sholay",
        "krrish", "don 2", "bahubali", "kgf", "pathaan", "jawan", "chennai express"
    ]
    for conf in famous_conflicts:
        if conf in title_lower and conf not in clean_canon:
            score -= 300

    # Boost verified official film studios and distributors
    STUDIO_KEYWORDS = [
        "yrf", "yash raj", "shemaroo", "ultra", "goldmines",
        "sony", "zee", "eros", "tips", "warner", "paramount", "universal", "lionsgate", "disney", "netflix", "amazon", "hbo"
    ]
    is_studio = any(sk in uploader_lower for sk in STUDIO_KEYWORDS)
    if is_studio:
        score += 150

    # Heavily penalize notorious clickbait / pirated re-upload channels
    SPAM_CHANNEL_WORDS = [
        "wala", "hub", "clips", "status", "creation", "junction", "zone", "club", "fun", "port", "volly", "online free", "movies time", "nightcipher", "free movie", "trailers"
    ]
    if any(sw in uploader_lower for sw in SPAM_CHANNEL_WORDS):
        score -= 200

    # Platform credibility: Dailymotion and Archive.org have genuine full releases
    platform = (c.get("source") or c.get("platform") or "").lower()
    if "dailymotion" in platform and dur >= 3600:
        score += 60
    elif "archive" in platform and (dur >= 3000 or c.get("longform_source")):
        score += 50
    elif "youtube" in platform:
        is_verified = bool(c.get("verified") or c.get("is_official"))
        if not is_studio and not is_verified:
            score -= 90  # Unverified non-studio YouTube upload has high risk of fake title

    # Cast verification
    if cast_names:
        for actor in cast_names:
            for part in actor.split():
                if len(part) > 3 and re.search(r'\b' + re.escape(part.lower()) + r'\b', title_lower):
                    score += 20
        other_top_actors = ["shahrukh", "sharukh", "salman", "deepika", "ranbir", "ranveer", "akshay", "ajay devgn"]
        for ota in other_top_actors:
            if ota in title_lower and not any(ota in a.lower() for a in cast_names):
                score -= 100

    # Healthy full movie duration window (1 hr 10 mins to 4 hrs)
    if 4200 <= dur <= 14400:
        score += 30

    return score


def resolve_streaming_media_to_downloadable(url, status_callback=None, use_stealth=True):
    """
    Given a Netflix, Amazon Prime, IMDb, or subscription platform URL,
    automatically extract the title, resolve canonical release metadata,
    and find the authentic full movie / complete episode stream for direct download.
    """
    url_lower = url.lower()
    title = None
    imdb_id = None
    release_year = None
    cast_names = []

    # Check for IMDb title ID (e.g. tt1833673)
    m_imdb = re.search(r'/title/(tt\d+)', url)
    if m_imdb:
        imdb_id = m_imdb.group(1)
        from .search import search_imdb
        items = search_imdb(imdb_id, count=1)
        if items:
            title = items[0].get("title")
            release_year = items[0].get("year")
            raw_cast = items[0].get("cast") or ""
            if raw_cast:
                cast_names = [c.strip() for c in raw_cast.split(",") if c.strip()]

    # Check for Netflix search or title URL
    if not title and "netflix.com" in url_lower:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        if "q" in params:
            title = params["q"][0]
        else:
            title = resolve_page_title(url) or ""
            if not title:
                parts = [p for p in parsed.path.split('/') if p and p not in ('search', 'title', 'watch', 'browse', 'pk', 'in', 'us', 'gb')]
                if parts and not parts[-1].isdigit():
                    title = parts[-1].replace('-', ' ').title()

    # Check for Prime Video
    if not title and ("primevideo.com" in url_lower or "amazon.com" in url_lower):
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        if "phrase" in params:
            title = params["phrase"][0]
        elif "k" in params:
            title = params["k"][0]
        else:
            title = resolve_page_title(url) or ""
            if not title:
                parts = [p for p in parsed.path.split('/') if p and p not in ('detail', 'gp', 'video')]
                if parts and not re.match(r'^[A-Z0-9]{10,}$', parts[-1], re.I):
                    title = parts[-1].replace('-', ' ').title()

    # Fallback to page title
    if not title:
        title = resolve_page_title(url) or ""

    if not title:
        return None

    from .search import search_videos, search_dailymotion, search_archive_org, _clean_title_for_catalog
    clean_name = _clean_title_for_catalog(title)
    clean_query = re.sub(r'\s*\(\d{4}\)', '', clean_name).strip()
    if status_callback:
        status_callback(f"Authenticating verified movie release for '{clean_name}'...", 10)

    # Harvest candidates across multiple video platforms
    candidates = []
    try:
        dm_candidates = search_dailymotion(f"{clean_query} full movie", count=6, use_stealth=use_stealth)
        if dm_candidates:
            candidates.extend(dm_candidates)
    except Exception:
        pass

    try:
        yt_candidates = search_videos(f"{clean_query} full movie", count=6, provider="youtube", use_stealth=use_stealth)
        if yt_candidates:
            candidates.extend(yt_candidates)
    except Exception:
        pass

    try:
        arch_candidates = search_archive_org(clean_query, count=3)
        if arch_candidates:
            candidates.extend(arch_candidates)
    except Exception:
        pass

    if candidates:
        scored = []
        for c in candidates:
            s = _score_movie_candidate(c, clean_name, year=release_year, cast_names=cast_names)
            if s > 0:
                scored.append((s, c))

        scored.sort(key=lambda x: x[0], reverse=True)
        if scored:
            chosen = scored[0][1]
            if status_callback:
                dur_mins = round((chosen.get("duration") or 0) / 60)
                raw_title = chosen.get('title') or clean_name
                safe_title = raw_title.encode('ascii', 'replace').decode('ascii')
                platform = chosen.get("platform") or chosen.get("source") or "Web"
                status_callback(f"Selected verified release: {safe_title} on {platform} ({dur_mins} mins)", 15)
            return chosen.get("url")

    return None


def download_media(url, output_path, quality="4k", progress_hook=None,
                   postprocessor_hook=None, use_turbo=True, cookies_from_browser=None,
                   status_callback=None, use_stealth=True, cancel_check=None):
    """
    Execute high-speed download into an isolated staging directory,
    then relocate safely into the destination folder without leaving temp clutter.
    If 403 Forbidden or platform locks are encountered, automatically fall back to
    the StealthStreamInterceptor to bypass Cloudflare and stream protections.
    """
    def _abort_if_cancelled():
        if cancel_check and cancel_check():
            raise (DownloadCancelled() if DownloadCancelled else RuntimeError("Cancelled"))

    _abort_if_cancelled()
    resolved_url, query_term = normalize_url(url)
    if not resolved_url:
        # User entered a search query instead of a direct link.
        from .search import search_videos, search_imdb
        if status_callback:
            status_callback(f"Finding complete, authentic release for: {query_term}...", 10)
            
        # Check if query matches a known movie via IMDb
        imdb_items = []
        try:
            imdb_items = search_imdb(query_term, count=1)
        except Exception:
            pass
            
        if imdb_items and imdb_items[0].get("imdb_id"):
            clean_match = re.sub(r'\s*\(\d{4}\)', '', imdb_items[0].get("title", "")).strip().lower()
            if query_term.lower() in clean_match or clean_match in query_term.lower():
                imdb_url = f"https://www.imdb.com/title/{imdb_items[0].get('imdb_id')}/"
                resolved_stream = resolve_streaming_media_to_downloadable(imdb_url, status_callback=status_callback, use_stealth=use_stealth)
                if resolved_stream:
                    resolved_url = resolved_stream

        if not resolved_url:
            is_movie = bool(imdb_items or "movie" in query_term.lower() or "film" in query_term.lower())
            search_q = f"{query_term} full movie" if is_movie else query_term
            candidates = search_videos(search_q, count=5, provider="youtube", use_stealth=use_stealth)
            if not candidates:
                candidates = search_videos(query_term, count=3, provider="youtube", use_stealth=use_stealth)
            if not candidates:
                raise RuntimeError(f"No downloadable media found for query: '{query_term}'")
            resolved_url = candidates[0].get("url")
            if status_callback:
                status_callback(f"Selected: {candidates[0].get('title')}", 15)

    _abort_if_cancelled()
    url = resolved_url
    url_lower = url.lower()

    # Check for known DRM-protected commercial subscription services or IMDb links
    is_subscription_or_imdb = any(domain in url_lower for domain in DRM_SUBSCRIPTION_PLATFORMS) or "imdb.com" in url_lower or url.startswith("stream://")
    if is_subscription_or_imdb:
        if status_callback:
            status_callback("Subscription platform recognized. Engaging Direct Stream Resolver (bypassing login/paywall)...", 8)
        resolved_stream = resolve_streaming_media_to_downloadable(url, status_callback=status_callback, use_stealth=use_stealth)
        if resolved_stream:
            url = resolved_stream
            url_lower = url.lower()
        else:
            title = resolve_page_title(url) or ""
            suggested = title.strip()
            raise RuntimeError(f"Unable to find an open full-length stream release for '{suggested}'. Try searching for the movie by title in the Search tab.")


    # Proactive high-speed routing for known protected platforms
    if use_stealth and any(domain in url_lower for domain in ["udemy.com", "coursera.org", "skillshare.com"]):
        if status_callback:
            status_callback("Platform lock recognized. Engaging Stealth Stream Interceptor...", 10)
        from .stealth_sniffer import StealthStreamInterceptor
        interceptor = StealthStreamInterceptor(
            output_path=output_path, status_callback=status_callback, cancel_check=cancel_check
        )
        try:
            res = interceptor.bypass_and_extract(url, quality=quality)
            if res and res.get("filepath"):
                valid, err = verify_download_integrity(res["filepath"], is_audio=(quality == "audio"))
                if not valid:
                    raise RuntimeError(f"Stream verification failed: {err}")
                return res
            # A None result means the resolver found nothing; fall through to
            # the standard pipeline rather than reporting a phantom success.
            raise RuntimeError("Stealth interceptor found no downloadable stream.")
        except Exception as stealth_err:
            if DownloadCancelled and isinstance(stealth_err, DownloadCancelled):
                raise
            if status_callback:
                status_callback(f"Stealth note: {stealth_err}. Falling back to standard pipeline...", 15)

    session_id = uuid.uuid4().hex[:10]
    stage_dir = os.path.join(tempfile.gettempdir(), f"studio_stage_{session_id}")
    temp_dir = os.path.join(tempfile.gettempdir(), f"studio_chunks_{session_id}")
    os.makedirs(stage_dir, exist_ok=True)
    os.makedirs(temp_dir, exist_ok=True)

    ydl_opts = build_engine_opts(
        stage_dir, temp_dir, quality=quality,
        progress_hook=progress_hook,
        postprocessor_hook=postprocessor_hook,
        use_turbo=use_turbo,
        cookies_from_browser=cookies_from_browser,
        use_stealth=use_stealth
    )

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        staged_path = resolve_filepath(info)
        primary_name = os.path.basename(staged_path) if staged_path else None
        final_path = _move_into(output_path, stage_dir, primary_name)

        if final_path:
            valid, err = verify_download_integrity(final_path, is_audio=(quality == "audio"))
            if not valid:
                raise RuntimeError(f"Download completed but file is damaged or incomplete: {err}")
            if isinstance(info, dict):
                downloads = info.get("requested_downloads") or []
                if downloads:
                    downloads[0]["filepath"] = final_path
                else:
                    info["filepath"] = final_path

        return info
    except Exception as e:
        if DownloadCancelled and isinstance(e, DownloadCancelled):
            raise

        if "DRM_PROTECTED|" in str(e):
            raise

        err_str = str(e).lower()
        # yt-dlp phrases this as "This video is DRM protected"; the old check
        # required "widevine"/"drm protection" and therefore never matched.
        drm_markers = ("drm protected", "drm-protected", "drm protection", "widevine",
                       "playready", "fairplay", "[drm]", "protected by drm",
                       "encrypted content", "requires a decryption key")
        if any(marker in err_str for marker in drm_markers):
            title = resolve_page_title(url) or ""
            suggested = title.strip()
            title_display = f" '{suggested}'" if suggested else ""
            raise RuntimeError(
                f"DRM_PROTECTED|DRM Platform|{suggested}|"
                f"This stream is protected with DRM encryption (Widevine/PlayReady) and requires account authorization. "
                f"Direct stream decryption is not supported. "
                f"Use the 'Search Media by Name' tab to find available open releases for{title_display}."
            )

        is_locked_or_blocked = any(k in err_str for k in [
            "403", "forbidden", "cloudflare", "bot", "protected", "login", "authenticate", "udemy"
        ])

        if use_stealth and is_locked_or_blocked and (url.startswith("http://") or url.startswith("https://")):
            if status_callback:
                status_callback("Cloudflare / Platform lock detected. Activating Stealth Stream Interceptor...", 15)
            from .stealth_sniffer import StealthStreamInterceptor
            interceptor = StealthStreamInterceptor(
                output_path=output_path, status_callback=status_callback, cancel_check=cancel_check
            )
            return interceptor.bypass_and_extract(url, quality=quality)

        raise RuntimeError(f"Engine failure: {e}")
    finally:
        # Guarantee full cleanup of staging and chunks
        shutil.rmtree(stage_dir, ignore_errors=True)
        shutil.rmtree(temp_dir, ignore_errors=True)



def resolve_filepath(info):
    """Best-effort extraction of the saved file path from yt-dlp info dict."""
    if not isinstance(info, dict):
        return None
    downloads = info.get("requested_downloads") or []
    if downloads:
        return downloads[0].get("filepath") or downloads[0].get("_filename")
    return info.get("filepath") or info.get("_filename")
