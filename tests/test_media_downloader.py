"""
Comprehensive Scrutiny & Regression Test Suite
Quidian - Media Downloader
Developed by Kamran Ashraf
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Ensure project root is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from engines.stealth_sniffer import StealthStreamInterceptor
from engines.library import open_downloaded_file, BLOCKED_EXEC_EXTENSIONS, MEDIA_EXTENSIONS
from engines.subtitles import _convert_vtt_to_srt, extract_subtitles
from engines.downloader import _score_movie_candidate
from engines.search import _clean_platform_search_query, _clean_title_for_catalog
from engines.audio import _apply_audio_tags
from mutagen.easyid3 import EasyID3
from app import app, _sanitize_output_path, _prune_jobs, JOBS


class TestCodebaseScrutiny(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()

    @patch("yt_dlp.YoutubeDL")
    def test_course_resolver_unbound_variable_fixed(self, mock_ydl):
        """Verify _resolve_course_media does not raise UnboundLocalError when target_duration is unset."""
        mock_instance = MagicMock()
        mock_instance.extract_info.return_value = {"title": "Test Lecture", "id": "123"}
        mock_instance.prepare_filename.return_value = "Test_Lecture.mp4"
        mock_ydl.return_value.__enter__.return_value = mock_instance

        interceptor = StealthStreamInterceptor()

        def resolve(url):
            """Return the result, or the exception type, but never a NameError."""
            try:
                res = interceptor._resolve_course_media(url)
                self.assertTrue(res is None or isinstance(res, dict))
                return res
            except (UnboundLocalError, NameError) as e:
                self.fail(f"Unbound name in the course resolver: {e}")
            except RuntimeError:
                # Expected with a mocked yt-dlp: no output file is produced, and
                # the resolver now reports that instead of inventing a path.
                return None

        resolve("https://www.coursera.org/learn/machine-learning/lecture/xyz")
        resolve("https://www.skillshare.com/classes/Graphic-Design-Basics/12345")

    def test_library_executable_blocking_security(self):
        """Verify open_downloaded_file blocks .exe, .bat, .cmd and dangerous executables."""
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as f:
            f.write(b"mock executable")
            exe_path = f.name
        
        try:
            with self.assertRaises(PermissionError):
                open_downloaded_file(exe_path)
        finally:
            if os.path.exists(exe_path):
                os.remove(exe_path)

        with tempfile.NamedTemporaryFile(suffix=".bat", delete=False) as f:
            f.write(b"@echo off")
            bat_path = f.name
        try:
            with self.assertRaises(PermissionError):
                open_downloaded_file(bat_path)
        finally:
            if os.path.exists(bat_path):
                os.remove(bat_path)

    def test_pure_python_vtt_to_srt_conversion(self):
        """Verify WebVTT to SRT conversion replaces dot timestamps with commas and formats cue blocks."""
        vtt_content = """WEBVTT
Kind: captions
Language: en

00:00:01.500 --> 00:00:04.200
<c>Hello</c> and welcome to the video!

00:00:04.500 --> 00:00:08.000
<i>This is a test subtitle.</i>
"""
        with tempfile.TemporaryDirectory() as td:
            vtt_file = os.path.join(td, "test.vtt")
            srt_file = os.path.join(td, "test.srt")
            with open(vtt_file, "w", encoding="utf-8") as f:
                f.write(vtt_content)

            success = _convert_vtt_to_srt(vtt_file, srt_file)
            self.assertTrue(success)
            self.assertTrue(os.path.exists(srt_file))

            with open(srt_file, "r", encoding="utf-8") as f:
                srt_out = f.read()

            self.assertIn("00:00:01,500 --> 00:00:04,200", srt_out)
            self.assertIn("00:00:04,500 --> 00:00:08,000", srt_out)
            self.assertIn("Hello and welcome to the video!", srt_out)
            self.assertIn("This is a test subtitle.", srt_out)
            self.assertNotIn("WEBVTT", srt_out)

    @patch("yt_dlp.YoutubeDL")
    def test_subtitle_isolation_preserves_user_files(self, mock_ydl):
        """Subtitle extraction stages in a temp dir and NEVER touches pre-existing
        .vtt files in the destination folder.

        Updated for the two-pass extractor: pass 1 resolves which caption track
        exists (download=False), pass 2 downloads only that track.
        """
        with tempfile.TemporaryDirectory() as user_dir:
            # A user .vtt file that must not be converted or deleted.
            existing_user_vtt = os.path.join(user_dir, "my_existing_movie.vtt")
            with open(existing_user_vtt, "w", encoding="utf-8") as f:
                f.write("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nKeep me safe!")

            info = {
                "title": "Sample Movie",
                "subtitles": {"en": [{"url": "https://example.com/en.vtt", "ext": "vtt"}]},
                "automatic_captions": {},
            }

            def fake_extract_info(url, download=True):
                if not download:
                    return info
                # Pass 2: drop a caption file wherever yt-dlp was told to stage.
                args, kwargs = mock_ydl.call_args
                opts = args[0] if args else kwargs.get("ydl_opts", {})
                stage_dir = (opts.get("paths") or {}).get("home", "")
                if stage_dir and os.path.isdir(stage_dir):
                    with open(os.path.join(stage_dir, "downloaded.en.vtt"), "w", encoding="utf-8") as f:
                        f.write("WEBVTT\n\n00:00:05.100 --> 00:00:09.200\nDownloaded caption")
                return info

            mock_instance = MagicMock()
            mock_instance.extract_info.side_effect = fake_extract_info
            mock_ydl.return_value.__enter__.return_value = mock_instance

            res = extract_subtitles("https://www.youtube.com/watch?v=mock123", lang="en", output_path=user_dir)
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["track"], "en")
            self.assertFalse(res["auto_generated"])

            # The user's pre-existing .vtt must still be there, untouched.
            self.assertTrue(os.path.exists(existing_user_vtt), "Pre-existing .vtt file was deleted!")
            with open(existing_user_vtt, "r", encoding="utf-8") as f:
                self.assertIn("Keep me safe!", f.read())

            srt_files = [f for f in os.listdir(user_dir) if f.endswith(".srt")]
            self.assertGreaterEqual(len(srt_files), 1)

    def test_audio_id3_tagging(self):
        """Verify _apply_audio_tags correctly writes ID3 metadata to audio files."""
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            # Write minimal valid MP3 frame
            f.write(b"\xff\xfb\x90\x44" + b"\x00" * 1000)
            mp3_path = f.name

        try:
            _apply_audio_tags(mp3_path, target_format="mp3", title="Neon Horizon", artist="CyberPulse", album="Retrowave 2026")
            audio = EasyID3(mp3_path)
            self.assertEqual(audio.get("title"), ["Neon Horizon"])
            self.assertEqual(audio.get("artist"), ["CyberPulse"])
            self.assertEqual(audio.get("album"), ["Retrowave 2026"])
        finally:
            if os.path.exists(mp3_path):
                os.remove(mp3_path)

    def test_movie_candidate_scoring_hardened(self):
        """Verify strict scoring: word boundaries, missing primary token penalty, and year mismatch penalty."""
        canonical = "Inception 2010"

        # Conflicting year: 2024 instead of 2010
        mismatched_year = {"title": "Inception 2024 Teaser", "duration": 5000, "uploader": "Random"}
        score_mismatch = _score_movie_candidate(mismatched_year, canonical)
        # Should be heavily penalized
        self.assertLess(score_mismatch, 0)

        # Exact match with matching year and full duration
        exact_match = {"title": "Inception 2010 Full Movie HD", "duration": 8800, "uploader": "Warner"}
        score_exact = _score_movie_candidate(exact_match, canonical)
        self.assertGreater(score_exact, 100)

        # Word boundary test: "Don" should not match "London"
        short_title = "Don"
        unrelated = {"title": "Travel to London Vlog", "duration": 7000, "uploader": "Traveler"}
        score_unrelated = _score_movie_candidate(unrelated, short_title)
        self.assertLess(score_unrelated, 0)

        # Junk words should disqualify even zero duration
        junk_candidate = {"title": "Inception 2010 Official Trailer", "duration": 0, "uploader": "MovieClips"}
        score_junk = _score_movie_candidate(junk_candidate, canonical)
        self.assertLess(score_junk, -200)

    def test_catalog_query_cleaners(self):
        """Verify query cleaning extracts canonical titles properly."""
        q1 = "Dhoom 3 from Netflix"
        self.assertEqual(_clean_platform_search_query(q1), "Dhoom 3")

        q2 = "Inception full movie"
        self.assertEqual(_clean_title_for_catalog(q2), "Inception")

        q3 = "Interstellar on Amazon Prime Video"
        self.assertEqual(_clean_platform_search_query(q3), "Interstellar")

    def test_path_sanitization(self):
        """Verify _sanitize_output_path rejects null bytes and falls back safely."""
        self.assertNotIn("\x00", _sanitize_output_path("C:\\Users\\test\x00bad"))
        self.assertTrue(os.path.isabs(_sanitize_output_path("")))
        self.assertTrue(os.path.isdir(_sanitize_output_path(None)))

    def test_media_extensions_coverage(self):
        """Verify extended media extensions include common formats."""
        self.assertIn(".m4v", MEDIA_EXTENSIONS["video"])
        self.assertIn(".wmv", MEDIA_EXTENSIONS["video"])
        self.assertIn(".bmp", MEDIA_EXTENSIONS["image"])
        self.assertIn(".jfif", MEDIA_EXTENSIONS["image"])

    def test_flask_endpoints(self):
        """Verify health, preset_paths, error handling, and resolve_imdb proxy."""
        res_health = self.client.get("/api/health")
        self.assertEqual(res_health.status_code, 200)
        self.assertIn("yt_dlp", res_health.json)

        res_presets = self.client.get("/api/preset_paths")
        self.assertEqual(res_presets.status_code, 200)
        self.assertIn("downloads", res_presets.json)

        # Empty search query -> 400
        res_empty = self.client.post("/api/search", json={})
        self.assertEqual(res_empty.status_code, 400)

        # /api/resolve_imdb missing title -> 400
        res_imdb_no_title = self.client.get("/api/resolve_imdb")
        self.assertEqual(res_imdb_no_title.status_code, 400)

        # /api/resolve_imdb with valid movie title -> 200 and json with imdb_id
        res_imdb = self.client.get("/api/resolve_imdb?title=The+Matrix")
        self.assertEqual(res_imdb.status_code, 200)
        self.assertIn("imdb_id", res_imdb.json)


if __name__ == '__main__':
    unittest.main(verbosity=2)
