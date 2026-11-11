from __future__ import annotations
from .i18n import tr
import copy
from pathlib import Path

import yaml
from PySide6.QtCore import Signal, QUrl, QTimer, QProcess, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget, QComboBox,
    QLineEdit, QPushButton, QPlainTextEdit, QSpinBox, QCheckBox, QLabel,
    QFileDialog, QMessageBox, QInputDialog, QScrollArea,
)

from .config import atomic_write, load_bundle, validate_scenario, parse_yaml
from .widgets import EditableChoice
from .discovery import discover, installation_plan


def dump(data):
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def editor():
    widget = QPlainTextEdit()
    widget.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    return widget


class SettingsPanel(QWidget):
    saved = Signal()

    def __init__(self, store, parent=None, background=None):
        super().__init__(parent)
        self.store = store
        self.background = background
        self.discovery_generation = 0
        self.detected = None
        self.discovery_timer = QTimer(self)
        self.discovery_timer.setSingleShot(True)
        self.discovery_timer.timeout.connect(self._discover)
        self.loading = False
        self.dirty = False
        self.scenario_dirty = False
        self.scenario_selected_index = -1
        self.base_signature = None
        layout = QVBoxLayout(self)
        language_row = QHBoxLayout()
        language_row.addWidget(QLabel(tr("Interface language (restart required)")))
        self.language = QComboBox()
        self.language.addItem(tr("System language"), "auto")
        self.language.addItem("English", "en")
        self.language.addItem(tr("Russian"), "ru")
        language_row.addWidget(self.language)
        language_row.addStretch()
        layout.addLayout(language_row)
        config_path = QLabel(tr('Application configuration: ') + str(store.root.resolve()))
        config_path.setWordWrap(True)
        config_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(config_path)
        edit_config = QPushButton(tr('Open settings.yaml in an editor'))
        edit_config.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(store.root.resolve() / "settings.yaml"))))
        layout.addWidget(edit_config)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        connections = QWidget()
        form = QFormLayout(connections)
        self.profiles = QComboBox()
        self.profiles.currentIndexChanged.connect(self._profile_changed)
        row = QHBoxLayout()
        row.addWidget(self.profiles)
        add = QPushButton(tr('Add profile'))
        add.clicked.connect(self._add_profile)
        row.addWidget(add)
        self.active_profile = QComboBox()
        form.addRow(tr('Edited profile'), row)
        form.addRow(tr('Active profile'), self.active_profile)
        self.backend = QComboBox()
        self.backend.addItems(["native", "wsl"])
        form.addRow(tr('Backend'), self.backend)
        self.profile_fields = {}
        for key, title in (("ssh", tr('SSH program')), ("sftp", tr('SFTP program')),
                           ("config", tr('SSH config (empty = default)')),
                           ("known_hosts", tr('known_hosts (empty = default)')),
                           ("wsl_distribution", tr('WSL distribution')),
                           ("wsl_home", tr('HOME in WSL (empty = detect)'))):
            line = EditableChoice()
            self.profile_fields[key] = line
            row = QHBoxLayout()
            row.addWidget(line)
            if key in ("config", "known_hosts"):
                browse = QPushButton(tr('Browse...'))
                browse.clicked.connect(lambda checked=False, field=line: self._browse(field))
                row.addWidget(browse)
            form.addRow(title, row)
        self.keys = QPlainTextEdit()
        self.keys.setMaximumHeight(90)
        self.keys.setPlaceholderText(tr('Private key paths, one per line. Empty uses the agent and OpenSSH settings.'))
        form.addRow("IdentityFile", self.keys)
        add_key = QPushButton(tr('Add key...'))
        add_key.clicked.connect(self._add_key)
        form.addRow("", add_key)
        key_row = QHBoxLayout()
        self.found_keys = QComboBox()
        key_row.addWidget(self.found_keys, 1)
        add_found_key = QPushButton(tr('Add detected key'))
        add_found_key.clicked.connect(self._add_found_key)
        key_row.addWidget(add_found_key)
        form.addRow(tr('Detected keys'), key_row)
        checks = QHBoxLayout()
        self.check_environment = QPushButton(tr('Detect / check environment'))
        self.check_environment.clicked.connect(self._discover)
        checks.addWidget(self.check_environment)
        self.install_dependency = QPushButton(tr('Install missing dependencies...'))
        self.install_dependency.setEnabled(False)
        self.install_dependency.clicked.connect(self._install_dependencies)
        checks.addWidget(self.install_dependency)
        form.addRow(checks)
        self.environment_status = QLabel()
        self.environment_status.setWordWrap(True)
        self.environment_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(self.environment_status)
        hint = QLabel(tr('Use Unix paths in WSL. On Windows, install the application and keep transferred files on a local drive, not a UNC share.'))
        hint.setWordWrap(True)
        form.addRow(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(connections)
        self.tabs.addTab(scroll, tr('Connections'))
        self.backend.currentIndexChanged.connect(self._backend_changed)

        sources = QWidget()
        sl = QVBoxLayout(sources)
        sl.addWidget(QLabel(tr('inventory.yaml - additional hosts, OS, shells, groups and tags')))
        self.inventory = editor()
        sl.addWidget(self.inventory)
        self.tabs.addTab(sources, tr('Host sources'))

        execution = QWidget()
        ef = QFormLayout(execution)
        self.mode = QComboBox()
        self.mode.addItem(tr('Sequential'), "sequential")
        self.mode.addItem(tr('Parallel'), "parallel")
        ef.addRow(tr('Mode'), self.mode)
        self.parallelism = QSpinBox()
        self.parallelism.setRange(1, 100)
        ef.addRow(tr('Concurrent hosts'), self.parallelism)
        self.stop_error = QCheckBox(tr('Do not start new hosts or steps after an error'))
        ef.addRow(self.stop_error)
        self.sudo_user = QLineEdit()
        ef.addRow(tr('Default sudo user'), self.sudo_user)
        self.sudo_user.textEdited.connect(self.mark_dirty)
        self.timeouts = {}
        for key, title, lo, hi in (("connect_timeout", tr('Connection timeout, s'), 1, 300),
                                   ("step_timeout", tr('Step timeout, s'), 1, 86400),
                                   ("auth_timeout", tr('Password timeout, s'), 5, 600)):
            spin = QSpinBox()
            spin.setRange(lo, hi)
            self.timeouts[key] = spin
            ef.addRow(title, spin)
        self.tabs.addTab(execution, tr('Execution'))

        security = QWidget()
        sec = QVBoxLayout(security)
        note = QLabel(tr('Rules help prevent operator mistakes; they cannot prove a script is safe. deny takes precedence over confirm and allow. SSH configs are trusted: ProxyCommand and Match exec may run local programs.'))
        note.setWordWrap(True)
        sec.addWidget(note)
        self.safety = editor()
        sec.addWidget(self.safety)
        self.tabs.addTab(security, tr('Safety'))

        logs = QWidget()
        lf = QFormLayout(logs)
        self.days = QSpinBox()
        self.days.setRange(1, 36500)
        self.max_mib = QSpinBox()
        self.max_mib.setRange(1, 1048576)
        self.run_mib = QSpinBox()
        self.run_mib.setRange(1, 1048576)
        self.reserve_mib = QSpinBox()
        self.reserve_mib.setRange(0, 1048576)
        self.blocks = QSpinBox()
        self.blocks.setRange(100, 100000)
        self.verbose = QCheckBox(tr('Detailed log on startup'))
        lf.addRow(tr('Keep history, days'), self.days)
        lf.addRow(tr('History directory budget, MiB'), self.max_mib)
        lf.addRow(tr('Run log limit, MiB'), self.run_mib)
        lf.addRow(tr('Free disk reserve, MiB'), self.reserve_mib)
        lf.addRow(tr('GUI buffer lines'), self.blocks)
        lf.addRow(self.verbose)
        self.tabs.addTab(logs, tr('Logs and storage'))

        scenarios = QWidget()
        sc = QVBoxLayout(scenarios)
        self.scenario_selector = QComboBox()
        self.scenario_selector.currentIndexChanged.connect(self._load_scenario)
        sc.addWidget(self.scenario_selector)
        self.scenario_editor = editor()
        self.scenario_editor.setPlaceholderText(tr('Select a scenario or create one. {{param}} must be a separate, unquoted shell word.'))
        sc.addWidget(self.scenario_editor)
        self.scenario_editor.textChanged.connect(self._scenario_edited)
        actions = QHBoxLayout()
        new = QPushButton(tr('New scenario'))
        new.clicked.connect(self._new_scenario)
        save = QPushButton(tr('Save scenario'))
        save.clicked.connect(self._save_scenario)
        actions.addWidget(new)
        actions.addWidget(save)
        sc.addLayout(actions)
        self.tabs.addTab(scenarios, tr('Scenarios'))

        buttons = QHBoxLayout()
        save = QPushButton(tr('Save settings'))
        save.clicked.connect(self.save)
        reload_button = QPushButton(tr('Reload files'))
        reload_button.clicked.connect(lambda: self.load(self.store.bundle, force=True))
        folder = QPushButton(tr('Open configuration directory'))
        folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.root))))
        buttons.addWidget(save)
        buttons.addWidget(reload_button)
        buttons.addWidget(folder)
        buttons.addStretch()
        layout.addLayout(buttons)

        for widget in [self.active_profile, self.backend, self.mode, self.language]:
            widget.currentIndexChanged.connect(self.mark_dirty)
        for line in self.profile_fields.values():
            line.textEdited.connect(self.mark_dirty)
        for widget in [self.keys, self.inventory, self.safety]:
            widget.textChanged.connect(self.mark_dirty)
        for spin in [self.parallelism, self.days, self.max_mib, self.run_mib, self.reserve_mib, self.blocks, *self.timeouts.values()]:
            spin.valueChanged.connect(self.mark_dirty)
        for check in [self.stop_error, self.verbose]:
            check.toggled.connect(self.mark_dirty)
        self.keys.textChanged.connect(lambda: self.discovery_timer.start(600) if not self.loading else None)
        for field in self.profile_fields.values():
            field.textEdited.connect(lambda *_: self.discovery_timer.start(600) if not self.loading else None)
        self.load(store.bundle, force=True)

    def _backend_changed(self, *_):
        is_wsl = self.backend.currentText() == "wsl"
        for key in ("wsl_distribution", "wsl_home"):
            self.profile_fields[key].setEnabled(is_wsl)
        if not self.loading:
            self.discovery_timer.start(0)

    def _profile_snapshot(self):
        return {**self.profile_data[self.profiles.currentText()],
                "backend": self.backend.currentText(),
                "keys": [line.strip() for line in self.keys.toPlainText().splitlines() if line.strip()],
                **{key: field.text().strip() for key, field in self.profile_fields.items()}}

    def _discover(self, *_):
        if self.loading or not self.profiles.currentText():
            return
        self.discovery_generation += 1
        generation = self.discovery_generation
        profile = self._profile_snapshot()
        dependencies = copy.deepcopy(self.store.bundle["dependencies"])
        self.detected = None
        self.check_environment.setEnabled(False)
        self.install_dependency.setEnabled(False)
        self.environment_status.setText(tr('Checking the selected environment...'))
        def done(data):
            if generation != self.discovery_generation:
                return
            self.check_environment.setEnabled(True)
            if profile != self._profile_snapshot():
                self.discovery_timer.start(0)
                return
            was_loading, self.loading = self.loading, True
            for key, values in data["choices"].items():
                self.profile_fields[key].suggestions(values)
            self.found_keys.clear()
            self.found_keys.addItems(data["keys"])
            self.loading = was_loading
            self.detected = data
            self.detected_profile = profile
            self.install_dependency.setEnabled(bool(data["missing"]))
            target = ("WSL: " + profile["wsl_distribution"]) if profile["backend"] == "wsl" else tr('Current system')
            self.environment_status.setText(target + "\n" + "\n".join(data["messages"]))
        def failed(message):
            if generation == self.discovery_generation:
                self.check_environment.setEnabled(True)
                self.environment_status.setText(tr('Cannot check environment: ') + message)
        if self.background:
            self.background(lambda: discover(profile, dependencies), done, failed)
        else:
            try:
                done(discover(profile, dependencies))
            except (ValueError, OSError) as exc:
                failed(str(exc))

    def _add_found_key(self):
        key = self.found_keys.currentText()
        if key and key not in self.keys.toPlainText().splitlines():
            self.keys.appendPlainText(key)

    def _install_dependencies(self):
        if not self.detected or self._profile_snapshot() != self.detected_profile:
            self._discover()
            return
        try:
            import shlex
            command, launch, description = installation_plan(
                self.detected_profile, self.detected, self.store.bundle["dependencies"])
            text = description + tr('\n\nEnvironment: ') + (
                "WSL: " + self.detected_profile["wsl_distribution"] if self.detected_profile["backend"] == "wsl"
                else tr('this operating system')) + tr('\nCommand:\n') + shlex.join(command) +                 tr('\n\nCommand source: ') + str(self.store.root / "dependencies.yaml") +                 tr('\nOpen the system installer? Check the environment again after installation.')
            if QMessageBox.question(self, tr('Install dependencies'), text,
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            started, _ = QProcess.startDetached(launch[0], launch[1])
            if not started:
                raise ValueError(tr('Cannot open the system installer.'))
            self.environment_status.setText(tr('Installation started. Check the environment again when it finishes. Complete initial setup for a new WSL distribution.'))
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, tr('Install dependencies'), str(exc))

    def mark_dirty(self, *_):
        if not self.loading:
            self.dirty = True
            self.notice.setText(tr('There are unsaved changes.'))

    def _capture_profile(self):
        name = self.profiles.currentText()
        if name:
            self.profile_data[name] = {**self.profile_data[name], "backend": self.backend.currentText(),
                                      "keys": [p.strip() for p in self.keys.toPlainText().splitlines() if p.strip()],
                                      **{key: line.text().strip() for key, line in self.profile_fields.items()}}

    def _profile_changed(self, *_):
        if self.loading:
            return
        previous = getattr(self, "displayed_profile", "")
        if previous:
            self.profile_data[previous] = {**self.profile_data[previous], "backend": self.backend.currentText(),
                "keys": [p.strip() for p in self.keys.toPlainText().splitlines() if p.strip()],
                **{k: v.text().strip() for k, v in self.profile_fields.items()}}
        self._display_profile()

    def _display_profile(self):
        name = self.profiles.currentText()
        if not name:
            return
        was_loading, self.loading = self.loading, True
        data = self.profile_data[name]
        self.backend.setCurrentText(data["backend"])
        for key, line in self.profile_fields.items():
            line.setText(data[key])
        self.keys.setPlainText("\n".join(data["keys"]))
        self.displayed_profile = name
        self.loading = was_loading
        self._backend_changed()
        if not self.loading:
            self.discovery_timer.start(0)

    def _add_profile(self):
        name, ok = QInputDialog.getText(self, tr('Profile'), tr('Unique name for the new profile:'))
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in self.profile_data:
            QMessageBox.warning(self, tr('Profile'), tr('This name already exists.'))
            return
        self._capture_profile()
        template = copy.deepcopy(next(iter(self.profile_data.values())))
        template.update({"backend": "wsl" if name.lower() == "wsl" else "native",
                         "config": "", "keys": [], "known_hosts": "", "wsl_home": ""})
        self.profile_data[name] = template
        self.profiles.addItem(name)
        self.active_profile.addItem(name)
        self.profiles.setCurrentText(name)
        self.mark_dirty()

    def _browse(self, field):
        if self.backend.currentText() == "wsl":
            path, ok = QInputDialog.getText(self, tr('WSL path'), tr('Absolute Unix path or ~/...:'), text=field.text())
        else:
            path, _ = QFileDialog.getOpenFileName(self, tr('Select a file'))
            ok = bool(path)
        if ok:
            field.setText(path)
            self.mark_dirty()
            self.discovery_timer.start(0)

    def _add_key(self):
        if self.backend.currentText() == "wsl":
            path, ok = QInputDialog.getText(self, tr('WSL key'), tr('Private key path inside WSL:'))
        else:
            path, _ = QFileDialog.getOpenFileName(self, tr('Select a private key'))
            ok = bool(path)
        if ok:
            self.keys.appendPlainText(path)

    def load(self, bundle, force=False):
        if (self.dirty or self.scenario_dirty) and not force:
            self.notice.setText(tr('Files changed externally. Your edits remain in the editor; reload files before saving.'))
            return
        self.loading = True
        settings = bundle["settings"]
        self.profile_data = copy.deepcopy(settings["profiles"])
        self.profiles.clear()
        self.profiles.addItems(self.profile_data)
        self.active_profile.clear()
        self.active_profile.addItems(self.profile_data)
        self.active_profile.setCurrentText(settings["active_profile"])
        self.language.setCurrentIndex(self.language.findData(settings.get("language", "auto")))
        self.profiles.setCurrentText(settings["active_profile"])
        self._display_profile()
        self.inventory.setPlainText((self.store.root / "inventory.yaml").read_text(encoding="utf-8-sig"))
        self.safety.setPlainText((self.store.root / "safety.yaml").read_text(encoding="utf-8-sig"))
        e, log = settings["execution"], settings["logging"]
        self.mode.setCurrentIndex(self.mode.findData(e["mode"]))
        self.parallelism.setValue(e["parallelism"])
        self.stop_error.setChecked(e["stop_on_error"])
        self.sudo_user.setText(e.get("sudo_user", "root"))
        for key, spin in self.timeouts.items():
            spin.setValue(e[key])
        self.days.setValue(log["retention_days"])
        self.max_mib.setValue(max(1, log.get("history_max_bytes", log["max_bytes"]) // 1048576))
        self.run_mib.setValue(max(1, log.get("run_max_bytes", log["max_bytes"]) // 1048576))
        self.reserve_mib.setValue(log.get("min_free_bytes", 16777216) // 1048576)
        self.blocks.setValue(log["gui_blocks"])
        self.verbose.setChecked(log["verbose"])
        self.scenario_selector.clear()
        for path in sorted((self.store.root / "scenarios").glob("*.yaml")):
            self.scenario_selector.addItem(path.stem, str(path))
        self._load_scenario()
        self.base_signature = self.store.signature()
        self.loading = False
        self.dirty = False
        self.notice.setText(tr('Settings apply automatically to subsequent runs.'))
        self.discovery_timer.start(0)

    def save(self):
        try:
            previous_language = self.store.bundle["settings"].get("language", "auto")
            if self.store.signature() != self.base_signature:
                raise ValueError(tr('Files changed externally. Click Reload files first.'))
            self._capture_profile()
            settings = copy.deepcopy(self.store.bundle["settings"])
            settings.update({"active_profile": self.active_profile.currentText(), "profiles": self.profile_data})
            settings["language"] = self.language.currentData()
            settings["execution"].update({"mode": self.mode.currentData(), "parallelism": self.parallelism.value(),
                "stop_on_error": self.stop_error.isChecked(), "sudo_user": self.sudo_user.text().strip(), **{k: s.value() for k, s in self.timeouts.items()}})
            settings["logging"].update({"retention_days": self.days.value(),
                "max_bytes": self.max_mib.value() * 1048576, "gui_blocks": self.blocks.value(),
                "history_max_bytes": self.max_mib.value() * 1048576,
                "run_max_bytes": self.run_mib.value() * 1048576,
                "min_free_bytes": self.reserve_mib.value() * 1048576,
                "verbose": self.verbose.isChecked()})
            documents = {"settings.yaml": dump(settings), "inventory.yaml": self.inventory.toPlainText(),
                         "safety.yaml": self.safety.toPlainText()}
            load_bundle(self.store.root, documents)
            for name, text in documents.items():
                atomic_write(self.store.root / name, text)
            self.dirty = False
            self.base_signature = self.store.signature()
            self.store.reload()
            self.saved.emit()
            if settings["language"] != previous_language:
                self.notice.setText(tr("Language saved. Restart NodeBatch to apply it."))
        except (ValueError, OSError, TypeError, KeyError) as exc:
            QMessageBox.warning(self, tr('Cannot save'), str(exc))

    def _scenario_edited(self):
        if not self.loading:
            self.scenario_dirty = True
            self.notice.setText(tr('There are unsaved scenario edits.'))

    def _discard_scenario(self):
        return not self.scenario_dirty or QMessageBox.question(self, tr('Scenario'),
            tr('Discard unsaved scenario edits?'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    def _load_scenario(self):
        if not self.loading and not self._discard_scenario():
            self.scenario_selector.blockSignals(True)
            self.scenario_selector.setCurrentIndex(self.scenario_selected_index)
            self.scenario_selector.blockSignals(False)
            return
        self.scenario_selected_index = self.scenario_selector.currentIndex()
        path = self.scenario_selector.currentData()
        self.scenario_path = Path(path) if path else None
        self.scenario_base = self.scenario_path.read_text(encoding="utf-8-sig") if self.scenario_path else ""
        was_loading, self.loading = self.loading, True
        self.scenario_editor.setPlainText(self.scenario_base)
        self.loading = was_loading
        self.scenario_dirty = False

    def _new_scenario(self):
        if not self._discard_scenario():
            return
        self.scenario_path = None
        self.scenario_base = ""
        self.scenario_editor.setPlainText(dump({"id": "new-scenario", "name": tr('New scenario'),
            "description": "", "os": ["unix"], "shells": ["sh", "bash"], "parameters": {},
            "steps": [{"name": tr('Diagnostics'), "command": "uname -a", "sudo": False}]}))

    def _save_scenario(self):
        try:
            text = self.scenario_editor.toPlainText()
            scenario = validate_scenario(parse_yaml(text))
            path = self.scenario_path or self.store.root / "scenarios" / (scenario.id + ".yaml")
            if path.exists() and path.read_text(encoding="utf-8-sig") != self.scenario_base:
                raise ValueError(tr('The file changed externally or already exists; reload it.'))
            for other in (self.store.root / "scenarios").glob("*.yaml"):
                if other != path and parse_yaml(other.read_text(encoding="utf-8-sig")).get("id") == scenario.id:
                    raise ValueError(tr('This scenario ID already exists.'))
            atomic_write(path, text)
            self.scenario_path, self.scenario_base = path, text
            self.scenario_dirty = False
            self.base_signature = self.store.signature()
            self.store.reload()
            self.notice.setText(tr('Scenario saved.'))
        except (ValueError, OSError, TypeError, KeyError) as exc:
            QMessageBox.warning(self, tr('Scenario'), str(exc))
