import sqlite3
import threading
import time
from concurrent.futures import CancelledError

import pytest

from nodebatch.data_lock import DataDirectoryBusy
from nodebatch.history import History, Redactor
from nodebatch.models import Host, Job, Step


def completed_run(history, bundle, *, age=0, size=0):
    job = Job.create([Host("example")], [Step("One", "true")], bundle, ())
    history.start(job, Redactor())
    if size:
        history.event(job.id, "example", "One", "stdout", "x" * size, size * 2)
    history.finish(job.id, "success")
    history.db.execute("UPDATE runs SET started=? WHERE id=?", (time.time() - age * 86400, job.id))
    history.db.commit()
    return job


def test_idle_history_checks_do_not_checkpoint_or_compact(tmp_path, bundle):
    history = History(tmp_path)
    try:
        completed_run(history, bundle)
        statements = []
        history.db.set_trace_callback(statements.append)
        for _ in range(3):
            assert not history.schedule_cleanup(30, 100 * 1048576, force=False)
            assert history.cleanup(30, 100 * 1048576)["removed"] == 0
        assert not any("VACUUM" in sql.upper() or "CHECKPOINT" in sql.upper() for sql in statements)
    finally:
        history.close()


def test_small_expiration_reuses_pages_without_compaction(tmp_path, bundle):
    history = History(tmp_path)
    try:
        old = completed_run(history, bundle, age=40)
        recent = completed_run(history, bundle)
        statements = []
        history.db.set_trace_callback(statements.append)
        report = history.cleanup(30, 100 * 1048576)
        assert report["removed"] == 1
        assert history.runs()[0][0] == recent.id
        assert not history.log_path(old.id).exists()
        assert not any("VACUUM" in sql.upper() for sql in statements)
    finally:
        history.close()


def test_substantial_free_pages_are_reclaimed_without_full_rewrite_or_eviction(tmp_path, bundle):
    history = History(tmp_path)
    try:
        run = completed_run(history, bundle)
        history.db.execute("CREATE TABLE spare (value BLOB)")
        history.db.execute("INSERT INTO spare VALUES (zeroblob(12000000))")
        history.db.commit()
        history.db.execute("DELETE FROM spare")
        history.db.commit()
        before = history.storage_bytes()
        assert history.cleanup_needed(30, 100 * 1048576)
        statements = []
        history.db.set_trace_callback(statements.append)
        report = history.cleanup(30, 100 * 1048576)
        assert report["removed"] == 0 and report["bytes"] < before - 10000000
        assert history.runs()[0][0] == run.id
        assert any("incremental_vacuum" in sql for sql in statements)
        assert not any(sql.upper() == "VACUUM" for sql in statements)
        assert not history.cleanup_needed(30, 100 * 1048576)
    finally:
        history.close()


def test_legacy_database_migrates_once_under_storage_pressure(tmp_path, bundle):
    root = tmp_path / "history"
    root.mkdir()
    db = sqlite3.connect(root / "history.sqlite3")
    db.execute("CREATE TABLE old_payload (value BLOB)")
    db.execute("INSERT INTO old_payload VALUES (zeroblob(2000000))")
    db.commit()
    db.execute("DROP TABLE old_payload")
    db.commit()
    db.close()
    history = History(root)
    try:
        assert history.db.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
        run = completed_run(history, bundle)
        statements = []
        history.db.set_trace_callback(statements.append)
        report = history.cleanup(30, 128 * 1024)
        assert report["removed"] == 0 and not report["over_budget"]
        assert history.db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        assert history.runs()[0][0] == run.id
        assert sum(sql.upper() == "VACUUM" for sql in statements) == 1
        for _ in range(5):
            run = completed_run(history, bundle, age=40, size=100000)
        report = history.cleanup(30, 128 * 1024)
        assert report["removed"] == 5 and not report["over_budget"]
        assert sum(sql.upper() == "VACUUM" for sql in statements) == 1
    finally:
        history.close()


def test_close_returns_promptly_and_holds_lock_until_worker_stops(tmp_path, monkeypatch):
    history = History(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = history.cleanup
    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(history, "cleanup", paused)
    try:
        assert history.schedule_cleanup(30, 1048576)
        assert entered.wait(2)
        start = time.monotonic()
        history.close()
        assert time.monotonic() - start < .5
        with pytest.raises(DataDirectoryBusy):
            History(tmp_path)
        release.set()
        with pytest.raises(CancelledError):
            history._maintenance.result(timeout=3)
        deadline = time.monotonic() + 2
        while history._lock._file is not None and time.monotonic() < deadline:
            time.sleep(.005)
        assert history._lock._file is None
        History(tmp_path).close()
    finally:
        release.set()
        history.close()


def test_close_interrupts_legacy_vacuum_and_preserves_database(tmp_path, monkeypatch):
    root = tmp_path / "history"
    root.mkdir()
    db = sqlite3.connect(root / "history.sqlite3")
    db.execute("CREATE TABLE payload (value BLOB)")
    db.execute("WITH RECURSIVE seq(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM seq WHERE n<10000) "
               "INSERT INTO payload SELECT zeroblob(1000) FROM seq")
    db.commit()
    db.close()
    history = History(root)
    entered, release = threading.Event(), threading.Event()
    connect = sqlite3.connect
    class PausedCompaction(sqlite3.Connection):
        def set_progress_handler(self, handler, interval):
            if handler is None:
                return super().set_progress_handler(None, interval)
            def pause():
                entered.set()
                assert release.wait(5)
                return handler()
            return super().set_progress_handler(pause, interval)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, **kwargs, factory=PausedCompaction))
    try:
        assert history.schedule_cleanup(30, 128 * 1024)
        assert entered.wait(3)
        start = time.monotonic()
        history.close()
        assert time.monotonic() - start < .5
        release.set()
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            history._maintenance.result(timeout=3)
        db = connect(root / "history.sqlite3")
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("SELECT sum(length(value)) FROM payload").fetchone()[0] == 10000000
        db.close()
    finally:
        release.set()
        history.close()
