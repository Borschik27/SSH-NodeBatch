from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QListWidget, QComboBox


class CheckList(QListWidget):
    """A click anywhere in the row toggles membership; modifiers are unnecessary."""
    def mousePressEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if item is not None and event.button() == Qt.MouseButton.LeftButton:
            self.setCurrentItem(item)
            item.setCheckState(Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked
                               else Qt.CheckState.Checked)
            event.accept()
        else:
            super().mousePressEvent(event)

    def keyPressEvent(self, event):
        item = self.currentItem()
        if event.key() == Qt.Key.Key_Space and item is not None:
            item.setCheckState(Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked
                               else Qt.CheckState.Checked)
            event.accept()
        else:
            super().keyPressEvent(event)


class EditableChoice(QComboBox):
    textEdited = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.lineEdit().textEdited.connect(self.textEdited.emit)
        self.textActivated.connect(self.textEdited.emit)

    def text(self):
        return self.currentText()

    def setText(self, text):
        self.setCurrentText(text)

    def suggestions(self, values):
        current = self.text()
        self.blockSignals(True)
        self.clear()
        self.addItems(list(dict.fromkeys(values)))
        self.setText(current)
        self.blockSignals(False)
