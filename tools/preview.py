"""Generate demo-only screenshots without SSH connections."""
import argparse
from pathlib import Path
import yaml
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from nodebatch.config import bootstrap, atomic_write, ConfigStore
from nodebatch.gui import MainWindow
from nodebatch.i18n import set_language, install_qt_translations

parser = argparse.ArgumentParser()
parser.add_argument("output", type=Path)
parser.add_argument("--language", choices=("auto", "en", "ru"), default="auto")
args = parser.parse_args()
set_language(args.language)
args.output.mkdir(parents=True, exist_ok=True)
config = args.output / "preview-config"
bootstrap(config)
ssh = args.output / "preview-ssh-config"
ssh.write_text("Host *\n User operator\n", encoding="utf-8")
settings = yaml.safe_load((config / "settings.yaml").read_text(encoding="utf-8"))
settings["profiles"]["native"]["config"] = str(ssh.resolve())
settings["profiles"]["native"]["known_hosts"] = str(args.output / "preview-known-hosts")
atomic_write(config / "settings.yaml", yaml.safe_dump(settings))
inventory = {"version": 1, "hosts": [
    {"alias": f"worker-{i:02}", "address": f"192.0.2.{i}", "os": "linux",
     "groups": ["workers"], "tags": ["demo"]} for i in range(1, 9)] + [
    {"alias": "storage-bsd", "address": "192.0.2.20", "os": "freebsd", "groups": ["storage"]}]}
atomic_write(config / "inventory.yaml", yaml.safe_dump(inventory))
app = QApplication([])
install_qt_translations(app)
from nodebatch.app import configure_font
configure_font(app)
store = ConfigStore(config)
window = MainWindow(store, args.output / "preview-data")
window.setWindowTitle("NodeBatch - demo data")
window.show()


def capture():
    if not window.catalog_ready or window.tasks:
        QTimer.singleShot(100, capture)
        return
    for i in (1, 2, 3):
        window.host_list.item(i).setCheckState(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.CheckState.Checked)
    window.command.setPlainText("uname -a\nuptime\ndf -h /")
    window._result("worker-01", "Diagnostics", "success", 0.42, 0, "")
    window._result("worker-02", "Diagnostics", "success", 0.38, 0, "")
    window._result("worker-03", "Diagnostics", "failed", 10.0, 255, "Example: connection timed out")
    window._flush_output()
    window.statusBar().showMessage("Preview - no host connections")
    window.grab().save(str(args.output / "nodebatch-main.png"))
    window.tabs.setCurrentIndex(3)
    window.settings_panel.tabs.setCurrentIndex(2)
    app.processEvents()
    window.grab().save(str(args.output / "nodebatch-settings.png"))
    window.tabs.setCurrentIndex(1)
    app.processEvents()
    window.grab().save(str(args.output / "nodebatch-files.png"))
    window.tabs.setCurrentIndex(3)
    window.settings_panel.tabs.setCurrentIndex(0)
    app.processEvents()
    window.grab().save(str(args.output / "nodebatch-connections.png"))
    window.close()


QTimer.singleShot(300, capture)
raise SystemExit(app.exec())
