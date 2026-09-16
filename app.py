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
import string
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

base_dir = os.path.dirname(os.path.abspath(__file__))

# Ensure safe stdout and stderr when executed via pythonw or without a console window
if sys.stdout is None:
    try:
        log_file = os.path.join(base_dir, "server_stdout.log")
        sys.stdout = open(log_file, "a", encoding="utf-8", buffering=1)
    except Exception:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    try:
        err_file = os.path.join(base_dir, "server_stderr.log")
        sys.stderr = open(err_file, "a", encoding="utf-8", buffering=1)
    except Exception:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

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
    JobStore,
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


def _is_local_or_lan_host(host):
    if not host or host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return True
    # Allow private LAN IPv4 (192.168.x.x, 10.x.x.x, 172.16-31.x.x)
    return bool(re.match(r'^(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})$', host))


@app.before_request
def _guard_origin():
    """Block DNS-rebinding and drive-by cross-site calls into the local API.

    Allows loopback and private LAN addresses, while rejecting foreign domains
    and cross-origin requests from outside the host.
    """
    host = (request.host or "").split(":")[0].strip("[]").lower()
    if not _is_local_or_lan_host(host):
        return jsonify({"error": "Invalid Host header for local service."}), 403
    origin = request.headers.get("Origin")
    if origin:
        port = request.host.split(":")[-1] if ":" in request.host else "80"
        allowed = {
            f"http://127.0.0.1:{port}", f"http://localhost:{port}",
            f"http://{request.host}", f"https://{request.host}"
        }
        if origin not in allowed:
            return jsonify({"error": "Cross-origin requests are not permitted."}), 403
    return None


# ---------------------------------------------------------------------------
# Folder picker
# ---------------------------------------------------------------------------
_DIALOG_LOCK = threading.Lock()


def _choose_folder_dialog(initial_dir=None):
    """Launch the native directory dialog without freezing Flask."""
    init = initial_dir if (initial_dir and os.path.isdir(initial_dir)) else DOWNLOADS_DIR
    if not _DIALOG_LOCK.acquire(blocking=False):
        return None
    try:
        if os.name == "nt":
            safe_init = init.replace("'", "''")
            ps_script = (
                "[System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null; "
                "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
                "$d.Description = 'Select Download Destination Folder'; "
                f"$d.SelectedPath = '{safe_init}'; "
                "$d.ShowNewFolderButton = $true; "
                "$top = New-Object System.Windows.Forms.Form; "
                "$top.TopMost = $true; "
                "$top.StartPosition = 'CenterScreen'; "
                "$top.Size = New-Object System.Drawing.Size(1, 1); "
                "$top.Opacity = 0; "
                "$top.Show(); "
                "$top.BringToFront(); "
                "$top.Activate(); "
                "$res = $d.ShowDialog($top); "
                "$top.Close(); "
                "if ($res -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }"
            )
            try:
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-STA", "-Command", ps_script],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=25,
                )
                folder = (proc.stdout or "").strip()
                if folder and os.path.isdir(folder):
                    return os.path.normpath(folder)
            except Exception as ps_err:
                app.logger.warning("PowerShell folder picker error: %s", ps_err)

        # Fallback: Tkinter askdirectory
        code = (
            "import tkinter as tk; from tkinter import filedialog; "
            "root = tk.Tk(); root.withdraw(); root.wm_attributes('-topmost', 1); "
            f"p = filedialog.askdirectory(initialdir={init!r}, title='Select Download Destination Folder'); "
            "print(p if p else '')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=25,
        )
        folder = (proc.stdout or "").strip()
        if folder and os.path.isdir(folder):
            return os.path.normpath(folder)
    except Exception as e:
        app.logger.warning("Folder picker error: %s", e)
    finally:
        _DIALOG_LOCK.release()
    return None


def _search_directories(query=None):
    """
    Fast live directory search across Windows drives, standard user folders, and subdirectories.
    Returns a list of dicts: {"name": ..., "path": ..., "exists": bool, "can_create": bool}
    """
    home = os.path.expanduser("~")
    base_places = [
        os.path.join(home, "Downloads"),
        os.path.join(home, "Desktop"),
        os.path.join(home, "Videos"),
        os.path.join(home, "Documents"),
        os.path.join(home, "Music"),
        DOWNLOADS_DIR,
    ]
    for d in string.ascii_uppercase:
        drv = f"{d}:\\"
        if os.path.exists(drv):
            base_places.append(drv)

    q = (query or "").strip().strip('"').strip("'")
    results = []
    seen = set()

    def add_result(p, label=None, exists=True, can_create=False):
        norm = os.path.normpath(p)
        k = norm.lower()
        if k not in seen:
            seen.add(k)
            name = label or os.path.basename(norm) or norm
            results.append({"name": name, "path": norm, "exists": exists, "can_create": can_create})

    if not q:
        for p in base_places:
            norm = os.path.normpath(p)
            if os.path.isdir(norm):
                add_result(norm)
        return results

    q_norm = os.path.expanduser(q)

    # 1. Exact directory match: list its children
    if os.path.isdir(q_norm):
        add_result(q_norm, f"{os.path.basename(q_norm) or q_norm} (Current Folder)")
        try:
            with os.scandir(q_norm) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(('.', '$')):
                        add_result(entry.path)
                        if len(results) >= 20:
                            break
        except Exception:
            pass
        return results

    # 2. Parent directory exists, match subdirectories by prefix
    parent = os.path.dirname(q_norm)
    child_prefix = os.path.basename(q_norm).lower()
    if parent and os.path.isdir(parent):
        try:
            with os.scandir(parent) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(('.', '$')):
                        if child_prefix in entry.name.lower():
                            add_result(entry.path)
                            if len(results) >= 20:
                                break
        except Exception:
            pass
        if results:
            return results

    # 3. Fuzzy search keyword in standard user places
    needle = q.lower()
    for root_dir in base_places:
        if not os.path.isdir(root_dir):
            continue
        if needle in os.path.basename(root_dir).lower():
            add_result(root_dir)
        try:
            with os.scandir(root_dir) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(('.', '$')):
                        if needle in entry.name.lower():
                            add_result(entry.path)
                            if len(results) >= 20:
                                break
        except Exception:
            pass

    # 4. If path looks like a new folder path, offer creation
    if len(q) >= 3 and (":\\" in q or ":/" in q or q.startswith("\\\\")):
        parent_dir = os.path.dirname(q_norm)
        if os.path.isdir(parent_dir):
            add_result(q_norm, f"Create new folder: {os.path.basename(q_norm)}", exists=False, can_create=True)

    return results


# ---------------------------------------------------------------------------
# Persistent job registry backed by SQLite with in-memory caching
# ---------------------------------------------------------------------------
JOB_STORE = JobStore()
JOBS = JOB_STORE._cache
JOBS_LOCK = JOB_STORE._lock
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
    JOB_STORE.prune_stale_jobs(ttl_seconds=JOB_TTL_SECONDS, max_records=JOB_MAX_RECORDS)


# Staging directories this app creates under the system temp dir. A crash or a
# hard kill leaves these behind, and they can hold gigabytes.
_STAGING_PREFIXES = ("studio_stage_", "studio_chunks_", "course_stage_",
                     "course_chunks_", "audio_stage_", "audio_temp_",
                     "sub_stage_", "sub_temp_")
_STAGING_MAX_AGE = 6 * 3600


def sweep_orphan_staging(max_age=_STAGING_MAX_AGE):
    """Reclaim staging directories orphaned by a previous crashed run.

    Only this app's own distinctively-prefixed directories are touched, and
    only once they are older than `max_age`, so a concurrently running instance
    is never disturbed. Returns the number of directories removed.
    """
    import tempfile
    root = tempfile.gettempdir()
    now = time.time()
    removed = 0
    try:
        entries = os.listdir(root)
    except OSError:
        return 0
    for name in entries:
        if not name.startswith(_STAGING_PREFIXES):
            continue
        path = os.path.join(root, name)
        try:
            if not os.path.isdir(path):
                continue
            if now - os.path.getmtime(path) < max_age:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += not os.path.exists(path)
        except OSError:
            continue
    return removed


_STOP_PRUNE = threading.Event()


def _periodic_prune():
    while not _STOP_PRUNE.wait(600):
        try:
            with JOBS_LOCK:
                _prune_jobs()
            sweep_orphan_staging()
        except Exception as e:
            app.logger.warning("Periodic prune warning: %s", e)


threading.Thread(target=_periodic_prune, name="quidian-prune", daemon=True).start()

try:
    _recovered = JOB_STORE.recover_interrupted_jobs(DOWNLOADS_DIR)
    if _recovered:
        print(f"[Quidian] Auto-recovered {_recovered} completed/interrupted download(s) from previous session.")
except Exception as e:
    app.logger.warning("Job recovery note: %s", e)

try:
    _reclaimed = sweep_orphan_staging()
    if _reclaimed:
        print(f"[Quidian] Reclaimed {_reclaimed} orphaned staging folder(s) from a previous run.")
except Exception:
    pass


def _new_job(job_type="download"):
    job_id = uuid.uuid4().hex[:12]
    _prune_jobs()
    JOB_STORE.create_job(job_id, job_type=job_type)
    return job_id


def _update_job(job_id, **kwargs):
    JOB_STORE.update_job(job_id, force=False, **kwargs)


def _is_cancelled(job_id):
    j = JOB_STORE.get_job(job_id)
    return bool(j and j.get("cancel"))


def _force_job(job_id, **kwargs):
    """Terminal state write that bypasses the finished-job guard."""
    JOB_STORE.update_job(job_id, force=True, **kwargs)


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
        fpaths = [d.get("filepath") for d in downloads if isinstance(d, dict) and d.get("filepath")]
        if not fpaths and info.get("filepath"):
            fpaths = [info.get("filepath")]
        fpath = fpaths[0] if fpaths else None
        fname = os.path.basename(fpath) if fpath else None
        with JOBS_LOCK:
            prev_title = JOBS.get(job_id, {}).get("title")
        final_title = title or prev_title or (os.path.splitext(fname)[0] if fname else "Media")

        if len(fpaths) > 1:
            display_names = [os.path.basename(p) for p in fpaths[:3]]
            msg = f"Saved {len(fpaths)} video(s): {', '.join(display_names)}{'...' if len(fpaths) > 3 else ''}"
        else:
            msg = f"Saved: {fname}" if fname else f"Saved to {target_dir}"

        _update_job(
            job_id,
            status="done",
            percent=100.0,
            title=final_title,
            filepath=fpath,
            filename=fname,
            downloaded_files=fpaths,
            finished=time.time(),
            message=msg,
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


def _list_directories(target_dir=None):
    """
    List subdirectories within target_dir, available drives, and parent path.
    Enables instant in-browser folder exploration without relying on OS popups.
    """
    home = os.path.expanduser("~")
    drives = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            drv = f"{letter}:\\"
            if os.path.exists(drv):
                drives.append(drv)
    if not drives:
        drives = [os.path.normpath(home)]

    raw_target = _clean_str(target_dir) if target_dir else ""
    if raw_target:
        candidate = os.path.expanduser(raw_target)
        if os.path.isdir(candidate):
            cur = os.path.abspath(candidate)
        elif os.path.isdir(os.path.dirname(candidate)):
            cur = os.path.abspath(os.path.dirname(candidate))
        else:
            cur = DOWNLOADS_DIR if os.path.isdir(DOWNLOADS_DIR) else home
    else:
        cur = DOWNLOADS_DIR if os.path.isdir(DOWNLOADS_DIR) else home

    cur = os.path.normpath(cur)
    parent = os.path.dirname(cur)
    if parent == cur:
        parent = None

    folders = []
    try:
        with os.scandir(cur) as it:
            for entry in it:
                try:
                    name = entry.name
                    if name.startswith((".", "$")):
                        continue
                    if name.lower() in ("system volume information", "recovery", "$recycle.bin"):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        folders.append({
                            "name": name,
                            "path": os.path.normpath(entry.path)
                        })
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError) as e:
        app.logger.warning("Directory scan error for %s: %s", cur, e)

    folders.sort(key=lambda x: x["name"].lower())

    return {
        "status": "ok",
        "current": cur,
        "parent": os.path.normpath(parent) if parent else None,
        "drives": drives,
        "folders": folders,
        "can_create": True
    }


@app.route('/api/list_dirs', methods=['GET', 'POST'])
def api_list_dirs():
    """Directory navigation endpoint for in-modal visual folder picker."""
    if request.method == 'POST':
        data = _json_body()
        path = data.get('path') or data.get('current')
    else:
        path = request.args.get('path') or request.args.get('current')

    res = _list_directories(path)
    return jsonify(res)


@app.route('/api/browse_folder', methods=['POST'])
def api_browse_folder():
    """Open the native directory selection dialog."""
    data = _json_body()
    initial = _clean_str(data.get('current')) or DOWNLOADS_DIR
    folder = _choose_folder_dialog(initial)
    if folder:
        return jsonify({"status": "selected", "path": folder})
    return jsonify({"status": "cancelled", "path": initial})


@app.route('/api/search_folders', methods=['GET', 'POST'])
def api_search_folders():
    """Live directory search & autocomplete endpoint."""
    if request.method == 'POST':
        data = _json_body()
        query = data.get('query') or data.get('current')
    else:
        query = request.args.get('query') or request.args.get('q')

    results = _search_directories(query)
    return jsonify({"status": "ok", "query": query or "", "results": results})


@app.route('/api/create_folder', methods=['POST'])
def api_create_folder():
    """Create a new folder directly if it doesn't exist."""
    data = _json_body()
    folder_path = _clean_str(data.get('path'))
    if not folder_path:
        return jsonify({"error": "No folder path provided."}), 400

    target = _sanitize_output_path(folder_path, create=True)
    if os.path.isdir(target):
        return jsonify({"status": "created", "path": target})
    return jsonify({"error": "Could not create specified folder."}), 500


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
            "type": items[0].get("type"),
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
    if not _is_http_url(source):
        domain_pattern = r'^(?:www\.)?[a-zA-Z0-9-]+(?:\.[a-zA-Z]{2,})+(?:/.*)?$'
        if re.match(domain_pattern, source):
            source = f"https://{source}"

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
    """Open a downloaded file in the OS default application, either by direct path or by name."""
    data = _json_body()
    raw = data.get('path') or data.get('file_path') or data.get('name') or data.get('filename') or DOWNLOADS_DIR
    filepath = _safe_local_path(raw)

    # If exact path didn't resolve directly, search by name in downloads folders
    if not filepath:
        search_term = str(raw or "").strip()
        if search_term and search_term != DOWNLOADS_DIR:
            target_dirs = [DOWNLOADS_DIR, os.path.join(base_dir, "Downloads")]
            custom_dest = data.get('destination') or data.get('dir')
            if custom_dest and os.path.isdir(custom_dest):
                target_dirs.insert(0, custom_dest)

            clean_term = re.sub(r'[\\/*?:"<>|]', "", search_term).lower().strip()
            tokens = [t for t in re.split(r'[\s\-_]+', clean_term) if len(t) > 2]

            best_match = None
            best_score = -1
            for search_root in target_dirs:
                if not os.path.isdir(search_root):
                    continue
                def _safe_mtime(fname):
                    try:
                        return os.path.getmtime(os.path.join(root, fname))
                    except (OSError, FileNotFoundError):
                        return 0

                try:
                    for root, _, files in os.walk(search_root):
                        for f in sorted(files, key=_safe_mtime, reverse=True):
                            fl = f.lower()
                            # Check substring match
                            if clean_term and clean_term in fl:
                                best_match = os.path.join(root, f)
                                best_score = 100
                                break
                            # Token match
                            if tokens:
                                matched = sum(1 for t in tokens if t in fl)
                                if matched > best_score and matched >= max(1, len(tokens) // 2):
                                    best_score = matched
                                    best_match = os.path.join(root, f)
                        if best_score == 100:
                            break
                except Exception:
                    pass
                if best_match and best_score >= 100:
                    break

            if best_match:
                filepath = _safe_local_path(best_match)

    if not filepath:
        return jsonify({"error": f"File '{raw}' not found."}), 404
    try:
        open_downloaded_file(filepath)
        return jsonify({"status": "opened", "path": filepath, "filename": os.path.basename(filepath)})
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except OSError as e:
        return jsonify({"error": f"Could not launch file: {e}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/open_by_name', methods=['POST'])
def api_open_by_name():
    """Universal endpoint to find and open downloaded video by search query / name."""
    return api_library_open()


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
    job = JOB_STORE.get_job(job_id)
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
    out = JOB_STORE.get_jobs_bulk(ids[:MAX_BATCH_URLS])
    counts = {}
    for j in out.values():
        key = j.get("status", "unknown")
        counts[key] = counts.get(key, 0) + 1
    return jsonify({"status": "success", "jobs": out, "counts": counts})


@app.route('/api/cancel/<job_id>', methods=['POST'])
def api_cancel(job_id):
    job = JOB_STORE.get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job id"}), 404
    if job.get("status") in ("done", "error", "cancelled"):
        return jsonify({"status": job.get("status"), "message": "Job already finished."})
    JOB_STORE.update_job(job_id, cancel=True, message="Cancelling...")
    if job.get("status") == "queued":
        # Nothing has started yet, so retire it immediately.
        JOB_STORE.update_job(job_id, force=True, status="cancelled", percent=0.0, finished=time.time(), message="Cancelled before start.")
        return jsonify({"status": "cancelled"})
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


@app.route('/api/ping', methods=['GET'])
def api_ping():
    """Zero-overhead ping endpoint for localhost guardian watchdog."""
    return jsonify({
        "status": "ok",
        "app": "quidian_media_downloader",
        "port": _as_int(os.environ.get("QUIDIAN_PORT"), 5050, lo=1, hi=65535),
        "timestamp": time.time()
    })


@app.route('/api/status', methods=['GET'])
def api_status():
    """Universal status endpoint matching launch_quidian_media.vbs and monitors."""
    return jsonify({
        "status": "ok",
        "app": "quidian_media_downloader",
        "ready": True,
        "timestamp": time.time()
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
    host = os.environ.get("QUIDIAN_HOST", "0.0.0.0")
    print("=" * 65)
    print("  QUIDIAN - Media Downloader (Production Engine)")
    print("  Developed by Kamran Ashraf")
    print(f"  Listening on http://127.0.0.1:{port} and http://localhost:{port}")
    print("=" * 65)
    try:
        from waitress import serve
        threads = max(4, min(16, int(os.environ.get("QUIDIAN_SERVER_THREADS") or 8)))
        print(f"  [Waitress] Starting robust production WSGI server with {threads} threads...")
        serve(app, host=host, port=port, threads=threads, channel_timeout=120, cleanup_interval=30)
    except ImportError:
        print("  [Fallback] Waitress not installed, falling back to threaded Werkzeug...")
        app.run(host=host, port=port, debug=False, threaded=True)
