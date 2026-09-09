"""
Hardening & Regression Suite
Quidian - Media Downloader

Every test here pins a defect that was found and fixed during the deep audit.
Each docstring names the bug so a future regression is self-explanatory.
"""

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import app as appmod
from app import app as flask_app
from engines import audio as audiomod
from engines import gallery as gallerymod
from engines import library as librarymod
from engines import search as searchmod
from engines import subtitles as submod
from engines.downloader import (
    QUALITY_FORMATS,
    _move_into,
    _score_movie_candidate,
    build_engine_opts,
    normalize_url,
)


# ---------------------------------------------------------------------------
# Flask API contract
# ---------------------------------------------------------------------------
class TestApiContract(unittest.TestCase):
    def setUp(self):
        self.client = flask_app.test_client()

    def test_non_json_post_does_not_return_html_415(self):
        """BUG: request.json raised 415 and returned an HTML page the UI could not parse."""
        res = self.client.post(
            "/api/download_video",
            data="url=https://example.com/v",
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(res.content_type.split(";")[0], "application/json")
        self.assertIn(res.status_code, (200, 400))

    def test_missing_body_is_a_clean_400(self):
        res = self.client.post("/api/social/download", json={})
        self.assertEqual(res.status_code, 400)
        self.assertIn("error", res.json)

    def test_non_numeric_count_does_not_500(self):
        """BUG: int(data['count']) raised ValueError -> HTML 500."""
        res = self.client.post("/api/search", json={"query": "test", "count": "not-a-number"})
        self.assertNotEqual(res.status_code, 500)
        self.assertEqual(res.content_type.split(";")[0], "application/json")

    def test_unknown_api_route_returns_json(self):
        """BUG: 404/500 returned HTML, so `await res.json()` threw in every caller."""
        res = self.client.get("/api/definitely-not-a-route")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.content_type.split(";")[0], "application/json")
        self.assertIn("error", res.json)

    def test_library_get_never_creates_directories(self):
        """BUG: a read-only GET ran os.makedirs() on any path the client sent."""
        target = os.path.join(tempfile.gettempdir(), "quidian_should_not_exist_%d" % time.time())
        self.assertFalse(os.path.isdir(target))
        res = self.client.get("/api/library", query_string={"dir": target})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(os.path.isdir(target), "GET /api/library created a directory")
        self.assertEqual(res.json["directory"], appmod.DOWNLOADS_DIR)

    def test_library_open_rejects_missing_path(self):
        res = self.client.post("/api/library/open", json={"path": os.path.join(tempfile.gettempdir(), "nope.mp4")})
        self.assertEqual(res.status_code, 404)

    def test_audio_rip_rejects_unknown_format(self):
        """BUG: an arbitrary 'format' string became the output file extension."""
        res = self.client.post("/api/audio/rip", json={"source": "https://example.com/a", "format": "exe"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("Unsupported audio format", res.json["error"])

    def test_subtitle_rejects_bogus_language_code(self):
        res = self.client.post("/api/download_subtitle", json={"url": "https://x/y", "lang": "../../etc"})
        self.assertEqual(res.status_code, 400)

    def test_playlist_batch_rejects_non_http_and_dedupes(self):
        with patch.object(appmod.EXECUTOR, "submit") as submit:
            res = self.client.post("/api/playlist/download", json={
                "urls": [
                    "https://example.com/a",
                    "https://example.com/a",       # duplicate
                    "file:///C:/Windows/System32",  # not http(s)
                    "javascript:alert(1)",
                ],
            })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json["count"], 1)
        self.assertEqual(res.json["skipped"], 3)
        self.assertEqual(submit.call_count, 1)

    def test_playlist_inspect_requires_url(self):
        res = self.client.post("/api/playlist/inspect", json={"url": "not a url"})
        self.assertEqual(res.status_code, 400)

    def test_cross_origin_requests_are_refused(self):
        """Drive-by POSTs from any browser tab must not reach the local API."""
        res = self.client.post(
            "/api/download_video",
            json={"url": "https://example.com/x"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(res.status_code, 403)

    def test_rebinding_host_header_is_refused(self):
        res = self.client.get("/api/health", headers={"Host": "attacker.example.com"})
        self.assertEqual(res.status_code, 403)

    def test_health_reports_real_engine_state(self):
        """BUG: gallery_dl and yt_dlp were hard-coded to True."""
        res = self.client.get("/api/health")
        body = res.json
        for key in ("ffmpeg", "ffprobe", "aria2c", "gallery_dl", "yt_dlp", "playwright"):
            self.assertIn(key, body)
            self.assertIsInstance(body[key], bool)
        self.assertEqual(body["gallery_dl"], gallerymod.check_gallery_dl_installed())

    def test_bulk_progress_endpoint(self):
        """A 250-item batch used to need 250 requests per poll tick."""
        jid = appmod._new_job("batch_item")
        res = self.client.post("/api/progress_bulk", json={"job_ids": [jid, "does-not-exist"]})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json["jobs"][jid]["status"], "queued")
        self.assertEqual(res.json["jobs"]["does-not-exist"]["status"], "expired")
        self.assertEqual(res.json["counts"]["expired"], 1)
        appmod.JOBS.pop(jid, None)

    def test_progress_for_expired_job_is_flagged(self):
        res = self.client.get("/api/progress/deadbeefdead")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json["status"], "expired")


# ---------------------------------------------------------------------------
# Job registry semantics
# ---------------------------------------------------------------------------
class TestJobRegistry(unittest.TestCase):
    def setUp(self):
        self.client = flask_app.test_client()

    def tearDown(self):
        appmod.JOBS.clear()

    def test_cancelling_a_queued_job_retires_it_immediately(self):
        jid = appmod._new_job("single")
        res = self.client.post(f"/api/cancel/{jid}")
        self.assertEqual(res.json["status"], "cancelled")
        self.assertEqual(appmod.JOBS[jid]["status"], "cancelled")

    def test_finished_job_is_not_resurrected_by_a_late_hook(self):
        """BUG: a worker thread that had not noticed cancellation could flip a
        cancelled job back to 'downloading'."""
        jid = appmod._new_job("single")
        appmod._finish_cancelled(jid)
        appmod._update_job(jid, status="downloading", percent=42.0)
        self.assertEqual(appmod.JOBS[jid]["status"], "cancelled")
        self.assertEqual(appmod.JOBS[jid]["percent"], 0.0)

    def test_error_text_containing_cancel_is_not_reported_as_cancelled(self):
        """BUG: `'cancel' in str(e).lower()` turned 'the uploader cancelled this
        premiere' into a user cancellation."""
        jid = appmod._new_job("single")
        appmod._update_job(jid, status="downloading")
        err = RuntimeError("Video unavailable: the premiere was cancelled by the uploader")
        self.assertFalse(appmod._was_cancelled(jid, err))

    def test_orphan_staging_is_reclaimed_but_only_when_stale(self):
        """A crashed run leaves multi-GB staging folders in %TEMP% forever."""
        import time as _time
        root = tempfile.gettempdir()
        stale = os.path.join(root, "studio_stage_unittest_stale")
        fresh = os.path.join(root, "studio_stage_unittest_fresh")
        foreign = os.path.join(root, "someone_elses_unittest_dir")
        for d in (stale, fresh, foreign):
            os.makedirs(d, exist_ok=True)
        try:
            old = _time.time() - (24 * 3600)
            os.utime(stale, (old, old))
            os.utime(foreign, (old, old))

            appmod.sweep_orphan_staging()

            self.assertFalse(os.path.isdir(stale), "stale staging dir was not reclaimed")
            self.assertTrue(os.path.isdir(fresh), "a fresh dir from a live run was deleted")
            self.assertTrue(os.path.isdir(foreign), "a directory we do not own was deleted")
        finally:
            import shutil as _shutil
            for d in (stale, fresh, foreign):
                _shutil.rmtree(d, ignore_errors=True)

    def test_registry_is_bounded(self):
        for _ in range(20):
            jid = appmod._new_job("batch_item")
            appmod.JOBS[jid].update(status="done", finished=0)
        with appmod.JOBS_LOCK:
            appmod._prune_jobs()
        self.assertEqual(len(appmod.JOBS), 0, "TTL-expired jobs were not pruned")


# ---------------------------------------------------------------------------
# Path handling & OS launch safety
# ---------------------------------------------------------------------------
class TestPathSafety(unittest.TestCase):
    def test_sanitize_output_path_read_only_mode(self):
        target = os.path.join(tempfile.gettempdir(), "quidian_ro_probe")
        self.assertEqual(appmod._sanitize_output_path(target, create=False), appmod.DOWNLOADS_DIR)
        self.assertFalse(os.path.isdir(target))

    def test_sanitize_rejects_null_bytes_and_none(self):
        self.assertNotIn("\x00", appmod._sanitize_output_path("C:\\Users\\test\x00bad"))
        self.assertTrue(os.path.isdir(appmod._sanitize_output_path(None)))

    def test_safe_local_path_requires_existence(self):
        self.assertIsNone(appmod._safe_local_path("C:\\nope\\nope\\nope.mp4"))
        self.assertIsNone(appmod._safe_local_path("with\x00null"))
        self.assertIsNotNone(appmod._safe_local_path(tempfile.gettempdir()))

    def test_reveal_never_builds_a_shell_string(self):
        """BUG: reveal_in_explorer ran `explorer.exe /select,"<path>"` through the
        shell, so a filename with a quote and '&' executed arbitrary commands."""
        import inspect
        source = inspect.getsource(librarymod.reveal_in_explorer)
        self.assertNotIn("shell=True", source)
        self.assertNotIn('f\'explorer.exe', source)

        evil_name = 'clip" & calc.exe & "x.mp4'
        with tempfile.TemporaryDirectory() as td:
            # The filename itself is illegal on Windows; assert on argv shape.
            with patch("subprocess.run") as run:
                path = os.path.join(td, "ok.mp4")
                open(path, "wb").write(b"x")
                librarymod.reveal_in_explorer(path)
                args, kwargs = run.call_args
                self.assertIsInstance(args[0], list)
                self.assertNotIn("shell", kwargs)
        self.assertTrue(evil_name)  # documents the payload shape

    def test_executable_launch_still_blocked_and_widened(self):
        for ext in (".exe", ".bat", ".ps1", ".lnk", ".hta", ".msi"):
            self.assertIn(ext, librarymod.BLOCKED_EXEC_EXTENSIONS)
        with tempfile.NamedTemporaryFile(suffix=".hta", delete=False) as f:
            f.write(b"x")
            p = f.name
        try:
            with self.assertRaises(PermissionError):
                librarymod.open_downloaded_file(p)
        finally:
            os.remove(p)


# ---------------------------------------------------------------------------
# Downloader
# ---------------------------------------------------------------------------
class TestDownloader(unittest.TestCase):
    def test_move_into_returns_media_not_thumbnail(self):
        """BUG: with no primary name, an unordered listdir() could return the
        cover-art .jpg as the download's file path."""
        with tempfile.TemporaryDirectory() as stage, tempfile.TemporaryDirectory() as out:
            with open(os.path.join(stage, "a_cover.jpg"), "wb") as f:
                f.write(b"0" * 5000)
            with open(os.path.join(stage, "z_movie.mp4"), "wb") as f:
                f.write(b"0" * 500000)
            primary = _move_into(out, stage, primary_name=None)
            self.assertTrue(primary.endswith("z_movie.mp4"), primary)
            self.assertFalse(os.path.isdir(stage), "staging directory was not cleaned up")

    def test_move_into_honours_explicit_primary(self):
        with tempfile.TemporaryDirectory() as stage, tempfile.TemporaryDirectory() as out:
            for name, size in (("small.mp4", 100), ("big.mkv", 90000)):
                with open(os.path.join(stage, name), "wb") as f:
                    f.write(b"0" * size)
            primary = _move_into(out, stage, primary_name="small.mp4")
            self.assertTrue(primary.endswith("small.mp4"))

    def test_move_into_never_overwrites(self):
        with tempfile.TemporaryDirectory() as stage, tempfile.TemporaryDirectory() as out:
            with open(os.path.join(out, "clip.mp4"), "wb") as f:
                f.write(b"original")
            with open(os.path.join(stage, "clip.mp4"), "wb") as f:
                f.write(b"new" * 1000)
            _move_into(out, stage)
            self.assertEqual(open(os.path.join(out, "clip.mp4"), "rb").read(), b"original")
            self.assertTrue(os.path.exists(os.path.join(out, "clip (1).mp4")))

    def test_engine_opts_have_network_timeouts(self):
        """BUG: no socket timeout meant a stalled CDN hung the worker forever."""
        opts = build_engine_opts("stage", "temp", quality="1080p", use_turbo=False)
        self.assertIn("socket_timeout", opts)
        self.assertGreater(opts["socket_timeout"], 0)
        self.assertFalse(opts["overwrites"])
        self.assertEqual(opts["trim_file_name"], 180)

    def test_engine_opts_are_accepted_by_yt_dlp(self):
        """Every option name must actually exist - a typo is silently ignored."""
        import yt_dlp
        opts = build_engine_opts("stage", "temp", quality="4k", use_turbo=False)
        opts.update(quiet=True, no_warnings=True)
        ydl = yt_dlp.YoutubeDL(opts)
        for key in ("socket_timeout", "trim_file_name", "overwrites", "extractor_retries"):
            self.assertIn(key, ydl.params, f"yt-dlp does not know option {key!r}")
        ydl.close()

    def test_aria2c_cancellation_tradeoff_is_documented(self):
        import inspect
        from engines import downloader
        source = inspect.getsource(downloader.build_engine_opts)
        self.assertIn("interrupt aria2c mid-stream", source)

    def test_aria2c_args_include_retry_and_timeout(self):
        opts = build_engine_opts("stage", "temp", use_turbo=True)
        args = opts.get("external_downloader_args")
        if args is None:
            self.skipTest("aria2c is not installed on this machine")
        self.assertIn("--max-tries=5", args)
        self.assertIn("--connect-timeout=20", args)

    def test_quality_presets_cover_the_ui_options(self):
        for q in ("best", "4k", "1080p", "720p", "audio"):
            self.assertIn(q, QUALITY_FORMATS)

    def test_normalize_url_flags_local_files(self):
        """BUG: a pasted local path was silently sent to YouTube search."""
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"x")
            p = f.name
        try:
            with self.assertRaises(ValueError):
                normalize_url(p)
        finally:
            os.remove(p)

    def test_normalize_url_schemes(self):
        self.assertEqual(normalize_url("youtube.com/watch?v=abc")[0], "https://youtube.com/watch?v=abc")
        self.assertEqual(normalize_url("just a search phrase"), (None, "just a search phrase"))

    def test_drm_error_phrasing_is_detected(self):
        """BUG: the DRM check required 'widevine'/'drm protection', so yt-dlp's
        actual message ('This video is DRM protected') never matched."""
        import inspect
        from engines import downloader
        source = inspect.getsource(downloader.download_media)
        self.assertIn("drm protected", source)
        markers = ("drm protected", "drm-protected", "widevine", "playready", "fairplay")
        real_message = "ERROR: [generic] video: This video is DRM protected".lower()
        self.assertTrue(any(m in real_message for m in markers))

    def test_longform_source_is_not_penalised_for_unknown_duration(self):
        """Archive.org items no longer carry a fabricated 5400s runtime."""
        archive_item = {
            "title": "Inception 2010",
            "duration": None,
            "uploader": "Internet Archive",
            "source": "archive",
            "longform_source": True,
        }
        plain_unknown = dict(archive_item)
        plain_unknown.pop("longform_source")
        plain_unknown["source"] = "web"
        self.assertGreater(
            _score_movie_candidate(archive_item, "Inception 2010"),
            _score_movie_candidate(plain_unknown, "Inception 2010"),
        )

    def test_short_runtime_still_disqualifies_a_movie(self):
        clip = {"title": "Inception 2010", "duration": 300, "uploader": "Someone"}
        self.assertLess(_score_movie_candidate(clip, "Inception 2010"), -900)


# ---------------------------------------------------------------------------
# Stealth interceptor
# ---------------------------------------------------------------------------
class TestStealthInterceptor(unittest.TestCase):
    def test_course_download_is_staged_not_written_to_downloads(self):
        """BUG: the course resolver pointed yt-dlp straight at the user's
        Downloads folder, so a failed lecture left a multi-GB .part file there."""
        import inspect
        from engines.stealth_sniffer import StealthStreamInterceptor
        source = inspect.getsource(StealthStreamInterceptor._resolve_course_media)
        self.assertIn("course_stage_", source)
        self.assertIn('"paths": {"home": stage_dir, "temp": chunk_dir}', source)
        self.assertIn("shutil.rmtree(stage_dir, ignore_errors=True)", source)

    def test_course_resolver_never_returns_a_phantom_path(self):
        """BUG: when nothing was produced it returned a filename it had never
        checked existed - and could pick up an unrelated pre-existing file."""
        import inspect
        from engines.stealth_sniffer import StealthStreamInterceptor
        source = inspect.getsource(StealthStreamInterceptor._resolve_course_media)
        self.assertNotIn("final_file = expected_file", source)
        self.assertIn("downloaded no playable output", source)

    def test_ffmpeg_is_killed_not_abandoned(self):
        """BUG: communicate(timeout=...) raised but left FFmpeg running, writing
        to the user's disk forever."""
        import inspect
        from engines.stealth_sniffer import StealthStreamInterceptor
        source = inspect.getsource(StealthStreamInterceptor._run_ffmpeg)
        self.assertIn("self._kill(p)", source)
        self.assertIn("timed out", source)

    def test_partial_downloads_are_removed(self):
        import inspect
        from engines.stealth_sniffer import StealthStreamInterceptor
        source = inspect.getsource(StealthStreamInterceptor._stream_to_file)
        self.assertIn("_remove_quietly(dest)", source)

    def test_manifests_are_preferred_over_the_first_response(self):
        """The first captured .mp4 is very often an ad or preview segment."""
        import inspect
        from engines.stealth_sniffer import StealthStreamInterceptor
        source = inspect.getsource(StealthStreamInterceptor._sniff_with_playwright)
        self.assertIn('priority = {"hls": 0, "dash": 1, "mp4": 2}', source)

    def test_interceptor_accepts_a_cancel_check(self):
        from engines.stealth_sniffer import StealthStreamInterceptor
        i = StealthStreamInterceptor(output_path=tempfile.gettempdir(), cancel_check=lambda: True)
        self.assertTrue(i.cancelled())
        with self.assertRaises(Exception):
            i._abort_if_cancelled()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
class TestSearch(unittest.TestCase):
    def test_search_media_does_not_block_on_a_slow_provider(self):
        """BUG: `with ThreadPoolExecutor(...)` called shutdown(wait=True) on exit,
        so a hung provider blocked the request no matter what timeout was set."""
        def slow(*a, **k):
            time.sleep(30)
            return []

        started = time.monotonic()
        with patch.object(searchmod, "search_videos", side_effect=slow), \
             patch.object(searchmod, "search_dailymotion", return_value=[]), \
             patch.object(searchmod, "search_archive_org", return_value=[]), \
             patch.object(searchmod, "search_imdb", return_value=[]), \
             patch.object(searchmod, "search_web", return_value=[]):
            searchmod.search_media("anything", count=5)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 20, f"search_media blocked for {elapsed:.1f}s on a hung provider")

    def test_include_web_false_suppresses_web_results(self):
        """BUG: the include_web flag was accepted and then ignored."""
        web_item = [{"title": "W", "url": "https://w.example/1", "source": "web", "platform": "W"}]
        with patch.object(searchmod, "search_videos", return_value=[]), \
             patch.object(searchmod, "search_dailymotion", return_value=[]), \
             patch.object(searchmod, "search_archive_org", return_value=[]), \
             patch.object(searchmod, "search_imdb", return_value=[]), \
             patch.object(searchmod, "search_web", return_value=web_item) as web:
            res = searchmod.search_media("q", count=10, include_web=False)
        web.assert_not_called()
        self.assertFalse(any(r.get("source") == "web" for r in res["results"]))

    def test_platform_counts_match_returned_results(self):
        """BUG: counts were computed before the list was trimmed, so the filter
        chips advertised more items than the grid contained."""
        items = [{"title": f"t{i}", "url": f"https://y/{i}", "source": "youtube", "platform": "YouTube"}
                 for i in range(10)]
        with patch.object(searchmod, "search_videos", return_value=items), \
             patch.object(searchmod, "search_dailymotion", return_value=[]), \
             patch.object(searchmod, "search_archive_org", return_value=[]), \
             patch.object(searchmod, "search_imdb", return_value=[]), \
             patch.object(searchmod, "search_web", return_value=[]):
            res = searchmod.search_media("q", count=3, include_web=False)
        self.assertEqual(len(res["results"]), 3)
        self.assertEqual(sum(res["platform_counts"].values()), 3)

    def test_ddg_fallback_regex_matches_real_markup(self):
        """BUG: the pattern needed re.DOTALL, so the stealth web fallback always
        returned zero results."""
        import re
        sample = (
            '<a rel="nofollow" class="result__url" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fvimeo.com%2F1">\n'
            '   vimeo.com/1\n'
            '</a>'
        )
        found = re.findall(
            r'<a[^>]+class="result__(?:url|a)"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            sample, re.S)
        self.assertEqual(len(found), 1)

    def test_junk_terms_use_word_boundaries(self):
        """BUG: substring matching made 'amv' fire inside unrelated words."""
        self.assertEqual(searchmod._junk_hits("ramvijay full movie", ""), [])
        self.assertIn("reaction", searchmod._junk_hits("first reaction to the film", ""))
        self.assertEqual(searchmod._junk_hits("movie reaction", "reaction"), [])

    def test_full_movie_intent_rejects_commentary(self):
        entry = {"title": "Inception Ending Explained", "duration": 5000, "uploader": "Someone"}
        self.assertLessEqual(searchmod._score_entry(entry, "inception full movie", "full_movie"), -9000)

    def test_no_fabricated_durations(self):
        """BUG: IMDb/Netflix/Prime cards claimed a 7200s runtime and Archive.org
        claimed 5400s, all invented."""
        cards = searchmod.search_streaming_platforms("Inception", imdb_items=[{
            "title": "Inception (2010)", "imdb_id": "tt1375666", "id": "tt1375666",
            "thumbnail": None, "cast": None, "year": "2010",
        }])
        self.assertTrue(cards)
        for c in cards:
            self.assertIsNone(c["duration"])
            self.assertEqual(c["imdb_id"], "tt1375666")

    def test_streaming_card_ignores_synthetic_imdb_id(self):
        """BUG: it read .get('id'), which falls back to a fake 'imdb_<slug>'."""
        cards = searchmod.search_streaming_platforms("Whatever", imdb_items=[{
            "title": "Whatever", "imdb_id": None, "id": "imdb_whatever",
        }])
        for c in cards:
            self.assertIsNone(c["imdb_id"])

    def test_query_cleaners_unchanged(self):
        self.assertEqual(searchmod._clean_platform_search_query("Dhoom 3 from Netflix"), "Dhoom 3")
        self.assertEqual(searchmod._clean_title_for_catalog("Inception full movie"), "Inception")


# ---------------------------------------------------------------------------
# Subtitles
# ---------------------------------------------------------------------------
class TestSubtitles(unittest.TestCase):
    def test_cue_settings_are_stripped_from_timing_line(self):
        """BUG: 'align:start position:0%' was written into the SRT timestamp,
        which breaks strict players."""
        vtt = ("WEBVTT\n\n"
               "00:00:01.500 --> 00:00:04.200 align:start position:0%\n"
               "Hello there\n")
        with tempfile.TemporaryDirectory() as td:
            v, s = os.path.join(td, "a.vtt"), os.path.join(td, "a.srt")
            open(v, "w", encoding="utf-8").write(vtt)
            self.assertTrue(submod._convert_vtt_to_srt(v, s))
            out = open(s, encoding="utf-8").read()
        self.assertIn("00:00:01,500 --> 00:00:04,200\n", out)
        self.assertNotIn("align:start", out)

    def test_cue_identifier_is_not_treated_as_dialogue(self):
        vtt = "WEBVTT\n\ncue-42\n00:00:01.000 --> 00:00:02.000\nReal text\n"
        cues = submod.parse_vtt_cues(vtt)
        self.assertEqual(cues[0][2], ["Real text"])

    def test_short_timestamps_are_normalised(self):
        cues = submod.parse_vtt_cues("WEBVTT\n\n02:15.250 --> 02:18.000\nX\n")
        self.assertEqual(cues[0][0], "00:02:15,250")

    def test_rolling_duplicate_captions_are_merged(self):
        vtt = ("WEBVTT\n\n"
               "00:00:01.000 --> 00:00:03.000\nSame line\n\n"
               "00:00:03.000 --> 00:00:05.000\nSame line\n")
        cues = submod._dedupe_rolling(submod.parse_vtt_cues(vtt))
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0][1], "00:00:05,000")

    def test_distinct_repeat_lines_are_kept(self):
        vtt = ("WEBVTT\n\n"
               "00:00:01.000 --> 00:00:03.000\nNo!\n\n"
               "00:00:09.000 --> 00:00:11.000\nNo!\n")
        cues = submod._dedupe_rolling(submod.parse_vtt_cues(vtt))
        self.assertEqual(len(cues), 2)

    def test_single_track_is_resolved_instead_of_a_wildcard(self):
        """BUG: subtitleslangs=['en.*'] made yt-dlp fetch every auto-translated
        variant (en-af, en-sq, ...), which is slow and earns an HTTP 429."""
        info = {"subtitles": {}, "automatic_captions": {
            "en": [{"url": "u"}], "en-af": [{"url": "u"}], "en-GB": [{"url": "u"}], "fr": [{"url": "u"}],
        }}
        self.assertEqual(submod.pick_subtitle_track(info, "en"), ("en", "automatic_captions"))

    def test_published_subtitles_beat_auto_captions(self):
        info = {"subtitles": {"en-US": [{"url": "u"}]}, "automatic_captions": {"en": [{"url": "u"}]}}
        self.assertEqual(submod.pick_subtitle_track(info, "en"), ("en-US", "subtitles"))

    def test_unavailable_language_resolves_to_nothing(self):
        info = {"subtitles": {"de": [{"url": "u"}]}, "automatic_captions": {}}
        self.assertEqual(submod.pick_subtitle_track(info, "ur"), (None, None))

    def test_wildcard_lang_is_gone_from_the_options(self):
        import inspect
        source = inspect.getsource(submod.extract_subtitles)
        self.assertNotIn('f"{lang}.*"', source)
        self.assertIn('"subtitleslangs": [track]', source)

    @patch("yt_dlp.YoutubeDL")
    def test_missing_subtitles_raise_instead_of_faking_success(self, mock_ydl):
        """BUG: when nothing was downloaded the call still reported success."""
        inst = MagicMock()
        inst.extract_info.return_value = {"title": "Silent Movie", "subtitles": {}, "automatic_captions": {"fr": []}}
        mock_ydl.return_value.__enter__.return_value = inst
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(RuntimeError) as ctx:
                submod.extract_subtitles("https://example.com/v", lang="en", output_path=out)
        self.assertIn("No 'en' subtitles", str(ctx.exception))
        self.assertIn("fr", str(ctx.exception))


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------
class TestAudio(unittest.TestCase):
    def test_bitrate_is_honoured_not_collapsed(self):
        """BUG: anything other than 320k silently became 128 kbps."""
        self.assertEqual(audiomod._bitrate_kbps("320k"), "320")
        self.assertEqual(audiomod._bitrate_kbps("192k"), "192")
        self.assertEqual(audiomod._bitrate_kbps("256k"), "256")
        self.assertEqual(audiomod._bitrate_kbps("garbage"), "320")

    def test_unknown_format_is_rejected(self):
        with self.assertRaises(ValueError):
            audiomod.rip_and_tag_audio("https://x/y", tempfile.gettempdir(), target_format="exe")

    def test_aac_uses_an_m4a_container(self):
        self.assertEqual(audiomod._CONTAINER_FOR["aac"], "m4a")

    def test_unique_path_never_overwrites(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "song.mp3")
            open(p, "wb").write(b"x")
            self.assertEqual(os.path.basename(audiomod._unique_path(p)), "song (1).mp3")

    def test_silent_source_gets_an_actionable_error(self):
        """BUG: a video with no audio track produced FFmpeg's opaque
        'Invalid argument' instead of an explanation."""
        with patch.object(audiomod, "probe_audio_streams", return_value=0):
          with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "silent.mp4")
            open(src, "wb").write(b"x")
            with self.assertRaises(RuntimeError) as ctx:
                audiomod.rip_and_tag_audio(src, td, target_format="mp3")
            self.assertIn("no audio track", str(ctx.exception))

    def test_ffmpeg_error_summary_picks_the_useful_lines(self):
        """BUG: blindly tailing stderr surfaced 'Metadata: | handler_name'."""
        stderr = chr(10).join([
            "  Metadata:",
            "    handler_name    : VideoHandler",
            "[out#0/mp3] Output file does not contain any stream",
            "Error opening output files: Invalid argument",
        ])
        summary = audiomod._summarise_ffmpeg_error(stderr)
        self.assertIn("does not contain any stream", summary)
        self.assertNotIn("handler_name", summary)

    def test_tagging_survives_a_file_with_no_id3_header(self):
        """BUG: MP3() parsed the MPEG stream and raised HeaderNotFoundError, so
        tags were silently dropped."""
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(b"\xff\xfb\x90\x44" + b"\x00" * 1000)
            p = f.name
        try:
            ok = audiomod._apply_audio_tags(p, "mp3", title="T", artist="A", album="B")
            self.assertTrue(ok)
            from mutagen.easyid3 import EasyID3
            self.assertEqual(EasyID3(p).get("title"), ["T"])
        finally:
            os.remove(p)


# ---------------------------------------------------------------------------
# Gallery / social scraper
# ---------------------------------------------------------------------------
class TestGallery(unittest.TestCase):
    def test_requires_http_url(self):
        with self.assertRaises(ValueError):
            gallerymod.download_social_gallery("not-a-url", tempfile.gettempdir())

    def test_both_pipes_are_drained_concurrently(self):
        """BUG: only stdout was read, so a child that filled the stderr pipe
        buffer blocked forever and the job never finished."""
        import inspect
        source = inspect.getsource(gallerymod.download_social_gallery)
        self.assertEqual(source.count("threading.Thread"), 2)
        self.assertIn("process.stderr", source)

    def test_cancellation_terminates_the_child(self):
        with tempfile.TemporaryDirectory() as out:
            start = time.monotonic()
            res = gallerymod.download_social_gallery(
                "https://example.com/gallery",
                out,
                cancel_check=lambda: True,
                timeout=60,
            )
            self.assertTrue(res.get("cancelled"))
            self.assertLess(time.monotonic() - start, 20)


# ---------------------------------------------------------------------------
# Static front end (source-level invariants)
# ---------------------------------------------------------------------------
class TestFrontEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = open(os.path.join(BASE_DIR, "static", "app.js"), encoding="utf-8").read()
        cls.html = open(os.path.join(BASE_DIR, "templates", "index.html"), encoding="utf-8").read()

    def test_no_unescaped_thumbnail_interpolation(self):
        """BUG: `<img src="${item.thumbnail}">` let a third-party search result
        close the attribute and inject an event handler."""
        self.assertNotIn('src="${item.thumbnail}"', self.js)
        self.assertIn("img.src = thumbUrl", self.js)

    def test_no_attacker_data_inside_inline_onclick_strings(self):
        """BUG: onclick="filterResultsByPlatform('${escapeHtml(p)}')" - HTML
        entity-decoding restores the quote before JS parses the handler."""
        self.assertNotIn("filterResultsByPlatform('${", self.js)
        self.assertNotIn("switchToSearchTab('${", self.js)

    def test_urls_are_validated_before_use(self):
        self.assertIn("function safeHttpUrl(", self.js)
        self.assertIn('parsed.protocol === "http:"', self.js)

    def test_window_open_is_not_globally_disabled(self):
        """BUG: AdShield replaced window.open with a null-returning stub, which
        broke the app's own 'open on Netflix / Prime' buttons."""
        self.assertIn("const nativeOpen = window.open.bind(window)", self.js)
        self.assertIn("function openExternal(", self.js)

    def test_best_match_targets_a_download_button(self):
        """BUG: it clicked the first .btn-card-action, i.e. 'Play (No Login)'
        whenever the top card was a stream card."""
        self.assertIn("topPlayableDownloadBtn", self.js)
        self.assertNotIn(".btn-card-action:not(.stream-action)", self.js)

    def test_pollers_handle_expired_jobs(self):
        self.assertGreaterEqual(self.js.count('status === "expired"'), 2)
        self.assertGreaterEqual(self.js.count("missCount"), 4)

    def test_cancelling_state_is_surfaced_not_frozen(self):
        """With aria2c turbo the transfer cannot be interrupted mid-stream, so
        the UI must say "Cancelling" rather than appear stuck."""
        self.assertIn("data.cancel && data.status !==", self.js)
        self.assertIn("j.cancel && j.status !==", self.js)
        self.assertIn("Cancelling", self.js)

    def test_batch_uses_the_bulk_progress_endpoint(self):
        self.assertIn("/api/progress_bulk", self.js)

    def test_cinema_iframe_is_sandboxed(self):
        self.assertIn('sandbox=', self.html)
        self.assertIn('iframe.setAttribute("sandbox"', self.js)
        self.assertIn('referrerpolicy', self.js.lower())

    def test_playlist_quality_control_exists(self):
        self.assertIn('id="playlist-quality"', self.html)
        self.assertIn('getElementById("playlist-quality")', self.js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
