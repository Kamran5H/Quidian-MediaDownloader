"""
Regression tests for quality-selection accuracy, playlist progress reporting,
cross-platform folder handling, and desktop-opener robustness.
"""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import yt_dlp

import app as appmod
from engines import library as librarymod
from engines.downloader import (
    QUALITY_FORMATS,
    build_engine_opts,
    download_playlist_sequential,
    is_playlist_url,
)


def _yt_like_formats():
    """A typical YouTube ladder: 4K/1440p exist only as VP9/WebM, H.264 stops at 1080p."""
    def v(fid, h, ext, vc):
        return {"format_id": fid, "height": h, "width": h * 16 // 9, "ext": ext, "vcodec": vc,
                "acodec": "none", "fps": 30, "url": f"https://cdn.invalid/{fid}", "protocol": "https"}

    def a(fid, ext, ac, abr):
        return {"format_id": fid, "ext": ext, "acodec": ac, "vcodec": "none", "abr": abr,
                "url": f"https://cdn.invalid/{fid}", "protocol": "https"}

    return [
        v("313", 2160, "webm", "vp9"), v("271", 1440, "webm", "vp9"),
        v("137", 1080, "mp4", "avc1.640028"), v("248", 1080, "webm", "vp9"),
        v("136", 720, "mp4", "avc1.4d401f"), v("247", 720, "webm", "vp9"),
        v("135", 480, "mp4", "avc1.4d401e"),
        a("140", "m4a", "mp4a.40.2", 128), a("251", "webm", "opus", 160),
    ]


def _selected_format_id(quality):
    """Run yt-dlp's real format selection (no network) with our options."""
    opts = build_engine_opts("unused_stage", "unused_temp", quality=quality, use_turbo=False)
    params = {
        "format": opts["format"],
        "format_sort": opts["format_sort"],
        "merge_output_format": opts.get("merge_output_format"),
        "quiet": True,
        "no_warnings": True,
        "simulate": True,
    }
    info = {"id": "abc", "title": "t", "extractor": "youtube", "extractor_key": "Youtube",
            "webpage_url": "https://www.youtube.com/watch?v=abc", "formats": _yt_like_formats()}
    with yt_dlp.YoutubeDL(params) as ydl:
        result = ydl.process_ie_result(info, download=False)
    return result["format_id"]


class TestQualitySelection(unittest.TestCase):
    def test_4k_picks_real_2160p_even_when_only_vp9(self):
        """BUG: `[ext=mp4]` in the first alternative matched the 1080p H.264
        stream, so the '4K' preset never delivered 4K on YouTube."""
        self.assertEqual(_selected_format_id("4k"), "313+140")

    def test_best_picks_highest_resolution(self):
        self.assertEqual(_selected_format_id("best"), "313+140")

    def test_1080p_and_below_prefer_universally_playable_h264(self):
        self.assertEqual(_selected_format_id("1080p"), "137+140")
        self.assertEqual(_selected_format_id("720p"), "136+140")
        self.assertEqual(_selected_format_id("480p"), "135+140")

    def test_every_preset_has_an_unconditional_fallback(self):
        for q, spec in QUALITY_FORMATS.items():
            self.assertTrue(spec.endswith(("/b", "/best")), q)

    def test_mp3_is_true_320kbps_as_advertised(self):
        opts = build_engine_opts("s", "t", quality="audio", use_turbo=False)
        extract = next(p for p in opts["postprocessors"] if p["key"] == "FFmpegExtractAudio")
        self.assertEqual(extract["preferredquality"], "320")


class TestPlaylistDetection(unittest.TestCase):
    def test_video_opened_from_a_mix_is_a_single_video(self):
        self.assertFalse(is_playlist_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ&start_radio=1"))
        self.assertFalse(is_playlist_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=WL"))

    def test_real_playlists_still_detected(self):
        self.assertTrue(is_playlist_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1234567890ABCDEF"))
        self.assertTrue(is_playlist_url("https://www.youtube.com/playlist?list=PL1234567890ABCDEF"))
        self.assertTrue(is_playlist_url("https://www.youtube.com/playlist?list=OLAK5uy_album"))


class TestPlaylistProgress(unittest.TestCase):
    """The server-side hook maps 'finished' to 95% and audio-only streams to
    80%+, so the playlist wrapper must never forward those per-item signals."""

    @patch("engines.downloader.verify_download_integrity", return_value=(True, None))
    @patch("engines.playlist.inspect_playlist")
    @patch("yt_dlp.YoutubeDL")
    def test_progress_is_monotonic_and_never_jumps_to_95(self, mock_ydl, mock_inspect, _verify):
        entries = [{"url": f"https://youtube.com/watch?v=v{i}", "id": f"v{i}", "title": f"Part {i}"}
                   for i in range(1, 5)]
        mock_inspect.return_value = {"title": "Series", "entries": entries}

        def fake_extract_info(url, download=True):
            opts = mock_ydl.call_args[0][0]
            vid = url.rsplit("=", 1)[1]
            for hook in opts.get("progress_hooks", []):
                for info in ({"vcodec": "avc1", "acodec": "none"}, {"vcodec": "none", "acodec": "mp4a"}):
                    hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100, "info_dict": info})
                    hook({"status": "finished", "downloaded_bytes": 100, "total_bytes": 100, "info_dict": info})
            path = os.path.join(opts["paths"]["home"], f"{vid} [{vid}].mp4")
            with open(path, "wb") as f:
                f.write(b"x")
            return {"filepath": path}

        instance = MagicMock()
        instance.extract_info.side_effect = fake_extract_info
        mock_ydl.return_value.__enter__.return_value = instance

        job_id = appmod._new_job(job_type="test")
        hook = appmod._make_progress_hook(job_id)
        seen = []

        def recording_hook(d):
            hook(d)
            seen.append(appmod.JOB_STORE.get_job(job_id)["percent"])

        with tempfile.TemporaryDirectory() as dest:
            res = download_playlist_sequential("https://youtube.com/playlist?list=PLx", dest,
                                               progress_hook=recording_hook)
        self.assertEqual(res["success_count"], 4)
        self.assertEqual(seen, sorted(seen))
        # After the first of four items, overall progress must be ~25%, not 80-95%.
        first_item_max = max(seen[:4])
        self.assertLessEqual(first_item_max, 30.0)
        self.assertLess(max(seen), 95.0)

    @patch("engines.downloader.verify_download_integrity", return_value=(True, None))
    @patch("engines.playlist.inspect_playlist")
    def test_skipping_existing_items_does_not_fake_completion(self, mock_inspect, _verify):
        entries = [{"url": f"https://youtube.com/watch?v=v{i}", "id": f"v{i}", "title": f"Part {i}"}
                   for i in range(1, 5)]
        mock_inspect.return_value = {"title": "Series", "entries": entries}
        statuses = []
        with tempfile.TemporaryDirectory() as dest:
            for i in range(1, 5):
                open(os.path.join(dest, f"0{i} - Part {i} [v{i}].mp4"), "wb").write(b"x")
            res = download_playlist_sequential("https://youtube.com/playlist?list=PLx", dest,
                                               progress_hook=lambda d: statuses.append(d["status"]))
        self.assertEqual(res["success_count"], 4)
        self.assertEqual(len(statuses), 4)
        self.assertNotIn("finished", statuses)


class TestPathsAndFolders(unittest.TestCase):
    def setUp(self):
        appmod.app.config["TESTING"] = True
        self.client = appmod.app.test_client()

    def test_tilde_output_path_resolves_to_home(self):
        home = os.path.expanduser("~")
        self.assertEqual(appmod._sanitize_output_path("~", create=False), os.path.normpath(home))
        self.assertEqual(appmod._safe_local_path("~"), os.path.realpath(home))

    def test_create_offered_next_to_a_prefix_match(self):
        """Typing 'Vid' beside an existing 'Videos' must still offer to create 'Vid'."""
        with tempfile.TemporaryDirectory() as td:
            os.mkdir(os.path.join(td, "Videos"))
            target = os.path.join(td, "Vid")
            res = self.client.post("/api/search_folders", json={"query": target}).get_json()
            creatable = [r for r in res["results"] if r["can_create"]]
            self.assertEqual([r["path"] for r in creatable], [os.path.normpath(target)])
            self.assertTrue(any(r["path"].endswith("Videos") for r in res["results"]))

    def test_existing_folder_is_never_offered_for_creation(self):
        with tempfile.TemporaryDirectory() as td:
            res = self.client.post("/api/search_folders", json={"query": td}).get_json()
            self.assertFalse(any(r["can_create"] for r in res["results"]))

    def test_favicon_is_served(self):
        res = self.client.get("/favicon.ico")
        self.assertEqual(res.status_code, 200)
        self.assertGreater(len(res.data), 0)


class TestDesktopOpener(unittest.TestCase):
    def test_missing_opener_gives_actionable_error(self):
        with tempfile.TemporaryDirectory() as td, \
                patch.object(librarymod.sys, "platform", "linux"), \
                patch.object(librarymod.shutil, "which", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                librarymod.reveal_in_explorer(td)
            self.assertIn("xdg-utils", str(ctx.exception))

    def test_reveal_endpoint_reports_missing_opener_as_json_400(self):
        client = appmod.app.test_client()
        with tempfile.TemporaryDirectory() as td, \
                patch.object(librarymod.sys, "platform", "linux"), \
                patch.object(librarymod.shutil, "which", return_value=None):
            res = client.post("/api/library/reveal", json={"path": td})
        self.assertEqual(res.status_code, 400)
        self.assertIn("file manager", res.get_json()["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
