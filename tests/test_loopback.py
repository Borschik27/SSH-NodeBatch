"""Opt-in integration: real system OpenSSH against our disposable loopback server."""
import os
import shutil
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("NODEBATCH_TEST_LOOPBACK") != "1",
                               reason="Set NODEBATCH_TEST_LOOPBACK=1 for local SSH/SFTP integration")


@pytest.fixture
def connection(qapp, tmp_path, bundle, wait):
    pytest.importorskip("paramiko")
    if not shutil.which("ssh") or not shutil.which("sftp"):
        pytest.skip("System OpenSSH required")
    from loopback_server import LoopbackServer
    from nodebatch.auth import AuthBroker
    from nodebatch.history import History
    from nodebatch.engine import JobRunner
    from nodebatch.models import Job, Host
    server = LoopbackServer(tmp_path / "remote")
    config = tmp_path / "loopback config"
    config.write_text(f"""Host nodebatch-loopback
    HostName 127.0.0.1
    Port {server.port}
    User tester
    PreferredAuthentications password
    PubkeyAuthentication no
    IdentityAgent none
    LogLevel DEBUG2
    GlobalKnownHostsFile none
""", encoding="utf-8")
    profile = bundle["settings"]["profiles"]["native"]
    profile.update(config=str(config), keys=[],
                                                   known_hosts=str(tmp_path / "known_hosts"))
    history, auth = History(tmp_path / "state"), AuthBroker()
    requests, runners = [], []
    def answer(request):
        requests.append((request["kind"], request["prompt"]))
        auth.answer(request["id"], "yes" if request["kind"] == "confirm" else "topsecret")
    auth.requested.connect(answer)
    def make(steps=(), transfer=None):
        job = Job.create([Host("nodebatch-loopback", os="linux")], steps, bundle, (), transfer)
        runner = JobRunner(job, history, auth)
        runner.sources = lambda: ()
        runners.append(runner)
        return runner
    make.profile = profile
    make.config = config
    yield make, history, auth, requests, server, tmp_path
    for runner in runners:
        if not runner.complete:
            runner.cancel()
    wait(lambda: all(r.complete for r in runners), 5)
    auth.close()
    history.close()
    server.close()
    for runner in runners:
        runner.deleteLater()
    auth.deleteLater()


def test_real_openssh_password_fingerprint_sudo_and_masking(connection, wait):
    from nodebatch.models import Step
    make, history, auth, requests, server, root = connection
    runner = make([Step("plain", "printf test-output"), Step("root", "emit-secret", True)])
    runner.start()
    wait(lambda: runner.complete, 90)
    assert history.runs()[0][2] == "success", (history.results(runner.job.id), requests, list(auth.pending))
    assert any(kind == "confirm" and "SHA256:" in text for kind, text in requests)
    assert any(kind == "secret" for kind, _ in requests)
    output = history.log_path(runner.job.id).read_text(encoding="utf-8")
    assert "test-output" in output and "topsecret" not in output and "***" in output
    assert (root / "known_hosts").is_file()


def test_changed_host_key_is_rejected(connection, wait):
    import paramiko
    from nodebatch.models import Step
    make, history, auth, requests, server, root = connection
    first = make([Step("plain", "printf test-output")])
    first.start()
    wait(lambda: first.complete, 90)
    assert history.runs()[0][2] == "success"
    server.key = paramiko.RSAKey.generate(2048)
    requests.clear()
    second = make([Step("must not run", "emit-secret")])
    second.start()
    wait(lambda: second.complete, 60)
    assert history.runs()[0][2] == "failed"
    assert not requests
    assert not any(row[1] == "must not run" for row in history.results(second.job.id))


def test_real_sftp_upload_download_and_overwrite(connection, wait):
    from nodebatch.models import Transfer
    make, history, auth, requests, server, root = connection
    source = root / "source with spaces.txt"
    source.write_text("file payload", encoding="utf-8")
    upload = make(transfer=Transfer("upload", str(source), "/file with spaces.txt"))
    upload.start()
    wait(lambda: upload.complete, 90)
    assert history.runs()[0][2] == "success", history.results(upload.job.id)
    assert server.path("/file with spaces.txt").read_text(encoding="utf-8") == "file payload"
    download_root = root / "downloads"
    download_root.mkdir()
    download = make(transfer=Transfer("download", str(download_root), "/file with spaces.txt"))
    download.start()
    wait(lambda: download.complete, 90)
    assert history.runs()[0][2] == "success", history.results(download.job.id)
    destination = download_root / "nodebatch-loopback/file with spaces.txt"
    assert destination.read_text(encoding="utf-8") == "file payload"
    destination.write_text("keep existing", encoding="utf-8")
    again = make(transfer=Transfer("download", str(download_root), "/file with spaces.txt"))
    again.overwrite_needed.connect(lambda worker, path: worker.confirm_overwrite(False))
    again.start()
    wait(lambda: again.complete, 90)
    assert destination.read_text(encoding="utf-8") == "keep existing"
    assert history.runs()[0][2] == "failed"




def test_encrypted_identity_file_passphrase(connection, wait):
    import os
    import subprocess

    import paramiko
    from nodebatch.models import Step

    make, history, auth, requests, server, root = connection

    identity = paramiko.RSAKey.generate(2048)
    server.authorized.add(identity.asbytes())

    keyfile = root / "identity with spaces"
    identity.write_private_key_file(
        str(keyfile),
        password="topsecret",
    )

    keyfile.chmod(0o600)

    if os.name == "nt":
        # Restrict ACLs on the disposable test SSH key.
        # Use .NET APIs without Get-Acl / Set-Acl.
        script = r"""
$ErrorActionPreference = 'Stop'

$path = $env:NODEBATCH_TEST_KEYFILE
$file = [System.IO.FileInfo]::new($path)
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User

# Read current NTFS permissions.
$acl = [System.IO.FileSystemAclExtensions]::GetAccessControl($file)

# Disable inheritance and remove inherited ACEs.
$acl.SetAccessRuleProtection($true, $false)

# Remove remaining explicit access rules.
$rules = $acl.GetAccessRules(
    $true,
    $false,
    [System.Security.Principal.SecurityIdentifier]
)

foreach ($entry in $rules) {
    [void]$acl.RemoveAccessRuleSpecific($entry)
}

# Set the current user as owner.
$acl.SetOwner($sid)

# Grant access only to the current user.
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
    $sid,
    [System.Security.AccessControl.FileSystemRights]::FullControl,
    [System.Security.AccessControl.AccessControlType]::Allow
)

$acl.AddAccessRule($rule)

# Apply the restricted ACL.
[System.IO.FileSystemAclExtensions]::SetAccessControl($file, $acl)
"""

        result = subprocess.run(
            [
                "pwsh.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            env={
                **os.environ,
                "NODEBATCH_TEST_KEYFILE": str(keyfile),
            },
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, (
            "Failed to configure Windows SSH key ACL:\n"
            f"{result.stdout}\n{result.stderr}"
        )

        # Verify ACL for CI diagnostics.
        acl_check = subprocess.run(
            ["icacls", str(keyfile)],
            capture_output=True,
            text=True,
            check=True,
        )

        print("SSH test key ACL:\n", acl_check.stdout)

    make.profile["keys"] = [str(keyfile)]

    text = make.config.read_text(encoding="utf-8")

    text = text.replace(
        "PreferredAuthentications password",
        "PreferredAuthentications publickey",
    ).replace(
        "PubkeyAuthentication no",
        "PubkeyAuthentication yes",
    )

    make.config.write_text(
        text + "\n    IdentitiesOnly yes\n",
        encoding="utf-8",
    )

    runner = make([
        Step("key login", "printf test-output"),
    ])

    runner.start()
    wait(lambda: runner.complete, 90)

    assert history.runs()[0][2] == "success", (
        history.results(runner.job.id)
    )

    assert any(
        kind == "secret" and "passphrase" in text.lower()
        for kind, text in requests
    )

    assert "topsecret" not in (
        history.log_path(runner.job.id)
        .read_text(encoding="utf-8")
    )



def test_secret_request_uses_real_ssh_stdin_not_argv(connection, wait, qapp):
    from PySide6.QtCore import QTimer
    from nodebatch.models import Step
    make, history, auth, requests, server, root = connection
    value = "synthetic-descriptor-private-value"
    runner = make([Step("descriptor", "emit-secret", secret_inputs=(("password", value),))])
    observed = []
    timer = QTimer()
    def observe():
        for worker in runner.active.values():
            if worker.phase == "command" and worker.process:
                observed.append(worker.process.arguments())
    timer.timeout.connect(observe)
    timer.start(1)
    try:
        runner.start()
        wait(lambda: runner.complete, 60)
    finally:
        timer.stop()
    assert history.runs()[0][2] == "success"
    assert observed and all(value not in str(argv) for argv in observed)
    assert server.requests[-1]["secrets"] == {"password": value}
    assert value not in history.log_path(runner.job.id).read_text(encoding="utf-8")
    assert value not in "\n".join(history.db.iterdump())
