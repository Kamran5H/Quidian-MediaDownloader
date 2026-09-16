# 🎬 Quidian — All-in-One 4K Media & Playlist Turbo Downloader

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://github.com/Kamran5H/Quidian-MediaDownloader)
[![Engine: yt-dlp](https://img.shields.io/badge/Core-yt--dlp%20Master-FF0000?style=for-the-badge&logo=youtube&logoColor=white)](https://github.com/yt-dlp/yt-dlp)
[![Turbo: aria2c](https://img.shields.io/badge/Turbo-aria2c%2016x%20Multi--Thread-10B981?style=for-the-badge)](https://aria2.github.io/)
[![Video Quality](https://img.shields.io/badge/Video-Up%20to%208K%20HDR%20%7C%2060FPS-8B5CF6?style=for-the-badge)](https://github.com/Kamran5H/Quidian-MediaDownloader)

**Enterprise-grade media acquisition suite powered by yt-dlp, multi-connection aria2c acceleration, automated subtitle extraction, and modern web & desktop dashboards.**

[Features](#-key-features) • [Architecture](#-architecture) • [Supported Formats](#-supported-formats--resolutions) • [Quickstart](#-quick-start) • [License](#-license)

</div>

---

## 🌟 Executive Overview

**Quidian Media Downloader** is a high-performance media downloader engineered to extract videos, audio tracks, full playlists, and multi-language subtitles from 1,000+ streaming sites. By pairing **yt-dlp** with **aria2c multi-connection acceleration** (up to 16 concurrent HTTP/HTTPS segments per file) and automated **FFmpeg post-processing**, Quidian achieves maximum saturation of high-speed internet connections while preserving pristine 4K/8K HDR video and uncompressed audio.

---

## 🚀 Key Features

- **⚡ aria2c Turbo Multi-Threading**: Splits downloads across 16 parallel socket streams, achieving up to 10x faster download speeds than standard browser downloads.
- **🎬 Complete Resolution Spectrum**: Flawlessly downloads 1080p, 2K, 4K, and 8K HDR at 60 FPS with VP9, AV1, or H.264 codecs.
- **📑 Multi-Language Subtitle Extractor**: Automatically retrieves embedded and auto-generated subtitles, translating and saving them in pristine `.srt` or `.vtt` format.
- **🎵 Lossless Audio Extraction**: Extracts pristine MP3, M4A, FLAC, and WAV audio streams with embedded album art and metadata.
- **📋 Batch & Playlist Ingestion**: Ingests complete YouTube playlists, course catalogs, or channel archives with automated index numbering and resume support.
- **🖥️ Dual Interface Options**: Run via an interactive modern Web GUI dashboard or launch the lightweight desktop client with `run.bat`.

---

## 🏗️ Architecture

```mermaid
flowchart TD
    A[URL: Single Video / Playlist / Channel] --> B[Quidian Dispatcher: app.py]
    B --> C[yt-dlp Format Analyzer & Extractor]
    C -->|Split into 16 Parallel Connections| D[aria2c Turbo Download Engine]
    D -->|Raw Video & Audio Chunks| E[Chunk Reassembler]
    E -->|Mux Video + Audio + Subtitles| F[FFmpeg Post-Processor]
    F --> G[(Pristine High-Bitrate Media File)]
    B <--> H[Web Dashboard / Tkinter Interface]
```

---

## 📁 Repository Structure

```text
Quidian-MediaDownloader/
├── app.py                      # Primary web dashboard server & API controller
├── run.bat                     # Turnkey Windows desktop launcher
├── setup_studio.bat            # Automated dependency installer
├── launch_quidian_media.vbs    # Silent background launcher
├── Video_Downloader/           # Dedicated video extraction engine
├── Subtitle_Downloader/        # Multi-language subtitle parser
├── engines/                    # yt-dlp, aria2c, and FFmpeg wrappers
├── templates/ & static/        # Web dashboard UI assets and styling
├── requirements.txt            # Python dependencies
├── .gitignore                  # Media cache & output exclusions
└── LICENSE                     # Open-source MIT License
```

---

## ⚡ Quick Start

### 1. Installation
```bash
git clone https://github.com/Kamran5H/Quidian-MediaDownloader.git
cd Quidian-MediaDownloader

# Run automated Windows setup
setup_studio.bat
```

### 2. Launch
Double click [`run.bat`](run.bat) or execute:
```bash
python app.py
```
Open [http://localhost:5000](http://localhost:5000) to start downloading media at maximum speeds!

---

## 📜 License

This project is open-source and released under the [MIT License](LICENSE).  
Copyright (c) 2024-2026 **Kamran Ashraf**.
