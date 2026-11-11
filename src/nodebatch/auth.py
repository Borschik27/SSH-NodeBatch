from __future__ import annotations
import json
import secrets
import uuid
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer


class AuthBroker(QObject):
    requested = Signal(object)
    secret_entered = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.name = "nodebatch-" + uuid.uuid4().hex
        if not self.server.listen(self.name):
            raise RuntimeError(self.server.errorString())
        self.contexts = {}
        self.pending = {}
        self.buffers = {}
        self.server.newConnection.connect(self._connect)

    def register(self, job_id, host, timeout):
        token = secrets.token_urlsafe(32)
        self.contexts[token] = (job_id, host, timeout)
        return {"NODEBATCH_AUTH_SERVER": self.name, "NODEBATCH_AUTH_TOKEN": token}

    def revoke(self, token):
        if token is None:
            return
        self.contexts.pop(token, None)
        for request_id, request in list(self.pending.items()):
            if request.get("token") == token:
                self.answer(request_id, None)

    def _connect(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self.buffers[socket] = bytearray()
            socket.readyRead.connect(lambda s=socket: self._read(s))
            socket.disconnected.connect(lambda s=socket: self._disconnected(s))

    def _disconnected(self, socket):
        self.buffers.pop(socket, None)
        for request_id, request in list(self.pending.items()):
            if request.get("socket") is socket:
                self.pending.pop(request_id, None)
        socket.deleteLater()

    def _read(self, socket):
        buffer = self.buffers.get(socket)
        if buffer is None:
            return
        buffer.extend(bytes(socket.readAll()))
        if len(buffer) > 32768:
            socket.disconnectFromServer()
            return
        if b"\n" not in buffer:
            return
        try:
            data = json.loads(bytes(buffer).split(b"\n", 1)[0])
            token = data.get("token", "")
            job_id, host, timeout = self.contexts[token]
            prompt = data["prompt"]
            if not isinstance(prompt, str) or len(prompt) > 16000:
                raise ValueError()
            kind = "confirm" if data.get("kind") == "confirm" else "secret"
            buffer.clear()
            request_id = uuid.uuid4().hex
            request = {"id": request_id, "job": job_id, "host": host, "prompt": prompt,
                       "kind": kind, "socket": socket, "token": token}
            self.pending[request_id] = request
            QTimer.singleShot(timeout * 1000, lambda r=request_id: self.answer(r, None))
            self.requested.emit(request)
        except (ValueError, KeyError, TypeError):
            socket.disconnectFromServer()

    def ask_local(self, job_id, host, prompt, timeout, callback, token=None):
        request_id = uuid.uuid4().hex
        request = {"id": request_id, "job": job_id, "host": host, "prompt": prompt,
                   "kind": "secret", "callback": callback, "token": token}
        self.pending[request_id] = request
        QTimer.singleShot(timeout * 1000, lambda: self.answer(request_id, None))
        self.requested.emit(request)
        return request_id

    def answer(self, request_id, answer):
        request = self.pending.pop(request_id, None)
        if request is None:
            return
        if answer is not None and request["kind"] == "secret":
            self.secret_entered.emit(request["job"], answer)
        if "socket" in request:
            socket = request["socket"]
            socket.write((json.dumps({"answer": answer}, ensure_ascii=False) + "\n").encode("utf-8"))
            socket.flush()
            socket.disconnectFromServer()
        else:
            request["callback"](answer)

    def cancel_job(self, job_id):
        for token, context in list(self.contexts.items()):
            if context[0] == job_id:
                self.revoke(token)
        for request_id, request in list(self.pending.items()):
            if request["job"] == job_id:
                self.answer(request_id, None)

    def close(self):
        for request_id in list(self.pending):
            self.answer(request_id, None)
        self.server.close()
