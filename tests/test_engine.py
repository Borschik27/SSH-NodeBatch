import os
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QProcess, QProcessEnvironment

from nodebatch import transport
from nodebatch.auth import AuthBroker
from nodebatch.engine import JobRunner
from nodebatch.history import History
from nodebatch.models import Job, Host, Step, Transfer


@pytest.fixture
def harness(qapp, tmp_path, bundle, monkeypatch, wait):
    fake = str(Path(__file__).with_name("fake_ssh.py").resolve())
    def cmd(profile, execution, host, step=None, marker="", resolve=False, transfer=False):
        return sys.executable, [fake, "resolve" if resolve else "sftp" if transfer else "command",
                                host.alias, step.command if step else "", marker]
    monkeypatch.setattr(transport, "command", cmd)
    history, auth = History(tmp_path / "data"), AuthBroker()
    runners = []
    def make(steps=(Step("one", "ok"),), count=3, mode="sequential", parallelism=4,
             stop=True, transfer=None, requirements=None):
        bundle["settings"]["execution"].update(mode=mode, parallelism=parallelism, stop_on_error=stop)
        if requirements:
            bundle["requirements"] = requirements
        job = Job.create([Host(f"h{i}") for i in range(count)], steps, bundle, (), transfer,
                         secrets=["topsecret"])
        runner = JobRunner(job, history, auth, helper=sys.executable)
        runner.sources = lambda: ()
        runners.append(runner)
        return runner
    yield make, history, auth
    for runner in runners:
        if not runner.complete:
            runner.cancel()
    wait(lambda: all(r.complete for r in runners), 5)
    auth.close()
    history.close()
    for runner in runners:
        runner.deleteLater()
    auth.deleteLater()


def test_sequential_stop_and_codes(harness, wait):
    make, history, auth = harness
    runner = make([Step("one", "fail-test"), Step("never", "ok")])
    runner.start()
    wait(lambda: runner.complete, 30)
    assert history.runs()[0][2] == "failed"
    results = history.results(runner.job.id)
    assert len(results) == 3
    assert results[0][0:3] == ("h0", "one", "failed")
    assert results[0][4] == 7
    assert all(row[2] == "skipped" for row in results[1:])


def test_continue_and_host_first_order(harness, wait):
    make, history, auth = harness
    runner = make([Step("one", "ok"), Step("two", "fail-test")], count=2, stop=False)
    runner.start()
    wait(lambda: runner.complete, 30)
    assert [(row[0], row[1]) for row in history.results(runner.job.id)] == [
        ("h0", "one"), ("h0", "two"), ("h1", "one"), ("h1", "two")]


def test_parallel_limit_and_cancellation_unknown(harness, wait):
    make, history, auth = harness
    runner = make([Step("slow", "sleep-test")], count=6, mode="parallel", parallelism=2)
    started = set()
    runner.event.connect(lambda host, step, channel, text: started.add(host) if "started" in text else None)
    runner.start()
    wait(lambda: len(started) == 2, 30)
    assert len(runner.active) == 2 and len(runner.pending) == 4
    runner.cancel()
    wait(lambda: runner.complete)
    assert history.runs()[0][2] == "unknown"
    assert sum(row[2] == "unknown" for row in history.results(runner.job.id)) == 2


def test_timeout_is_unknown(harness, wait):
    make, history, auth = harness
    runner = make([Step("slow", "sleep-test", timeout=1)], count=1)
    runner.start()
    wait(lambda: runner.complete, 30)
    assert history.runs()[0][2] == "unknown"
    assert history.db.execute("SELECT command_started FROM results").fetchone()[0] == 1


def test_sudo_split_prompt_and_stream_secret(harness, wait):
    make, history, auth = harness
    auth.requested.connect(lambda r: auth.answer(r["id"], "topsecret"))
    runner = make([Step("root", "sudo-test", True), Step("secret", "split-secret")], count=1)
    runner.start()
    wait(lambda: runner.complete, 30)
    assert history.runs()[0][2] == "success"
    output = history.log_path(runner.job.id).read_text(encoding="utf-8")
    assert "topsecret" not in output and "__NODEBATCH_SUDO_" not in output
    assert "***" in output
    assert not runner.job.secrets and not runner.job.steps


def test_changed_sources_pause_until_review(harness, wait):
    make, history, auth = harness
    runner = make(count=1)
    state = [("changed",)]
    runner.sources = lambda: tuple(state)
    runner.start()
    wait(lambda: runner.is_paused)
    assert not runner.active
    runner.resume()
    wait(lambda: runner.complete, 30)
    assert history.runs()[0][2] == "success"


def test_os_compatibility_blocks_mutations(harness, wait, monkeypatch):
    monkeypatch.setenv("NODEBATCH_TEST_OS", "FreeBSD")
    make, history, auth = harness
    runner = make(count=1, requirements={"os": ["linux"], "shells": ["sh"]})
    runner.start()
    wait(lambda: runner.complete, 30)
    assert history.runs()[0][2] == "failed"
    assert not any(row[1] == "one" for row in history.results(runner.job.id))


def test_posix_freebsd_scenario(harness, wait, monkeypatch):
    monkeypatch.setenv("NODEBATCH_TEST_OS", "FreeBSD")
    make, history, auth = harness
    runner = make(count=1, requirements={"os": ["unix"], "shells": ["sh"]})
    runner.start()
    wait(lambda: runner.complete, 30)
    assert history.runs()[0][2] == "success"


def test_upload_staging_and_refuse_overwrite(harness, wait, tmp_path, monkeypatch):
    make, history, auth = harness
    source = tmp_path / "source"
    source.write_text("data", encoding="utf-8")
    monkeypatch.setenv("NODEBATCH_TEST_EXISTS", "1")
    runner = make((), count=1, transfer=Transfer("upload", str(source), "/tmp/destination"))
    requests = []
    runner.overwrite_needed.connect(lambda worker, dest: (requests.append(dest), worker.confirm_overwrite(False)))
    runner.start()
    wait(lambda: runner.complete, 30)
    assert requests == ["/tmp/destination"]
    assert history.runs()[0][2] == "failed"


def test_upload_staging_success(harness, wait, tmp_path):
    make, history, auth = harness
    source = tmp_path / "source"
    source.write_text("data", encoding="utf-8")
    runner = make((), count=1, transfer=Transfer("upload", str(source), "/tmp/destination"))
    runner.start()
    wait(lambda: runner.complete, 30)
    assert history.runs()[0][2] == "success"


def test_askpass_real_ipc(qapp, wait):
    broker = AuthBroker()
    secret = "password with spaces"
    broker.requested.connect(lambda r: broker.answer(r["id"], secret))
    env = QProcessEnvironment.systemEnvironment()
    for key, value in broker.register("job", "test-host", 20).items():
        env.insert(key, value)
    process = QProcess()
    process.setProcessEnvironment(env)
    process.start(sys.executable, ["-m", "nodebatch.askpass", "Password:"])
    wait(lambda: process.state() == QProcess.ProcessState.NotRunning, 45)
    assert process.exitCode() == 0
    assert bytes(process.readAllStandardOutput()).decode().strip() == secret
    broker.close()


def test_log_failure_stops_queue(harness, wait, monkeypatch):
    make, history, auth = harness
    runner = make([Step("one", "ok")], count=4)
    failures, results = [], []
    runner.fatal.connect(failures.append)
    runner.result.connect(lambda *row: results.append(row))
    def no_space(*args, **kwargs):
        raise OSError("simulated disk full")
    monkeypatch.setattr(history, "event", no_space)
    runner.start()
    wait(lambda: runner.complete, 30)
    assert runner.storage_broken and failures
    assert not any(row[0] != "h0" and row[2] == "success" for row in results)
