import ast
import json
from pathlib import Path
from string import Formatter

import pytest
import yaml
from PySide6.QtWidgets import QMessageBox

from nodebatch.config import ConfigStore, bootstrap, validate_settings
from nodebatch.gui import MainWindow
from nodebatch.i18n import get_language, install_qt_translations, russian_catalog, set_language, tr
from nodebatch.models import Host, Job, Step


@pytest.fixture
def restore_language(qapp):
    previous = get_language()
    yield
    set_language(previous)
    install_qt_translations(qapp)


def test_catalog_preserves_format_arguments_and_hyphens():
    def fields(text):
        return sorted(field for _, field, _, _ in Formatter().parse(text) if field is not None)
    for english, russian in russian_catalog().items():
        assert fields(english) == fields(russian), english
        assert not any(dash in english + russian for dash in ("\u2013", "\u2014"))


@pytest.mark.parametrize("language", ["en", "ru"])
def test_both_interfaces_and_qt_buttons(qapp, tmp_path, wait, restore_language, language):
    set_language(language)
    install_qt_translations(qapp)
    root = tmp_path / "config"
    bootstrap(root)
    settings_path = root / "settings.yaml"
    settings = yaml.safe_load(settings_path.read_text())
    settings["language"] = language
    ssh_config = tmp_path / "empty-ssh-config"
    ssh_config.write_text("")
    settings["profiles"]["native"]["config"] = str(ssh_config)
    settings_path.write_text(yaml.safe_dump(settings))
    store = ConfigStore(root)
    window = MainWindow(store, tmp_path / "state")
    window.show()
    try:
        wait(lambda: window.catalog_ready)
        assert window.tabs.tabText(0) == tr("Hosts and commands")
        assert window.run_button.text() == tr("Review and run")
        assert window.settings_panel.language.currentData() == language
        assert "NodeBatch" in window.windowTitle()
        dialog = QMessageBox()
        dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        assert dialog.button(QMessageBox.StandardButton.Yes).text().replace("&", "") == tr("Yes")
        panel = window.settings_panel
        other_language = "ru" if language == "en" else "en"
        panel.language.setCurrentIndex(panel.language.findData(other_language))
        panel.save()
        assert store.bundle["settings"]["language"] == other_language
        assert get_language() == language  # An active run keeps its current language.
        assert panel.notice.text() == tr("Language saved. Restart NodeBatch to apply it.")
    finally:
        wait(lambda: not window.tasks)
        window.close()
        window.deleteLater()
        store.poll.stop()
        store.debounce.stop()
        store.deleteLater()


def test_language_setting_is_validated_and_backward_compatible(bundle):
    settings = bundle["settings"]
    settings.pop("language", None)
    validate_settings(settings)
    for language in ("auto", "en", "ru"):
        settings["language"] = language
        validate_settings(settings)
    settings["language"] = "unsupported"
    with pytest.raises(ValueError):
        validate_settings(settings)


def test_language_does_not_modify_commands_or_job_data(bundle, restore_language):
    for language in ("en", "ru"):
        set_language(language)
        command = "printf '%s' '$HOME --literal'"
        job = Job.create([Host("example")], [Step("User label", command)], bundle, ())
        assert job.steps[0].command == command and job.steps[0].name == "User label"


def test_all_source_message_keys_have_russian_translations():
    root = Path(__file__).resolve().parents[1] / "src/nodebatch"
    catalog = russian_catalog()
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr":
                if node.args and isinstance(node.args[0], ast.Constant):
                    assert node.args[0].value in catalog, (path.name, node.args[0].value)
