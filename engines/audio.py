import os
import sys
import subprocess
import shutil
import tempfile
import uuid
import yt_dlp
import mutagen
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC


def _apply_audio_tags(filepath, target_format="mp3", title=None, artist=None, album=None):
    """Safely embed metadata tags (title, artist, album) into MP3 or FLAC files."""
    if not (title or artist or album) or not filepath or not os.path.isfile(filepath):
        return
    fmt = (target_format or "mp3").lower()
    if fmt == "mp3":
        try:
            audio = MP3(filepath, ID3=EasyID3)
            if title: audio["title"] = title
            if artist: audio["artist"] = artist
            if album: audio["album"] = album
            audio.save()
        except Exception:
            try:
                # If file lacks ID3 header, initialize it
                audio = EasyID3()
                audio.save(filepath)
                if title: audio["title"] = title
                if artist: audio["artist"] = artist
                if album: audio["album"] = album
                audio.save(filepath)
            except Exception:
                pass
    elif fmt == "flac":
        try:
            from mutagen.flac import FLAC
            audio = FLAC(filepath)
            if title: audio["title"] = title
            if artist: audio["artist"] = artist
            if album: audio["album"] = album
            audio.save()
        except Exception:
            pass


def rip_and_tag_audio(source, output_path, target_format="mp3", bitrate="320k",
                      title=None, artist=None, album=None):
    """
    Rip audio from online URL or convert local video/audio file into studio-quality
    MP3 (320kbps), FLAC (lossless), AAC, or WAV, with ID3 tagging.
    """
    os.makedirs(output_path, exist_ok=True)
    target_format = target_format.lower()

    if source.startswith("http://") or source.startswith("https://"):
        # Online source via yt-dlp
        temp_dir = os.path.join(tempfile.gettempdir(), f"audio_temp_{uuid.uuid4().hex[:8]}")
        os.makedirs(temp_dir, exist_ok=True)

        ydl_opts = {
            "format": "bestaudio/best",
            "paths": {"home": output_path, "temp": temp_dir},
            "outtmpl": {"default": "%(title).180s.%(ext)s"},
            "noplaylist": True,
            "windowsfilenames": True,
            "no_warnings": True,
            "quiet": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": target_format if target_format in ("mp3", "aac", "flac", "wav", "m4a") else "mp3",
                    "preferredquality": "0" if bitrate == "320k" else "128",
                },
                {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
                {"key": "FFmpegMetadata"},
                {"key": "EmbedThumbnail"},
            ],
            "writethumbnail": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(source, download=True)
            saved_title = title or (info or {}).get("title") or "Audio Track"
            dest_name = os.path.basename(os.path.normpath(output_path)) or "destination"

            # Locate downloaded audio file to embed user ID3 metadata tags
            saved_file = None
            if info:
                prep = ydl.prepare_filename(info)
                base = os.path.splitext(prep)[0]
                target_ext = "m4a" if target_format == "aac" else target_format
                for candidate in [f"{base}.{target_format}", f"{base}.{target_ext}", prep]:
                    if os.path.exists(candidate):
                        saved_file = candidate
                        break

            if saved_file:
                _apply_audio_tags(saved_file, target_format, title=title, artist=artist, album=album)

            return {
                "status": "success",
                "title": saved_title,
                "format": target_format,
                "file": saved_file,
                "message": f"'{saved_title}' saved as high-quality {target_format.upper()} in {dest_name}."
            }
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    else:
        # Local video/audio conversion via FFmpeg
        if not os.path.exists(source):
            raise FileNotFoundError(f"Source file not found: {source}")

        base_name = os.path.splitext(os.path.basename(source))[0]
        out_file = os.path.join(output_path, f"{base_name}.{target_format}")
        
        counter = 1
        while os.path.exists(out_file):
            out_file = os.path.join(output_path, f"{base_name} ({counter}).{target_format}")
            counter += 1

        if not shutil.which("ffmpeg"):
            raise RuntimeError("FFmpeg is required for local audio conversion but was not found on system PATH.")

        cmd = ["ffmpeg", "-y", "-i", source, "-vn"]
        if target_format == "mp3":
            cmd.extend(["-codec:a", "libmp3lame", "-b:a", bitrate])
        elif target_format == "flac":
            cmd.extend(["-codec:a", "flac"])
        elif target_format == "aac":
            cmd.extend(["-codec:a", "aac", "-b:a", "256k"])
        elif target_format == "wav":
            cmd.extend(["-codec:a", "pcm_s16le"])
        cmd.append(out_file)

        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )
        if res.returncode != 0:
            raise RuntimeError(f"FFmpeg conversion failed: {res.stderr}")

        # Apply ID3 metadata tags
        _apply_audio_tags(out_file, target_format, title=title, artist=artist, album=album)

        return {
            "status": "success",
            "file": out_file,
            "format": target_format,
            "message": f"Converted '{os.path.basename(out_file)}' successfully."
        }

