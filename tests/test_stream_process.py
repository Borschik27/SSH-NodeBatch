import hashlib
import sys
import time
import threading
import os
import pytest

from PySide6.QtCore import QProcess

from nodebatch.stream_process import StreamingProcess


@pytest.mark.skipif(os.name != "nt", reason="Windows NTSTATUS exit codes")
def test_windows_native_failure_is_not_reported_as_normal_exit(qapp, wait):
    process = StreamingProcess()
    result = []
    process.finished.connect(lambda code, status: result.append((code, status)))
    process.started.connect(process.closeWriteChannel)
    process.start(sys.executable, ["-I", "-c", "import ctypes; ctypes.windll.kernel32.ExitProcess(0xC0000005)"])
    wait(lambda: result)
    assert result[0][0] != 0 and result[0][1] == QProcess.ExitStatus.CrashExit
    process.deleteLater()


def test_spawn_resource_failure_is_reported_without_hanging(qapp, wait, monkeypatch):
    import nodebatch.stream_process as module
    def fail(*args, **kwargs):
        raise RuntimeError("simulated resource exhaustion")
    monkeypatch.setattr(module._SPAWNERS, "submit", fail)
    process = StreamingProcess()
    errors = []
    process.errorOccurred.connect(errors.append)
    process.start(sys.executable, ["-I", "-c", "pass"])
    wait(lambda: errors)
    assert errors == [QProcess.ProcessError.FailedToStart]
    assert process.state() == QProcess.ProcessState.NotRunning
    process.deleteLater()


def test_slow_process_creation_does_not_block_qt_or_send_after_cancel(qapp, wait, monkeypatch):
    import nodebatch.stream_process as module
    original = module.subprocess.Popen
    entered, release = threading.Event(), threading.Event()
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(module.subprocess, "Popen", delayed)
    process = StreamingProcess()
    started, result = [], []
    process.started.connect(lambda: started.append(True))
    process.finished.connect(lambda *args: result.append(args))
    before = time.monotonic()
    process.start(sys.executable, ["-I", "-c", "pass"])
    try:
        assert time.monotonic() - before < .5
        wait(lambda: entered.is_set())
        assert process.state() == QProcess.ProcessState.Starting
        process.kill()
        release.set()
        wait(lambda: result)
        assert not started and result[0][1] == QProcess.ExitStatus.CrashExit
    finally:
        release.set()
        if process.state() != QProcess.ProcessState.NotRunning:
            process.kill()
            wait(lambda: process.state() == QProcess.ProcessState.NotRunning)
        process.deleteLater()


def test_paused_consumer_backpressures_both_pipes_without_loss(qapp, wait):
    process = StreamingProcess()
    result = []
    process.finished.connect(lambda code, status: result.append((code, status)))
    process.started.connect(process.closeWriteChannel)
    code = "import sys\nfor _ in range(32):\n sys.stdout.buffer.write(b'x'*65536)\n sys.stderr.buffer.write(b'y'*65536)\n"
    process.start(sys.executable, ["-I", "-c", code])
    try:
        wait(lambda: process.bytesAvailable() >= 224 * 1024)
        time.sleep(.15)  # Consumer deliberately stops, native pipes must apply pressure.
        assert process.state() == QProcess.ProcessState.Running and not result
        assert process._stdout.size() <= 262144 and process._stderr.size() <= 262144
        hashes = {channel: hashlib.sha256() for channel in ("stdout", "stderr")}
        counts = {channel: 0 for channel in hashes}
        def drain(channel):
            data = process.readAllStandardOutput() if channel == "stdout" else process.readAllStandardError()
            hashes[channel].update(data)
            counts[channel] += len(data)
        process.readyReadStandardOutput.connect(lambda: drain("stdout"))
        process.readyReadStandardError.connect(lambda: drain("stderr"))
        wait(lambda: result, 15)
        assert result == [(0, QProcess.ExitStatus.NormalExit)]
        assert counts == {"stdout": 2 * 1048576, "stderr": 2 * 1048576}
        assert hashes["stdout"].digest() == hashlib.sha256(b"x" * (2 * 1048576)).digest()
        assert hashes["stderr"].digest() == hashlib.sha256(b"y" * (2 * 1048576)).digest()
    finally:
        if process.state() != QProcess.ProcessState.NotRunning:
            process.kill()
            wait(lambda: process.state() == QProcess.ProcessState.NotRunning)
        process.deleteLater()
