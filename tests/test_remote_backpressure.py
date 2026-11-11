"""Real helper pipes with short reads and a deliberately stalled wire receiver."""
import json
import os
import shlex
import subprocess
import sys
import time

import pytest

from nodebatch.wire import Decoder

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Remote helper targets Unix")
TOKEN = "c" * 64

# Short reads are legal pipe behavior. Make them deterministic so more than one
# foreground frame remains at shell exit, regardless of the kernel pipe size.
BOOTSTRAP = """
import os, subprocess, sys
from pathlib import Path
source, marker = sys.argv[1:]
read = os.read
os.read = lambda fd, size: read(fd, min(size, 1024))
original = subprocess.Popen
class ObservedProcess(original):
    def poll(self):
        code = super().poll()
        if code is not None:
            Path(marker).touch()
        return code
subprocess.Popen = ObservedProcess
sys.argv = ['remote-helper']
exec(compile(source, 'remote_agent.txt', 'exec'))
"""


@pytest.fixture
def launch(tmp_path):
    import fcntl
    from importlib.resources import files
    if not hasattr(fcntl, "F_SETPIPE_SZ"):
        pytest.skip("Needs configurable pipe size to force transport backpressure")
    processes = []
    def start(command):
        marker = tmp_path / ("shell-exited-" + str(len(processes)))
        source = files("nodebatch").joinpath("remote_agent.txt").read_text()
        process = subprocess.Popen([sys.executable, "-u", "-c", BOOTSTRAP, source, str(marker)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        processes.append(process)
        fcntl.fcntl(process.stdout.fileno(), fcntl.F_SETPIPE_SZ, 4096)
        ready = process.stdout.readline()
        assert json.loads(ready) == {"v": 2, "type": "ready"}
        process.stdin.write(json.dumps({"token": TOKEN, "shell": "sh", "command": command}).encode() + b"\n")
        process.stdin.close()
        process.stdin = None
        return process, marker, ready
    yield start
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def decode(content):
    decoder = Decoder(TOKEN)
    events = []
    for offset in range(0, len(content), 32768):
        events.extend(decoder.feed(content[offset:offset + 32768]))
    decoder.feed(b"", final=True)
    return decoder, events


def until(predicate, seconds=4):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.01)
    assert predicate(), "Timed out waiting for helper progress"


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
def test_stalled_delivery_preserves_all_normal_output_after_shell_exit(launch, channel):
    import selectors
    redirect = " >&2" if channel == "stderr" else ""
    process, marker, prefix = launch("head -c 8192 /dev/zero" + redirect + "; exit 7")
    # Advance transport only until actual shell exit has been observed, then
    # stop reading for longer than the inherited-stream grace period.
    deadline = time.monotonic() + 4
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while not marker.exists() and time.monotonic() < deadline:
            if selector.select(.02):
                prefix += os.read(process.stdout.fileno(), 256)
    assert marker.exists()
    time.sleep(1.4)
    assert process.poll() is None  # Delivery is pending, not timed out locally.
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0 and not stderr
    decoder, events = decode(prefix + stdout)
    assert decoder.result == 7 and ("output_incomplete", None) not in events
    assert b"".join(data for name, data in events if name == channel) == b"\0" * 8192


def test_inherited_stream_grace_expires_while_wire_is_blocked(launch, tmp_path):
    from test_protocol import stop_background
    probe = tmp_path / "probe"
    pid_file = tmp_path / "child-pid"
    script = ("import os,time; time.sleep(1.8)\n"
              "try: os.write(1,b'late')\n"
              "except BrokenPipeError: state='closed'\n"
              "else: state='open'\n"
              f"open({str(probe)!r},'w').write(state)\n")
    command = (f"{shlex.quote(sys.executable)} -c {shlex.quote(script)} & "
               f"printf '%s' $! > {shlex.quote(str(pid_file))}; head -c 8192 /dev/zero")
    try:
        process, marker, ready = launch(command)
        until(lambda: probe.exists() and probe.read_text() in ("closed", "open"))
        assert probe.read_text() == "closed"
        assert marker.exists() and process.poll() is None
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0 and not stderr
        decoder, events = decode(ready + stdout)
        assert decoder.result == 0 and ("output_incomplete", None) in events
        assert b"".join(data for channel, data in events if channel == "stdout") == b"\0" * 8192
    finally:
        stop_background(pid_file)


def test_blocked_wire_backpressures_producer_instead_of_growing_queue(launch, tmp_path):
    progress = tmp_path / "progress"
    size = 2 * 1048576
    script = ("import os\n"
              f"progress = {str(progress)!r}\n"
              "pending = progress + '.tmp'\n"
              f"for n in range({size // 4096}):\n"
              " os.write(1,b'x'*4096)\n"
              " with open(pending,'w') as stream:\n"
              "  stream.write(str((n+1)*4096))\n"
              " os.replace(pending,progress)\n")
    process, marker, ready = launch(f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}")
    # Publish only closed snapshots. Existence now means a complete counter,
    # even if the producer is descheduled while writing the next temporary file.
    until(progress.exists)
    time.sleep(.4)
    written = int(progress.read_text())
    assert 0 < written < 1048576 and process.poll() is None and not marker.exists()
    stdout, stderr = process.communicate(timeout=15)
    assert process.returncode == 0 and not stderr
    decoder, events = decode(ready + stdout)
    assert decoder.result == 0 and ("output_incomplete", None) not in events
    assert b"".join(data for channel, data in events if channel == "stdout") == b"x" * size
