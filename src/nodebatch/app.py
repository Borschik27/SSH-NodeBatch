from .i18n import tr, set_language, install_qt_translations
import argparse
import os
import sys
import sqlite3
from pathlib import Path


def default_paths():
    if os.name == "nt":
        return (Path(os.environ.get("APPDATA", str(Path.home()))) / "NodeBatch",
                Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NodeBatch")
    return (Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "nodebatch",
            Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "nodebatch")


def configure_font(app):
    # Qt's offscreen Windows plugin has no system font database. Load a system
    # font explicitly for previews; native Windows also gets a readable UI size.
    if os.name == "nt":
        from PySide6.QtGui import QFont, QFontDatabase
        if not QFontDatabase.families():
            path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/segoeui.ttf"
            if path.is_file():
                QFontDatabase.addApplicationFont(str(path))
        app.setFont(QFont("Segoe UI", 10))


def main():
    parser = argparse.ArgumentParser(description=tr('NodeBatch - SSH batch runner'))
    config, data = default_paths()
    parser.add_argument("--config-dir", type=Path, default=config)
    parser.add_argument("--data-dir", type=Path, default=data)
    parser.add_argument("--language", choices=("auto", "en", "ru"), help="Interface language (overrides settings.yaml)")
    parser.add_argument("--validate-config", action="store_true")
    parser.add_argument("--smoke-test", action="store_true", help=tr('Open the GUI and exit without SSH connections'))
    args = parser.parse_args()
    if args.language:
        set_language(args.language)
    from .config import ConfigStore, bootstrap, load_bundle
    if args.validate_config:
        bootstrap(args.config_dir)
        try:
            bundle = load_bundle(args.config_dir)
            print(f"OK: {len(bundle['hosts'])} inventory hosts, {len(bundle['scenarios'])} scenarios")
            return 0
        except (ValueError, OSError, TypeError, KeyError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication(sys.argv[:1])
    from . import __version__
    app.setApplicationName("NodeBatch")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("NodeBatch")
    configure_font(app)
    try:
        store = ConfigStore(args.config_dir)
        set_language(args.language or store.bundle["settings"].get("language", "auto"))
        install_qt_translations(app)
        from .gui import MainWindow
        window = MainWindow(store, args.data_dir)
    except (ValueError, OSError, TypeError, KeyError, RuntimeError, sqlite3.Error) as exc:
        QMessageBox.critical(None, tr('NodeBatch: startup error'), tr('{p0}\n\nConfiguration: {p1}', p0=exc, p1=args.config_dir))
        return 2
    window.show()
    if args.smoke_test:
        def finish():
            if window.tasks:
                QTimer.singleShot(100, finish)
            else:
                window.close()
        QTimer.singleShot(300, finish)
    return app.exec()
