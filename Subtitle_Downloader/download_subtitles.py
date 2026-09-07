import os
import sys
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
        'subtitleslangs': [f'{lang}.*', lang], 
        'paths': {'home': output_path, 'temp': temp_dir},
        'outtmpl': {'default': '%(title)s.%(ext)s'},
        'noplaylist': True,  # Prevent downloading entire playlists/mixes
        'no_warnings': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        print(f"\n[✔] Subtitle download from URL completed! Saved in '{output_path}'.")
    except yt_dlp.utils.DownloadError as e:
        print(f"\n[✖] Download failed: {e}")
        raise RuntimeError(f"Download failed: {e}")
    except Exception as e:
        print(f"\n[✖] An unexpected error occurred: {e}")
        raise RuntimeError(f"Unexpected error: {e}")

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
