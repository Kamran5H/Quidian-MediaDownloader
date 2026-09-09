"""
Quidian - Media Downloader
Central Flask orchestrator and REST API.
Developed by Kamran Ashraf
"""

import warnings
warnings.filterwarnings("ignore")
warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", message=".*duckduckgo_search.*")

import atexit
import importlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

base_dir = os.path.dirname(os.path.abspath(__file__))

from engines import (
    download_media,
    check_aria2c_installed,
    check_gallery_dl_installed,
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

# Re-exported for callers that import them from `app` (scripts, tests).
__all__ = ["app", "download_batch", "search_videos", "search_web"]
import yt_dlp

DownloadCancelled = getattr(yt_dlp.utils, "DownloadCancelled", None)

app = Flask(__name__)
# Reject oversized request bodies outright (batch lists are the only large payload).
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

DOWNLOADS_DIR = os.path.join(os.path.expanduser("~"), "Downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Tunables (env-overridable so the app can be sized to the machine it runs on).
MAX_WORKERS = max(1, min(8, int(os.environ.get("QUIDIAN_WORKERS") or 3)))
MAX_BATCH_URLS = max(1, min(500, int(os.environ.get("QUIDIAN_MAX_BATCH") or 250)))
JOB_TTL_SECONDS = 30 * 60
JOB_MAX_RECORDS = 2000

VALID_QUALITIES = ("best", "4k", "1080p", "720p", "audio")
VALID_AUDIO_FORMATS = ("mp3", "flac", "aac", "wav", "m4a")
VALID_BITRATES = ("320k", "256k", "192k", "128k", "96k")
_LANG_RE = re.compile(r"^[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{2,8})*$")


# ---------------------------------------------------------------------------
# Request / response plumbing
# ---------------------------------------------------------------------------
def _json_body():
    """Always return a dict.

    ``request.json`` raises 415 when the client omits the JSON content type,
    which turned every malformed request into an HTML error page the front end
    could not parse. Be permissive on input, strict on output.
    """
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    if request.form:
        return request.form.to_dict()
    return {}


def _as_int(value, default, lo=None, hi=None):
    """Coerce user input to int without ever raising."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _as_bool(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _clean_str(value, limit=4096):
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "").strip().strip('"').strip("'")[:limit]


def _is_http_url(value):
    return isinstance(value, str) and value.lower().startswith(("http://", "https://"))


@app.errorhandler(HTTPException)
def _handle_http_error(e):
    """Every API failure must be JSON - the UI only ever parses JSON."""
    if request.path.startswith("/api/"):
        return jsonify({"error": e.description or e.name, "status": "error", "code": e.code}), e.code
    return e


@app.errorhandler(Exception)
def _handle_unexpected_error(e):
    app.logger.exception("Unhandled error on %s", request.path)
    if request.path.startswith("/api/"):
        return jsonify({"error": f"Internal error: {e}", "status": "error", "code": 500}), 500
    raise e


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    if request.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.before_request
def _guard_origin():
    """Block DNS-rebinding and drive-by cross-site calls into the local API.

    The server binds to loopback, but a hostile page can still point its own
    domain at 127.0.0.1. Requiring a loopback Host header closes that hole, and
    rejecting foreign Origins stops drive-by POSTs from any browser tab.
    """
    host = (request.host or "").split(":")[0].strip("[]").lower()
    if host not in ("127.0.0.1", "localhost", "::1", ""):
        return jsonify({"error": "Invalid Host header for local service."}), 403
    origin = request.headers.get("Origin")
    if origin:
        port = request.host.split(":")[-1] if ":" in request.host else "80"
        allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        if origin not in allowed:
            return jsonify({"error": "Cross-origin requests are not permitted."}), 403
    return None


# ---------------------------------------------------------------------------
# Folder picker
# ---------------------------------------------------------------------------
_DIALOG_LOCK = threading.Lock()


def _choose_folder_dialog(initial_dir=None):
    """Launch the native directory dialog out-of-process so Flask keeps serving."""
    init = initial_dir if (initial_dir and os.path.isdir(initial_dir)) else DOWNLOADS_DIR
    code = (
        "import tkinter as tk; from tkinter import filedialog; "
        "root = tk.Tk(); root.withdraw(); root.wm_attributes('-topmost', 1); "
        f"p = filedialog.askdirectory(initialdir={init!r}, title='Select Download Destination Folder'); "
        "print(p if p else '')"
    )
    # Only ever one dialog at a time; a second would steal focus and confuse.
    if not _DIALOG_LOCK.acquire(blocking=False):
        return None
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        folder = (proc.stdout or "").strip()
        if folder and os.path.isdir(folder):
            return os.path.normpath(folder)
    except Exception as e:
        app.logger.warning("Folder picker error: %s", e)
    finally:
        _DIALOG_LOCK.release()
    return None


# ---------------------------------------------------------------------------
# In-memory job registry for non-blocking multi-engine execution
# ---------------------------------------------------------------------------
JOBS = {}
JOBS_LOCK = threading.RLock()
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="quidian")


class _Cancelled(Exception):
    """Internal cancellation signal."""


def _sanitize_output_path(path, create=True):
    """Normalize/validate a download directory, falling back to user Downloads.

    ``create=False`` is used by read-only endpoints so a mere GET can never
    materialise directories on disk.
    """
    if not path or not isinstance(path, str):
        return DOWNLOADS_DIR
    clean = path.strip().strip('"').strip("'")
    if not clean or "\x00" in clean:
        return DOWNLOADS_DIR
    try:
        norm = os.path.normpath(os.path.abspath(clean))
        if os.path.isdir(norm):
            return norm
        if not create:
            return DOWNLOADS_DIR
        os.makedirs(norm, exist_ok=True)
        return norm
    except Exception:
        return DOWNLOADS_DIR


def _safe_local_path(path):
    """Resolve a client-supplied path to an existing file/dir, or return None."""
    if not isinstance(path, str):
        return None
    clean = path.strip().strip('"').strip("'")
    if not clean or "\x00" in clean:
        return None
    try:
        norm = os.path.realpath(os.path.normpath(os.path.abspath(clean)))
    except Exception:
        return None
    return norm if os.path.exists(norm) else None


def _prune_jobs():
    """Drop stale jobs so a long session cannot grow the registry without bound."""
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

    # Hard ceiling: if a huge batch outruns the TTL, evict the oldest finished jobs.
    if len(JOBS) > JOB_MAX_RECORDS:
        finished = sorted(
            (j for j in JOBS.values() if j.get("status") in ("done", "error", "cancelled")),
            key=lambda j: j.get("finished", j.get("created", 0)),
        )
        for j in finished[: len(JOBS) - JOB_MAX_RECORDS]:
            JOBS.pop(j["id"], None)


_STOP_PRUNE = threading.Event()


def _periodic_prune():
    while not _STOP_PRUNE.wait(600):
        try:
            with JOBS_LOCK:
                _prune_jobs()
        except Exception as e:
            app.logger.warning("Periodic prune warning: %s", e)


threading.Thread(target=_periodic_prune, name="quidian-prune", daemon=True).start()


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
        job = JOBS.get(job_id)
        if job is None:
            return
        # A finished job must never be resurrected by a late hook from a
        # worker thread that has not noticed the cancellation yet.
        if job.get("status") in ("done", "error", "cancelled"):
            return
        job.update(kwargs)


def _is_cancelled(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        return bool(j and j.get("cancel"))


def _force_job(job_id, **kwargs):
    """Terminal state write that bypasses the finished-job guard."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job.update(kwargs)


def _finish_cancelled(job_id, message="Task cancelled."):
    _force_job(job_id, status="cancelled", percent=0.0, finished=time.time(), message=message, cancel=True)


def _fail(job_id, exc):
    _force_job(job_id, status="error", error=str(exc), finished=time.time(), message=str(exc))


def _raise_cancel():
    raise (DownloadCancelled() if DownloadCancelled else _Cancelled())


def _make_progress_hook(job_id):
    max_pct = [0.0]

    def hook(d):
        if _is_cancelled(job_id):
            _raise_cancel()
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

            with JOBS_LOCK:
                prev_title = JOBS.get(job_id, {}).get("title")
            _update_job(
                job_id,
                status="downloading",
                percent=round(max_pct[0], 1),
                speed=d.get("_speed_str") or (f"{(d.get('speed') or 0) / 1048576:.2f} MiB/s" if d.get("speed") else None),
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
            _raise_cancel()
        if d.get("status") == "started":
            name = d.get("postprocessor") or ""
            if "Merger" in name:
                msg = "Merging video + audio streams..."
            elif "ExtractAudio" in name:
                msg = "Converting audio track..."
            elif "EmbedSubtitle" in name:
                msg = "Embedding subtitles..."
            elif "ThumbnailsConvertor" in name:
                msg = "Preparing cover art..."
            else:
                msg = "Finalizing media..."
            _update_job(job_id, status="processing", percent=98.0, phase="processing", message=msg)

    return hook


def _was_cancelled(job_id, exc):
    """A job counts as cancelled only when the user asked for it - never
    because an upstream error message happened to contain the word 'cancel'."""
    if DownloadCancelled and isinstance(exc, DownloadCancelled):
        return True
    if isinstance(exc, _Cancelled):
        return True
    return _is_cancelled(job_id)


# ---------------------------------------------------------------------------
# Background task bodies
# ---------------------------------------------------------------------------
def _run_download_task(job_id, url, quality, use_turbo, output_path=None, use_stealth=True):
    if _is_cancelled(job_id):
        _finish_cancelled(job_id, "Download cancelled.")
        return

    target_dir = _sanitize_output_path(output_path)
    _update_job(job_id, status="downloading", message="Starting stream extraction...")
    try:
        def status_cb(msg, pct=None):
            if _is_cancelled(job_id):
                _raise_cancel()
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
            cancel_check=lambda: _is_cancelled(job_id),
        )
        if _is_cancelled(job_id):
            _finish_cancelled(job_id, "Download cancelled.")
            return

        info = info if isinstance(info, dict) else {}
        title = info.get("title")
        downloads = info.get("requested_downloads") or []
        fpath = downloads[0].get("filepath") if downloads else info.get("filepath")
        fname = os.path.basename(fpath) if fpath else None
        with JOBS_LOCK:
            prev_title = JOBS.get(job_id, {}).get("title")
        final_title = title or prev_title or (os.path.splitext(fname)[0] if fname else "Media")
        _update_job(
            job_id,
            status="done",
            percent=100.0,
            title=final_title,
            filepath=fpath,
            filename=fname,
            finished=time.time(),
            message=f"Saved: {fname}" if fname else f"Saved to {target_dir}",
        )
    except Exception as e:
        if _was_cancelled(job_id, e):
            _finish_cancelled(job_id, "Download cancelled.")
            return
        err_msg = str(e)
        if "DRM_PROTECTED|" in err_msg:
            parts = err_msg.split("|")
            platform = parts[1] if len(parts) > 1 else "Protected Platform"
            suggested_title = parts[2] if len(parts) > 2 else ""
            clean_msg = parts[3] if len(parts) > 3 else err_msg
            _force_job(
                job_id,
                status="error",
                error_type="drm_protected",
                platform=platform,
                suggested_title=suggested_title,
                error=clean_msg,
                finished=time.time(),
                message=clean_msg,
            )
        else:
            _fail(job_id, e)


def _run_social_task(job_id, url, output_path=None):
    if _is_cancelled(job_id):
        _finish_cancelled(job_id)
        return

    target_dir = _sanitize_output_path(output_path)
    _update_job(job_id, status="downloading", message="Scraping social media via gallery-dl...")
    try:
        count_seen = [0]

        def on_item(fname):
            count_seen[0] += 1
            _update_job(job_id, message=f"Saved {count_seen[0]}: {fname}")

        res = download_social_gallery(
            url,
            output_path=target_dir,
            progress_callback=on_item,
            cancel_check=lambda: _is_cancelled(job_id),
        )
        if _is_cancelled(job_id):
            _finish_cancelled(job_id)
            return
        count = res.get("count", 0)
        _update_job(
            job_id,
            status="done",
            percent=100.0,
            finished=time.time(),
            message=f"Scraped {count} item(s) successfully into {target_dir}.",
        )
    except Exception as e:
        if _was_cancelled(job_id, e):
            _finish_cancelled(job_id)
        else:
            _fail(job_id, e)


def _run_audio_task(job_id, source, fmt, bitrate, meta, output_path=None):
    if _is_cancelled(job_id):
        _finish_cancelled(job_id)
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
            cancel_check=lambda: _is_cancelled(job_id),
        )
        if _is_cancelled(job_id):
            _finish_cancelled(job_id)
            return
        saved = res.get("file")
        _update_job(
            job_id,
            status="done",
            percent=100.0,
            finished=time.time(),
            title=res.get("title"),
            filepath=saved,
            filename=os.path.basename(saved) if saved else None,
            message=res.get("message", f"Audio saved successfully to {target_dir}."),
        )
    except Exception as e:
        if _was_cancelled(job_id, e):
            _finish_cancelled(job_id)
        else:
            _fail(job_id, e)


def _run_subtitles_task(job_id, url, lang, output_path=None):
    if _is_cancelled(job_id):
        _finish_cancelled(job_id)
        return

    target_dir = _sanitize_output_path(output_path)
    _update_job(job_id, status="downloading", message="Extracting & converting subtitles to .srt...")
    try:
        res = extract_subtitles(url, lang=lang, output_path=target_dir)
        if _is_cancelled(job_id):
            _finish_cancelled(job_id)
            return
        _update_job(
            job_id,
            status="done",
            percent=100.0,
            finished=time.time(),
            title=res.get("title"),
            message=res.get("message", f"Subtitles saved successfully to {target_dir}."),
        )
    except Exception as e:
        if _was_cancelled(job_id, e):
            _finish_cancelled(job_id)
        else:
            _fail(job_id, e)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/preset_paths', methods=['GET'])
def api_preset_paths():
    """Return standard preset paths for the download destination picker."""
    home = os.path.expanduser("~")
    downloads = os.path.join(home, "Downloads")
    onedrive_desktop = os.path.join(home, "OneDrive", "Desktop")
    desktop = onedrive_desktop if os.path.isdir(onedrive_desktop) else os.path.join(home, "Desktop")
    videos = os.path.join(home, "Videos")
    project_downloads = os.path.join(base_dir, "Downloads")
    return jsonify({
        "downloads": downloads,
        "desktop": desktop,
        "videos": videos,
        "project": project_downloads,
        "default": downloads,
    })


@app.route('/api/browse_folder', methods=['POST'])
def api_browse_folder():
    """Open the native directory selection dialog."""
    data = _json_body()
    initial = _clean_str(data.get('current')) or DOWNLOADS_DIR
    folder = _choose_folder_dialog(initial)
    if folder:
        return jsonify({"status": "selected", "path": folder})
    return jsonify({"status": "cancelled", "path": initial})


def _start_download(data, job_type):
    url = _clean_str(data.get('url'))
    if not url:
        return None, (jsonify({"error": "No URL or search query provided"}), 400)

    quality = _clean_str(data.get('quality')).lower() or '4k'
    if quality not in VALID_QUALITIES:
        quality = '4k'
    use_turbo = _as_bool(data.get('use_turbo'), True)
    use_stealth = _as_bool(data.get('use_stealth'), True)
    output_path = _clean_str(data.get('output_path'))

    job_id = _new_job(job_type=job_type)
    EXECUTOR.submit(_run_download_task, job_id, url, quality, use_turbo, output_path, use_stealth)
    return job_id, None


@app.route('/api/download_video', methods=['POST'])
def api_download_video():
    """Start a download with optional aria2c turbo booster and stealth bypass."""
    job_id, err = _start_download(_json_body(), "single")
    if err:
        return err
    return jsonify({"status": "started", "job_id": job_id})


@app.route('/api/download_selected', methods=['POST'])
def api_download_selected():
    """Download an item chosen from search results."""
    job_id, err = _start_download(_json_body(), "search_item")
    if err:
        return err
    return jsonify({"status": "started", "job_id": job_id})


@app.route('/api/search', methods=['POST'])
def api_search():
    """Search across YouTube, Dailymotion, Archive, IMDb and the open web."""
    data = _json_body()
    query = _clean_str(data.get('query'), limit=512)
    count = _as_int(data.get('count'), 20, lo=1, hi=60)
    include_web = _as_bool(data.get('include_web'), True)
    use_stealth = _as_bool(data.get('use_stealth'), True)

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
            "original_query": query,
        }), 200

    return jsonify({
        "status": "success",
        "results": results,
        "platform_counts": res.get("platform_counts", {}),
        "total_found": res.get("total_found", len(results)),
        "stealth_active": use_stealth,
        "cleaned_query": res.get("cleaned_query"),
        "original_query": query,
    })


@app.route('/api/resolve_imdb', methods=['GET'])
def api_resolve_imdb():
    """Server-side IMDb lookup proxy so the browser never hits a CORS wall."""
    title = _clean_str(request.args.get('title'), limit=256)
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


@app.route('/api/playlist/inspect', methods=['POST'])
def api_playlist_inspect():
    """Inspect a playlist or channel without downloading anything."""
    data = _json_body()
    url = _clean_str(data.get('url'))
    if not url:
        return jsonify({"error": "Please provide a playlist or channel URL."}), 400
    if not _is_http_url(url):
        return jsonify({"error": "Playlist inspection requires a full http(s) URL."}), 400

    try:
        meta = inspect_playlist(url)
        return jsonify({
            "status": "success",
            "data": meta,
            "title": meta.get("title"),
            "total_items": meta.get("total_items"),
            "uploader": meta.get("uploader"),
            "entries": meta.get("entries"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route('/api/playlist/download', methods=['POST'])
def api_playlist_download():
    """Queue playlist entries or pasted batch links."""
    data = _json_body()
    urls = data.get('urls') or []
    if not isinstance(urls, list):
        return jsonify({"error": "'urls' must be a list."}), 400

    quality = _clean_str(data.get('quality')).lower() or '1080p'
    if quality not in VALID_QUALITIES:
        quality = '1080p'
    use_turbo = _as_bool(data.get('use_turbo'), True)
    use_stealth = _as_bool(data.get('use_stealth'), True)
    output_path = _clean_str(data.get('output_path'))

    if not urls:
        return jsonify({"error": "No URLs provided for batch download."}), 400

    seen = set()
    job_ids = []
    skipped = max(0, len(urls) - MAX_BATCH_URLS)
    for raw in urls[:MAX_BATCH_URLS]:
        u = _clean_str(raw)
        if not _is_http_url(u) or u in seen:
            skipped += 1
            continue
        seen.add(u)
        jid = _new_job(job_type="batch_item")
        EXECUTOR.submit(_run_download_task, jid, u, quality, use_turbo, output_path, use_stealth)
        job_ids.append(jid)

    if not job_ids:
        return jsonify({"error": "No valid http(s) URLs in the batch."}), 400

    return jsonify({"status": "queued", "count": len(job_ids), "job_ids": job_ids, "skipped": skipped})


@app.route('/api/social/download', methods=['POST'])
def api_social_download():
    """Scrape social posts/galleries via gallery-dl."""
    data = _json_body()
    url = _clean_str(data.get('url'))
    output_path = _clean_str(data.get('output_path'))
    if not url:
        return jsonify({"error": "Please provide a social media URL."}), 400
    if not _is_http_url(url):
        return jsonify({"error": "The social scraper needs a full http(s) URL."}), 400

    job_id = _new_job(job_type="social_scrape")
    EXECUTOR.submit(_run_social_task, job_id, url, output_path)
    return jsonify({"status": "started", "job_id": job_id})


@app.route('/api/audio/rip', methods=['POST'])
def api_audio_rip():
    """Rip and tag audio from a link or a local media file."""
    data = _json_body()
    source = _clean_str(data.get('source'))
    fmt = _clean_str(data.get('format')).lower() or 'mp3'
    bitrate = _clean_str(data.get('bitrate')).lower() or '320k'
    raw_meta = data.get('metadata') or {}
    meta = {k: _clean_str(raw_meta.get(k), limit=256) for k in ("title", "artist", "album")} if isinstance(raw_meta, dict) else {}
    output_path = _clean_str(data.get('output_path'))

    if not source:
        return jsonify({"error": "Please provide a source URL or file path."}), 400
    if fmt not in VALID_AUDIO_FORMATS:
        return jsonify({"error": f"Unsupported audio format '{fmt}'. Choose one of: {', '.join(VALID_AUDIO_FORMATS)}."}), 400
    if bitrate not in VALID_BITRATES:
        bitrate = '320k'
    if not _is_http_url(source) and not _safe_local_path(source):
        return jsonify({"error": "Local source file not found."}), 400

    job_id = _new_job(job_type="audio_rip")
    EXECUTOR.submit(_run_audio_task, job_id, source, fmt, bitrate, meta, output_path)
    return jsonify({"status": "started", "job_id": job_id})


@app.route('/api/download_subtitle', methods=['POST'])
def api_download_subtitle():
    """Download and normalise subtitles into .srt."""
    data = _json_body()
    input_str = _clean_str(data.get('input_str') or data.get('url'))
    lang = _clean_str(data.get('lang'), limit=16) or 'en'
    output_path = _clean_str(data.get('output_path'))

    if not input_str:
        return jsonify({"error": "No input link provided"}), 400
    if not _LANG_RE.match(lang):
        return jsonify({"error": f"Invalid language code '{lang}'."}), 400

    job_id = _new_job(job_type="subtitle")
    EXECUTOR.submit(_run_subtitles_task, job_id, input_str, lang, output_path)
    return jsonify({"status": "started", "job_id": job_id, "message": "Subtitle extraction in progress."})


@app.route('/api/library', methods=['GET'])
def api_library():
    """List recent media in the chosen directory (read-only: never creates it)."""
    target_dir = _sanitize_output_path(request.args.get('dir'), create=False)
    limit = _as_int(request.args.get('limit'), 50, lo=1, hi=500)
    try:
        items = list_downloads(target_dir, limit=limit)
        return jsonify({
            "status": "success",
            "downloads_dir": target_dir,
            "directory": target_dir,
            "items": items,
            "files": items,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/library/open', methods=['POST'])
def api_library_open():
    """Open a downloaded file in the OS default application."""
    data = _json_body()
    raw = data.get('path') or data.get('file_path') or DOWNLOADS_DIR
    filepath = _safe_local_path(raw)
    if not filepath:
        return jsonify({"error": "File not found."}), 404
    try:
        open_downloaded_file(filepath)
        return jsonify({"status": "opened", "path": filepath})
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/library/reveal', methods=['POST'])
def api_library_reveal():
    """Highlight the file in the native file manager."""
    data = _json_body()
    filepath = _safe_local_path(data.get('path') or data.get('file_path'))
    if not filepath:
        return jsonify({"error": "Path not found"}), 404
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
            return jsonify({"error": "Unknown job id", "status": "expired"}), 404
        return jsonify(dict(job))


@app.route('/api/progress_bulk', methods=['POST'])
def api_progress_bulk():
    """Batch progress lookup.

    Polling a 250-item batch one job at a time meant 250 requests per tick;
    this collapses that into a single round trip.
    """
    data = _json_body()
    ids = data.get('job_ids') or []
    if not isinstance(ids, list):
        return jsonify({"error": "'job_ids' must be a list."}), 400
    out = {}
    with JOBS_LOCK:
        for jid in ids[:MAX_BATCH_URLS]:
            if not isinstance(jid, str):
                continue
            job = JOBS.get(jid)
            out[jid] = dict(job) if job else {"id": jid, "status": "expired"}
    counts = {}
    for j in out.values():
        key = j.get("status", "unknown")
        counts[key] = counts.get(key, 0) + 1
    return jsonify({"status": "success", "jobs": out, "counts": counts})


@app.route('/api/cancel/<job_id>', methods=['POST'])
def api_cancel(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job id"}), 404
        if job["status"] in ("done", "error", "cancelled"):
            return jsonify({"status": job["status"], "message": "Job already finished."})
        job["cancel"] = True
        if job["status"] == "queued":
            # Nothing has started yet, so retire it immediately.
            job.update(status="cancelled", percent=0.0, finished=time.time(), message="Cancelled before start.")
            return jsonify({"status": "cancelled"})
        job["message"] = "Cancelling..."
    return jsonify({"status": "cancelling"})


@app.route('/api/open_folder', methods=['POST'])
def api_open_folder():
    """Open a directory in the file manager."""
    data = _json_body()
    folder = _safe_local_path(data.get('path'))
    if not folder or not os.path.isdir(folder):
        folder = DOWNLOADS_DIR
    try:
        open_downloaded_file(folder)
        return jsonify({"status": "opened", "path": folder})
    except Exception as e:
        return jsonify({"error": str(e), "path": folder}), 500


def _playwright_available():
    try:
        importlib.import_module("playwright")
        return True
    except Exception:
        return False


@app.route('/api/health', methods=['GET'])
def api_health():
    """Report the real readiness of every underlying engine."""
    return jsonify({
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "aria2c": check_aria2c_installed(),
        "gallery_dl": check_gallery_dl_installed(),
        "yt_dlp": True,
        "yt_dlp_version": getattr(getattr(yt_dlp, "version", None), "__version__", None),
        "playwright": _playwright_available(),
        "downloads_dir": DOWNLOADS_DIR,
        "workers": MAX_WORKERS,
    })


@atexit.register
def _shutdown():
    _STOP_PRUNE.set()
    try:
        EXECUTOR.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        EXECUTOR.shutdown(wait=False)


if __name__ == '__main__':
    port = _as_int(os.environ.get("QUIDIAN_PORT"), 5050, lo=1, hi=65535)
    print("=" * 65)
    print("  QUIDIAN - Media Downloader")
    print("  Developed by Kamran Ashraf")
    print(f"  Running at http://127.0.0.1:{port}")
    print("=" * 65)
    app.run(host='127.0.0.1', port=port, debug=False, threaded=True)
