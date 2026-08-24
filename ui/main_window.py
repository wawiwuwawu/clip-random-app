import json
import os
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ui.widgets import (
    NoWheelComboBox,
    NoWheelDoubleSpinBox,
    NoWheelSpinBox,
)

from core.ffmpeg_engine import (
    AUTO_ENCODER_LABEL,
    CPU_ENCODER_DISPLAY,
    FFmpegEngine,
    GPU_DISPLAY_BY_KEY,
    compiler_output_name,
    make_timestamp,
    silence_output_name,
)
from core import history
from ui.clip_preview import ClipPreviewDialog
from ui.history_dialog import HistoryDialog

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a", ".wma"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
MEDIA_FILTER = (
    "All Media Files (*.mp4 *.mov *.avi *.mkv *.webm *.m4v *.wmv "
    "*.mp3 *.wav *.aac *.flac *.ogg *.m4a *.wma)"
)

ENCODER_CHOICES = [
    "NVIDIA NVENC (h264_nvenc)",
    "Intel QSV (h264_qsv)",
    "AMD AMF (h264_amf)",
    "CPU Software (libx264)",
    "NVIDIA NVENC HEVC (hevc_nvenc)",
    "CPU HEVC (libx265)",
]

ASPECT_LANDSCAPE = "Landscape (source)"
ASPECT_PORTRAIT = "Portrait 9:16 (Shorts/TikTok)"


class DropZone(QWidget):
    """Drag-and-drop target for a single video file."""

    file_dropped = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setObjectName("drop-zone")

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(8)
        layout.setContentsMargins(20, 20, 20, 20)

        title = QLabel("Drag and drop your media file here")
        title.setObjectName("drop-zone-title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        or_label = QLabel("or")
        or_label.setObjectName("drop-zone-or")
        or_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.select_button = QPushButton("Select File")
        self.select_button.setObjectName("secondary-button")
        self.select_button.setFixedWidth(110)
        self.select_button.setCursor(Qt.CursorShape.PointingHandCursor)

        layout.addWidget(title)
        layout.addWidget(or_label)
        layout.addWidget(self.select_button, alignment=Qt.AlignmentFlag.AlignCenter)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setProperty("dragging", True)
            self.style().unpolish(self)
            self.style().polish(self)

    def dragLeaveEvent(self, event) -> None:
        self.setProperty("dragging", False)
        self.style().unpolish(self)
        self.style().polish(self)

    def dropEvent(self, event) -> None:
        self.setProperty("dragging", False)
        self.style().unpolish(self)
        self.style().polish(self)

        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if Path(path).suffix.lower() in MEDIA_EXTENSIONS:
                self.file_dropped.emit(path)
                break


class FolderDropLineEdit(QLineEdit):
    """Read-only line edit that accepts drag-and-drop of a folder."""

    def __init__(self, on_folder_dropped=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.on_folder_dropped = on_folder_dropped
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if Path(path).is_dir():
                self.setText(path)
                if self.on_folder_dropped:
                    self.on_folder_dropped(path)
                break


class SourceList(QListWidget):
    """QListWidget that accepts dragged files and folders."""

    def __init__(self, owner: "SourceListWidget", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._owner = owner
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._owner.add_urls(event.mimeData().urls())


class SourceListWidget(QWidget):
    """Mixed video sources picker: individual files and/or whole folders."""

    sources_changed = Signal()

    KIND_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._known_paths: set[str] = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.list_widget = SourceList(self)
        self.list_widget.setObjectName("source-list")
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        layout.addWidget(self.list_widget)

        buttons_row = QWidget()
        buttons_layout = QHBoxLayout(buttons_row)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(8)

        self.add_files_button = QPushButton("Add Files...")
        self.add_files_button.setObjectName("secondary-button")
        self.add_files_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_files_button.clicked.connect(self._browse_files)

        self.add_folder_button = QPushButton("Add Folder...")
        self.add_folder_button.setObjectName("secondary-button")
        self.add_folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_folder_button.clicked.connect(self._browse_folder)

        self.remove_button = QPushButton("Remove Selected")
        self.remove_button.setObjectName("secondary-button")
        self.remove_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_button.clicked.connect(self._remove_selected)

        self.clear_button = QPushButton("Clear All")
        self.clear_button.setObjectName("secondary-button")
        self.clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_button.clicked.connect(self.clear)

        buttons_layout.addWidget(self.add_files_button)
        buttons_layout.addWidget(self.add_folder_button)
        buttons_layout.addStretch()
        buttons_layout.addWidget(self.remove_button)
        buttons_layout.addWidget(self.clear_button)
        layout.addWidget(buttons_row)

    # ------------------------------------------------------------------
    # Adding entries
    # ------------------------------------------------------------------

    def add_urls(self, urls) -> int:
        added = 0
        for url in urls:
            path = url.toLocalFile()
            if not path:
                continue
            if Path(path).is_dir():
                added += self._append_entry("dir", path)
            elif Path(path).suffix.lower() in VIDEO_EXTENSIONS:
                added += self._append_entry("file", path)
        if added:
            self.sources_changed.emit()
        return added

    def add_paths(self, paths: list[str]) -> int:
        added = 0
        for path in paths:
            if Path(path).is_dir():
                added += self._append_entry("dir", path)
            elif Path(path).suffix.lower() in VIDEO_EXTENSIONS:
                added += self._append_entry("file", path)
        if added:
            self.sources_changed.emit()
        return added

    def _append_entry(self, kind: str, path: str) -> int:
        normalized = os.path.abspath(path).lower()
        if normalized in self._known_paths:
            return 0
        self._known_paths.add(normalized)

        item = QListWidgetItem(path)
        item.setData(self.KIND_ROLE, kind)
        if kind == "dir":
            item.setToolTip(f"Folder — every video inside will be scanned.\n{path}")
            item.setForeground(QColor("#2563EB"))
        else:
            item.setToolTip(f"Single video file.\n{path}")
            item.setForeground(QColor("#111827"))
        self.list_widget.addItem(item)
        return 1

    # ------------------------------------------------------------------
    # Removing entries
    # ------------------------------------------------------------------

    def _remove_selected(self) -> None:
        for item in self.list_widget.selectedItems():
            self._forget(item)
        self.sources_changed.emit()

    def clear(self) -> None:
        for item in self._iter_items():
            self._forget(item)
        self.sources_changed.emit()

    def _forget(self, item: QListWidgetItem) -> None:
        self._known_paths.discard(os.path.abspath(item.text()).lower())
        self.list_widget.takeItem(self.list_widget.row(item))

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_sources(self) -> list[tuple[str, str]]:
        return [
            (item.data(self.KIND_ROLE), item.text())
            for item in self._iter_items()
        ]

    def set_sources(self, sources: list[tuple[str, str]]) -> None:
        self.list_widget.blockSignals(True)
        self._known_paths.clear()
        self.list_widget.clear()
        self.list_widget.blockSignals(False)
        for kind, path in sources:
            self._append_entry(kind, path)
        self.sources_changed.emit()

    def _iter_items(self):
        return [self.list_widget.item(i) for i in range(self.list_widget.count())]

    # ------------------------------------------------------------------
    # Browse dialogs
    # ------------------------------------------------------------------

    def _browse_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select Video Files", str(Path.home()), MEDIA_FILTER
        )
        if files:
            self.add_paths(files)

    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select Video Folder", str(Path.home())
        )
        if folder:
            self.add_paths([folder])


class GpuDetectWorker(QThread):
    """Runs hardware encoder detection off the UI thread."""

    detected = Signal(str)

    def run(self) -> None:
        key = FFmpegEngine().detect_gpu_encoder()
        self.detected.emit(key)


class SettingsDialog(QDialog):
    """Application settings: FFmpeg location and default output folder."""

    def __init__(
        self,
        parent: QWidget | None,
        ffmpeg_dir: str,
        default_output: str,
        version_info: str,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        grid = QGridLayout()
        grid.setVerticalSpacing(12)
        grid.setHorizontalSpacing(10)

        ffmpeg_label = QLabel("FFmpeg Location")
        ffmpeg_label.setObjectName("field-label")
        ffmpeg_label.setFixedWidth(150)
        self.ffmpeg_edit = QLineEdit(ffmpeg_dir)
        self.ffmpeg_edit.setReadOnly(True)
        self.ffmpeg_edit.setPlaceholderText(
            "Automatic — bundled binary or system PATH"
        )
        ffmpeg_browse = QPushButton("Browse...")
        ffmpeg_browse.setObjectName("secondary-button")
        ffmpeg_browse.setFixedWidth(90)
        ffmpeg_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        ffmpeg_browse.clicked.connect(self._browse_ffmpeg_dir)
        ffmpeg_clear = QPushButton("Reset")
        ffmpeg_clear.setObjectName("secondary-button")
        ffmpeg_clear.setFixedWidth(70)
        ffmpeg_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        ffmpeg_clear.clicked.connect(lambda: self.ffmpeg_edit.clear())

        output_label = QLabel("Default Output")
        output_label.setObjectName("field-label")
        output_label.setFixedWidth(150)
        self.output_edit = QLineEdit(default_output)
        self.output_edit.setReadOnly(True)
        self.output_edit.setPlaceholderText("No default output folder set")
        output_browse = QPushButton("Browse...")
        output_browse.setObjectName("secondary-button")
        output_browse.setFixedWidth(90)
        output_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        output_browse.clicked.connect(self._browse_default_output)
        output_clear = QPushButton("Reset")
        output_clear.setObjectName("secondary-button")
        output_clear.setFixedWidth(70)
        output_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        output_clear.clicked.connect(lambda: self.output_edit.clear())

        grid.addWidget(ffmpeg_label, 0, 0)
        grid.addWidget(self.ffmpeg_edit, 0, 1)
        grid.addWidget(ffmpeg_browse, 0, 2)
        grid.addWidget(ffmpeg_clear, 0, 3)
        grid.addWidget(output_label, 1, 0)
        grid.addWidget(self.output_edit, 1, 1)
        grid.addWidget(output_browse, 1, 2)
        grid.addWidget(output_clear, 1, 3)
        layout.addLayout(grid)

        info = QLabel(
            f"Active FFmpeg:\n{version_info}\n\n"
            "Override only needed when the bundled/system FFmpeg lacks GPU encoders."
        )
        info.setObjectName("hint-label")
        info.setWordWrap(True)
        layout.addWidget(info)

        layout.addStretch()

        buttons_row = QWidget()
        buttons_layout = QHBoxLayout(buttons_row)
        buttons_layout.setContentsMargins(0, 0, 0, 0)

        self.reset_all_button = QPushButton("Reset All Settings")
        self.reset_all_button.setObjectName("danger-button")
        self.reset_all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_all_button.clicked.connect(self._reset_all_settings)

        close_button = QPushButton("Cancel")
        close_button.setObjectName("secondary-button")
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.clicked.connect(self.reject)

        save_button = QPushButton("Save")
        save_button.setObjectName("primary-button")
        save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        save_button.clicked.connect(self.accept)

        buttons_layout.addWidget(self.reset_all_button)
        buttons_layout.addStretch()
        buttons_layout.addWidget(close_button)
        buttons_layout.addWidget(save_button)
        layout.addWidget(buttons_row)

    # ------------------------------------------------------------------
    def _browse_ffmpeg_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select Folder Containing ffmpeg.exe / ffprobe.exe",
            self.ffmpeg_edit.text() or str(Path.home()),
        )
        if folder:
            self.ffmpeg_edit.setText(folder)

    def _browse_default_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select Default Output Folder",
            self.output_edit.text() or str(Path.home()),
        )
        if folder:
            self.output_edit.setText(folder)

    def _reset_all_settings(self) -> None:
        reply = QMessageBox.question(
            self,
            "Reset Settings",
            "All saved settings will be cleared.\n"
            "The application will use defaults after restart.\n\nContinue?",
        )
        if reply == QMessageBox.StandardButton.Yes:
            QSettings("SmartVideoCompiler", "MainWindow").clear()
            self.parent()._settings_reset_requested = True
            self.reject()

    def ffmpeg_dir_value(self) -> str:
        return self.ffmpeg_edit.text().strip()

    def default_output_value(self) -> str:
        return self.output_edit.text().strip()


class MainWindow(QMainWindow):
    """Primary application window for the Smart Video Compiler desktop UI."""

    # Public widget references for external access
    compile_pill: QPushButton
    silence_pill: QPushButton
    content_stack: QStackedWidget
    source_list: SourceListWidget
    output_folder_edit: FolderDropLineEdit
    total_duration_spin: NoWheelSpinBox
    clip_duration_spin: NoWheelSpinBox
    aspect_combo: NoWheelComboBox
    scene_check: QPushButton
    fade_check: QPushButton
    encoder_combo: NoWheelComboBox
    encoder_warning: QLabel
    detected_label: QLabel
    preview_label: QLabel
    queue_status_label: QLabel
    silence_drop_zone: DropZone
    silence_file_edit: QLineEdit
    threshold_spin: NoWheelSpinBox
    min_duration_spin: NoWheelDoubleSpinBox
    padding_spin: NoWheelDoubleSpinBox
    noise_check: QPushButton
    noise_mode_combo: NoWheelComboBox
    noise_strength_combo: NoWheelComboBox
    compile_button: QPushButton
    silence_button: QPushButton
    cancel_button: QPushButton
    progress_bar: QProgressBar
    status_label: QLabel
    log_console: QPlainTextEdit

    # Public signals
    plan_requested = Signal(list, str, int, int, str, bool, bool, float)
    render_requested = Signal(object)
    discard_session_requested = Signal(object)
    silence_removal_requested = Signal(str, str, str, int, float, float, bool, str, str)
    cancellation_requested = Signal()
    ffmpeg_override_changed = Signal(str)

    PLAN_BUTTON_TEXT = "Plan Clips"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.setWindowTitle("Smart Video Compiler")
        self.setMinimumSize(780, 550)
        self.resize(960, 700)

        self._busy_owner: str | None = None
        self._detected_key: str | None = None
        self._detect_worker: GpuDetectWorker | None = None
        self._settings_reset_requested = False
        self._loading = False
        self._queued_count = 0

        self._build_ui()
        self._apply_stylesheet()
        self._connect_dynamic_updates()
        self._load_settings()
        self._update_output_preview()
        self._restore_status_label()

        QTimer.singleShot(250, self.start_detection)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = self._create_header_bar()
        layout.addWidget(header)

        scroll = self._create_scroll_area()
        layout.addWidget(scroll, stretch=1)

    def _create_header_bar(self) -> QFrame:
        header = QFrame()
        header.setObjectName("header-bar")

        layout = QHBoxLayout(header)
        layout.setContentsMargins(24, 0, 24, 0)
        layout.setSpacing(0)

        brand = QWidget()
        brand_layout = QHBoxLayout(brand)
        brand_layout.setContentsMargins(0, 0, 0, 0)
        brand_layout.setSpacing(10)

        icon = QFrame()
        icon.setObjectName("header-icon")
        icon.setFixedSize(22, 22)

        title = QLabel("Smart Video Compiler")
        title.setObjectName("header-title")

        brand_layout.addWidget(icon)
        brand_layout.addWidget(title)
        layout.addWidget(brand)

        layout.addSpacing(48)

        self.compile_pill = QPushButton("Clip Compiler")
        self.compile_pill.setObjectName("mode-pill")
        self.compile_pill.setCheckable(True)
        self.compile_pill.setChecked(True)
        self.compile_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        self.compile_pill.clicked.connect(self._show_compiler_mode)

        self.silence_pill = QPushButton("Silence Removal")
        self.silence_pill.setObjectName("mode-pill")
        self.silence_pill.setCheckable(True)
        self.silence_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        self.silence_pill.clicked.connect(self._show_silence_mode)

        pill_group = QButtonGroup(self)
        pill_group.setExclusive(True)
        pill_group.addButton(self.compile_pill)
        pill_group.addButton(self.silence_pill)

        layout.addWidget(self.compile_pill)
        layout.addSpacing(4)
        layout.addWidget(self.silence_pill)

        layout.addStretch()

        self.history_button = QPushButton("History")
        self.history_button.setObjectName("header-text-button")
        self.history_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.history_button.setToolTip("Past jobs")
        self.history_button.clicked.connect(self._open_history)

        self.settings_button = QPushButton("\u2699")
        self.settings_button.setObjectName("icon-button")
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.setToolTip("Settings")
        self.settings_button.clicked.connect(self._open_settings)

        self.help_button = QPushButton("?")
        self.help_button.setObjectName("icon-button")
        self.help_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.help_button.setToolTip("Help")
        self.help_button.clicked.connect(self._open_help)

        layout.addWidget(self.history_button)
        layout.addSpacing(12)
        layout.addWidget(self.settings_button)
        layout.addSpacing(12)
        layout.addWidget(self.help_button)

        return header

    def _create_scroll_area(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName("content-scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        scroll_content = QWidget()
        scroll_content.setStyleSheet("background-color: #F2F2F2;")
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(32, 28, 32, 16)
        scroll_layout.setSpacing(18)

        self.content_stack = QStackedWidget()
        self.content_stack.addWidget(self._create_compiler_page())
        self.content_stack.addWidget(self._create_silence_page())
        scroll_layout.addWidget(self.content_stack)

        scroll_layout.addWidget(self._create_shared_output_card())
        scroll_layout.addWidget(self._create_progress_log_card())
        scroll_layout.addWidget(self._create_footer())

        scroll.setWidget(scroll_content)
        return scroll

    def _create_compiler_page(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        title = self._create_mode_title(
            "Clip Compiler",
            "Review random highlight clips before compiling them into one video.",
        )
        layout.addWidget(title)

        sources_card, sources_layout = self._create_card("Source Videos")
        self.source_list = SourceListWidget()
        self.source_list.sources_changed.connect(self._on_sources_changed)
        sources_layout.addWidget(self.source_list)
        layout.addWidget(sources_card)

        settings_card, settings_layout = self._create_card("Compilation Settings")
        settings_grid = QGridLayout()
        settings_grid.setVerticalSpacing(12)
        settings_grid.setHorizontalSpacing(10)

        total_label = QLabel("Total Compilation Duration")
        total_label.setObjectName("field-label")
        total_label.setFixedWidth(160)
        self.total_duration_spin = NoWheelSpinBox()
        self.total_duration_spin.setRange(1, 10)
        self.total_duration_spin.setSuffix(" min")
        self.total_duration_spin.setValue(5)
        self.total_duration_spin.setFixedWidth(90)

        clip_label = QLabel("Duration per Clip")
        clip_label.setObjectName("field-label")
        clip_label.setFixedWidth(160)
        self.clip_duration_spin = NoWheelSpinBox()
        self.clip_duration_spin.setRange(1, 60)
        self.clip_duration_spin.setSuffix(" sec")
        self.clip_duration_spin.setValue(10)
        self.clip_duration_spin.setFixedWidth(90)

        aspect_label = QLabel("Aspect")
        aspect_label.setObjectName("field-label")
        aspect_label.setFixedWidth(160)
        self.aspect_combo = NoWheelComboBox()
        self.aspect_combo.addItem(ASPECT_LANDSCAPE)
        self.aspect_combo.addItem(ASPECT_PORTRAIT)
        self.aspect_combo.setFixedWidth(260)
        self.aspect_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        options_grid_row = 3

        self.scene_check = QPushButton("Cut at scene boundaries (slower planning)")
        self.scene_check.setObjectName("check-pill")
        self.scene_check.setCheckable(True)
        self.scene_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scene_check.setToolTip(
            "Snap clip starts to detected scene changes instead of raw offsets."
        )

        self.fade_check = QPushButton("Smooth fade between clips (0.5s)")
        self.fade_check.setObjectName("check-pill")
        self.fade_check.setCheckable(True)
        self.fade_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fade_check.setToolTip(
            "Off = hard cut (default). On = 0.5s crossfade between clips."
        )

        settings_grid.addWidget(total_label, 0, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
        settings_grid.addWidget(self.total_duration_spin, 0, 1, alignment=Qt.AlignmentFlag.AlignLeft)
        settings_grid.addWidget(clip_label, 1, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
        settings_grid.addWidget(self.clip_duration_spin, 1, 1, alignment=Qt.AlignmentFlag.AlignLeft)
        settings_grid.addWidget(aspect_label, 2, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
        settings_grid.addWidget(self.aspect_combo, 2, 1, alignment=Qt.AlignmentFlag.AlignLeft)
        settings_grid.addWidget(self.scene_check, options_grid_row, 0, 1, 2, alignment=Qt.AlignmentFlag.AlignLeft)
        settings_grid.addWidget(self.fade_check, options_grid_row + 1, 0, 1, 2, alignment=Qt.AlignmentFlag.AlignLeft)
        settings_layout.addLayout(settings_grid)
        layout.addWidget(settings_card)

        self.compile_button = QPushButton(self.PLAN_BUTTON_TEXT)
        self.compile_button.setMinimumHeight(44)
        self.compile_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.compile_button.clicked.connect(self._on_compile_clicked)
        self.compile_button.setStyleSheet(self._primary_button_qss())
        layout.addWidget(self.compile_button)

        return widget

    def _create_silence_page(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        title = self._create_mode_title(
            "Silence Removal",
            "Remove silent parts from a single media file and save the trimmed result.",
        )
        layout.addWidget(title)

        source_card, source_layout = self._create_card("Select Source Media")

        self.silence_drop_zone = DropZone()
        self.silence_drop_zone.file_dropped.connect(self._on_silence_file_dropped)
        self.silence_drop_zone.select_button.clicked.connect(self._browse_silence_file)
        source_layout.addWidget(self.silence_drop_zone)

        file_row = QWidget()
        file_layout = QHBoxLayout(file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        file_layout.setSpacing(8)

        file_label = QLabel("Selected File")
        file_label.setObjectName("field-label")
        file_label.setFixedWidth(90)
        self.silence_file_edit = QLineEdit()
        self.silence_file_edit.setReadOnly(True)
        self.silence_file_edit.setPlaceholderText("No media file selected...")
        file_browse = QPushButton("Browse...")
        file_browse.setObjectName("secondary-button")
        file_browse.setFixedWidth(90)
        file_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        file_browse.clicked.connect(self._browse_silence_file)

        file_layout.addWidget(file_label, alignment=Qt.AlignmentFlag.AlignVCenter)
        file_layout.addWidget(self.silence_file_edit, stretch=1)
        file_layout.addWidget(file_browse)
        source_layout.addWidget(file_row)

        layout.addWidget(source_card)

        params_card, params_layout = self._create_card("Silence Detection Parameters")
        params_grid = QGridLayout()
        params_grid.setVerticalSpacing(12)
        params_grid.setHorizontalSpacing(10)

        threshold_label = QLabel("Threshold (dB)")
        threshold_label.setObjectName("field-label")
        threshold_label.setFixedWidth(160)
        self.threshold_spin = NoWheelSpinBox()
        self.threshold_spin.setRange(-60, 0)
        self.threshold_spin.setSuffix(" dB")
        self.threshold_spin.setValue(-30)
        self.threshold_spin.setFixedWidth(90)

        min_dur_label = QLabel("Minimum Duration (s)")
        min_dur_label.setObjectName("field-label")
        min_dur_label.setFixedWidth(160)
        self.min_duration_spin = NoWheelDoubleSpinBox()
        self.min_duration_spin.setRange(0.1, 10.0)
        self.min_duration_spin.setSuffix(" s")
        self.min_duration_spin.setValue(0.5)
        self.min_duration_spin.setSingleStep(0.1)
        self.min_duration_spin.setDecimals(1)
        self.min_duration_spin.setFixedWidth(90)

        padding_label = QLabel("Padding (s)")
        padding_label.setObjectName("field-label")
        padding_label.setFixedWidth(160)
        self.padding_spin = NoWheelDoubleSpinBox()
        self.padding_spin.setRange(0.0, 5.0)
        self.padding_spin.setSuffix(" s")
        self.padding_spin.setValue(0.0)
        self.padding_spin.setSingleStep(0.1)
        self.padding_spin.setDecimals(1)
        self.padding_spin.setFixedWidth(90)

        noise_mode_label = QLabel("Denoise Mode")
        noise_mode_label.setObjectName("field-label")
        noise_mode_label.setFixedWidth(160)
        self.noise_mode_combo = NoWheelComboBox()
        self.noise_mode_combo.addItem("FFT (fast)")
        self.noise_mode_combo.addItem("AI RNNoise (best for voice)")
        self.noise_mode_combo.setFixedWidth(260)
        self.noise_mode_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        noise_strength_label = QLabel("Strength")
        noise_strength_label.setObjectName("field-label")
        noise_strength_label.setFixedWidth(160)
        self.noise_strength_combo = NoWheelComboBox()
        self.noise_strength_combo.addItems(["Light", "Medium", "Strong"])
        self.noise_strength_combo.setCurrentIndex(1)
        self.noise_strength_combo.setFixedWidth(260)
        self.noise_strength_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        params_grid.addWidget(threshold_label, 0, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
        params_grid.addWidget(self.threshold_spin, 0, 1, alignment=Qt.AlignmentFlag.AlignLeft)
        params_grid.addWidget(min_dur_label, 1, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
        params_grid.addWidget(self.min_duration_spin, 1, 1, alignment=Qt.AlignmentFlag.AlignLeft)
        params_grid.addWidget(padding_label, 2, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
        params_grid.addWidget(self.padding_spin, 2, 1, alignment=Qt.AlignmentFlag.AlignLeft)

        self.noise_check = QPushButton("Remove background noise")
        self.noise_check.setObjectName("check-pill")
        self.noise_check.setCheckable(True)
        self.noise_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.noise_check.toggled.connect(self._on_noise_toggled)

        self.noise_mode_label = noise_mode_label
        self.noise_strength_label = noise_strength_label

        params_grid.addWidget(self.noise_check, 3, 0, 1, 2, alignment=Qt.AlignmentFlag.AlignLeft)
        params_grid.addWidget(noise_mode_label, 4, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
        params_grid.addWidget(self.noise_mode_combo, 4, 1, alignment=Qt.AlignmentFlag.AlignLeft)
        params_grid.addWidget(noise_strength_label, 5, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
        params_grid.addWidget(self.noise_strength_combo, 5, 1, alignment=Qt.AlignmentFlag.AlignLeft)
        params_layout.addLayout(params_grid)

        self.noise_hint = QLabel(
            "FFT: fast, good against hiss/fan noise. "
            "AI RNNoise: best clarity for voice content."
        )
        self.noise_hint.setObjectName("hint-label")
        self.noise_hint.setWordWrap(True)
        params_layout.addWidget(self.noise_hint)

        self._set_noise_controls_visible(False)

        layout.addWidget(params_card)

        self.silence_button = QPushButton("Remove Silence & Save")
        self.silence_button.setMinimumHeight(44)
        self.silence_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.silence_button.clicked.connect(self._on_silence_clicked)
        self.silence_button.setStyleSheet(self._primary_button_qss())
        layout.addWidget(self.silence_button)

        return widget

    def _create_shared_output_card(self) -> QFrame:
        card, layout = self._create_card("Output & Encoder")

        grid = QGridLayout()
        grid.setVerticalSpacing(12)
        grid.setHorizontalSpacing(10)

        output_label = QLabel("Output Folder")
        output_label.setObjectName("field-label")
        output_label.setFixedWidth(160)
        self.output_folder_edit = FolderDropLineEdit(
            on_folder_dropped=lambda p: self.append_log(f"Output folder set to: {p}")
        )
        self.output_folder_edit.setReadOnly(True)
        self.output_folder_edit.setPlaceholderText(
            "Where compiled / cleaned results will be saved..."
        )
        output_browse = QPushButton("Browse...")
        output_browse.setObjectName("secondary-button")
        output_browse.setFixedWidth(90)
        output_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        output_browse.clicked.connect(self._browse_output_folder)

        encoder_label = QLabel("Hardware Encoder")
        encoder_label.setObjectName("field-label")
        encoder_label.setFixedWidth(160)
        self.encoder_combo = NoWheelComboBox()
        self.encoder_combo.addItem(AUTO_ENCODER_LABEL)
        self.encoder_combo.addItems(ENCODER_CHOICES)
        self.encoder_combo.setCurrentIndex(0)
        self.encoder_combo.setFixedWidth(300)
        self.encoder_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        grid.addWidget(output_label, 0, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(self.output_folder_edit, 0, 1)
        grid.addWidget(output_browse, 0, 2)
        grid.addWidget(encoder_label, 1, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(self.encoder_combo, 1, 1, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addLayout(grid)

        self.detected_label = QLabel("Detecting GPU encoder...")
        self.detected_label.setObjectName("hint-label")
        layout.addWidget(self.detected_label)

        self.encoder_warning = QLabel(
            "HEVC: files are ~30-40% smaller but need newer devices/players "
            "for playback."
        )
        self.encoder_warning.setObjectName("warn-label")
        self.encoder_warning.setWordWrap(True)
        self.encoder_warning.setVisible(False)
        layout.addWidget(self.encoder_warning)

        self.preview_label = QLabel("")
        self.preview_label.setObjectName("hint-label")
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)

        return card

    def _create_progress_log_card(self) -> QFrame:
        card, layout = self._create_card("Progress Logs")

        self.status_label = QLabel("Ready \u2014 Add video sources to begin.")
        self.status_label.setObjectName("status-label")
        layout.addWidget(self.status_label)

        self.queue_status_label = QLabel("")
        self.queue_status_label.setObjectName("queue-label")
        self.queue_status_label.setVisible(False)
        layout.addWidget(self.queue_status_label)

        progress_row = QWidget()
        progress_layout = QHBoxLayout(progress_row)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(10)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setMaximumHeight(10)
        self.progress_bar.setVisible(False)
        progress_layout.addWidget(self.progress_bar, stretch=1)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("cancel-button")
        self.cancel_button.setFixedHeight(32)
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        progress_layout.addWidget(self.cancel_button)

        layout.addWidget(progress_row)

        self.log_console = QPlainTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setPlaceholderText("Activity logs will appear here...")
        self.log_console.setObjectName("log-console")
        self.log_console.setStyleSheet(
            "QPlainTextEdit { background-color: #2D2D2D; color: #D1D5DB;"
            " font-family: 'JetBrains Mono', 'Consolas', monospace; font-size: 13px;"
            " border: 1px solid #E5E5E5; border-radius: 6px; padding: 12px; }"
        )
        layout.addWidget(self.log_console, stretch=1)

        return card

    def _create_footer(self) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        copyright = QLabel("\u00a9 2026 Smart Video Compiler \u2014 Desktop Pro Edition")
        copyright.setObjectName("footer-label")

        links = QLabel("Documentation    Support    Release Notes")
        links.setObjectName("footer-link")

        layout.addWidget(copyright)
        layout.addStretch()
        layout.addWidget(links)

        return widget

    def _create_card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("card")

        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title_label = QLabel(title)
        title_label.setObjectName("card-title")
        layout.addWidget(title_label)

        return card, layout

    def _create_mode_title(self, title: str, subtitle: str) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        title_label = QLabel(title)
        title_label.setObjectName("mode-title")
        layout.addWidget(title_label)

        subtitle_label = QLabel(subtitle)
        subtitle_label.setObjectName("mode-subtitle")
        layout.addWidget(subtitle_label)

        return widget

    @staticmethod
    def _primary_button_qss() -> str:
        return (
            "QPushButton { background-color: #2563EB; color: #FFFFFF;"
            " border: none; border-radius: 8px; font-size: 14px; font-weight: 600;"
            " padding: 10px 24px; }"
            "QPushButton:hover { background-color: #1D4ED8; }"
            "QPushButton:pressed { background-color: #1E40AF; }"
            "QPushButton:disabled { background-color: #93C5FD; color: #FFFFFF; }"
        )

    # ------------------------------------------------------------------
    # Stylesheet
    # ------------------------------------------------------------------

    def _apply_stylesheet(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
                font-family: "Inter", "Segoe UI", sans-serif;
                font-size: 14px;
                color: #111827;
            }

            QMainWindow {
                background-color: #F2F2F2;
            }

            #header-bar {
                background-color: #FFFFFF;
                border-bottom: 1px solid #E5E5E5;
                min-height: 54px;
                max-height: 54px;
            }

            #header-icon {
                background-color: #2563EB;
                border-radius: 6px;
            }

            #header-title {
                font-size: 15px;
                font-weight: 600;
                color: #111827;
            }

            #mode-pill {
                background-color: transparent;
                border: none;
                border-radius: 9999px;
                color: #6B7280;
                padding: 6px 14px;
                font-weight: 500;
                font-size: 13px;
            }

            #mode-pill:checked {
                background-color: #2563EB;
                color: #FFFFFF;
            }

            #mode-pill:hover:!checked {
                color: #374151;
            }

            #header-text-button {
                background-color: transparent;
                border: none;
                color: #6B7280;
                font-size: 13px;
                padding: 6px 8px;
            }

            #header-text-button:hover {
                color: #2563EB;
            }

            #icon-button {
                background-color: transparent;
                border: none;
                color: #6B7280;
                font-size: 14px;
                min-width: 28px;
                max-width: 28px;
                min-height: 28px;
                max-height: 28px;
                border-radius: 9999px;
            }

            #icon-button:hover {
                background-color: #F3F4F6;
                color: #374151;
            }

            #content-scroll {
                background-color: #F2F2F2;
                border: none;
            }

            #content-scroll > QWidget {
                background-color: #F2F2F2;
            }

            #mode-title {
                font-size: 25px;
                font-weight: 700;
                color: #111827;
                line-height: 32px;
            }

            #mode-subtitle {
                font-size: 14px;
                color: #6B7280;
                line-height: 20px;
            }

            #card {
                background-color: #FFFFFF;
                border: 1px solid #E5E5E5;
                border-radius: 8px;
            }

            #card-title {
                font-size: 12px;
                font-weight: 600;
                color: #374151;
                line-height: 16px;
            }

            #field-label {
                font-size: 12px;
                font-weight: 500;
                color: #4B5563;
                line-height: 16px;
            }

            #hint-label {
                font-size: 11px;
                color: #6B7280;
                line-height: 15px;
            }

            #warn-label {
                font-size: 11px;
                color: #B45309;
                line-height: 15px;
            }

            #queue-label {
                font-size: 11px;
                color: #2563EB;
                font-weight: 600;
            }

            #check-pill {
                background-color: #F9FAFB;
                border: 1px solid #D1D5DB;
                border-radius: 9999px;
                color: #4B5563;
                padding: 6px 14px;
                font-size: 12px;
            }

            #check-pill:checked {
                background-color: #EFF6FF;
                border: 1px solid #2563EB;
                color: #1D4ED8;
                font-weight: 600;
            }

            #check-pill:hover:!checked {
                border: 1px solid #9CA3AF;
            }

            QLineEdit,
            QSpinBox,
            QDoubleSpinBox,
            QComboBox {
                background-color: #F9FAFB;
                border: 1px solid #E5E7EB;
                border-radius: 6px;
                padding: 9px 12px;
                color: #1F2937;
                font-size: 13px;
                min-height: 16px;
            }

            QLineEdit:focus,
            QSpinBox:focus,
            QDoubleSpinBox:focus,
            QComboBox:focus {
                border: 1px solid #2563EB;
            }

            QSpinBox::up-button,
            QSpinBox::down-button,
            QDoubleSpinBox::up-button,
            QDoubleSpinBox::down-button {
                width: 18px;
                border: none;
                background: transparent;
            }

            QComboBox::drop-down {
                border: none;
                width: 24px;
            }

            QComboBox::down-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid #6B7280;
                width: 0;
                height: 0;
            }

            QComboBox QAbstractItemView {
                background-color: #FFFFFF;
                border: 1px solid #E5E7EB;
                border-radius: 6px;
                selection-background-color: #EFF6FF;
                selection-color: #111827;
                padding: 4px;
            }

            #source-list,
            #preview-list {
                background-color: #FFFFFF;
                border: 1px dashed #C2C6D4;
                border-radius: 8px;
                padding: 6px;
                font-size: 13px;
                min-height: 120px;
            }

            #source-list::item,
            #preview-list::item {
                padding: 6px 8px;
                border-radius: 4px;
            }

            #source-list::item:selected,
            #preview-list::item:selected {
                background-color: #EFF6FF;
                color: #1D4ED8;
            }

            #source-list::item:hover:!selected,
            #preview-list::item:hover:!selected {
                background-color: #F3F4F6;
            }

            QPushButton#primary-button {
                background-color: #2563EB;
                color: #FFFFFF;
                border: none;
                border-radius: 8px;
                padding: 12px 20px;
                font-weight: 600;
                font-size: 14px;
                min-height: 44px;
            }

            QPushButton#primary-button:hover {
                background-color: #1D4ED8;
            }

            QPushButton#primary-button:pressed {
                background-color: #1E40AF;
            }

            QPushButton#primary-button:disabled {
                background-color: #93C5FD;
                color: #FFFFFF;
            }

            QPushButton#secondary-button {
                background-color: #FFFFFF;
                border: 1px solid #D1D5DB;
                color: #374151;
                border-radius: 6px;
                padding: 8px 14px;
                font-weight: 500;
                font-size: 12px;
                min-height: 34px;
            }

            QPushButton#secondary-button:hover {
                background-color: #F9FAFB;
                border: 1px solid #9CA3AF;
            }

            QPushButton#secondary-button:pressed {
                background-color: #F3F4F6;
            }

            QPushButton#danger-button {
                background-color: #FFFFFF;
                border: 1px solid #BA1A1A;
                color: #BA1A1A;
                border-radius: 6px;
                padding: 8px 14px;
                font-weight: 500;
                font-size: 12px;
                min-height: 34px;
            }

            QPushButton#danger-button:hover {
                background-color: #FFDAD6;
            }

            QPushButton#cancel-button {
                background-color: transparent;
                border: 1px solid #ba1a1a;
                color: #ba1a1a;
                border-radius: 8px;
                padding: 8px 16px;
                font-weight: 500;
                font-size: 13px;
                min-height: 32px;
            }

            QPushButton#cancel-button:hover {
                background-color: #ffdad6;
            }

            QPushButton#cancel-button:pressed {
                background-color: #ffb4ab;
            }

            QPushButton#cancel-button:disabled {
                background-color: transparent;
                border: 1px solid #f5b5b5;
                color: #d97373;
            }

            #drop-zone {
                background-color: #F8F9FA;
                color: #9CA3AF;
                border: 2px dashed #c2c6d4;
                border-radius: 10px;
                padding: 8px;
                font-size: 13px;
            }

            #drop-zone:hover {
                border: 2px dashed #2563EB;
                background-color: #F0F7FC;
            }

            #drop-zone[dragging="true"] {
                border: 2px dashed #2563EB;
                background-color: #EFF6FF;
                color: #2563EB;
            }

            #drop-zone-title {
                color: #6B7280;
                font-size: 13px;
                font-weight: 500;
                background-color: transparent;
            }

            #drop-zone-or {
                color: #9CA3AF;
                font-size: 12px;
                background-color: transparent;
            }

            #status-label {
                font-size: 12px;
                font-weight: 400;
                color: #22C55E;
                line-height: 16px;
            }

            QProgressBar {
                background-color: #E5E5E5;
                border: none;
                border-radius: 9999px;
                text-align: center;
                color: #6B7280;
                font-weight: 700;
                font-size: 10px;
                max-height: 10px;
            }

            QProgressBar::chunk {
                background-color: #2563EB;
                border-radius: 9999px;
            }

            #footer-label {
                font-size: 11px;
                color: #9CA3AF;
                line-height: 16px;
            }

            #footer-link {
                font-size: 11px;
                color: #6B7280;
                line-height: 16px;
            }
            """
        )

    # ------------------------------------------------------------------
    # Dynamic wiring
    # ------------------------------------------------------------------

    def _connect_dynamic_updates(self) -> None:
        self.total_duration_spin.valueChanged.connect(lambda _: self._save_settings())
        self.clip_duration_spin.valueChanged.connect(lambda _: self._save_settings())
        self.encoder_combo.currentTextChanged.connect(
            lambda _: self._on_encoder_changed()
        )
        self.aspect_combo.currentTextChanged.connect(lambda _: self._save_settings())
        self.scene_check.toggled.connect(lambda _: self._save_settings())
        self.fade_check.toggled.connect(lambda _: self._save_settings())
        self.threshold_spin.valueChanged.connect(lambda _: self._save_settings())
        self.min_duration_spin.valueChanged.connect(lambda _: self._save_settings())
        self.padding_spin.valueChanged.connect(lambda _: self._save_settings())

        self.output_folder_edit.textChanged.connect(lambda _: self._on_output_changed())
        self.silence_file_edit.textChanged.connect(
            lambda _: self._on_silence_file_text_changed()
        )

    def _on_encoder_changed(self) -> None:
        text = self.encoder_combo.currentText().lower()
        is_hevc = "hevc" in text and text != AUTO_ENCODER_LABEL.lower()
        self.encoder_warning.setVisible(is_hevc)
        if not self._loading:
            self._save_settings()

    def _on_noise_toggled(self, checked: bool) -> None:
        self._set_noise_controls_visible(checked)
        if not self._loading:
            self._save_settings()

    def _set_noise_controls_visible(self, visible: bool) -> None:
        for widget in (
            self.noise_mode_label,
            self.noise_mode_combo,
            self.noise_strength_label,
            self.noise_strength_combo,
            self.noise_hint,
        ):
            widget.setVisible(visible)

    # ------------------------------------------------------------------
    # Settings persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _settings() -> QSettings:
        return QSettings("SmartVideoCompiler", "MainWindow")

    def _load_settings(self) -> None:
        self._loading = True
        try:
            self._load_settings_impl()
        finally:
            self._loading = False

    def _load_settings_impl(self) -> None:
        settings = self._settings()

        output = settings.value("output_folder", "", type=str)
        if output and Path(output).exists():
            self.output_folder_edit.setText(output)
        else:
            default_output = settings.value("default_output_folder", "", type=str)
            if default_output and Path(default_output).exists():
                self.output_folder_edit.setText(default_output)

        raw_sources = settings.value("sources", "", type=str)
        pairs: list[tuple[str, str]] = []
        if raw_sources:
            try:
                data = json.loads(raw_sources)
                pairs = [
                    (entry["kind"], entry["path"])
                    for entry in data
                    if entry.get("kind") in ("file", "dir") and entry.get("path")
                ]
            except (ValueError, TypeError, KeyError):
                pairs = []
        if pairs:
            self.source_list.set_sources(pairs)

        self.total_duration_spin.setValue(int(settings.value("total_duration", 5)))
        self.clip_duration_spin.setValue(int(settings.value("clip_duration", 10)))

        aspect_choice = settings.value("aspect_choice", ASPECT_LANDSCAPE, type=str)
        aspect_index = self.aspect_combo.findText(aspect_choice)
        if aspect_index >= 0:
            self.aspect_combo.setCurrentIndex(aspect_index)

        self.scene_check.setChecked(settings.value("scene_cut", False, type=bool))
        self.fade_check.setChecked(settings.value("crossfade", False, type=bool))

        encoder_choice = settings.value("encoder_choice", "", type=str)
        index = self.encoder_combo.findText(encoder_choice)
        if index >= 0:
            self.encoder_combo.setCurrentIndex(index)

        self.threshold_spin.setValue(int(settings.value("threshold_db", -30)))
        self.min_duration_spin.setValue(float(settings.value("min_duration", 0.5)))
        self.padding_spin.setValue(float(settings.value("padding", 0.0)))

        self.noise_check.setChecked(settings.value("noise_enabled", False, type=bool))
        noise_mode = settings.value("noise_mode", "FFT (fast)", type=str)
        noise_index = self.noise_mode_combo.findText(noise_mode)
        if noise_index >= 0:
            self.noise_mode_combo.setCurrentIndex(noise_index)
        strength = settings.value("noise_strength", "Medium", type=str)
        strength_index = self.noise_strength_combo.findText(strength)
        if strength_index >= 0:
            self.noise_strength_combo.setCurrentIndex(strength_index)

        last_media = settings.value("last_media_file", "", type=str)
        if last_media and Path(last_media).exists():
            self.silence_file_edit.setText(last_media)

    def _save_settings(self) -> None:
        settings = self._settings()
        sources = self.source_list.get_sources()
        settings.setValue(
            "sources",
            json.dumps([{"kind": kind, "path": path} for kind, path in sources]),
        )
        settings.setValue("output_folder", self.output_folder_edit.text().strip())
        settings.setValue("total_duration", self.total_duration_spin.value())
        settings.setValue("clip_duration", self.clip_duration_spin.value())
        settings.setValue("aspect_choice", self.aspect_combo.currentText())
        settings.setValue("scene_cut", self.scene_check.isChecked())
        settings.setValue("crossfade", self.fade_check.isChecked())
        settings.setValue("encoder_choice", self.encoder_combo.currentText())
        settings.setValue("threshold_db", self.threshold_spin.value())
        settings.setValue("min_duration", self.min_duration_spin.value())
        settings.setValue("padding", self.padding_spin.value())
        settings.setValue("noise_enabled", self.noise_check.isChecked())
        settings.setValue("noise_mode", self.noise_mode_combo.currentText())
        settings.setValue("noise_strength", self.noise_strength_combo.currentText())
        settings.setValue("last_media_file", self.silence_file_edit.text().strip())
        settings.sync()

    def _on_output_changed(self) -> None:
        if self._loading:
            return
        self._save_settings()
        self._update_output_preview()

    def _on_silence_file_text_changed(self) -> None:
        if self._loading:
            return
        self._save_settings()
        self._update_output_preview()

    def _on_sources_changed(self) -> None:
        if self._loading:
            return
        self._save_settings()
        self._update_output_preview()

    # ------------------------------------------------------------------
    # GPU detection
    # ------------------------------------------------------------------

    def start_detection(self) -> None:
        if self._detect_worker is not None:
            return
        self.detected_label.setText("Detecting GPU encoder...")
        self._detect_worker = GpuDetectWorker()
        self._detect_worker.detected.connect(self._on_gpu_detected)
        self._detect_worker.start()

    def _on_gpu_detected(self, key: str) -> None:
        self._detected_key = key
        self._detect_worker = None
        self._update_detected_label()
        if key == "cpu":
            self.append_log("Startup detection: no usable GPU encoder found.")
        else:
            self.append_log(f"Startup detection: {GPU_DISPLAY_BY_KEY[key]} available.")

    def _update_detected_label(self) -> None:
        if self._detected_key is None:
            self.detected_label.setText("Detecting GPU encoder...")
        elif self._detected_key == "cpu":
            self.detected_label.setText(
                "No GPU encoder detected \u2014 CPU encoding will be used."
            )
        else:
            self.detected_label.setText(
                f"GPU detected: {GPU_DISPLAY_BY_KEY[self._detected_key]}"
            )

    def _effective_encoder_display(self) -> str:
        choice = self.encoder_combo.currentText()
        if choice != AUTO_ENCODER_LABEL:
            return choice

        if self._detected_key is None:
            self._detected_key = FFmpegEngine().detect_gpu_encoder()
            self._update_detected_label()

        if self._detected_key == "cpu":
            display = CPU_ENCODER_DISPLAY
        else:
            display = GPU_DISPLAY_BY_KEY[self._detected_key]
        self.append_log(f"Auto-detected encoder: {display}")
        return display

    # ------------------------------------------------------------------
    # Output preview
    # ------------------------------------------------------------------

    def _update_output_preview(self) -> None:
        output_folder = self.output_folder_edit.text().strip()
        if not output_folder:
            self.preview_label.setText(
                "Select an output folder to preview the result path."
            )
            return

        stamp = make_timestamp()
        if self.content_stack.currentIndex() == 0:
            name = f"{compiler_output_name(stamp)}.mp4"
            self.preview_label.setText(f"Output: {os.path.join(output_folder, name)}")
            return

        media_path = self.silence_file_edit.text().strip()
        if not media_path:
            self.preview_label.setText(
                "Select a media file to preview the result path."
            )
            return

        stem = Path(media_path).stem
        extension = (
            ".m4a" if Path(media_path).suffix.lower() in AUDIO_EXTENSIONS else ".mp4"
        )
        name = silence_output_name(stem, extension, stamp)
        self.preview_label.setText(f"Output: {os.path.join(output_folder, name)}")

    # ------------------------------------------------------------------
    # Mode switching
    # ------------------------------------------------------------------

    def _show_compiler_mode(self) -> None:
        self.content_stack.setCurrentIndex(0)
        self._restore_status_label()
        self._update_output_preview()

    def _show_silence_mode(self) -> None:
        self.content_stack.setCurrentIndex(1)
        self._restore_status_label()
        self._update_output_preview()

    def _restore_status_label(self) -> None:
        if self._busy_owner is not None:
            return
        self.status_label.setStyleSheet("color: #22C55E;")
        if self.content_stack.currentIndex() == 0:
            self.status_label.setText("Ready \u2014 Add video sources to begin.")
        else:
            self.status_label.setText("Ready \u2014 Waiting for media file.")

    # ------------------------------------------------------------------
    # Browsing
    # ------------------------------------------------------------------

    def _browse_output_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Output Folder",
            self.output_folder_edit.text() or str(Path.home()),
        )
        if folder:
            self.output_folder_edit.setText(folder)
            self.append_log(f"Output folder set to: {folder}")

    def _browse_silence_file(self) -> None:
        start_dir = self.silence_file_edit.text() or str(Path.home())
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Media File", start_dir, MEDIA_FILTER
        )
        if file_path:
            self.silence_file_edit.setText(file_path)
            self.append_log(f"Silence removal file selected: {file_path}")

    def _on_silence_file_dropped(self, file_path: str) -> None:
        self.silence_file_edit.setText(file_path)
        self.append_log(f"Silence removal file dropped: {file_path}")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validated_output_folder(self) -> str | None:
        folder = self.output_folder_edit.text().strip()
        if not folder:
            QMessageBox.warning(
                self, "Missing Output Folder", "Please select an output folder."
            )
            return None
        if not Path(folder).exists():
            QMessageBox.warning(
                self, "Invalid Output Folder",
                f"The output folder does not exist:\n{folder}",
            )
            return None
        return folder

    @staticmethod
    def _overlaps_source(kind: str, path: str, output_folder: str) -> bool:
        target = path if kind == "dir" else str(Path(path).parent)
        return os.path.abspath(target) == os.path.abspath(output_folder)

    def _confirm_no_overlap(
        self, sources: list[tuple[str, str]], output_folder: str
    ) -> bool:
        for kind, path in sources:
            if self._overlaps_source(kind, path, output_folder):
                reply = QMessageBox.question(
                    self,
                    "Output Inside Source",
                    "The output folder is the same as a source folder.\n"
                    "Result files may appear in future compilations.\n\nContinue anyway?",
                )
                return reply == QMessageBox.StandardButton.Yes
        return True

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_compile_clicked(self) -> None:
        sources = self.source_list.get_sources()
        if not sources:
            QMessageBox.warning(
                self, "No Sources",
                "Add at least one video file or folder to compile.",
            )
            return

        output_folder = self._validated_output_folder()
        if output_folder is None:
            return
        if not self._confirm_no_overlap(sources, output_folder):
            return

        total_dur_seconds = self.total_duration_spin.value() * 60
        clip_dur = self.clip_duration_spin.value()
        if clip_dur > total_dur_seconds:
            QMessageBox.warning(
                self,
                "Invalid Clip Duration",
                "Duration per clip cannot be greater than the total compilation duration.",
            )
            return

        encoder = self._effective_encoder_display()
        portrait = self.aspect_combo.currentIndex() == 1
        scene_cut = self.scene_check.isChecked()
        crossfade = 0.5 if self.fade_check.isChecked() else 0.0

        self.compile_button.setEnabled(False)
        self.compile_button.setText("Planning...")
        self.append_log("Planning clips...")

        self.plan_requested.emit(
            sources, output_folder, total_dur_seconds, clip_dur,
            encoder, portrait, scene_cut, crossfade,
        )

    def _on_silence_clicked(self) -> None:
        file_path = self.silence_file_edit.text().strip()
        if not file_path:
            QMessageBox.warning(
                self, "Missing Media File",
                "Please drop or browse for a media file.",
            )
            return
        if not Path(file_path).exists():
            QMessageBox.warning(
                self, "Invalid Media File",
                f"The selected file does not exist:\n{file_path}",
            )
            return

        output_folder = self._validated_output_folder()
        if output_folder is None:
            return
        if not self._confirm_no_overlap([("file", file_path)], output_folder):
            return

        encoder = self._effective_encoder_display()
        threshold_db = self.threshold_spin.value()
        min_duration = self.min_duration_spin.value()
        padding = self.padding_spin.value()

        noise_on = self.noise_check.isChecked()
        noise_mode = (
            "ai" if "ai" in self.noise_mode_combo.currentText().lower() else "fft"
        )
        noise_strength = self.noise_strength_combo.currentText().lower()

        self.append_log("Starting silence removal...")
        self.silence_removal_requested.emit(
            file_path, output_folder, encoder, threshold_db,
            min_duration, padding, noise_on, noise_mode, noise_strength,
        )

    def _on_cancel_clicked(self) -> None:
        self.cancel_button.setText("Cancelling...")
        self.cancel_button.setEnabled(False)
        self.append_log("Cancellation requested...")
        self.cancellation_requested.emit()

    # ------------------------------------------------------------------
    # Job state (driven by Application via begin_job / end_job)
    # ------------------------------------------------------------------

    def begin_job(self, kind: str) -> None:
        """Mark a job as started. Only one owner drives the progress UI."""
        if self._busy_owner is None:
            self._busy_owner = kind
            self.progress_bar.setValue(0)
            self.progress_bar.setVisible(True)
            self.cancel_button.setVisible(True)
            self.cancel_button.setEnabled(True)
            self.cancel_button.setText("Cancel")
            self.status_label.setStyleSheet("color: #2563EB;")
            self.status_label.setText("Processing...")
            self._log_separator()
        elif self._busy_owner == "plan" and kind == "render":
            # Render supersedes planning-only ownership.
            self._busy_owner = "render"
            self.progress_bar.setValue(0)
            self.status_label.setStyleSheet("color: #2563EB;")
            self.status_label.setText("Processing...")

        if kind != "plan":
            self.compile_button.setEnabled(True)
            self.compile_button.setText(self.PLAN_BUTTON_TEXT)

    def end_job(self) -> None:
        """Reset all job visuals back to idle."""
        self._busy_owner = None
        self.progress_bar.setVisible(False)
        self.cancel_button.setVisible(False)
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Cancel")
        self.compile_button.setEnabled(True)
        self.compile_button.setText(self.PLAN_BUTTON_TEXT)
        self._restore_status_label()

    def update_queue_count(self, count: int) -> None:
        self._queued_count = count
        if count > 0:
            self.queue_status_label.setText(f"{count} job(s) queued")
            self.queue_status_label.setVisible(True)
        else:
            self.queue_status_label.setVisible(False)

    def _planning_cleanup(self) -> None:
        """Re-enable the Plan button when a plan attempt ends without render."""
        if self._busy_owner is None:
            self.compile_button.setEnabled(True)
            self.compile_button.setText(self.PLAN_BUTTON_TEXT)

    def _log_separator(self) -> None:
        from datetime import datetime

        timestamp = datetime.now().strftime("%H:%M:%S")
        self.append_log("")
        self.append_log(f"--- Session started at {timestamp} ---")

    # ------------------------------------------------------------------
    # Dialogs
    # ------------------------------------------------------------------

    def on_plan_ready(self, session) -> None:
        """Open the preview dialog for a freshly planned session."""
        dialog = ClipPreviewDialog(
            self, session, FFmpegEngine(), log_fn=self.append_log
        )
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if accepted and dialog.included_clips():
            self.render_requested.emit(session)
        else:
            if accepted and not dialog.included_clips():
                self.append_log("No clips selected \u2014 nothing to render.")
            self.discard_session_requested.emit(session)
            self.end_job()

    def _open_settings(self) -> None:
        settings = self._settings()
        engine = FFmpegEngine()
        dialog = SettingsDialog(
            self,
            ffmpeg_dir=settings.value("ffmpeg_dir", "", type=str),
            default_output=settings.value("default_output_folder", "", type=str),
            version_info=engine.get_ffmpeg_version(),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            if getattr(self, "_settings_reset_requested", False):
                self._settings_reset_requested = False
                self.append_log("All settings have been reset. Restart to fully apply.")
            return

        new_dir = dialog.ffmpeg_dir_value()
        new_default = dialog.default_output_value()
        settings.setValue("ffmpeg_dir", new_dir)
        settings.setValue("default_output_folder", new_default)

        FFmpegEngine.default_binary_dir = new_dir or None
        self.ffmpeg_override_changed.emit(new_dir)
        self.append_log(
            f"FFmpeg location updated: {new_dir or 'automatic'} "
            f"(resolved: {engine.get_resolved_ffmpeg_path()})"
        )

    def _open_history(self) -> None:
        HistoryDialog(self).exec()

    def _open_help(self) -> None:
        if self._detected_key is None:
            gpu_line = "GPU detection still running..."
        elif self._detected_key == "cpu":
            gpu_line = "CPU software encoding (no usable GPU encoder found)"
        else:
            gpu_line = GPU_DISPLAY_BY_KEY[self._detected_key]

        engine = FFmpegEngine()
        help_text = (
            "<h3>Clip Compiler</h3>"
            "<p>Add single video files and/or entire folders, then click "
            "<b>Plan Clips</b>. Review the planned clips (exclude / re-roll "
            "any of them) and confirm to render.</p>"
            "<h3>Silence Removal</h3>"
            "<p>Drop one media file (video or audio). Silent regions are "
            "removed and the remaining parts are merged into a cleaned copy.</p>"
            "<h3>Queue</h3>"
            "<p>Starting a job while another runs adds it to the queue; jobs "
            "are processed one by one.</p>"
            "<h3>Current Encoding</h3>"
            f"<p>{gpu_line}<br>"
            f"FFmpeg: {engine.get_resolved_ffmpeg_path()}</p>"
        )
        box = QMessageBox(self)
        box.setWindowTitle("Help \u2014 Smart Video Compiler")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(help_text)
        box.exec()

    # ------------------------------------------------------------------
    # Public slots
    # ------------------------------------------------------------------

    def set_phase(self, message: str) -> None:
        if self._busy_owner is not None:
            self.status_label.setText(message)

    def append_log(self, message: str) -> None:
        self.log_console.appendPlainText(message)
        self.log_console.verticalScrollBar().setValue(
            self.log_console.verticalScrollBar().maximum()
        )

    def update_progress(self, percent: int) -> None:
        self.progress_bar.setValue(max(0, min(100, percent)))
        if not self.progress_bar.isVisible():
            self.progress_bar.setVisible(True)

    def on_compilation_finished(self, output_path: str) -> None:
        self.end_job()
        self.append_log(f"Finished: {output_path}")

        box = QMessageBox(self)
        box.setWindowTitle("Completed")
        box.setText(f"Saved to:\n{output_path}")
        open_button = box.addButton("Open Folder", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
        box.exec()

        if box.clickedButton() is open_button:
            folder = str(Path(output_path).parent)
            try:
                os.startfile(folder)
            except OSError as exc:
                self.append_log(f"Could not open folder \"{folder}\": {exc}")

    def on_compilation_error(self, error_message: str) -> None:
        cancelled = error_message.strip() == "Cancelled by user."
        self.end_job()
        if cancelled:
            self.status_label.setStyleSheet("color: #DC2626;")
            self.status_label.setText("Cancelled by user.")
            self.append_log("Operation cancelled.")
            return

        self.status_label.setStyleSheet("color: #DC2626;")
        self.status_label.setText("Failed \u2014 see logs for details.")
        self.append_log(f"ERROR: {error_message}")
        QMessageBox.critical(
            self,
            "Compilation Failed",
            f"An error occurred during compilation:\n{error_message}",
        )


if __name__ == "__main__":
    app = QApplication([])
    window = MainWindow()
    window.show()
    app.exec()
