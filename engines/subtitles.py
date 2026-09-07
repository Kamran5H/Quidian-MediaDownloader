import os
import re
import shutil
import tempfile
import uuid
import yt_dlp


def _convert_vtt_to_srt(vtt_path, srt_path):
    """Convert WebVTT subtitle file to standard SubRip (.srt) format in pure Python."""
    try:
        with open(vtt_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()

        srt_lines = []
        cue_index = 1
        in_cue = False

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("WEBVTT") or stripped.startswith("NOTE") or stripped.startswith("STYLE"):
                if in_cue:
                    srt_lines.append("\n")
                    in_cue = False
                continue

            # Match timing line: e.g. 00:01:23.456 --> 00:01:25.789
            if "-->" in stripped:
                timing = stripped.replace(".", ",")
                srt_lines.append(f"{cue_index}\n")
                srt_lines.append(f"{timing}\n")
                cue_index += 1
                in_cue = True
            elif in_cue:
                # Strip inline WebVTT formatting tags like <c>, <v Speaker>, <i>, etc.
                clean_text = re.sub(r'<[^>]+>', '', stripped)
                if clean_text:
                    srt_lines.append(f"{clean_text}\n")

        with open(srt_path, 'w', encoding='utf-8') as f:
            f.writelines(srt_lines)
        return True
    except Exception:
        return False


def extract_subtitles(url, lang="en", output_path="Downloads"):
    """
    Download subtitles from a video link, converting automatically into clean .srt format.
    Supports English, Urdu, Spanish, Hindi, Arabic, French, German, Japanese, etc.
    Equipped with pure-Python VTT-to-SRT conversion fallback if FFmpeg is unavailable.
    """
    if not (url.startswith("http://") or url.startswith("https://") or os.path.isfile(url)):
        url = f"ytsearch1:{url}"

    os.makedirs(output_path, exist_ok=True)
    session_id = uuid.uuid4().hex[:8]
    stage_dir = os.path.join(tempfile.gettempdir(), f"sub_stage_{session_id}")
    temp_dir = os.path.join(tempfile.gettempdir(), f"sub_temp_{session_id}")
    os.makedirs(stage_dir, exist_ok=True)
    os.makedirs(temp_dir, exist_ok=True)

    has_ffmpeg = bool(shutil.which("ffmpeg"))

    ydl_opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [f"{lang}.*", lang],
        "subtitlesformat": "srt/vtt/best",
        "paths": {"home": stage_dir, "temp": temp_dir},
        "outtmpl": {"default": "%(title).180s.%(ext)s"},
        "noplaylist": True,
        "windowsfilenames": True,
        "no_warnings": True,
        "quiet": True,
    }

    if has_ffmpeg:
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegSubtitlesConvertor",
            "format": "srt"
        }]

    saved_files = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        dest_name = os.path.basename(os.path.normpath(output_path)) or "destination"
        title = (info or {}).get("title") or "Video"

        # Pure-Python fallback: Convert any remaining .vtt files inside the staging directory
        for f in os.listdir(stage_dir):
            if f.endswith(".vtt"):
                vtt_full = os.path.join(stage_dir, f)
                srt_full = os.path.splitext(vtt_full)[0] + ".srt"
                if _convert_vtt_to_srt(vtt_full, srt_full):
                    try:
                        os.remove(vtt_full)
                    except Exception:
                        pass

        # Relocate newly generated subtitle files into output_path safely
        for f in os.listdir(stage_dir):
            src = os.path.join(stage_dir, f)
            if not os.path.isfile(src):
                continue
            base, ext = os.path.splitext(f)
            dst = os.path.join(output_path, f)
            counter = 1
            while os.path.exists(dst):
                dst = os.path.join(output_path, f"{base} ({counter}){ext}")
                counter += 1
            shutil.move(src, dst)
            saved_files.append(dst)

        return {
            "status": "success",
            "title": title,
            "lang": lang,
            "files": saved_files,
            "message": f"Subtitles for '{title}' saved as .srt in {dest_name}."
        }
    except Exception as e:
        raise RuntimeError(f"Subtitle extraction error: {e}")
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
        shutil.rmtree(temp_dir, ignore_errors=True)

