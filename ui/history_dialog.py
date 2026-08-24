"""HistoryDialog — browse past jobs with quick folder access."""

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from core import history


class HistoryDialog(QDialog):
    """Lists recent jobs; double-click opens the output folder."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Job History")
        self.resize(640, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("source-list")
        self.list_widget.itemDoubleClicked.connect(self._open_selected)
        layout.addWidget(self.list_widget, stretch=1)

        entries = history.load_entries()
        if not entries:
            empty = QLabel("No jobs yet.")
            empty.setObjectName("hint-label")
            layout.addWidget(empty)

        for entry in entries:
            status = "OK" if entry.get("ok") else "FAILED"
            summary = entry.get("summary") or Path(entry.get("output", "")).name
            text = f"[{entry.get('ts', '?')}] {entry.get('mode', '?')}  \u00b7  {summary}  \u00b7  {status}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, entry.get("output", ""))
            if not entry.get("ok"):
                item.setForeground(Qt.GlobalColor.red)
            self.list_widget.addItem(item)

        buttons = QHBoxLayout()
        open_button = QPushButton("Open Folder")
        open_button.setObjectName("secondary-button")
        open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        open_button.clicked.connect(self._open_selected)

        clear_button = QPushButton("Clear History")
        clear_button.setObjectName("danger-button")
        clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_button.clicked.connect(self._clear_history)

        close_button = QPushButton("Close")
        close_button.setObjectName("primary-button")
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.clicked.connect(self.accept)

        buttons.addWidget(open_button)
        buttons.addWidget(clear_button)
        buttons.addStretch()
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    # ------------------------------------------------------------------
    def _open_selected(self, *_args) -> None:
        item = self.list_widget.currentItem()
        if item is None:
            return
        output = item.data(Qt.ItemDataRole.UserRole)
        if not output:
            return
        folder = str(Path(output).parent)
        try:
            os.startfile(folder)
        except OSError:
            pass

    def _clear_history(self) -> None:
        reply = QMessageBox.question(
            self, "Clear History",
            "Delete all saved job history entries?",
        )
        if reply == QMessageBox.StandardButton.Yes:
            history.clear()
            self.list_widget.clear()
