"""Local-only synthetic protocol producer; performs no SSH or shell execution."""
import argparse
import base64
import json
import os
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=("resolve", "uname", "run"))
parser.add_argument("--rate", type=int, default=5242880)
parser.add_argument("--shape", choices=("long", "short", "utf8"), default="long")
args = parser.parse_args()
if args.mode == "resolve":
    print("hostname 192.0.2.1\nuser synthetic\nport 22")
    raise SystemExit(0)
if args.mode == "uname":
    print("Linux")
    raise SystemExit(0)
print(json.dumps({"v": 2, "type": "ready", "session": os.environ["NODEBATCH_PROTOCOL_SESSION"]}), flush=True)
request = json.loads(sys.stdin.readline())
print(json.dumps({"v": 2, "type": "started", "token": request["token"]}), flush=True)
size = min(16384, max(256, args.rate // 5))
unit = {"long": b"x", "short": b"line\n", "utf8": "\u03bb\u4e16\u754c \U0001f30d\n".encode()}[args.shape]
data = (unit * (size // len(unit) + 1))[:size]
# Split UTF-8 is intentional; the engine must decode incrementally.
encoded = base64.b64encode(data).decode()
start, emitted = time.monotonic(), 0
try:
    while True:
        channel = "stdout" if emitted % 2 == 0 else "stderr"
        if args.shape == "utf8":
            offset = ((emitted // 2) * size) % len(unit)
            data = (unit * (size // len(unit) + 2))[offset:offset + size]
            encoded = base64.b64encode(data).decode()
        print(json.dumps(dict(v=2, type="data", token=request["token"], channel=channel, data=encoded)), flush=True)
        emitted += 1
        due = start + emitted * size / args.rate
        time.sleep(max(0, due - time.monotonic()))
except (BrokenPipeError, KeyboardInterrupt):
    pass
