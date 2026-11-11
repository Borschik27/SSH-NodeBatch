"""A password/EOF test double only; never grants privileges or calls sudo."""
import os
import subprocess
import sys
import time

marker = sys.argv[1]
if not os.environ.get("NODEBATCH_TEST_NOPASSWD"):
    for _ in range(3):
        sys.stderr.write(marker[:10])
        sys.stderr.flush()
        time.sleep(.01)
        sys.stderr.write(marker[10:])
        sys.stderr.flush()
        if sys.stdin.buffer.readline().strip() == b"synthetic-sudo-password":
            break
    else:
        raise SystemExit(1)
# No password bytes are forwarded to the child: it reads only after ready.
raise SystemExit(subprocess.call(sys.argv[2:], stdin=sys.stdin.buffer))
