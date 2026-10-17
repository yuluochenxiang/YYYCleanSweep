"""YYYCleanSweep 磁盘清理工具 离线版 —— 入口。完全离线，无任何网络请求。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    """程序入口。

    先看有没有命令行参数：有的话走无界面扫描分支，连 PySide6 都不加载
    （命令行的意义之一就是别把这几十 MB 的界面库拖起来）。
    没有参数才是正常开窗口。全程不联网。
    """
    from core.cli import has_cli_args, main_cli

    if has_cli_args():
        sys.exit(main_cli())

    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow
    from ui.theme import THEMES
    from core.version import APP_NAME

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(THEMES["light"])

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
