"""
Quidian - Social media / gallery scraper (gallery-dl wrapper).
Developed by Kamran Ashraf
"""

import importlib
import os
import shutil
import subprocess
import sys
import threading
import time

DEFAULT_TIMEOUT = 900  # 15 minutes of wall clock for a whole gallery


def check_gallery_dl_installed():
    """Report whether gallery-dl can actually be imported or found on PATH."""
    try:
        importlib.import_module("gallery_dl")
        return True
    except Exception:
        return bool(shutil.which("gallery-dl"))


def _drain(stream, sink, on_line=None):
    """Consume a pipe to completion on its own thread.

    Reading only stdout while stderr filled its 64 KB pipe buffer would block
    gallery-dl forever; draining both concurrently is what prevents that.
    """
    try:
        for line in stream:
            sink.append(line)
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def download_social_gallery(url, output_path, progress_callback=None,
                            cancel_check=None, timeout=DEFAULT_TIMEOUT):
    """
    Download photos, videos, carousels or stories from Instagram, TikTok,
    Reddit, Twitter/X, Pinterest and Imgur using gallery-dl.

    Returns ``{"count", "files", "output_path"}``.
    """
    if not url or not str(url).lower().startswith(("http://", "https://")):
        raise ValueError("The social scraper requires a full http(s) URL.")

    os.makedirs(output_path, exist_ok=True)

    cmd = [
        sys.executable, "-m", "gallery_dl",
        "--directory", output_path,
        "--no-mtime",
        url,
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )

    downloaded_files = []
    seen = set()
    out_lines, err_lines = [], []

    def handle_stdout_line(line):
        line_str = line.strip()
        if not line_str or line_str.startswith("#"):
            # '#' marks an already-present file that gallery-dl skipped.
            return
        candidate = line_str if os.path.isabs(line_str) else os.path.join(output_path, line_str)
        if candidate in seen or not os.path.exists(candidate):
            return
        seen.add(candidate)
        downloaded_files.append(candidate)
        if progress_callback:
            try:
                progress_callback(os.path.basename(candidate))
            except Exception:
                pass

    threads = [
        threading.Thread(target=_drain, args=(process.stdout, out_lines, handle_stdout_line), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, err_lines), daemon=True),
    ]
    for t in threads:
        t.start()

    deadline = time.time() + max(30, int(timeout or DEFAULT_TIMEOUT))
    cancelled = False
    timed_out = False
    while process.poll() is None:
        if cancel_check and cancel_check():
            cancelled = True
            break
        if time.time() > deadline:
            timed_out = True
            break
        time.sleep(0.25)

    if cancelled or timed_out:
        _terminate(process)

    for t in threads:
        t.join(timeout=5)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _terminate(process)

    if cancelled:
        return {"count": len(downloaded_files), "files": downloaded_files,
                "output_path": output_path, "cancelled": True}

    if timed_out and not downloaded_files:
        raise RuntimeError(f"Social Scraper timed out after {timeout}s without downloading anything.")

    if process.returncode not in (0, None) and not downloaded_files:
        error_msg = "".join(err_lines).strip() or "Unknown error running gallery-dl"
        lowered = error_msg.lower()
        if "login" in lowered or "auth" in lowered or "403" in lowered:
            error_msg += " (This post is private or requires authentication.)"
        # Keep the surfaced message short enough to fit the status banner.
        if len(error_msg) > 600:
            error_msg = error_msg[:600] + "..."
        raise RuntimeError(f"Social Scraper failed: {error_msg}")

    return {"count": len(downloaded_files), "files": downloaded_files, "output_path": output_path}


def _terminate(process):
    """Stop a child process, escalating to kill if it ignores the terminate."""
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
