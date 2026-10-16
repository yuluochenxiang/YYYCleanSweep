"""把音效挂到界面上：按钮点击、勾选切换、页签切换、右键菜单等。

装在 QApplication 上的全局事件过滤，不需要给每个控件单独连线，
新加的按钮自动就有点击音。

分级原则：控件的"级别"越高（越主要、越需要用户确认），声音越长越饱满。
  · 主操作（开始扫描 / 开始清理 / 确认）→ confirm：三音上行
  · 危险操作（停止 / 删除 / 清空）      → warn：下行警告音
  · 正向操作（白名单 / 复用）            → keep：温和上行
  · 导航（页签 / 分类 / 下拉）           → nav：轻快二音
  · 勾选项                              → check_on / check_off
  · 其余普通按钮                        → click：极短促
右键菜单的每一条还可以用 setData("音效名") 单独指定，见 attach_menu()。
"""
from PySide6.QtCore import QObject, QEvent
from PySide6.QtWidgets import (
    QPushButton, QToolButton, QCheckBox, QRadioButton, QComboBox, QTabBar,
    QSlider, QMenu, QListWidget, QTreeWidget, QTableWidget,
)

# objectName -> 音效名。没列到的按钮走默认 click
BTN_LEVELS = {
    "primary": "confirm",     # 开始扫描 / 开始清理 / 确认执行
    "danger": "warn",         # 停止 / 永久删除 / 清空历史
    "warn": "warn",           # 继续扫描 / 标记可清理
    "ok": "keep",             # 白名单 / 复用历史
    "toggle": "toggle",       # 展开子目录 / 主题 / 关于
    "mini": "tick",           # 试听之类的小按钮
}


def sound_of_button(btn):
    """按按钮的功能级别取音效名。"""
    name = btn.objectName()
    if name in BTN_LEVELS:
        return BTN_LEVELS[name]
    return "click"


class NullSfx:
    """没有音效器时的空实现，省得到处写 if self.sfx。"""

    def play(self, *_a, **_k):
        """空实现：不发声（音效关闭或非 Windows 时用这个占位）。"""
        pass

    def configure(self, *_a, **_k):
        """空实现：忽略配置。"""
        pass

    @staticmethod
    def supported():
        """空实现：永远报「不支持」。"""
        return False


class SoundFilter(QObject):
    """全局事件过滤：按控件类型 / 级别播放对应的短音。"""

    def __init__(self, sfx, parent=None):
        super().__init__(parent)
        self.sfx = sfx

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.Type.MouseButtonPress:
            if isinstance(obj, (QPushButton, QToolButton)):
                self.sfx.play(sound_of_button(obj))
            elif isinstance(obj, (QCheckBox, QRadioButton)):
                self.sfx.play("tick")
            elif isinstance(obj, QComboBox):
                self.sfx.play("open")
            elif isinstance(obj, QSlider):
                self.sfx.play("tick")
            elif isinstance(obj, QListWidget):
                # 左边分类列表属于"换了个位置"，用导航级；其它列表还是轻点级
                self.sfx.play("nav" if obj.objectName() == "nav" else "tick")
            elif isinstance(obj, (QTreeWidget, QTableWidget)):
                self.sfx.play("tick")
        elif t == QEvent.Type.MouseButtonRelease:
            if isinstance(obj, QTabBar):
                self.sfx.play("nav")
        return False                      # 不拦截，事件照常往下走


def attach_menu(menu: QMenu, sfx, default="menu"):
    """给右键菜单挂音效。

    菜单里每一条 QAction 可以用 `act.setData("音效名")` 指定自己的声音，
    没指定的走 default。这样「删除」是警告音、「打开位置」是打开音、
    「加入白名单」是确认音，不用逐个 connect。
    """
    def _on(act):
        name = act.data()
        if not isinstance(name, str) or not name:
            name = default
        sfx.play(name)
    menu.triggered.connect(_on)
    return menu


def act(menu, text, snd=None, fn=None, enabled=True):
    """造一条菜单项，顺手指定它的音效与回调。"""
    a = menu.addAction(text)
    if snd:
        a.setData(snd)
    if fn:
        a.triggered.connect(fn)
    a.setEnabled(enabled)
    return a
