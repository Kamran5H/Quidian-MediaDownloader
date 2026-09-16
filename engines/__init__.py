"""
Quidian - Media Downloader engines
Developed by Kamran Ashraf
"""

import warnings
warnings.filterwarnings("ignore")

from .downloader import download_media, check_aria2c_installed, verify_download_integrity
from .search import search_media, search_videos, search_web
from .playlist import inspect_playlist, download_batch
from .gallery import download_social_gallery, check_gallery_dl_installed
from .subtitles import extract_subtitles
from .audio import rip_and_tag_audio
from .library import list_downloads, open_downloaded_file, reveal_in_explorer
from .stealth_sniffer import StealthStreamInterceptor
from .series import SeriesDownloader
from .job_store import JobStore

__all__ = [
    "JobStore",
    "download_media",
    "check_aria2c_installed",
    "verify_download_integrity",
    "search_media",
    "search_videos",
    "search_web",
    "inspect_playlist",
    "download_batch",
    "download_social_gallery",
    "check_gallery_dl_installed",
    "extract_subtitles",
    "rip_and_tag_audio",
    "list_downloads",
    "open_downloaded_file",
    "reveal_in_explorer",
    "StealthStreamInterceptor",
    "SeriesDownloader",
]
