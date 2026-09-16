"""
Quidian - Stealth-Powered Search Engine
Developed by Kamran Ashraf

Provides ranked, intent-aware media search across YouTube and the Open Web
with integrated anti-bot evasion and TLS browser impersonation (curl_cffi).
"""

import warnings
warnings.filterwarnings("ignore")
warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", message=".*duckduckgo_search.*")

import re
import math
import html
import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

def _clean_platform_search_query(query):
    """
    Cleans queries like 'Dhoom 3 from Netflix' or 'watch Inception on Prime Video'
    into clean media titles like 'Dhoom 3' or 'Inception'.
    """
    if not query:
        return ""
    patterns = [
        r'^(?:watch|download|stream|find)\s+',
        r'\b(?:from|on|at|in)\s+(?:netflix|amazon\s*prime\s*video|prime\s*video|amazon\s*prime|amazon|prime|imdb|disney(?:\s*\+)?|hulu|hbo\s*max|hbo|max|apple\s*tv(?:\s*\+)?|peacock|paramount(?:\s*\+)?)(\b|$)',
        r'\b(?:netflix|amazon\s*prime\s*video|prime\s*video|amazon\s*prime|amazon|prime|imdb|disney(?:\s*\+)?|hulu|hbo|apple\s*tv)\s+(?:movie|film|series|show|video)\b',
    ]
    c = query
    for p in patterns:
        c = re.sub(p, '', c, flags=re.IGNORECASE)
    c = re.sub(r'\s+', ' ', c).strip()
    return c if len(c) >= 2 else query


def _clean_title_for_catalog(query):
    """
    Cleans search terms like 'full movie', 'full hd movie', 'full video', 'full film'
    to get the canonical title (e.g. 'Dhoom 3 full movie' -> 'Dhoom 3')
    specifically for catalog lookups on IMDb, Netflix, and Prime Video.
    """
    if not query:
        return ""
    c = _clean_platform_search_query(query)
    c = re.sub(r'\b(?:full\s+movie|full\s+film|full\s+video|full\s+vedio|full\s+hd\s+movie|complete\s+movie|full\s+length|full\s+episode|full\s+tutorial|entire\s+movie|full|movie|film|vedio)\b', '', c, flags=re.IGNORECASE)
    c = re.sub(r'\s+', ' ', c).strip()
    return c if len(c) >= 2 else (_clean_platform_search_query(query) or query)



try:
    import yt_dlp
except ImportError:
    yt_dlp = None

try:
    from curl_cffi import requests as cffi_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    cffi_requests = None
    CURL_CFFI_AVAILABLE = False

try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None

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

_VIDEO_HOSTS = (
    "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "facebook.com",
    "instagram.com", "tiktok.com", "twitter.com", "x.com", "ok.ru", "rumble.com",
    "bitchute.com", "archive.org", "streamable.com", "twitch.tv", "soundcloud.com",
    "bandcamp.com", "reddit.com", "9gag.com", "bilibili.com", "ted.com",
)

_SKIP_HOSTS = ("wikipedia.org", "duckduckgo.com", "bing.com", "google.com")

STEALTH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="131", "Google Chrome";v="131", "Not-A.Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _norm_tokens(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


# Pre-compiled word-boundary matchers. Plain substring tests made short terms
# like "amv" fire inside unrelated words and rejected legitimate results.
_JUNK_RES = [(term, re.compile(r"\b" + re.escape(term) + r"\b")) for term in _JUNK_TERMS]


def _junk_hits(title_lower, query_lower):
    """Junk terms present in the title that the user did not ask for."""
    return [t for t, rx in _JUNK_RES if rx.search(title_lower) and t not in query_lower]


def _clean_platform_name(domain_or_source):
    """Format platform domain or slug into clean, human-readable name for UI badges."""
    if not domain_or_source:
        return "Web"
    s = str(domain_or_source).lower()
    if "youtube" in s or "youtu.be" in s:
        return "YouTube"
    if "dailymotion" in s or "dmcdn" in s:
        return "Dailymotion"
    if "imdb" in s:
        return "IMDb"
    if "netflix" in s:
        return "Netflix"
    if "prime" in s or "amazon" in s:
        return "Prime Video"
    if "archive.org" in s or "archive" in s:
        return "Archive.org"
    if "vimeo" in s:
        return "Vimeo"
    if "facebook" in s or "fb.watch" in s:
        return "Facebook"
    if "instagram" in s:
        return "Instagram"
    if "tiktok" in s:
        return "TikTok"
    if "twitter" in s or "x.com" in s:
        return "X / Twitter"
    if "rumble" in s:
        return "Rumble"
    if "bilibili" in s:
        return "Bilibili"
    if "reddit" in s:
        return "Reddit"
    if "soundcloud" in s:
        return "SoundCloud"
    if "twitch" in s:
        return "Twitch"
    if "ok.ru" in s:
        return "OK.ru"
    
    parts = s.replace("www.", "").split(".")
    if len(parts) >= 2:
        return parts[0].capitalize()
    return s.capitalize()


def _detect_intent(query):
    q = query.lower()
    if any(w in q for w in ("full movie", "full film", "full hd movie", "pelicula completa", "complete movie", "entire movie")):
        return "full_movie"
    if any(w in q for w in ("full video", "full vedio", "full length", "complete video", "entire video", "full show", "full episode", "full lecture", "full tutorial")):
        return "full_video"
    if any(w in q for w in ("tutorial", "how to", "guide", "lecture", "lesson", "course", "review", "reaction", "interview", "podcast", "photography", "camera", "editing", "sound design")):
        return "general"
    if re.search(r'\b(?:movie|film|cinema)\b', q):
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
    q = (query or "").lower().strip()
    qtokens = _norm_tokens(query)
    ttokens = _norm_tokens(title)
    dur = entry.get("duration") or 0
    if isinstance(dur, str):
        dur = _parse_time_str(dur) or 0
    elif not isinstance(dur, (int, float)):
        dur = 0

    # -----------------------------------------------------------------------
    # CRITICAL ZERO-TOLERANCE FILTER: FULL VIDEO & MOVIE QUERIES
    # When user asks for a movie or full video, NEVER return review, overview,
    # recap, breakdown, trailer, teaser, or reaction!
    # -----------------------------------------------------------------------
    is_full_intent = intent in ("full_movie", "movie", "full_video")
    junk_hits = _junk_hits(tl, q)
    if is_full_intent:
        # Commentary / review / trailer terms disqualify outright.
        if junk_hits:
            return -9999.0

        if ("trailer" in tl or "teaser" in tl) and "trailer" not in q and "teaser" not in q:
            return -9999.0  # Immediate hard disqualification

        # Strict duration gates
        if intent in ("full_movie", "movie"):
            # A full movie is at least 40 minutes (2400 seconds)
            if 0 < dur < 2400:
                return -9999.0  # Reject trailers, short clips, 10-minute reviews
        elif intent == "full_video":
            # A full video when specified "full" is at least 10 minutes (600 seconds)
            if 0 < dur < 600:
                return -9999.0

    score = 0.0

    # Relevance: query overlap
    if qtokens:
        overlap = len(qtokens & ttokens) / len(qtokens)
        score += overlap * 45
        if q and q in tl:
            score += 18

    # General junk penalty if not already disqualified
    score -= 35 * len(junk_hits)

    # Official / trusted source boosts
    STUDIO_KEYWORDS = ["yrf", "yash raj", "shemaroo", "ultra", "goldmines", "sony", "zee", "eros", "tips", "warner", "paramount", "universal", "lionsgate", "disney"]
    if any(sk in uploader for sk in STUDIO_KEYWORDS):
        score += 45
    if uploader.endswith("- topic"):
        score += 28
    if "vevo" in uploader:
        score += 24
    if any(o in tl for o in _OFFICIAL_TERMS):
        score += 16
    elif "official" in tl and "official" not in q:
        score += 6
    if entry.get("channel_is_verified") or entry.get("uploader_verified") or entry.get("verified"):
        score += 12
        
    # Spam / piracy clickbait re-upload channel penalty
    SPAM_CHANNEL_WORDS = ["wala", "hub", "clips", "status", "creation", "junction", "zone", "club", "fun", "port", "volly", "online free", "movies time", "nightcipher"]
    if any(sw in uploader for sw in SPAM_CHANNEL_WORDS):
        score -= 75

    # Popularity
    vc = entry.get("view_count") or 0
    if vc > 0:
        score += min(22.0, math.log10(vc + 1) * 4.2)

    # Duration shaping
    if intent == "music":
        if 60 <= dur <= 600:
            score += 14
        elif dur > 1800:
            score -= 16
        elif 0 < dur < 45:
            score -= 12
    elif intent in ("full_movie", "movie"):
        if dur >= 4500:       # 1 hour 15m+ (feature film)
            score += 50
        elif dur >= 3000:     # 50m+
            score += 35
        elif dur >= 2400:     # 40m+
            score += 20
        elif 0 < dur < 2400:
            score -= 100
    elif intent == "full_video":
        if dur >= 1800:       # 30m+
            score += 35
        elif dur >= 900:      # 15m+
            score += 20
    elif intent == "trailer":
        if "trailer" in tl or "teaser" in tl:
            score += 20
        if 30 <= dur <= 240:
            score += 10
    else:
        if 0 < dur < 30:
            score -= 10
        if "trailer" in tl and "trailer" not in q and 0 < dur < 200:
            score -= 10

    return score


def _entry_badge(entry):
    uploader = (entry.get("uploader") or entry.get("channel") or "").lower()
    tl = (entry.get("title") or "").lower()
    if uploader.endswith("- topic") or "vevo" in uploader:
        return "Official Audio"
    if any(o in tl for o in _OFFICIAL_TERMS):
        return "Official"
    if entry.get("channel_is_verified") or entry.get("uploader_verified") or entry.get("verified"):
        return "Verified"
    return None


def _parse_time_str(time_str):
    """Convert '3:45' or '1:12:30' into total seconds."""
    if not time_str:
        return None
    try:
        parts = [float(p) for p in str(time_str).strip().split(":")]
        if len(parts) == 1:
            return int(parts[0]) if parts[0].is_integer() else parts[0]
        elif len(parts) == 2:
            val = parts[0] * 60 + parts[1]
            return int(val) if isinstance(val, float) and val.is_integer() else val
        elif len(parts) == 3:
            val = parts[0] * 3600 + parts[1] * 60 + parts[2]
            return int(val) if isinstance(val, float) and val.is_integer() else val
    except Exception:
        pass
    return None


def _parse_views_str(v_str):
    """Convert '1.2M views' or '45,210 views' into integer."""
    if not v_str:
        return None
    try:
        s = str(v_str).lower().replace("views", "").replace("view", "").strip()
        s = s.replace(",", "")
        if "m" in s:
            return int(float(s.replace("m", "")) * 1_000_000)
        if "k" in s:
            return int(float(s.replace("k", "")) * 1_000)
        if "b" in s:
            return int(float(s.replace("b", "")) * 1_000_000_000)
        return int(float(s))
    except Exception:
        return None


def _stealth_youtube_search(query, count=8):
    """
    Direct stealth YouTube search using curl_cffi with Chrome TLS impersonation.
    Bypasses anti-bot blocks, IP throttling, and JS challenges that cause yt-dlp to fail.
    """
    if not CURL_CFFI_AVAILABLE:
        return []

    encoded = urllib.parse.quote_plus(query)
    search_url = f"https://www.youtube.com/results?search_query={encoded}"

    try:
        resp = cffi_requests.get(
            search_url,
            headers=STEALTH_HEADERS,
            impersonate="chrome124",
            timeout=12
        )
        if resp.status_code != 200:
            return []

        # Find embedded ytInitialData JSON
        m = re.search(r'var ytInitialData\s*=\s*({.+?});</script>', resp.text)
        if not m:
            m = re.search(r'window\["ytInitialData"\]\s*=\s*({.+?});</script>', resp.text)
        if not m:
            return []

        data = json.loads(m.group(1))

        # Traverse YouTube search structure
        contents = (
            data.get("contents", {})
            .get("twoColumnSearchResultsRenderer", {})
            .get("primaryContents", {})
            .get("sectionListRenderer", {})
            .get("contents", [])
        )

        entries = []
        for section in contents:
            item_section = section.get("itemSectionRenderer", {})
            for item in item_section.get("contents", []):
                vr = item.get("videoRenderer")
                if not vr:
                    continue

                vid = vr.get("videoId")
                if not vid:
                    continue

                title_obj = vr.get("title", {})
                title = ""
                if title_obj.get("runs"):
                    title = "".join(r.get("text", "") for r in title_obj["runs"])
                elif title_obj.get("simpleText"):
                    title = title_obj["simpleText"]

                if not title:
                    continue

                # Channel / uploader
                owner = vr.get("ownerText", {}) or vr.get("longBylineText", {})
                channel = ""
                if owner.get("runs"):
                    channel = "".join(r.get("text", "") for r in owner["runs"])

                # Verified badge
                badges = vr.get("ownerBadges", [])
                verified = any("VERIFIED" in str(b) for b in badges)

                # Duration
                length_obj = vr.get("lengthText", {})
                dur_str = length_obj.get("simpleText", "")
                dur = _parse_time_str(dur_str)

                # View count
                vc_obj = vr.get("viewCountText", {})
                vc_str = vc_obj.get("simpleText", "")
                if not vc_str and vc_obj.get("runs"):
                    vc_str = "".join(r.get("text", "") for r in vc_obj["runs"])
                views = _parse_views_str(vc_str)

                # Thumbnail
                thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                thumbs = vr.get("thumbnail", {}).get("thumbnails", [])
                if thumbs:
                    thumb = thumbs[-1].get("url") or thumb

                entries.append({
                    "id": vid,
                    "title": html.unescape(title),
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "duration": dur,
                    "uploader": channel,
                    "channel": channel,
                    "thumbnail": thumb,
                    "view_count": views,
                    "channel_is_verified": verified,
                    "source": "youtube",
                })

        return entries
    except Exception:
        return []


def search_videos(query, count=8, provider="youtube", use_stealth=True):
    """
    Search YouTube for media by name and return RANKED candidates.
    Equipped with anti-bot stealth evasions, platform query cleaning, and TLS impersonation fallback.
    """
    intent = _detect_intent(query)
    clean_q = _clean_platform_search_query(query)
    search_query = clean_q or query
    pool = max(int(count) * 3, 18)
    pool = min(pool, 40)
    search_term = f"ytsearch{pool}:{search_query}"

    entries = []
    yt_dlp_error = None

    # Step 1: Fast yt-dlp flat extraction with stealth headers
    if yt_dlp is not None:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "default_search": "ytsearch",
        }
        if use_stealth:
            ydl_opts["http_headers"] = STEALTH_HEADERS

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(search_term, download=False)
            entries = (info or {}).get("entries", []) or []
        except Exception as e:
            yt_dlp_error = str(e)

    # Step 2: Stealth TLS Impersonation Fallback if yt-dlp fails or is blocked
    if not entries and use_stealth:
        stealth_entries = _stealth_youtube_search(search_query, count=pool)
        if stealth_entries:
            entries = stealth_entries

    if not entries and yt_dlp_error:
        raise RuntimeError(f"Search failed: {yt_dlp_error}")

    # Step 3: Score, filter junk, rank candidates
    scored = []
    for entry in entries:
        if not entry:
            continue
        vid = entry.get("id")
        url = entry.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else None)
        if not url or not entry.get("title"):
            continue

        # Drop unavailable or live-only streams
        if entry.get("live_status") in ("is_live", "is_upcoming"):
            continue
        if entry.get("availability") in ("private", "premium_only", "subscriber_only", "needs_auth"):
            continue
        # Drop YouTube Shorts (vertical ≤60s clips) - they are never full-length media
        entry_url = entry.get("url") or ""
        if "/shorts/" in entry_url:
            continue

        thumb = entry.get("thumbnail")
        thumbs = entry.get("thumbnails") or []
        if not thumb and thumbs:
            thumb = thumbs[-1].get("url")
        if not thumb and vid:
            thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

        dur = entry.get("duration") or 0
        score = _score_entry(entry, search_query, intent)
        # If disqualified by the strict full video / movie filter, omit completely
        if score <= -9000:
            continue

        badge = _entry_badge(entry)
        if dur >= 2400 and intent in ("movie", "full_movie"):
            badge = "Full Movie"
        elif dur >= 1200 and intent == "full_video":
            badge = "Full Video"

        scored.append((score, {
            "title": entry.get("title") or "Untitled",
            "url": url,
            "id": vid,
            "duration": dur,
            "uploader": entry.get("uploader") or entry.get("channel"),
            "channel": entry.get("channel") or entry.get("uploader"),
            "thumbnail": thumb,
            "view_count": entry.get("view_count"),
            "is_official": bool(badge),
            "verified": bool(entry.get("channel_is_verified")),
            "badge": badge,
            "score": round(score, 1),
            "source": "youtube",
            "platform": "YouTube",
            "stealth_protected": use_stealth,
        }))

    scored.sort(key=lambda x: x[0], reverse=True)

    # The yt-dlp pass and the stealth HTML pass can surface the same video, and
    # YouTube itself repeats items across sections.
    unique, seen_ids = [], set()
    for _s, item in scored:
        key = item.get("id") or item.get("url")
        if key in seen_ids:
            continue
        seen_ids.add(key)
        unique.append(item)
        if len(unique) >= int(count):
            break
    return unique


def search_dailymotion(query, count=6, use_stealth=True):
    """
    Search Dailymotion for full media, movies, and alternative video streams.
    """
    try:
        clean_q = _clean_platform_search_query(query) or query
        clean_q = re.sub(r'\s*\(\d{4}\)', '', clean_q).strip()
        encoded = urllib.parse.quote_plus(clean_q)
        fetch_limit = max(int(count) * 4, 25)
        api_url = f"https://api.dailymotion.com/videos?search={encoded}&fields=id,title,url,duration,views_total,owner.screenname,thumbnail_360_url&limit={fetch_limit}"
        
        headers = STEALTH_HEADERS if use_stealth else {"User-Agent": "Mozilla/5.0"}
        if CURL_CFFI_AVAILABLE and use_stealth:
            resp = cffi_requests.get(api_url, headers=headers, impersonate="chrome124", timeout=8)
        else:
            import requests
            resp = requests.get(api_url, headers=headers, timeout=8)
            
        if resp.status_code != 200:
            return []
            
        data = resp.json()
        raw_items = data.get("list", [])
        intent = _detect_intent(query)
        
        scored = []
        for d in raw_items:
            vid = d.get("id")
            title = d.get("title")
            dur = d.get("duration") or 0
            if not vid or not title:
                continue
            entry = {
                "id": vid,
                "title": html.unescape(title),
                "url": f"https://www.dailymotion.com/video/{vid}",
                "duration": dur,
                "uploader": d.get("owner.screenname") or "Dailymotion Creator",
                "channel": d.get("owner.screenname") or "Dailymotion Creator",
                "thumbnail": d.get("thumbnail_360_url"),
                "view_count": d.get("views_total"),
                "source": "dailymotion",
                "platform": "Dailymotion",
            }
            score = _score_entry(entry, clean_q, intent)
            if score <= -9000:
                continue
            
            badge = "Full Movie" if dur >= 2400 else ("Full Video" if dur >= 1200 else None)
            scored.append((score, {
                **entry,
                "score": round(score, 1),
                "badge": badge,
                "stealth_protected": use_stealth,
            }))
            
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _s, item in scored[:int(count)]]
    except Exception:
        return []


def search_archive_org(query, count=4):
    """
    Search Internet Archive for public domain full feature movies and longform streams.
    """
    try:
        clean_q = _clean_platform_search_query(query) or query
        encoded = urllib.parse.quote_plus(clean_q)
        api_url = f"https://archive.org/advancedsearch.php?q={encoded}+AND+mediatype:movies&fl[]=identifier,title,description,downloads,item_size&rows={count * 2}&output=json"
        
        if CURL_CFFI_AVAILABLE:
            resp = cffi_requests.get(api_url, headers=STEALTH_HEADERS, impersonate="chrome124", timeout=8)
        else:
            import requests
            resp = requests.get(api_url, timeout=8)
            
        if resp.status_code != 200:
            return []
            
        data = resp.json()
        docs = data.get("response", {}).get("docs", [])
        intent = _detect_intent(query)
        
        results = []
        for doc in docs:
            ident = doc.get("identifier")
            title = doc.get("title")
            if not ident or not title:
                continue
            tl = title.lower()
            if any(j in tl for j in ("review", "trailer", "teaser", "reaction", "recap")):
                continue
            results.append({
                "id": ident,
                "title": html.unescape(title),
                "url": f"https://archive.org/details/{ident}",
                # Report the truth: the advanced-search API gives no runtime.
                # A hard-coded 5400s used to show a fake "1:30:00" in the UI and
                # fed a fabricated duration into the movie authenticity scorer.
                "duration": None,
                "longform_source": True,
                "uploader": "Internet Archive",
                "channel": "Internet Archive",
                "thumbnail": f"https://archive.org/services/img/{ident}",
                "view_count": doc.get("downloads"),
                "source": "archive",
                "platform": "Archive.org",
                "badge": "Archive Movie" if intent in ("movie", "full_movie") else "Archive",
                "score": 45.0,
                "stealth_protected": False,
            })
        return results[:int(count)]
    except Exception:
        return []


def _stealth_web_search(query, count=8):
    """Fallback open web search using curl_cffi on DuckDuckGo HTML."""
    if not CURL_CFFI_AVAILABLE:
        return []
    try:
        url = "https://html.duckduckgo.com/html/"
        resp = cffi_requests.post(
            url,
            data={"q": query, "b": ""},
            headers=STEALTH_HEADERS,
            impersonate="chrome124",
            timeout=10
        )
        if resp.status_code != 200:
            return []

        # DuckDuckGo's HTML endpoint puts the anchor text on its own line, so
        # this pattern only ever matched with DOTALL enabled - without it the
        # entire stealth web fallback silently returned nothing.
        links = re.findall(
            r'<a[^>]+class="result__(?:url|a)"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            resp.text,
            re.S,
        )
        results = []
        seen_urls = set()
        for href, display in links:
            clean_url = html.unescape(href.strip())
            if clean_url.startswith("//"):
                clean_url = "https:" + clean_url
            # Resolve duckduckgo redirect url if needed
            if "uddg=" in clean_url:
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(clean_url).query)
                clean_url = parsed.get("uddg", [clean_url])[0]

            if not clean_url.lower().startswith(("http://", "https://")):
                continue
            if clean_url in seen_urls:
                continue
            seen_urls.add(clean_url)

            title_clean = html.unescape(re.sub(r'<.*?>', '', display)).strip()
            domain = urllib.parse.urlparse(clean_url).netloc.lower()
            if domain.startswith("www."):
                domain = domain[4:]
            if not domain or any(s in domain for s in _SKIP_HOSTS):
                continue

            results.append({
                "title": title_clean or f"Media on {domain}",
                "url": clean_url,
                "id": None,
                "duration": None,
                "uploader": domain,
                "thumbnail": None,
                "view_count": None,
                "source": "web",
                "platform": _clean_platform_name(domain),
                "stealth_protected": True,
            })
        return results[:int(count)]
    except Exception:
        return []


def search_web(query, count=8, use_stealth=True):
    """
    Search the open web for downloadable media with stealth fallback.
    """
    search_query = _clean_platform_search_query(query) or query
    items = []
    ddgs_error = None

    if DDGS is not None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                with DDGS() as ddgs:
                    results = ddgs.text(search_query, max_results=20)
                    for r in results:
                        items.append((r.get('href'), r.get('title')))
        except Exception as e:
            ddgs_error = str(e)

    # If DDGS failed, was blocked, or returned empty, engage Stealth Web Search
    if not items and use_stealth:
        stealth_results = _stealth_web_search(search_query, count=count)
        if stealth_results:
            return stealth_results

    # Surface the real reason rather than discarding it. search_media() stores
    # this as web_error and only shows it when nothing was found anywhere.
    if not items and ddgs_error:
        raise RuntimeError(f"Open-web search failed: {ddgs_error}")

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
            "platform": _clean_platform_name(domain),
            "stealth_protected": use_stealth,
        })

    cleaned.sort(key=lambda x: 0 if any(h in x["uploader"] for h in _VIDEO_HOSTS) else 1)
    return cleaned[:int(count)]


def search_imdb(query, count=3):
    """
    Query IMDb official suggestion API for accurate title, release year,
    primary cast, poster artwork, and IMDb reference.
    """
    try:
        clean_q = _clean_title_for_catalog(query) or query
        clean = re.sub(r'[^\w\s\-]', '', clean_q, flags=re.UNICODE).strip()
        slug = urllib.parse.quote(clean.lower().replace(" ", "_"))
        url = f"https://v3.sg.media-imdb.com/suggestion/x/{slug}.json"
        
        headers = STEALTH_HEADERS.copy() if CURL_CFFI_AVAILABLE else {'User-Agent': 'Mozilla/5.0'}
        data = {}
        if CURL_CFFI_AVAILABLE:
            try:
                resp = cffi_requests.get(url, headers=headers, impersonate="chrome124", timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
            except Exception:
                data = {}

        if not data:
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=5) as r:
                    data = json.loads(r.read().decode('utf-8'))
            except Exception:
                data = {}
                
        items = data.get('d', [])
        results = []
        for it in items:
            title = it.get('l')
            if not title:
                continue
            q_type = it.get('q', '')
            year = it.get('y')
            imdb_id = it.get('id')
            cast = it.get('s')
            img = it.get('i', {}).get('imageUrl') if it.get('i') else None
            
            badge_label = "IMDb " + (q_type.capitalize() if q_type else "Official")
            
            results.append({
                "id": imdb_id or f"imdb_{clean}",
                "imdb_id": imdb_id,
                "title": f"{title} ({year})" if year else title,
                "url": f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else f"https://www.imdb.com/find?q={urllib.parse.quote(query)}",
                "duration": None,
                "uploader": "IMDb Official" + (f" - {cast}" if cast else ""),
                "channel": "IMDb",
                "thumbnail": img,
                "view_count": None,
                "source": "imdb",
                "platform": "IMDb",
                "badge": badge_label,
                "score": 58.0,
                "year": year,
                "cast": cast,
                "type": q_type,
                "is_streaming": True,
                "stealth_protected": False,
            })
            if len(results) >= count:
                break
        return results
    except Exception:
        return []


def search_streaming_platforms(query, imdb_items=None, count=2):
    """
    Generate official streaming media availability cards for Netflix and Prime Video.
    """
    try:
        clean_q = _clean_title_for_catalog(query) or query
        clean_name = re.sub(r'[^a-zA-Z0-9\s]', '', clean_q).strip()
        encoded = urllib.parse.quote_plus(clean_name or query)
        
        top_thumb = None
        top_cast = None
        top_year = None
        top_title = clean_q.title()
        top_imdb_id = None
        if imdb_items and len(imdb_items) > 0:
            top_thumb = imdb_items[0].get("thumbnail")
            top_cast = imdb_items[0].get("cast")
            top_year = imdb_items[0].get("year")
            top_title = imdb_items[0].get("title")
            # "id" falls back to a synthetic "imdb_<slug>" string; only a real
            # tt-prefixed id is usable by the player, so read imdb_id directly.
            top_imdb_id = imdb_items[0].get("imdb_id")

        results = []
        # Netflix Stream Card
        results.append({
            "id": f"netflix_{encoded}",
            "imdb_id": top_imdb_id,
            "title": f"{top_title} (Watch on Netflix)",
            "url": f"https://www.netflix.com/search?q={encoded}",
            "duration": None,
            "uploader": "Netflix Official" + (f" - {top_cast}" if top_cast else ""),
            "channel": "Netflix",
            "thumbnail": top_thumb or "https://assets.nflxext.com/ffe/siteui/common/icons/monogram/icon-512.png",
            "view_count": None,
            "source": "netflix",
            "platform": "Netflix",
            "badge": "Netflix Stream",
            "score": 55.0,
            "year": top_year,
            "is_streaming": True,
            "stealth_protected": False,
        })

        # Prime Video Stream Card
        results.append({
            "id": f"prime_{encoded}",
            "imdb_id": top_imdb_id,
            "title": f"{top_title} (Watch on Prime Video)",
            "url": f"https://www.primevideo.com/search/ref=atv_nb_sr?phrase={encoded}",
            "duration": None,
            "uploader": "Prime Video" + (f" - {top_cast}" if top_cast else ""),
            "channel": "Amazon Prime",
            "thumbnail": top_thumb or "https://m.media-amazon.com/images/G/01/digital/video/web/logo-min-remake.png",
            "view_count": None,
            "source": "prime",
            "platform": "Prime Video",
            "badge": "Prime Video",
            "score": 54.0,
            "year": top_year,
            "is_streaming": True,
            "stealth_protected": False,
        })
        return results[:count]
    except Exception:
        return []


def search_media(query, count=16, include_web=True, use_stealth=True):
    """
    Unified entrypoint: Concurrent multi-source aggregation across ALL sources:
    YouTube, Dailymotion, Internet Archive, IMDb, Netflix, Prime Video, and Open Web.
    Enforces strict 'full movie / full video' filtering and distinct source platform tagging.
    """
    clean_q = _clean_platform_search_query(query)
    search_q = clean_q or query
    intent = _detect_intent(query)
    
    yt_results = []
    dm_results = []
    arch_results = []
    imdb_results = []
    web_results = []
    yt_error = None
    web_error = None

    # Step 1: Run searches across all platforms concurrently.
    #
    # NOTE: this deliberately does NOT use `with ThreadPoolExecutor(...)`.
    # Leaving that context manager calls shutdown(wait=True), which blocks until
    # every future finishes - so a slow provider hung the whole request and made
    # the per-future timeouts below completely ineffective.
    executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="quidian-search")
    try:
        future_yt = executor.submit(search_videos, query, count=8, provider="youtube", use_stealth=use_stealth)
        future_dm = executor.submit(search_dailymotion, query, count=5, use_stealth=use_stealth)
        future_arch = executor.submit(search_archive_org, query, count=4)
        future_imdb = executor.submit(search_imdb, search_q, count=3)
        future_web = executor.submit(search_web, query, count=5, use_stealth=use_stealth) if include_web else None

        deadline = time.monotonic() + 12.0

        def _collect(future, fallback):
            """Read a future within the shared deadline, never blocking past it."""
            if future is None:
                return fallback, None
            remaining = max(0.5, deadline - time.monotonic())
            try:
                value = future.result(timeout=remaining)
                return (value if value is not None else fallback), None
            except FuturesTimeout:
                future.cancel()
                return fallback, "timed out"
            except Exception as exc:
                return fallback, str(exc)

        yt_results, yt_error = _collect(future_yt, [])
        dm_results, _ = _collect(future_dm, [])
        arch_results, _ = _collect(future_arch, [])
        imdb_results, _ = _collect(future_imdb, [])
        web_results, web_error = _collect(future_web, [])
    finally:
        # Abandon any straggler threads instead of waiting on them.
        executor.shutdown(wait=False)

    # Step 2: Generate streaming platform availability cards (Netflix & Prime Video)
    streaming_results = search_streaming_platforms(query, imdb_items=imdb_results, count=2)

    # Step 3: Interleave and blend results from all sources to ensure diverse representation
    combined = []
    seen_urls = set()

    def _add_items(item_list):
        for item in item_list:
            u = item.get("url")
            if u and u not in seen_urls:
                seen_urls.add(u)
                combined.append(item)

    # Balanced interleaving:
    # 1. When querying for a known movie/show (or query matches IMDb title), feature the canonical IMDb card first!
    is_imdb_match = False
    if imdb_results:
        top_imdb_title = re.sub(r'\s*\(\d{4}\)', '', imdb_results[0].get("title", "")).strip().lower()
        clean_q_lower = query.strip().lower()
        if clean_q_lower in top_imdb_title or top_imdb_title in clean_q_lower or "movie" in clean_q_lower or intent in ("movie", "full_movie"):
            is_imdb_match = True

    if is_imdb_match:
        _add_items(imdb_results[:1])
        if dm_results:
            _add_items(dm_results[:1])
        if yt_results:
            _add_items(yt_results[:1])
    else:
        # Standard video / music search interleaving
        if yt_results:
            _add_items(yt_results[:2])
        if dm_results:
            _add_items(dm_results[:1])
        if imdb_results:
            _add_items(imdb_results[:1])

    # 3. Public domain / Internet Archive full film
    if arch_results:
        _add_items(arch_results[:1])

    # 4. Official Netflix & Prime Video stream links
    if streaming_results:
        _add_items(streaming_results)

    # 5. Remaining high-ranked items from all sources
    if yt_results:
        _add_items(yt_results[2:])
    if dm_results:
        _add_items(dm_results[1:])
    if arch_results:
        _add_items(arch_results[1:])
    if imdb_results:
        _add_items(imdb_results[1:])
    if include_web and web_results:
        _add_items(web_results)

    visible = combined[:int(count)] if count else combined

    # Counts are computed over the results actually returned, so the filter
    # chips can never advertise more items than the grid contains.
    platform_counts = {}
    for it in visible:
        p = it.get("platform") or "Web"
        platform_counts[p] = platform_counts.get(p, 0) + 1

    return {
        "results": visible,
        "total_found": len(combined),
        "platform_counts": platform_counts,
        "query": query,
        "cleaned_query": clean_q,
        "intent": intent,
        "yt_error": yt_error,
        "web_error": web_error,
        "stealth_active": use_stealth,
    }


