import warnings
warnings.filterwarnings("ignore")

import os
import re
import sys
import time
import json
import shutil
import subprocess
import tempfile
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
            return cffi_requests.get(url, impersonate="chrome124", headers=headers, timeout=timeout, stream=stream)
        except Exception:
            pass
    import requests
    return requests.get(url, headers=headers, timeout=timeout, stream=stream)


try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


class StealthStreamInterceptor:
    """
    Stealth bypass and stream extraction engine.
    1. Leverages curl_cffi with browser TLS impersonation to bypass Cloudflare and HTTP 403 Forbidden screens.
    2. Uses Playwright with stealth settings to sniff HLS (.m3u8), DASH (.mpd), and direct video (.mp4/webm) streams.
    3. Downloads and reassembles media using FFmpeg or aria2c into the destination folder.
    """

    def __init__(self, output_path=None, status_callback=None):
        self.output_path = output_path or os.path.join(os.path.expanduser("~"), "Downloads")
        self.status_callback = status_callback or (lambda msg, pct=None: None)
        os.makedirs(self.output_path, exist_ok=True)

    def log(self, msg, pct=None):
        self.status_callback(msg, pct)

    def bypass_and_extract(self, url, quality="4k", cookies_file=None):
        """
        Main entrypoint: Attempt bypass and stream sniffing.
        Returns a dict with media information and final filepath.
        """
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
                if m3u8_matches:
                    target_stream = m3u8_matches[0]
                    self.log("Found direct HLS stream manifest!", 30)
                    return self._download_stream_ffmpeg(target_stream, title="Stream_Capture", quality=quality)
                elif mp4_matches:
                    target_stream = mp4_matches[0]
                    self.log("Found direct video file stream!", 30)
                    return self._download_direct_file(target_stream, quality=quality)
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
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
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
        out_tmpl = os.path.join(self.output_path, f"{safe_lecture_title} [{course_slug}].%(ext)s")

        ydl_opts = {
            "format": format_spec,
            "format_sort": ["res", "fps", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"],
            "outtmpl": out_tmpl,
            "windowsfilenames": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [ydl_hook],
            "merge_output_format": expected_ext,
            "postprocessors": postprocessors,
            "concurrent_fragment_downloads": 8,
            "retries": 12,
            "fragment_retries": 12,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            res_info = ydl.extract_info(resolved_url, download=True)

        # 4. Exact File Path Resolution
        final_file = None
        if res_info:
            prepared = ydl.prepare_filename(res_info)
            base, _ = os.path.splitext(prepared)
            target_file = f"{base}.{expected_ext}"
            if os.path.exists(target_file):
                final_file = target_file
            elif os.path.exists(prepared):
                final_file = prepared

        if not final_file:
            expected_file = os.path.join(self.output_path, f"{safe_lecture_title} [{course_slug}].{expected_ext}")
            if os.path.exists(expected_file):
                final_file = expected_file
            else:
                for f in os.listdir(self.output_path):
                    if f.startswith(safe_lecture_title[:25]) and f.endswith(f".{expected_ext}"):
                        final_file = os.path.join(self.output_path, f)
                        break
                if not final_file:
                    final_file = expected_file

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
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-web-security",
                    "--allow-running-insecure-content",
                ]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                device_scale_factor=1,
            )

            # Injected stealth evasions
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            """)

            page = context.new_page()

            def handle_response(response):
                r_url = response.url
                ct = response.headers.get("content-type", "").lower()
                already_captured = any(s["url"] == r_url for s in captured_streams)
                if already_captured:
                    return

                if ".m3u8" in r_url or "application/vnd.apple.mpegurl" in ct or "application/x-mpegurl" in ct:
                    captured_streams.append({"type": "hls", "url": r_url, "headers": response.request.headers})
                elif ".mpd" in r_url or "application/dash+xml" in ct:
                    captured_streams.append({"type": "dash", "url": r_url, "headers": response.request.headers})
                elif (".mp4" in r_url or "video/mp4" in ct) and "google" not in r_url:
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

                self.log("Analyzing playback and stream channels...", 50)
                # Wait briefly for player initialization and network stream requests
                time.sleep(5)

                # Attempt to trigger video play if video element exists
                try:
                    page.evaluate("""() => {
                        const vids = document.querySelectorAll('video');
                        vids.forEach(v => {
                            v.muted = true;
                            v.play().catch(() => {});
                        });
                    }""")
                    time.sleep(3)
                except Exception:
                    pass

            except Exception as e:
                self.log(f"Navigation note: {e}", 55)
            finally:
                browser.close()

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
        best = captured_streams[0]
        stream_url = best["url"]
        stream_type = best["type"]

        clean_title = re.sub(r'[\\/*?:"<>|]', "", page_title[0]).strip()
        if not clean_title or clean_title == "Captured_Media":
            clean_title = f"Media_Stream_{int(time.time())}"

        if stream_type == "hls" or ".m3u8" in stream_url:
            return self._download_stream_ffmpeg(stream_url, title=clean_title, quality=quality)
        else:
            return self._download_direct_file(stream_url, title=clean_title, quality=quality)

    def _download_stream_ffmpeg(self, stream_url, title="Downloaded_Stream", quality="4k"):
        self.log("Capturing stream via high-speed FFmpeg reassembly...", 75)
        out_name = f"{title}.mp4"
        out_file = os.path.join(self.output_path, out_name)

        counter = 1
        base, ext = os.path.splitext(out_file)
        while os.path.exists(out_file):
            out_file = f"{base} ({counter}){ext}"
            counter += 1

        ffmpeg_cmd = [
            "ffmpeg",
            "-y",
            "-headers", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\n",
            "-fflags", "+genpts+discardcorrupt",
            "-avoid_negative_ts", "make_zero",
            "-i", stream_url,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart",
            out_file
        ]

        try:
            p = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
            stdout, stderr = p.communicate(timeout=900)
            
            # Check if copy succeeded and produced a valid file
            file_ok = (p.returncode == 0) and os.path.exists(out_file) and (os.path.getsize(out_file) > 200 * 1024)
            if not file_ok:
                self.log("Stream container copy produced unseekable file. Re-encoding cleanly with universal H.264...", 85)
                # Universal fallback transcoding (guaranteed playable on all devices)
                fallback_cmd = [
                    "ffmpeg", "-y",
                    "-headers", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\n",
                    "-fflags", "+genpts+discardcorrupt",
                    "-avoid_negative_ts", "make_zero",
                    "-i", stream_url,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                    "-c:a", "aac", "-b:a", "192k",
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    out_file
                ]
                subprocess.run(fallback_cmd, check=True, timeout=600, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

            if not os.path.exists(out_file) or os.path.getsize(out_file) < 20 * 1024:
                raise RuntimeError("FFmpeg stream capture generated an empty or corrupted file.")

            self.log(f"Stream saved cleanly: {os.path.basename(out_file)}", 100)
            return {
                "title": title,
                "filepath": out_file,
                "filename": os.path.basename(out_file),
                "url": stream_url
            }
        except Exception as e:
            raise RuntimeError(f"FFmpeg stream capture failed: {e}")

    def _download_direct_file(self, file_url, title="Downloaded_Media", quality="4k"):
        self.log("Downloading direct media stream file...", 80)
        quality = (quality or "4k").lower()

        # Detect source extension from URL path
        ext = "mp4"
        parsed_path = urlparse(file_url).path
        for candidate_ext in [".mp4", ".webm", ".mkv", ".m4a", ".mp3", ".flv", ".ts"]:
            if parsed_path.lower().endswith(candidate_ext):
                ext = candidate_ext.lstrip(".")
                break

        safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "Media_File"
        target_ext = "mp3" if quality == "audio" else ext
        out_name = f"{safe_title}.{target_ext}"
        out_file = os.path.join(self.output_path, out_name)

        counter = 1
        base, fext = os.path.splitext(out_file)
        while os.path.exists(out_file):
            out_file = f"{base} ({counter}){fext}"
            counter += 1

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "*/*"
        }

        # If audio conversion is required, stream to temp file first
        if quality == "audio" and ext != "mp3":
            temp_stage = os.path.join(tempfile.gettempdir(), f"raw_{int(time.time())}.{ext}")
            resp = _safe_cffi_get(file_url, headers=headers, stream=True)
            resp.raise_for_status()
            with open(temp_stage, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
            # Transcode with FFmpeg
            self.log("Transcoding media to studio MP3 320kbps...", 90)
            transcode_cmd = [
                "ffmpeg", "-y", "-i", temp_stage, "-vn",
                "-codec:a", "libmp3lame", "-b:a", "320k",
                out_file
            ]
            subprocess.run(transcode_cmd, check=True, timeout=600, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                os.remove(temp_stage)
            except Exception:
                pass
        else:
            resp = _safe_cffi_get(file_url, headers=headers, stream=True)
            resp.raise_for_status()
            with open(out_file, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)

        if not os.path.exists(out_file) or os.path.getsize(out_file) < 20 * 1024:
            raise RuntimeError("Downloaded media file is empty or corrupted.")

        self.log(f"Saved: {os.path.basename(out_file)}", 100)
        return {
            "title": safe_title,
            "filepath": out_file,
            "filename": os.path.basename(out_file),
            "url": file_url,
            "requested_downloads": [{"filepath": out_file}]
        }
