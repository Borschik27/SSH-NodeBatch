"""Opt-in real Windows -> WSL -> Windows askpass round trip."""
import json
import os
import uuid
from pathlib import Path
import pytest
from PySide6.QtCore import QProcess
from nodebatch.auth import AuthBroker
from nodebatch.engine import JobRunner
from nodebatch.history import History
from nodebatch.models import Job, Host, Step, Transfer

pytestmark = pytest.mark.skipif(os.name != "nt" or not os.environ.get("NODEBATCH_TEST_WSL_HELPER"),
                               reason="Requires an explicit local Windows helper and WSL test environment")


def test_windows_wsl_password_sudo_and_sftp(qapp, wait, tmp_path, bundle):
    distro = os.environ["NODEBATCH_TEST_WSL_DISTRO"]
    repo = os.environ["NODEBATCH_TEST_WSL_REPO"].rstrip("/")
    python = os.environ["NODEBATCH_TEST_WSL_PYTHON"]
    helper = os.environ["NODEBATCH_TEST_WSL_HELPER"]
    root = repo + "/.test-temp/wsl-bridge-" + uuid.uuid4().hex
    local_root = Path("\\\\wsl.localhost\\" + distro + root.replace("/", "\\"))
    local_root.mkdir(parents=True)
    server = QProcess()
    server.start("wsl.exe", ["--distribution", distro, "--exec", python,
                             repo + "/tools/wsl_test_server.py", root])
    wait(lambda: server.canReadLine() or server.state() == QProcess.ProcessState.NotRunning, 30)
    line = bytes(server.readLine()).decode()
    assert line, bytes(server.readAllStandardError()).decode()
    info = json.loads(line)
    config = local_root / "ssh-config"
    config.write_text(f"""Host nodebatch-wsl-loopback
    HostName 127.0.0.1
    Port {info['port']}
    User tester
    PreferredAuthentications password
    PubkeyAuthentication no
    GlobalKnownHostsFile none
""", encoding="utf-8")
    profile = bundle["settings"]["profiles"]["native"]
    profile.update(backend="wsl", wsl_distribution=distro, wsl_home=info["home"],
                   config=root + "/ssh-config", known_hosts=root + "/known_hosts", keys=[])
    from nodebatch.discovery import discover
    detected = discover(profile, bundle["dependencies"])
    assert distro in detected["distros"]
    assert detected["choices"]["ssh"] and detected["choices"]["sftp"]
    assert not detected["missing"]
    bundle["settings"]["execution"].update(step_timeout=20, auth_timeout=20)
    history, auth = History(tmp_path / "state"), AuthBroker()
    requests, runners = [], []
    def answer(request):
        requests.append(request["kind"])
        auth.answer(request["id"], "yes" if request["kind"] == "confirm" else "topsecret")
    auth.requested.connect(answer)
    def run(steps=(), transfer=None):
        job = Job.create([Host("nodebatch-wsl-loopback", os="linux")], steps, bundle, (), transfer)
        runner = JobRunner(job, history, auth, helper=helper)
        runner.sources = lambda: ()
        runner.overwrite_needed.connect(lambda worker, path: worker.confirm_overwrite(False))
        runners.append(runner)
        runner.start()
        try:
            wait(lambda: runner.complete, 40)
        except AssertionError as exc:
            raise AssertionError([(w.phase, w.current_step, str(w.process.state()) if w.process else None)
                                  for w in runner.active.values()]) from exc
        assert history.runs()[0][2] == "success", history.results(runner.job.id)
        assert "topsecret" not in history.log_path(runner.job.id).read_text(encoding="utf-8")
    try:
        run([Step("plain", "printf test-output"), Step("root", "emit-secret", True)])
        private = "synthetic-wsl-descriptor-value"
        run([Step("descriptor", "emit-secret", secret_inputs=(("token", private),))])
        assert private not in "\n".join(history.db.iterdump())
        assert "confirm" in requests and "secret" in requests
        source = tmp_path / "WSL source with spaces"
        source.write_text("WSL transfer", encoding="utf-8")
        run(transfer=Transfer("upload", str(source), "/destination"))
        assert (local_root / "remote/destination").read_text(encoding="utf-8") == "WSL transfer"
        download = tmp_path / "download"
        download.mkdir()
        run(transfer=Transfer("download", str(download), "/destination"))
        assert (download / "nodebatch-wsl-loopback/destination").read_text(encoding="utf-8") == "WSL transfer"
    finally:
        for runner in runners:
            if not runner.complete:
                runner.cancel()
        wait(lambda: all(r.complete for r in runners), 5)
        auth.close()
        history.close()
        server.write(b"stop\n")
        server.closeWriteChannel()
        wait(lambda: server.state() == QProcess.ProcessState.NotRunning, 10)
