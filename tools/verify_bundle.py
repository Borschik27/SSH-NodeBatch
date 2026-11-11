"""Smoke-test a frozen app AND its actual askpass executable, without SSH hosts."""
import argparse
import os
import tempfile
import time
import json
from pathlib import Path
from PySide6.QtCore import QCoreApplication, QProcess, QProcessEnvironment
from nodebatch.auth import AuthBroker
from release_files import DOCUMENTS

parser = argparse.ArgumentParser()
parser.add_argument("bundle", type=Path, nargs="?",
                    default=Path("dist") / ("windows" if os.name == "nt" else "linux") / "NodeBatch")
options = parser.parse_args()
bundle = options.bundle.resolve()
for document in DOCUMENTS:
    if not (bundle / document).is_file():
        raise RuntimeError(f"Frozen bundle is missing a public document: {document}")
suffix = ".exe" if os.name == "nt" else ""
app = QCoreApplication([])
agent = bundle / "_internal" / "nodebatch" / "remote_agent.txt"
if not agent.is_file():
    raise RuntimeError("Frozen bundle is missing the remote protocol source asset")
compile(agent.read_text(encoding="utf-8"), "remote_agent.txt", "exec")
catalog = bundle / "_internal" / "nodebatch" / "locales" / "ru.json"
assert json.loads(catalog.read_text(encoding="utf-8"))["Save"]
broker = AuthBroker()
broker.requested.connect(lambda request: broker.answer(request["id"], "bundle-probe"))


def wait(process, timeout=30):
    deadline = time.monotonic() + timeout
    while process.state() != QProcess.ProcessState.NotRunning and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    if process.state() != QProcess.ProcessState.NotRunning:
        process.kill()
        process.waitForFinished(3000)
        raise RuntimeError("Frozen executable did not finish")
    stderr = bytes(process.readAllStandardError()).decode("utf-8", "replace")
    if process.exitStatus() != QProcess.ExitStatus.NormalExit or process.exitCode() != 0:
        raise RuntimeError(f"Frozen executable failed ({process.exitCode()}): {stderr}")


helper = bundle / "nodebatch-askpass" / ("nodebatch-askpass" + suffix)
environment = QProcessEnvironment.systemEnvironment()
for key, value in broker.register("probe", "local", 20).items():
    environment.insert(key, value)
process = QProcess()
process.setProcessEnvironment(environment)
process.start(str(helper), ["Password:"])
wait(process)
assert bytes(process.readAllStandardOutput()).decode("utf-8").strip() == "bundle-probe"
print("OK: frozen askpass IPC")
with tempfile.TemporaryDirectory(prefix="nodebatch-bundle-probe-") as directory:
    for language in ("en", "ru"):
        process = QProcess()
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("QT_QPA_PLATFORM", "offscreen")
        process.setProcessEnvironment(environment)
        process.start(str(bundle / ("NodeBatch" + suffix)), ["--smoke-test", "--language", language,
                      "--config-dir", str(Path(directory) / "config"),
                      "--data-dir", str(Path(directory) / "state")])
        wait(process)
        print(f"OK: frozen GUI startup/shutdown ({language})")
broker.close()
