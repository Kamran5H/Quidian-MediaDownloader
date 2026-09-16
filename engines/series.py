"""
Quidian - Series and Episodic Drama Downloader Engine.
Supports batch downloading full seasons/episodes in Full HD (1080p)
along with synchronized, cleaned subtitle tracks.
"""

import os
import sys
import re
import json
import time
import subprocess
import urllib.request
import urllib.parse
from bs4 import BeautifulSoup

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


class SeriesDownloader:
    """Automated series and Turkish drama downloader with subtitle sync."""

    def __init__(self, output_dir=None, quality="1080p"):
        self.output_dir = output_dir or os.path.join(os.path.expanduser("~"), "Downloads")
        self.quality = quality
        os.makedirs(self.output_dir, exist_ok=True)

    def clean_subtitles(self, raw_text):
        """Clean promo tags, fix timestamps, keep all dialogue lines, and renumber cues sequentially."""
        text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\ufeff", "").replace("\u200b", "")
        raw_blocks = [b.strip() for b in re.split(r'\n\s*\n', text) if b.strip()]
        cues = []
        time_re = re.compile(r'^(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})(?:[^\n]*)?$')
        
        for block in raw_blocks:
            lines = [l.rstrip() for l in block.split('\n') if l.strip()]
            if not lines:
                continue
            time_idx = -1
            for idx, line in enumerate(lines):
                if '-->' in line and time_re.match(line.strip()):
                    time_idx = idx
                    break
            if time_idx == -1:
                continue
            timing_line = lines[time_idx].strip()
            m = time_re.match(timing_line)
            start_t = m.group(1).replace('.', ',')
            end_t = m.group(2).replace('.', ',')
            timing = f"{start_t} --> {end_t}"
            
            payload_lines = lines[time_idx + 1:]
            if not payload_lines:
                continue
                
            filtered_payload = []
            for pl in payload_lines:
                lowered = pl.lower()
                if any(ad in lowered for ad in ["opensubtitles", "advertise your product", "osmanonline", "kayifamily"]):
                    if not any(k in lowered for k in ["osman", "bey", "hatun", "sultan", "sheikh", "balgay"]):
                        continue
                filtered_payload.append(pl.strip())
                
            payload = "\n".join(filtered_payload).strip()
            if not payload:
                continue
            cues.append((timing, payload))

        out_lines = []
        for idx, (timing, payload) in enumerate(cues, 1):
            out_lines.append(f"{idx}\n{timing}\n{payload}\n")
        return "\n".join(out_lines) + "\n"

    def fetch_subtitles_from_cat(self, series_name, episode_num, season_num=1):
        """Fetch matching English subtitle file from community repository."""
        clean_name = series_name.replace(" ", ".")
        candidates = [
            f"https://www.subtitlecat.com/subs/714/%2B{clean_name}.S{season_num:02d}E{episode_num}.html",
            f"https://www.subtitlecat.com/subs/238/{clean_name}.S{season_num:02d}E{episode_num:02d}.html",
            f"https://www.subtitlecat.com/subs/239/{clean_name}.S{season_num:02d}E{episode_num:02d}.html",
        ]
        headers = {"User-Agent": "Mozilla/5.0"}

        for page_url in candidates:
            try:
                req = urllib.request.Request(page_url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    soup = BeautifulSoup(resp.read().decode('utf-8', errors='ignore'), 'html.parser')
                    for a in soup.find_all('a', href=True):
                        if '-en.srt' in a['href']:
                            dl_url = urllib.parse.urljoin("https://www.subtitlecat.com", a['href'])
                            sub_req = urllib.request.Request(dl_url, headers=headers)
                            with urllib.request.urlopen(sub_req, timeout=15) as sub_resp:
                                return sub_resp.read().decode('utf-8', errors='ignore')
            except Exception:
                continue

        # Search fallback
        try:
            search_url = f"https://www.subtitlecat.com/index.php?search={urllib.parse.quote_plus(f'{series_name} {episode_num}')}"
            req = urllib.request.Request(search_url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                soup = BeautifulSoup(resp.read().decode('utf-8', errors='ignore'), 'html.parser')
                for a in soup.find_all('a', href=True):
                    if f"E{episode_num}" in a['href'] or f"{episode_num}." in a['href']:
                        sub_page = urllib.parse.urljoin("https://www.subtitlecat.com", a['href'])
                        sub_req = urllib.request.Request(sub_page, headers=headers)
                        with urllib.request.urlopen(sub_req, timeout=10) as pr:
                            psoup = BeautifulSoup(pr.read().decode('utf-8', errors='ignore'), 'html.parser')
                            for pa in psoup.find_all('a', href=True):
                                if '-en.srt' in pa['href']:
                                    dl_url = urllib.parse.urljoin("https://www.subtitlecat.com", pa['href'])
                                    with urllib.request.urlopen(urllib.request.Request(dl_url, headers=headers), timeout=15) as sr:
                                        return sr.read().decode('utf-8', errors='ignore')
        except Exception:
            pass

        return None

    def search_youtube_episode(self, series_name, episode_num):
        """Search YouTube for the official full episode video ID."""
        queries = [
            f"ytsearch5:{series_name} {episode_num}. Bolum",
            f"ytsearch5:{series_name} Episode {episode_num}",
        ]
        
        # In-process search via yt_dlp module if available
        if yt_dlp is not None:
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": True,
                "skip_download": True,
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    for q in queries:
                        try:
                            info = ydl.extract_info(q, download=False)
                            entries = (info or {}).get("entries", []) or []
                            for data in entries:
                                if not data:
                                    continue
                                title = data.get("title", "")
                                duration = data.get("duration") or 0
                                vid_id = data.get("id")
                                if duration > 2700:
                                    if f"{episode_num}. B" in title or f"{episode_num}.b" in title or f"{episode_num} " in title or f"Episode {episode_num}" in title:
                                        return vid_id, title, duration
                        except Exception:
                            continue
            except Exception:
                pass

        python_exe = sys.executable
        for q in queries:
            try:
                cmd = [python_exe, "-m", "yt_dlp", q, "--dump-json", "--flat-playlist"]
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    timeout=30,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                )
                for line in res.stdout.splitlines():
                    try:
                        data = json.loads(line)
                        title = data.get("title", "")
                        duration = data.get("duration") or 0
                        vid_id = data.get("id")
                        # Check duration is a full episode (> 45 min = 2700s)
                        if duration > 2700:
                            if f"{episode_num}. B" in title or f"{episode_num}.b" in title or f"{episode_num} " in title or f"Episode {episode_num}" in title:
                                return vid_id, title, duration
                    except Exception:
                        continue
            except Exception:
                continue
        return None, None, None

    def verify_video(self, video_path):
        """Verify video exists, has audio, and has expected resolution."""
        if not os.path.exists(video_path) or os.path.getsize(video_path) < 50_000_000:
            return False
        try:
            cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=width,height,codec_type", "-of", "json", video_path]
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
            data = json.loads(res.stdout)
            has_vid = any(s.get("codec_type") == "video" for s in data.get("streams", []))
            has_aud = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
            return has_vid and has_aud
        except Exception:
            return False

    def download_episode(self, series_name, episode_num, season_num=1, base_filename=None):
        """Download a single episode with matching subtitles."""
        if not base_filename:
            safe_name = re.sub(r'[^\w\-]', '_', series_name)
            base_filename = f"{safe_name}_E{episode_num:02d}"

        out_mkv = os.path.join(self.output_dir, f"{base_filename}.mkv")
        out_srt = os.path.join(self.output_dir, f"{base_filename}.en.srt")

        # 1. Download & Save Subtitles
        if not (os.path.exists(out_srt) and os.path.getsize(out_srt) > 5000):
            print(f"[*] Fetching subtitles for {series_name} Episode {episode_num}...")
            raw_sub = self.fetch_subtitles_from_cat(series_name, episode_num, season_num)
            if raw_sub:
                cleaned = self.clean_subtitles(raw_sub)
                with open(out_srt, "w", encoding="utf-8-sig") as f:
                    f.write(cleaned)
                print(f"[✓] Saved subtitles: {os.path.basename(out_srt)}")
            else:
                print(f"[!] Warning: Subtitles not found online for Episode {episode_num}")

        # 2. Check if video already exists
        if self.verify_video(out_mkv):
            print(f"[✓] Video already downloaded: {os.path.basename(out_mkv)}")
            return True

        # 3. Locate YouTube Video
        vid_id, title, duration = self.search_youtube_episode(series_name, episode_num)
        if not vid_id:
            print(f"[X] Could not find full episode video for {series_name} Episode {episode_num}")
            return False

        print(f"[*] Downloading {title} (ID: {vid_id}, Duration: {duration//60}m)...")
        url = f"https://www.youtube.com/watch?v={vid_id}"
        out_tmpl = os.path.join(self.output_dir, f"{base_filename}.%(ext)s")

        fmt = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best" if self.quality == "1080p" else "bestvideo+bestaudio/best"
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--no-playlist",
            "-f", fmt,
            "--merge-output-format", "mkv",
            "--retries", "10",
            "--fragment-retries", "10",
            "--concurrent-fragments", "5",
            "-o", out_tmpl,
            url
        ]
        try:
            subprocess.run(
                cmd,
                check=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
            return self.verify_video(out_mkv)
        except Exception as e:
            print(f"[X] Download error on Episode {episode_num}: {e}")
            return False

    def download_range(self, series_name, start_ep, end_ep, season_num=1):
        """Batch download a range of episodes."""
        print(f"\n==================================================================")
        print(f"Starting batch download for {series_name}: Episodes {start_ep} to {end_ep}")
        print(f"Output folder: {self.output_dir}")
        print(f"==================================================================\n")
        results = {}
        for ep in range(start_ep, end_ep + 1):
            ok = self.download_episode(series_name, ep, season_num)
            results[ep] = ok
            time.sleep(1)
        return results
