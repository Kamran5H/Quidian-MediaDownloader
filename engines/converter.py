"""
Quidian - Media Compressor & Format Converter Studio
Engineered with high-speed FFmpeg pipelines for Discord, Web, Lossless Trimming,
and Universal Media Transcoding.
Developed by Kamran Ashraf
"""

import os
import re
import json
import shutil
import subprocess
import time
from urllib.parse import unquote

PRESETS = {
    "discord_8mb": {
        "name": "Discord Free (8MB)",
        "description": "Compresses video to fit Discord's legacy 8MB upload limit",
        "target_mb": 7.8,
        "format": "mp4",
        "vcodec": "libx264",
        "acodec": "aac",
        "audio_bitrate": "96k",
        "preset": "medium",
    },
    "discord_25mb": {
        "name": "Discord Standard / Nitro Basic (25MB)",
        "description": "Optimized high-clarity 25MB encode for Discord Nitro Basic",
        "target_mb": 24.5,
        "format": "mp4",
        "vcodec": "libx264",
        "acodec": "aac",
        "audio_bitrate": "128k",
        "preset": "fast",
    },
    "discord_50mb": {
        "name": "Discord Nitro (50MB)",
        "description": "Pristine 1080p high-bitrate encode under 50MB",
        "target_mb": 49.0,
        "format": "mp4",
        "vcodec": "libx264",
        "acodec": "aac",
        "audio_bitrate": "192k",
        "preset": "fast",
    },
    "discord_100mb": {
        "name": "Discord Server Booster (100MB)",
        "description": "Near-lossless high bitrate encode under 100MB",
        "target_mb": 98.0,
        "format": "mp4",
        "vcodec": "libx264",
        "acodec": "aac",
        "audio_bitrate": "256k",
        "preset": "fast",
    },
    "whatsapp_16mb": {
        "name": "WhatsApp Video (16MB)",
        "description": "Mobile-optimized MP4 with faststart under 16MB",
        "target_mb": 15.5,
        "format": "mp4",
        "vcodec": "libx264",
        "acodec": "aac",
        "audio_bitrate": "96k",
        "preset": "fast",
    },
    "web_fast_1080p": {
        "name": "Web Universal 1080p (H.264 / AAC)",
        "description": "Universal compatibility for all browsers, TVs, and mobile players",
        "format": "mp4",
        "vcodec": "libx264",
        "crf": "22",
        "max_height": 1080,
        "acodec": "aac",
        "audio_bitrate": "192k",
        "preset": "fast",
    },
    "compact_hevc": {
        "name": "Compact HEVC / H.265 (50% smaller)",
        "description": "High-efficiency video coding for modern 4K/1080p playback",
        "format": "mp4",
        "vcodec": "libx265",
        "crf": "26",
        "acodec": "aac",
        "audio_bitrate": "128k",
        "preset": "fast",
    },
    "web_efficient_av1": {
        "name": "Next-Gen AV1 (Ultra-Compact)",
        "description": "State-of-the-art open codec with highest compression ratio",
        "format": "mp4",
        "vcodec": "libsvtav1",
        "crf": "30",
        "acodec": "libopus",
        "audio_bitrate": "128k",
        "preset": "6",
    },
    "animated_gif": {
        "name": "High-Quality Animated GIF",
        "description": "Extracts video clip to smooth 15 FPS GIF with custom palette",
        "format": "gif",
        "is_gif": True,
        "fps": 15,
        "max_width": 640,
    },
    "extract_audio_mp3": {
        "name": "Extract Studio MP3 (320 kbps)",
        "description": "Rips and normalizes audio track to pristine MP3 320k",
        "format": "mp3",
        "is_audio": True,
        "acodec": "libmp3lame",
        "audio_bitrate": "320k",
    },
    "extract_audio_flac": {
        "name": "Extract Lossless FLAC",
        "description": "Bit-perfect uncompressed lossless audio track",
        "format": "flac",
        "is_audio": True,
        "acodec": "flac",
    },
    "lossless_trim": {
        "name": "Instant Lossless Trim",
        "description": "Cuts start/end range in under 1 second without re-encoding",
        "format": "copy",
        "is_trim": True,
        "stream_copy": True,
    }
}

# Aliases for flexible UI / API invocation
PRESETS["web_mp4_1080p"] = PRESETS["web_fast_1080p"]
PRESETS["web_av1"] = PRESETS["web_efficient_av1"]
PRESETS["web_hevc"] = PRESETS["compact_hevc"]
PRESETS["gif_hq"] = PRESETS["animated_gif"]
PRESETS["audio_mp3_320k"] = PRESETS["extract_audio_mp3"]
PRESETS["audio_flac"] = PRESETS["extract_audio_flac"]


def parse_time(time_str):
    """Parse '01:23:45', '12:34', or '75.5' into float seconds."""
    if time_str is None:
        return None
    s = str(time_str).strip()
    if not s:
        return None
    try:
        if ":" in s:
            parts = [float(p) for p in s.split(":")]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            elif len(parts) == 2:
                return parts[0] * 60 + parts[1]
        return float(s)
    except (ValueError, TypeError):
        return None


def format_time(seconds):
    """Format seconds into HH:MM:SS or MM:SS."""
    if seconds is None or seconds < 0:
        return "00:00"
    total = int(round(seconds))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def probe_media(file_path):
    """
    Run ffprobe to extract comprehensive container, video, and audio stream metrics.
    Returns clean dictionary with duration, resolution, codecs, bitrates, and stream info.
    """
    if not file_path or not os.path.exists(file_path):
        raise FileNotFoundError(f"Media file not found: {file_path}")

    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required for media inspection but was not found on PATH.")

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        file_path,
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffprobe failed: {proc.stderr}")
        data = json.loads(proc.stdout)
    except Exception as e:
        raise RuntimeError(f"Failed to probe media: {e}")

    fmt = data.get("format", {})
    streams = data.get("streams", [])

    duration_sec = float(fmt.get("duration") or 0.0)
    size_bytes = int(fmt.get("size") or os.path.getsize(file_path))
    bitrate_kbps = int(float(fmt.get("bit_rate") or 0) / 1000) if fmt.get("bit_rate") else None

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    width = video_stream.get("width") if video_stream else None
    height = video_stream.get("height") if video_stream else None
    vcodec = video_stream.get("codec_name") if video_stream else None
    acodec = audio_stream.get("codec_name") if audio_stream else None

    # Calculate FPS
    fps = None
    if video_stream and video_stream.get("r_frame_rate"):
        r_fr = video_stream["r_frame_rate"]
        if "/" in r_fr:
            n, d = r_fr.split("/")
            try:
                if float(d) > 0:
                    fps = round(float(n) / float(d), 2)
            except Exception:
                pass

    if duration_sec <= 0 and video_stream and video_stream.get("duration"):
        try:
            duration_sec = float(video_stream["duration"])
        except Exception:
            pass

    return {
        "filepath": os.path.normpath(file_path),
        "filename": os.path.basename(file_path),
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / (1024 * 1024), 2),
        "duration_sec": round(duration_sec, 2),
        "duration_formatted": format_time(duration_sec),
        "bitrate_kbps": bitrate_kbps,
        "has_video": video_stream is not None,
        "has_audio": audio_stream is not None,
        "width": width,
        "height": height,
        "resolution": f"{width}x{height}" if width and height else None,
        "fps": fps,
        "video_codec": vcodec,
        "audio_codec": acodec,
        "audio_channels": audio_stream.get("channels") if audio_stream else None,
        "audio_sample_rate": audio_stream.get("sample_rate") if audio_stream else None,
        "format_name": fmt.get("format_name"),
    }


def _get_unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    counter = 1
    while os.path.exists(f"{base} ({counter}){ext}"):
        counter += 1
    return f"{base} ({counter}){ext}"


def convert_media(
    input_path,
    output_dir=None,
    output_filename=None,
    preset="discord_25mb",
    start_time=None,
    end_time=None,
    custom_bitrate=None,
    custom_crf=None,
    custom_resolution=None,
    progress_callback=None,
    cancel_check=None,
):
    """
    Execute high-speed conversion, compression, or lossless trimming via FFmpeg.
    Reports progress via progress_callback(dict(percent=..., speed=..., eta=...)).
    Supports clean cancellation.
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    meta = probe_media(input_path)
    duration = meta["duration_sec"]

    parsed_start = parse_time(start_time)
    parsed_end = parse_time(end_time)

    # Determine effective duration for progress calculation
    eff_start = parsed_start or 0.0
    eff_end = parsed_end if (parsed_end and parsed_end > eff_start) else duration
    effective_duration = max(1.0, eff_end - eff_start)

    preset_cfg = PRESETS.get(preset, PRESETS["discord_25mb"])
    out_dir = output_dir or os.path.dirname(input_path)
    os.makedirs(out_dir, exist_ok=True)

    base_name = os.path.splitext(os.path.basename(input_path))[0]
    target_ext = preset_cfg.get("format") or os.path.splitext(input_path)[1].lstrip(".") or "mp4"

    if output_filename:
        out_name = output_filename
        if not out_name.lower().endswith(f".{target_ext.lower()}"):
            out_name = f"{out_name}.{target_ext}"
    else:
        suffix = f"_{preset}" if preset != "lossless_trim" else "_trimmed"
        out_name = f"{base_name}{suffix}.{target_ext}"

    out_file = _get_unique_path(os.path.join(out_dir, out_name))

    # Fast Lossless Trim (Stream Copy)
    if preset == "lossless_trim" or preset_cfg.get("stream_copy"):
        cmd = ["ffmpeg", "-nostdin", "-y"]
        if parsed_start is not None:
            cmd.extend(["-ss", str(parsed_start)])
        if parsed_end is not None:
            cmd.extend(["-to", str(parsed_end)])
        cmd.extend([
            "-i", input_path,
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            out_file,
        ])
        _execute_ffmpeg(cmd, effective_duration, out_file, progress_callback, cancel_check)
        return {
            "status": "success",
            "filepath": out_file,
            "filename": os.path.basename(out_file),
            "size_bytes": os.path.getsize(out_file),
            "size_mb": round(os.path.getsize(out_file) / (1024 * 1024), 2),
            "duration_sec": effective_duration,
        }

    # High-Quality Animated GIF
    if preset_cfg.get("is_gif"):
        fps = preset_cfg.get("fps", 15)
        max_w = preset_cfg.get("max_width", 640)
        vf_filter = f"fps={fps},scale={max_w}:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
        cmd = ["ffmpeg", "-nostdin", "-y"]
        if parsed_start is not None:
            cmd.extend(["-ss", str(parsed_start)])
        if parsed_end is not None:
            cmd.extend(["-to", str(parsed_end)])
        cmd.extend([
            "-i", input_path,
            "-vf", vf_filter,
            out_file,
        ])
        _execute_ffmpeg(cmd, effective_duration, out_file, progress_callback, cancel_check)
        return {
            "status": "success",
            "filepath": out_file,
            "filename": os.path.basename(out_file),
            "size_bytes": os.path.getsize(out_file),
            "size_mb": round(os.path.getsize(out_file) / (1024 * 1024), 2),
            "duration_sec": effective_duration,
        }

    # Audio Extraction Only
    if preset_cfg.get("is_audio"):
        acodec = preset_cfg.get("acodec", "libmp3lame")
        cmd = ["ffmpeg", "-nostdin", "-y"]
        if parsed_start is not None:
            cmd.extend(["-ss", str(parsed_start)])
        if parsed_end is not None:
            cmd.extend(["-to", str(parsed_end)])
        cmd.extend([
            "-i", input_path,
            "-vn",
            "-codec:a", acodec,
        ])
        if preset_cfg.get("audio_bitrate"):
            cmd.extend(["-b:a", preset_cfg["audio_bitrate"]])
        cmd.append(out_file)
        _execute_ffmpeg(cmd, effective_duration, out_file, progress_callback, cancel_check)
        return {
            "status": "success",
            "filepath": out_file,
            "filename": os.path.basename(out_file),
            "size_bytes": os.path.getsize(out_file),
            "size_mb": round(os.path.getsize(out_file) / (1024 * 1024), 2),
            "duration_sec": effective_duration,
        }

    # Video Compression / Transcode
    cmd = ["ffmpeg", "-nostdin", "-y"]
    if parsed_start is not None:
        cmd.extend(["-ss", str(parsed_start)])
    if parsed_end is not None:
        cmd.extend(["-to", str(parsed_end)])
    cmd.extend(["-i", input_path])

    # Calculate Target Bitrate for Target MB Limit
    target_mb = preset_cfg.get("target_mb")
    vcodec = preset_cfg.get("vcodec", "libx264")
    acodec = preset_cfg.get("acodec", "aac")
    abitrate_str = preset_cfg.get("audio_bitrate", "128k")
    abitrate_kbps = int(abitrate_str.rstrip("k"))

    vf_filters = []
    if custom_resolution:
        vf_filters.append(f"scale={custom_resolution}")
    elif preset_cfg.get("max_height") and meta["height"] and meta["height"] > preset_cfg["max_height"]:
        vf_filters.append(f"scale=-2:{preset_cfg['max_height']}")

    if target_mb and effective_duration > 0:
        # Target MB to total bits: (target_mb * 8192) kbits
        total_kbits = target_mb * 8192
        total_kbps = total_kbits / effective_duration
        target_vkbps = max(100, int(total_kbps - abitrate_kbps))
        cmd.extend([
            "-c:v", vcodec,
            "-b:v", f"{target_vkbps}k",
            "-maxrate", f"{int(target_vkbps * 1.35)}k",
            "-bufsize", f"{int(target_vkbps * 2.5)}k",
            "-preset", preset_cfg.get("preset", "fast"),
            "-c:a", acodec,
            "-b:a", f"{abitrate_kbps}k",
        ])
    else:
        # Quality-based CRF
        crf = custom_crf or preset_cfg.get("crf", "22")
        cmd.extend([
            "-c:v", vcodec,
            "-crf", str(crf),
            "-preset", preset_cfg.get("preset", "fast"),
            "-c:a", acodec,
            "-b:a", f"{abitrate_kbps}k",
        ])

    if vf_filters:
        cmd.extend(["-vf", ",".join(vf_filters)])

    cmd.extend([
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        out_file,
    ])

    _execute_ffmpeg(cmd, effective_duration, out_file, progress_callback, cancel_check)

    return {
        "status": "success",
        "filepath": out_file,
        "filename": os.path.basename(out_file),
        "size_bytes": os.path.getsize(out_file),
        "size_mb": round(os.path.getsize(out_file) / (1024 * 1024), 2),
        "duration_sec": effective_duration,
    }


def _execute_ffmpeg(cmd, total_duration, out_file, progress_cb=None, cancel_check=None):
    """Execute FFmpeg with real-time stderr progress tracking and cancellation."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is required but was not found on PATH.")

    # Append -progress pipe:1 for machine-readable progress
    cmd_with_prog = list(cmd)
    try:
        # Insert progress before output file
        cmd_with_prog.insert(-1, "-progress")
        cmd_with_prog.insert(-1, "pipe:1")
    except Exception:
        pass

    p = subprocess.Popen(
        cmd_with_prog,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        universal_newlines=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )

    last_pct = 0.0
    time_re = re.compile(r'out_time_ms=(\d+)')
    speed_re = re.compile(r'speed=\s*([0-9.]+x)')

    def check_cancel():
        if cancel_check and cancel_check():
            try:
                p.kill()
                p.wait(timeout=3)
            except Exception:
                pass
            if os.path.exists(out_file):
                try:
                    os.remove(out_file)
                except Exception:
                    pass
            raise RuntimeError("Operation cancelled by user.")

    while True:
        check_cancel()
        line = p.stdout.readline()
        if not line and p.poll() is not None:
            break
        if line:
            line_str = line.strip()
            if line_str.startswith("out_time_us=") or line_str.startswith("out_time_ms="):
                try:
                    val = int(line_str.split("=")[1])
                    cur_sec = val / 1_000_000.0 if "out_time_us" in line_str else val / 1000.0
                    if total_duration > 0:
                        pct = min(99.0, max(0.0, (cur_sec / total_duration) * 100.0))
                        last_pct = round(pct, 1)
                        if progress_cb:
                            progress_cb(last_pct, f"Processing media: {last_pct}%")
                except Exception:
                    pass
            elif line_str == "progress=end":
                if progress_cb:
                    progress_cb(100.0, "Finalizing converted media...")

    rc = p.poll()
    if rc != 0:
        err = p.stderr.read()
        if os.path.exists(out_file):
            try:
                os.remove(out_file)
            except Exception:
                pass
        raise RuntimeError(f"FFmpeg processing failed (exit {rc}): {(err or '').strip().splitlines()[-3:]}")

    if not os.path.exists(out_file) or os.path.getsize(out_file) < 512:
        if os.path.exists(out_file):
            try:
                os.remove(out_file)
            except Exception:
                pass
        raise RuntimeError("FFmpeg generated an empty or incomplete output file.")

    if progress_cb:
        progress_cb(100.0, f"Completed: {os.path.basename(out_file)}")
