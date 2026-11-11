import json
import time
import pytest
from nodebatch.history import Redactor, History
from nodebatch.models import Job, Host, Step


@pytest.mark.parametrize("secret,chunks", [
    ("password", ["pa", "ss", "word", " end"]), ("aaa", ["aa", "a", "aa", "a"]),
    ("my 'key'", ["my ", "'ke", "y'"]), ("abcd", ["start ab", "cd finish"]),
])
def test_stream_redaction(secret, chunks):
    redactor = Redactor([secret])
    output = "".join(redactor.feed("stderr", chunk) for chunk in chunks) + redactor.feed("stderr", "", True)
    assert secret not in output
    assert "***" in output


def test_history_redacted_metadata_retention_and_recovery(tmp_path, bundle):
    history = History(tmp_path)
    job = Job.create([Host("a")], [Step("x", "echo SECRET")], bundle, (), secrets=["SECRET"])
    redactor = Redactor(job.secrets)
    history.start(job, redactor)
    history.event(job.id, "a", "x", "stdout", redactor.text("SECRET"), 10000)
    history.result(job.id, "a", "x", "success", .5, 0, "")
    history.finish(job.id, "success")
    row = history.db.execute("SELECT snapshot FROM runs").fetchone()
    assert "SECRET" not in row[0]
    assert "SECRET" not in history.log_path(job.id).read_text(encoding="utf-8")
    history.db.execute("UPDATE runs SET started=?", (time.time() - 40 * 86400,))
    history.db.commit()
    history.cleanup(30, 10000)
    assert not history.runs() and not history.log_path(job.id).exists()
    job2 = Job.create([Host("a")], [Step("x", "true")], bundle, ())
    history.start(job2, Redactor())
    history.close()
    reopened = History(tmp_path)
    assert reopened.runs()[0][2] == "unknown"
    reopened.close()
