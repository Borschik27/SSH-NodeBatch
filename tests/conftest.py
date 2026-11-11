import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import time
import pytest
from PySide6.QtWidgets import QApplication
from nodebatch.config import bootstrap, load_bundle


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool
    QThreadPool.globalInstance().waitForDone(5000)
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def wait(qapp):
    def until(predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert predicate(), "Timed out waiting for Qt"
    return until


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "config"
    bootstrap(root)
    data = load_bundle(root)
    config = tmp_path / "ssh config"
    config.write_text("Host test-a test-b\n  HostName 192.0.2.1\n", encoding="utf-8")
    profile = data["settings"]["profiles"]["native"]
    profile.update({"config": str(config), "known_hosts": str(tmp_path / "known_hosts")})
    return data
