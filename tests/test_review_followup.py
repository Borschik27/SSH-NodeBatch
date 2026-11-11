import sqlite3
import time

from nodebatch.history import History, Redactor
from nodebatch.models import Host, Job, Step


def test_cleanup_stops_after_first_sufficient_deletion_despite_wal_growth(tmp_path, bundle):
    history = History(tmp_path / "history")
    try:
        jobs = []
        for i in range(3):
            job = Job.create([Host("a")], [Step("One", "true")], bundle, ())
            history.start(job, Redactor())
            for _ in range(20):
                history.result(job.id, "a", "One", "success", 0, 0, "x" * 8000)
            history.finish(job.id, "success")
            history.db.execute("UPDATE runs SET started=? WHERE id=?", (time.time() + i, job.id))
            history.db.commit()
            jobs.append(job)
        history.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        reference = sqlite3.connect(tmp_path / "reference.sqlite3")
        history.db.backup(reference)
        reference.execute("DELETE FROM results WHERE run_id=?", (jobs[0].id,))
        reference.execute("DELETE FROM runs WHERE id=?", (jobs[0].id,))
        reference.commit()
        reference.execute("VACUUM")
        reference.close()
        overhead = sum(p.stat().st_size for p in history.root.iterdir()
                       if p.name != "history.sqlite3" and not p.name.endswith("-wal"))
        budget = (tmp_path / "reference.sqlite3").stat().st_size + overhead + 8192
        report = history.cleanup(30, budget)
        assert report["removed"] == 1 and report["bytes"] <= budget
        assert {row[0] for row in history.runs()} == {job.id for job in jobs[1:]}
        assert all(history.log_path(job.id).exists() for job in jobs[1:])
    finally:
        history.close()


def test_cleanup_compacts_existing_free_pages_before_deleting_runs(tmp_path, bundle):
    history = History(tmp_path)
    try:
        job = Job.create([Host("a")], [Step("One", "true")], bundle, ())
        history.start(job, Redactor())
        for _ in range(30):
            history.result(job.id, "a", "One", "success", 0, 0, "x" * 10000)
        history.finish(job.id, "success")
        history.db.execute("DELETE FROM results")
        history.db.commit()
        report = history.cleanup(30, 128 * 1024)
        assert report["removed"] == 0 and report["bytes"] <= 128 * 1024
        assert history.runs()[0][0] == job.id
    finally:
        history.close()
