"""统一的「扫描中」表现：旋转指示器 + 流动条纹 + 状态条。

以前每个页面各写一套扫描提示——清理页写进顶栏的一行小字、目录分析只有一根
5px 的进度条、大文件只有一行 label、重复文件又是另一种。结果就是"到底扫没扫"
全靠猜。现在四个页面共用同一个 ScanBanner：

    旋转指示器 │ 主文案（粗）    │ 已用 3.2s  │ 停止
               │ 副文案（当前项）│            │
    ─────────────────────────────────────────
    流动条纹（不知道总量时） / 实心进度条（知道总量时）

卡片左侧有一条强调色竖条，扫描时整块很显眼，不会被人忽略。
"""
import time

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QLinearGradient
from PySide6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
)

from ui.theme import palette, DEFAULT_THEME


class Spinner(QWidget):
    """自绘旋转圆弧。比 QProgressBar 的 busy 模式在深色下更清楚。"""

    def __init__(self, parent=None, size=22, thick=3, span=110):
        super().__init__(parent)
        self.pal = palette(DEFAULT_THEME)
        self.thick = thick
        self.span = span
        self._a = 0
        self.setFixedSize(size, size)
        self._timer = QTimer(self)
        self._timer.setInterval(55)
        self._timer.timeout.connect(self._tick)

    def set_palette(self, pal):
        """换主题配色（扫描条跟着主题走）。"""
        self.pal = pal
        self.update()

    def start(self):
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self.update()

    def _tick(self):
        self._a = (self._a + 34) % 360
        self.update()

    def paintEvent(self, _e):
        p = self.pal
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        m = self.thick / 2.0 + 1.0
        r = QRectF(m, m, self.width() - 2 * m, self.height() - 2 * m)
        pen = QPen(QColor(p["track"]), self.thick)
        painter.setPen(pen)
        painter.drawArc(r, 0, 360 * 16)
        pen = QPen(QColor(p["accent"]), self.thick)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        # drawArc 的角度是从 3 点方向逆时针算的，取负角让它顺时针转
        painter.drawArc(r, int(-self._a * 16), int(self.span * 16))
        painter.end()


class ProgressStrip(QWidget):
    """底部那根 5px 的条：有总量时画实心进度，没总量时画流动条纹。"""

    def __init__(self, parent=None, height=5):
        super().__init__(parent)
        self.pal = palette(DEFAULT_THEME)
        self._off = 0.0
        self._val = None          # None = 走流动条纹
        self._max = 0
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._timer = QTimer(self)
        self._timer.setInterval(38)
        self._timer.timeout.connect(self._tick)

    def set_palette(self, pal):
        """换主题配色（扫描条跟着主题走）。"""
        self.pal = pal
        self.update()

    def start(self):
        self._off = 0.0
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self.update()

    def set_value(self, v, maximum=0):
        """maximum > 0 时用实心进度；否则恢复流动条纹。"""
        if maximum > 0:
            self._val = max(0.0, min(1.0, float(v) / float(maximum)))
            self.stop()
        else:
            self._val = None
        self.update()

    def clear_value(self):
        """清掉右侧数值，回到「未开始」的占位状态。"""
        self._val = None
        self.update()

    def _tick(self):
        self._off += 3.0
        self.update()

    def paintEvent(self, _e):
        p = self.pal
        w, h = self.width(), self.height()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        # 左右各留一点：底条若铺满整宽，会把外层横幅（QFrame#card）的
        # 圆角顶成方形，看着像圆角被啃掉一块
        m = 7.0
        iw = max(0.0, w - 2 * m)
        painter.setBrush(QColor(p["track"]))
        painter.drawRoundedRect(QRectF(m, 0, iw, h), h / 2.0, h / 2.0)
        painter.setClipRect(QRectF(m, 0, iw, h))
        if self._val is not None:
            painter.setBrush(QColor(p["accent"]))
            painter.drawRoundedRect(
                QRectF(m, 0, max(h, iw * self._val), h), h / 2.0, h / 2.0)
            painter.end()
            return
        seg = max(56.0, iw * 0.34)
        period = iw + seg
        for k in (0.0, 0.5):
            x = m + (((self._off + k * period) % period) - seg)
            g = QLinearGradient(QPointF(x, 0), QPointF(x + seg, 0))
            c = QColor(p["accent"])
            g.setColorAt(0.0, QColor(c.red(), c.green(), c.blue(), 0))
            g.setColorAt(0.5, c)
            g.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 0))
            painter.setBrush(g)
            painter.drawRoundedRect(QRectF(x, 0, seg, h), h / 2.0, h / 2.0)
        painter.end()


class ScanBanner(QFrame):
    """四个扫描页面共用的状态条。"""

    def __init__(self, parent=None, title="正在扫描", danger=False,
                 stop_label="停止"):
        super().__init__(parent)
        self.setObjectName("card")
        self._danger = danger
        self._stop_label = stop_label
        self.pal = palette(DEFAULT_THEME)
        self._t0 = 0.0
        self._stop_fn = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        inner = QWidget()
        h = QHBoxLayout(inner)
        h.setContentsMargins(14, 9, 14, 9)
        h.setSpacing(12)

        self.spinner = Spinner(inner, 22)
        h.addWidget(self.spinner)

        box = QVBoxLayout()
        box.setSpacing(2)
        self.lb_main = QLabel(title)
        self.lb_main.setObjectName("h2")
        self.lb_sub = QLabel("")
        self.lb_sub.setObjectName("hint")
        box.addWidget(self.lb_main)
        box.addWidget(self.lb_sub)
        h.addLayout(box, 1)

        self.lb_time = QLabel("")
        self.lb_time.setObjectName("num")
        h.addWidget(self.lb_time)

        self.btn_stop = QPushButton(stop_label)
        self.btn_stop.setObjectName("danger")
        self.btn_stop.hide()
        self.btn_stop.clicked.connect(self._on_stop)
        h.addWidget(self.btn_stop)
        outer.addWidget(inner)

        self.strip = ProgressStrip(self, 5)
        outer.addWidget(self.strip)

        self._timer = QTimer(self)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._elapsed)
        self.hide()
        # 让 danger 配色在初始化时就生效（danger=True 时切红色强调色）
        self.set_palette(self.pal)

    # ---------------------------------------------------------------- 控制
    def set_palette(self, pal):
        """换主题配色（删除条跟着主题走）。"""
        self.pal = dict(pal)
        if self._danger:
            # 删除场景用危险色（红），和"正在扫描"的蓝强调色在视觉上区分开
            self.pal["accent"] = pal["risk"]["danger"]
        self.spinner.set_palette(self.pal)
        self.strip.set_palette(self.pal)
        # 左侧那条强调色竖条让整块在页面里很显眼
        self.setStyleSheet(
            f"QFrame#card {{ background:{self.pal['panel']};"
            f" border:1px solid {self.pal['border']};"
            f" border-left:4px solid {self.pal['accent']};"
            f" border-radius:12px; }}")

    def start(self, main, sub="", on_stop=None, maximum=0):
        self.lb_main.setText(main)
        self.lb_sub.setText(sub or "")
        self.lb_time.setText("")          # 清掉上一轮留下的"耗时"
        self._t0 = time.time()
        self._stop_fn = on_stop
        self.btn_stop.setVisible(on_stop is not None)
        self.btn_stop.setEnabled(True)
        self.strip.clear_value()
        if maximum > 0:
            self.strip.set_value(0, maximum)
            self.strip.stop()
        else:
            self.strip.start()
        self.spinner.start()
        self._timer.start()
        self.show()

    def set_sub(self, text, value=None, maximum=None):
        """更新副标题；给了 value/maximum 就顺便显示进度。"""
        self.lb_sub.setText(text or "")
        if value is not None:
            self.strip.set_value(value, maximum or 0)

    def _elapsed(self):
        if not self._t0:
            return
        s = time.time() - self._t0
        self.lb_time.setText(f"{s:.1f}s" if s < 100 else f"{int(s)}s")

    def _on_stop(self):
        if self._stop_fn:
            self.btn_stop.setEnabled(False)
            self.lb_sub.setText("正在停止…")
            self._stop_fn()

    def finish(self, main, sub=""):
        """扫完了：停掉动画，切成一句静态文案，由调用方决定何时隐藏。"""
        self.spinner.stop()
        self.strip.stop()
        self._timer.stop()
        self.lb_main.setText(main)
        self.lb_sub.setText(sub or "")
        self.btn_stop.hide()
        secs = time.time() - self._t0 if self._t0 else 0
        self.lb_time.setText(f"耗时 {secs:.1f}s" if secs else "")


class DeleteBanner(ScanBanner):
    """删除场景专用状态条：危险色（红）强调 + "正在删除" 标题 + "取消" 按钮。

    与 ScanBanner 完全同构（旋转指示 + 进度条 + 计时 + 取消），只是把强调色
    换成危险红，让"正在删除"和"正在扫描"在视觉上一眼区分。
    """

    def __init__(self, parent=None, title="正在删除"):
        super().__init__(parent, title=title, danger=True, stop_label="取消")
