"""
Tests for Live Folder Search & Autocomplete, Directory Creation, and
Sequential One-by-One Playlist Downloading with Immediate Destination Saving.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import app as appmod
from engines.downloader import is_playlist_url, download_playlist_sequential, download_media


class TestFolderSearchAndPlaylist(unittest.TestCase):
    def setUp(self):
        appmod.app.config["TESTING"] = True
        self.client = appmod.app.test_client()

    def test_search_folders_empty_query_returns_presets(self):
        """Empty query returns default drives and standard user folders."""
        res = self.client.get("/api/search_folders")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data.get("status"), "ok")
        results = data.get("results", [])
        self.assertGreater(len(results), 0)
        paths = [r["path"] for r in results]
        self.assertTrue(any("download" in p.lower() or ":\\" in p for p in paths))

    def test_search_folders_exact_path_lists_children(self):
        """Querying an existing directory returns its subdirectories."""
        with tempfile.TemporaryDirectory() as temp_dir:
            sub1 = os.path.join(temp_dir, "SubfolderAlpha")
            sub2 = os.path.join(temp_dir, "SubfolderBeta")
            os.makedirs(sub1, exist_ok=True)
            os.makedirs(sub2, exist_ok=True)

            res = self.client.post("/api/search_folders", json={"query": temp_dir})
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            paths = [r["path"] for r in data.get("results", [])]
            self.assertIn(os.path.normpath(sub1), paths)
            self.assertIn(os.path.normpath(sub2), paths)

    def test_search_folders_new_path_can_create(self):
        """Querying a non-existent child inside an existing parent offers can_create=True."""
        with tempfile.TemporaryDirectory() as temp_dir:
            new_target = os.path.join(temp_dir, "MyNewDownloadFolder")
            res = self.client.post("/api/search_folders", json={"query": new_target})
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            results = data.get("results", [])
            matches = [r for r in results if r["path"] == os.path.normpath(new_target)]
            self.assertTrue(len(matches) > 0)
            self.assertTrue(matches[0]["can_create"])
            self.assertFalse(matches[0]["exists"])

    def test_create_folder_endpoint(self):
        """POST /api/create_folder creates the directory and returns normalized path."""
        with tempfile.TemporaryDirectory() as temp_dir:
            target = os.path.join(temp_dir, "CreatedByApiTest", "Nested")
            res = self.client.post("/api/create_folder", json={"path": target})
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("status"), "created")
            self.assertTrue(os.path.isdir(target))

    def test_is_playlist_url_detection(self):
        """Verify URL pattern matching distinguishes playlists from single videos."""
        self.assertTrue(is_playlist_url("https://www.youtube.com/playlist?list=PL1234567890ABCDEF"))
        self.assertTrue(is_playlist_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1234567890ABCDEF"))
        self.assertTrue(is_playlist_url("https://soundcloud.com/artist/sets/my-album"))
        self.assertTrue(is_playlist_url("https://www.dailymotion.com/playlist/x1234"))

        # Single video links should NOT be flagged as playlists
        self.assertFalse(is_playlist_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
        self.assertFalse(is_playlist_url("https://vimeo.com/12345678"))
        self.assertFalse(is_playlist_url("https://example.com/video.mp4"))
        self.assertFalse(is_playlist_url(""))
        self.assertFalse(is_playlist_url(None))

    @patch("engines.downloader.verify_download_integrity", return_value=(True, None))
    @patch("engines.playlist.inspect_playlist")
    @patch("yt_dlp.YoutubeDL")
    def test_sequential_playlist_download_saves_one_by_one(self, mock_ydl, mock_inspect, mock_verify):
        """
        Verify that sequential playlist download downloads each video individually,
        applies zero-padded number prefixes, and saves them directly into output_path
        one-by-one.
        """
        with tempfile.TemporaryDirectory() as dest_dir:
            mock_inspect.return_value = {
                "title": "Tutorial Series",
                "entries": [
                    {"index": 1, "url": "https://youtube.com/watch?v=item1", "title": "Introduction to Python"},
                    {"index": 2, "url": "https://youtube.com/watch?v=item2", "title": "Data Structures"},
                ]
            }

            def fake_extract_info(url, download=True):
                args, kwargs = mock_ydl.call_args
                opts = args[0] if args else kwargs.get("ydl_opts", {})
                home = opts.get("paths", {}).get("home", "")
                if "item1" in url:
                    fname = "01 - Introduction to Python [item1].mp4"
                else:
                    fname = "02 - Data Structures [item2].mp4"
                fpath = os.path.join(home, fname)
                with open(fpath, "wb") as f:
                    f.write(b"MOCK_PLAYLIST_VIDEO_DATA")
                return {"filepath": fpath, "title": fname}

            instance = MagicMock()
            instance.extract_info.side_effect = fake_extract_info
            mock_ydl.return_value.__enter__.return_value = instance

            status_updates = []
            res = download_playlist_sequential(
                "https://youtube.com/playlist?list=PLmock",
                output_path=dest_dir,
                status_callback=lambda msg, pct=None: status_updates.append(msg)
            )

            self.assertIsNotNone(res)
            self.assertTrue(res.get("is_playlist"))
            self.assertEqual(res.get("total_items"), 2)
            self.assertEqual(res.get("success_count"), 2)
            self.assertEqual(len(res.get("downloaded_files", [])), 2)

            dest_files = os.listdir(dest_dir)
            self.assertIn("01 - Introduction to Python [item1].mp4", dest_files)
            self.assertIn("02 - Data Structures [item2].mp4", dest_files)
            self.assertTrue(any("Saved:" in msg for msg in status_updates))

    @patch("engines.downloader.verify_download_integrity", return_value=(True, None))
    @patch("engines.playlist.inspect_playlist")
    @patch("yt_dlp.YoutubeDL")
    def test_sequential_playlist_fault_tolerant_continuation(self, mock_ydl, mock_inspect, mock_verify):
        """
        Verify that if item 2 fails (e.g. video deleted/private), item 1 and item 3
        are still saved directly into the destination folder without stopping the playlist.
        """
        with tempfile.TemporaryDirectory() as dest_dir:
            mock_inspect.return_value = {
                "title": "Three Part Series",
                "entries": [
                    {"index": 1, "url": "https://youtube.com/watch?v=v1", "title": "Part One"},
                    {"index": 2, "url": "https://youtube.com/watch?v=v2", "title": "Part Two Deleted"},
                    {"index": 3, "url": "https://youtube.com/watch?v=v3", "title": "Part Three"},
                ]
            }

            def fake_extract_info(url, download=True):
                if "v2" in url:
                    raise RuntimeError("Video unavailable: deleted by uploader")
                args, kwargs = mock_ydl.call_args
                opts = args[0] if args else kwargs.get("ydl_opts", {})
                home = opts.get("paths", {}).get("home", "")
                name = "01 - Part One [v1].mp4" if "v1" in url else "03 - Part Three [v3].mp4"
                fpath = os.path.join(home, name)
                with open(fpath, "wb") as f:
                    f.write(b"PLAYLIST_VIDEO_BYTES")
                return {"filepath": fpath, "title": name}

            instance = MagicMock()
            instance.extract_info.side_effect = fake_extract_info
            mock_ydl.return_value.__enter__.return_value = instance

            res = download_playlist_sequential(
                "https://youtube.com/playlist?list=PL3parts",
                output_path=dest_dir
            )

            self.assertEqual(res.get("success_count"), 2)
            self.assertEqual(res.get("failed_count"), 1)
            self.assertEqual(len(res.get("failed_items", [])), 1)
            self.assertIn("deleted by uploader", res["failed_items"][0]["error"])

            dest_files = os.listdir(dest_dir)
            self.assertIn("01 - Part One [v1].mp4", dest_files)
            self.assertIn("03 - Part Three [v3].mp4", dest_files)

    def test_list_dirs_returns_current_parent_and_folders(self):
        """Verify /api/list_dirs returns current folder, parent, drives, and child directories."""
        with tempfile.TemporaryDirectory() as temp_dir:
            sub1 = os.path.join(temp_dir, "FolderAlpha")
            sub2 = os.path.join(temp_dir, "FolderBeta")
            os.makedirs(sub1, exist_ok=True)
            os.makedirs(sub2, exist_ok=True)

            res = self.client.post("/api/list_dirs", json={"path": temp_dir})
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("status"), "ok")
            self.assertEqual(os.path.normpath(data.get("current")), os.path.normpath(temp_dir))
            self.assertIsNotNone(data.get("parent"))
            self.assertTrue(len(data.get("drives", [])) > 0)
            folder_names = [f["name"] for f in data.get("folders", [])]
            self.assertIn("FolderAlpha", folder_names)
            self.assertIn("FolderBeta", folder_names)

    def test_list_dirs_defaults_when_empty(self):
        """Verify /api/list_dirs defaults cleanly when given no path or invalid path."""
        res = self.client.get("/api/list_dirs")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data.get("status"), "ok")
        self.assertTrue(os.path.isdir(data.get("current")))
        self.assertTrue(len(data.get("drives", [])) > 0)


if __name__ == "__main__":
    unittest.main()

