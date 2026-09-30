"""
Quidian - Core Engine Live Self-Updater
Keeps yt-dlp, gallery-dl, and core extractors automatically fresh against streaming platform changes.
Developed by Kamran Ashraf
"""

import sys
import json
import subprocess
import urllib.request


def _get_pypi_version(package_name, timeout=5):
    """Fetch latest released package version directly from PyPI JSON API."""
    url = f"https://pypi.org/pypi/{package_name}/json"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Quidian-MediaDownloader-Updater/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("info", {}).get("version")
    except Exception:
        return None


def get_engine_versions():
    """
    Check current installed versions vs latest remote versions for critical media engines.
    Returns status dict with installed and latest versions, and update_available flags.
    """
    results = {}

    # 1. yt-dlp
    yt_installed = None
    try:
        import yt_dlp.version
        yt_installed = getattr(yt_dlp.version, "__version__", None)
    except Exception:
        pass
    yt_latest = _get_pypi_version("yt-dlp")
    results["yt-dlp"] = {
        "installed": yt_installed,
        "latest": yt_latest,
        "update_available": bool(yt_latest and yt_installed and str(yt_installed).strip() != str(yt_latest).strip()),
        "description": "Primary 4K media extraction engine and streaming protocol parser",
    }

    # 2. gallery-dl
    gdl_installed = None
    try:
        import gallery_dl
        gdl_installed = getattr(gallery_dl, "__version__", None)
    except Exception:
        pass
    gdl_latest = _get_pypi_version("gallery-dl")
    results["gallery-dl"] = {
        "installed": gdl_installed,
        "latest": gdl_latest,
        "update_available": bool(gdl_latest and gdl_installed and str(gdl_installed).strip() != str(gdl_latest).strip()),
        "description": "Social media gallery & image board scraper",
    }

    # 3. curl_cffi
    cffi_installed = None
    try:
        import curl_cffi
        cffi_installed = getattr(curl_cffi, "__version__", None)
    except Exception:
        pass
    results["curl_cffi"] = {
        "installed": cffi_installed,
        "latest": None,
        "update_available": False,
        "description": "Browser TLS impersonation & Cloudflare evasion engine",
    }

    has_any_update = any(pkg.get("update_available") for pkg in results.values())
    return {
        "status": "success",
        "has_updates": has_any_update,
        "engines": results,
    }


def upgrade_engine(package="yt-dlp", progress_callback=None):
    """
    Perform a clean, in-process pip upgrade of the target engine.
    """
    allowed_packages = {"yt-dlp", "gallery-dl", "curl_cffi"}
    if package not in allowed_packages:
        raise ValueError(f"Package '{package}' is not an authorized core engine.")

    if progress_callback:
        progress_callback(10, f"Contacting repository to upgrade {package}...")

    cmd = [
        sys.executable,
        "-m", "pip",
        "install",
        "--upgrade",
        "--no-warn-script-location",
        package,
    ]

    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )

    if progress_callback:
        progress_callback(40, f"Downloading & compiling latest {package} wheels...")

    stdout, stderr = p.communicate(timeout=180)

    if p.returncode != 0:
        err_msg = (stderr or stdout or "Unknown error").strip()
        raise RuntimeError(f"Failed to update {package}: {err_msg}")

    # Re-verify newly installed version
    new_version = None
    try:
        mod_name = package.replace("-", "_")
        code = f"import {mod_name}; print(getattr({mod_name}, '__version__', getattr(getattr({mod_name}, 'version', None), '__version__', 'unknown')))"
        chk = subprocess.run(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        if chk.returncode == 0:
            new_version = chk.stdout.strip()
    except Exception:
        pass

    if progress_callback:
        progress_callback(100, f"Successfully upgraded {package} to {new_version or 'latest'}!")

    return {
        "status": "success",
        "package": package,
        "version": new_version,
        "log": stdout.strip(),
    }
