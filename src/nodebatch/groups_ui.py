"""GUI group editor. Only the app inventory changes; SSH configs stay authoritative."""
from .i18n import tr
import copy
import yaml
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget,
                              QListWidgetItem, QPushButton, QInputDialog, QLineEdit,
                              QMessageBox, QDialogButtonBox)
from .widgets import CheckList


class GroupDialog(QDialog):
    def __init__(self, inventory, hosts, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr('Host groups'))
        self.resize(760, 560)
        self.inventory = copy.deepcopy(inventory)
        self.groups = {name: set(members) for name, members in inventory.get("groups", {}).items()}
        for row in inventory["hosts"]:
            for group in row.get("groups", []):
                self.groups.setdefault(group, set()).add(row["alias"])
        self.available = {host.alias: host for host in hosts}
        self.active_group = None
        self.loading = False
        layout = QVBoxLayout(self)
        note = QLabel(tr("1. Create a group. 2. Select hosts on the right. 3. Click Save. SSH config and inventory aliases are available. Groups are saved in the application's inventory.yaml."))
        note.setWordWrap(True)
        layout.addWidget(note)
        body = QHBoxLayout()
        left = QVBoxLayout()
        self.group_list = QListWidget()
        self.group_list.currentTextChanged.connect(self._switch)
        left.addWidget(self.group_list)
        for title, callback in ((tr('Create group'), self.create_group), (tr('Rename'), self.rename_group),
                                (tr('Delete group'), self.delete_group)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            left.addWidget(button)
        body.addLayout(left, 1)
        right = QVBoxLayout()
        right.addWidget(QLabel(tr('Hosts from SSH config and inventory')))
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr('Find a host'))
        self.search.textChanged.connect(self._filter)
        right.addWidget(self.search)
        self.members = CheckList()
        self.members.itemChanged.connect(self._member_changed)
        right.addWidget(self.members)
        self.count = QLabel()
        right.addWidget(self.count)
        body.addLayout(right, 2)
        layout.addLayout(body)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(tr('Save'))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr('Cancel'))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._reload()

    def _reload(self, select=None):
        self.group_list.blockSignals(True)
        self.group_list.clear()
        self.group_list.addItems(sorted(self.groups))
        self.group_list.blockSignals(False)
        names = sorted(self.groups)
        if names:
            self.group_list.setCurrentRow(names.index(select) if select in names else 0)
        else:
            self._switch("")

    def _switch(self, name):
        self.active_group = name or None
        self.loading = True
        self.members.clear()
        selected = self.groups.get(name, set())
        # Preserve unavailable members when editing from another SSH profile.
        for alias in sorted(set(self.available) | selected):
            item = QListWidgetItem(alias + ("" if alias in self.available else tr(' (not in the current profile)')))
            item.setData(Qt.ItemDataRole.UserRole, alias)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if alias in selected else Qt.CheckState.Unchecked)
            self.members.addItem(item)
        self.members.setEnabled(bool(name))
        self.loading = False
        self._filter()
        self._count()

    def _filter(self, *_):
        query = self.search.text().lower()
        for i in range(self.members.count()):
            item = self.members.item(i)
            item.setHidden(query not in item.text().lower())

    def _member_changed(self, item):
        if self.loading or not self.active_group:
            return
        alias = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.groups[self.active_group].add(alias)
        else:
            self.groups[self.active_group].discard(alias)
        self._count()

    def _count(self):
        self.count.setText(tr('In group: {p0}', p0=len(self.groups.get(self.active_group, set()))))

    def _name(self, title, current=""):
        name, ok = QInputDialog.getText(self, title, tr('Group name:'), text=current)
        name = name.strip()
        if not ok:
            return None
        if not name or any(c in name for c in "\r\n\x00") or (name != current and name in self.groups):
            QMessageBox.warning(self, title, tr('Enter a nonempty, unique group name.'))
            return None
        return name

    def create_group(self):
        name = self._name(tr('New group'))
        if name is not None:
            self.groups[name] = set()
            self._reload(name)
            return name
        return None

    def rename_group(self):
        if self.active_group:
            old = self.active_group
            name = self._name(tr('Rename group'), old)
            if name is not None and name != old:
                self.groups[name] = self.groups.pop(old)
                self._reload(name)

    def delete_group(self):
        if not self.active_group:
            return
        if QMessageBox.question(self, tr('Delete group'), tr('Delete group "{p0}"?\nHosts will remain in SSH config and inventory.', p0=self.active_group), QMessageBox.StandardButton.Yes |
             QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            del self.groups[self.active_group]
            self._reload()

    def document(self):
        result = copy.deepcopy(self.inventory)
        # Normalize legacy per-host memberships without changing connection fields.
        for row in result["hosts"]:
            row.pop("groups", None)
        result["groups"] = {name: sorted(members) for name, members in sorted(self.groups.items())}
        return yaml.safe_dump(result, allow_unicode=True, sort_keys=False)
