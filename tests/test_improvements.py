from nodebatch.i18n import tr
import copy
import shlex
from pathlib import Path
import pytest
import yaml
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QInputDialog, QMessageBox

from nodebatch.catalog import catalog
from nodebatch.config import validate_scenario, validate_groups, validate_dependencies, atomic_write
from nodebatch.models import Host, Step, download_destination
from nodebatch.groups_ui import GroupDialog
from nodebatch.transport import remote_command, command
from nodebatch.discovery import decode_output, discover, installation_plan

# Reuse the isolated window fixture; it contains only dummy aliases.
from test_gui import window


def test_click_rows_without_modifiers_checks_multiple(window):
    for index in (0, 1):
        item = window.host_list.item(index)
        QTest.mouseClick(window.host_list.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, window.host_list.visualItemRect(item).center())
    assert [h.alias for h in window._selected_hosts()] == ["node-000", "node-001"]
    window._clear_hosts()
    assert not window._selected_hosts()


def test_search_preserves_checks_and_refresh_preserves_members(window, wait):
    window.host_list.item(3).setCheckState(Qt.CheckState.Checked)
    window.search.setText("node-099")
    assert window._selected_hosts()[0].alias == "node-003"
    assert tr('hidden by search: 1') in window.selection_label.text()
    window._refresh_catalog()
    wait(lambda: window.catalog_ready)
    assert [h.alias for h in window._selected_hosts()] == ["node-003"]


def test_brief_success_output_visible_and_survives_mode_toggle(window):
    window._event("node-000", tr('Command'), "stdout", "DISTRIB_ID=Ubuntu\nDISTRIB_RELEASE=22.04\n")
    window._result("node-000", tr('Command'), "success", .2, 0, "")
    window._flush_output()
    assert "DISTRIB_ID=Ubuntu" in window.log_view.toPlainText()
    for mode in (1, 0):
        window.log_mode.setCurrentIndex(mode)
        assert "DISTRIB_RELEASE=22.04" in window.log_view.toPlainText()


def test_files_show_shared_hosts_and_exact_routes(window, tmp_path):
    window.host_list.item(0).setCheckState(Qt.CheckState.Checked)
    assert window.files_hosts.item(0).checkState() == Qt.CheckState.Checked
    window.files_hosts.item(1).setCheckState(Qt.CheckState.Checked)
    assert len(window._selected_hosts()) == 2
    window.local_path.setText(str(tmp_path / "payload.txt"))
    window.remote_path.setText("/tmp/payload.txt")
    assert "node-000: /tmp/payload.txt" in window.transfer_routes.toPlainText()
    assert "node-001" in window.files_selection.text()
    window.direction.setCurrentIndex(1)
    assert str(download_destination(str(tmp_path / "payload.txt"), "node-001", "/tmp/payload.txt")) \
        in window.transfer_routes.toPlainText()
    window.files_group.setCurrentIndex(window.files_group.findData("even"))
    assert len(window._selected_hosts()) == 50
    assert all("even" in host.groups for host in window._selected_hosts())


def test_groups_create_rename_delete_preserve_ssh_config(window, wait, monkeypatch):
    source = Path(window.runtime_profile["config"])
    original_ssh = source.read_bytes()
    inventory = window.store.bundle["inventory"]
    dialog = GroupDialog(inventory, window.hosts, window)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("gpu", True))
    dialog.create_group()
    assert dialog.active_group == "gpu"
    for i in (0, 2):
        dialog.members.item(i).setCheckState(Qt.CheckState.Checked)
    document = dialog.document()
    data = yaml.safe_load(document)
    assert data["groups"]["gpu"] == ["node-000", "node-002"]
    assert len(data["groups"]["even"]) == 50
    assert "groups" not in data["hosts"][0]
    window.store.save("inventory.yaml", document)
    wait(lambda: window.catalog_ready and window.group.findData("gpu") >= 0)
    window.group.setCurrentIndex(window.group.findData("gpu"))
    assert [h.alias for h in window._selected_hosts()] == ["node-000", "node-002"]
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("accelerators", True))
    dialog.rename_group()
    assert dialog.active_group == "accelerators" and "gpu" not in dialog.groups
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    dialog.delete_group()
    assert "accelerators" not in dialog.groups
    assert source.read_bytes() == original_ssh
    dialog.deleteLater()


def test_groups_for_ssh_only_aliases_and_empty_groups(tmp_path, bundle):
    profile = bundle["settings"]["profiles"]["native"]
    groups = {"workers": ["test-a"], "empty": []}
    validate_groups({"groups": groups})
    hosts = catalog(profile, (), groups)[0]
    assert hosts[0].alias == "test-a" and hosts[0].groups == ("workers",)
    assert hosts[1].groups == ()


@pytest.mark.parametrize("groups", [{"g": "host"}, {"": []}, {"g": ["-bad"]}, {"g": ["a", "a"]}])
def test_reject_bad_group_files(groups):
    with pytest.raises(ValueError):
        validate_groups({"groups": groups})


def test_nonroot_sudo_target_is_separate_from_ssh_user(bundle):
    step = Step("inspect", "id", True, sudo_user="postgres")
    host = Host("node", user="operator")
    words = shlex.split(remote_command(host, step, "PROMPT"))
    assert words[words.index("-u") + 1] == "postgres"
    _, argv = command(bundle["settings"]["profiles"]["native"], bundle["settings"]["execution"], host, step)
    assert "User=operator" in argv
    definition = {"id": "su", "name": "su", "os": ["unix"], "shells": ["sh"],
                  "steps": [{"name": "id", "command": "id", "sudo": True, "sudo_user": "postgres"}]}
    scenario = validate_scenario(definition)
    assert scenario.render({})[0][0].sudo_user == "postgres"
    definition["steps"][0]["sudo_user"] = "postgres;reboot"
    with pytest.raises(ValueError):
        validate_scenario(definition)


def test_gui_sudo_uses_target_without_changing_login(window, monkeypatch):
    captured = []
    monkeypatch.setattr(window, "_prepare", lambda steps, *args, **kwargs: captured.extend(steps))
    window.privilege.setCurrentIndex(1)
    window.sudo_target.setText("postgres")
    window.command.setPlainText("id")
    window._run_command()
    assert captured[0].sudo and captured[0].sudo_user == "postgres"


def test_wsl_utf16_output_is_decoded():
    assert decode_output("Ubuntu-26.04\r\nDebian\r\n".encode("utf-16-le")).splitlines() == ["Ubuntu-26.04", "Debian"]


def test_discovery_is_read_only_and_suggests_executables(bundle, tmp_path, monkeypatch):
    from nodebatch import discovery
    profile = copy.deepcopy(bundle["settings"]["profiles"]["native"])
    key = tmp_path / "key"
    key.write_text("private fixture - do not read", encoding="utf-8")
    profile["keys"] = [str(key)]
    profile["ssh"] = "not-a-real-ssh"
    original = copy.deepcopy(profile)
    monkeypatch.setattr(Path, "read_bytes", lambda *args: pytest.fail("Discovery must not read private keys"))
    detected = discovery.discover(profile, bundle["dependencies"])
    assert profile == original
    assert "ssh" in detected["invalid"]
    assert "ssh" not in detected["missing"]  # Existing system ssh is suggested, never reinstalled.
    assert detected["choices"]["ssh"]
    assert str(key) in detected["keys"]


def test_editable_program_fields_preserve_manual_paths(window, wait):
    panel = window.settings_panel
    wait(lambda: not window.tasks)
    field = panel.profile_fields["ssh"]
    assert field.isEditable() and field.count() >= 1
    assert not panel.profile_fields["wsl_distribution"].isEnabled()
    field.setText("/custom/ssh")
    field.suggestions(["/usr/bin/ssh"])
    assert field.text() == "/custom/ssh"


def test_installation_uses_manifest_and_correct_wsl_target(bundle, monkeypatch):
    from nodebatch import discovery
    profile = dict(bundle["settings"]["profiles"]["native"], backend="wsl", wsl_distribution="My Linux")
    detected = {"missing": ["ssh"], "manager": "apt-get"}
    command, launch, explanation = installation_plan(profile, detected, bundle["dependencies"])
    assert command[:4] == ["wsl.exe", "--distribution", "My Linux", "--exec"]
    assert "sudo" in command
    assert "openssh-client" in command[-1]
    assert launch[0] == "powershell.exe"
    with pytest.raises(ValueError):
        installation_plan(profile, {"missing": [], "manager": ""}, bundle["dependencies"])
    # Installation plans are pure data: no package manager is executed.


def test_install_cancel_does_not_launch_any_process(window, wait, monkeypatch):
    panel = window.settings_panel
    wait(lambda: not window.tasks)
    panel.detected_profile = panel._profile_snapshot()
    panel.detected = {"missing": ["ssh"], "manager": "apt-get"}
    monkeypatch.setattr("nodebatch.settings_ui.installation_plan",
        lambda *args: (["installer"], ("installer", []), "Install"))
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No)
    monkeypatch.setattr("nodebatch.settings_ui.QProcess.startDetached",
                        lambda *args: pytest.fail("Cancelled installation must not execute"))
    panel._install_dependencies()


def test_dependency_hot_reload_keeps_last_good(window):
    store = window.store
    previous = copy.deepcopy(store.bundle["dependencies"])
    atomic_write(store.root / "dependencies.yaml", "version: 1\nlinux: []\n")
    assert not store.reload()
    assert store.bundle["dependencies"] == previous
    atomic_write(store.root / "dependencies.yaml", yaml.safe_dump(previous))
    assert store.reload()


def test_configuration_reload_preserves_custom_sudo_target(window):
    window.privilege.setCurrentIndex(1)
    window.sudo_target.setText("postgres")
    window.store.reload()
    assert window.sudo_target.text() == "postgres"


def test_files_select_and_clear_buttons_update_both_tabs(window):
    window.files_select_all.click()
    assert len(window._selected_hosts()) == 100
    assert window.files_hosts.item(0).checkState() == Qt.CheckState.Checked
    window.files_clear_all.click()
    assert not window._selected_hosts()
    assert all(window.host_list.item(i).checkState() == Qt.CheckState.Unchecked
               for i in range(window.host_list.count()))
    assert tr('none') in window.files_selection.text()
    assert tr('Select hosts') in window.transfer_routes.toPlainText()


def test_files_select_all_obeys_file_group_not_command_search(window):
    window.files_group.setCurrentIndex(window.files_group.findData("even"))
    window.files_clear_all.click()
    window.search.setText("node-000")
    window.files_select_all.click()
    assert len(window._selected_hosts()) == 50
    assert all("even" in host.groups for host in window._selected_hosts())


@pytest.mark.parametrize("button", ["create_group_button", "files_create_group"])
def test_create_group_buttons_offer_ssh_hosts_and_save(window, monkeypatch, wait, button):
    from PySide6.QtWidgets import QDialog
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("SSH workers", True))
    def choose_members(dialog):
        assert dialog.active_group == "SSH workers"
        assert dialog.members.count() == 100
        for i in range(dialog.members.count()):
            item = dialog.members.item(i)
            if item.data(Qt.ItemDataRole.UserRole) in ("node-001", "node-003"):
                item.setCheckState(Qt.CheckState.Checked)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(GroupDialog, "exec", choose_members)
    getattr(window, button).click()
    wait(lambda: window.catalog_ready and window.group.currentData() == "SSH workers")
    assert window.store.bundle["groups"]["SSH workers"] == ["node-001", "node-003"]
    assert [h.alias for h in window._selected_hosts()] == ["node-001", "node-003"]
