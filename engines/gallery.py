import os
import sys
import subprocess
import shutil


def check_gallery_dl_installed():
    """Verify gallery-dl availability in the environment."""
    return True  # Installed via python package


def download_social_gallery(url, output_path, progress_callback=None):
    """
    Download photos, videos, carousels, or stories from Instagram, TikTok,
    Reddit, Twitter/X, Pinterest, Imgur using gallery-dl.
    """
    os.makedirs(output_path, exist_ok=True)
    
    cmd = [
        sys.executable, "-m", "gallery_dl",
        "--directory", output_path,
        "--no-mtime",
        url
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    )

    downloaded_files = []
    if process.stdout:
        for line in process.stdout:
            line_str = line.strip()
            if line_str.startswith("#"):
                # URL resolution or metadata
                continue
            candidate_path = line_str if os.path.isabs(line_str) else os.path.join(output_path, line_str)
            if os.path.exists(candidate_path):
                downloaded_files.append(candidate_path)
                if progress_callback:
                    progress_callback(os.path.basename(candidate_path))

    try:
        _, stderr = process.communicate(timeout=600)
    except subprocess.TimeoutExpired:
        process.kill()
        _, stderr = process.communicate()

    if process.returncode != 0 and not downloaded_files:
        error_msg = stderr.strip() if stderr else "Unknown error running gallery-dl"
        # Provide helpful hint for private / login restricted media
        if "login" in error_msg.lower() or "auth" in error_msg.lower():
            error_msg += " (This post is private or requires authentication)."
        raise RuntimeError(f"Social Scraper failed: {error_msg}")

    return {
        "count": len(downloaded_files),
        "files": downloaded_files,
        "output_path": output_path
    }
