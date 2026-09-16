import os
import sys
import re
import subprocess
import shutil

try:
    import yt_dlp
except ImportError:
    print("[✖] Error: 'yt_dlp' is not installed.")
    print("    Please install it by running: pip install yt-dlp")
    sys.exit(1)

def download_subtitles_from_url(url, lang="en", output_path="Downloads"):
    """Downloads the best available subtitles for a video link or search query from the internet."""
    if not (url.startswith("http://") or url.startswith("https://")):
        print(f"\n[*] Treating input as search query: {url}")
        url = f"ytsearch1:{url}"
        
    print(f"\n[*] Fetching subtitles for URL/Query: {url}")
    
    if not os.path.exists(output_path):
        os.makedirs(output_path)
        
    deno_path = os.path.expanduser(r"~\.deno\bin")
    if os.path.exists(deno_path) and deno_path not in os.environ["PATH"]:
        os.environ["PATH"] += os.pathsep + deno_path

    import tempfile
    import uuid
    
    # Use system temp directory for intermediate files to avoid OneDrive file locks (WinError 32)
    temp_dir = os.path.join(tempfile.gettempdir(), "yt_dlp_temp", uuid.uuid4().hex[:10])
    if not os.path.exists(temp_dir):
        os.makedirs(temp_dir)

    ydl_opts = {
        'skip_download': True,           
        'writesubtitles': True,          
        'writeautomaticsub': True,       
        'subtitleslangs': [lang], 
        'paths': {'home': output_path, 'temp': temp_dir},
        'outtmpl': {'default': '%(title)s.%(ext)s'},
        'noplaylist': True,
        'no_warnings': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        print(f"\n[✔] Subtitle download from URL completed! Saved in '{output_path}'.")
    except Exception as e:
        print(f"\n[*] Standard fetch encountered: {e}. Attempting community subtitle database lookup...")
        try:
            _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if _base not in sys.path:
                sys.path.append(_base)
            from engines.series import SeriesDownloader
            s_engine = SeriesDownloader(output_dir=output_path)
            # Try to extract title
            with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
                info = ydl.extract_info(url, download=False)
                t = info.get('title', '')
                m = re.search(r'(\d+)', t)
                ep_num = int(m.group(1)) if m else 1
                raw = s_engine.fetch_subtitles_from_cat(t, ep_num)
                if raw:
                    cleaned = s_engine.clean_subtitles(raw)
                    clean_t = re.sub(r'[\\/*?:"<>|]', '', t).strip() or "Subtitles"
                    out_file = os.path.join(output_path, f"{clean_t}.{lang}.srt")
                    with open(out_file, 'w', encoding='utf-8-sig') as f:
                        f.write(cleaned)
                    print(f"\n[✔] Successfully fetched accurate subtitles from database! Saved: {out_file}")
                    return
        except Exception as fb_err:
            pass
        raise RuntimeError(f"Could not retrieve subtitles: {e}")

def download_series_subtitles(series_name, start_ep, end_ep, season_num=1, output_path="Downloads"):
    """Batch download cleaned, synchronized subtitles for a whole series."""
    _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _base not in sys.path:
        sys.path.append(_base)
    from engines.series import SeriesDownloader
    downloader = SeriesDownloader(output_dir=output_path)
    for ep in range(start_ep, end_ep + 1):
        raw = downloader.fetch_subtitles_from_cat(series_name, ep, season_num)
        if raw:
            cleaned = downloader.clean_subtitles(raw)
            safe_name = re.sub(r'[^\w\-]', '_', series_name)
            out_file = os.path.join(output_path, f"{safe_name}_E{ep:02d}.en.srt")
            with open(out_file, 'w', encoding='utf-8-sig') as f:
                f.write(cleaned)
            print(f"[✓] Saved subtitles: {os.path.basename(out_file)}")
        else:
            print(f"[!] Subtitles not found for Episode {ep}")

def download_subtitles_from_file(filepath, lang="en"):
    """Finds and downloads the most accurate subtitles from subtitle databases for a local video file."""
    if not shutil.which("subliminal"):
        print("\n[✖] Error: 'subliminal' is not installed or not in PATH.")
        raise RuntimeError("'subliminal' is not installed. Please install it with: pip install subliminal")

    print(f"\n[*] Searching subtitle databases for: {os.path.basename(filepath)}")
    cmd = ["subliminal", "download", "-l", lang, filepath]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        print(result.stdout)
        if result.returncode == 0:
            print("[✔] Subtitle search and download for local file completed!")
        else:
            print(f"[✖] Subliminal returned an error: {result.stderr}")
            raise RuntimeError(f"Subliminal error: {result.stderr.strip()}")
    except Exception as e:
        print(f"\n[✖] Error using subliminal: {e}")
        raise RuntimeError(f"Error executing subliminal: {e}")

if __name__ == "__main__":
    print("=" * 65)
    print("                UNIVERSAL SUBTITLE DOWNLOADER                ")
    print("=" * 65)
    print("Instructions: Paste a web link (e.g., YouTube) OR the path to a local video file.\n")
    
    try:
        user_input = input("Paste the link or file path here: ").strip()
        user_input = user_input.strip('"').strip("'")
        
        if not user_input:
            print("[!] No input provided. Exiting.")
        elif user_input.startswith("http://") or user_input.startswith("https://"):
            lang = input("Enter language code (e.g., 'en', 'ur') [default: en]: ").strip()
            lang = lang if lang else "en"
            download_subtitles_from_url(user_input, lang)
        elif os.path.exists(user_input) and os.path.isfile(user_input):
            lang = input("Enter language code (e.g., 'en', 'ur') [default: en]: ").strip()
            lang = lang if lang else "en"
            download_subtitles_from_file(user_input, lang)
        else:
            print("\n[✖] Invalid input. Please provide a valid URL or an existing file path.")
    except KeyboardInterrupt:
        print("\n\n[!] Process interrupted by user. Exiting.")
        sys.exit(0)
