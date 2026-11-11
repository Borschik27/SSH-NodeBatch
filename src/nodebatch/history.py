from __future__ import annotations
from .i18n import tr
import json
import os
import re
import sqlite3
import time
import shutil
from concurrent.futures import CancelledError, ThreadPoolExecutor
from threading import Event, Lock
from datetime import datetime, timezone
from pathlib import Path

from .data_lock import DataDirectoryLock


class Redactor:
    """Streaming exact-secret replacement, including secrets split between reads."""
    def __init__(self, secrets=()):
        self.secrets = set(filter(None, secrets))
        self.buffers = {}

    def add(self, secret):
        if secret:
            self.secrets.add(secret)

    def text(self, text):
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "***")
        # Do not render terminal escape/control sequences in the desktop view.
        text = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|$))", "", text)
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)

    def feed(self, channel, text, final=False):
        combined = self.buffers.get(channel, "") + text
        if final or not self.secrets:
            self.buffers[channel] = ""
            return self.text(combined)
        # Complete matches are consumed greedily; only the tail AFTER the last
        # complete match can be an unfinished secret. This handles overlapping
        # prefixes such as "aaa" split into "aa", "a", "aa", "a".
        pattern = re.compile("|".join(re.escape(s) for s in sorted(self.secrets, key=len, reverse=True)))
        last_end = 0
        for match in pattern.finditer(combined):
            last_end = match.end()
        tail = combined[last_end:]
        retain = 0
        for secret in self.secrets:
            for length in range(1, min(len(secret), len(tail) + 1)):
                if tail.endswith(secret[:length]):
                    retain = max(retain, length)
        boundary = len(combined) - retain
        self.buffers[channel] = combined[boundary:]
        return self.text(combined[:boundary])

    def clear(self):
        self.buffers.clear()
        self.secrets.clear()


class History:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name == "nt" and str(self.root.resolve()).startswith("\\\\"):
            raise ValueError(tr('SQLite history must be on a local Windows drive, not a UNC share. Use the default data directory or --data-dir C:\\...'))
        self._lock = DataDirectoryLock(self.root)
        self.db = None
        try:
            if os.name != "nt":
                self.root.chmod(0o700)
            self._open()
        except BaseException:
            if self.db is not None:
                self.db.close()
            self._lock.close()
            raise

    def _open(self):
        self.db = sqlite3.connect(self.root / "history.sqlite3")
        # Takes effect immediately for a new database. Existing databases migrate
        # only when compaction is actually necessary, not on every application start.
        self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, started REAL NOT NULL, finished REAL,
                status TEXT NOT NULL, summary TEXT NOT NULL, snapshot TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS results (
                run_id TEXT NOT NULL, host TEXT NOT NULL, step TEXT NOT NULL,
                status TEXT NOT NULL, duration REAL, code INTEGER, reason TEXT);
            CREATE INDEX IF NOT EXISTS runs_finished_started ON runs(started)
                WHERE finished IS NOT NULL;
        """)
        self.streams = {}
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(results)")}
        for name, kind in (("ssh_code", "INTEGER"), ("command_code", "INTEGER"),
                           ("command_started", "INTEGER"), ("transport_reason", "TEXT")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE results ADD COLUMN {name} {kind}")
        if "output_incomplete_reason" not in {row[1] for row in self.db.execute("PRAGMA table_info(runs)")}:
            self.db.execute("ALTER TABLE runs ADD COLUMN output_incomplete_reason TEXT")
        self.sizes = {}
        self.truncated = set()
        self.truncation_reasons = {}
        self.limits = None
        self._estimated_bytes = 0
        self._last_scan = 0
        self._maintenance = None
        self._maintenance_stop = Event()
        self._maintenance_guard = Lock()
        self._compaction_db = None
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="history-maintenance")
        # A closed desktop cannot know what happened to remote commands.
        self.db.execute("UPDATE runs SET status='unknown', finished=? WHERE finished IS NULL", (time.time(),))
        self.db.commit()

    def start(self, job, redact):
        if self._maintenance is not None:
            raise ValueError(tr('History maintenance is in progress. Try again when it finishes.'))
        self.configure(job.config["settings"]["logging"])
        if self.storage_bytes() >= self.limits["history_max_bytes"]:
            raise OSError(tr('History budget exhausted. Run cleanup or increase the history limit.'))
        if shutil.disk_usage(self.root).free < self.limits["min_free_bytes"]:
            raise OSError(tr('Not enough free disk space for a new run'))
        snapshot = {
            "hosts": [h.alias for h in job.hosts],
            "steps": [{"name": s.name, "command": redact.text(s.command), "sudo": s.sudo, "sudo_user": s.sudo_user}
                      for s in job.steps],
            "profile": job.config["settings"]["active_profile"],
            "execution": job.config["settings"]["execution"],
            "transfer": ({"direction": job.transfer.direction, "local": job.transfer.local,
                          "remote": job.transfer.remote} if job.transfer else None),
        }
        summary = tr('{p0} hosts - ', p0=len(job.hosts)) + (tr('file transfer') if job.transfer else
                                                  ", ".join(s.name for s in job.steps))
        self.db.execute("INSERT INTO runs (id,started,finished,status,summary,snapshot) VALUES (?,?,NULL,?,?,?)",
                        (job.id, time.time(), "running", redact.text(summary),
                         json.dumps(snapshot, ensure_ascii=False)))
        self.db.commit()
        self.streams[job.id] = (self.root / f"{job.id}.jsonl").open("a", encoding="utf-8", newline="\n")
        self.sizes[job.id] = 0
        self.storage_bytes()

    def configure(self, logging):
        legacy = logging["max_bytes"]
        self.limits = {"run_max_bytes": logging.get("run_max_bytes", legacy),
                       "history_max_bytes": logging.get("history_max_bytes", legacy),
                       "min_free_bytes": logging.get("min_free_bytes", 16 * 1048576)}

    def storage_bytes(self):
        # Count each directory entry exactly once, including SQLite/WAL/SHM and lock.
        self._estimated_bytes = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
        self._last_scan = time.monotonic()
        return self._estimated_bytes

    def event(self, job_id, host, step, channel, text, max_bytes):
        if job_id in self.truncated:
            return False
        entry = json.dumps({"time": datetime.now(timezone.utc).isoformat(),
                            "host": host, "step": step, "channel": channel, "text": text},
                           ensure_ascii=False) + "\n"
        size = len(entry.encode("utf-8"))
        if time.monotonic() - self._last_scan > 1:
            self.storage_bytes()
        limit = self.limits["run_max_bytes"] if self.limits else max_bytes
        reason = tr('Run log limit reached')
        exhausted = self.sizes[job_id] + size > limit
        if self.limits:
            if self._estimated_bytes + size > self.limits["history_max_bytes"]:
                exhausted, reason = True, tr('Total history budget reached')
            if shutil.disk_usage(self.root).free - size < self.limits["min_free_bytes"]:
                exhausted, reason = True, tr('Free disk space reserve reached')
        if exhausted:
            if job_id not in self.truncated:
                self.truncated.add(job_id)
                self.truncation_reasons[job_id] = reason + tr('; further output is not saved.')
                self.db.execute("UPDATE runs SET output_incomplete_reason=?, summary=summary || ? WHERE id=?",
                                (reason, tr(' [output saved incompletely: ') + reason + "]", job_id))
                self.db.commit()
                self.storage_bytes()
            return False
        self.streams[job_id].write(entry)
        self.streams[job_id].flush()
        self.sizes[job_id] += size
        self._estimated_bytes += size
        return True

    def mark_output_incomplete(self, job_id, reason):
        self.db.execute("UPDATE runs SET output_incomplete_reason="
                        "CASE WHEN output_incomplete_reason IS NULL THEN ? ELSE output_incomplete_reason || '; ' || ? END, "
                        "summary=summary || ? WHERE id=? AND instr(COALESCE(output_incomplete_reason,''),?)=0",
                        (reason, reason, tr(' [output saved incompletely: ') + reason + "]", job_id, reason))
        self.db.commit()

    def result(self, job_id, host, step, status, duration, code, reason,
               ssh_code=None, command_code=None, transport_reason="", command_started=None):
        self.db.execute("INSERT INTO results (run_id,host,step,status,duration,code,reason,"
                        "ssh_code,command_code,transport_reason,command_started) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (job_id, host, step, status, duration, code, reason,
                         ssh_code, command_code, transport_reason, command_started))
        self.db.commit()
        self.storage_bytes()

    def finish(self, job_id, status):
        stream = self.streams.pop(job_id, None)
        if stream:
            stream.close()
        self.db.execute("UPDATE runs SET finished=?,status=? WHERE id=?", (time.time(), status, job_id))
        self.db.commit()
        self.sizes.pop(job_id, None)
        self.truncated.discard(job_id)
        self.truncation_reasons.pop(job_id, None)

    def runs(self):
        return self.db.execute("SELECT id,started,status,summary FROM runs ORDER BY started DESC").fetchall()

    def results(self, job_id):
        return self.db.execute("SELECT host,step,status,duration,code,reason FROM results WHERE run_id=?",
                               (job_id,)).fetchall()

    def log_path(self, job_id):
        if not re.fullmatch("[a-f0-9]{32}", job_id):
            raise ValueError(tr('Invalid ID'))
        return self.root / f"{job_id}.jsonl"

    def _free_pages(self, db):
        return db.execute("PRAGMA freelist_count").fetchone()[0]

    def _substantial_free_space(self, db):
        free = self._free_pages(db)
        pages = db.execute("PRAGMA page_count").fetchone()[0]
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        return free * page_size >= 8 * 1048576 and free * 4 >= pages

    def cleanup_needed(self, days, max_bytes, *, connection=None):
        """Cheap limit checks, without checkpoints or database rewrites."""
        db = connection or self.db
        return bool(db.execute("SELECT 1 FROM runs WHERE finished IS NOT NULL AND started<? LIMIT 1",
                               (time.time() - days * 86400,)).fetchone()
                    or self.storage_bytes() > max_bytes or self._substantial_free_space(db))

    def cleanup(self, days, max_bytes, *, connection=None):
        if self.streams:
            raise ValueError(tr('History maintenance is deferred until active runs finish'))
        db = connection or self.db
        def check_cancelled():
            if self._maintenance_stop.is_set():
                raise CancelledError()

        check_cancelled()
        if not self.cleanup_needed(days, max_bytes, connection=db):
            return {"bytes": self._estimated_bytes, "budget": max_bytes, "over_budget": False, "removed": 0}
        cutoff = time.time() - days * 86400
        removed = 0
        def checkpoint():
            check_cancelled()
            if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                raise OSError(tr('History has an active reader; checkpoint did not finish and cleanup was stopped'))

        def compact():
            checkpoint()
            mode = db.execute("PRAGMA auto_vacuum").fetchone()[0]
            if mode == 0:
                database_bytes = (self.root / "history.sqlite3").stat().st_size
                reserve = self.limits["min_free_bytes"] if self.limits else 0
                if shutil.disk_usage(self.root).free < reserve + database_bytes:
                    raise OSError(tr('Not enough free space to safely VACUUM history'))
            with self._maintenance_guard:
                check_cancelled()
                self._compaction_db = db
            db.set_progress_handler(lambda: int(self._maintenance_stop.is_set()), 1000)
            try:
                if mode == 0:
                    # One full rewrite converts a legacy database. Future cleanup
                    # moves only free pages, rather than copying the entire database.
                    db.execute("PRAGMA auto_vacuum=INCREMENTAL")
                    db.execute("VACUUM")
                else:
                    db.execute("PRAGMA auto_vacuum=INCREMENTAL")
                    while self._free_pages(db):
                        check_cancelled()
                        db.execute("PRAGMA incremental_vacuum(256)").fetchall()
                        checkpoint()
            finally:
                with self._maintenance_guard:
                    self._compaction_db = None
                db.set_progress_handler(None, 0)
            checkpoint()
            return self.storage_bytes()

        def remove(run_id):
            check_cancelled()
            self.log_path(run_id).unlink(missing_ok=True)
            db.execute("DELETE FROM results WHERE run_id=?", (run_id,))
            db.execute("DELETE FROM runs WHERE id=?", (run_id,))
            db.commit()

        # Age expiration is independent of the byte budget. Small deletions leave
        # reusable pages in place; only storage pressure or substantial waste
        # warrants compaction. Checkpoints keep WAL overhead out of eviction math.
        checkpoint()
        expired = db.execute("SELECT id FROM runs WHERE finished IS NOT NULL AND started<? ORDER BY started",
                             (cutoff,)).fetchall()
        for (run_id,) in expired:
            remove(run_id)
            removed += 1
        checkpoint()
        actual = self.storage_bytes()
        if actual > max_bytes or self._substantial_free_space(db):
            actual = compact()
        rows = db.execute("SELECT id FROM runs WHERE finished IS NOT NULL ORDER BY started").fetchall()
        for (run_id,) in rows:
            if actual <= max_bytes:
                break
            remove(run_id)
            removed += 1
            checkpoint()
            actual = self.storage_bytes()
            reclaimable = self._free_pages(db) * db.execute("PRAGMA page_size").fetchone()[0]
            # Batch small deletions until released pages can satisfy the budget.
            # Confirm the physical size before deleting any further run.
            if actual > max_bytes and (actual - reclaimable <= max_bytes or self._substantial_free_space(db)):
                actual = compact()
        if actual > max_bytes and self._free_pages(db):
            actual = compact()
        return {"bytes": actual, "budget": max_bytes, "over_budget": actual > max_bytes, "removed": removed}

    def schedule_cleanup(self, days, max_bytes, *, force=True):
        if self._closed or self.streams or self._maintenance is not None:
            return False
        if not force and not self.cleanup_needed(days, max_bytes):
            return False
        def maintain():
            db = sqlite3.connect(self.root / "history.sqlite3", timeout=.1)
            try:
                return self.cleanup(days, max_bytes, connection=db)
            finally:
                db.close()
        self._maintenance = self._executor.submit(maintain)
        return True

    def poll_cleanup(self):
        if self._maintenance is None or not self._maintenance.done():
            return None
        future, self._maintenance = self._maintenance, None
        return future.result()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._maintenance_stop.set()
        with self._maintenance_guard:
            if self._compaction_db is not None:
                self._compaction_db.interrupt()
        self._executor.shutdown(wait=False, cancel_futures=True)
        try:
            for stream in self.streams.values():
                stream.close()
            self.streams.clear()
        finally:
            try:
                self.db.close()
            finally:
                # Keep exclusive ownership until the worker has closed its own
                # connection, but never make the GUI wait for maintenance.
                if self._maintenance is not None:
                    self._maintenance.add_done_callback(lambda future: self._lock.close())
                else:
                    self._lock.close()
