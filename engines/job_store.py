"""
Quidian - Persistent Job Store
Zero-dependency, SQLite-backed job registry with in-memory caching and auto-healing.
Survives process restarts, crashes, and power events.
Developed by Kamran Ashraf
"""

import json
import os
import sqlite3
import threading
import time

# Thread-local storage: one cached SQLite connection per OS thread.
# This eliminates the open/close overhead that was occurring on every
# DB call in tight progress-update loops during active downloads.
_thread_local = threading.local()


class JobDict(dict):
    """Dictionary that synchronizes .clear() and .pop() with the underlying SQLite store."""
    def __init__(self, store, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._store = store

    def clear(self):
        super().clear()
        if self._store:
            try:
                conn = self._store._get_connection()
                with conn:
                    conn.execute("DELETE FROM jobs;")
            except Exception:
                pass

    def pop(self, key, default=None):
        val = super().pop(key, default)
        if self._store:
            try:
                conn = self._store._get_connection()
                with conn:
                    conn.execute("DELETE FROM jobs WHERE id = ?;", (key,))
            except Exception:
                pass
        return val

    def __delitem__(self, key):
        super().__delitem__(key)
        if self._store:
            try:
                conn = self._store._get_connection()
                with conn:
                    conn.execute("DELETE FROM jobs WHERE id = ?;", (key,))
            except Exception:
                pass


class JobStore:
    """Thread-safe persistent job registry backed by SQLite with in-memory caching."""

    def __init__(self, db_path=None):
        if db_path is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(base_dir, "jobs.db")
        self.db_path = db_path
        self._lock = threading.RLock()
        self._cache = JobDict(self)
        self._init_db()
        self._load_cache()

    def _get_connection(self):
        """Return a per-thread cached SQLite connection.

        Creates a new connection the first time a thread calls this.
        The connection lives for the lifetime of the thread, eliminating
        open/close overhead on every DB operation.
        """
        conn = getattr(_thread_local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=15000;")
            _thread_local.conn = conn
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._get_connection()
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        type TEXT,
                        status TEXT,
                        percent REAL DEFAULT 0.0,
                        speed TEXT,
                        eta REAL,
                        title TEXT,
                        filename TEXT,
                        filepath TEXT,
                        downloaded_files TEXT,
                        phase TEXT,
                        message TEXT,
                        error TEXT,
                        error_type TEXT,
                        platform TEXT,
                        suggested_title TEXT,
                        cancel INTEGER DEFAULT 0,
                        created REAL,
                        finished REAL
                    );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created);")

    def _row_to_dict(self, row):
        if row is None:
            return None
        d = dict(row)
        # Parse JSON fields
        if d.get("downloaded_files"):
            try:
                d["downloaded_files"] = json.loads(d["downloaded_files"])
            except Exception:
                d["downloaded_files"] = []
        else:
            d["downloaded_files"] = []
        d["cancel"] = bool(d.get("cancel"))
        return d

    def _load_cache(self):
        with self._lock:
            conn = self._get_connection()
            cursor = conn.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 2000;")
            for row in cursor.fetchall():
                job_dict = self._row_to_dict(row)
                if job_dict and "id" in job_dict:
                    self._cache[job_dict["id"]] = job_dict

    def create_job(self, job_id, job_type="download", **kwargs):
        now = time.time()
        job = {
            "id": job_id,
            "type": job_type,
            "status": "queued",
            "percent": 0.0,
            "speed": None,
            "eta": None,
            "title": None,
            "filename": None,
            "filepath": None,
            "downloaded_files": [],
            "phase": None,
            "message": "Queued...",
            "error": None,
            "error_type": None,
            "platform": None,
            "suggested_title": None,
            "cancel": False,
            "created": now,
            "finished": None,
        }
        job.update(kwargs)

        with self._lock:
            self._cache[job_id] = dict(job)
            conn = self._get_connection()
            with conn:
                conn.execute("""
                    INSERT OR REPLACE INTO jobs (
                        id, type, status, percent, speed, eta, title, filename, filepath,
                        downloaded_files, phase, message, error, error_type, platform,
                        suggested_title, cancel, created, finished
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    job["id"], job["type"], job["status"], job["percent"], job["speed"],
                    job["eta"], job["title"], job["filename"], job["filepath"],
                    json.dumps(job["downloaded_files"]), job["phase"], job["message"],
                    job["error"], job.get("error_type"), job.get("platform"),
                    job.get("suggested_title"), 1 if job["cancel"] else 0,
                    job["created"], job["finished"]
                ))
        return job_id

    def update_job(self, job_id, force=False, **kwargs):
        with self._lock:
            job = self._cache.get(job_id)
            if job is None:
                job = self.get_job(job_id)
                if job is None:
                    return

            if not force and job.get("status") in ("done", "error", "cancelled"):
                return

            job.update(kwargs)
            self._cache[job_id] = dict(job)

            VALID_JOB_COLUMNS = {
                "id", "type", "status", "percent", "speed", "eta", "title",
                "filename", "filepath", "downloaded_files", "phase", "message",
                "error", "error_type", "platform", "suggested_title", "cancel",
                "created", "finished"
            }
            conn = self._get_connection()
            with conn:
                set_clauses = []
                params = []
                for k, v in kwargs.items():
                    if k not in VALID_JOB_COLUMNS:
                        continue
                    if k == "downloaded_files" and isinstance(v, list):
                        v = json.dumps(v)
                    elif k == "cancel":
                        v = 1 if v else 0
                    set_clauses.append(f"{k} = ?")
                    params.append(v)
                if set_clauses:
                    params.append(job_id)
                    sql = f"UPDATE jobs SET {', '.join(set_clauses)} WHERE id = ?;"
                    conn.execute(sql, params)

    def get_job(self, job_id):
        if not job_id or not isinstance(job_id, str):
            return None
        with self._lock:
            if job_id in self._cache:
                return dict(self._cache[job_id])

            conn = self._get_connection()
            cursor = conn.execute("SELECT * FROM jobs WHERE id = ?;", (job_id,))
            row = cursor.fetchone()
            job = self._row_to_dict(row)
            if job:
                self._cache[job_id] = dict(job)
            return job

    def get_jobs_bulk(self, job_ids):
        out = {}
        with self._lock:
            for jid in job_ids:
                if not isinstance(jid, str):
                    continue
                job = self.get_job(jid)
                if job:
                    out[jid] = job
                else:
                    out[jid] = {"id": jid, "status": "expired"}
        return out

    def prune_stale_jobs(self, ttl_seconds=1800, max_records=2000):
        now = time.time()
        with self._lock:
            conn = self._get_connection()
            with conn:
                # Sync any in-memory dictionary mutations made directly to JOBS[jid]
                for jid, j in list(self._cache.items()):
                    conn.execute("""
                        UPDATE jobs SET status = ?, finished = ?, percent = ?, message = ?
                        WHERE id = ?;
                    """, (j.get("status"), j.get("finished"), j.get("percent", 0.0), j.get("message"), jid))

                # Remove finished/terminal jobs older than ttl_seconds
                conn.execute("""
                    DELETE FROM jobs
                    WHERE status IN ('done', 'error', 'cancelled')
                    AND (? - COALESCE(finished, created)) > ?;
                """, (now, ttl_seconds))

                # Remove very stale queued jobs (> 2 hours)
                conn.execute("""
                    DELETE FROM jobs
                    WHERE status = 'queued'
                    AND (? - created) > 7200;
                """, (now,))

                # Hard ceiling: evict oldest terminal records when over the cap
                cursor = conn.execute("SELECT COUNT(*) FROM jobs;")
                count = cursor.fetchone()[0]
                if count > max_records:
                    conn.execute("""
                        DELETE FROM jobs WHERE id IN (
                            SELECT id FROM jobs
                            WHERE status IN ('done', 'error', 'cancelled')
                            ORDER BY COALESCE(finished, created) ASC
                            LIMIT ?
                        );
                    """, (count - max_records,))

            # Refresh cache after prune without calling JobDict.clear() which deletes rows
            dict.clear(self._cache)
            self._load_cache()

    def recover_interrupted_jobs(self, downloads_dir):
        """Self-healing sweep at startup: promote completed files or mark interrupted."""
        with self._lock:
            conn = self._get_connection()
            recovered = 0
            cursor = conn.execute("""
                SELECT * FROM jobs
                WHERE status IN ('queued', 'downloading', 'processing');
            """)
            rows = cursor.fetchall()
            for row in rows:
                j = self._row_to_dict(row)
                jid = j["id"]
                fpath = j.get("filepath")
                title = j.get("title") or ""
                clean_title = "".join(c for c in title if c.isalnum() or c in " .-_").strip().lower()

                target_file = None
                if fpath and os.path.isfile(fpath) and os.path.getsize(fpath) > 1024:
                    target_file = fpath
                elif clean_title and downloads_dir and os.path.isdir(downloads_dir):
                    try:
                        for entry in os.scandir(downloads_dir):
                            try:
                                if entry.is_file() and entry.stat().st_size > 1024 * 1024:
                                    name_lower = entry.name.lower()
                                    if clean_title in name_lower or (len(clean_title) > 8 and clean_title[:20] in name_lower):
                                        target_file = entry.path
                                        break
                            except (OSError, PermissionError):
                                continue
                    except (OSError, PermissionError):
                        pass

                if target_file and os.path.isfile(target_file):
                    fname = os.path.basename(target_file)
                    self.update_job(
                        jid,
                        force=True,
                        status="done",
                        percent=100.0,
                        filepath=target_file,
                        filename=fname,
                        finished=os.path.getmtime(target_file),
                        message=f"Saved: {fname}",
                    )
                    recovered += 1
                else:
                    self.update_job(
                        jid,
                        force=True,
                        status="interrupted",
                        message="Download was interrupted during a previous session.",
                    )
            return recovered

    def clear(self):
        """Clear cache and DB table (used primarily in test suites)."""
        with self._lock:
            self._cache.clear()
            conn = self._get_connection()
            with conn:
                conn.execute("DELETE FROM jobs;")
