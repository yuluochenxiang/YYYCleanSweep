"""临时脚本：离屏渲染各页截图，用来检查主题配色与布局。"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtCore import QTimer                   # noqa: E402

from ui.main_window import MainWindow               # noqa: E402
from ui.theme import palette                        # noqa: E402

THEME = sys.argv[1] if len(sys.argv) > 1 else "light"
TAG = sys.argv[2] if len(sys.argv) > 2 else "before"

app = QApplication([])
w = MainWindow()
w.cfg.set("theme", THEME)
w.theme = THEME
w.pal = palette(THEME)
w._apply_theme()
w.resize(1280, 800)
w.show()


def shot(i, name, done):
    w.tabs.setCurrentIndex(i)
    app.processEvents()
    w.repaint()
    app.processEvents()
    path = "_shot_%s_%s_%s.png" % (TAG, THEME, name)
    w.grab().save(path)
    print("SAVED", path)
    done()


steps = []


def run_next():
    if not steps:
        app.quit()
        return
    i, name = steps.pop(0)
    QTimer.singleShot(500, lambda: shot(i, name, run_next))


steps = [(0, "clean"), (1, "big"), (2, "dir"), (3, "dup"),
         (5, "sysclean"), (6, "marks"), (7, "settings")]
QTimer.singleShot(3500, run_next)
app.exec()
