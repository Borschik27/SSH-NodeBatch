import os
from pathlib import Path
import subprocess
import sys

import pytest

from nodebatch.data_lock import DataDirectoryBusy, DataDirectoryLock
from nodebatch.history import History, Redactor
from nodebatch.models import Host, Job, Step


def child(code, root):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
               PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-c", code, str(root)], env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=15)


def test_second_process_cannot_recover_or_cleanup_active_run(tmp_path, bundle):
    history = History(tmp_path)
    try:
        job = Job.create([Host("a")], [Step("One", "true")], bundle, ())
        history.start(job, Redactor())
        history.event(job.id, "a", "One", "stdout", "active", 10000)
        before = history.log_path(job.id).read_bytes()
        result = child("""
import sys
from nodebatch.history import History
from nodebatch.data_lock import DataDirectoryBusy
try:
    history = History(sys.argv[1])
except DataDirectoryBusy as exc:
    print(exc)
else:
    history.cleanup(1, 0)
    history.close()
    raise SystemExit(1)
""", tmp_path)
        assert result.returncode == 0, result.stderr
        assert str(tmp_path.resolve()) in result.stdout
        assert history.runs()[0][2] == "running"
        assert history.log_path(job.id).read_bytes() == before
        history.result(job.id, "a", "One", "success", .1, 0, "")
        history.finish(job.id, "success")
        assert history.results(job.id)[0][2] == "success"
    finally:
        history.close()


def test_alternate_path_and_independent_directories(tmp_path):
    root = tmp_path / "state"
    first = DataDirectoryLock(root)
    try:
        (root / "sub").mkdir()
        with pytest.raises(DataDirectoryBusy):
            DataDirectoryLock(root / "sub" / "..")
        other = DataDirectoryLock(tmp_path / "other")
        other.close()
    finally:
        first.close()
    again = DataDirectoryLock(root)
    again.close()


def test_symlink_resolves_to_same_lock(tmp_path):
    root, alias = tmp_path / "state", tmp_path / "alias"
    first = DataDirectoryLock(root)
    try:
        try:
            alias.symlink_to(root, target_is_directory=True)
        except OSError:
            pytest.skip("Symlinks unavailable for this account")
        with pytest.raises(DataDirectoryBusy):
            DataDirectoryLock(alias)
    finally:
        first.close()


def test_abrupt_exit_releases_lock_and_recovers(tmp_path):
    result = child("""
import os, sys
from nodebatch.history import History
history = History(sys.argv[1])
history.db.execute("INSERT INTO runs (id,started,finished,status,summary,snapshot) VALUES ('unfinished', 0, NULL, 'running', '', '{}')")
history.db.commit()
os._exit(0)
""", tmp_path)
    assert result.returncode == 0, result.stderr
    recovered = History(tmp_path)
    try:
        assert recovered.runs()[0][2] == "unknown"
    finally:
        recovered.close()


def test_failed_database_open_releases_lock(tmp_path, monkeypatch):
    import sqlite3
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("test failure")
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", fail)
        with pytest.raises(sqlite3.OperationalError):
            History(tmp_path)
    history = History(tmp_path)
    history.close()
