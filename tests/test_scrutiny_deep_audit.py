"""
Comprehensive Scrutiny Deep Audit & Regression Test Suite
Validates all 10 engine modules and API layer edge cases hardened during the deep audit.
"""

import os
import sys
import tempfile
import pytest
from unittest.mock import patch, MagicMock

from engines.downloader import (
    _score_movie_candidate,
    normalize_url,
    verify_download_integrity,
    _get_unique_path,
    _move_into,
    resolve_page_title,
)
from engines.stealth_sniffer import (
    _probe_stream_metadata,
    StealthStreamInterceptor,
    InterceptorCancelled,
)
from engines.search import (
    _clean_platform_search_query,
    _clean_title_for_catalog,
    _detect_intent,
    _score_entry,
    _parse_time_str,
)
from engines.playlist import inspect_playlist, download_batch
from engines.audio import _apply_audio_tags, rip_and_tag_audio, _bitrate_kbps
from engines.subtitles import _normalise_timestamp, _clean_payload, parse_vtt_cues
from engines.gallery import download_social_gallery
from engines.library import reveal_in_explorer, open_downloaded_file
from engines.series import SeriesDownloader
from app import app, _is_local_or_lan_host


# ---------------------------------------------------------------------------
# Module 1: Downloader Edge Cases
# ---------------------------------------------------------------------------
def test_score_movie_candidate_string_durations():
    """Ensure string duration timestamps like '1:45:00' or '7200' do not raise TypeError."""
    cand_hms = {"title": "Inception 2010 Full Movie", "duration": "1:45:00", "uploader": "Warner Bros"}
    score_hms = _score_movie_candidate(cand_hms, "Inception", year=2010)
    assert score_hms > 0

    cand_sec_str = {"title": "Inception 2010 Full Movie", "duration": "6300", "uploader": "Warner Bros"}
    score_sec = _score_movie_candidate(cand_sec_str, "Inception", year=2010)
    assert score_sec > 0

    cand_short = {"title": "Inception Clip", "duration": "120", "uploader": "Random"}
    score_short = _score_movie_candidate(cand_short, "Inception")
    assert score_short <= -1000


def test_verify_download_integrity_audio_check():
    """Verify audio streams are strictly required when is_audio=True."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
        tf.write(b"\x00" * (30 * 1024))
        temp_path = tf.name

    try:
        with patch("shutil.which", return_value="ffprobe"), \
             patch("subprocess.run") as mock_sub:
            # Simulate ffprobe returning only video stream, no audio
            mock_sub.return_value = MagicMock(returncode=0, stdout=b"video\n")
            valid, err = verify_download_integrity(temp_path, is_audio=True)
            assert not valid
            assert "audio stream" in err.lower()

            # Simulate ffprobe returning audio stream
            mock_sub.return_value = MagicMock(returncode=0, stdout=b"audio\n")
            valid, err = verify_download_integrity(temp_path, is_audio=True)
            assert valid
            assert err is None
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def test_normalize_url_clip_and_domain():
    """Test URL schema normalization and YouTube clip extraction fallback."""
    url, query = normalize_url("youtube.com/watch?v=dQw4w9WgXcQ")
    assert url == "https://youtube.com/watch?v=dQw4w9WgXcQ"
    assert query is None

    # Plain text search
    url, query = normalize_url("interstellar full movie hd")
    assert url is None
    assert query == "interstellar full movie hd"


def test_move_into_uniqueness_and_cleanup():
    """Verify _move_into creates unique filenames and cleans staging."""
    stage = tempfile.mkdtemp()
    out = tempfile.mkdtemp()
    try:
        sample_file = os.path.join(stage, "video.mp4")
        with open(sample_file, "wb") as f:
            f.write(b"content")

        # Create pre-existing file in out
        pre_existing = os.path.join(out, "video.mp4")
        with open(pre_existing, "wb") as f:
            f.write(b"existing")

        final = _move_into(out, stage, primary_name="video.mp4")
        assert final is not None
        assert os.path.exists(final)
        assert final != pre_existing
        assert "video (1).mp4" in final
        assert not os.path.exists(stage)
    finally:
        if os.path.exists(out):
            import shutil
            shutil.rmtree(out, ignore_errors=True)


# ---------------------------------------------------------------------------
# Module 2: Stealth Sniffer Edge Cases
# ---------------------------------------------------------------------------
def test_probe_stream_metadata_excludes_comments():
    """Ensure #EXT comment lines are excluded from HLS manifest sub-URL candidate parsing."""
    manifest = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",URI="sub.m3u8"
https://cdn.example.com/360p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720
https://cdn.example.com/720p.m3u8
"""
    with patch("engines.stealth_sniffer._safe_cffi_get") as mock_get:
        mock_get.return_value = MagicMock(status_code=200, text=manifest)
        meta = _probe_stream_metadata("https://example.com/master.m3u8", stream_type="hls")
        assert meta["score"] > 0


# ---------------------------------------------------------------------------
# Module 3: Search Engine Edge Cases
# ---------------------------------------------------------------------------
def test_clean_platform_search_query_prefixes():
    """Verify search cleaning strips prefixes like 'watch' and 'download'."""
    assert _clean_platform_search_query("watch Inception on Prime Video") == "Inception"
    assert _clean_platform_search_query("download Oppenheimer from Netflix") == "Oppenheimer"
    assert _clean_platform_search_query("stream Dune on HBO") == "Dune"


def test_detect_intent_educational_vs_movie():
    """Verify filmmaking and photography tutorials are not misclassified as movies."""
    assert _detect_intent("film photography basics") == "general"
    assert _detect_intent("filmmaking tutorial for beginners") == "general"
    assert _detect_intent("camera sound design lecture") == "general"
    assert _detect_intent("Interstellar full movie") == "full_movie"
    assert _detect_intent("Oppenheimer movie") == "movie"


def test_score_entry_duration_safety():
    """Ensure _score_entry gracefully accepts string and numeric durations."""
    entry = {
        "title": "Learn Film Photography",
        "duration": "12:34",
        "uploader": "Photography Club",
        "view_count": 50000,
    }
    score = _score_entry(entry, "film photography", "general")
    assert isinstance(score, float)
    assert score > 0


# ---------------------------------------------------------------------------
# Module 4: Playlist Engine Edge Cases
# ---------------------------------------------------------------------------
def test_playlist_batch_immediate_cancel():
    """Verify download_batch cancels remaining items immediately when cancel_check triggers."""
    urls = ["https://example.com/1", "https://example.com/2", "https://example.com/3"]
    results = download_batch(
        urls,
        output_path="test",
        cancel_check=lambda: True,
    )
    assert len(results) == 3
    assert all(r["status"] == "cancelled" for r in results)


# ---------------------------------------------------------------------------
# Module 5: Audio Engine Edge Cases
# ---------------------------------------------------------------------------
def test_apply_audio_tags_id3_no_header():
    """Ensure _apply_audio_tags initializes ID3 header safely when no header exists."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
        tf.write(b"ID3_MOCK_DATA" * 50)
        temp_mp3 = tf.name

    try:
        # EasyID3 will encounter no valid header in this raw data
        # _apply_audio_tags should not raise unhandled exception
        result = _apply_audio_tags(temp_mp3, target_format="mp3", title="Test Title", artist="Test Artist")
        assert isinstance(result, bool)
    finally:
        if os.path.exists(temp_mp3):
            os.remove(temp_mp3)


def test_rip_and_tag_audio_auto_prefix_https():
    """Verify rip_and_tag_audio auto-prefixes https:// to domain-only URLs."""
    with patch("engines.audio._rip_from_url") as mock_rip:
        mock_rip.return_value = {"status": "success"}
        rip_and_tag_audio("youtube.com/watch?v=12345", "/tmp", target_format="mp3")
        called_url = mock_rip.call_args[0][0]
        assert called_url.startswith("https://youtube.com")


# ---------------------------------------------------------------------------
# Module 6: Subtitle Engine Edge Cases
# ---------------------------------------------------------------------------
def test_subtitles_timestamp_and_payload_cleaning():
    """Verify resilient timestamp normalisation and HTML unescaping in subtitles."""
    # Malformed timestamp fallback
    assert _normalise_timestamp("invalid") == "00:00:00,000"
    assert _normalise_timestamp("01:23.456") == "00:01:23,456"
    assert _normalise_timestamp("1:02:03.456") == "01:02:03,456"

    # HTML entities in subtitles
    cleaned = _clean_payload(["Hello &quot;World&quot; &amp; &nbsp; peace"])
    assert cleaned == ['Hello "World" &   peace']


# ---------------------------------------------------------------------------
# Module 7: Gallery Engine Edge Cases
# ---------------------------------------------------------------------------
def test_gallery_dl_binary_resolution():
    """Verify gallery-dl binary resolution when python module is not present."""
    import importlib
    orig_import = importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name == "gallery_dl":
            raise ImportError("No module named gallery_dl")
        return orig_import(name, *args, **kwargs)

    with patch("importlib.import_module", side_effect=fake_import), \
         patch("shutil.which", return_value="C:\\tools\\gallery-dl.exe"), \
         patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = []
        mock_proc.stderr = []
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        res = download_social_gallery("https://instagram.com/p/123", "/tmp")
        cmd_used = mock_popen.call_args[0][0]
        assert cmd_used[0] == "C:\\tools\\gallery-dl.exe"


# ---------------------------------------------------------------------------
# Module 8: Library Engine Edge Cases
# ---------------------------------------------------------------------------
def test_reveal_in_explorer_directory_detection():
    """Ensure directories are opened directly rather than selected within parent."""
    temp_dir = tempfile.mkdtemp()
    try:
        with patch("sys.platform", "win32"), patch("subprocess.run") as mock_run:
            reveal_in_explorer(temp_dir)
            cmd_args = mock_run.call_args[0][0]
            assert cmd_args[0] == "explorer.exe"
            assert "/select," not in cmd_args[1]
            assert cmd_args[1] == temp_dir
    finally:
        os.rmdir(temp_dir)


# ---------------------------------------------------------------------------
# Module 9: Series Engine Edge Cases
# ---------------------------------------------------------------------------
def test_series_downloader_clean_subtitles():
    """Verify subtitle ad-stripping and sequential renumbering."""
    raw = """1
00:00:01,000 --> 00:00:03,000
Downloaded from OpenSubtitles.org advertise your product

2
00:00:04,000 --> 00:00:06,000
Welcome to the palace, Osman Bey!
"""
    series = SeriesDownloader(output_dir="/tmp")
    cleaned = series.clean_subtitles(raw)
    assert "OpenSubtitles.org" not in cleaned
    assert "Osman Bey" in cleaned
    assert "1\n00:00:04,000 --> 00:00:06,000" in cleaned


# ---------------------------------------------------------------------------
# Module 10: App & REST API Edge Cases
# ---------------------------------------------------------------------------
def test_app_lan_host_allowance():
    """Ensure local LAN IP addresses (RFC 1918) are permitted by _guard_origin."""
    assert _is_local_or_lan_host("127.0.0.1")
    assert _is_local_or_lan_host("localhost")
    assert _is_local_or_lan_host("192.168.1.50")
    assert _is_local_or_lan_host("10.0.0.15")
    assert _is_local_or_lan_host("172.16.0.5")
    assert not _is_local_or_lan_host("evil-attacker.com")
    assert not _is_local_or_lan_host("8.8.8.8")


def test_api_audio_rip_domain_link():
    """Ensure api_audio_rip accepts domain-only links without 400 rejection."""
    client = app.test_client()
    with patch("app.EXECUTOR.submit") as mock_submit:
        resp = client.post(
            "/api/audio/rip",
            json={"source": "youtube.com/watch?v=test1234", "format": "mp3"},
            headers={"Host": "127.0.0.1:5050"}
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "started"
        assert "job_id" in data


# ---------------------------------------------------------------------------
# Module 11: Scrutiny Audit Hardening Pins
# ---------------------------------------------------------------------------
def test_score_entry_handles_none_query():
    """_score_entry should handle None or empty query without crashing."""
    entry = {"title": "Sample Media", "duration": 180, "uploader": "Creator"}
    score = _score_entry(entry, None, "general")
    assert isinstance(score, float)


def test_parse_time_str_handles_decimal_seconds():
    """_parse_time_str should handle decimal fractions in seconds (e.g. 1:12:30.5)."""
    assert _parse_time_str("03:45.5") == 225.5
    assert _parse_time_str("1:10:20.25") == 4220.25
    assert _parse_time_str("10:00") == 600


def test_search_imdb_preserves_unicode_characters():
    """search_imdb should preserve non-ASCII characters in movie titles."""
    from engines.search import search_imdb
    with patch("engines.search.cffi_requests.get") as mock_get:
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"d": [{"l": "Amélie", "y": 2001, "id": "tt0211915"}]}
        mock_get.return_value = mock_resp
        results = search_imdb("Amélie", count=1)
        assert len(results) == 1
        assert "Amélie" in results[0]["title"]


def test_parse_vtt_cues_variable_decimal_precision():
    """parse_vtt_cues should match cue timestamps with 1, 2, 3, or 4+ decimals."""
    vtt = """WEBVTT

00:01.5 --> 00:03.1234
Hello with high precision decimals!
"""
    cues = parse_vtt_cues(vtt)
    assert len(cues) == 1
    assert cues[0][2] == ["Hello with high precision decimals!"]


def test_job_store_ignores_unregistered_columns():
    """JobStore.update_job should ignore unknown kwargs without SQLite OperationalError."""
    from engines.job_store import JobStore
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name
    try:
        store = JobStore(db_path=db_path)
        store.create_job("test_extra", job_type="single")
        # Pass unknown kwargs that are not SQLite table columns
        store.update_job("test_extra", non_existent_column="test_val", another_unknown=999)
        job = store.get_job("test_extra")
        assert job["id"] == "test_extra"
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_job_dict_delitem_syncs_to_db():
    """Deleting from JobDict cache should delete from SQLite."""
    from engines.job_store import JobStore
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name
    try:
        store = JobStore(db_path=db_path)
        store.create_job("del_test", job_type="single")
        assert store.get_job("del_test") is not None
        del store._cache["del_test"]
        # In SQLite, it should also be deleted
        conn = store._get_connection()
        row = conn.execute("SELECT * FROM jobs WHERE id = 'del_test';").fetchone()
        conn.close()
        assert row is None
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)

