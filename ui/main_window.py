import subprocess
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)


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

        title = QLabel("Drag and drop your video file here")
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

        video_extensions = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a", ".wma"}
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if Path(path).suffix.lower() in video_extensions:
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


class MainWindow(QMainWindow):
    """Primary application window for the Smart Video Compiler desktop UI."""

    # Public widget references for external access
    compile_pill: QPushButton
    silence_pill: QPushButton
    compiler_content: QWidget
    silence_content: QWidget
    input_folder_edit: QLineEdit
    output_folder_edit: QLineEdit
    total_duration_spin: QSpinBox
    clip_duration_spin: QSpinBox
    encoder_combo: QComboBox
    silence_encoder_combo: QComboBox
    silence_file_edit: QLineEdit
    silence_drop_zone: DropZone
    threshold_spin: QSpinBox
    min_duration_spin: QDoubleSpinBox
    padding_spin: QDoubleSpinBox
    compile_button: QPushButton
    silence_button: QPushButton
    cancel_button: QPushButton
    progress_bar: QProgressBar
    status_label: QLabel
    log_console: QPlainTextEdit

    # Public signals
    compilation_requested = Signal(str, str, int, int, str)
    silence_removal_requested = Signal(str, str, str, int, float, float)
    cancellation_requested = Signal()

    @staticmethod
    def _detect_gpu_encoder_display() -> str:
        """Run ffmpeg -encoders to detect available GPU, return display name."""
        key_to_display = {
            "nvenc": "NVIDIA NVENC (h264_nvenc)",
            "qsv": "Intel QSV (h264_qsv)",
            "amf": "AMD AMF (h264_amf)",
            "cpu": "CPU Software (libx264)",
        }
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=5,
            )
            output = result.stdout.lower() + result.stderr.lower()
            for key in ("nvenc", "qsv", "amf"):
                if f"h264_{key}" in output:
                    return key_to_display[key]
        except Exception:
            pass
        return key_to_display["cpu"]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.setWindowTitle("Smart Video Compiler")
        self.setMinimumSize(780, 550)
        self.resize(960, 700)

        self._build_ui()
        self._apply_stylesheet()
        self._load_output_folder()

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

        # App icon + title
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

        # Mode switcher pills
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

        # Settings + help placeholders
        settings_button = QPushButton("⚙")
        settings_button.setObjectName("icon-button")
        settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_button.setToolTip("Settings")

        help_button = QPushButton("?")
        help_button.setObjectName("icon-button")
        help_button.setCursor(Qt.CursorShape.PointingHandCursor)
        help_button.setToolTip("Help")

        layout.addWidget(settings_button)
        layout.addSpacing(12)
        layout.addWidget(help_button)

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

        # Mode-specific content
        self.content_stack = QStackedWidget()
        self.compiler_content = self._create_compiler_content()
        self.silence_content = self._create_silence_content()
        self.content_stack.addWidget(self.compiler_content)
        self.content_stack.addWidget(self.silence_content)
        scroll_layout.addWidget(self.content_stack)

        # Progress Logs card
        progress_card = self._create_progress_log_card()
        scroll_layout.addWidget(progress_card)

        # Footer
        footer = self._create_footer()
        scroll_layout.addWidget(footer)

        scroll.setWidget(scroll_content)
        return scroll

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

    def _create_compiler_content(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        # Page title
        title = self._create_mode_title(
            "Clip Compiler",
            "Remove silence and compile the best clips into one video.",
        )
        layout.addWidget(title)

        # Input & Output card
        io_card, io_layout = self._create_card("Input & Output")
        io_grid = QGridLayout()
        io_grid.setVerticalSpacing(14)
        io_grid.setHorizontalSpacing(8)

        input_label = QLabel("Input Folder")
        input_label.setObjectName("field-label")
        input_label.setFixedWidth(110)
        self.input_folder_edit = FolderDropLineEdit(on_folder_dropped=self._on_input_folder_dropped)
        self.input_folder_edit.setReadOnly(True)
        self.input_folder_edit.setPlaceholderText("Select a folder containing source videos...")
        input_browse = QPushButton("Browse...")
        input_browse.setObjectName("secondary-button")
        input_browse.setFixedWidth(90)
        input_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        input_browse.clicked.connect(self._browse_input_folder)

        output_label = QLabel("Output Folder")
        output_label.setObjectName("field-label")
        output_label.setFixedWidth(110)
        self.output_folder_edit = QLineEdit()
        self.output_folder_edit.setReadOnly(True)
        self.output_folder_edit.setPlaceholderText("Choose where the final video will be saved...")
        output_browse = QPushButton("Browse...")
        output_browse.setObjectName("secondary-button")
        output_browse.setFixedWidth(90)
        output_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        output_browse.clicked.connect(self._browse_output_folder)

        io_grid.addWidget(input_label, 0, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
        io_grid.addWidget(self.input_folder_edit, 0, 1)
        io_grid.addWidget(input_browse, 0, 2)
        io_grid.addWidget(output_label, 1, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
        io_grid.addWidget(self.output_folder_edit, 1, 1)
        io_grid.addWidget(output_browse, 1, 2)
        io_layout.addLayout(io_grid)
        layout.addWidget(io_card)

        # Compilation Settings card
        settings_card, settings_layout = self._create_card("Compilation Settings")
        settings_grid = QGridLayout()
        settings_grid.setVerticalSpacing(12)
        settings_grid.setHorizontalSpacing(10)

        total_label = QLabel("Total Compilation Duration")
        total_label.setObjectName("field-label")
        total_label.setFixedWidth(160)
        self.total_duration_spin = QSpinBox()
        self.total_duration_spin.setRange(1, 10)
        self.total_duration_spin.setSuffix(" min")
        self.total_duration_spin.setValue(5)
        self.total_duration_spin.setFixedWidth(90)

        clip_label = QLabel("Duration per Clip")
        clip_label.setObjectName("field-label")
        clip_label.setFixedWidth(160)
        self.clip_duration_spin = QSpinBox()
        self.clip_duration_spin.setRange(1, 60)
        self.clip_duration_spin.setSuffix(" sec")
        self.clip_duration_spin.setValue(10)
        self.clip_duration_spin.setFixedWidth(90)

        encoder_label = QLabel("Hardware Encoder")
        encoder_label.setObjectName("field-label")
        encoder_label.setFixedWidth(160)
        self.encoder_combo = QComboBox()
        self.encoder_combo.addItems(
            [
                "NVIDIA NVENC (h264_nvenc)",
                "Intel QSV (h264_qsv)",
                "AMD AMF (h264_amf)",
                "CPU Software (libx264)",
            ]
        )
        self.encoder_combo.insertItem(0, "Auto (Detect)")
        self.encoder_combo.setCurrentIndex(0)
        self.encoder_combo.setFixedWidth(260)
        self.encoder_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        settings_grid.addWidget(total_label, 0, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
        settings_grid.addWidget(self.total_duration_spin, 0, 1, alignment=Qt.AlignmentFlag.AlignLeft)
        settings_grid.addWidget(clip_label, 1, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
        settings_grid.addWidget(self.clip_duration_spin, 1, 1, alignment=Qt.AlignmentFlag.AlignLeft)
        settings_grid.addWidget(encoder_label, 2, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
        settings_grid.addWidget(self.encoder_combo, 2, 1, alignment=Qt.AlignmentFlag.AlignLeft)
        settings_layout.addLayout(settings_grid)
        layout.addWidget(settings_card)

        # Action button
        self.compile_button = QPushButton("Start Compilation")
        self.compile_button.setMinimumHeight(44)
        self.compile_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.compile_button.clicked.connect(self._on_compile_clicked)
        self.compile_button.setStyleSheet(
            "QPushButton { background-color: #2563EB; color: #FFFFFF;"
            " border: none; border-radius: 8px; font-size: 14px; font-weight: 600;"
            " padding: 10px 24px; }"
            "QPushButton:hover { background-color: #1D4ED8; }"
            "QPushButton:pressed { background-color: #1E40AF; }"
            "QPushButton:disabled { background-color: #93C5FD; color: #FFFFFF; }"
        )
        layout.addWidget(self.compile_button)

        return widget

    def _create_silence_content(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        # Page title
        title = self._create_mode_title(
            "Silence Removal",
            "Remove silent parts from a single video and save the trimmed result.",
        )
        layout.addWidget(title)

        # Select Source Video card
        source_card, source_layout = self._create_card("Select Source Video")

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
        self.silence_file_edit.setPlaceholderText("No video file selected...")
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

        # Silence Detection Parameters card
        params_card, params_layout = self._create_card("Silence Detection Parameters")
        params_grid = QGridLayout()
        params_grid.setVerticalSpacing(12)
        params_grid.setHorizontalSpacing(10)

        threshold_label = QLabel("Threshold (dB)")
        threshold_label.setObjectName("field-label")
        threshold_label.setFixedWidth(160)
        self.threshold_spin = QSpinBox()
        self.threshold_spin.setRange(-60, 0)
        self.threshold_spin.setSuffix(" dB")
        self.threshold_spin.setValue(-30)
        self.threshold_spin.setFixedWidth(90)

        min_dur_label = QLabel("Minimum Duration (s)")
        min_dur_label.setObjectName("field-label")
        min_dur_label.setFixedWidth(160)
        self.min_duration_spin = QDoubleSpinBox()
        self.min_duration_spin.setRange(0.1, 10.0)
        self.min_duration_spin.setSuffix(" s")
        self.min_duration_spin.setValue(0.5)
        self.min_duration_spin.setSingleStep(0.1)
        self.min_duration_spin.setDecimals(1)
        self.min_duration_spin.setFixedWidth(90)

        padding_label = QLabel("Padding (s)")
        padding_label.setObjectName("field-label")
        padding_label.setFixedWidth(160)
        self.padding_spin = QDoubleSpinBox()
        self.padding_spin.setRange(0.0, 5.0)
        self.padding_spin.setSuffix(" s")
        self.padding_spin.setValue(0.0)
        self.padding_spin.setSingleStep(0.1)
        self.padding_spin.setDecimals(1)
        self.padding_spin.setFixedWidth(90)

        encoder_label = QLabel("Hardware Encoder")
        encoder_label.setObjectName("field-label")
        encoder_label.setFixedWidth(160)
        self.silence_encoder_combo = QComboBox()
        self.silence_encoder_combo.addItems(
            [
                "NVIDIA NVENC (h264_nvenc)",
                "Intel QSV (h264_qsv)",
                "AMD AMF (h264_amf)",
                "CPU Software (libx264)",
            ]
        )
        self.silence_encoder_combo.insertItem(0, "Auto (Detect)")
        self.silence_encoder_combo.setCurrentIndex(0)
        self.silence_encoder_combo.setFixedWidth(260)
        self.silence_encoder_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        params_grid.addWidget(threshold_label, 0, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
        params_grid.addWidget(self.threshold_spin, 0, 1, alignment=Qt.AlignmentFlag.AlignLeft)
        params_grid.addWidget(min_dur_label, 1, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
        params_grid.addWidget(self.min_duration_spin, 1, 1, alignment=Qt.AlignmentFlag.AlignLeft)
        params_grid.addWidget(padding_label, 2, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
        params_grid.addWidget(self.padding_spin, 2, 1, alignment=Qt.AlignmentFlag.AlignLeft)
        params_grid.addWidget(encoder_label, 3, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
        params_grid.addWidget(self.silence_encoder_combo, 3, 1, alignment=Qt.AlignmentFlag.AlignLeft)
        params_layout.addLayout(params_grid)

        layout.addWidget(params_card)

        # Action button
        self.silence_button = QPushButton("Remove Silence & Save")
        self.silence_button.setMinimumHeight(44)
        self.silence_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.silence_button.clicked.connect(self._on_silence_clicked)
        self.silence_button.setStyleSheet(
            "QPushButton { background-color: #2563EB; color: #FFFFFF;"
            " border: none; border-radius: 8px; font-size: 14px; font-weight: 600;"
            " padding: 10px 24px; }"
            "QPushButton:hover { background-color: #1D4ED8; }"
            "QPushButton:pressed { background-color: #1E40AF; }"
            "QPushButton:disabled { background-color: #93C5FD; color: #FFFFFF; }"
        )
        layout.addWidget(self.silence_button)

        return widget

    def _create_progress_log_card(self) -> QFrame:
        card, layout = self._create_card("Progress Logs")

        self.status_label = QLabel("Ready — Waiting for input folder...")
        self.status_label.setObjectName("status-label")
        layout.addWidget(self.status_label)

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
        # Direct stylesheet overrides palette — guaranteed to work
        self.log_console.setStyleSheet(
            "QPlainTextEdit { background-color: #F5F5F5; color: #111827;"
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

        copyright = QLabel("© 2024 Smart Video Compiler — Desktop Pro Edition")
        copyright.setObjectName("footer-label")

        links = QLabel("Documentation    Support    Release Notes")
        links.setObjectName("footer-link")

        layout.addWidget(copyright)
        layout.addStretch()
        layout.addWidget(links)

        return widget

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

            QCheckBox {
                spacing: 8px;
                color: #4B5563;
                font-size: 12px;
            }

            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 4px;
                border: 1px solid #D1D5DB;
                background-color: #FFFFFF;
            }

            QCheckBox::indicator:checked {
                background-color: #2563EB;
                border: 1px solid #2563EB;
            }

            QCheckBox::indicator:hover {
                border: 1px solid #93C5FD;
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

            #log-console {
                background-color: #2D2D2D;
                color: #D1D5DB;
                border: none;
                border-radius: 6px;
                padding: 12px;
                font-family: "JetBrains Mono", "Consolas", "Courier New", monospace;
                font-size: 13px;
                line-height: 1.5;
                selection-background-color: #4B5563;
            }

            #log-console QScrollBar:vertical {
                background-color: #3D3D3D;
                width: 12px;
                border-radius: 6px;
            }

            #log-console QScrollBar::handle:vertical {
                background-color: #6B7280;
                border-radius: 6px;
                min-height: 28px;
            }

            #log-console QScrollBar::handle:vertical:hover {
                background-color: #9CA3AF;
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
    # Settings persistence
    # ------------------------------------------------------------------
    def _load_output_folder(self) -> None:
        settings = QSettings("SmartVideoCompiler", "MainWindow")
        saved_output = settings.value("output_folder", "")
        if saved_output and Path(saved_output).exists():
            self.output_folder_edit.setText(saved_output)

    # ------------------------------------------------------------------
    # Internal behavior
    # ------------------------------------------------------------------
    def _show_compiler_mode(self) -> None:
        self.content_stack.setCurrentIndex(0)
        self.status_label.setText("Ready — Waiting for input folder...")

    def _show_silence_mode(self) -> None:
        self.content_stack.setCurrentIndex(1)
        self.status_label.setText("Ready — Waiting for video file...")

    def _browse_input_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Input Folder",
            self.input_folder_edit.text() or str(Path.home()),
        )
        if folder:
            self.input_folder_edit.setText(folder)
            self.append_log(f"Input folder set to: {folder}")

    def _on_input_folder_dropped(self, folder: str) -> None:
        self.append_log(f"Input folder set to: {folder}")

    def _browse_output_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Output Folder",
            self.output_folder_edit.text() or str(Path.home()),
        )
        if folder:
            self.output_folder_edit.setText(folder)
            QSettings("SmartVideoCompiler", "MainWindow").setValue("output_folder", folder)
            self.append_log(f"Output folder set to: {folder}")

    def _browse_silence_file(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Video File",
            str(Path.home()),
            "All Media Files (*.mp4 *.mov *.avi *.mkv *.webm *.m4v *.wmv *.mp3 *.wav *.aac *.flac *.ogg *.m4a *.wma)",
        )
        if file_path:
            self.silence_file_edit.setText(file_path)
            self.append_log(f"Silence removal file selected: {file_path}")

    def _on_silence_file_dropped(self, file_path: str) -> None:
        self.silence_file_edit.setText(file_path)
        self.append_log(f"Silence removal file dropped: {file_path}")

    def _validate_folders(self) -> tuple[str, str] | None:
        """Return (input_folder, output_folder) if both are valid, otherwise None."""
        input_folder = self.input_folder_edit.text().strip()
        output_folder = self.output_folder_edit.text().strip()

        if not input_folder:
            QMessageBox.warning(self, "Missing Input Folder", "Please select an input folder.")
            return None

        if not Path(input_folder).exists():
            QMessageBox.warning(self, "Invalid Input Folder", f"The input folder does not exist:\n{input_folder}")
            return None

        if not output_folder:
            QMessageBox.warning(self, "Missing Output Folder", "Please select an output folder.")
            return None

        if not Path(output_folder).exists():
            QMessageBox.warning(self, "Invalid Output Folder", f"The output folder does not exist:\n{output_folder}")
            return None

        return input_folder, output_folder

    def _set_processing_state(self, processing: bool) -> None:
        self.compile_button.setEnabled(not processing)
        self.silence_button.setEnabled(not processing)

        if processing:
            self.progress_bar.setValue(0)
            self.progress_bar.setVisible(True)
            self.cancel_button.setVisible(True)
            self.cancel_button.setEnabled(True)
            self.cancel_button.setText("Cancel")
            self.status_label.setText("Processing...")
            self.status_label.setStyleSheet("color: #2563EB;")
            self._log_separator()
        else:
            self.progress_bar.setVisible(False)
            self.cancel_button.setVisible(False)
            self.cancel_button.setEnabled(True)
            self.cancel_button.setText("Cancel")
            self.compile_button.setText("Start Compilation")
            self.silence_button.setText("Remove Silence & Save")
            self.status_label.setStyleSheet("color: #22C55E;")
            if self.content_stack.currentIndex() == 0:
                self.status_label.setText("Ready — Waiting for input folder...")
            else:
                self.status_label.setText("Ready — Waiting for video file...")

    def _log_separator(self) -> None:
        from datetime import datetime
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.append_log("")
        self.append_log(f"--- Session started at {timestamp} ---")

    def _on_compile_clicked(self) -> None:
        folders = self._validate_folders()
        if folders is None:
            return

        input_folder, output_folder = folders
        total_dur_seconds = self.total_duration_spin.value() * 60
        clip_dur = self.clip_duration_spin.value()
        encoder = self.encoder_combo.currentText()
        if encoder == "Auto (Detect)":
            encoder = self._detect_gpu_encoder_display()
            self.append_log(f"Auto-detected encoder: {encoder}")

        if clip_dur > total_dur_seconds:
            QMessageBox.warning(
                self,
                "Invalid Clip Duration",
                "Duration per clip cannot be greater than the total compilation duration.",
            )
            return

        self.compile_button.setText("Processing...")
        self._set_processing_state(True)

        self.append_log("Starting clip compilation...")
        self.compilation_requested.emit(input_folder, output_folder, total_dur_seconds, clip_dur, encoder)

    def _on_silence_clicked(self) -> None:
        file_path = self.silence_file_edit.text().strip()
        output_folder = self.output_folder_edit.text().strip()
        encoder = self.silence_encoder_combo.currentText()
        threshold_db = self.threshold_spin.value()
        min_duration = self.min_duration_spin.value()
        padding = self.padding_spin.value()

        if encoder == "Auto (Detect)":
            encoder = self._detect_gpu_encoder_display()
            self.append_log(f"Auto-detected encoder: {encoder}")

        if not file_path:
            QMessageBox.warning(self, "Missing Video File", "Please drop or browse for a video file.")
            return

        if not Path(file_path).exists():
            QMessageBox.warning(self, "Invalid Video File", f"The selected file does not exist:\n{file_path}")
            return

        if not output_folder:
            QMessageBox.warning(self, "Missing Output Folder", "Please select an output folder.")
            return

        if not Path(output_folder).exists():
            QMessageBox.warning(self, "Invalid Output Folder", f"The output folder does not exist:\n{output_folder}")
            return

        self.silence_button.setText("Processing...")
        self._set_processing_state(True)

        self.append_log("Starting silence removal...")
        self.silence_removal_requested.emit(file_path, output_folder, encoder, threshold_db, min_duration, padding)

    def _on_cancel_clicked(self) -> None:
        self.cancel_button.setText("Cancelling...")
        self.cancel_button.setEnabled(False)
        self.append_log("Cancellation requested...")
        self.cancellation_requested.emit()

    # ------------------------------------------------------------------
    # Public slots
    # ------------------------------------------------------------------
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
        self._set_processing_state(False)
        self.append_log(f"Compilation finished: {output_path}")
        QMessageBox.information(
            self,
            "Compilation Complete",
            f"Your compiled video has been saved to:\n{output_path}",
        )

    def on_compilation_error(self, error_message: str) -> None:
        self._set_processing_state(False)
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
