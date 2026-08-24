"""
Input widgets that ignore mouse-wheel events.

Scrolling the main page with the cursor hovering above a combo box or
spin box no longer changes its value — the wheel event propagates to the
parent ``QScrollArea`` instead. Values are still changed by clicking,
typing, or the spin buttons as usual.
"""

from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QSpinBox


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event) -> None:
        event.ignore()


class NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event) -> None:
        event.ignore()


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event) -> None:
        event.ignore()
