import warnings
warnings.filterwarnings("ignore")
warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", message=".*duckduckgo_search.*")

import sys
import os
import uuid
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, render_template

base_dir = os.path.dirname(os.path.abspath(__file__))

from engines import (
    download_media,
    check_aria2c_installed,
    inspect_playlist,
    download_batch,
    download_social_gallery,
    extract_subtitles,
    rip_and_tag_audio,
    list_downloads,
    open_downloaded_file,
    reveal_in_explorer,
    search_media,
    search_videos,
    search_web,
)
import yt_dlp

DownloadCancelled = getattr(yt_dlp.utils, "DownloadCancelled", None)

app = Flask(__name__)

DOWNLOADS_DIR = os.path.join(os.path.expanduser("~"), "Downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)


def _choose_folder_dialog(initial_dir=None):
    """Launch native Windows directory dialog without freezing Flask event loop."""
    init = initial_dir if (initial_dir and os.path.isdir(initial_dir)) else DOWNLOADS_DIR
    code = (
        "import tkinter as tk; from tkinter import filedialog; "
        "root = tk.Tk(); root.withdraw(); root.wm_attributes('-topmost', 1); "
        f"p = filedialog.askdirectory(initialdir={repr(init)}, title='Select Download Destination Folder'); "
        "print(p if p else '')"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )
        folder = proc.stdout.strip()
        if folder and os.path.isdir(folder):
            return os.path.normpath(folder)
    except Exception as e:
        print(f"Folder picker error: {e}")
    return None

# ---------------------------------------------------------------------------
# In-Memory Job Registry for non-blocking multi-engine execution
# ---------------------------------------------------------------------------
JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_TTL_SECONDS = 30 * 60
EXECUTOR = ThreadPoolExecutor(max_workers=3)


class _Cancelled(Exception):
    """Internal cancellation signal."""


def _sanitize_output_path(path):
    """Normalize and validate target download directory, falling back safely to user Downloads."""
    if not path or not isinstance(path, str):
        return DOWNLOADS_DIR
    clean = path.strip().strip('"').strip("'")
    if not clean or "\x00" in clean:
        return DOWNLOADS_DIR
    try:
        norm = os.path.normpath(os.path.abspath(clean))
        if os.path.isdir(norm):
            return norm
        os.makedirs(norm, exist_ok=True)
        return norm
    except Exception:
        return DOWNLOADS_DIR


def _prune_jobs():
    """Drop finished/errored/abandoned jobs older than the TTL to bound memory."""
    now = time.time()
    dead = []
    for jid, j in list(JOBS.items()):
        status = j.get("status")
        if status in ("done", "error", "cancelled") and (now - j.get("finished", j.get("created", now)) > JOB_TTL_SECONDS):
            dead.append(jid)
        elif status == "queued" and (now - j.get("created", now) > 7200):
            dead.append(jid)
    for jid in dead:
        JOBS.pop(jid, None)


def _periodic_prune():
    while True:
        time.sleep(600)
        try:
            with JOBS_LOCK:
                _prune_jobs()
        except Exception as e:
            print(f"Periodic prune warning: {e}")

threading.Thread(target=_periodic_prune, daemon=True).start()


def _new_job(job_type="download"):
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        _prune_jobs()
        JOBS[job_id] = {
            "id": job_id,
            "type": job_type,
            "status": "queued",
            "percent": 0.0,
            "speed": None,
            "eta": None,
            "title": None,
            "filename": None,
            "filepath": None,
            "phase": None,
            "message": "Queued...",
            "error": None,
            "cancel": False,
            "created": time.time(),
        }
    return job_id


def _update_job(job_id, **kwargs):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


def _is_cancelled(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        return bool(j and j.get("cancel"))


def _make_progress_hook(job_id):
    max_pct = [0.0]

    def hook(d):
        if _is_cancelled(job_id):
            raise (DownloadCancelled() if DownloadCancelled else _Cancelled())
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes") or 0
            if total:
                raw_pct = downloaded / total * 100.0
            elif d.get("fragment_count"):
                raw_pct = (d.get("fragment_index") or 0) / d["fragment_count"] * 100.0
            else:
                raw_pct = 0.0

            info = d.get("info_dict") or {}
            vcodec, acodec = info.get("vcodec"), info.get("acodec")
            has_v = vcodec and vcodec != "none"
            has_a = acodec and acodec != "none"
            if has_v and not has_a:
                phase = "Downloading video"
                eff_pct = min(80.0, raw_pct * 0.8)
            elif has_a and not has_v:
                phase = "Downloading audio"
                eff_pct = min(95.0, 80.0 + (raw_pct * 0.15))
            else:
                phase = "Downloading"
                eff_pct = min(95.0, raw_pct * 0.95)

            max_pct[0] = max(max_pct[0], eff_pct)
            display_pct = max_pct[0]

            with JOBS_LOCK:
                prev_title = JOBS.get(job_id, {}).get("title")
            _update_job(
                job_id,
                status="downloading",
                percent=round(display_pct, 1),
                speed=d.get("_speed_str") or (f"{(d.get('speed') or 0)/1048576:.2f} MiB/s" if d.get("speed") else None),
                eta=d.get("eta"),
                title=info.get("title") or prev_title,
                filename=os.path.basename(d.get("filename") or ""),
                phase=phase,
                message=phase + "...",
            )
        elif status == "finished":
            max_pct[0] = max(max_pct[0], 95.0)
            _update_job(job_id, percent=max_pct[0], message="Stream complete, finalizing media...")
    return hook


def _make_pp_hook(job_id):
    def hook(d):
        if _is_cancelled(job_id):
            raise (DownloadCancelled() if DownloadCancelled else _Cancelled())
        if d.get("status") == "started":
            name = d.get("postprocessor") or ""
            if "Merger" in name:
                msg = "Merging video + audio streams..."
            elif "ExtractAudio" in name:
                msg = "Converting audio to MP3..."
            elif "EmbedSubtitle" in name:
                msg = "Embedding subtitles..."
            elif "ThumbnailsConvertor" in name:
                msg = "Preparing cover art..."
            else:
                msg = "Finalizing media..."
            _update_job(job_id, status="processing", percent=98.0, phase="processing", message=msg)
    return hook


def _run_download_task(job_id, url, quality, use_turbo, output_path=None, use_stealth=True):
    if _is_cancelled(job_id):
        _update_job(job_id, status="cancelled", percent=0.0, finished=time.time(), message="Download cancelled.")
        return

    target_dir = _sanitize_output_path(output_path)
    _update_job(job_id, status="downloading", message="Starting stream extraction...")
    try:
        def status_cb(msg, pct=None):
            kw = {"message": msg}
            if pct is not None:
                kw["percent"] = pct
            _update_job(job_id, **kw)

        info = download_media(
            url,
            output_path=target_dir,
            quality=quality,
            progress_hook=_make_progress_hook(job_id),
            postprocessor_hook=_make_pp_hook(job_id),
            use_turbo=use_turbo,
            status_callback=status_cb,
            use_stealth=use_stealth,
        )
        title = (info or {}).get("title") if isinstance(info, dict) else None
        downloads = (info or {}).get("requested_downloads") or []
        fpath = downloads[0].get("filepath") if downloads else (info or {}).get("filepath")
        fname = os.path.basename(fpath) if fpath else None
        with JOBS_LOCK:
            prev_title = JOBS.get(job_id, {}).get("title")
        final_title = title or prev_title or (os.path.splitext(fname)[0] if fname else "Media")
        _update_job(job_id, status="done", percent=100.0, title=final_title,
                    filepath=fpath, filename=fname,
                    finished=time.time(),
                    message=f"Saved: {fname}" if fname else f"Saved to {target_dir}")
    except Exception as e:
        cancelled = (DownloadCancelled and isinstance(e, DownloadCancelled)) or isinstance(e, _Cancelled) \
            or "cancel" in str(e).lower()
        if cancelled:
            _update_job(job_id, status="cancelled", percent=0.0, finished=time.time(), message="Download cancelled.")
        else:
            err_msg = str(e)
            if "DRM_PROTECTED|" in err_msg:
                parts = err_msg.split("|")
                platform = parts[1] if len(parts) > 1 else "Protected Platform"
                suggested_title = parts[2] if len(parts) > 2 else ""
                clean_msg = parts[3] if len(parts) > 3 else err_msg
                _update_job(
                    job_id,
                    status="error",
                    error_type="drm_protected",
                    platform=platform,
                    suggested_title=suggested_title,
                    error=clean_msg,
                    finished=time.time(),
                    message=clean_msg
                )
            else:
                _update_job(job_id, status="error", error=err_msg, finished=time.time(), message=err_msg)


def _run_social_task(job_id, url, output_path=None):
    if _is_cancelled(job_id):
        _update_job(job_id, status="cancelled", percent=0.0, finished=time.time(), message="Task cancelled.")
        return

    target_dir = _sanitize_output_path(output_path)
    _update_job(job_id, status="downloading", message="Scraping social media via gallery-dl...")
    try:
        def on_item(fname):
            _update_job(job_id, message=f"Saved: {fname}")

        res = download_social_gallery(url, output_path=target_dir, progress_callback=on_item)
        count = res.get("count", 0)
        _update_job(job_id, status="done", percent=100.0, finished=time.time(),
                    message=f"Scraped {count} items successfully into {target_dir}.")
    except Exception as e:
        _update_job(job_id, status="error", error=str(e), finished=time.time(), message=str(e))


def _run_audio_task(job_id, source, fmt, bitrate, meta, output_path=None):
    if _is_cancelled(job_id):
        _update_job(job_id, status="cancelled", percent=0.0, finished=time.time(), message="Task cancelled.")
        return

    target_dir = _sanitize_output_path(output_path)
    _update_job(job_id, status="downloading", message=f"Ripping audio to {fmt.upper()} ({bitrate})...")
    try:
        res = rip_and_tag_audio(
            source,
            output_path=target_dir,
            target_format=fmt,
            bitrate=bitrate,
            title=(meta or {}).get("title"),
            artist=(meta or {}).get("artist"),
            album=(meta or {}).get("album"),
        )
        _update_job(job_id, status="done", percent=100.0, finished=time.time(),
                    title=res.get("title"),
                    message=res.get("message", f"Audio saved successfully to {target_dir}."))
    except Exception as e:
        _update_job(job_id, status="error", error=str(e), finished=time.time(), message=str(e))


def _run_subtitles_task(job_id, url, lang, output_path=None):
    if _is_cancelled(job_id):
        _update_job(job_id, status="cancelled", percent=0.0, finished=time.time(), message="Task cancelled.")
        return

    target_dir = _sanitize_output_path(output_path)
    _update_job(job_id, status="downloading", message="Extracting & converting subtitles to .srt...")
    try:
        res = extract_subtitles(url, lang=lang, output_path=target_dir)
        _update_job(job_id, status="done", percent=100.0, finished=time.time(),
                    message=res.get("message", f"Subtitles saved successfully to {target_dir}."))
    except Exception as e:
        _update_job(job_id, status="error", error=str(e), finished=time.time(), message=str(e))



# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/preset_paths', methods=['GET'])
def api_preset_paths():
    """Return standard Windows preset paths for download destination."""
    home = os.path.expanduser("~")
    downloads = os.path.join(home, "Downloads")
    onedrive_desktop = os.path.join(home, "OneDrive", "Desktop")
    if os.path.isdir(onedrive_desktop):
        desktop = onedrive_desktop
    else:
        desktop = os.path.join(home, "Desktop")
    videos = os.path.join(home, "Videos")
    project_downloads = os.path.join(base_dir, "Downloads")
    return jsonify({
        "downloads": downloads,
        "desktop": desktop,
        "videos": videos,
        "project": project_downloads,
        "default": downloads
    })


@app.route('/api/browse_folder', methods=['POST'])
def api_browse_folder():
    """Open native Windows directory selection dialog."""
    data = request.json or {}
    initial = (data.get('current') or '').strip() or DOWNLOADS_DIR
    folder = _choose_folder_dialog(initial)
    if folder:
        return jsonify({"status": "selected", "path": folder})
    return jsonify({"status": "cancelled", "path": initial})


@app.route('/api/download_video', methods=['POST'])
def api_download_video():
    """Start high-speed download with optional aria2c turbo booster and stealth lock bypass."""
    data = request.json or {}
    url = (data.get('url') or '').strip().strip('"').strip("'")
    quality = (data.get('quality') or '4k').lower()
    use_turbo = bool(data.get('use_turbo', True))
    use_stealth = bool(data.get('use_stealth', True))
    output_path = (data.get('output_path') or '').strip()

    if not url:
        return jsonify({"error": "No URL or search query provided"}), 400

    job_id = _new_job(job_type="single")
    EXECUTOR.submit(_run_download_task, job_id, url, quality, use_turbo, output_path, use_stealth)
    return jsonify({"status": "started", "job_id": job_id})


@app.route('/api/search', methods=['POST'])
def api_search():
    """Search videos across YouTube and Open Web with stealth anti-bot protection."""
    data = request.json or {}
    query = (data.get('query') or '').strip()
    count = int(data.get('count') or 20)
    include_web = bool(data.get('include_web', True))
    use_stealth = bool(data.get('use_stealth', True))

    if not query:
        return jsonify({"error": "No search query provided"}), 400

    res = search_media(query, count=count, include_web=include_web, use_stealth=use_stealth)
    results = res.get("results", [])

    if not results:
        msg = res.get("yt_error") or res.get("web_error") or "No matching media found for this query."
        return jsonify({
            "status": "empty",
            "results": [],
            "message": msg,
            "stealth_active": use_stealth,
            "cleaned_query": res.get("cleaned_query"),
            "original_query": query
        }), 200

    return jsonify({
        "status": "success",
        "results": results,
        "platform_counts": res.get("platform_counts", {}),
        "total_found": res.get("total_found", len(results)),
        "stealth_active": use_stealth,
        "cleaned_query": res.get("cleaned_query"),
        "original_query": query
    })


@app.route('/api/resolve_imdb', methods=['GET'])
def api_resolve_imdb():
    """Server-side IMDb lookup proxy to avoid browser client CORS blocks."""
    title = (request.args.get('title') or '').strip()
    if not title:
        return jsonify({"error": "No title provided"}), 400
    from engines.search import search_imdb
    items = search_imdb(title, count=1)
    if items:
        return jsonify({
            "status": "success",
            "imdb_id": items[0].get("imdb_id") or items[0].get("id"),
            "title": items[0].get("title"),
            "year": items[0].get("year"),
            "cast": items[0].get("cast"),
            "thumbnail": items[0].get("thumbnail"),
        })
    return jsonify({"status": "not_found", "message": "No matching IMDb entry found"}), 404



@app.route('/api/download_selected', methods=['POST'])
def api_download_selected():
    """Download item from search results with turbo booster and stealth lock bypass."""
    data = request.json or {}
    url = (data.get('url') or '').strip()
    quality = (data.get('quality') or '4k').lower()
    use_turbo = bool(data.get('use_turbo', True))
    use_stealth = bool(data.get('use_stealth', True))
    output_path = (data.get('output_path') or '').strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = _new_job(job_type="search_item")
    EXECUTOR.submit(_run_download_task, job_id, url, quality, use_turbo, output_path, use_stealth)
    return jsonify({"status": "started", "job_id": job_id})


@app.route('/api/playlist/inspect', methods=['POST'])
def api_playlist_inspect():
    """Inspect full playlist or channel items without downloading."""
    data = request.json or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({"error": "Please provide a playlist or channel URL."}), 400

    try:
        meta = inspect_playlist(url)
        return jsonify({
            "status": "success",
            "data": meta,
            "title": meta.get("title"),
            "total_items": meta.get("total_items"),
            "uploader": meta.get("uploader"),
            "entries": meta.get("entries")
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/playlist/download', methods=['POST'])
def api_playlist_download():
    """Queue multiple playlist entries or batch links with stealth protection."""
    data = request.json or {}
    urls = data.get('urls') or []
    quality = (data.get('quality') or '1080p').lower()
    use_turbo = bool(data.get('use_turbo', True))
    use_stealth = bool(data.get('use_stealth', True))
    output_path = (data.get('output_path') or '').strip()

    if not urls:
        return jsonify({"error": "No URLs provided for batch download."}), 400

    job_ids = []
    for u in urls:
        if u and (u.startswith("http://") or u.startswith("https://")):
            jid = _new_job(job_type="batch_item")
            EXECUTOR.submit(_run_download_task, jid, u, quality, use_turbo, output_path, use_stealth)
            job_ids.append(jid)

    return jsonify({"status": "queued", "count": len(job_ids), "job_ids": job_ids})


@app.route('/api/social/download', methods=['POST'])
def api_social_download():
    """Scrape social media posts/galleries via gallery-dl."""
    data = request.json or {}
    url = (data.get('url') or '').strip()
    output_path = (data.get('output_path') or '').strip()
    if not url:
        return jsonify({"error": "Please provide a social media URL."}), 400

    job_id = _new_job(job_type="social_scrape")
    EXECUTOR.submit(_run_social_task, job_id, url, output_path)
    return jsonify({"status": "started", "job_id": job_id})


@app.route('/api/audio/rip', methods=['POST'])
def api_audio_rip():
    """Rip and tag audio from video links or files."""
    data = request.json or {}
    source = (data.get('source') or '').strip()
    fmt = (data.get('format') or 'mp3').lower()
    bitrate = (data.get('bitrate') or '320k').lower()
    meta = data.get('metadata') or {}
    output_path = (data.get('output_path') or '').strip()

    if not source:
        return jsonify({"error": "Please provide a source URL or file path."}), 400

    job_id = _new_job(job_type="audio_rip")
    EXECUTOR.submit(_run_audio_task, job_id, source, fmt, bitrate, meta, output_path)
    return jsonify({"status": "started", "job_id": job_id})


@app.route('/api/download_subtitle', methods=['POST'])
def api_download_subtitle():
    """Download clean .srt subtitles asynchronously."""
    data = request.json or {}
    input_str = (data.get('input_str') or data.get('url') or '').strip().strip('"').strip("'")
    lang = (data.get('lang') or 'en').strip()
    output_path = (data.get('output_path') or '').strip()

    if not input_str:
        return jsonify({"error": "No input link provided"}), 400

    job_id = _new_job(job_type="subtitle")
    EXECUTOR.submit(_run_subtitles_task, job_id, input_str, lang, output_path)
    return jsonify({"status": "started", "job_id": job_id, "message": "Subtitle extraction in progress."})


@app.route('/api/library', methods=['GET'])
def api_library():
    """List recent downloads in the user's Downloads directory."""
    target_dir = _sanitize_output_path(request.args.get('dir'))
    try:
        items = list_downloads(target_dir, limit=50)
        return jsonify({
            "status": "success",
            "downloads_dir": target_dir,
            "directory": target_dir,
            "items": items,
            "files": items
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/library/open', methods=['POST'])
def api_library_open():
    """Open a downloaded file or launch Explorer."""
    data = request.json or {}
    filepath = data.get('path') or data.get('file_path') or DOWNLOADS_DIR
    try:
        open_downloaded_file(filepath)
        return jsonify({"status": "opened", "path": filepath})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/library/reveal', methods=['POST'])
def api_library_reveal():
    """Highlight and select the file in Windows File Explorer."""
    data = request.json or {}
    filepath = data.get('path') or data.get('file_path')
    if not filepath:
        return jsonify({"error": "No file path provided"}), 400
    try:
        reveal_in_explorer(filepath)
        return jsonify({"status": "revealed", "path": filepath})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/progress/<job_id>', methods=['GET'])
def api_progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job id"}), 404
        return jsonify(dict(job))


@app.route('/api/cancel/<job_id>', methods=['POST'])
def api_cancel(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job id"}), 404
        if job["status"] in ("done", "error", "cancelled"):
            return jsonify({"status": job["status"], "message": "Job already finished."})
        job["cancel"] = True
    return jsonify({"status": "cancelling"})


@app.route('/api/open_folder', methods=['POST'])
def api_open_folder():
    """Open Downloads directory or chosen path."""
    data = request.json or {}
    folder = data.get('path') or DOWNLOADS_DIR
    if not os.path.isdir(folder):
        folder = DOWNLOADS_DIR
    try:
        open_downloaded_file(folder)
        return jsonify({"status": "opened", "path": folder})
    except Exception as e:
        return jsonify({"error": str(e), "path": folder}), 500



@app.route('/api/health', methods=['GET'])
def api_health():
    """Report readiness of all underlying open-source engines."""
    return jsonify({
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "aria2c": check_aria2c_installed(),
        "gallery_dl": True,
        "yt_dlp": True,
        "downloads_dir": DOWNLOADS_DIR,
    })


if __name__ == '__main__':
    print("=" * 65)
    print("  QUIDIAN — Media Downloader")
    print("  Developed by Kamran Ashraf")
    print("  Running at http://127.0.0.1:5050")
    print("=" * 65)
    app.run(host='127.0.0.1', port=5050, debug=False, threaded=True)
