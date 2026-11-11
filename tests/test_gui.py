from nodebatch.i18n import tr
import yaml
from PySide6.QtCore import Qt
import pytest
from nodebatch.config import bootstrap, atomic_write, ConfigStore
from nodebatch.gui import MainWindow


@pytest.fixture
def window(qapp, tmp_path, wait):
    config = tmp_path / "config"
    bootstrap(config)
    settings_file = config / "settings.yaml"
    settings = yaml.safe_load(settings_file.read_text(encoding="utf-8"))
    ssh = tmp_path / "ssh config"
    ssh.write_text("Host *\n User operator\n", encoding="utf-8")
    settings["profiles"]["native"]["config"] = str(ssh)
    settings["profiles"]["native"]["known_hosts"] = str(tmp_path / "known_hosts")
    atomic_write(settings_file, yaml.safe_dump(settings))
    inventory = {"version": 1, "hosts": [{"alias": f"node-{i:03}", "groups": ["even" if i % 2 == 0 else "odd"],
                                        "os": "linux"} for i in range(100)]}
    atomic_write(config / "inventory.yaml", yaml.safe_dump(inventory))
    store = ConfigStore(config)
    widget = MainWindow(store, tmp_path / "data")
    widget.show()
    wait(lambda: widget.catalog_ready)
    yield widget
    wait(lambda: not widget.tasks)
    widget.close()
    widget.deleteLater()
    store.poll.stop()
    store.debounce.stop()
    store.deleteLater()
    qapp.processEvents()


def test_100_hosts_filtered_selection_and_bounded_logs(window):
    assert window.host_list.count() == 100
    window._select_visible()
    assert len(window._selected_hosts()) == 100
    window.group.setCurrentIndex(window.group.findData("even"))
    assert len(window._selected_hosts()) == 50
    window.search.setText("node-002")
    assert len(window._selected_hosts()) == 50
    assert tr('hidden by search: 49') in window.selection_label.text()
    for i in range(5000):
        window._event("node-002", "one", "stdout", f"line {i}")
    assert len(window.live_entries) == window.store.bundle["settings"]["logging"]["gui_blocks"]
    window.log_mode.setCurrentIndex(1)
    assert window.log_view.blockCount() <= window.log_view.maximumBlockCount()


@pytest.mark.parametrize("backend", ["native", "wsl"])
@pytest.mark.parametrize("sudo", [False, True])
def test_secret_scenario_rejected_before_job_preparation(window, monkeypatch, backend, sudo):
    from PySide6.QtWidgets import QMessageBox
    from nodebatch.config import validate_scenario
    scenario = validate_scenario({
        "id": "private", "name": "Private", "os": ["unix"], "shells": ["sh"],
        "parameters": {"value": {"secret": True}},
        "steps": [{"name": "One", "command": "printf '%s' {{value}}", "sudo": sudo}],
    })
    window.store.bundle["settings"]["profiles"]["native"]["backend"] = backend
    window.scenario.addItem(scenario.name, scenario)
    window.scenario.setCurrentIndex(window.scenario.count() - 1)
    window.parameter_fields["value"].setText("synthetic-private-value")
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[2]))
    monkeypatch.setattr(window, "_prepare", lambda *args, **kwargs: pytest.fail("Secret reached job preparation"))
    window._run_command()
    assert len(errors) == 1 and tr('secure step.secrets channel') in errors[0]
    assert "synthetic-private-value" not in errors[0]
    assert not window.history.runs()


def test_settings_forms_save_files(window, wait):
    panel = window.settings_panel
    panel.mode.setCurrentIndex(panel.mode.findData("parallel"))
    panel.parallelism.setValue(7)
    panel.save()
    assert window.store.bundle["settings"]["execution"]["mode"] == "parallel"
    assert window.store.bundle["settings"]["execution"]["parallelism"] == 7
    assert not panel.dirty
    wait(lambda: window.catalog_ready)


def test_start_during_maintenance_recovers_controls_without_launch(window, monkeypatch, wait):
    import sys
    import threading
    from PySide6.QtWidgets import QMessageBox
    from nodebatch import transport
    release, entered = threading.Event(), threading.Event()
    original = window.history.cleanup
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(window.history, "cleanup", slow)
    monkeypatch.setattr(window, "_review", lambda *args: True)
    monkeypatch.setattr(transport, "askpass_executable", lambda: sys.executable)
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[2]))
    try:
        window._cleanup(True)
        assert entered.wait(2)
        window.host_list.item(0).setCheckState(Qt.CheckState.Checked)
        window.command.setPlainText("true")
        window._run_command()
        assert errors and tr('maintenance') in errors[0]
        assert window.run_button.isEnabled() and window.transfer_button.isEnabled()
        assert not window.stop_button.isEnabled()
        assert window.runner.complete and not window.runner.active and not window.history.runs()
    finally:
        release.set()
        wait(lambda: window.history._maintenance is None or window.history._maintenance.done())


def test_close_does_not_wait_for_history_maintenance(window, monkeypatch):
    import threading
    import time
    from concurrent.futures import CancelledError
    entered, release = threading.Event(), threading.Event()
    original = window.history.cleanup
    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(window.history, "cleanup", paused)
    try:
        window._cleanup(True)
        assert entered.wait(2)
        start = time.monotonic()
        window.close()
        assert time.monotonic() - start < .5
        assert not window.isVisible() and window.history._closed
    finally:
        release.set()
        with pytest.raises(CancelledError):
            window.history._maintenance.result(timeout=3)


def test_external_reload_does_not_discard_scenario_edits(window):
    panel = window.settings_panel
    text = panel.scenario_editor.toPlainText() + "\n# unsaved"
    panel.scenario_editor.setPlainText(text)
    assert panel.scenario_dirty
    window.store.reload()
    assert panel.scenario_editor.toPlainText() == text


def test_idle_ssh_changes_refresh_without_restart(window, wait):
    profile = window.runtime_profile
    with open(profile["config"], "a", encoding="utf-8") as out:
        out.write("\nHost newly-added\n")
    window._poll_sources()
    wait(lambda: window.catalog_ready and window.host_list.count() == 101)


def test_close_waits_for_cancel_before_closing_history(window, wait, monkeypatch):
    import sys
    from pathlib import Path
    from PySide6.QtWidgets import QMessageBox
    from nodebatch import transport
    fake = str(Path(__file__).with_name("fake_ssh.py").resolve())
    def command(profile, execution, host, step=None, marker="", resolve=False, transfer=False):
        return sys.executable, [fake, "resolve" if resolve else "command", host.alias,
                                step.command if step else "", marker]
    monkeypatch.setattr(transport, "command", command)
    monkeypatch.setattr(transport, "askpass_executable", lambda: sys.executable)
    monkeypatch.setattr(window, "_review", lambda *args: True)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    window.host_list.item(0).setCheckState(Qt.CheckState.Checked)
    window.command.setPlainText("sleep-test")
    window._run_command()
    wait(lambda: any("started" in text for text in window.live_entries), 30)
    window.close()
    wait(lambda: window.closing and window.runner.complete)
    assert window.runner.had_unknown
