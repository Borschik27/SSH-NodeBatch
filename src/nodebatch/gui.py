from __future__ import annotations
from .i18n import tr
import copy
import time
import sqlite3
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget,
    QSplitter, QLineEdit, QComboBox, QPushButton, QLabel, QListWidget, QListWidgetItem,
    QPlainTextEdit, QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView,
    QMessageBox, QFileDialog, QDialog, QDialogButtonBox, QInputDialog,
)

from . import __version__
from .auth import AuthBroker
from .catalog import catalog, fingerprint
from .config import load_bundle, validate_sudo_user, read_yaml
from .engine import JobRunner
from .history import History, Redactor
from .models import Job, Step, Transfer
from .safety import assess
from .settings_ui import SettingsPanel
from .transport import diagnostics, resolve_profile
from .widgets import CheckList
from .groups_ui import GroupDialog
from .buffers import TextBuffer

STATUS = {"success": "Success", "failed": "Error", "unknown": "Outcome unknown",
          "running": "Running", "cancelled": "Cancelled", "skipped": "Skipped"}


class WorkSignals(QObject):
    done = Signal(object)
    error = Signal(str)


class Work(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = WorkSignals()

    def run(self):
        try:
            self.signals.done.emit(self.function())
        except Exception as exc:
            self.signals.error.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self, store, data_root):
        super().__init__()
        self.store, self.history, self.auth = store, History(data_root), AuthBroker(self)
        self.runner, self.runtime_profile = None, None
        self.hosts, self.catalog_generation, self.catalog_ready = (), 0, False
        self.tasks, self.auth_queue, self.auth_dialog = set(), deque(), None
        self.live_entries, self.summary_entries = TextBuffer(), TextBuffer()
        self.output_queue = TextBuffer(maxlen=200, max_bytes=1048576)
        self.closing = False
        self.closing_requested = False
        self.setWindowTitle(tr('NodeBatch {p0} - SSH batch runner', p0=__version__))
        self.resize(1250, 850)
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self._commands_page()
        self._files_page()
        self._history_page()
        self.settings_panel = SettingsPanel(store, background=self._background)
        self.tabs.addTab(self.settings_panel, tr('Settings'))
        self.store.changed.connect(self._configuration_changed)
        self.store.invalid.connect(self._config_error)
        self.auth.requested.connect(self._queue_auth)
        self.flush_timer = QTimer(self)
        self.flush_timer.timeout.connect(self._flush_output)
        self.flush_timer.start(100)
        self.cleanup_timer = QTimer(self)
        self.cleanup_timer.timeout.connect(self._cleanup)
        self.cleanup_timer.start(60000)
        self.source_timer = QTimer(self)
        self.source_timer.timeout.connect(self._poll_sources)
        self.source_timer.start(store.bundle["settings"]["watch"]["poll_ms"])
        diag = QPushButton(tr('OpenSSH diagnostics'))
        diag.clicked.connect(self.run_diagnostics)
        self.statusBar().addPermanentWidget(diag)
        self._configuration_changed(store.bundle)

    def _commands_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.config_notice = QLabel()
        self.config_notice.setWordWrap(True)
        self.config_notice.hide()
        layout.addWidget(self.config_notice)
        splitter = QSplitter()
        layout.addWidget(splitter, 1)
        left, right = QWidget(), QWidget()
        ll, rl = QVBoxLayout(left), QVBoxLayout(right)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr('Name, address or tag'))
        self.search.textChanged.connect(self._filter_hosts)
        self.group = QComboBox()
        self.group.addItem(tr('All groups'), "")
        self.group.currentIndexChanged.connect(self._group_changed)
        ll.addWidget(self.search)
        ll.addWidget(self.group)
        group_row = QHBoxLayout()
        self.create_group_button = QPushButton(tr('Create group...'))
        self.create_group_button.clicked.connect(self._create_group)
        group_row.addWidget(self.create_group_button)
        self.manage_groups_button = QPushButton(tr('Manage groups...'))
        self.manage_groups_button.clicked.connect(self._manage_groups)
        group_row.addWidget(self.manage_groups_button)
        ll.addLayout(group_row)
        self.host_list = CheckList()
        self.host_list.itemChanged.connect(self._selection_changed)
        ll.addWidget(self.host_list)
        buttons = QHBoxLayout()
        for title, action in ((tr('Select visible'), self._select_visible),
                              (tr('Clear selection'), self._clear_hosts), (tr('Refresh'), self._refresh_catalog)):
            button = QPushButton(title)
            button.clicked.connect(action)
            buttons.addWidget(button)
        ll.addLayout(buttons)
        self.selection_label = QLabel(tr('Selected: 0'))
        ll.addWidget(self.selection_label)
        top = QHBoxLayout()
        self.scenario = QComboBox()
        self.scenario.addItem(tr('Custom command'), None)
        self.scenario.currentIndexChanged.connect(self._scenario_changed)
        top.addWidget(self.scenario, 1)
        self.privilege = QComboBox()
        self.privilege.addItem(tr('SSH user'), False)
        self.privilege.addItem(tr('Use sudo'), True)
        top.addWidget(self.privilege)
        self.sudo_target = QLineEdit()
        self.sudo_target.setPlaceholderText(tr('sudo user'))
        self.sudo_target.setMaximumWidth(180)
        self.sudo_target.setToolTip(tr('The SSH connection uses the SSH user. sudo -u selects the user that runs the command.'))
        self.sudo_target.hide()
        self.privilege.currentIndexChanged.connect(
            lambda: self.sudo_target.setVisible(bool(self.privilege.currentData())))
        top.addWidget(self.sudo_target)
        rl.addLayout(top)
        self.scenario_description = QLabel()
        self.scenario_description.setWordWrap(True)
        rl.addWidget(self.scenario_description)
        self.parameters_widget = QWidget()
        self.parameters_layout = QFormLayout(self.parameters_widget)
        self.parameter_fields = {}
        rl.addWidget(self.parameters_widget)
        self.command = QPlainTextEdit()
        self.command.setPlaceholderText(tr('Command or multiline shell block. You will review it before running.'))
        rl.addWidget(self.command, 1)
        controls = QHBoxLayout()
        self.run_button = QPushButton(tr('Review and run'))
        self.run_button.clicked.connect(self._run_command)
        self.stop_button = QPushButton(tr('Stop'))
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        self.resume_button = QPushButton(tr('Review SSH files'))
        self.resume_button.hide()
        self.resume_button.clicked.connect(self._resume)
        for button in (self.run_button, self.stop_button, self.resume_button):
            controls.addWidget(button)
        controls.addStretch()
        rl.addLayout(controls)
        self.execution_label = QLabel()
        self.execution_label.setWordWrap(True)
        rl.addWidget(self.execution_label)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([350, 850])
        self.results = QTableWidget(0, 6)
        self.results.setHorizontalHeaderLabels([tr('Host'), tr('Step'), tr('Status'), tr('Duration, s'), tr('Code'), tr('Reason')])
        self.results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.results.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.results.setMaximumHeight(210)
        layout.addWidget(self.results)
        log_bar = QHBoxLayout()
        log_bar.addWidget(QLabel(tr('Output')))
        self.log_mode = QComboBox()
        self.log_mode.addItems([tr('Results and status'), tr('Detailed log')])
        self.log_mode.currentIndexChanged.connect(self._render_live_log)
        log_bar.addWidget(self.log_mode)
        log_bar.addStretch()
        export = QPushButton(tr('Export log...'))
        export.clicked.connect(lambda: self._export(self.runner.job.id if self.runner else None))
        log_bar.addWidget(export)
        layout.addLayout(log_bar)
        self.log_view = QPlainTextEdit()
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(220)
        layout.addWidget(self.log_view)
        self.tabs.addTab(page, tr('Hosts and commands'))

    def _files_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel(tr('Select hosts below. Selection is shared with the commands tab. SFTP uses the SSH user; the destination path includes the filename.'))
        note.setWordWrap(True)
        layout.addWidget(note)
        self.files_profile = QLabel()
        layout.addWidget(self.files_profile)
        self.files_group = QComboBox()
        self.files_group.currentIndexChanged.connect(self._files_group_changed)
        layout.addWidget(self.files_group)
        group_buttons = QHBoxLayout()
        self.files_create_group = QPushButton(tr('Create group...'))
        self.files_create_group.clicked.connect(self._create_group)
        self.files_manage_groups = QPushButton(tr('Manage groups...'))
        self.files_manage_groups.clicked.connect(self._manage_groups)
        group_buttons.addWidget(self.files_create_group)
        group_buttons.addWidget(self.files_manage_groups)
        group_buttons.addStretch()
        layout.addLayout(group_buttons)
        self.files_hosts = CheckList()
        self.files_hosts.setMaximumHeight(200)
        self.files_hosts.itemChanged.connect(self._files_host_changed)
        layout.addWidget(self.files_hosts)
        selection_buttons = QHBoxLayout()
        self.files_select_all = QPushButton(tr('Select all'))
        self.files_select_all.clicked.connect(self._select_file_hosts)
        self.files_clear_all = QPushButton(tr('Clear selection'))
        self.files_clear_all.clicked.connect(self._clear_hosts)
        selection_buttons.addWidget(self.files_select_all)
        selection_buttons.addWidget(self.files_clear_all)
        selection_buttons.addStretch()
        layout.addLayout(selection_buttons)
        self.files_selection = QLabel()
        self.files_selection.setWordWrap(True)
        layout.addWidget(self.files_selection)
        form = QFormLayout()
        self.direction = QComboBox()
        self.direction.addItem(tr('Upload to hosts'), "upload")
        self.direction.addItem(tr('Download from hosts'), "download")
        self.local_path, self.remote_path = QLineEdit(), QLineEdit()
        self.remote_path.setPlaceholderText("/tmp/example.txt")
        self.direction.currentIndexChanged.connect(self._transfer_preview)
        self.local_path.textChanged.connect(self._transfer_preview)
        self.remote_path.textChanged.connect(self._transfer_preview)
        local_row = QHBoxLayout()
        local_row.addWidget(self.local_path)
        browse = QPushButton(tr('Browse...'))
        browse.clicked.connect(self._browse_transfer)
        local_row.addWidget(browse)
        form.addRow(tr('Direction'), self.direction)
        self.local_caption = QLabel(tr('File on this computer'))
        form.addRow(self.local_caption, local_row)
        form.addRow(tr('Full file path on remote hosts'), self.remote_path)
        layout.addLayout(form)
        self.transfer_routes = QPlainTextEdit()
        self.transfer_routes.setReadOnly(True)
        self.transfer_routes.setMaximumHeight(160)
        layout.addWidget(QLabel(tr('Transfer route for each selected host')))
        layout.addWidget(self.transfer_routes)
        self.transfer_button = QPushButton(tr('Review and transfer'))
        self.transfer_button.clicked.connect(self._run_transfer)
        layout.addWidget(self.transfer_button)
        layout.addStretch()
        self.tabs.addTab(page, tr('Files'))

    def _history_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        actions = QHBoxLayout()
        for title, action in ((tr('Refresh'), self._refresh_history),
                              (tr('Clean up to limits'), lambda: self._cleanup(True)),
                              (tr('Export full log...'), lambda: self._export(self._history_id()))):
            button = QPushButton(title)
            button.clicked.connect(action)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels([tr('Time'), tr('Status'), tr('Job'), "ID"])
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.history_table.itemSelectionChanged.connect(self._show_history)
        layout.addWidget(self.history_table)
        self.history_details = QPlainTextEdit()
        self.history_details.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.history_details.setReadOnly(True)
        layout.addWidget(self.history_details)
        more = QPushButton(tr('Read another 64 KiB of log'))
        more.clicked.connect(self._history_more)
        layout.addWidget(more)
        self.history_cursor = 0
        self.tabs.addTab(page, tr('History'))

    def _background(self, function, done, error):
        task = Work(function)
        self.tasks.add(task)
        def completed(result):
            self.tasks.discard(task)
            if not self.closing:
                done(result)
        def failed(message):
            self.tasks.discard(task)
            if not self.closing:
                error(message)
        task.signals.done.connect(completed)
        task.signals.error.connect(failed)
        QThreadPool.globalInstance().start(task)

    def _configuration_changed(self, bundle):
        self.config_notice.hide()
        self.settings_panel.load(bundle)
        e, log = bundle["settings"]["execution"], bundle["settings"]["logging"]
        self.source_timer.setInterval(bundle["settings"]["watch"]["poll_ms"])
        sudo_default = e.get("sudo_user", "root")
        if not self.sudo_target.text() or self.sudo_target.text() == getattr(self, "_applied_sudo_default", ""):
            self.sudo_target.setText(sudo_default)
        self._applied_sudo_default = sudo_default
        mode = tr('sequential') if e["mode"] == "sequential" else tr('parallel, up to {p0} hosts', p0=e['parallelism'])
        self.execution_label.setText(tr('Next run: {p0}; ', p0=mode) +
            (tr('stop on error') if e["stop_on_error"] else tr('continue after errors')) +
            tr('. Change in Settings > Execution.'))
        self.log_mode.setCurrentIndex(1 if log["verbose"] else 0)
        self.log_view.setMaximumBlockCount(log["gui_blocks"])
        self.history_details.setMaximumBlockCount(log["gui_blocks"])
        self.live_entries = TextBuffer(self.live_entries, maxlen=log["gui_blocks"])
        self.summary_entries = TextBuffer(self.summary_entries, maxlen=log["gui_blocks"])
        previous = self.scenario.currentData()
        previous_parameters = {name: field.text() for name, field in self.parameter_fields.items()}
        selected_id = previous.id if previous else None
        self.scenario.blockSignals(True)
        self.scenario.clear()
        self.scenario.addItem(tr('Custom command'), None)
        for scenario in bundle["scenarios"]:
            self.scenario.addItem(scenario.name, scenario)
            if scenario.id == selected_id:
                self.scenario.setCurrentIndex(self.scenario.count() - 1)
        self.scenario.blockSignals(False)
        self._scenario_changed()
        current = self.scenario.currentData()
        if previous and current and previous.id == current.id:
            for name, value in previous_parameters.items():
                if name in self.parameter_fields and previous.parameters.get(name) == current.parameters.get(name):
                    self.parameter_fields[name].setText(value)
        self._refresh_catalog()
        self._cleanup()
        self._refresh_history()

    def _runtime_error(self, message):
        self.config_notice.setText(message)
        self.config_notice.show()
        self.statusBar().showMessage(message)

    def _config_error(self, message):
        self.config_notice.setText(message)
        self.config_notice.show()
        self.statusBar().showMessage(tr('Configuration error: using the last valid configuration'))

    def _refresh_catalog(self):
        self.catalog_generation += 1
        generation = self.catalog_generation
        self.catalog_ready = False
        bundle = copy.deepcopy(self.store.bundle)
        profile = bundle["settings"]["profiles"][bundle["settings"]["active_profile"]]
        def load():
            effective = resolve_profile(profile)
            hosts, watched, patterns, warnings = catalog(effective, bundle["hosts"], bundle.get("groups"))
            return effective, hosts, fingerprint(watched, patterns), warnings
        def done(result):
            if generation != self.catalog_generation:
                return
            self.runtime_profile, self.hosts, self.ssh_fingerprint, _ = result
            self.catalog_ready = True
            selected = {h.alias for h in self._selected_hosts()}
            self.host_list.blockSignals(True)
            self.host_list.clear()
            for host in self.hosts:
                item = QListWidgetItem(host.alias + (" · " + ", ".join(host.groups) if host.groups else ""))
                item.setData(Qt.ItemDataRole.UserRole, host)
                item.setToolTip(f"{host.address or tr('address from OpenSSH')} | {host.os} | {host.shell}\n" +
                                ", ".join(host.tags))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if host.alias in selected else Qt.CheckState.Unchecked)
                self.host_list.addItem(item)
            self.host_list.blockSignals(False)
            current = self.group.currentData()
            self.group.blockSignals(True)
            self.group.clear()
            self.group.addItem(tr('All groups'), "")
            for name in sorted({g for h in self.hosts for g in h.groups} | set(bundle.get("groups", {}))):
                self.group.addItem(name, name)
            self.group.setCurrentIndex(max(0, self.group.findData(current)))
            self.group.blockSignals(False)
            self.files_hosts.blockSignals(True)
            self.files_hosts.clear()
            for host in self.hosts:
                item = QListWidgetItem(host.alias + (" · " + host.address if host.address else tr(' - address from SSH config')))
                item.setData(Qt.ItemDataRole.UserRole, host)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
                self.files_hosts.addItem(item)
            self.files_hosts.blockSignals(False)
            self.files_profile.setText(tr('Connection profile: ') + bundle["settings"]["active_profile"])
            pending = getattr(self, "_choose_group_after_reload", None)
            if pending is not None:
                self._choose_group_after_reload = None
                self.group.blockSignals(True)
                self.group.setCurrentIndex(max(0, self.group.findData(pending)))
                self.group.blockSignals(False)
                self._group_changed()
            else:
                self._filter_hosts()
            self.statusBar().showMessage(tr('Hosts: {p0}. Profile: ', p0=len(self.hosts)) +
                                         bundle["settings"]["active_profile"])
        def failed(message):
            if generation == self.catalog_generation:
                self.runtime_profile = None
                self.config_notice.setText(tr('Cannot load host list: ') + message)
                self.config_notice.show()
                self.statusBar().showMessage(message)
        self._background(load, done, failed)

    def _poll_sources(self):
        if self.tasks or (self.runner and not self.runner.complete):
            return
        if not self.catalog_ready or not self.runtime_profile:
            self._refresh_catalog()
            return
        try:
            _, watched, patterns, _ = catalog(self.runtime_profile, self.store.bundle["hosts"], self.store.bundle.get("groups"))
            if fingerprint(watched, patterns) != self.ssh_fingerprint:
                self._refresh_catalog()
        except (ValueError, OSError) as exc:
            self.catalog_ready = False
            self.config_notice.setText(tr('SSH configuration error: ') + str(exc))
            self.config_notice.show()

    def _filter_hosts(self, *_):
        query, group = self.search.text().lower(), self.group.currentData()
        for index in range(self.host_list.count()):
            item = self.host_list.item(index)
            host = item.data(Qt.ItemDataRole.UserRole)
            searchable = " ".join((host.alias, host.address, *host.groups, *host.tags)).lower()
            hidden = bool((query and query not in searchable) or (group and group not in host.groups))
            item.setHidden(hidden)
        self._selection_changed()

    def _selected_hosts(self):
        return [self.host_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.host_list.count())
                if self.host_list.item(i).checkState() == Qt.CheckState.Checked]

    def _select_visible(self):
        self.host_list.blockSignals(True)
        for i in range(self.host_list.count()):
            item = self.host_list.item(i)
            if not item.isHidden():
                item.setCheckState(Qt.CheckState.Checked)
        self.host_list.blockSignals(False)
        self._selection_changed()

    def _clear_hosts(self):
        self.host_list.blockSignals(True)
        for i in range(self.host_list.count()):
            self.host_list.item(i).setCheckState(Qt.CheckState.Unchecked)
        self.host_list.blockSignals(False)
        self._selection_changed()

    def _group_changed(self, *_):
        self.search.clear()
        group = self.group.currentData()
        self.host_list.blockSignals(True)
        for i in range(self.host_list.count()):
            item = self.host_list.item(i)
            host = item.data(Qt.ItemDataRole.UserRole)
            item.setCheckState(Qt.CheckState.Checked if group and group in host.groups else Qt.CheckState.Unchecked)
        self.host_list.blockSignals(False)
        self._filter_hosts()

    def _selection_changed(self, *_):
        hosts = self._selected_hosts()
        hidden = sum(self.host_list.item(i).isHidden() and
                     self.host_list.item(i).checkState() == Qt.CheckState.Checked for i in range(self.host_list.count()))
        self.selection_label.setText(tr('Checked: {p0}', p0=len(hosts)) + (tr(' (hidden by search: {p0})', p0=hidden) if hidden else ""))
        if not hasattr(self, "files_hosts"):
            return
        checked = {h.alias for h in hosts}
        self.files_hosts.blockSignals(True)
        for i in range(self.files_hosts.count()):
            item = self.files_hosts.item(i)
            group = self.group.currentData()
            item.setHidden(bool(group and group not in item.data(Qt.ItemDataRole.UserRole).groups))
            item.setCheckState(Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole).alias in checked
                               else Qt.CheckState.Unchecked)
        self.files_hosts.blockSignals(False)
        self.files_group.blockSignals(True)
        self.files_group.clear()
        for i in range(self.group.count()):
            self.files_group.addItem(self.group.itemText(i), self.group.itemData(i))
        self.files_group.setCurrentIndex(self.group.currentIndex())
        self.files_group.blockSignals(False)
        self.files_selection.setText(tr('Selected hosts: ') + (", ".join(h.alias for h in hosts) or tr('none')))
        self._transfer_preview()

    def _files_host_changed(self, item):
        alias = item.data(Qt.ItemDataRole.UserRole).alias
        for i in range(self.host_list.count()):
            target = self.host_list.item(i)
            if target.data(Qt.ItemDataRole.UserRole).alias == alias:
                target.setCheckState(item.checkState())
                break

    def _select_file_hosts(self):
        visible = {self.files_hosts.item(i).data(Qt.ItemDataRole.UserRole).alias
                   for i in range(self.files_hosts.count()) if not self.files_hosts.item(i).isHidden()}
        self.host_list.blockSignals(True)
        for i in range(self.host_list.count()):
            item = self.host_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole).alias in visible:
                item.setCheckState(Qt.CheckState.Checked)
        self.host_list.blockSignals(False)
        self._selection_changed()

    def _files_group_changed(self, *_):
        self.group.setCurrentIndex(max(0, self.group.findData(self.files_group.currentData())))

    def _transfer_preview(self, *_):
        if not hasattr(self, "transfer_routes"):
            return
        from .models import download_destination
        upload = self.direction.currentData() == "upload"
        self.local_caption.setText(tr('File on this computer') if upload else tr('Directory on this computer'))
        local = str(Path(self.local_path.text()).resolve()) if self.local_path.text() else tr('<no local path selected>')
        remote = self.remote_path.text() or tr('<no remote file specified>')
        lines = []
        for host in self._selected_hosts():
            if upload:
                lines.append(tr('This computer: {p0} -> {p1}: {p2}', p0=local, p1=host.alias, p2=remote))
            else:
                destination = download_destination(local, host.alias, remote)
                lines.append(tr('{p0}: {p1} -> this computer: {p2}', p0=host.alias, p1=remote, p2=destination))
        self.transfer_routes.setPlainText("\n".join(lines) if lines else tr('Select hosts for the transfer.'))

    def _create_group(self, *_):
        self._manage_groups(create=True)

    def _manage_groups(self, create=False):
        try:
            original = (self.store.root / "inventory.yaml").read_text(encoding="utf-8-sig")
            dialog = GroupDialog(read_yaml(self.store.root / "inventory.yaml"), self.hosts, self)
            if not create and self.group.currentData():
                dialog._reload(self.group.currentData())
            if create:
                if not dialog.create_group():
                    dialog.deleteLater()
                    return
                checked = {host.alias for host in self._selected_hosts()}
                for i in range(dialog.members.count()):
                    item = dialog.members.item(i)
                    item.setCheckState(Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) in checked
                                       else Qt.CheckState.Unchecked)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            if (self.store.root / "inventory.yaml").read_text(encoding="utf-8-sig") != original:
                raise ValueError(tr('Inventory changed externally. Reopen the group editor.'))
            self._choose_group_after_reload = dialog.active_group or ""
            self.store.save("inventory.yaml", dialog.document())
        except (ValueError, OSError) as exc:
            self._choose_group_after_reload = None
            QMessageBox.warning(self, tr('Groups'), str(exc))

    def _scenario_changed(self):
        while self.parameters_layout.count():
            item = self.parameters_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.parameter_fields = {}
        scenario = self.scenario.currentData()
        self.privilege.setEnabled(scenario is None)
        self.sudo_target.setEnabled(scenario is None)
        self.sudo_target.setVisible(scenario is None and bool(self.privilege.currentData()))
        self.command.setReadOnly(scenario is not None)
        self.scenario_description.setText(scenario.description if scenario else "")
        if scenario:
            for name, spec in scenario.parameters.items():
                field = QLineEdit(str(spec.get("default", "")))
                if spec.get("secret"):
                    field.setEchoMode(QLineEdit.EchoMode.Password)
                self.parameter_fields[name] = field
                self.parameters_layout.addRow(str(spec.get("label", name)), field)
            self.command.setPlainText("\n\n".join(
                f"# {s.name}" + (f" [sudo → {s.sudo_user}]" if s.sudo else "") + "\n" + s.command for s in scenario.steps))
        elif getattr(self, "_was_scenario", False):
            self.command.clear()
        self._was_scenario = scenario is not None

    def _run_command(self):
        try:
            scenario = self.scenario.currentData()
            requirements, secrets = {}, ()
            if scenario:
                steps, secrets = scenario.render({k: v.text() for k, v in self.parameter_fields.items()})
                requirements = {"os": list(scenario.os), "shells": list(scenario.shells)}
            else:
                command = self.command.toPlainText().strip()
                if not command or "\x00" in command:
                    raise ValueError(tr('Enter a command without NUL characters.'))
                target = validate_sudo_user(self.sudo_target.text().strip()) if self.privilege.currentData() else "root"
                steps = (Step(tr('Command'), command, bool(self.privilege.currentData()), sudo_user=target),)
            self._prepare(steps, requirements, secrets=secrets)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            QMessageBox.warning(self, tr('Run command'), str(exc))

    def _browse_transfer(self):
        if self.direction.currentData() == "upload":
            value, _ = QFileDialog.getOpenFileName(self, tr('File to upload'))
        else:
            value = QFileDialog.getExistingDirectory(self, tr('Download directory'))
        if value:
            self.local_path.setText(value)

    def _run_transfer(self):
        try:
            remote, local = self.remote_path.text().strip(), self.local_path.text().strip()
            if not remote or remote.endswith("/") or any(c in remote for c in "\r\n\x00") or not local:
                raise ValueError(tr('Enter a local path and a remote path including the filename.'))
            if self.direction.currentData() == "upload" and not Path(local).is_file():
                raise ValueError(tr('Local file not found.'))
            if self.direction.currentData() == "download" and not Path(local).is_dir():
                raise ValueError(tr('Select an existing download directory.'))
            import posixpath
            if remote.startswith("~"):
                raise ValueError(tr('Use an absolute remote path or a relative path without ~.'))
            remote = posixpath.normpath(remote)
            if remote in ("/", ".", "..") or posixpath.basename(remote) in ("", ".", ".."):
                raise ValueError(tr('The remote path must include a regular filename.'))
            remote = "/" + remote.lstrip("/") if remote.startswith("/") else "./" + remote
            self._prepare((), {}, transfer=Transfer(self.direction.currentData(), str(Path(local).resolve()), remote))
        except (ValueError, OSError, TypeError, KeyError) as exc:
            QMessageBox.warning(self, tr('File transfers'), str(exc))

    def _prepare(self, steps, requirements, transfer=None, secrets=()):
        if self.runner and not self.runner.complete:
            raise ValueError(tr('Wait for the current run to finish.'))
        if not self.catalog_ready:
            raise ValueError(tr('Host list is not ready. Check the connection profile.'))
        hosts = self._selected_hosts()
        if not hosts:
            raise ValueError(tr('Select at least one host.'))
        bundle = load_bundle(self.store.root)
        if bundle["settings"] != self.store.bundle["settings"] or bundle["hosts"] != self.store.bundle["hosts"] or bundle.get("groups") != self.store.bundle.get("groups"):
            raise ValueError(tr('Settings changed. Refresh the host list.'))
        profile_name = bundle["settings"]["active_profile"]
        bundle["settings"]["profiles"][profile_name] = copy.deepcopy(self.runtime_profile)
        bundle["requirements"] = requirements
        incompatible = [h.alias for h in hosts if h.shell not in requirements.get("shells", ["sh", "bash"]) or
                        (h.os != "unix" and "unix" not in requirements.get("os", ["unix"]) and
                         h.os not in requirements["os"])]
        if incompatible:
            raise ValueError(tr('Incompatible hosts: ') + ", ".join(incompatible))
        decision = assess(bundle["safety"], steps, transfer.remote if transfer and transfer.direction == "upload"
                          else None, transfer is not None)
        if decision.action == "deny":
            raise ValueError(tr('Run blocked:\n') + "\n".join(decision.reasons))
        redactor, e = Redactor(secrets), bundle["settings"]["execution"]
        description = (tr('Sequential') if e["mode"] == "sequential" else
                       tr('Parallel, at most {p0} hosts', p0=e['parallelism']))
        description += "\n" + (tr('Stop on error') if e["stop_on_error"] else tr('Continue after errors'))
        description += tr('\n\nHosts:\n') + "\n".join(
            f"• {h.alias} → {h.address or tr('address from SSH configuration')} ({h.os}, {h.shell})" for h in hosts)
        if transfer:
            description += tr('\n\n{p0}: {p1}\n<-> {p2}\nOverwriting an existing file will require separate approval.', p0=transfer.direction, p1=transfer.local, p2=transfer.remote)
        else:
            description += "\n\n" + "\n\n".join(
                f"{s.name}" + (tr(' [sudo -> {p0}; connected as the SSH user]', p0=s.sudo_user) if s.sudo else "") + "\n" + redactor.text(s.command)
                for s in steps)
        if decision.reasons:
            description += tr('\n\nReview required:\n') + "\n".join(decision.reasons)
        description += tr('\n\nText checks cannot guarantee script safety. There is no automatic rollback.')
        if not self._review(tr('Review run'), description, tr('Run')):
            return
        effective, watched, patterns, _ = catalog(self.runtime_profile, bundle["hosts"], bundle.get("groups"))
        by_alias = {h.alias: h for h in effective}
        if any(by_alias.get(h.alias) != h for h in hosts):
            raise ValueError(tr('Host list changed. Refresh the list.'))
        current = fingerprint(watched, patterns)
        if current != self.ssh_fingerprint:
            raise ValueError(tr('SSH files changed. Refresh and review again.'))
        job = Job.create(hosts, steps, bundle, current, transfer, secrets)
        runner = JobRunner(job, self.history, self.auth, parent=self)
        runner.event.connect(self._event)
        runner.result.connect(self._result)
        runner.finished.connect(self._finished)
        runner.paused.connect(self._paused)
        runner.fatal.connect(self._runtime_error)
        runner.overwrite_needed.connect(self._overwrite)
        if self.runner:
            self.runner.deleteLater()
        self.runner = runner
        self.results.setRowCount(0)
        self.live_entries.clear()
        self.summary_entries.clear()
        self.output_queue.clear()
        self.log_view.clear()
        self.run_button.setEnabled(False)
        self.transfer_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.tabs.setCurrentIndex(0)
        try:
            runner.start()
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.run_button.setEnabled(True)
            self.transfer_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            raise ValueError(tr('Cannot create the log; no commands were started: ') + str(exc)) from exc
        self.statusBar().showMessage(tr('Running job ') + job.id[:8])

    def _review(self, title, text, button_text):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(780, 620)
        layout = QVBoxLayout(dialog)
        view = QPlainTextEdit(text)
        view.setReadOnly(True)
        layout.addWidget(view)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.addButton(button_text, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _event(self, host, step, channel, text):
        if channel == "system" and tr('not saved') in text:
            self.config_notice.setText(text)
            self.config_notice.show()
        visible = (tr('[GUI: beginning of entry omitted] ') if len(text) > 2048 else "") + text[-2048:]
        lines = visible.rsplit("\n", 20)
        if len(lines) > 20:
            visible = tr('[GUI: beginning of entry omitted]\n') + "\n".join(lines[-20:])
        entry = f"[{time.strftime('%H:%M:%S')}] [{host}] [{step}] [{channel}] {visible}"
        self.live_entries.append(entry)
        if channel in ("stdout", "stderr"):
            brief = f"[{host}] [{step}] {visible}"
            self.summary_entries.append(brief)
            if self.log_mode.currentIndex() == 0:
                self.output_queue.append(brief)
        if self.log_mode.currentIndex() == 1:
            self.output_queue.append(entry)

    def _result(self, host, step, status, duration, code, reason):
        row = self.results.rowCount()
        self.results.insertRow(row)
        for column, value in enumerate((host, step, tr(STATUS.get(status, status)), f"{duration:.2f}",
                                        "" if code is None else str(code), reason)):
            self.results.setItem(row, column, QTableWidgetItem(value))
        self.results.scrollToBottom()
        summary = tr('[{p0}] {p1}: {p2} - {p3:.2f} s', p0=host, p1=step, p2=tr(STATUS.get(status, status)), p3=duration) + \
                  (tr(' - code {p0}', p0=code) if code is not None else "") + (f" · {reason}" if reason else "")
        self.summary_entries.append(summary)
        if self.log_mode.currentIndex() == 0:
            self.output_queue.append(summary)

    def _flush_output(self):
        try:
            report = self.history.poll_cleanup()
            if report and report["over_budget"]:
                self.config_notice.setText(tr('History uses {p0} bytes: its minimum size exceeds the budget. Increase the limit.', p0=report['bytes']))
                self.config_notice.show()
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.config_notice.setText(tr('History maintenance error: ') + str(exc))
            self.config_notice.show()
        if self.output_queue:
            evicted = self.live_entries.dropped + self.summary_entries.dropped
            if evicted:
                self.statusBar().showMessage(tr('GUI: {p0} entries evicted from buffers; check the saved log', p0=evicted))
            if self.output_queue.dropped:
                self.log_view.appendPlainText(tr('[GUI: {p0} queued entries omitted; check the saved log]', p0=self.output_queue.dropped))
            self.log_view.appendPlainText("\n".join(self.output_queue))
            self.output_queue.clear()
            self._trim_document(self.log_view)

    @staticmethod
    def _trim_document(widget):
        # Block count alone does not constrain a long line or Qt's glyph layout.
        widget.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        size = widget.document().characterCount()
        if size > 262144:
            cursor = widget.textCursor()
            cursor.setPosition(0)
            cursor.setPosition(size - 196608, cursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
            cursor.insertText(tr('[GUI: beginning of visible buffer omitted; full output is in the log]\n'))

    def _render_live_log(self, *_):
        self.output_queue.clear()
        entries = self.live_entries if self.log_mode.currentIndex() else self.summary_entries
        note = tr('[GUI: {p0} entries evicted from the buffer]\n', p0=entries.dropped) if entries.dropped else ""
        text = "\n".join(entries)
        if len(text) > 98304:
            text = tr('[GUI: beginning of visible buffer omitted; full output is in the log]\n') + text[-98304:]
        self.log_view.setPlainText(note + text)
        self._trim_document(self.log_view)
        cursor = self.log_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.log_view.setTextCursor(cursor)

    def _stop(self):
        if self.runner and not self.runner.complete:
            self.runner.cancel()

    def _paused(self, reason):
        self.resume_button.show()
        self.config_notice.setText(reason)
        self.config_notice.show()
        self.statusBar().showMessage(tr('Run paused: SSH files changed'))

    def _resume(self):
        if not self.runner or self.runner.complete:
            return
        try:
            current = self.runner.sources()
            old = {row[0]: row[1:] for row in self.runner.baseline}
            changed = [row[0] for row in current if old.get(row[0]) != row[1:]]
            changed += [path for path in old if path not in {row[0] for row in current}]
            text = tr('SSH files changed:\n') + "\n".join(changed) + \
                   tr('\n\nRemaining connections will use the updated SSH settings.\nHosts:\n') + \
                   "\n".join(h.alias for h in self.runner.job.hosts)
            if self._review(tr('Review SSH changes'), text, tr('Continue')):
                self.runner.resume()
                self.resume_button.hide()
                self.config_notice.hide()
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, tr('SSH check'), str(exc))

    def _overwrite(self, worker, destination):
        if worker.done:
            return
        accepted = QMessageBox.question(self, tr('Overwrite file'),
            tr('Host: {p0}\nFile already exists:\n{p1}\n\nReplace it?', p0=worker.host.alias, p1=destination),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
        worker.confirm_overwrite(accepted)

    def _queue_auth(self, request):
        self.auth_queue.append(request)
        self._next_auth()

    def _next_auth(self):
        if self.auth_dialog:
            return
        while self.auth_queue:
            request = self.auth_queue.popleft()
            if request["id"] not in self.auth.pending:
                continue
            if request["kind"] == "confirm":
                dialog = QMessageBox(self)
                dialog.setIcon(QMessageBox.Icon.Warning)
                dialog.setWindowTitle(tr('Verify SSH host key'))
                dialog.setText(tr('Host: {p0}\n\n{p1}\n\nVerify the fingerprint through a trusted source.', p0=request['host'], p1=request['prompt']))
                dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                dialog.setDefaultButton(QMessageBox.StandardButton.No)
                def extract(result):
                    return "yes" if result == QMessageBox.StandardButton.Yes else None
            else:
                dialog = QInputDialog(self)
                dialog.setWindowTitle("SSH / sudo - " + request["host"])
                dialog.setLabelText(request["prompt"] + tr('\nThe password is not saved.'))
                dialog.setTextEchoMode(QLineEdit.EchoMode.Password)
                dialog.setInputMode(QInputDialog.InputMode.TextInput)
                def extract(result, d=dialog):
                    return d.textValue() if result == QDialog.DialogCode.Accepted else None
            self.auth_dialog = dialog
            def answered(result, r=request, d=dialog, value=extract):
                self.auth.answer(r["id"], value(result))
                self.auth_dialog = None
                d.deleteLater()
                QTimer.singleShot(0, self._next_auth)
            dialog.finished.connect(answered)
            dialog.open()
            timer = QTimer(dialog)
            timer.setInterval(250)
            timer.timeout.connect(lambda r=request, d=dialog: d.reject() if r["id"] not in self.auth.pending else None)
            timer.start()
            break

    def _finished(self, status):
        if self.closing_requested:
            QTimer.singleShot(0, self.close)
            return
        scenario = self.scenario.currentData()
        if scenario:
            for name, field in self.parameter_fields.items():
                if scenario.parameters[name].get("secret"):
                    field.clear()
        self.run_button.setEnabled(True)
        self.transfer_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.resume_button.hide()
        self.statusBar().showMessage(tr('Run completed: ') + tr(STATUS.get(status, status)))
        self._refresh_history()
        self._refresh_catalog()

    def _refresh_history(self):
        if not hasattr(self, "history_table"):
            return
        self.history_table.setRowCount(0)
        for run_id, started, status, summary in self.history.runs():
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)
            for col, value in enumerate((time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
                                         tr(STATUS.get(status, status)), summary, run_id)):
                self.history_table.setItem(row, col, QTableWidgetItem(value))

    def _history_id(self):
        row = self.history_table.currentRow()
        item = self.history_table.item(row, 3) if row >= 0 else None
        return item.text() if item else None

    def _show_history(self):
        run_id = self._history_id()
        self.history_cursor = 0
        self.history_details.clear()
        if run_id:
            for host, step, status, duration, code, reason in self.history.results(run_id):
                self.history_details.appendPlainText(tr('{p0} - {p1} - {p2} - {p3:.2f} s - code {p4} - {p5}', p0=host, p1=step, p2=tr(STATUS.get(status, status)), p3=duration, p4=code, p5=reason))
            self._history_more()

    def _history_more(self):
        run_id = self._history_id()
        if not run_id or not self.history.log_path(run_id).exists():
            return
        with self.history.log_path(run_id).open("rb") as source:
            source.seek(self.history_cursor)
            chunk = source.read(65536)
            self.history_cursor = source.tell()
        self.history_details.appendPlainText(chunk.decode("utf-8", "replace"))
        self._trim_document(self.history_details)

    def _export(self, run_id):
        if not run_id:
            return
        path, _ = QFileDialog.getSaveFileName(self, tr('Export log'), f"nodebatch-{run_id[:8]}.jsonl",
                                             "JSON Lines (*.jsonl)")
        if path:
            import shutil
            try:
                shutil.copyfile(self.history.log_path(run_id), path)
            except OSError as exc:
                QMessageBox.warning(self, tr('Export'), str(exc))

    def _cleanup(self, force=False):
        if not self.runner or self.runner.complete:
            log = self.store.bundle["settings"]["logging"]
            self.history.configure(log)
            self.history.schedule_cleanup(log["retention_days"], log.get("history_max_bytes", log["max_bytes"]),
                                          force=force)

    def run_diagnostics(self):
        profile = copy.deepcopy(self.store.bundle["settings"]["profiles"][
            self.store.bundle["settings"]["active_profile"]])
        self._background(lambda: diagnostics(profile),
            lambda result: QMessageBox.information(self, tr('OpenSSH diagnostics'), result[1]),
            lambda error: QMessageBox.warning(self, tr('OpenSSH diagnostics'), error))

    def closeEvent(self, event):
        if self.runner and not self.runner.complete:
            if not self.closing_requested:
                answer = QMessageBox.question(self, tr('Close NodeBatch'),
                    tr('Cancel the run and close the application? The remote command may have continued.'),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if answer != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
                self.closing_requested = True
                self.runner.cancel()
            event.ignore()
            return
        self.closing = True
        self.flush_timer.stop()
        self.cleanup_timer.stop()
        self.source_timer.stop()
        self.settings_panel.discovery_timer.stop()
        self.auth.close()
        self.history.close()
        event.accept()
