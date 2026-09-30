"""
Comprehensive Test Suite for Ads-Free, Stealth Armor, Media Streaming, and Compressor Studio
Quidian - Media Downloader
"""

import os
import re
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app import app as flask_app, JOB_STORE
from engines import converter, updater, stealth_sniffer
from engines.downloader import build_engine_opts


class TestAdsFreeSponsorBlock(unittest.TestCase):
    """Verify SponsorBlock and ad excision configuration."""

    def test_sponsorblock_enabled_by_default(self):
        opts = build_engine_opts("dummy_stage", "dummy_temp", remove_sponsors=True)
        postprocessors = opts.get("postprocessors", [])
        pps_keys = [pp.get("key") for pp in postprocessors if isinstance(pp, dict)]
        self.assertIn("SponsorBlock", pps_keys)
        self.assertIn("ModifyChapters", pps_keys)

        sb = next(pp for pp in postprocessors if pp.get("key") == "SponsorBlock")
        self.assertEqual(sb.get("when"), "after_filter")

        mc = next(pp for pp in postprocessors if pp.get("key") == "ModifyChapters")
        self.assertIn("remove_sponsor_segments", mc)

    def test_sponsorblock_disabled_when_flag_false(self):
        opts = build_engine_opts("dummy_stage", "dummy_temp", remove_sponsors=False)
        postprocessors = opts.get("postprocessors", [])
        pps_keys = [pp.get("key") for pp in postprocessors if isinstance(pp, dict)]
        self.assertNotIn("SponsorBlock", pps_keys)
        self.assertNotIn("ModifyChapters", pps_keys)

    def test_stealth_sniffer_ad_filters(self):
        """Verify network-level ad-blocking filters in stealth sniffer."""
        ad_urls = [
            "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js",
            "https://adservice.google.com/adsid/google/ui",
            "https://securepubads.g.doubleclick.net/gampad/ads",
            "https://cdn.adnxs.com/v/vpaid.js",
            "https://track.clickfunnels.com/event",
            "https://popads.net/serve/popunder.js",
            "https://video-stream.example.com/preroll/ad_segment_1080p.m3u8",
            "https://stream.host.com/vast/interstitial_commercial.ts",
        ]
        clean_urls = [
            "https://manifest.googlevideo.com/api/manifest/hls_variant/video.m3u8",
            "https://example.com/videos/master.mpd",
            "https://cdn.vidsrc.me/stream/720p.m3u8",
        ]

        for url in ad_urls:
            is_ad = bool(stealth_sniffer.AD_AND_TRACKER_DOMAINS.search(url) or
                         stealth_sniffer.AD_PATH_KEYWORDS.search(url))
            self.assertTrue(is_ad, f"Expected ad filter to match: {url}")

        for url in clean_urls:
            is_ad = bool(stealth_sniffer.AD_AND_TRACKER_DOMAINS.search(url) or
                         stealth_sniffer.AD_PATH_KEYWORDS.search(url))
            self.assertFalse(is_ad, f"Clean media stream should NOT match ad filter: {url}")


class TestStealthArmorAndCookies(unittest.TestCase):
    """Verify stealth headers and browser cookie vault integration."""

    def test_cookies_from_browser_in_engine_opts(self):
        opts = build_engine_opts("dummy_stage", "dummy_temp", cookies_from_browser="firefox")
        self.assertEqual(opts.get("cookiesfrombrowser"), ("firefox",))

    def test_stealth_browser_headers_present(self):
        opts = build_engine_opts("dummy_stage", "dummy_temp", use_stealth=True)
        headers = opts.get("http_headers", {})
        self.assertIn("Sec-Ch-Ua", headers)
        self.assertIn("Sec-Ch-Ua-Platform", headers)
        self.assertIn("Sec-Fetch-Dest", headers)

    def test_cookie_detect_api(self):
        client = flask_app.test_client()
        res = client.get("/api/browser_cookies/detect")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data.get("status"), "success")
        browsers = data.get("browsers", [])
        self.assertIsInstance(browsers, list)
        browser_ids = [b["id"] for b in browsers]
        self.assertIn("chrome", browser_ids)
        self.assertIn("firefox", browser_ids)
        self.assertIn("edge", browser_ids)


class TestMediaStreamingHttp206(unittest.TestCase):
    """Verify RFC 7233 HTTP Range streaming for in-app ad-free playback."""

    def setUp(self):
        self.client = flask_app.test_client()
        # Create a temporary dummy media file
        self.test_content = b"QUIDIAN_STREAMING_TEST_MEDIA_BYTES_0123456789" * 100  # 4500 bytes
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        self.temp_file.write(self.test_content)
        self.temp_file.close()

    def tearDown(self):
        if os.path.exists(self.temp_file.name):
            try:
                os.remove(self.temp_file.name)
            except OSError:
                pass

    def test_stream_without_range_returns_200(self):
        res = self.client.get(f"/api/media/stream?path={self.temp_file.name}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, self.test_content)
        self.assertEqual(res.headers.get("Accept-Ranges"), "bytes")
        self.assertEqual(int(res.headers.get("Content-Length")), len(self.test_content))
        self.assertIn("video/mp4", res.headers.get("Content-Type"))

    def test_stream_with_valid_range_returns_206(self):
        # Request bytes 0 to 49 (50 bytes)
        headers = {"Range": "bytes=0-49"}
        res = self.client.get(f"/api/media/stream?path={self.temp_file.name}", headers=headers)
        self.assertEqual(res.status_code, 206)
        self.assertEqual(len(res.data), 50)
        self.assertEqual(res.data, self.test_content[0:50])
        self.assertEqual(res.headers.get("Content-Range"), f"bytes 0-49/{len(self.test_content)}")
        self.assertEqual(res.headers.get("Content-Length"), "50")

    def test_stream_with_open_ended_range_returns_206(self):
        # Request from byte 100 to the end
        start_byte = 100
        headers = {"Range": f"bytes={start_byte}-"}
        res = self.client.get(f"/api/media/stream?path={self.temp_file.name}", headers=headers)
        self.assertEqual(res.status_code, 206)
        expected_len = len(self.test_content) - start_byte
        self.assertEqual(len(res.data), expected_len)
        self.assertEqual(res.data, self.test_content[start_byte:])
        self.assertEqual(
            res.headers.get("Content-Range"),
            f"bytes {start_byte}-{len(self.test_content) - 1}/{len(self.test_content)}"
        )

    def test_stream_with_invalid_range_returns_416(self):
        # Range beyond file size
        headers = {"Range": "bytes=999999-1000000"}
        res = self.client.get(f"/api/media/stream?path={self.temp_file.name}", headers=headers)
        self.assertEqual(res.status_code, 416)
        self.assertIn(f"bytes */{len(self.test_content)}", res.headers.get("Content-Range", ""))

    def test_stream_missing_file_returns_404(self):
        res = self.client.get("/api/media/stream?path=C:/nonexistent_file_xyz_123.mp4")
        self.assertEqual(res.status_code, 404)

    def test_stream_missing_param_returns_400(self):
        res = self.client.get("/api/media/stream")
        self.assertEqual(res.status_code, 400)


class TestMediaConverterEngineAndApi(unittest.TestCase):
    """Verify Compressor Studio presets, probe, and conversion queue."""

    def setUp(self):
        self.client = flask_app.test_client()

    def test_presets_registry_completeness(self):
        presets = converter.PRESETS
        self.assertIn("discord_25mb", presets)
        self.assertIn("discord_8mb", presets)
        self.assertIn("discord_50mb", presets)
        self.assertIn("whatsapp_16mb", presets)
        self.assertIn("web_mp4_1080p", presets)
        self.assertIn("web_av1", presets)
        self.assertIn("web_hevc", presets)
        self.assertIn("gif_hq", presets)
        self.assertIn("audio_mp3_320k", presets)

        for key, p in presets.items():
            self.assertIn("name", p)
            self.assertIn("format", p)

    def test_api_convert_presets(self):
        res = self.client.get("/api/convert/presets")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data.get("status"), "success")
        self.assertTrue(len(data.get("presets", [])) >= 8)

    def test_api_convert_probe_nonexistent_file(self):
        res = self.client.post("/api/convert/probe", json={"path": "C:/fake_file_does_not_exist.mp4"})
        self.assertEqual(res.status_code, 404)

    def test_api_convert_start_validates_path(self):
        res = self.client.post("/api/convert/start", json={"path": "C:/fake_nonexistent.mp4"})
        self.assertEqual(res.status_code, 404)


class TestEngineUpdater(unittest.TestCase):
    """Verify PyPI version checking and self-updater logic."""

    def setUp(self):
        self.client = flask_app.test_client()

    def test_get_engine_versions_structure(self):
        versions = updater.get_engine_versions()
        engines = versions.get("engines", {})
        self.assertIn("yt-dlp", engines)
        self.assertIn("gallery-dl", engines)
        ytdlp_info = engines["yt-dlp"]
        self.assertIn("installed", ytdlp_info)
        self.assertIn("latest", ytdlp_info)
        self.assertIn("update_available", ytdlp_info)

    def test_api_check_updates(self):
        res = self.client.get("/api/engine/check_updates")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data.get("status"), "success")
        self.assertIn("yt-dlp", data.get("engines", {}))


if __name__ == "__main__":
    unittest.main()
