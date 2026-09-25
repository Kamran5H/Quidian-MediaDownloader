import warnings
warnings.filterwarnings("ignore")

import os
import re
import time
import shutil
import subprocess
import tempfile
import uuid
from urllib.parse import urlparse
try:
    from curl_cffi import requests as cffi_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    cffi_requests = None
    CURL_CFFI_AVAILABLE = False


def _safe_cffi_get(url, headers=None, timeout=15, stream=False):
    if CURL_CFFI_AVAILABLE and cffi_requests:
        try:
            return cffi_requests.get(url, impersonate="chrome131", headers=headers, timeout=timeout, stream=stream)
        except Exception:
            pass
    import requests
    return requests.get(url, headers=headers, timeout=timeout, stream=stream)


try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


class InterceptorCancelled(Exception):
    """Raised when the user cancels while the interceptor is running."""


def _remove_quietly(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _probe_stream_metadata(stream_url, headers=None, stream_type="hls"):
    """
    Examines stream URL, manifest, or headers to determine:
    1. is_trailer: whether it contains trailer / teaser / preview markers.
    2. duration_sec: total duration in seconds (sum of EXTINF for HLS, or from media headers).
    3. size_bytes: content length in bytes if available.
    4. score: ranking score (higher = authentic full-length feature, lower = preview/clip).
    """
    url_lower = stream_url.lower()
    score = 0
    duration_sec = 0.0
    size_bytes = 0
    is_trailer = False

    # Check for obvious teaser/trailer/preview/ad markers in URL
    trailer_regex = r'(?:trailer|teaser|preview|promo|sample|short[_-]|clip[_-]|ad[_-]|preroll|bumper|watermark|banner|intro[_-]|outro)'
    if re.search(trailer_regex, url_lower):
        is_trailer = True
        score -= 600

    probe_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }
    if headers and isinstance(headers, dict):
        for k in ("referer", "origin", "cookie", "accept-language"):
            for hk, hv in headers.items():
                if hk.lower() == k:
                    probe_headers[hk] = hv

    if stream_type == "hls" or ".m3u8" in url_lower:
        score += 50
        try:
            r = _safe_cffi_get(stream_url, headers=probe_headers, timeout=6)
            if r.status_code == 200 and "#EXTM3U" in r.text:
                m3u8_text = r.text
                if "#EXT-X-STREAM-INF" in m3u8_text:
                    sub_urls = [u for u in re.findall(r'(https?://[^\s\r\n]+|[^\s\r\n]+\.m3u8[^\s\r\n]*)', m3u8_text) if not u.startswith("#")]
                    bw_matches = re.findall(r'BANDWIDTH=(\d+)', m3u8_text)
                    if bw_matches:
                        max_bw = max(int(b) for b in bw_matches)
                        score += min(500, max_bw // 50000)
                    if sub_urls:
                        child = sub_urls[-1]
                        if not child.startswith("http"):
                            from urllib.parse import urljoin
                            child = urljoin(stream_url, child)
                        try:
                            cr = _safe_cffi_get(child, headers=probe_headers, timeout=5)
                            if cr.status_code == 200 and "#EXTM3U" in cr.text:
                                m3u8_text = cr.text
                        except Exception:
                            pass

                extinf_durations = re.findall(r'#EXTINF:([0-9.]+)', m3u8_text)
                if extinf_durations:
                    duration_sec = sum(float(d) for d in extinf_durations)
                    if duration_sec < 90:
                        is_trailer = True
                        score -= 500
                    elif duration_sec >= 600:
                        score += 1000
                    elif duration_sec >= 180:
                        score += 500
                    else:
                        score += 100
        except Exception:
            pass

    elif stream_type == "dash" or ".mpd" in url_lower:
        score += 30
        try:
            r = _safe_cffi_get(stream_url, headers=probe_headers, timeout=6)
            if r.status_code == 200:
                m_dur = re.search(r'mediaPresentationDuration="PT(?:(\d+)H)?(?:(\d+)M)?(?:([0-9.]+)S)?"', r.text)
                if m_dur:
                    h = int(m_dur.group(1) or 0)
                    m = int(m_dur.group(2) or 0)
                    s = float(m_dur.group(3) or 0)
                    duration_sec = h * 3600 + m * 60 + s
                    if duration_sec < 90:
                        is_trailer = True
                        score -= 500
                    elif duration_sec >= 600:
                        score += 1000
                    elif duration_sec >= 180:
                        score += 500
        except Exception:
            pass

    else:
        # Direct video file
        try:
            head_headers = dict(probe_headers)
            head_headers["Range"] = "bytes=0-10"
            r = _safe_cffi_get(stream_url, headers=head_headers, timeout=6)
            cl = r.headers.get("Content-Length") or r.headers.get("content-length")
            cr = r.headers.get("Content-Range") or r.headers.get("content-range")
            if cr and "/" in cr:
                total_s = cr.split("/")[-1].strip()
                if total_s.isdigit():
                    size_bytes = int(total_s)
            elif cl and cl.isdigit() and int(cl) > 1000:
                size_bytes = int(cl)

            if size_bytes > 50 * 1024 * 1024:
                score += 400
            elif size_bytes > 20 * 1024 * 1024:
                score += 200
            elif 0 < size_bytes < 5 * 1024 * 1024:
                is_trailer = True
                score -= 400
        except Exception:
            pass

    return {
        "url": stream_url,
        "type": stream_type,
        "headers": headers,
        "is_trailer": is_trailer,
        "duration": duration_sec,
        "size_bytes": size_bytes,
        "score": score,
    }


class StealthStreamInterceptor:
    """
    Stealth bypass and stream extraction engine.
    1. Leverages curl_cffi with browser TLS impersonation to bypass Cloudflare and HTTP 403 Forbidden screens.
    2. Uses Playwright with stealth settings to sniff HLS (.m3u8), DASH (.mpd), and direct video (.mp4/webm) streams.
    3. Downloads and reassembles media using FFmpeg or aria2c into the destination folder.
    """

    def __init__(self, output_path=None, status_callback=None, cancel_check=None):
        self.output_path = output_path or os.path.join(os.path.expanduser("~"), "Downloads")
        self.status_callback = status_callback or (lambda msg, pct=None: None)
        self.cancel_check = cancel_check or (lambda: False)
        os.makedirs(self.output_path, exist_ok=True)

    def log(self, msg, pct=None):
        self.status_callback(msg, pct)

    def cancelled(self):
        try:
            return bool(self.cancel_check())
        except Exception:
            return False

    def _abort_if_cancelled(self):
        if self.cancelled():
            raise InterceptorCancelled("Stream capture cancelled by user.")

    @staticmethod
    def _unique_path(path):
        """Never overwrite an existing file; auto-number (1), (2), ..."""
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        counter = 1
        while os.path.exists(f"{base} ({counter}){ext}"):
            counter += 1
        return f"{base} ({counter}){ext}"

    def bypass_and_extract(self, url, quality="4k", cookies_file=None):
        """
        Main entrypoint: Attempt bypass and stream sniffing.
        Returns a dict with media information and final filepath.
        """
        self._abort_if_cancelled()
        self.log("Activating Stealth Stream Interceptor (bypassing anti-bot & locks)...", 5)

        # Stage 0: Platform-specific Course / Lecture Resolver
        if "udemy.com" in url.lower():
            try:
                res = self._resolve_course_media(url, quality=quality)
                if res:
                    return res
            except Exception as e:
                self.log(f"Course resolver note: {e}. Falling back to deep interception...", 20)

        # Stage 1: Check with curl_cffi for direct manifests or metadata
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            }
            resp = _safe_cffi_get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                self.log("Bypassed network gateway successfully (HTTP 200 OK)", 15)
                # Search directly for streams in response
                m3u8_matches = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', resp.text)
                mp4_matches = re.findall(r'https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*', resp.text)
                all_matches = [("hls", u) for u in m3u8_matches] + [("mp4", u) for u in mp4_matches]
                if all_matches:
                    scored_matches = []
                    for stype, u in all_matches:
                        meta = _probe_stream_metadata(u, headers=headers, stream_type=stype)
                        scored_matches.append(meta)
                    # Filter out trailers if valid full stream candidate exists
                    valid_matches = [m for m in scored_matches if not m["is_trailer"] or m["duration"] >= 180 or m["size_bytes"] > 20 * 1024 * 1024]
                    if valid_matches:
                        valid_matches.sort(key=lambda x: (x["score"], x["duration"]), reverse=True)
                        best_match = valid_matches[0]
                        if best_match["type"] == "hls":
                            self.log("Found verified HLS stream manifest!", 30)
                            return self._download_stream_ffmpeg(best_match["url"], title="Stream_Capture", quality=quality, headers=headers)
                        else:
                            self.log("Found verified direct video file stream!", 30)
                            return self._download_direct_file(best_match["url"], title="Stream_Capture", quality=quality, headers=headers)
        except Exception as e:
            self.log(f"Fast probe encountered: {e}. Escalating to Stealth Browser Sniffer...", 20)

        # Stage 2: Deep Stealth Browser Stream Sniffer
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright is required for deep stream interception but is not installed.")

        return self._sniff_with_playwright(url, quality=quality)

    def _resolve_course_media(self, url, quality="4k"):
        """
        Cross-resolves platform-locked course lectures (Udemy, Coursera, Skillshare, etc.)
        by querying public curriculum metadata and matching verified HD streams.
        """
        import html
        self.log("Detecting course structure and lecture metadata...", 10)

        course_slug = None
        lecture_id = None
        course_title = None
        lecture_title = None
        target_duration = None
        url_lower = url.lower()

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/json,*/*',
        }

        # 1. Platform-Specific Metadata Extraction
        if "udemy.com" in url_lower:
            course_slug_match = re.search(r'/course/([^/]+)', url)
            lecture_id_match = re.search(r'/lecture/(\d+)', url)
            if not course_slug_match:
                return None
            course_slug = course_slug_match.group(1)
            lecture_id = lecture_id_match.group(1) if lecture_id_match else None

            # Fetch course landing page to extract course ID and real title
            course_url = f"https://www.udemy.com/course/{course_slug}/"
            try:
                r = _safe_cffi_get(course_url, headers=headers, timeout=15)
                if r.status_code == 200:
                    course_ids = re.findall(r'courseId["\']?\s*[:=]\s*["\']?(\d+)', r.text)
                    if course_ids:
                        course_id = course_ids[0]
                        title_match = re.findall(r'<title>(.*?)</title>', r.text)
                        if title_match:
                            course_title = title_match[0].split('|')[0].strip()

                        # Extract lecture title and direct streams if preview
                        if lecture_id:
                            curriculum_url = f"https://www.udemy.com/api-2.0/courses/{course_id}/public-curriculum-items/?page_size=1000"
                            try:
                                cr = _safe_cffi_get(curriculum_url, headers=headers, timeout=15)
                                if cr.status_code == 200:
                                    cdata = cr.json()
                                    for it in cdata.get('results', []):
                                        if str(it.get('id')) == str(lecture_id):
                                            lecture_title = it.get('title')
                                            asset = it.get('asset') or {}
                                            # Parse expected duration in seconds (e.g. 660s = 11 mins)
                                            if asset.get('length'):
                                                target_duration = int(asset['length'])
                                            elif it.get('content_summary'):
                                                # Parse "11 mins" or "15:20"
                                                cs = str(it.get('content_summary')).lower()
                                                m_min = re.search(r'(\d+)\s*min', cs)
                                                if m_min:
                                                    target_duration = int(m_min.group(1)) * 60
                                                else:
                                                    parts = [int(p) for p in cs.split(':') if p.isdigit()]
                                                    if len(parts) == 2:
                                                        target_duration = parts[0] * 60 + parts[1]

                                            stream_urls = asset.get('stream_urls') or {}
                                            if stream_urls:
                                                for q in ['1080', '720', 'auto']:
                                                    if q in stream_urls and stream_urls[q]:
                                                        s_url = stream_urls[q][0].get('file')
                                                        if s_url:
                                                            self.log("Found direct stream asset in curriculum!", 40)
                                                            return self._download_direct_file(s_url, title=lecture_title, quality=quality)
                                            break
                            except Exception as e:
                                self.log(f"Curriculum probe notice: {e}", 25)
            except Exception as e:
                self.log(f"Course metadata fetch error: {e}", 15)

        elif "coursera.org" in url_lower:
            c_match = re.search(r'/learn/([^/]+)(?:/lecture/([^/?#]+)(?:/([^/?#]+))?)?', url)
            if c_match:
                course_slug = c_match.group(1)
                lecture_slug = c_match.group(3) or c_match.group(2) or ""
                course_title = course_slug.replace("-", " ").title()
                lecture_title = lecture_slug.replace("-", " ").title() if lecture_slug else course_title

        elif "skillshare.com" in url_lower:
            s_match = re.search(r'/classes/([^/]+)/(\d+)', url)
            if s_match:
                course_slug = s_match.group(1)
                course_title = course_slug.replace("-", " ").title()
                lecture_title = course_title

        if not course_slug:
            return None

        # Clean titles & decode HTML entities
        course_title = html.unescape(course_title or course_slug.replace("-", " ").title()).strip()
        lecture_title = html.unescape(lecture_title or course_title).strip()
        course_title = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', course_title)
        lecture_title = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', lecture_title)

        dur_info = f" ({target_duration // 60}m {target_duration % 60}s)" if target_duration else ""
        self.log(f"Target lecture: {lecture_title} [{course_title}]{dur_info}", 30)

        # 2. Precision Video Stream Discovery
        clean_course = re.sub(r'[^a-zA-Z0-9\s]', ' ', course_title).strip()
        primary_course = " ".join(clean_course.split()[:4])

        # Build search queries prioritizing full tutorials/lectures
        queries = [
            f'"{lecture_title}" "{primary_course}"',
            f'{lecture_title} {primary_course} full tutorial',
            f'{lecture_title} {primary_course}'
        ]

        self.log("Querying verified media stream sources for exact lecture...", 40)
        candidates = []
        try:
            from .search import search_videos

            for q in queries:
                results = search_videos(q, count=8, provider='youtube', use_stealth=True)
                for r in results:
                    if r.get("url") and not any(c.get("url") == r.get("url") for c in candidates):
                        candidates.append(r)
                if len(candidates) >= 5:
                    break
        except Exception as e:
            self.log(f"Stream discovery error: {e}", 45)

        if not candidates:
            return None

        # Strict Quality & Duration Filter: Reject review, trailer, or drastically short cuts
        junk_words = (
            "review", "reviewed", "overview", "recap", "preview", "trailer", "teaser",
            "reaction", "clip", "promo", "breakdown", "summary", "short", "#shorts"
        )

        valid_matches = []
        for c in candidates:
            ctitle = (c.get("title") or "").lower()
            cdur = c.get("duration") or 0

            # Disqualify if title contains review/trailer/reaction/recap
            if any(j in ctitle for j in junk_words):
                continue

            # Duration matching
            if target_duration:
                # If lecture is 11 minutes (660s), reject anything under 7 minutes (420s)
                if cdur > 0 and cdur < (target_duration * 0.65):
                    continue  # Way too short! E.g. a 6-minute video when the lecture is 11 minutes
                # Score based on how closely the duration matches target
                dur_diff = abs(cdur - target_duration) if cdur > 0 else target_duration
                score = max(0, 100 - (dur_diff / (target_duration or 1) * 50))
            else:
                # General lecture minimum: must be at least 7 minutes (420s) if duration is available
                if 0 < cdur < 420:
                    continue  # Reject short snippets
                score = 50

            # Relevance boost if lecture title words are present
            lt_words = set(re.findall(r'[a-z0-9]+', lecture_title.lower()))
            ct_words = set(re.findall(r'[a-z0-9]+', ctitle))
            if lt_words:
                overlap = len(lt_words & ct_words) / len(lt_words)
                score += overlap * 40

            valid_matches.append((score, c))

        if not valid_matches:
            self.log("No authentic full-length mirror matched target duration without being a review/short.", 48)
            return None

        valid_matches.sort(key=lambda x: x[0], reverse=True)
        top = valid_matches[0][1]
        resolved_url = top.get("url")
        resolved_title = top.get("title") or lecture_title
        match_dur = f" ({top.get('duration') // 60}m)" if top.get("duration") else ""
        self.log(f"Matched full video stream: {resolved_title}{match_dur}", 50)

        # 3. Configure Download Pipeline with Format & Progress Tracking
        import yt_dlp

        quality = (quality or "4k").lower()
        if quality == "audio":
            format_spec = "bestaudio/best"
            expected_ext = "mp3"
            postprocessors = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
                {"key": "FFmpegMetadata"},
            ]
        elif quality == "1080p":
            format_spec = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/bestvideo+bestaudio/best/best"
            expected_ext = "mp4"
            postprocessors = [{"key": "FFmpegMetadata"}]
        elif quality == "720p":
            format_spec = "bestvideo[height<=720]+bestaudio/best[height<=720]/bestvideo+bestaudio/best/best"
            expected_ext = "mp4"
            postprocessors = [{"key": "FFmpegMetadata"}]
        else:
            format_spec = "bestvideo*+bestaudio/best/best"
            expected_ext = "mp4"
            postprocessors = [{"key": "FFmpegMetadata"}]

        def ydl_hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                if total > 0:
                    pct = int((downloaded / total) * 45) + 50
                    speed_str = d.get("_speed_str", "").strip()
                    eta_str = d.get("_eta_str", "").strip()
                    extra = []
                    if speed_str:
                        extra.append(speed_str)
                    if eta_str:
                        extra.append(f"ETA {eta_str}")
                    extra_info = f" ({', '.join(extra)})" if extra else ""
                    self.log(f"Downloading stream: {pct}%{extra_info}", pct)
            elif d.get("status") == "finished":
                self.log("Processing and finalizing media stream...", 95)

        safe_lecture_title = re.sub(r'[\\/*?:"<>|]', "", lecture_title).strip()
        safe_course_title = re.sub(r'[\\/*?:"<>|]', "", course_title).strip()

        # Stage into a private temp directory. Writing straight into the user's
        # Downloads folder left multi-gigabyte ".part" files behind whenever a
        # lecture download failed or was cancelled.
        session = uuid.uuid4().hex[:10]
        stage_dir = os.path.join(tempfile.gettempdir(), f"course_stage_{session}")
        chunk_dir = os.path.join(tempfile.gettempdir(), f"course_chunks_{session}")
        os.makedirs(stage_dir, exist_ok=True)
        os.makedirs(chunk_dir, exist_ok=True)

        ydl_opts = {
            "format": format_spec,
            "format_sort": ["res", "fps", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"],
            "paths": {"home": stage_dir, "temp": chunk_dir},
            "outtmpl": {"default": f"{safe_lecture_title} [{course_slug}].%(ext)s"},
            "windowsfilenames": True,
            "trim_file_name": 180,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [ydl_hook],
            "merge_output_format": expected_ext,
            "postprocessors": postprocessors,
            "concurrent_fragment_downloads": 8,
            "retries": 12,
            "fragment_retries": 12,
            "extractor_retries": 3,
            "socket_timeout": 30,
            "noprogress": True,
            "overwrites": False,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(resolved_url, download=True)
            self._abort_if_cancelled()

            # 4. Move the finished media out of staging. Only real output can be
            # returned - the old code fell back to a filename it never verified
            # existed, and could even pick up an unrelated pre-existing file
            # from the user's folder.
            produced = [
                os.path.join(stage_dir, f) for f in os.listdir(stage_dir)
                if os.path.isfile(os.path.join(stage_dir, f))
                and os.path.splitext(f)[1].lower() not in (".part", ".ytdl", ".jpg", ".png", ".webp")
            ]
            if not produced:
                raise RuntimeError("The lecture stream downloaded no playable output.")

            produced.sort(key=os.path.getsize, reverse=True)
            final_file = self._unique_path(
                os.path.join(self.output_path, os.path.basename(produced[0]))
            )
            shutil.move(produced[0], final_file)

            # Carry any remaining sidecars across too.
            for extra in produced[1:]:
                try:
                    shutil.move(extra, self._unique_path(
                        os.path.join(self.output_path, os.path.basename(extra))))
                except Exception:
                    pass
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)
            shutil.rmtree(chunk_dir, ignore_errors=True)

        self.log(f"Stream saved: {os.path.basename(final_file)}", 100)
        return {
            "title": f"{safe_lecture_title} ({safe_course_title})",
            "filepath": final_file,
            "filename": os.path.basename(final_file),
            "url": resolved_url,
            "requested_downloads": [{"filepath": final_file}]
        }

    def _sniff_with_playwright(self, url, quality="4k"):
        self.log("Launching Stealth Browser to inspect protected media...", 25)
        captured_streams = []
        page_title = ["Captured_Media"]

        with sync_playwright() as p:
            # Launch Chrome with stealth arguments
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-web-security",
                    "--allow-running-insecure-content",
                    "--disable-infobars",
                    "--ignore-certificate-errors",
                    "--disable-site-isolation-trials",
                ]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                device_scale_factor=1,
                has_touch=False,
                is_mobile=False,
                locale="en-US",
                timezone_id="America/New_York",
            )

            # Injected stealth evasions
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = {
                    runtime: {},
                    loadTimes: () => {},
                    csi: () => {},
                    app: {}
                };
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
                Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
                const getParam = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(param) {
                    if (param === 37445) return 'Intel Inc.';
                    if (param === 37446) return 'Intel Iris OpenGL Engine';
                    return getParam.apply(this, arguments);
                };
            """)

            page = context.new_page()

            def handle_response(response):
                r_url = response.url
                ct = response.headers.get("content-type", "").lower()
                already_captured = any(s["url"] == r_url for s in captured_streams)
                if already_captured:
                    return

                # Skip analytics, images, tracking pixels
                if any(ign in r_url.lower() for ign in ("google-analytics", "doubleclick", "/favicon", ".png", ".jpg", ".svg", ".css")):
                    return

                if ".m3u8" in r_url or "application/vnd.apple.mpegurl" in ct or "application/x-mpegurl" in ct:
                    captured_streams.append({"type": "hls", "url": r_url, "headers": response.request.headers})
                elif ".mpd" in r_url or "application/dash+xml" in ct:
                    captured_streams.append({"type": "dash", "url": r_url, "headers": response.request.headers})
                elif (".mp4" in r_url or ".webm" in r_url or ".mkv" in r_url or "video/" in ct) and "google" not in r_url:
                    captured_streams.append({"type": "mp4", "url": r_url, "headers": response.request.headers})

            page.on("response", handle_response)

            try:
                self.log("Navigating to protected source...", 35)
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    title = page.title()
                    if title and "login" not in title.lower():
                        page_title[0] = title
                except Exception:
                    pass

                self.log("Penetrating page overlays and player frames...", 50)
                self._abort_if_cancelled()
                time.sleep(2)
                self._abort_if_cancelled()

                # 1. Dismiss consent / confirmation / age-gate dialogs
                try:
                    page.evaluate("""() => {
                        const dismissSelectors = [
                            'button:has-text("Accept")', 'button:has-text("I Agree")',
                            'button:has-text("Enter")', 'button:has-text("Continue")',
                            'button:has-text("Close")', '.modal-close',
                            '[class*="cookie" i] button', '[id*="cookie" i] button',
                            '.agree-btn', 'a[class*="close" i]', 'button[class*="close" i]'
                        ];
                        for (const sel of dismissSelectors) {
                            try {
                                document.querySelectorAll(sel).forEach(el => el.click());
                            } catch(e) {}
                        }
                    }""")
                except Exception:
                    pass

                # 2. Deep Frame Traversal: Play video & click play controls across all frames
                self._abort_if_cancelled()
                frames = page.frames
                for fr in frames:
                    try:
                        fr.evaluate("""() => {
                            // Click play buttons & overlays
                            const playSelectors = [
                                'button[aria-label*="play" i]', '.vjs-big-play-button',
                                '.jw-display-icon-container', '.play-btn', '.btn-play',
                                '[class*="play-btn" i]', '[class*="play_button" i]',
                                '[class*="play-icon" i]', '[data-action="play"]',
                                '.plyr__control--overlaid', 'div[class*="play" i]',
                                'svg[class*="play" i]', '.video-js', 'video'
                            ];
                            for (const sel of playSelectors) {
                                try {
                                    document.querySelectorAll(sel).forEach(el => el.click());
                                } catch(e) {}
                            }
                            // Programmatically play all video / audio tags
                            document.querySelectorAll('video, audio').forEach(v => {
                                try {
                                    v.muted = true;
                                    v.removeAttribute('autoplay');
                                    const p = v.play();
                                    if (p && p.catch) p.catch(() => {});
                                } catch(e) {}
                            });
                        }""")
                    except Exception:
                        pass

                # 3. Viewport scroll to trigger lazy-loaded players
                try:
                    page.evaluate("() => { window.scrollBy(0, 600); }")
                    time.sleep(1)
                    page.evaluate("() => { window.scrollTo(0, 0); }")
                except Exception:
                    pass

                self._abort_if_cancelled()
                time.sleep(3)
                self._abort_if_cancelled()

                # 4. Extract embedded stream configs from scripts and DOM
                try:
                    extracted = page.evaluate("""() => {
                        const found = [];
                        // Scan scripts for m3u8 and mp4 URLs
                        document.querySelectorAll('script').forEach(s => {
                            const txt = s.textContent || '';
                            const m3u8s = txt.match(/https?:\\\\?\\/\\\\?\\/[^\\s"'<>]+?\\.m3u8[^\\s"'<>]*/g) || [];
                            m3u8s.forEach(u => found.push(u.replace(/\\\\/g, '')));
                            const mp4s = txt.match(/https?:\\\\?\\/\\\\?\\/[^\\s"'<>]+?\\.mp4[^\\s"'<>]*/g) || [];
                            mp4s.forEach(u => found.push(u.replace(/\\\\/g, '')));
                        });
                        // Scan video and source tags
                        document.querySelectorAll('video, source').forEach(el => {
                            if (el.src) found.push(el.src);
                            if (el.dataset && el.dataset.src) found.push(el.dataset.src);
                        });
                        return found;
                    }""")
                    if extracted:
                        for u in extracted:
                            if not any(s["url"] == u for s in captured_streams):
                                stype = "hls" if ".m3u8" in u.lower() else "mp4"
                                captured_streams.append({"type": stype, "url": u, "headers": {}})
                except Exception:
                    pass

            except Exception as e:
                self.log(f"Navigation note: {e}", 55)
            finally:
                for closer in (context.close, browser.close):
                    try:
                        closer()
                    except Exception:
                        pass

        if not captured_streams:
            try:
                res = self._resolve_course_media(url, quality=quality)
                if res:
                    return res
            except Exception:
                pass
            raise RuntimeError(
                "Content is strictly locked behind user login/enrollment on this platform. "
                "The link cannot be accessed publicly without account authorization."
            )

        self.log(f"Intercepted {len(captured_streams)} stream candidate(s)!", 65)

        # Prefer adaptive manifests over whatever happened to load first - the
        # first response is very often an ad or a preview segment.
        priority = {"hls": 0, "dash": 1, "mp4": 2}

        # Probe and score each stream candidate for duration, full-length vs trailer
        scored_streams = []
        for s in captured_streams:
            meta = _probe_stream_metadata(s["url"], headers=s.get("headers"), stream_type=s.get("type", "hls"))
            meta["original"] = s
            p_val = priority.get(s.get("type"), 9)
            meta["total_score"] = meta["score"] - (p_val * 10)
            scored_streams.append(meta)

        # Distinguish full-length videos from short preview clips / trailers
        full_videos = [st for st in scored_streams if not st["is_trailer"] or st["duration"] >= 180]
        pool = full_videos if full_videos else scored_streams

        # Sort pool by score, duration, and priority
        pool.sort(key=lambda x: (x["total_score"], x["duration"], -priority.get(x["type"], 9)), reverse=True)
        best_meta = pool[0]
        best = best_meta["original"]
        stream_url = best["url"]
        stream_type = best["type"]
        stream_headers = best.get("headers")

        clean_title = re.sub(r'[\\/*?:"<>|]', "", page_title[0]).strip()
        if not clean_title or clean_title == "Captured_Media":
            clean_title = f"Media_Stream_{int(time.time())}"

        # Download primary full-length stream
        if stream_type == "hls" or ".m3u8" in stream_url:
            primary_res = self._download_stream_ffmpeg(stream_url, title=clean_title, quality=quality, headers=stream_headers)
        else:
            primary_res = self._download_direct_file(stream_url, title=clean_title, quality=quality, headers=stream_headers)

        all_downloaded = [primary_res["filepath"]]

        # Support multi-video pages: identify and download any other distinct full videos present on the page
        distinct_candidates = []
        for cand in pool[1:]:
            cand_url = cand["url"]
            cand_base = cand_url.split("?")[0].rsplit("/", 1)[0]
            best_base = best_meta["url"].split("?")[0].rsplit("/", 1)[0]
            if cand_base != best_base and cand["total_score"] > 0:
                if not any(cand_url.split("?")[0].rsplit("/", 1)[0] == d["url"].split("?")[0].rsplit("/", 1)[0] for d in distinct_candidates):
                    distinct_candidates.append(cand)

        if distinct_candidates:
            self.log(f"Detected {len(distinct_candidates)} additional distinct video(s) on page! Downloading all...", 85)
            for idx, extra in enumerate(distinct_candidates):
                extra_stream = extra["original"]
                sub_title = f"{clean_title} - Video {idx + 2}"
                try:
                    if extra_stream["type"] == "hls" or ".m3u8" in extra_stream["url"]:
                        sub_res = self._download_stream_ffmpeg(extra_stream["url"], title=sub_title, quality=quality, headers=extra_stream.get("headers"))
                    else:
                        sub_res = self._download_direct_file(extra_stream["url"], title=sub_title, quality=quality, headers=extra_stream.get("headers"))
                    if sub_res and sub_res.get("filepath") and os.path.exists(sub_res["filepath"]):
                        all_downloaded.append(sub_res["filepath"])
                except Exception as extra_err:
                    self.log(f"Additional video {idx + 2} notice: {extra_err}", 90)

        primary_res["requested_downloads"] = [{"filepath": p} for p in all_downloaded]
        primary_res["count"] = len(all_downloaded)
        return primary_res

    def _run_ffmpeg(self, cmd, timeout, out_file):
        """Run FFmpeg, killing it on cancellation or timeout.

        communicate(timeout=...) raises but leaves the child running; an
        abandoned FFmpeg would keep writing to the user's disk indefinitely.
        """
        if not shutil.which("ffmpeg"):
            raise RuntimeError("FFmpeg is required for stream capture but was not found on PATH.")
        p = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        waited = 0.0
        while True:
            try:
                _out, err = p.communicate(timeout=1)
                return p.returncode, (err or b"").decode("utf-8", "replace")
            except subprocess.TimeoutExpired:
                waited += 1.0
                if self.cancelled():
                    self._kill(p)
                    _remove_quietly(out_file)
                    raise InterceptorCancelled("Stream capture cancelled by user.")
                if waited > timeout:
                    self._kill(p)
                    _remove_quietly(out_file)
                    raise RuntimeError(f"FFmpeg stream capture timed out after {int(timeout)}s.")

    @staticmethod
    def _kill(p):
        try:
            p.kill()
            p.wait(timeout=5)
        except Exception:
            pass

    def _download_stream_ffmpeg(self, stream_url, title="Downloaded_Stream", quality="4k", headers=None):
        self._abort_if_cancelled()
        self.log("Capturing stream via high-speed FFmpeg reassembly...", 75)
        out_file = self._unique_path(os.path.join(self.output_path, f"{title}.mp4"))

        header_lines = [
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ]
        if headers and isinstance(headers, dict):
            for hk, hv in headers.items():
                if hk.lower() in ("referer", "origin", "cookie", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site"):
                    header_lines.append(f"{hk}: {hv}")
        header_blob = "\r\n".join(header_lines) + "\r\n"

        common = [
            "ffmpeg", "-nostdin", "-y",
            "-headers", header_blob,
            "-fflags", "+genpts+discardcorrupt",
            "-avoid_negative_ts", "make_zero",
            "-i", stream_url,
        ]

        try:
            rc, err = self._run_ffmpeg(
                common + ["-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", out_file],
                timeout=900,
                out_file=out_file,
            )

            file_ok = rc == 0 and os.path.exists(out_file) and os.path.getsize(out_file) > 200 * 1024
            if not file_ok:
                self.log("Stream container copy produced an unusable file. Re-encoding to universal H.264...", 85)
                _remove_quietly(out_file)
                rc, err = self._run_ffmpeg(
                    common + [
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                        "-c:a", "aac", "-b:a", "192k",
                        "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart",
                        out_file,
                    ],
                    timeout=1800,
                    out_file=out_file,
                )
                if rc != 0:
                    tail = " | ".join((err or "").strip().splitlines()[-4:])
                    _remove_quietly(out_file)
                    raise RuntimeError(f"FFmpeg re-encode failed: {tail}")

            if not os.path.exists(out_file) or os.path.getsize(out_file) < 20 * 1024:
                _remove_quietly(out_file)
                raise RuntimeError("FFmpeg stream capture generated an empty or corrupted file.")

            self.log(f"Stream saved cleanly: {os.path.basename(out_file)}", 100)
            return {
                "title": title,
                "filepath": out_file,
                "filename": os.path.basename(out_file),
                "url": stream_url,
                "requested_downloads": [{"filepath": out_file}],
            }
        except InterceptorCancelled:
            raise
        except RuntimeError:
            raise
        except Exception as e:
            _remove_quietly(out_file)
            raise RuntimeError(f"FFmpeg stream capture failed: {e}")

    def _stream_to_file(self, url, headers, dest):
        """Stream a URL to disk, aborting cleanly on cancellation."""
        resp = _safe_cffi_get(url, headers=headers, stream=True, timeout=30)
        resp.raise_for_status()
        try:
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=262144):
                    if self.cancelled():
                        raise InterceptorCancelled("Download cancelled by user.")
                    if chunk:
                        f.write(chunk)
        except BaseException:
            # A partially written file is worse than none - it looks complete
            # in the library and fails to play.
            _remove_quietly(dest)
            raise
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _download_direct_file(self, file_url, title="Downloaded_Media", quality="4k", headers=None):
        self._abort_if_cancelled()
        self.log("Downloading direct media stream file...", 80)
        quality = (quality or "4k").lower()

        # Detect source extension from URL path
        ext = "mp4"
        parsed_path = urlparse(file_url).path
        for candidate_ext in [".mp4", ".webm", ".mkv", ".m4a", ".mp3", ".flv", ".ts", ".m4v", ".avi", ".mov", ".ogg", ".opus", ".wav", ".aac"]:
            if parsed_path.lower().endswith(candidate_ext):
                ext = candidate_ext.lstrip(".")
                break

        safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "Media_File"
        target_ext = "mp3" if quality == "audio" else ext
        out_file = self._unique_path(os.path.join(self.output_path, f"{safe_title}.{target_ext}"))

        req_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "*/*"
        }
        if headers and isinstance(headers, dict):
            for k in ("referer", "origin", "cookie"):
                for hk, hv in headers.items():
                    if hk.lower() == k:
                        req_headers[hk] = hv

        # If audio conversion is required, stream to a temp file first.
        if quality == "audio" and ext != "mp3":
            temp_stage = os.path.join(
                tempfile.gettempdir(), f"raw_{uuid.uuid4().hex[:10]}.{ext}"
            )
            try:
                self._stream_to_file(file_url, req_headers, temp_stage)
                self.log("Transcoding media to studio MP3 320kbps...", 90)
                rc, err = self._run_ffmpeg(
                    ["ffmpeg", "-nostdin", "-y", "-i", temp_stage, "-vn",
                     "-codec:a", "libmp3lame", "-b:a", "320k", out_file],
                    timeout=900,
                    out_file=out_file,
                )
                if rc != 0:
                    tail = " | ".join((err or "").strip().splitlines()[-4:])
                    _remove_quietly(out_file)
                    raise RuntimeError(f"Audio transcode failed: {tail}")
            finally:
                _remove_quietly(temp_stage)
        else:
            self._stream_to_file(file_url, req_headers, out_file)

        if not os.path.exists(out_file) or os.path.getsize(out_file) < 20 * 1024:
            _remove_quietly(out_file)
            raise RuntimeError("Downloaded media file is empty or corrupted.")

        self.log(f"Saved: {os.path.basename(out_file)}", 100)
        return {
            "title": safe_title,
            "filepath": out_file,
            "filename": os.path.basename(out_file),
            "url": file_url,
            "requested_downloads": [{"filepath": out_file}]
        }
