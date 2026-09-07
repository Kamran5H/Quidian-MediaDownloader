# Quidian — Media Downloader
**Developed by Kamran Ashraf**

An all-in-one media downloader combining top open-source engines into a single sidebar dashboard. Powered by 100% free and open-source tools with **zero paid APIs**.

---

## Workspaces

1. **⚡ Universal Downloader (`yt-dlp` + `aria2c` Turbo)**
   - Download 4K/2K/1080p video or high-fidelity audio from YouTube, Vimeo, TikTok, Facebook, Twitter/X, and 1,800+ websites.
   - Multi-connection turbo acceleration via `aria2c` (16 parallel chunk connections).
   - Real-time telemetry: live speed, phase stepper, ETA, percentage, and cancellation controls.
   - Built-in global media search across YouTube and the deep open web (Archive.org, Vimeo, Dailymotion).

2. **📑 Playlist & Batch Studio (`yt-dlp` Flat Inspector)**
   - Inspect full YouTube/SoundCloud playlists and channels instantly without pre-downloading.
   - Interactive tracklist table with individual checkboxes and duration tags.
   - Queue and download selected items in a controlled background batch.

3. **📸 Social Media & Gallery Scraper (`gallery-dl`)**
   - High-throughput scraper for Instagram Reels/carousels, TikTok clips, Reddit galleries, Twitter/X media, Pinterest boards, and Imgur.
   - Preserves maximum resolution and native metadata.

4. **🎙️ Subtitle Studio (`yt-dlp` Subtitle Converter)**
   - Extract online closed captions and convert them cleanly into standard `.srt` subtitles.
   - 1-click language chip presets (`en`, `ur`, `es`, `hi`, `ar`, `fr`, `de`, `ja`).

5. **🎛️ Audio Studio & Ripper (`mutagen` + `FFmpeg`)**
   - Extract and transcode lossless audio into MP3 (up to 320 kbps), FLAC, AAC, or raw WAV.
   - Custom ID3 metadata editor (Title, Artist, Album) embedded directly into exported audio tracks.

6. **🕒 Downloads Library & Launch Hub**
   - Live telemetry and file browser scanning the user's native Windows Downloads folder (`C:\Users\<User>\Downloads`).
   - File size badges, modification timestamps, category filters, and 1-click file playback.

---

## Quick Setup & Launch

### Option A: Automated One-Click Setup (Recommended)
Double-click **`setup_studio.bat`**. It will:
- Check for Conda or Python 3.10+.
- Offer to create the dedicated Conda `quidian` environment or install via `pip`.
- Verify system binaries (`ffmpeg`, `aria2c`).
- Perform an internal self-test across all engines.
- Launch the dashboard automatically.

### Option B: Conda Environment Setup
```bash
conda env create -f environment.yml
conda activate quidian
python app.py
```

### Option C: Standard Python Setup
```bash
python -m pip install -r requirements.txt
python app.py
```

---

## System Architecture

```
Media_Downloader_Project/
├── app.py                 # Central Flask Orchestrator & REST Endpoints
├── engines/               # Modular Open-Source Engine Subsystem
│   ├── __init__.py        # Unified engine export layer
│   ├── downloader.py      # Universal yt-dlp + aria2c turbo engine & anti-clickbait movie resolver
│   ├── search.py          # Multi-source concurrent search (YouTube, Dailymotion, IMDb, Archive, Web)
│   ├── stealth_sniffer.py # Stealth anti-bot bypass & stream sniffer
│   ├── playlist.py        # Flat playlist inspector & batch runner
│   ├── gallery.py         # gallery-dl social media scraper
│   ├── subtitles.py       # Subtitle extractor & pure-Python .srt converter
│   ├── audio.py           # Mutagen ID3 tagger & FFmpeg transcoder
│   └── library.py         # Native Downloads scanner & safe launcher
├── static/
│   ├── app.js             # Real-time telemetry, Quidian AdShield & UI controller
│   └── styles.css         # Quidian dark aurora glassmorphism design system
├── templates/
│   └── index.html         # Quidian dashboard UI & AdShield cinema player
├── environment.yml        # Conda environment definition
├── requirements.txt       # Python package dependencies
├── setup_studio.bat       # One-click environment installer & verifier
└── run.bat                # Instant Windows desktop launcher
```

---

## Credits
- **Developer:** Kamran Ashraf
- **Engines:** yt-dlp, gallery-dl, FFmpeg, aria2c, Mutagen, Flask


