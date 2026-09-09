"""
Quidian - Subtitle extractor and pure-Python WebVTT -> SubRip converter.
Developed by Kamran Ashraf
"""

import os
import re
import shutil
import tempfile
import uuid

import yt_dlp

SUBTITLE_EXTS = (".srt", ".vtt", ".ass", ".ssa", ".ttml", ".sbv", ".lrc", ".json3")

# 00:01:23.456 --> 00:01:25.789 align:start position:0%
_CUE_RE = re.compile(
    r"^(?P<start>(?:\d{1,3}:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*"
    r"(?P<end>(?:\d{1,3}:)?\d{1,2}:\d{2}[.,]\d{1,3})(?P<settings>.*)$"
)
_TAG_RE = re.compile(r"<[^>]+>")


def _normalise_timestamp(ts):
    """VTT allows MM:SS.mmm; SRT always wants HH:MM:SS,mmm."""
    ts = ts.strip().replace(".", ",")
    parts = ts.split(":")
    if len(parts) == 2:
        parts = ["00"] + parts
    h, m, rest = parts[0], parts[1], parts[2]
    sec, _, ms = rest.partition(",")
    ms = (ms + "000")[:3]
    return f"{int(h):02d}:{int(m):02d}:{int(sec):02d},{ms}"


def _clean_payload(lines):
    out = []
    for raw in lines:
        text = _TAG_RE.sub("", raw).strip()
        # &nbsp; and friends show up in auto-generated captions.
        text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        if text:
            out.append(text)
    return out


def parse_vtt_cues(text):
    """
    Parse WebVTT into ``[(start, end, [lines]), ...]``.

    Handles optional cue identifiers, cue positioning settings after the
    timestamps, NOTE/STYLE/REGION blocks, and both '.' and ',' decimals.
    """
    cues = []
    pending = []
    current = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r").strip("﻿")
        stripped = line.strip()

        m = _CUE_RE.match(stripped)
        if m:
            if current:
                cues.append((current[0], current[1], _clean_payload(current[2])))
            # An identifier line immediately above the timing is metadata, not text.
            pending = []
            current = (_normalise_timestamp(m.group("start")),
                       _normalise_timestamp(m.group("end")),
                       [])
            continue

        if not stripped:
            if current:
                cues.append((current[0], current[1], _clean_payload(current[2])))
                current = None
            pending = []
            continue

        if current is not None:
            current[2].append(stripped)
        else:
            upper = stripped.upper()
            if upper.startswith(("WEBVTT", "NOTE", "STYLE", "REGION", "KIND:", "LANGUAGE:")):
                continue
            pending.append(stripped)

    if current:
        cues.append((current[0], current[1], _clean_payload(current[2])))

    return [c for c in cues if c[2]]


def _dedupe_rolling(cues):
    """
    Drop the duplicate cues YouTube emits for rolling auto-captions.

    Only a cue whose text is identical to its immediate predecessor AND which
    starts exactly where that one ended is removed, so genuine repeated lines
    of dialogue survive.
    """
    out = []
    for start, end, lines in cues:
        if out:
            p_start, p_end, p_lines = out[-1]
            if p_lines == lines and p_end == start:
                out[-1] = (p_start, end, p_lines)
                continue
        out.append((start, end, lines))
    return out


def _convert_vtt_to_srt(vtt_path, srt_path):
    """Convert a WebVTT file to SubRip. Returns True when cues were written."""
    try:
        with open(vtt_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        cues = _dedupe_rolling(parse_vtt_cues(content))
        if not cues:
            return False

        with open(srt_path, "w", encoding="utf-8") as f:
            for idx, (start, end, lines) in enumerate(cues, 1):
                f.write(f"{idx}\n{start} --> {end}\n")
                f.write("\n".join(lines))
                f.write("\n\n")
        return True
    except Exception:
        return False


def _available_languages(info):
    langs = set()
    for key in ("subtitles", "automatic_captions"):
        for code in ((info or {}).get(key) or {}):
            langs.add(code)
    return sorted(langs)


def pick_subtitle_track(info, lang):
    """
    Choose the single best track code for ``lang``.

    Passing yt-dlp a wildcard such as ``en.*`` makes it fetch every
    auto-translated variant YouTube advertises (en-af, en-sq, en-am, ...),
    which is slow, writes dozens of unwanted files, and reliably earns an
    HTTP 429. Resolving one concrete code up front avoids all of that.

    Preference order: exact match, then <lang>-orig, then the shortest
    <lang>-<region> variant, checking manual subtitles before auto-captions.
    """
    lang = (lang or "en").lower()
    for key in ("subtitles", "automatic_captions"):
        table = (info or {}).get(key) or {}
        codes = [c for c in table if table[c]]
        if not codes:
            continue
        lowered = {c.lower(): c for c in codes}
        if lang in lowered:
            return lowered[lang], key
        if f"{lang}-orig" in lowered:
            return lowered[f"{lang}-orig"], key
        variants = sorted((c for c in codes if c.lower().split("-")[0] == lang), key=len)
        if variants:
            return variants[0], key
    return None, None


def extract_subtitles(url, lang="en", output_path="Downloads"):
    """
    Download subtitles for a video and normalise them into clean .srt.

    Raises when the requested language yields nothing, instead of silently
    reporting success with zero files.
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

    base_opts = {
        "skip_download": True,
        "noplaylist": True,
        "windowsfilenames": True,
        "no_warnings": True,
        "quiet": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 5,
    }

    saved_files = []
    try:
        # Pass 1: metadata only, to discover which caption tracks exist.
        with yt_dlp.YoutubeDL(dict(base_opts)) as probe:
            info = probe.extract_info(url, download=False)

        title = (info or {}).get("title") or "Video"
        track, source_kind = pick_subtitle_track(info, lang)
        if not track:
            available = _available_languages(info)
            hint = f" Available languages: {', '.join(available[:20])}." if available else \
                   " This video has no captions published."
            raise RuntimeError(f"No '{lang}' subtitles were found for '{title}'.{hint}")

        # Pass 2: download exactly the one track we resolved.
        ydl_opts = dict(base_opts)
        ydl_opts.update({
            "writesubtitles": source_kind == "subtitles",
            "writeautomaticsub": source_kind == "automatic_captions",
            "subtitleslangs": [track],
            "subtitlesformat": "srt/vtt/best",
            "paths": {"home": stage_dir, "temp": temp_dir},
            "outtmpl": {"default": "%(title).180s.%(ext)s"},
        })
        if has_ffmpeg:
            ydl_opts["postprocessors"] = [{"key": "FFmpegSubtitlesConvertor", "format": "srt"}]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        dest_name = os.path.basename(os.path.normpath(output_path)) or "destination"

        # Pure-Python fallback for anything FFmpeg did not already convert.
        for f in sorted(os.listdir(stage_dir)):
            if not f.lower().endswith(".vtt"):
                continue
            vtt_full = os.path.join(stage_dir, f)
            srt_full = os.path.splitext(vtt_full)[0] + ".srt"
            if os.path.exists(srt_full):
                _remove_quietly(vtt_full)
                continue
            if _convert_vtt_to_srt(vtt_full, srt_full):
                _remove_quietly(vtt_full)

        # Relocate the finished subtitle files into the destination folder.
        for f in sorted(os.listdir(stage_dir)):
            src = os.path.join(stage_dir, f)
            if not os.path.isfile(src):
                continue
            if os.path.splitext(f)[1].lower() not in SUBTITLE_EXTS:
                continue
            base, ext = os.path.splitext(f)
            dst = os.path.join(output_path, f)
            counter = 1
            while os.path.exists(dst):
                dst = os.path.join(output_path, f"{base} ({counter}){ext}")
                counter += 1
            shutil.move(src, dst)
            saved_files.append(dst)

        if not saved_files:
            raise RuntimeError(
                f"The '{track}' subtitle track for '{title}' could not be downloaded."
            )

        kind = "auto-generated" if source_kind == "automatic_captions" else "published"
        return {
            "status": "success",
            "title": title,
            "lang": lang,
            "track": track,
            "auto_generated": source_kind == "automatic_captions",
            "files": saved_files,
            "count": len(saved_files),
            "message": (f"{len(saved_files)} {kind} subtitle file(s) [{track}] "
                        f"for '{title}' saved as .srt in {dest_name}."),
        }
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Subtitle extraction error: {e}")
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
        shutil.rmtree(temp_dir, ignore_errors=True)


def _remove_quietly(path):
    try:
        os.remove(path)
    except Exception:
        pass
