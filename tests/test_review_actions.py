from nodebatch.i18n import tr
import copy
import os
from pathlib import Path
import subprocess
import threading
import time

import pytest

from nodebatch.buffers import TextBuffer
from nodebatch.config import validate_scenario
from nodebatch.history import History, Redactor
from nodebatch.models import Host, Job, Step


@pytest.mark.parametrize("command", [
    "printf '%s\\n' 'first\nsecond'",
    'f() { printf done; }\nf',
    'for x in first second; do\n printf "%s\\n" "$x"\ndone',
    '# comment with unmatched quote \'\nprintf done',
    'printf \\\n done',
    "cat <<'EOF'\nquotes '\" and $x\nEOF",
    "python3 <<'PY'\nvalue = '''first\nsecond'''\nprint(value)\nPY",
])
def test_multiline_block_preserved_and_shell_runs(command):
    parsed = validate_scenario({"id": "block", "name": "Block", "os": ["unix"],
                               "shells": ["sh"], "steps": [{"name": "One", "command": command}]})
    assert parsed.render({})[0][0].command == command
    if os.name != "nt":
        assert subprocess.run(["sh", "-c", command], capture_output=True, timeout=5).returncode == 0


def test_multiline_quotes_and_separate_parameter_keep_injection_protection():
    command = "printf '%s\\n' 'first\nsecond'\nprintf '%s' {{value}}"
    parsed = validate_scenario({"id": "block", "name": "Block", "os": ["unix"], "shells": ["sh"],
        "parameters": {"value": {}}, "steps": [{"name": "One", "command": command}]})
    value = "; echo INJECTED; #"
    rendered = parsed.render({"value": value})[0][0].command
    if os.name != "nt":
        out = subprocess.run(["sh", "-c", rendered], capture_output=True, timeout=5)
        assert out.stdout.decode() == "first\nsecond\n" + value


@pytest.mark.parametrize("command", [
    'echo "first\n{{value}}"', "echo '{{value}}'", "echo x{{value}}",
    'echo \\{{value}}', '# {{value}}\ntrue', 'echo "$(echo {{value}})"',
    'echo prefix\\\n{{value}}',
    'echo ${a:-{{value}}}', 'cat <<EOF\n{{value}}\nEOF',
])
def test_unsupported_parameter_contexts_rejected(command):
    with pytest.raises(ValueError):
        validate_scenario({"id": "block", "name": "Block", "os": ["unix"], "shells": ["sh"],
            "parameters": {"value": {}}, "steps": [{"name": "One", "command": command}]})


def job(bundle):
    return Job.create([Host("a")], [Step("One", "true")], bundle, ())


def test_budget_includes_large_database_and_vacuum_reclaims_bytes(tmp_path, bundle):
    history = History(tmp_path)
    try:
        run = job(bundle)
        history.start(run, Redactor())
        for _ in range(100):
            history.result(run.id, "a", "One", "failed", 0, 1, "x" * 10000)
        history.finish(run.id, "failed")
        before = history.storage_bytes()
        report = history.cleanup(30, 128 * 1024)
        assert before > 1000000
        assert report["removed"] == 1 and report["bytes"] < before
        assert report["bytes"] <= 128 * 1024 and not history.runs()
        assert report["bytes"] == sum(p.stat().st_size for p in tmp_path.iterdir() if p.is_file())
    finally:
        history.close()


def test_active_log_limits_reported_without_deletion(tmp_path, bundle):
    bundle["settings"]["logging"].update(run_max_bytes=1024, history_max_bytes=1048576)
    history = History(tmp_path)
    try:
        run = job(bundle)
        history.start(run, Redactor())
        assert history.event(run.id, "a", "One", "stdout", "before", 999999)
        before = history.log_path(run.id).read_bytes()
        assert not history.event(run.id, "a", "One", "stdout", "x" * 2000, 999999)
        assert tr('log limit') in history.truncation_reasons[run.id]
        assert tr('incompletely') in history.runs()[0][3]
        assert history.db.execute("SELECT output_incomplete_reason FROM runs").fetchone()[0]
        assert history.log_path(run.id).read_bytes() == before
        with pytest.raises(ValueError, match=tr('active')):
            history.cleanup(1, 0)
        assert not history.schedule_cleanup(1, 0)
        assert history.runs()[0][2] == "running"
    finally:
        history.close()


def test_total_budget_and_disk_reserve_stop_persistence(tmp_path, bundle, monkeypatch):
    import nodebatch.history as module
    history = History(tmp_path)
    try:
        run = job(bundle)
        history.start(run, Redactor())
        history.limits["history_max_bytes"] = history.storage_bytes() + 128
        assert not history.event(run.id, "a", "One", "stdout", "x" * 200, 999999)
        assert tr('budget') in history.truncation_reasons[run.id]
        history.finish(run.id, "success")
        run = job(bundle)
        history.start(run, Redactor())
        from collections import namedtuple
        Usage = namedtuple("Usage", "total used free")
        monkeypatch.setattr(module.shutil, "disk_usage", lambda p: Usage(100, 100, 0))
        assert not history.event(run.id, "a", "One", "stdout", "output", 999999)
        assert tr('reserve') in history.truncation_reasons[run.id]
    finally:
        history.close()


def test_cleanup_denied_does_not_silently_delete_metadata(tmp_path, bundle, monkeypatch):
    history = History(tmp_path)
    try:
        run = job(bundle)
        history.start(run, Redactor())
        history.finish(run.id, "success")
        original = Path.unlink
        def denied(path, *args, **kwargs):
            if path.suffix == ".jsonl":
                raise PermissionError("simulated denial")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", denied)
        with pytest.raises(PermissionError):
            history.cleanup(1, 0)
        assert history.runs() and history.log_path(run.id).exists()
    finally:
        history.close()


def test_background_maintenance_does_not_block_gui(qapp, tmp_path, bundle, monkeypatch, wait):
    history = History(tmp_path)
    release = threading.Event()
    entered = threading.Event()
    original = history.cleanup
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(history, "cleanup", slow)
    try:
        start = time.monotonic()
        assert history.schedule_cleanup(30, 1048576)
        assert time.monotonic() - start < .5
        assert entered.wait(2)
        with pytest.raises(ValueError, match=tr('maintenance')):
            history.start(job(bundle), Redactor())
        release.set()
        wait(lambda: history._maintenance.done())
        assert history.poll_cleanup()["bytes"] > 0
        history.start(job(bundle), Redactor())
    finally:
        release.set()
        history.close()


def test_text_buffers_bound_long_utf8_and_count_losses():
    buffer = TextBuffer(maxlen=100000, max_bytes=1048576)
    for _ in range(1000):
        buffer.append('\u044f' * 12000)
    assert buffer.bytes <= 1048576 and buffer.dropped > 0
    buffer.append('\u044f' * 1048576)
    assert buffer.bytes <= 1048576
    assert tr('omitted') in list(buffer)[-1]


def test_qt_document_is_bounded_for_long_unbroken_line(qapp):
    from PySide6.QtWidgets import QPlainTextEdit
    from nodebatch.gui import MainWindow
    widget = QPlainTextEdit()
    widget.setPlainText("x" * 1048576)
    MainWindow._trim_document(widget)
    assert widget.document().characterCount() <= 262144
    assert tr('beginning of visible buffer omitted') in widget.toPlainText()
    assert widget.lineWrapMode() == QPlainTextEdit.LineWrapMode.NoWrap


def test_jsonl_byte_accounting_matches_utf8_file_on_both_platforms(tmp_path, bundle):
    history = History(tmp_path)
    try:
        run = job(bundle)
        history.start(run, Redactor())
        for _ in range(5):
            assert history.event(run.id, "a", "One", "stdout", '\u0441\u0442\u0440\u043e\u043a\u0430 \U0001f30d\n', 999999)
        data = history.log_path(run.id).read_bytes()
        assert history.sizes[run.id] == len(data)
        assert b"\r\n" not in data
    finally:
        history.close()


def test_old_sqlite_schema_migrates_without_fabricating_confirmation(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "history.sqlite3")
    db.executescript("""
        CREATE TABLE runs (id TEXT PRIMARY KEY, started REAL NOT NULL, finished REAL,
            status TEXT NOT NULL, summary TEXT NOT NULL, snapshot TEXT NOT NULL);
        CREATE TABLE results (run_id TEXT NOT NULL, host TEXT NOT NULL, step TEXT NOT NULL,
            status TEXT NOT NULL, duration REAL, code INTEGER, reason TEXT);
        INSERT INTO runs VALUES ('old', 0, 1, 'unknown', 'old run', '{}');
        INSERT INTO results VALUES ('old', 'a', 'One', 'unknown', 1, 255, 'legacy');
    """)
    db.commit()
    db.close()
    history = History(tmp_path)
    try:
        assert history.results("old")[0][4] == 255
        assert history.db.execute("SELECT ssh_code,command_code FROM results").fetchone() == (None, None)
    finally:
        history.close()
