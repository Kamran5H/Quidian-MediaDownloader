"""
Quidian - Audio ripper, transcoder and ID3 tagger.
Developed by Kamran Ashraf
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

import yt_dlp
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError

SUPPORTED_FORMATS = ("mp3", "flac", "aac", "wav", "m4a")
SUPPORTED_BITRATES = ("320k", "256k", "192k", "128k", "96k")
# aac is delivered inside an m4a container by both yt-dlp and ffmpeg.
_CONTAINER_FOR = {"aac": "m4a"}
FFMPEG_TIMEOUT = 1800


class AudioCancelled(Exception):
    """Raised when the user cancels an audio job mid-flight."""


def _normalise_format(target_format):
    fmt = (target_format or "mp3").strip().lower()
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(
            f"Unsupported audio format '{target_format}'. Choose one of: {', '.join(SUPPORTED_FORMATS)}."
        )
    return fmt


def _bitrate_kbps(bitrate):
    """'320k' -> '320'. Falls back to 320 rather than silently picking 128."""
    m = re.match(r"^\s*(\d{2,4})\s*k?\s*$", str(bitrate or ""), re.I)
    if not m:
        return "320"
    return str(max(32, min(640, int(m.group(1)))))


def _unique_path(path):
    """Never overwrite an existing file; auto-number (1), (2), ..."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    counter = 1
    while os.path.exists(f"{base} ({counter}){ext}"):
        counter += 1
    return f"{base} ({counter}){ext}"


def _apply_audio_tags(filepath, target_format="mp3", title=None, artist=None, album=None):
    """Embed title/artist/album into MP3, FLAC or MP4-family files."""
    if not (title or artist or album) or not filepath or not os.path.isfile(filepath):
        return False
    fmt = (target_format or "mp3").lower()
    tags = {k: v for k, v in (("title", title), ("artist", artist), ("album", album)) if v}

    try:
        if fmt == "mp3":
            # EasyID3 touches only the tag block. Going through MP3() would
            # additionally parse the MPEG stream and blow up with
            # HeaderNotFoundError on files ffmpeg has not finished flushing.
            try:
                audio = EasyID3(filepath)
            except ID3NoHeaderError:
                from mutagen.id3 import ID3
                ID3().save(filepath)
                audio = EasyID3(filepath)
            for k, v in tags.items():
                audio[k] = v
            audio.save(filepath)
            return True

        if fmt == "flac":
            from mutagen.flac import FLAC
            audio = FLAC(filepath)
            for k, v in tags.items():
                audio[k] = v
            audio.save()
            return True

        if fmt in ("aac", "m4a"):
            from mutagen.mp4 import MP4
            audio = MP4(filepath)
            mapping = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb"}
            for k, v in tags.items():
                audio[mapping[k]] = [v]
            audio.save()
            return True
    except Exception:
        # Tagging is a nicety; a missing tag must never fail the whole rip.
        return False
    return False


def _check_cancel(cancel_check):
    if cancel_check and cancel_check():
        raise AudioCancelled("Audio job cancelled by user.")


def rip_and_tag_audio(source, output_path, target_format="mp3", bitrate="320k",
                      title=None, artist=None, album=None, cancel_check=None):
    """
    Rip audio from a URL, or transcode a local media file, into MP3/FLAC/AAC/WAV
    with ID3 (or equivalent) tagging.
    """
    os.makedirs(output_path, exist_ok=True)
    target_format = _normalise_format(target_format)
    container_ext = _CONTAINER_FOR.get(target_format, target_format)
    quality = _bitrate_kbps(bitrate)
    _check_cancel(cancel_check)

    src_str = str(source or "").strip().strip('"').strip("'")
    if not src_str.lower().startswith(("http://", "https://")):
        domain_pattern = r'^(?:www\.)?[a-zA-Z0-9-]+(?:\.[a-zA-Z]{2,})+(?:/.*)?$'
        if re.match(domain_pattern, src_str):
            src_str = f"https://{src_str}"

    if src_str.lower().startswith(("http://", "https://")):
        return _rip_from_url(src_str, output_path, target_format, container_ext,
                             quality, title, artist, album, cancel_check)
    return _convert_local(src_str, output_path, target_format, container_ext,
                          quality, title, artist, album, cancel_check)


def _rip_from_url(source, output_path, target_format, container_ext, quality,
                  title, artist, album, cancel_check):
    session = uuid.uuid4().hex[:8]
    stage_dir = os.path.join(tempfile.gettempdir(), f"audio_stage_{session}")
    temp_dir = os.path.join(tempfile.gettempdir(), f"audio_temp_{session}")
    os.makedirs(stage_dir, exist_ok=True)
    os.makedirs(temp_dir, exist_ok=True)

    def hook(d):
        _check_cancel(cancel_check)

    postprocessors = [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": target_format,
        # Lossless codecs ignore this; lossy ones now honour the chosen bitrate
        # instead of collapsing to 128k for everything except 320k.
        "preferredquality": quality,
    }]
    if target_format != "wav":
        postprocessors += [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ]

    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        # Staging keeps half-written files out of the user's folder, so a
        # cancelled or failed rip leaves nothing behind.
        "paths": {"home": stage_dir, "temp": temp_dir},
        "outtmpl": {"default": "%(title).180s.%(ext)s"},
        "noplaylist": True,
        "windowsfilenames": True,
        "no_warnings": True,
        "quiet": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 8,
        "fragment_retries": 8,
        "extractor_retries": 3,
        "concurrent_fragment_downloads": 8,
        "postprocessors": postprocessors,
        "writethumbnail": target_format != "wav",
        "progress_hooks": [hook],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(source, download=True)
        _check_cancel(cancel_check)

        saved_title = title or (info or {}).get("title") or "Audio Track"
        # Pick the produced audio file out of staging by extension - far more
        # reliable than guessing prepare_filename()'s pre-postprocessor name.
        wanted = {f".{target_format}", f".{container_ext}"}
        produced = [
            os.path.join(stage_dir, f) for f in os.listdir(stage_dir)
            if os.path.splitext(f)[1].lower() in wanted
        ]
        if not produced:
            produced = [
                os.path.join(stage_dir, f) for f in os.listdir(stage_dir)
                if os.path.isfile(os.path.join(stage_dir, f))
                and os.path.splitext(f)[1].lower() not in (".jpg", ".png", ".webp")
            ]
        if not produced:
            raise RuntimeError("Audio extraction produced no output file.")

        produced.sort(key=os.path.getsize, reverse=True)
        staged_file = produced[0]
        final_file = _unique_path(os.path.join(output_path, os.path.basename(staged_file)))
        moved = False
        for attempt in range(8):
            try:
                shutil.move(staged_file, final_file)
                moved = True
                break
            except (PermissionError, OSError):
                if attempt < 7:
                    time.sleep(0.5)
                else:
                    try:
                        shutil.copy2(staged_file, final_file)
                        try:
                            os.remove(staged_file)
                        except Exception:
                            pass
                        moved = True
                    except Exception:
                        pass
        if not moved and not os.path.exists(final_file):
            raise RuntimeError(f"Could not save audio file to '{final_file}'. Check folder write permissions.")

        _apply_audio_tags(final_file, target_format, title=title, artist=artist, album=album)

        dest_name = os.path.basename(os.path.normpath(output_path)) or "destination"
        return {
            "status": "success",
            "title": saved_title,
            "format": target_format,
            "file": final_file,
            "message": f"'{saved_title}' saved as high-quality {target_format.upper()} in {dest_name}.",
        }
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
        shutil.rmtree(temp_dir, ignore_errors=True)


def probe_audio_streams(path):
    """Return the number of audio streams in a file (-1 when unknown)."""
    if not shutil.which("ffprobe"):
        return -1
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "json", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if res.returncode != 0:
            return -1
        return len(json.loads(res.stdout.decode("utf-8", "replace")).get("streams", []))
    except Exception:
        return -1


def _summarise_ffmpeg_error(stderr):
    """Pull the lines that actually explain the failure out of FFmpeg's banner.

    Blindly taking the last N lines surfaced things like "Metadata: |
    handler_name", which tells the user nothing.
    """
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    keywords = ("error", "invalid", "failed", "not contain", "no such file",
                "unable", "unsupported", "denied", "does not")
    useful = [ln for ln in lines if any(k in ln.lower() for k in keywords)]
    chosen = useful[-4:] if useful else lines[-4:]
    return " | ".join(chosen) if chosen else "FFmpeg exited with a non-zero status."


def _convert_local(source, output_path, target_format, container_ext, quality,
                   title, artist, album, cancel_check):
    if not os.path.isfile(source):
        raise FileNotFoundError(f"Source file not found: {source}")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is required for local audio conversion but was not found on system PATH.")

    # Fail fast with a message the user can act on. Without this, a silent
    # video-only file produced an opaque "Invalid argument" from FFmpeg.
    if probe_audio_streams(source) == 0:
        raise RuntimeError(
            f"'{os.path.basename(source)}' has no audio track, so there is nothing to extract."
        )

    base_name = os.path.splitext(os.path.basename(source))[0]
    out_file = _unique_path(os.path.join(output_path, f"{base_name}.{container_ext}"))

    cmd = ["ffmpeg", "-nostdin", "-y", "-i", source, "-vn", "-map_metadata", "0"]
    if target_format == "mp3":
        cmd += ["-codec:a", "libmp3lame", "-b:a", f"{quality}k"]
    elif target_format == "flac":
        cmd += ["-codec:a", "flac"]
    elif target_format in ("aac", "m4a"):
        cmd += ["-codec:a", "aac", "-b:a", f"{quality}k"]
    elif target_format == "wav":
        cmd += ["-codec:a", "pcm_s16le"]
    cmd.append(out_file)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )

    # Drain stderr on its own thread. Repeated communicate(timeout=...) calls
    # returned a truncated buffer, so the real error line was lost.
    err_chunks = []

    def drain():
        try:
            for line in proc.stderr:
                err_chunks.append(line)
        except Exception:
            pass

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()

    waited = 0.0
    while proc.poll() is None:
        if cancel_check and cancel_check():
            _kill(proc)
            reader.join(timeout=2)
            _remove_quietly(out_file)
            raise AudioCancelled("Audio conversion cancelled by user.")
        if waited > FFMPEG_TIMEOUT:
            _kill(proc)
            reader.join(timeout=2)
            _remove_quietly(out_file)
            raise RuntimeError(f"FFmpeg conversion timed out after {FFMPEG_TIMEOUT}s.")
        waited += 0.25
        time.sleep(0.25)

    reader.join(timeout=5)
    stderr = "".join(err_chunks)

    if proc.returncode != 0 or not os.path.exists(out_file) or os.path.getsize(out_file) == 0:
        _remove_quietly(out_file)
        raise RuntimeError("FFmpeg conversion failed: " + _summarise_ffmpeg_error(stderr))

    _apply_audio_tags(out_file, target_format, title=title, artist=artist, album=album)

    return {
        "status": "success",
        "title": title or base_name,
        "file": out_file,
        "format": target_format,
        "message": f"Converted '{os.path.basename(out_file)}' successfully.",
    }


def _kill(proc):
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass


def _remove_quietly(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
