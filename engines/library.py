"""
Quidian - Downloads library scanner and safe launcher.
Developed by Kamran Ashraf
"""

import os
import shutil
import subprocess
import sys
import time

MEDIA_EXTENSIONS = {
    "video": (".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts", ".m4v",
              ".wmv", ".3gp", ".mpg", ".mpeg", ".m2ts", ".ogv"),
    "audio": (".mp3", ".m4a", ".flac", ".wav", ".aac", ".opus", ".ogg", ".wma",
              ".aiff", ".alac"),
    "subtitle": (".srt", ".vtt", ".ass", ".sub", ".smi", ".ssa"),
    "image": (".jpg", ".jpeg", ".png", ".webp", ".gif", ".jfif", ".bmp", ".svg",
              ".avif", ".heic"),
}

# Flattened lookup so categorising a file is O(1) instead of a nested scan.
_EXT_CATEGORY = {ext: cat for cat, exts in MEDIA_EXTENSIONS.items() for ext in exts}

# Never hand these to the shell/OS launcher, whatever the user clicks.
BLOCKED_EXEC_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse",
    ".scr", ".msi", ".msp", ".dll", ".com", ".pif", ".cpl", ".reg", ".wsf",
    ".wsh", ".hta", ".lnk", ".jar", ".apk", ".sh", ".application", ".gadget",
    ".msc", ".inf", ".scf", ".url",
}

# Bound the walk so a Downloads folder with 100k files cannot stall the UI.
_MAX_SCAN_ENTRIES = 20000


def _scan_entry(entry, items, group=None):
    """Append one media file to ``items``; silently skip anything unreadable."""
    ext = os.path.splitext(entry.name)[1].lower()
    category = _EXT_CATEGORY.get(ext)
    if not category:
        return

    try:
        stat = entry.stat()
    except (PermissionError, OSError):
        return

    size_bytes = stat.st_size
    size_mb = round(size_bytes / (1024 * 1024), 2)
    mod_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
    items.append({
        "name": entry.name,
        "path": entry.path,
        "ext": ext.replace(".", "").upper(),
        "category": category,
        "group": group,
        "size_bytes": size_bytes,
        "size_mb": size_mb,
        "size_formatted": _format_size(size_bytes),
        "mtime": stat.st_mtime,
        "date": mod_time,
        "modified": mod_time,
    })


def _format_size(size_bytes):
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / 1024 ** 3:.2f} GB"
    if size_bytes >= 1024 ** 2:
        return f"{size_bytes / 1024 ** 2:.2f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def list_downloads(downloads_dir, limit=50):
    """
    Scan a directory (plus its immediate subdirectories) and return recent
    media files, newest first.
    """
    if not downloads_dir or not os.path.isdir(downloads_dir):
        return []

    items = []
    scanned = 0
    try:
        with os.scandir(downloads_dir) as entries:
            for entry in entries:
                if scanned >= _MAX_SCAN_ENTRIES:
                    break
                scanned += 1
                try:
                    if entry.is_file(follow_symlinks=False):
                        _scan_entry(entry, items)
                    elif entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                        # One level down catches gallery-dl's per-album folders.
                        try:
                            with os.scandir(entry.path) as sub_entries:
                                for sub in sub_entries:
                                    if scanned >= _MAX_SCAN_ENTRIES:
                                        break
                                    scanned += 1
                                    try:
                                        if sub.is_file(follow_symlinks=False):
                                            _scan_entry(sub, items, group=entry.name)
                                    except (PermissionError, OSError):
                                        continue
                        except (PermissionError, OSError):
                            continue
                except (PermissionError, OSError):
                    continue

        items.sort(key=lambda x: x["mtime"], reverse=True)
    except Exception as e:
        print(f"Library scan error: {e}")

    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 50
    return items[:limit]


def _assert_launchable(filepath):
    """Normalize a path and refuse anything that could execute code."""
    if not filepath:
        raise FileNotFoundError("No path provided.")
    norm = os.path.realpath(os.path.normpath(os.path.abspath(filepath)))
    if not os.path.exists(norm):
        raise FileNotFoundError(f"File not found: {filepath}")
    if os.path.isfile(norm):
        ext = os.path.splitext(norm)[1].lower()
        if ext in BLOCKED_EXEC_EXTENSIONS:
            raise PermissionError(
                f"Direct launch of executable file '{os.path.basename(norm)}' is blocked for system security."
            )
    return norm


def open_downloaded_file(filepath):
    """Open a file or folder in the OS default application, never an executable."""
    norm = _assert_launchable(filepath)

    if sys.platform.startswith("win"):
        try:
            os.startfile(norm)  # noqa: S606 - extension allow-list enforced above
        except OSError as e:
            raise RuntimeError(f"No application is registered to open '{os.path.basename(norm)}' ({e}).")
    elif sys.platform == "darwin":
        subprocess.Popen(["open", norm])
    else:
        _xdg_open(norm)
    return True


def _xdg_open(target):
    """Open `target` on Linux/BSD desktops, with an actionable error when no
    opener is installed (headless boxes, minimal WSL) instead of a bare
    FileNotFoundError about 'xdg-open'."""
    for opener in ("xdg-open", "gio", "kde-open5", "kde-open"):
        exe = shutil.which(opener)
        if exe:
            argv = [exe, "open", target] if opener == "gio" else [exe, target]
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    raise RuntimeError(
        "No desktop file opener was found (install 'xdg-utils'). "
        f"The file is at: {target}"
    )


def reveal_in_explorer(filepath):
    """
    Highlight a file in the native file manager or open directory directly.

    The path is passed as an argv element, never interpolated into a shell
    string - a filename containing quotes or '&' would otherwise have been
    executed as a command.
    """
    norm = _assert_launchable(filepath)

    if sys.platform.startswith("win"):
        # explorer.exe returns a non-zero exit code even on success, so the
        # result is deliberately not checked.
        if os.path.isdir(norm):
            subprocess.run(["explorer.exe", norm], timeout=15, check=False)
        else:
            subprocess.run(["explorer.exe", f"/select,{norm}"], timeout=15, check=False)
        return True
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", norm], timeout=15, check=False)
        return True
    parent = norm if os.path.isdir(norm) else os.path.dirname(norm)
    _xdg_open(parent)
    return True
