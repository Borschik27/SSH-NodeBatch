"""Bounded native pipes: slow consumers backpressure producers, not Qt queues."""
from .i18n import tr
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import os
import queue
import subprocess
import threading

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

_SPAWNERS = ThreadPoolExecutor(max_workers=8, thread_name_prefix="nodebatch-spawn")


class _Buffer:
    def __init__(self):
        self.condition = threading.Condition()
        self.chunks = deque()
        self.bytes = 0
        self.stopped = self.eof = False
        self.error = False

    def put(self, data):
        with self.condition:
            self.condition.wait_for(lambda: self.stopped or self.bytes + len(data) <= 262144)
            if self.stopped:
                return False
            self.chunks.append(data)
            self.bytes += len(data)
            return True

    def take(self, count):
        chunks = []
        with self.condition:
            while self.chunks and count > 0:
                data = self.chunks.popleft()
                part, tail = data[:count], data[count:]
                chunks.append(part)
                count -= len(part)
                self.bytes -= len(part)
                if tail:
                    self.chunks.appendleft(tail)
            self.condition.notify_all()
        return b"".join(chunks)

    def size(self):
        with self.condition:
            return self.bytes

    def finish(self):
        with self.condition:
            self.eof = True

    def stop(self):
        with self.condition:
            self.stopped = True
            self.chunks.clear()
            self.bytes = 0
            self.condition.notify_all()


class StreamingProcess(QObject):
    started = Signal()
    readyReadStandardOutput = Signal()
    readyReadStandardError = Signal()
    finished = Signal(int, object)
    errorOccurred = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._environment = None
        self._argv = []
        self._process = None
        self._channel = QProcess.ProcessChannel.StandardOutput
        self._stdout, self._stderr = _Buffer(), _Buffer()
        self._input = queue.Queue(maxsize=8)
        self._close_input = self._killed = self._complete = False
        self._error = ""
        self._read_error_reported = False
        self._starting = self._spawn_done = self._started_emitted = False
        self._spawn_future = None
        self._timer = QTimer(self)
        self._timer.setInterval(5)
        self._timer.timeout.connect(self._pump)

    def setProcessEnvironment(self, environment):
        self._environment = {}
        for entry in environment.toStringList():
            separator = entry.find("=", 1)  # Preserve Windows '=C:' drive entries.
            self._environment[entry[:separator]] = entry[separator + 1:]

    def setProcessChannelMode(self, mode):
        if mode != QProcess.ProcessChannelMode.SeparateChannels:
            raise ValueError("StreamingProcess requires separate channels")

    def start(self, program, arguments):
        self._argv = [program, *arguments]
        self._starting = True
        self._timer.start()
        # CreateProcess may be slow (e.g. antivirus/UNC paths). Never block Qt.
        try:
            self._spawn_future = _SPAWNERS.submit(self._spawn)
        except RuntimeError as exc:
            self._error = str(exc)
            self._starting = False
            self._spawn_done = True

    def _spawn(self):
        try:
            if self._killed:
                return
            self._process = subprocess.Popen(self._argv, env=self._environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if self._killed:
                self._process.kill()
                self._process.stdin.close()
                self._process.stdout.close()
                self._process.stderr.close()
                return
            for pipe, buffer in ((self._process.stdout, self._stdout), (self._process.stderr, self._stderr)):
                threading.Thread(target=self._read_pipe, args=(pipe, buffer), daemon=True).start()
            threading.Thread(target=self._write_pipe, daemon=True).start()
        except (OSError, ValueError, RuntimeError) as exc:
            self._error = str(exc)
            if self._process is not None:
                self._stdout.stop()
                self._stderr.stop()
                self._process.kill()
                self._process.wait()
                for pipe in (self._process.stdin, self._process.stdout, self._process.stderr):
                    pipe.close()
                self._process = None
        finally:
            self._starting = False
            self._spawn_done = True

    @staticmethod
    def _read_pipe(pipe, buffer):
        try:
            while not buffer.stopped:
                data = pipe.read(32768)
                if not data or not buffer.put(data):
                    break
        except (OSError, ValueError):
            buffer.error = not buffer.stopped
        finally:
            pipe.close()
            buffer.finish()

    def _write_pipe(self):
        try:
            while not self._killed:
                try:
                    data = self._input.get(timeout=.05)
                except queue.Empty:
                    if self._close_input:
                        break
                    continue
                view = memoryview(data)
                while view and not self._killed:
                    count = self._process.stdin.write(view)
                    if not count:
                        return
                    view = view[count:]
        except (OSError, ValueError):
            pass
        finally:
            self._process.stdin.close()
            while True:
                try:
                    self._input.get_nowait()
                except queue.Empty:
                    break

    def _pump(self):
        if not self._spawn_done:
            return
        if self._process is None:
            self._complete = True
            self._timer.stop()
            if self._killed:
                self.finished.emit(-1, QProcess.ExitStatus.CrashExit)
            else:
                self.errorOccurred.emit(QProcess.ProcessError.FailedToStart)
            return
        if not self._started_emitted:
            self._started_emitted = True
            if not self._killed:
                self.started.emit()
        if (self._stdout.error or self._stderr.error) and not self._read_error_reported:
            self._read_error_reported = True
            self._error = tr('Cannot read the process stream')
            self.errorOccurred.emit(QProcess.ProcessError.ReadError)
        if self._stdout.size():
            self.readyReadStandardOutput.emit()
        if self._stderr.size():
            self.readyReadStandardError.emit()
        code = self._process.poll()
        drained = (self._stdout.eof and self._stderr.eof and
                   not self._stdout.size() and not self._stderr.size())
        if code is not None and (self._killed or drained):
            self._complete = True
            self._timer.stop()
            crashed = self._killed or code < 0 or (os.name == "nt" and code >= 2**31)
            status = QProcess.ExitStatus.CrashExit if crashed else QProcess.ExitStatus.NormalExit
            self.finished.emit(code if code < 2**31 else code - 2**32, status)

    def state(self):
        return (QProcess.ProcessState.Starting if self._starting else
                QProcess.ProcessState.NotRunning if self._complete or self._process is None
                else QProcess.ProcessState.Running)

    def errorString(self):
        return self._error

    def arguments(self):
        return self._argv[1:]

    def readChannel(self):
        return self._channel

    def setReadChannel(self, channel):
        self._channel = channel

    def _buffer(self):
        return self._stderr if self._channel == QProcess.ProcessChannel.StandardError else self._stdout

    def bytesAvailable(self):
        return self._buffer().size()

    def read(self, count):
        return self._buffer().take(count)

    def readAllStandardOutput(self):
        return self._stdout.take(65536)

    def readAllStandardError(self):
        return self._stderr.take(65536)

    def write(self, data):
        if self._close_input or self._killed or self._complete or self._process is None:
            return -1
        try:
            self._input.put_nowait(bytes(data))
        except queue.Full:
            return -1
        return len(data)

    def closeWriteChannel(self):
        self._close_input = True

    def kill(self):
        self._killed = True
        self._stdout.stop()
        self._stderr.stop()
        if self._spawn_future is not None and self._spawn_future.cancel():
            self._starting = False
            self._spawn_done = True
        if self._process is not None:
            if self._process.poll() is None:
                self._process.kill()
            # bufsize=0: closing does not wait on a buffered-reader lock.
            self._process.stdout.close()
            self._process.stderr.close()
