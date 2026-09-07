import os
import sys
import subprocess
import time


MEDIA_EXTENSIONS = {
    "video": (".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts", ".m4v", ".wmv", ".3gp"),
    "audio": (".mp3", ".m4a", ".flac", ".wav", ".aac", ".opus", ".ogg", ".wma"),
    "subtitle": (".srt", ".vtt", ".ass", ".sub", ".smi"),
    "image": (".jpg", ".jpeg", ".png", ".webp", ".gif", ".jfif", ".bmp", ".svg"),
}


def _scan_entry(entry, items):
    if not entry.is_file():
        return
    ext = os.path.splitext(entry.name)[1].lower()
    category = None
    for cat, extensions in MEDIA_EXTENSIONS.items():
        if ext in extensions:
            category = cat
            break
    if not category:
        return

    try:
        stat = entry.stat()
        size_mb = round(stat.st_size / (1024 * 1024), 2)
        mod_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
        items.append({
            "name": entry.name,
            "path": entry.path,
            "ext": ext.replace(".", "").upper(),
            "category": category,
            "size_mb": size_mb,
            "size_formatted": f"{size_mb} MB" if size_mb >= 1 else f"{round(stat.st_size / 1024, 1)} KB",
            "mtime": stat.st_mtime,
            "date": mod_time,
            "modified": mod_time,
        })
    except Exception:
        pass


BLOCKED_EXEC_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".scr",
    ".msi", ".dll", ".com", ".pif", ".cpl", ".reg", ".wsf"
}


def list_downloads(downloads_dir, limit=50):
    """
    Scan the user's Downloads directory (and immediate subdirectories)
    and return recent media files sorted newest-first.
    """
    if not os.path.exists(downloads_dir):
        return []

    items = []
    try:
        with os.scandir(downloads_dir) as entries:
            for entry in entries:
                try:
                    if entry.is_file():
                        _scan_entry(entry, items)
                    elif entry.is_dir() and not entry.name.startswith("."):
                        # Scan 1-level subdirectories (e.g., gallery-dl scraped media)
                        try:
                            with os.scandir(entry.path) as sub_entries:
                                for sub in sub_entries:
                                    try:
                                        if sub.is_file():
                                            _scan_entry(sub, items)
                                    except (PermissionError, OSError):
                                        pass
                        except (PermissionError, OSError):
                            pass
                except (PermissionError, OSError):
                    pass

        items.sort(key=lambda x: x["mtime"], reverse=True)
    except Exception as e:
        print(f"Library scan error: {e}")

    return items[:limit]


def open_downloaded_file(filepath):
    """Open the file in the default OS player/application, blocking dangerous executables."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")

    norm = os.path.normpath(filepath)
    ext = os.path.splitext(norm)[1].lower()
    if ext in BLOCKED_EXEC_EXTENSIONS:
        raise PermissionError(f"Direct launch of executable file '{os.path.basename(norm)}' is blocked for system security.")

    if sys.platform.startswith("win"):
        os.startfile(norm)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", norm])
    else:
        subprocess.Popen(["xdg-open", norm])
    return True


def reveal_in_explorer(filepath):
    """Highlight the file in Windows File Explorer or native file manager."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Path not found: {filepath}")

    norm = os.path.normpath(filepath)
    if sys.platform.startswith("win"):
        cmd = f'explorer.exe /select,"{norm}"'
        subprocess.run(cmd, shell=True, timeout=10)
        return True
    elif sys.platform == "darwin":
        subprocess.run(["open", "-R", norm], timeout=10)
        return True
    else:
        parent = os.path.dirname(norm)
        subprocess.Popen(["xdg-open", parent])
        return True
