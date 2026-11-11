"""No networking: deterministic child process used by engine tests."""
import os
import re
import sys
import time
import json
import base64
from pathlib import Path

mode, host = sys.argv[1:3]
if mode == "resolve":
    print("hostname 192.0.2.1\nuser test\nport 22")
    raise SystemExit(0)
if mode == "sftp":
    batch = sys.stdin.read()
    if "missing" in batch:
        raise SystemExit(1)
    # Test paths are supplied independently to avoid duplicating the SFTP parser.
    destination = os.environ.get("NODEBATCH_TEST_DOWNLOAD")
    if batch.startswith("get") and destination:
        Path(destination).write_text("downloaded", encoding="utf-8")
    raise SystemExit(0)
command = sys.argv[3]
wire = os.environ.get("NODEBATCH_PROTOCOL") == "1"
token = None
if wire:
    if command == "no-ready-test":
        print("python3 unavailable", file=sys.stderr, flush=True)
        raise SystemExit(127)
    if command == "sudo-test":
        marker = sys.argv[4]
        sys.stderr.write(marker[:10])
        sys.stderr.flush()
        time.sleep(.01)
        sys.stderr.write(marker[10:])
        sys.stderr.flush()
        if sys.stdin.readline().strip() != "topsecret":
            raise SystemExit(1)
    print(json.dumps({"v": 2, "type": "ready", "session": os.environ["NODEBATCH_PROTOCOL_SESSION"]}), flush=True)
    payload = json.loads(sys.stdin.readline())
    token = payload["token"]
    if command == "prepare-fail-test":
        print(json.dumps({"v": 2, "type": "not_started", "token": token,
                          "reason": "process_creation_failed"}), flush=True)
        raise SystemExit(125)
    print(json.dumps({"v": 2, "type": "started", "token": token}), flush=True)

def emit(text, channel="stdout"):
    if wire:
        print(json.dumps({"v": 2, "type": "data", "token": token,
                          "channel": channel, "data": base64.b64encode(text.encode()).decode()}), flush=True)
    else:
        print(text, file=sys.stderr if channel == "stderr" else sys.stdout, flush=True)

def finish(code):
    if wire:
        print(json.dumps({"v": 2, "type": "result", "token": token, "code": code}), flush=True)
        raise SystemExit(0)
    raise SystemExit(code)
if command == "uname -s":
    print(os.environ.get("NODEBATCH_TEST_OS", "Linux"))
    raise SystemExit(0)
if command == "sleep-test":
    emit("started")
    time.sleep(20)
elif command == "fail-test":
    emit("test failure", "stderr")
    finish(7)
elif command == "disconnect-test":
    raise SystemExit(255)
elif command == "exit255-test":
    finish(255)
elif command == "drop-after-test":
    print(json.dumps({"v": 2, "type": "result", "token": token, "code": 0}), flush=True)
    raise SystemExit(255)
elif command == "corrupt-test":
    print(json.dumps({"v": 2, "type": "result", "token": token, "code": 0}), flush=True)
    print("broken-protocol", flush=True)
    raise SystemExit(0)
elif command == "runtime-error-test":
    print(json.dumps({"v": 2, "type": "error", "token": token, "reason": "execution_failed"}), flush=True)
    raise SystemExit(125)
elif command == "burst-test":
    for i in range(200):
        emit("x" * 16384, "stdout" if i % 2 else "stderr")
elif command == "split-secret":
    for part in ("top", "sec", "ret"):
        emit(part)
        time.sleep(.01)
elif command == "sudo-test":
    if wire:
        emit("sudo success")
        finish(0)
    marker = sys.argv[4]
    sys.stderr.write(marker[:10])
    sys.stderr.flush()
    time.sleep(.01)
    sys.stderr.write(marker[10:])
    sys.stderr.flush()
    password = sys.stdin.readline().strip()
    if password != "topsecret":
        raise SystemExit(1)
    print("sudo success")
elif command.startswith("if [ -e"):
    if os.environ.get("NODEBATCH_TEST_EXISTS"):
        raise SystemExit(73)
elif command.startswith(("ln ", "mv -f")):
    print("committed")
else:
    emit("ok " + host)
finish(0)
