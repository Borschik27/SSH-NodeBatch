from __future__ import annotations
from .i18n import tr
import codecs
import json
import secrets
from dataclasses import replace
import os
import sqlite3
import posixpath
import shlex
import tempfile
import time
import uuid
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from . import transport
from .catalog import catalog, fingerprint
from .history import Redactor
from .models import Step
from .wire import Decoder, WireError
from .stream_process import StreamingProcess

OS_MAP = {"Linux": "linux", "FreeBSD": "freebsd", "OpenBSD": "openbsd",
          "NetBSD": "netbsd", "Darwin": "darwin"}


class JobRunner(QObject):
    event = Signal(str, str, str, str)  # host, step, channel, redacted text
    result = Signal(str, str, str, float, object, str)
    finished = Signal(str)
    paused = Signal(str)
    fatal = Signal(str)
    overwrite_needed = Signal(object, str)

    def __init__(self, job, history, auth, helper=None, parent=None):
        super().__init__(parent)
        self.job, self.history, self.auth = job, history, auth
        self.helper = helper or transport.askpass_executable()
        self.redactor = Redactor(job.secrets)
        self.pending = deque(job.hosts)
        self.active = {}
        self.terminating = set()
        self.waiting = []
        self.halted = self.cancelled = self.is_paused = self.complete = False
        self.had_failure = self.had_unknown = False
        self.storage_broken = False
        self.log_limit_reported = False
        self.baseline = job.ssh_fingerprint
        self.execution = job.config["settings"]["execution"]
        self.max_bytes = job.config["settings"]["logging"]["max_bytes"]
        self.limit = self.execution["parallelism"] if self.execution["mode"] == "parallel" else 1
        self.auth.secret_entered.connect(self._secret)
        self.guard = QTimer(self)
        self.guard.setInterval(job.config["settings"]["watch"]["poll_ms"])
        self.guard.timeout.connect(self.check_sources)

    def _secret(self, job_id, secret):
        if job_id == self.job.id:
            self.redactor.add(secret)

    def start(self):
        try:
            self.history.start(self.job, self.redactor)
        except (OSError, sqlite3.Error, ValueError):
            self.complete = True
            self.auth.secret_entered.disconnect(self._secret)
            self.redactor.clear()
            from dataclasses import replace
            self.job = replace(self.job, secrets=(), steps=())
            raise
        self.guard.start()
        QTimer.singleShot(0, self._schedule)

    def sources(self):
        _, watched, patterns, _ = catalog(self.job.profile, self.job.config["hosts"], self.job.config.get("groups"))
        return fingerprint(watched, patterns)

    def check_sources(self):
        if self.complete or self.cancelled or self.is_paused:
            return
        try:
            current = self.sources()
        except (ValueError, OSError) as exc:
            self._pause(tr('Cannot check SSH files: {p0}', p0=exc))
            return
        if current != self.baseline:
            self._pause(tr('SSH config, Include files, keys or known_hosts changed. New connections are paused. Review the changes.'))

    def _pause(self, reason):
        if not self.is_paused:
            self.is_paused = True
            self.paused.emit(reason)

    def gate(self, worker, callback):
        if self.cancelled:
            worker.end("cancelled", tr('Start cancelled'))
            return
        if self.halted or self.storage_broken:
            worker.end("skipped", tr('Stopped after an error or log failure'))
            return
        self.check_sources()
        if self.is_paused:
            self.waiting.append((worker, callback))
        else:
            try:
                callback()
            except (ValueError, OSError) as exc:
                worker.end("failed", str(exc))

    def resume(self):
        self.baseline = self.sources()
        self.is_paused = False
        waiting, self.waiting = self.waiting, []
        for worker, callback in waiting:
            if worker.host.alias in self.active:
                self.gate(worker, callback)
        self._schedule()

    def _schedule(self):
        if self.complete:
            return
        if not self.halted and not self.cancelled:
            self.check_sources()
            while self.pending and len(self.active) < self.limit and not self.is_paused:
                host = self.pending.popleft()
                worker = HostWorker(self, host)
                self.active[host.alias] = worker
                worker.start()
        if not self.active and not self.pending and not self.terminating:
            self._finish()

    def output(self, host, step, channel, text, final=False):
        key = (host, step, channel)
        safe = self.redactor.feed(key, text, final)
        if safe:
            if not self.storage_broken:
                try:
                    saved = self.history.event(self.job.id, host, step, channel, safe, self.max_bytes)
                    if not saved and not self.log_limit_reported:
                        self.log_limit_reported = True
                        self.event.emit(host, step, "system", self.history.truncation_reasons[self.job.id])
                except (OSError, sqlite3.Error) as exc:
                    self._persistence_error(exc)
            self.event.emit(host, step, channel, safe)

    def mark_output_incomplete(self, host, step):
        reason = tr('Output streams remained open after the shell exited; output may be incomplete.')
        self.output(host, step, "system", reason + "\n", True)
        if not self.storage_broken:
            try:
                self.history.mark_output_incomplete(self.job.id, reason)
            except (OSError, sqlite3.Error) as exc:
                self._persistence_error(exc)

    def record(self, host, step, status, duration, code, reason, **details):
        safe_reason = self.redactor.text(reason)
        if "transport_reason" in details:
            details["transport_reason"] = self.redactor.text(details["transport_reason"])
        if not self.storage_broken:
            try:
                self.history.result(self.job.id, host, step, status, duration, code, safe_reason, **details)
            except (OSError, sqlite3.Error) as exc:
                self._persistence_error(exc)
        self.result.emit(host, step, status, duration, code, safe_reason)

    def _persistence_error(self, exc):
        if self.storage_broken:
            return
        self.storage_broken = True
        self.halted = True
        self.had_failure = True
        self.fatal.emit(tr('Logging is unavailable; stopping the run. ') + str(exc))
        QTimer.singleShot(0, self.cancel)

    def worker_done(self, worker, status, reason):
        if worker.host.alias not in self.active:
            return
        self.active.pop(worker.host.alias)
        self.waiting = [(w, callback) for w, callback in self.waiting if w is not worker]
        if status in ("failed", "unknown") or (status == "cancelled" and not self.cancelled):
            self.had_failure = True
            self.had_unknown |= status == "unknown"
            if self.execution["stop_on_error"]:
                self.halted = True
                self._skip_pending(tr('Skipped after an error'))
                # Workers already executing finish their current step.
                for other, _ in list(self.waiting):
                    other.end("skipped", tr('Stopped after an error'))
        worker.deleteLater()
        QTimer.singleShot(0, self._schedule)

    def _skip_pending(self, reason):
        while self.pending:
            host = self.pending.popleft()
            self.record(host.alias, tr('Queue'), "skipped", 0, None, reason)

    def cancel(self):
        if self.complete:
            return
        self.cancelled = True
        self._skip_pending(tr('Cancelled by user'))
        self.auth.cancel_job(self.job.id)
        for worker in list(self.active.values()):
            worker.abort(tr('Cancelled by user'))
        self._schedule()

    def _finish(self):
        self.complete = True
        self.guard.stop()
        self.auth.cancel_job(self.job.id)
        self.auth.secret_entered.disconnect(self._secret)
        status = ("unknown" if self.had_unknown else "failed" if self.storage_broken else
                  "cancelled" if self.cancelled else "failed" if self.had_failure else "success")
        try:
            self.history.finish(self.job.id, status)
        except (OSError, sqlite3.Error) as exc:
            self._persistence_error(exc)
            status = "unknown" if self.had_unknown else "failed"
        # GUI schedules maintenance on a worker after this runner has completed.
        self.redactor.clear()
        from dataclasses import replace
        self.job = replace(self.job, secrets=(), steps=())
        self.finished.emit(status)


class HostWorker(QObject):
    def __init__(self, runner, host):
        super().__init__(runner)
        self.runner, self.host, self.job = runner, host, runner.job
        self.execution = runner.execution
        self.process = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self.abort(tr('Operation timed out'), timeout=True))
        self.step_index = 0
        self.current_step = tr('SSH check')
        self.phase = ""
        self.marker = ""
        self.started = time.monotonic()
        self.step_started = self.started
        self.done = False
        self.token = None
        self.stdout_capture = ""
        self.stderr_capture = ""
        self.stderr_tail = ""
        self.dec_stdout = self.dec_stderr = None
        self.dec_diagnostic = None
        self.on_exit = None
        self.temporary_local = None
        self.temporary_remote = None
        self.remote_exists = False
        self.upload_overwrite = False
        self.uncertain = False
        self.wire = None
        self.payload = None
        self.execution_timeout = None
        self.auth_state = "idle"
        self.protocol_error = False
        self.request_sent = False

    def start(self):
        self.runner.gate(self, self._resolve)

    def _resolve(self):
        self.current_step = tr('SSH configuration')
        binary, args = transport.command(self.job.profile, self.execution, self.host, resolve=True)
        self._launch(binary, args, "resolve", self._resolved, 15)

    def _resolved(self, code):
        if code:
            self.end("failed", tr('Cannot resolve SSH configuration: ') + self.stderr_capture.strip()[-2000:], code=code)
            return
        values = {}
        for line in self.stdout_capture.splitlines():
            key, _, value = line.partition(" ")
            if key in ("hostname", "user", "port", "proxyjump"):
                values[key] = value
        self.runner.output(self.host.alias, self.current_step, "system",
                           "OpenSSH: " + ", ".join(f"{k}={v}" for k, v in values.items()) + "\n", True)
        self.runner.gate(self, self._uname)

    def _uname(self):
        self.current_step = tr('OS check')
        self._ssh(Step(self.current_step, "uname -s"), "uname", self._os_checked,
                  self.execution["connect_timeout"] + self.execution["auth_timeout"])

    def _os_checked(self, code):
        if code:
            self.end("failed", tr('Cannot determine the remote OS: ') + self.stderr_capture.strip()[-2000:], code=code)
            return
        actual = OS_MAP.get(self.stdout_capture.strip(), "unix")
        if self.host.os != "unix" and self.host.os != actual:
            self.end("failed", tr('Expected OS {p0}, detected {p1}', p0=self.host.os, p1=actual))
            return
        requirements = self.job.config.get("requirements", {})
        allowed = requirements.get("os", ["unix"])
        if "unix" not in allowed and actual not in allowed:
            self.end("failed", tr('Scenario does not support OS {p0}', p0=actual))
            return
        if self.host.shell not in requirements.get("shells", ["sh", "bash"]):
            self.end("failed", tr('Scenario does not support shell {p0}', p0=self.host.shell))
            return
        if self.job.transfer:
            self.runner.gate(self, self._prepare_transfer)
        else:
            self._next_step()

    def _next_step(self):
        if self.step_index >= len(self.job.steps):
            self.end("success", tr('Completed'), record=False)
            return
        self.runner.gate(self, self._run_step)

    def _run_step(self):
        step = self.job.steps[self.step_index]
        self.current_step = step.name
        self.step_started = time.monotonic()
        self.runner.output(self.host.alias, step.name, "system",
                           (f"[sudo → {step.sudo_user}] " if step.sudo else "") + step.command + "\n", True)
        self._ssh(step, "command", self._step_completed, step.timeout or self.execution["step_timeout"])

    def _step_completed(self, code):
        duration = time.monotonic() - self.step_started
        confirmed = self.wire.result if self.wire and not self.protocol_error else None
        not_started = self.wire.not_started if self.wire and not self.protocol_error else None
        not_sent = self.wire is not None and not self.request_sent
        transport_reason = (tr('Invalid execution protocol') if self.protocol_error else
                            tr('Completion was not confirmed') if confirmed is None else
                            tr('SSH exited with code {p0} after confirmation', p0=code) if code else "")
        if confirmed is None and self.stderr_capture.strip():
            transport_reason += ": " + self.stderr_capture.strip()[-2000:]
        status = "unknown" if confirmed is None else "failed" if confirmed else "success"
        reason = transport_reason if confirmed is None else tr('Command exit code {p0}', p0=confirmed) if confirmed else ""
        if not_started or not_sent:
            status = "failed"
            descriptions = {"shell_unavailable": tr('The shell is not available on the host'),
                            "command_too_large": tr('Command exceeds 32 KiB UTF-8'),
                            "process_creation_failed": tr('Cannot create the shell process'),
                            "preparation_failed": tr('Input preparation failed')}
            reason = tr('Command was not started: ') + (descriptions[not_started] if not_started else
                tr('request was not sent to the helper; check Python 3 and authentication'))
            if not_started:
                transport_reason = ""
        elif confirmed is None:
            reason += tr('; outcome is unknown; no automatic retry')
        self.runner.record(self.host.alias, self.current_step, status, duration, confirmed, reason,
                           ssh_code=code, command_code=confirmed, transport_reason=transport_reason,
                           command_started=False if not_started or not_sent else True if self.wire and self.wire.started else None)
        if status != "success":
            self.end(status, reason, record=False)
            return
        self.step_index += 1
        self._next_step()

    def _ssh(self, step, phase, callback, timeout):
        self.wire = None
        self.payload = None
        self.protocol_error = False
        self.request_sent = False
        if phase == "command":
            step = replace(step, protocol=True, wire_id=uuid.uuid4().hex)
            token = secrets.token_hex(32)
            self.wire = Decoder(token, step.wire_id)
            self.payload = json.dumps({"token": token, "shell": self.host.shell,
                                       "command": step.command, "secrets": dict(step.secret_inputs)},
                                      ensure_ascii=True).encode("ascii") + b"\n"
            if len(self.payload) > 1048576:
                raise ValueError(tr('Command and input exceed 1 MiB'))
            self.execution_timeout = timeout
            timeout = self.execution["connect_timeout"] + self.execution["auth_timeout"]
            self.auth_state = "authorizing"
        self.marker = "__NODEBATCH_SUDO_" + uuid.uuid4().hex + "__" if step.sudo else ""
        binary, args = transport.command(self.job.profile, self.execution, self.host, step, self.marker)
        self._launch(binary, args, phase, callback, timeout)

    def _launch(self, binary, args, phase, callback, timeout, stdin=None):
        if self.done:
            return
        self.phase, self.on_exit = phase, callback
        self.stdout_capture = self.stderr_capture = self.stderr_tail = ""
        self.dec_stdout = codecs.getincrementaldecoder("utf-8")("replace")
        self.dec_stderr = codecs.getincrementaldecoder("utf-8")("replace")
        self.dec_diagnostic = codecs.getincrementaldecoder("utf-8")("replace")
        self.uncertain = phase in ("command", "sftp", "commit")
        self.process = StreamingProcess(self)
        process = self.process
        auth_env = self.runner.auth.register(self.job.id, self.host.alias, self.execution["auth_timeout"])
        self.token = auth_env["NODEBATCH_AUTH_TOKEN"]
        try:
            program, argv, env = transport.process_spec(self.job.profile, binary, args, auth_env, self.runner.helper)
        except (ValueError, OSError) as exc:
            self.end("failed", str(exc))
            return
        process.setProcessEnvironment(env)
        if self.wire:
            env.insert("NODEBATCH_PROTOCOL", "1")
            env.insert("NODEBATCH_PROTOCOL_SESSION", self.wire.session)
            process.setProcessEnvironment(env)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.readyReadStandardOutput.connect(self._stdout)
        process.readyReadStandardError.connect(self._stderr)
        process.finished.connect(self._exited)
        process.errorOccurred.connect(self._error)
        if stdin is not None:
            process.started.connect(lambda p=process, content=stdin: self._write_batch(p, content))
        if stdin is None and not self.marker and self.wire is None:
            process.started.connect(process.closeWriteChannel)
        process.start(program, argv)
        self.timer.start(timeout * 1000)

    def _write_batch(self, process, content):
        if process is self.process and not self.done:
            process.write(content.encode("utf-8"))
            process.closeWriteChannel()

    def _stdout(self):
        if self.process:
            if self.wire:
                self._wire_read()
                return
            text = self.dec_stdout.decode(bytes(self.process.readAllStandardOutput()))
            # Captures are needed only for small machine-readable preflight responses.
            if self.phase in ("resolve", "uname"):
                self.stdout_capture = (self.stdout_capture + text)[-65536:]
            if self.phase != "resolve":
                self.runner.output(self.host.alias, self.current_step, "stdout", text)

    def _wire_read(self, final=False):
        process = self.process
        if process is None:
            return
        try:
            remaining = 65536
            while self.process is process and not self.done and process.bytesAvailable() and remaining:
                data = bytes(process.read(min(32768, remaining)))
                remaining -= len(data)
                events = self.wire.feed(data)
                self._wire_events(events)
            if final and self.process is process and not self.done:
                self._wire_events(self.wire.feed(b"", final=True))
        except WireError:
            self.protocol_error = True
            self.payload = None
            if self.process is process:
                process.closeWriteChannel()
                process.kill()

    def _wire_events(self, events):
        for channel, data in events:
            if self.done or not self.process:
                return
            if channel == "ready":
                if self.auth_state != "authorizing":
                    raise WireError(tr('Repeated authentication'))
                self.auth_state = "executing"
                self.runner.auth.revoke(self.token)
                self.token = None
                self.step_started = time.monotonic()
                self.timer.start(self.execution_timeout * 1000)
                if self.process.write(self.payload) < 0:
                    self.end("failed", tr('Cannot send input to the helper'))
                    return
                self.request_sent = True
                self.payload = None
                self.process.closeWriteChannel()
            elif channel == "output_incomplete":
                self.runner.mark_output_incomplete(self.host.alias, self.current_step)
            elif channel in ("error", "not_started", "started"):
                pass  # Decoder owns the validated execution state.
            else:
                decoder = self.dec_stdout if channel == "stdout" else self.dec_stderr
                self.runner.output(self.host.alias, self.current_step, channel, decoder.decode(data))

    def _stderr(self, final=False):
        if not self.process:
            return
        # Wire stderr is SSH/sudo diagnostics; program stderr arrives in data frames.
        decoder = self.dec_diagnostic if self.wire else self.dec_stderr
        text = decoder.decode(bytes(self.process.readAllStandardError()), final=final)
        self.stderr_capture = (self.stderr_capture + text)[-8192:]
        if self.marker:
            self.stderr_tail += text
            while self.marker in self.stderr_tail:
                prefix, self.stderr_tail = self.stderr_tail.split(self.marker, 1)
                self.runner.output(self.host.alias, self.current_step, "stderr", prefix)
                if self.wire and self.auth_state != "authorizing":
                    continue
                process = self.process
                self.runner.auth.ask_local(self.job.id, self.host.alias, tr('sudo password (run as {p0}; connected as the SSH user)', p0=self.job.steps[self.step_index].sudo_user),
                                            self.execution["auth_timeout"],
                                            lambda password, p=process: self._sudo_password(password, p), self.token)
            keep = 0 if final else len(self.marker) - 1
            release = len(self.stderr_tail) - keep
            if release > 0:
                self.runner.output(self.host.alias, self.current_step, "stderr", self.stderr_tail[:release])
                self.stderr_tail = self.stderr_tail[release:]
        else:
            self.runner.output(self.host.alias, self.current_step, "stderr", text)

    def _sudo_password(self, password, process=None):
        if (self.done or not self.process or (process is not None and process is not self.process) or
                (self.wire and self.auth_state != "authorizing") or
                self.process.state() == QProcess.ProcessState.NotRunning):
            return
        if password is None:
            self.abort(tr('sudo password entry cancelled'))
        elif any(c in password for c in "\n\r\x00"):
            self.abort(tr('sudo password contains unsupported control characters'))
        else:
            self.process.write((password + "\n").encode("utf-8"))

    def _error(self, error):
        if error == QProcess.ProcessError.FailedToStart and not self.done:
            self.end("failed", self.process.errorString())
        elif error == QProcess.ProcessError.ReadError and not self.done:
            self.protocol_error = True
            self.abort(self.process.errorString())

    def _exited(self, code, exit_status):
        if self.done:
            return
        self.timer.stop()
        self._stdout()
        if self.done:
            return
        if self.wire:
            self._wire_read(final=True)
        self._stderr(final=True)
        if self.done:
            return
        remaining = self.dec_stdout.decode(b"", final=True)
        if remaining and self.phase != "resolve":
            self.runner.output(self.host.alias, self.current_step, "stdout", remaining)
        if self.wire:
            self.runner.output(self.host.alias, self.current_step, "stderr", self.dec_stderr.decode(b"", final=True))
        for channel in ("stdout", "stderr"):
            self.runner.output(self.host.alias, self.current_step, channel, "", True)
        self.runner.auth.revoke(self.token)
        self.token = None
        old, self.process = self.process, None
        old.deleteLater()
        self.marker = ""
        if exit_status == QProcess.ExitStatus.CrashExit and self.wire is None:
            self.end("unknown" if self.uncertain else "failed", tr('Connection process crashed'))
            return
        callback, self.on_exit = self.on_exit, None
        if callback:
            try:
                callback(code)
            except (ValueError, OSError) as exc:
                self.end("failed", str(exc))

    def _prepare_transfer(self):
        transfer = self.job.transfer
        self.current_step = tr('File transfer')
        self.step_started = time.monotonic()
        if transfer.direction == "upload":
            local = Path(transfer.local)
            if not local.is_file():
                self.end("failed", tr('Local file not found'))
                return
            parent = posixpath.dirname(transfer.remote) or "."
            self.temporary_remote = posixpath.join(parent, ".nodebatch-" + uuid.uuid4().hex + ".part")
            # A unique destination is staged; the final commit is atomic and refuses clobbering.
            script = f"if [ -e {shlex.quote(transfer.remote)} ] || [ -L {shlex.quote(transfer.remote)} ]; then exit 73; fi"
            self._ssh(Step(tr('Destination check'), script), "probe", self._remote_probe,
                      self.execution["step_timeout"])
        else:
            from .models import download_destination
            self.local_destination = download_destination(transfer.local, self.host.alias, transfer.remote)
            directory = self.local_destination.parent
            directory.mkdir(parents=True, exist_ok=True)
            name = posixpath.basename(transfer.remote)
            if name in ("", ".", ".."):
                self.end("failed", tr('Enter the full file path'))
                return
            self.local_destination = directory / name
            if self.local_destination.exists() or self.local_destination.is_symlink():
                self.runner.overwrite_needed.emit(self, str(self.local_destination))
            else:
                self._download_staged()

    def _remote_probe(self, code):
        if code == 73:
            self.remote_exists = True
            self.runner.overwrite_needed.emit(self, self.job.transfer.remote)
        elif code:
            self.end("failed", tr('Cannot check destination: code {p0}', p0=code))
        else:
            self.runner.gate(self, self._upload_staged)

    def confirm_overwrite(self, accepted):
        if self.done:
            return
        if not accepted:
            self.end("failed", tr('File overwrite declined'))
        elif self.job.transfer.direction == "upload":
            self.upload_overwrite = True
            self.runner.gate(self, self._upload_staged)
        else:
            self.download_overwrite = True
            self.runner.gate(self, self._download_staged)

    def _local_path(self, value):
        return transport.windows_to_wsl(value) if self.job.profile["backend"] == "wsl" else value.replace("\\", "/")

    def _upload_staged(self):
        transfer = self.job.transfer
        batch = "put " + transport.sftp_quote(self._local_path(str(Path(transfer.local).resolve()))) + " " + \
                transport.sftp_quote(self.temporary_remote) + "\n"
        self._sftp(batch, self._uploaded)

    def _download_staged(self):
        fd, name = tempfile.mkstemp(prefix=".nodebatch-", suffix=".part", dir=self.local_destination.parent)
        os.close(fd)
        self.temporary_local = Path(name)
        batch = "get " + transport.sftp_quote(self.job.transfer.remote) + " " + \
                transport.sftp_quote(self._local_path(str(self.temporary_local.resolve()))) + "\n"
        self._sftp(batch, self._downloaded)

    def _sftp(self, batch, callback):
        self.marker = ""
        binary, args = transport.command(self.job.profile, self.execution, self.host, transfer=True)
        self._launch(binary, args, "sftp", callback, self.execution["step_timeout"], batch)

    def _uploaded(self, code):
        if code:
            self.end("unknown" if code == 255 else "failed",
                     tr('SFTP exited with code {p0}; a temporary file may remain on the server', p0=code))
            return
        self.runner.gate(self, self._commit_upload)

    def _commit_upload(self):
        temp, destination = shlex.quote(self.temporary_remote), shlex.quote(self.job.transfer.remote)
        # Both paths are on the same filesystem. ln atomically refuses an existing name.
        script = f"mv -f -- {temp} {destination}" if self.upload_overwrite else \
                 f"ln {temp} {destination} && rm -f {temp}"
        self._ssh(Step(tr('Commit file'), script), "commit", self._upload_committed,
                  self.execution["step_timeout"])

    def _upload_committed(self, code):
        if code:
            self.end("unknown" if code == 255 else "failed",
                     tr('File was not committed: code {p0}. Temporary file: {p1}', p0=code, p1=self.temporary_remote))
        else:
            self.temporary_remote = None
            self.end("success", tr('File uploaded'))

    def _downloaded(self, code):
        if code:
            self.end("failed", tr('Cannot download file: code {p0}', p0=code))
            return
        try:
            if getattr(self, "download_overwrite", False):
                os.replace(self.temporary_local, self.local_destination)
            else:
                # Same-directory hard link prevents a check/use overwrite race.
                os.link(self.temporary_local, self.local_destination)
                self.temporary_local.unlink()
            self.temporary_local = None
        except OSError as exc:
            self.end("failed", tr('Cannot save file without overwriting: {p0}', p0=exc))
            return
        self.end("success", tr('Saved: {p0}', p0=self.local_destination))

    def abort(self, reason, timeout=False):
        if self.done:
            return
        command_not_sent = self.wire is not None and not self.request_sent
        active_remote = self.process is not None and self.uncertain and not command_not_sent
        self.end("unknown" if active_remote else "failed" if timeout else "cancelled",
                 reason + (tr('; the remote operation may have continued') if active_remote else ""))

    def end(self, status, reason, record=True, code=None):
        if self.done:
            return
        self.done = True
        self.timer.stop()
        if self.process:
            process, self.process = self.process, None
            process.finished.disconnect(self._exited)
            process.readyReadStandardOutput.disconnect(self._stdout)
            process.readyReadStandardError.disconnect(self._stderr)
            process.errorOccurred.disconnect(self._error)
            process.setParent(self.runner)
            if process.state() == QProcess.ProcessState.NotRunning:
                process.deleteLater()
            else:
                runner = self.runner
                runner.terminating.add(process)
                def reaped(*_):
                    runner.terminating.discard(process)
                    process.deleteLater()
                    runner._schedule()
                process.finished.connect(reaped)
                process.kill()
        if self.token:
            self.runner.auth.revoke(self.token)
            self.token = None
        if self.temporary_local:
            self.temporary_local.unlink(missing_ok=True)
        if self.temporary_remote:
            reason += tr('; a temporary file may remain: {p0}', p0=self.temporary_remote)
        for channel in ("stdout", "stderr"):
            self.runner.output(self.host.alias, self.current_step, channel, "", True)
        if record:
            started = (False if self.wire and (not self.request_sent or self.wire.not_started and not self.protocol_error)
                       else True if self.wire and self.wire.started and not self.protocol_error else None)
            details = ({"ssh_code": code, "command_code": None, "transport_reason": reason,
                        "command_started": started}
                       if self.wire else {})
            self.runner.record(self.host.alias, self.current_step, status,
                               time.monotonic() - self.step_started, 0 if status == "success" else code, reason, **details)
        self.runner.worker_done(self, status, reason)
        self.job = None
        self.payload = None
        self.stdout_capture = self.stderr_capture = self.stderr_tail = self.marker = ""
        self.on_exit = None
