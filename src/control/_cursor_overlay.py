"""Overlay process for Marionette's own pointer: one small click-through arrow.

Runs under the system python (needs PySide6, which the venv does not ship).
It only draws; `control.cursor` finds the window by title and moves it with
kdotool, because a Wayland client cannot position its own toplevel.
"""
import sys

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QApplication, QWidget

TITLE = "Marionette Cursor"
SIZE = 28


class Arrow(QWidget):
    def __init__(self, color: str) -> None:
        super().__init__()
        self._color = QColor(color)
        self.setWindowTitle(TITLE)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                            | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.resize(SIZE, SIZE)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        tip = [(2, 2), (2, 22), (8, 17), (13, 26), (17, 24), (12, 15), (20, 15)]
        p.setBrush(self._color)
        p.setPen(QPen(QColor("white"), 1.5))
        p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in tip]))


def main() -> int:
    app = QApplication(sys.argv[:1])
    arrow = Arrow(sys.argv[1] if len(sys.argv) > 1 else "#d97757")
    arrow.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
