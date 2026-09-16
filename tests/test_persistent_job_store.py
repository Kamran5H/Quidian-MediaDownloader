"""
Tests for Persistent SQLite JobStore & Server Resilience
Validates crash survival, self-healing recovery, and API endpoints.
"""

import os
import tempfile
import time
import unittest

from engines.job_store import JobStore
from app import app as flask_app, JOB_STORE


class TestPersistentJobStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="quidian_test_store_")
        self.db_path = os.path.join(self.temp_dir, "test_jobs.db")
        self.store = JobStore(db_path=self.db_path)

    def tearDown(self):
        self.store.clear()
        try:
            import shutil
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception:
            pass

    def test_create_and_read_job(self):
        jid = self.store.create_job("job_123", job_type="download", title="Subconscious Video")
        self.assertEqual(jid, "job_123")

        job = self.store.get_job("job_123")
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["title"], "Subconscious Video")
        self.assertEqual(job["percent"], 0.0)

    def test_update_job_persists(self):
        self.store.create_job("job_update", title="Test Update")
        self.store.update_job("job_update", status="downloading", percent=55.5, speed="3.2 MiB/s", eta=15)

        job = self.store.get_job("job_update")
        self.assertEqual(job["status"], "downloading")
        self.assertEqual(job["percent"], 55.5)
        self.assertEqual(job["speed"], "3.2 MiB/s")
        self.assertEqual(job["eta"], 15)

    def test_persistence_across_restarts(self):
        """Simulate process termination and startup: a new JobStore instance loads saved jobs."""
        self.store.create_job("job_persist", title="Persistent Media", filepath="C:\\fake\\path.mp4")
        self.store.update_job("job_persist", status="done", percent=100.0, message="Saved: path.mp4")

        # Create a completely fresh JobStore instance pointing to the exact same database file
        new_store = JobStore(db_path=self.db_path)
        loaded_job = new_store.get_job("job_persist")
        self.assertIsNotNone(loaded_job)
        self.assertEqual(loaded_job["status"], "done")
        self.assertEqual(loaded_job["title"], "Persistent Media")
        self.assertEqual(loaded_job["percent"], 100.0)

    def test_self_healing_promotes_existing_file_to_done(self):
        """If a download was interrupted but the file actually completed on disk, startup promotes it to done."""
        # Create a dummy downloaded file > 1MB
        fake_file = os.path.join(self.temp_dir, "16 - subconscious [w-cIY9sKSew].mp4")
        with open(fake_file, "wb") as f:
            f.write(b"0" * (1024 * 1024 * 2))  # 2MB fake file

        # Job was left in downloading state
        self.store.create_job(
            "job_subconscious",
            title="subconscious",
            status="downloading",
            percent=95.0,
            filepath=fake_file
        )

        recovered = self.store.recover_interrupted_jobs(self.temp_dir)
        self.assertEqual(recovered, 1)

        job = self.store.get_job("job_subconscious")
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["percent"], 100.0)
        self.assertEqual(job["filepath"], fake_file)

    def test_self_healing_marks_missing_file_as_interrupted(self):
        """If a download was interrupted and no file exists, mark status as interrupted instead of limbo."""
        self.store.create_job(
            "job_missing",
            title="NonExistentMedia",
            status="downloading",
            percent=40.0,
            filepath="C:\\nonexistent\\file.mp4"
        )

        self.store.recover_interrupted_jobs(self.temp_dir)
        job = self.store.get_job("job_missing")
        self.assertEqual(job["status"], "interrupted")

    def test_prune_stale_jobs(self):
        now = time.time()
        self.store.create_job("job_fresh", status="done", finished=now)
        self.store.create_job("job_stale", status="done", finished=now - 5000)

        self.store.prune_stale_jobs(ttl_seconds=1800)
        self.assertIsNotNone(self.store.get_job("job_fresh"))
        self.assertIsNone(self.store.get_job("job_stale"))


class TestApiEndpointsWithJobStore(unittest.TestCase):
    def setUp(self):
        self.client = flask_app.test_client()

    def test_api_status_endpoint(self):
        res = self.client.get("/api/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["app"], "quidian_media_downloader")
        self.assertTrue(data["ready"])

    def test_api_progress_and_cancel_integration(self):
        # Create a job through JOB_STORE
        jid = JOB_STORE.create_job("test_api_job", job_type="download", title="Integration Test")
        res = self.client.get(f"/api/progress/{jid}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["title"], "Integration Test")

        # Cancel it
        c_res = self.client.post(f"/api/cancel/{jid}")
        self.assertEqual(c_res.status_code, 200)

        # Bulk query
        b_res = self.client.post("/api/progress_bulk", json={"job_ids": [jid, "does_not_exist"]})
        self.assertEqual(b_res.status_code, 200)
        b_data = b_res.get_json()
        self.assertEqual(b_data["jobs"][jid]["status"], "cancelled")
        self.assertEqual(b_data["jobs"]["does_not_exist"]["status"], "expired")


if __name__ == "__main__":
    unittest.main()
