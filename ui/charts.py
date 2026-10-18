"""轻量图表控件：环形占比图 + 类型构成条形图。

不引第三方绘图库，全部用 QPainter 画，保证打包体积小、主题切换即时生效。
"""
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QFont
from PySide6.QtWidgets import QWidget, QVBoxLayout, QSizePolicy

from ui.theme import palette, DEFAULT_THEME
from ui.widgets import fmt, with_alpha
from core.sizefmt import fmt_bytes, fmt_pct, pct_of


class Donut(QWidget):
    """环形占比图：中间大字是容量，环上是占父级的比例。"""

    def __init__(self, parent=None, size=124, thickness=13):
        super().__init__(parent)
        self.pal = palette(DEFAULT_THEME)
        self.frac = 0.0
        self.center = "—"
        self.sub = ""
        self.color = self.pal["accent"]
        self._th = thickness
        self.setFixedSize(size, size)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def set_palette(self, p):
        """换主题配色。"""
        self.pal = p
        self.update()

    def set(self, frac, center, sub="", color=None):
        """设置环形图：占比、中心文字、副标题与配色。"""
        self.frac = 0.0 if frac is None else max(0.0, min(1.0, float(frac)))
        self.center = center or "—"
        self.sub = sub or ""
        self.color = color or self.pal["accent"]
        self.update()

    def paintEvent(self, e):
        p = self.pal
        side = min(self.width(), self.height())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        m = self._th / 2.0 + 2
        r = QRectF(m, m, side - 2 * m, side - 2 * m)

        # 底环
        pen = QPen(QColor(p["track"]), self._th)
        painter.setPen(pen)
        painter.drawArc(r, 0, 360 * 16)

        # 占比弧：从正上方顺时针
        if self.frac > 0:
            pen = QPen(QColor(self.color), self._th)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.drawArc(r, 90 * 16, -int(self.frac * 360 * 16))

        # 中间文字
        painter.setPen(QColor(p["text"]))
        f = QFont(self.font())
        f.setPointSize(13)
        f.setBold(True)
        painter.setFont(f)
        painter.drawText(QRectF(0, side * 0.30, side, side * 0.24),
                         Qt.AlignCenter, self.center)
        if self.sub:
            f2 = QFont(self.font())
            f2.setPointSize(8)
            painter.setFont(f2)
            painter.setPen(QColor(p["text_faint"]))
            painter.drawText(QRectF(0, side * 0.54, side, side * 0.20),
                             Qt.AlignCenter, self.sub)
        painter.end()


class _BarRow(QWidget):
    """一行类型构成：底色条 + 名称 + 大小 + 占比。"""

    H = 24

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pal = palette(DEFAULT_THEME)
        self.name = ""
        self.color = "#888888"
        self.size = 0
        self.count = 0
        self.frac = 0.0
        self.total = 0
        self.setFixedHeight(self.H)
        self.setMouseTracking(True)

    def set_palette(self, p):
        """换主题配色。"""
        self.pal = p
        self.update()

    def set_data(self, name, color, size, count, total):
        """设置条形图的一项：名称、颜色、占用、文件数、总量。"""
        self.name = name
        self.color = color
        self.size = size
        self.count = count
        self.total = total
        self.frac = pct_of(size, total)
        self.setToolTip(
            f"{name}\n{fmt(size)}（{fmt_bytes(size)} 字节）\n"
            f"{count} 个文件 · 占 {fmt_pct(size, total)}")
        self.update()

    def paintEvent(self, e):
        p = self.pal
        w = self.width()
        h = self.height()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0, 1, w, h - 2)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(p["panel2"]))
        painter.drawRoundedRect(r, 5, 5)
        if self.frac > 0:
            fr = QRectF(0, 1, max(6.0, w * self.frac), h - 2)
            painter.setBrush(with_alpha(self.color, 0.24))
            painter.drawRoundedRect(fr, 5, 5)

        # 左侧色块
        painter.setBrush(QColor(self.color))
        painter.drawRoundedRect(QRectF(7, h / 2.0 - 4, 8, 8), 2.5, 2.5)

        painter.setPen(QColor(p["text"]))
        f = QFont(self.font())
        f.setPointSize(9)
        painter.setFont(f)
        painter.drawText(QRectF(21, 0, w - 110, h),
                         Qt.AlignLeft | Qt.AlignVCenter,
                         self.name)
        right = f"{fmt(self.size)}  {fmt_pct(self.size, self.total)}"
        painter.setPen(QColor(p["text_dim"]))
        painter.drawText(QRectF(w - 104, 0, 98, h),
                         Qt.AlignRight | Qt.AlignVCenter, right)
        painter.end()


class TypeBars(QWidget):
    """类型构成列表：最多显示前 N 类，其余归到「其他」。"""

    def __init__(self, parent=None, max_rows=8):
        super().__init__(parent)
        self.pal = palette(DEFAULT_THEME)
        self.max_rows = max_rows
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(4)
        self.rows = []
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_palette(self, p):
        """换主题配色。"""
        self.pal = p
        for r in self.rows:
            r.set_palette(p)
        self.update()

    def set_items(self, items, total):
        """items: [(名称, 颜色, 字节数, 文件数)]"""
        for r in self.rows:
            self.lay.removeWidget(r)
            r.setParent(None)
            r.deleteLater()
        self.rows = []

        items = list(items or [])
        if not items:
            self.setFixedHeight(0)
            return
        items.sort(key=lambda x: x[2], reverse=True)
        shown = items[:self.max_rows]
        rest = items[self.max_rows:]
        if rest:
            shown.append(("其他", self.pal["text_faint"],
                          sum(x[2] for x in rest), sum(x[3] for x in rest)))
        for name, color, size, count in shown:
            row = _BarRow(self)
            row.set_palette(self.pal)
            row.set_data(name, color, size, count, total)
            self.lay.addWidget(row)
            self.rows.append(row)
        self.setFixedHeight(len(shown) * (_BarRow.H + 4))
