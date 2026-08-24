"""
ClipPreviewDialog — review planned clips before rendering.

Shows a checkable thumbnail list of every planned clip. The user can
exclude clips or re-roll them individually / globally, then confirm to
render only the included ones.
"""

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.clip_plan import ClipSession, reroll_clip


class ClipPreviewDialog(QDialog):
    """Modal review surface for a :class:`ClipSession`."""

    def __init__(
        self,
        parent: QWidget | None,
        session: ClipSession,
        engine,
        log_fn=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review Planned Clips")
        self.resize(860, 560)
        self.session = session
        self.engine = engine
        self._log = log_fn or (lambda msg: None)
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        hint = QLabel(
            "Uncheck clips you do not want. Re-roll replaces a clip with "
            "another random one from the same video."
        )
        hint.setObjectName("hint-label")
        layout.addWidget(hint)

        body = QHBoxLayout()
        body.setSpacing(14)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("preview-list")
        self.list_widget.setIconSize(
            self._thumb_icon_size(session.portrait)
        )
        self.list_widget.setUniformItemSizes(False)
        body.addWidget(self.list_widget, stretch=3)

        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(10)

        self.detail_label = QLabel("Select a clip to see details.")
        self.detail_label.setObjectName("hint-label")
        self.detail_label.setWordWrap(True)
        detail_layout.addWidget(self.detail_label)
        detail_layout.addStretch()

        reroll_button = QPushButton("Re-roll Selected")
        reroll_button.setObjectName("secondary-button")
        reroll_button.setCursor(Qt.CursorShape.PointingHandCursor)
        reroll_button.clicked.connect(self._reroll_selected)

        reroll_all_button = QPushButton("Re-roll All")
        reroll_all_button.setObjectName("secondary-button")
        reroll_all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        reroll_all_button.clicked.connect(self._reroll_all)

        detail_layout.addWidget(reroll_button)
        detail_layout.addWidget(reroll_all_button)
        body.addWidget(detail_panel, stretch=1)

        layout.addLayout(body, stretch=1)

        footer = QHBoxLayout()
        self.total_label = QLabel("")
        self.total_label.setObjectName("field-label")
        footer.addWidget(self.total_label)
        footer.addStretch()

        cancel_button = QPushButton("Cancel")
        cancel_button.setObjectName("secondary-button")
        cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_button.clicked.connect(self.reject)

        self.compile_button = QPushButton("Compile Included Clips")
        self.compile_button.setObjectName("primary-button")
        self.compile_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.compile_button.clicked.connect(self.accept)

        footer.addWidget(cancel_button)
        footer.addWidget(self.compile_button)
        layout.addLayout(footer)

        self._populate()
        self._refresh_totals()
        self.list_widget.currentRowChanged.connect(self._on_row_changed)
        self.list_widget.itemChanged.connect(self._on_item_changed)

    # ------------------------------------------------------------------
    @staticmethod
    def _thumb_icon_size(portrait: bool):
        if portrait:
            return QSize(96, 170)
        return QSize(160, 90)

    def _populate(self) -> None:
        self._updating = True
        try:
            for entry in self.session.clips:
                item = QListWidgetItem(self._entry_text(entry))
                icon_path = Path(entry.thumb_path)
                if icon_path.is_file():
                    item.setIcon(QIcon(str(icon_path)))
                else:
                    item.setText(f"(no preview) {item.text()}")
                item.setFlags(
                    item.flags() | Qt.ItemFlag.ItemIsUserCheckable
                )
                item.setCheckState(
                    Qt.CheckState.Unchecked
                    if entry.excluded
                    else Qt.CheckState.Checked
                )
                item.setData(Qt.ItemDataRole.UserRole, id(entry))
                self.list_widget.addItem(item)
        finally:
            self._updating = False

    def _entry_for(self, item: QListWidgetItem):
        token = item.data(Qt.ItemDataRole.UserRole)
        for entry in self.session.clips:
            if id(entry) == token:
                return entry
        return None

    @staticmethod
    def _entry_text(entry) -> str:
        name = Path(entry.video).name
        return (
            f"{name}\n"
            f"{entry.start:.1f}s \u2013 {entry.end:.1f}s ({entry.duration:.1f}s)"
        )

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_row_changed(self, row: int) -> None:
        if row < 0:
            self.detail_label.setText("Select a clip to see details.")
            return
        item = self.list_widget.item(row)
        entry = self._entry_for(item)
        if entry is None:
            return
        state = "EXCLUDED" if entry.excluded else "Included"
        self.detail_label.setText(
            f"<b>{Path(entry.video).name}</b><br>"
            f"Range: {entry.start:.2f}s \u2013 {entry.end:.2f}s<br>"
            f"Duration: {entry.duration:.1f}s<br>"
            f"Status: {state}"
        )

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        if self._updating:
            return
        entry = self._entry_for(item)
        if entry is None:
            return
        entry.excluded = item.checkState() != Qt.CheckState.Checked
        self._refresh_totals()

    def _reroll_selected(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0:
            return
        self._apply_reroll(row)

    def _reroll_all(self) -> None:
        for row in range(self.list_widget.count()):
            self._apply_reroll(row)

    def _apply_reroll(self, row: int) -> None:
        item = self.list_widget.item(row)
        entry = self._entry_for(item)
        if entry is None:
            return
        replaced = reroll_clip(self.session, row)
        if replaced is None:
            self._log(f"No alternative candidate available for clip #{row + 1}.")
            return

        mid = replaced.start + replaced.duration / 2
        ok = self.engine.generate_thumbnail(
            replaced.video, mid, replaced.thumb_path,
            portrait=self.session.portrait,
        )
        self._updating = True
        try:
            if ok and Path(replaced.thumb_path).is_file():
                item.setIcon(QIcon(replaced.thumb_path))
            item.setText(self._entry_text(replaced))
            item.setCheckState(Qt.CheckState.Checked)
        finally:
            self._updating = False
        self._refresh_totals()

    def _refresh_totals(self) -> None:
        active = len(self.session.included())
        total = self.session.included_total()
        target = self.session.target_total
        self.total_label.setText(
            f"Total: {total:.1f}s / target {target:.0f}s  \u00b7  "
            f"{active} active clip(s)"
        )
        self.compile_button.setEnabled(active > 0)
        self.compile_button.setText(f"Compile {active} Clips")

    # ------------------------------------------------------------------
    def included_clips(self) -> list:
        return self.session.included()
