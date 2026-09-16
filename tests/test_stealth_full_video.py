import os
import unittest
from unittest.mock import patch, MagicMock
from engines.stealth_sniffer import _probe_stream_metadata, StealthStreamInterceptor
from app import app


class TestStealthStreamProbing(unittest.TestCase):
    def test_trailer_url_penalized(self):
        meta = _probe_stream_metadata("https://cdn.example.com/videos/movie_trailer_1080p.m3u8", stream_type="hls")
        self.assertTrue(meta["is_trailer"])
        self.assertLess(meta["score"], 0)

    def test_teaser_preview_markers_flagged(self):
        for keyword in ["teaser", "preview", "promo", "sample", "clip_"]:
            meta = _probe_stream_metadata(f"https://example.com/{keyword}_video.mp4", stream_type="mp4")
            self.assertTrue(meta["is_trailer"])
            self.assertLess(meta["score"], 0)

    @patch("engines.stealth_sniffer._safe_cffi_get")
    def test_hls_duration_probing_distinguishes_trailer_from_full(self, mock_get):
        # 30-second preview manifest
        short_manifest = (
            "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
            "#EXTINF:10.0,\nseg1.ts\n#EXTINF:10.0,\nseg2.ts\n#EXTINF:10.0,\nseg3.ts\n#EXT-X-ENDLIST\n"
        )
        mock_resp_short = MagicMock(status_code=200, text=short_manifest)
        mock_get.return_value = mock_resp_short

        meta_short = _probe_stream_metadata("https://example.com/short_stream.m3u8", stream_type="hls")
        self.assertEqual(meta_short["duration"], 30.0)
        self.assertTrue(meta_short["is_trailer"])
        self.assertLess(meta_short["score"], 0)

        # 20-minute full video manifest
        long_segments = "\n".join(["#EXTINF:10.0,\nseg.ts" for _ in range(120)])
        long_manifest = f"#EXTM3U\n#EXT-X-VERSION:3\n{long_segments}\n#EXT-X-ENDLIST\n"
        mock_resp_long = MagicMock(status_code=200, text=long_manifest)
        mock_get.return_value = mock_resp_long

        meta_long = _probe_stream_metadata("https://example.com/full_feature.m3u8", stream_type="hls")
        self.assertEqual(meta_long["duration"], 1200.0)
        self.assertFalse(meta_long["is_trailer"])
        self.assertGreater(meta_long["score"], 500)


class TestStealthStreamInterceptor(unittest.TestCase):
    def test_ffmpeg_command_receives_browser_headers(self):
        interceptor = StealthStreamInterceptor(output_path="/tmp")
        headers = {
            "Referer": "https://secured.platform.com/watch/123",
            "Origin": "https://secured.platform.com",
            "Cookie": "session=abc123xyz",
        }
        with patch.object(interceptor, "_run_ffmpeg", return_value=(0, "")) as mock_run, \
             patch.object(interceptor, "_unique_path", side_effect=lambda p: p), \
             patch("os.path.exists", side_effect=lambda p: True if "Test_Video" in str(p) else os.path.exists(p)), \
             patch("os.path.getsize", return_value=1024 * 1024):
            interceptor._download_stream_ffmpeg("https://cdn.example.com/hls.m3u8", title="Test_Video", headers=headers)
            self.assertTrue(mock_run.called)
            cmd = mock_run.call_args[0][0]
            header_idx = cmd.index("-headers")
            header_str = cmd[header_idx + 1]
            self.assertIn("Referer: https://secured.platform.com/watch/123", header_str)
            self.assertIn("Origin: https://secured.platform.com", header_str)
            self.assertIn("Cookie: session=abc123xyz", header_str)


class TestLibraryOpenByName(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    @patch("app.open_downloaded_file", return_value=True)
    def test_open_by_name_finds_file_in_downloads(self, mock_open):
        with patch("app.DOWNLOADS_DIR", "/tmp/fake_downloads"), \
             patch("os.path.isdir", return_value=True), \
             patch("os.walk") as mock_walk, \
             patch("app._safe_local_path") as mock_safe:
            mock_walk.return_value = [
                ("/tmp/fake_downloads", [], ["The_Matrix_1080p.mp4", "Sample_Document.pdf"])
            ]
            mock_safe.side_effect = lambda p: p if p and "The_Matrix" in p else None

            res = self.client.post("/api/open_by_name", json={"name": "Matrix"})
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data["status"], "opened")
            self.assertTrue(mock_open.called)
