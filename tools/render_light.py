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
    """切到第 i 个页签、强制重绘后整窗截图。

    中间那两次 processEvents + repaint 不能省：离屏模式下控件不会自己排布，
    不手动刷一遍截出来是空白或半截。
    """
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
    """一个个页签接着截。延迟 500ms 是为了等懒加载/缩略图那一批异步活干完。"""
    if not steps:
        app.quit()
        return
    i, name = steps.pop(0)
    QTimer.singleShot(500, lambda: shot(i, name, run_next))


steps = [(0, "clean"), (1, "big"), (2, "dir"), (3, "dup"),
         (5, "sysclean"), (6, "marks"), (7, "settings")]
QTimer.singleShot(3500, run_next)
app.exec()
