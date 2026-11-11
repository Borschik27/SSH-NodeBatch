"""Disposable SSH/SFTP server. Binds ONLY to loopback; never runs shell commands."""
import os
import shlex
import socket
import threading
import time
import json
import base64
from pathlib import Path
import paramiko


class Files(paramiko.SFTPServerInterface):
    def __init__(self, server, *, root):
        self.root = Path(root).resolve()
        super().__init__(server)

    def path(self, path):
        result = (self.root / path.lstrip("/")).resolve()
        if not result.is_relative_to(self.root):
            raise PermissionError("outside disposable root")
        return result

    def canonicalize(self, path):
        return "/" + str(self.path(path).relative_to(self.root)).replace("\\", "/").lstrip(".")

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.path(path).stat())
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    lstat = stat

    def open(self, path, flags, attr):
        try:
            fd = os.open(self.path(path), flags, 0o600)
            mode = "r+b" if flags & os.O_RDWR else "wb" if flags & os.O_WRONLY else "rb"
            stream = os.fdopen(fd, mode)
            handle = paramiko.SFTPHandle(flags)
            handle.readfile = stream
            handle.writefile = stream
            return handle
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)


class Login(paramiko.ServerInterface):
    def __init__(self, fixture):
        self.fixture = fixture

    def get_allowed_auths(self, username):
        return "publickey" if self.fixture.authorized else "password"

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_SUCCESSFUL if username == "tester" and key.asbytes() in self.fixture.authorized else paramiko.AUTH_FAILED

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if username == "tester" and password == "topsecret" else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        threading.Thread(target=self.execute, args=(channel, command.decode()), daemon=True).start()
        return True

    def execute(self, channel, command):
        try:
            time.sleep(.03)  # Let the transport acknowledge the exec request.
            words = shlex.split(command)
            wire = False
            token = None
            if words[0] == "sudo":
                marker = words[words.index("-p") + 1]
                channel.send_stderr(marker.encode())
                received = b""
                channel.settimeout(20)
                while not received.endswith(b"\n"):
                    received += channel.recv(1024)
                if received.strip() != b"topsecret":
                    channel.send_exit_status(1)
                    return
                words = words[words.index("--") + 1:]
            if words[:3] == ["python3", "-u", "-c"]:
                from importlib.resources import files
                expected = files("nodebatch").joinpath("remote_agent.txt").read_text(encoding="utf-8")
                if len(words) != 5 or words[3] != expected:
                    raise ValueError("unsupported remote helper")
                channel.sendall((json.dumps({"v": 2, "type": "ready", "session": words[4]}) + "\n").encode())
                received = b""
                channel.settimeout(20)
                while not received.endswith(b"\n") and len(received) < 1048576:
                    data = channel.recv(16384)
                    if not data:
                        raise ValueError("missing request")
                    received += data
                request = json.loads(received)
                token, script, wire = request["token"], request["command"], True
                channel.sendall((json.dumps({"v": 2, "type": "started", "token": token}) + "\n").encode())
                self.fixture.requests.append(request)
            elif len(words) == 3 and words[:2] in (["sh", "-c"], ["bash", "-c"]):
                script = words[2]
            else:
                raise ValueError("unsupported test command")
            code, output = 0, b""
            if script == "uname -s":
                output = b"Linux\n"
            elif script == "printf test-output":
                output = b"test-output"
            elif script == "emit-secret":
                output = b"topsecret"
            elif script.startswith("if [ -e "):
                target = shlex.split(script)[3]
                code = 73 if self.fixture.path(target).exists() else 0
            elif script.startswith("ln "):
                args = shlex.split(script)
                os.link(self.fixture.path(args[1]), self.fixture.path(args[2]))
                self.fixture.path(args[1]).unlink()
            elif script.startswith("mv -f -- "):
                args = shlex.split(script)
                os.replace(self.fixture.path(args[-2]), self.fixture.path(args[-1]))
            else:
                raise ValueError("unsupported test script")
            if output:
                if wire:
                    channel.sendall((json.dumps({"v": 2, "type": "data", "token": token,
                        "channel": "stdout", "data": base64.b64encode(output).decode()}) + "\n").encode())
                else:
                    channel.sendall(output)
            if wire:
                channel.sendall((json.dumps({"v": 2, "type": "result", "token": token, "code": code}) + "\n").encode())
            channel.send_exit_status(0 if wire else code)
            channel.shutdown_write()
        except Exception as exc:
            try:
                channel.send_stderr(str(exc).encode())
                channel.send_exit_status(1)
            except Exception:
                pass
        finally:
            time.sleep(.03)
            channel.close()


class LoopbackServer:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.key = paramiko.RSAKey.generate(2048)
        self.authorized = set()
        self.requests = []  # synthetic requests only, used for protocol assertions
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.socket.listen(8)
        self.socket.settimeout(.1)
        self.stopped = threading.Event()
        self.transports = []
        self.workers = []
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def path(self, value):
        path = (self.root / value.lstrip("/")).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise PermissionError("outside disposable root")
        return path

    def accept(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            worker = threading.Thread(target=self.serve, args=(connection,), daemon=True)
            self.workers.append(worker)
            worker.start()

    def serve(self, connection):
        transport = paramiko.Transport(connection)
        self.transports.append(transport)
        try:
            transport.add_server_key(self.key)
            transport.set_subsystem_handler("sftp", paramiko.SFTPServer, Files, root=self.root)
            transport.start_server(server=Login(self))
            while transport.is_active() and not self.stopped.wait(.05):
                pass
        except (EOFError, OSError, paramiko.SSHException):
            pass
        finally:
            transport.close()

    def close(self):
        self.stopped.set()
        self.socket.close()
        for transport in self.transports:
            transport.close()
        self.thread.join(1)
        for transport in self.transports:
            if transport.ident:
                transport.join(1)
        for worker in self.workers:
            worker.join(1)
