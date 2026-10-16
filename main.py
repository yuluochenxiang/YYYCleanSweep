"""YYYCleanSweep 磁盘清理工具 离线版 —— 入口。完全离线，无任何网络请求。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow
from ui.theme import THEMES
from core.version import APP_NAME


def main():
    """程序入口：起 Qt、套主题、开主窗口。全程不联网。"""
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(THEMES["light"])

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
