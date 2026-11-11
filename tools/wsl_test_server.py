"""Local integration helper: Linux-side loopback server controlled by parent stdin."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from loopback_server import LoopbackServer

root = Path(sys.argv[1])
server = LoopbackServer(root / "remote")
print(json.dumps({"port": server.port, "home": os.environ["HOME"]}), flush=True)
try:
    sys.stdin.readline()
finally:
    server.close()
