"""Minimal OpenSSH askpass executable. IPC is user-restricted and token-scoped."""
import json
import os
import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalSocket


def main():
    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    server, token = os.environ.get("NODEBATCH_AUTH_SERVER"), os.environ.get("NODEBATCH_AUTH_TOKEN")
    if not server or not token:
        return 1
    socket = QLocalSocket()
    socket.connectToServer(server)
    if not socket.waitForConnected(5000):
        return 1
    prompt = sys.argv[1] if len(sys.argv) > 1 else "SSH authentication"
    # Host-key confirmation uses RP_ECHO in OpenSSH and may set the hint to
    # "none", not "confirm". Classify the explicit yes/no/fingerprint question.
    kind = os.environ.get("SSH_ASKPASS_PROMPT", "")
    if "(yes/no" in prompt.lower() and "fingerprint" in prompt.lower():
        kind = "confirm"
    socket.write((json.dumps({"token": token, "prompt": prompt, "kind": kind},
                             ensure_ascii=False) + "\n").encode("utf-8"))
    socket.flush()
    buffer = bytearray()
    # The GUI broker applies the configured authentication timeout.
    while b"\n" not in buffer:
        if not socket.waitForReadyRead(650000):
            return 1
        buffer.extend(bytes(socket.readAll()))
        if len(buffer) > 32768:
            return 1
    answer = json.loads(bytes(buffer).split(b"\n", 1)[0]).get("answer")
    if answer is None:
        return 1
    sys.stdout.buffer.write((answer + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
