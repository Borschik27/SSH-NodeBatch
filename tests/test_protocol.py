from nodebatch.i18n import tr
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import pytest

from nodebatch.config import validate_scenario
from nodebatch.models import Host, Step
from nodebatch.transport import remote_command
from nodebatch.wire import Decoder, WireError
from test_engine import harness

TOKEN = "a" * 64


def frame(kind, **kwargs):
    return (json.dumps(dict(v=2, type=kind, token=TOKEN, **kwargs)) + "\n").encode()


def test_split_wire_and_user_forgery_is_data():
    fake = frame("result", code=0)
    content = b'{"v":2,"type":"ready"}\n' + frame("started") + frame("data", channel="stderr", data=base64.b64encode(fake).decode()) + frame("result", code=255)
    for stride in (1, 7, len(content)):
        decoder = Decoder(TOKEN)
        events = []
        for offset in range(0, len(content), stride):
            events.extend(decoder.feed(content[offset:offset + stride]))
        decoder.feed(b"", final=True)
        assert decoder.result == 255
        assert events == [("ready", None), ("started", None), ("stderr", fake)]


@pytest.mark.parametrize("tail", [
    frame("result", code=True), frame("result", code=256), b"not-json\n",
    frame("result", code=0) + frame("result", code=0),
    frame("result", code=0) + b"truncated",
    frame("result", code=0).replace(TOKEN.encode(), b"b" * 64),
    frame("result", code=0, output_incomplete="yes"),
    frame("data", channel="stdout", data="!invalid!"), b"x" * 32769,
])
def test_invalid_wire_never_confirms_success(tail):
    decoder = Decoder(TOKEN)
    with pytest.raises(WireError):
        decoder.feed(b'{"v":2,"type":"ready"}\n' + frame("started") + tail, final=True)
    assert decoder.result is None


def bound_scenario(sudo=False):
    return validate_scenario({"id": "fd", "name": "FD", "os": ["unix"], "shells": ["sh"],
        "parameters": {"password": {"secret": True}},
        "steps": [{"name": "One", "sudo": sudo, "secrets": ["password"],
                   "command": 'cat "/dev/fd/$NODEBATCH_SECRET_password_FD"; cat; exit 255'}]})


def test_explicit_binding_keeps_secrets_out_of_command_and_argv():
    value = "synthetic-private-value"
    steps, secrets = bound_scenario().render({"password": value})
    assert value not in steps[0].command and value not in repr(steps)
    assert steps[0].secret_inputs == (("password", value),)
    assert value in secrets
    for sudo in (False, True):
        from dataclasses import replace
        argv = remote_command(Host("a"), replace(steps[0], protocol=True, sudo=sudo), "prompt")
        assert value not in argv


@pytest.mark.skipif(os.name == "nt", reason="Remote helper targets Unix; requires pass_fds and sh")
def test_real_remote_helper_eof_secret_and_exit255(tmp_path):
    from dataclasses import replace
    value = "synthetic-private-value"
    step = bound_scenario().render({"password": value})[0][0]
    args = shlex.split(remote_command(Host("a"), replace(step, protocol=True), ""))
    args[0] = sys.executable
    request = json.dumps({"token": TOKEN, "shell": "sh", "command": step.command,
                          "secrets": dict(step.secret_inputs)}).encode() + b"\n"
    completed = subprocess.run(args, input=request, capture_output=True, timeout=5, cwd=tmp_path)
    assert completed.returncode == 0 and not completed.stderr
    decoder = Decoder(TOKEN)
    events = decoder.feed(completed.stdout, final=True)
    assert decoder.result == 255
    assert b"".join(data for channel, data in events if channel == "stdout") == value.encode()
    assert not list(tmp_path.iterdir())


def stop_background(pid_file):
    if pid_file.exists():
        try:
            import signal
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.skipif(os.name == "nt", reason="Remote helper targets Unix")
@pytest.mark.parametrize("shell", ["sh", "bash"])
@pytest.mark.parametrize("background", ["sleep 30", "(while :; do printf 'tail\\n'; sleep .01; done)"])
def test_shell_exit_is_confirmed_with_silent_or_busy_inherited_streams(tmp_path, shell, background):
    from importlib.resources import files
    pid_file = tmp_path / "child-pid"
    command = f"printf foreground; {background} & printf '%s' $! > {shlex.quote(str(pid_file))}; exit 7"
    request = json.dumps({"token": TOKEN, "shell": shell, "command": command}).encode() + b"\n"
    try:
        start = time.monotonic()
        completed = subprocess.run([sys.executable, "-u", "-c", files("nodebatch").joinpath("remote_agent.txt").read_text()],
                                   input=request, capture_output=True, timeout=5)
        assert completed.returncode == 0 and not completed.stderr
        assert time.monotonic() - start < 4
        decoder = Decoder(TOKEN)
        events = decoder.feed(completed.stdout, final=True)
        assert decoder.result == 7
        assert ("output_incomplete", None) in events
        assert b"foreground" in b"".join(data for channel, data in events if channel == "stdout")
    finally:
        stop_background(pid_file)


@pytest.fixture
def real_helper(harness, monkeypatch):
    from nodebatch import transport
    original = transport.command
    def cmd(profile, execution, host, step=None, marker="", **kwargs):
        if step and step.protocol:
            args = shlex.split(remote_command(host, step, marker))
            if step.sudo:
                args = [sys.executable, str(Path(__file__).with_name("local_sudo.py")), marker,
                        sys.executable, *args[args.index("--") + 2:]]
            else:
                args[0] = sys.executable
            return args[0], args[1:]
        return original(profile, execution, host, step, marker, **kwargs)
    monkeypatch.setattr(transport, "command", cmd)
    return harness


@pytest.mark.skipif(os.name == "nt", reason="Remote helper targets Unix")
@pytest.mark.parametrize("command", [
    "sleep 30 </dev/null >/dev/null 2>&1 & printf '%s' $! > {pid_file}; printf foreground",
    "head -c 2000000 /dev/zero; sleep 1.2",
])
def test_detached_background_and_foreground_output_are_complete(tmp_path, command):
    from importlib.resources import files
    pid_file = tmp_path / "child-pid"
    request = json.dumps({"token": TOKEN, "shell": "sh", "command": command.format(pid_file=shlex.quote(str(pid_file)))}).encode() + b"\n"
    try:
        completed = subprocess.run([sys.executable, "-u", "-c", files("nodebatch").joinpath("remote_agent.txt").read_text()],
                                   input=request, capture_output=True, timeout=5)
        assert completed.returncode == 0 and not completed.stderr
        decoder = Decoder(TOKEN)
        events = decoder.feed(completed.stdout, final=True)
        assert decoder.result == 0 and ("output_incomplete", None) not in events
        output = b"".join(data for channel, data in events if channel == "stdout")
        assert output == (b"foreground" if pid_file.exists() else b"\0" * 2000000)
    finally:
        stop_background(pid_file)


@pytest.mark.skipif(os.name == "nt", reason="Remote helper targets Unix")
def test_engine_retains_shell_success_and_records_incomplete_background_output(real_helper, wait, tmp_path):
    make, history, auth = real_helper
    pid_file = tmp_path / "child-pid"
    try:
        command = f"sleep 30 & printf '%s' $! > {shlex.quote(str(pid_file))}; printf foreground"
        runner = make([Step("Background", command)], count=1)
        runner.start()
        wait(lambda: runner.complete, 5)
        assert history.runs()[0][2] == "success"
        assert history.db.execute("SELECT command_code,command_started FROM results").fetchone() == (0, 1)
        reason = history.db.execute("SELECT output_incomplete_reason FROM runs").fetchone()[0]
        assert reason == tr('Output streams remained open after the shell exited; output may be incomplete.')
        assert reason in history.log_path(runner.job.id).read_text()
    finally:
        stop_background(pid_file)


@pytest.mark.skipif(os.name == "nt", reason="Remote helper targets Unix")
@pytest.mark.parametrize("sudo,nopasswd,retry", [(False, False, False), (True, False, True), (True, True, False)])
def test_engine_secret_eof_auth_and_confirmed_result(real_helper, wait, monkeypatch, sudo, nopasswd, retry):
    make, history, auth = real_helper
    if nopasswd:
        monkeypatch.setenv("NODEBATCH_TEST_NOPASSWD", "1")
    answers = []
    def answer(request):
        answers.append(request)
        auth.answer(request["id"], "wrong" if retry and len(answers) == 1 else "synthetic-sudo-password")
    auth.requested.connect(answer)
    value = "synthetic-private-value"
    steps, secrets = bound_scenario(sudo).render({"password": value})
    runner = make(steps, count=1)
    runner.redactor.add(value)
    runner.start()
    wait(lambda: runner.complete, 15)
    assert history.runs()[0][2] == "failed"  # confirmed command exit, not SSH failure
    assert history.results(runner.job.id)[0][4] == 255
    assert history.db.execute("SELECT ssh_code,command_code,transport_reason FROM results").fetchone() == (0, 255, "")
    assert value not in history.log_path(runner.job.id).read_text(encoding="utf-8")
    assert len(answers) == (2 if retry else 0)


@pytest.mark.parametrize("command,status,ssh_code,confirmed", [
    ("exit255-test", "failed", 0, 255),
    ("disconnect-test", "unknown", 255, None),
    ("drop-after-test", "success", 255, 0),
    ("corrupt-test", "unknown", None, None),
])
def test_result_is_independent_of_ssh_exit(harness, wait, command, status, ssh_code, confirmed):
    make, history, auth = harness
    runner = make([Step("One", command)], count=1)
    runner.start()
    wait(lambda: runner.complete)
    assert history.runs()[0][2] == status
    raw, actual, reason = history.db.execute("SELECT ssh_code,command_code,transport_reason FROM results").fetchone()
    assert actual == confirmed
    if ssh_code is not None:
        assert raw == ssh_code
    assert status == "failed" or reason


@pytest.mark.skipif(os.name == "nt", reason="Remote helper targets Unix")
def test_sudo_cancel_and_late_reply_never_reach_next_process(real_helper, wait):
    make, history, auth = real_helper
    pending = []
    auth.requested.connect(pending.append)
    runner = make([Step("One", "cat", True), Step("Never", "true")], count=1)
    runner.start()
    wait(lambda: pending)
    request = pending[0]
    auth.answer(request["id"], None)
    wait(lambda: runner.complete)
    # A UI holding an old dialog cannot send to another process after cancellation.
    auth.answer(request["id"], "synthetic-sudo-password")
    assert not auth.pending
    assert not any(row[1] == "Never" for row in history.results(runner.job.id))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Inspect actual Linux process argv/environment")
def test_remote_interpreter_and_child_argv_and_environment_are_secret_free(tmp_path):
    from dataclasses import replace
    script = """import os
from pathlib import Path
value = os.read(int(os.environ['NODEBATCH_SECRET_token_FD']), 4096)
assert value
for pid in (os.getpid(), os.getppid()):
    for field in ('cmdline', 'environ'):
        if value in Path('/proc/%s/%s' % (pid, field)).read_bytes():
            raise SystemExit(10)
print('checked')
"""
    step = Step("argv", "exec python3 -c " + shlex.quote(script), protocol=True)
    args = shlex.split(remote_command(Host("a"), step, ""))
    args[0] = sys.executable
    request = json.dumps({"token": TOKEN, "shell": "sh", "command": step.command,
                          "secrets": {"token": "synthetic-descriptor-private-value"}}).encode() + b"\n"
    completed = subprocess.run(args, input=request, capture_output=True, timeout=5, cwd=tmp_path)
    decoder = Decoder(TOKEN)
    events = decoder.feed(completed.stdout, final=True)
    assert decoder.result == 0 and completed.returncode == 0
    assert any(data == b"checked\n" for channel, data in events if channel == "stdout")
    assert not list(tmp_path.iterdir())


def test_startup_text_cannot_impersonate_session_ready():
    decoder = Decoder(TOKEN, "c" * 32)
    with pytest.raises(WireError):
        decoder.feed(b'{"v":2,"type":"ready"}\n')
    assert not decoder.ready


@pytest.mark.parametrize("messages", [
    frame("data", channel="stdout", data="YQ=="), frame("result", code=0),
    frame("started") + frame("started"),
    frame("started") + frame("not_started", reason="shell_unavailable"),
    frame("not_started", reason="shell_unavailable") + frame("started"),
    frame("not_started", reason="unknown"), frame("error", reason="execution_failed"),
])
def test_execution_state_transitions_reject_impossible_sequences(messages):
    decoder = Decoder(TOKEN)
    with pytest.raises(WireError):
        decoder.feed(b'{"v":2,"type":"ready"}\n' + messages, final=True)
    assert decoder.result is None and decoder.not_started is None


@pytest.mark.parametrize("command,status,started,ssh_code", [
    ("prepare-fail-test", "failed", 0, 125), ("runtime-error-test", "unknown", 1, 125),
    ("no-ready-test", "failed", 0, 127),
])
def test_preparation_failure_is_distinct_from_running_failure(harness, wait, command, status, started, ssh_code):
    make, history, auth = harness
    runner = make([Step("One", command)], count=1)
    runner.start()
    wait(lambda: runner.complete)
    assert history.runs()[0][2] == status
    assert history.db.execute("SELECT command_started,command_code,ssh_code FROM results").fetchone() == (started, None, ssh_code)
    if not started:
        assert tr('was not started') in history.results(runner.job.id)[0][-1]


@pytest.mark.skipif(os.name == "nt", reason="Remote helper targets Unix")
def test_missing_shell_is_confirmed_not_started(real_helper, wait, monkeypatch):
    make, history, auth = real_helper
    monkeypatch.setenv("PATH", "")
    runner = make([Step("One", "printf SHOULD_NOT_APPEAR")], count=1)
    runner.start()
    wait(lambda: runner.complete)
    assert history.runs()[0][2] == "failed" and not runner.had_unknown
    assert history.db.execute("SELECT command_started,command_code FROM results").fetchone() == (0, None)
    assert tr('The shell is not available') in history.results(runner.job.id)[0][-1]


@pytest.mark.skipif(os.name == "nt", reason="Remote helper targets Unix")
@pytest.mark.parametrize("size", [32768, 150000])
def test_remote_size_boundary_is_enforced_before_shell_launch(tmp_path, size):
    command = "#" + "x" * (size - len("\nprintf done") - 1) + "\nprintf done"
    step = Step("One", "true", protocol=True)
    args = shlex.split(remote_command(Host("a"), step, ""))
    args[0] = sys.executable
    request = json.dumps({"token": TOKEN, "shell": "sh", "command": command, "secrets": {}}).encode() + b"\n"
    completed = subprocess.run(args, input=request, capture_output=True, cwd=tmp_path, timeout=5)
    decoder = Decoder(TOKEN)
    decoder.feed(completed.stdout, final=True)
    if size == 32768:
        assert decoder.result == 0 and decoder.started
    else:
        assert decoder.not_started == "command_too_large" and not decoder.started
        assert decoder.result is None and completed.returncode == 125


def test_large_command_is_rejected_locally_including_after_substitution():
    with pytest.raises(ValueError, match=tr('32 KiB')):
        Step("One", '\u044f' * 20000)
    scenario = validate_scenario({"id": "large", "name": "Large", "os": ["unix"], "shells": ["sh"],
        "parameters": {"value": {}}, "steps": [{"name": "One", "command": "printf %s {{value}}"}]})
    with pytest.raises(ValueError, match=tr('32 KiB')):
        scenario.render({"value": '\u044f' * 20000})


def test_burst_output_and_slow_persistence_do_not_cancel_command(harness, wait, monkeypatch):
    import time
    make, history, auth = harness
    original = history.event
    paused = False
    def slow(job_id, host, step, channel, text, max_bytes):
        nonlocal paused
        if step == "Burst" and channel in ("stdout", "stderr") and not paused:
            paused = True
            time.sleep(.25)
        return original(job_id, host, step, channel, text, max_bytes)
    monkeypatch.setattr(history, "event", slow)
    runner = make([Step("Burst", "burst-test", timeout=30)], count=1)
    runner.start()
    wait(lambda: runner.complete, 30)
    assert paused and history.runs()[0][2] == "success"
    entries = [json.loads(line) for line in history.log_path(runner.job.id).read_text(encoding="utf-8").splitlines()]
    assert sum(len(row["text"]) for row in entries if row["step"] == "Burst" and row["channel"] in ("stdout", "stderr")) == 200 * 16384
