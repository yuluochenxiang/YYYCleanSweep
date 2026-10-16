"""Treemap 视图：用方块面积表示目录/文件占用。

为什么自己算：squarified treemap（Bruls / Huizing / van Wijk 1999）只有几十行，
自己实现就不必为它引入第三方包，打包体积不受影响。

- 面积 ∝ 占用大小，同层按占用从大到小排列；
- 递归到 max_depth 层，越深的块颜色越深；
- 单击选中（发出 path_clicked），双击下钻（发出 path_activated）；
- 鼠标停在块上显示完整路径与占用（不占横向空间，与 DaisyDisk 的思路一致）。

配色（v3.9 重做）
----------------
原来只按层级深度取色，块与块之间区分度不够，文字也和底色糊在一起。现在：

1. **颜色表达属性**，三种模式可切换（参考 WizTree 的「按文件类型」思路）：
   - ``type``   按文件类型：这块里占大头的是视频 / 图片 / 程序 / 文档……一眼看出
     这块盘到底被什么吃掉了（默认，也是最实用的一种）；
   - ``folder`` 按顶层子文件夹：同一个子文件夹及其所有下层共用一个色系，
     适合看「某个软件 / 某个项目一共占了多少地方」；
   - ``risk``   按知识库建议：可清理（绿）/ 谨慎（黄）/ 危险（红）/ 保留（灰），
     颜色直接告诉你这块能不能动。
2. **文字永远看得清**：每块的文字颜色按底色亮度自动选黑或白，并加 1px 反向描边，
   浅色块上用深色字、深色块上用白字，不会再出现「字和底一个色」的情况。
3. 底部常驻图例，说明当前颜色代表什么。
"""
import os

from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QPainter, QColor, QPen, QFont
from PySide6.QtWidgets import QWidget, QToolTip

from core.sizefmt import fmt_size, fmt_bytes
from core.knowledge import filetypes

# 三种着色模式：id -> 显示名
COLOR_MODES = [
    ("type", "按文件类型"),
    ("folder", "按子文件夹"),
    ("risk", "按风险"),
]

# 「按子文件夹」模式用的色板：Tableau 10，色相分散、彼此区分度高
FOLDER_COLORS = [
    "#4E79E6", "#F28E2B", "#59A14F", "#E15759", "#9C59B6",
    "#EDC948", "#76B7B2", "#FF9DA7", "#B07AA1", "#8CD17D",
]

LEGEND_H = 26      # 底部图例高度
GAP = 1.5          # 块与块之间的缝隙


def squarify(values, x, y, w, h):
    """squarified treemap 布局。

    values: 一组非负数（通常是各子项的占用大小）
    返回: [(x, y, w, h, index)]，index 对应 values 里的下标。
    """
    items = [(v, i) for i, v in enumerate(values) if v > 0]
    total = sum(v for v, _ in items)
    if not items or total <= 0 or w <= 0 or h <= 0:
        return []
    area = float(w) * float(h)
    pool = [{"a": v / total * area, "i": i} for v, i in items]
    out = []

    def worst(row, length):
        """这行放进去之后最差的长宽比，越小越接近正方形。"""
        s = sum(r["a"] for r in row)
        if s <= 0:
            return float("inf")
        mx = max(r["a"] for r in row)
        mn = min(r["a"] for r in row)
        if mn <= 0:
            return float("inf")
        return max((length * length * mx) / (s * s),
                   (s * s) / (length * length * mn))

    def place(row, x, y, w, h):
        """把一行矩形按 squarified 算法铺进给定区域（沿短边切分）。"""
        s = sum(r["a"] for r in row)
        if w >= h:
            # 沿短边（高）铺：这一列占一块宽度，内部纵向切分
            cw = (s / h) if h else 0.0
            cy = y
            for r in row:
                ch = (r["a"] / cw) if cw else 0.0
                out.append((x, cy, cw, ch, r["i"]))
                cy += ch
            return x + cw, y, w - cw, h
        ch = (s / w) if w else 0.0
        cx = x
        for r in row:
            cw = (r["a"] / ch) if ch else 0.0
            out.append((cx, y, cw, ch, r["i"]))
            cx += cw
        return x, y + ch, w, h - ch

    row = []
    while pool:
        cand = row + [pool[0]]
        if not row or worst(cand, min(w, h)) <= worst(row, min(w, h)):
            row = cand
            pool.pop(0)
        else:
            x, y, w, h = place(row, x, y, w, h)
            row = []
    if row:
        place(row, x, y, w, h)
    return out


def _pos(e):
    """兼容 Qt6 的 position() 与旧写法的 pos()。"""
    if hasattr(e, "position"):
        return e.position().toPoint()
    return e.pos()


def _gpos(e):
    if hasattr(e, "globalPosition"):
        return e.globalPosition().toPoint()
    return e.globalPos()


def _luma(c):
    """感知亮度（0~1），粗略用。判断主题深浅、排序用。"""
    return (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) / 255.0


def _rel_luma(c):
    """WCAG 相对亮度，用来算真实的文字对比度。"""
    def _ch(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * _ch(c.red()) + 0.7152 * _ch(c.green()) + 0.0722 * _ch(c.blue())


def contrast_text(bg):
    """在这块底色上，白字和黑字哪个更清楚就用哪个（WCAG 对比度择优）。

    这是「色块和文字糊在一起」的根治办法：绿色 / 黄色这类亮底色会自动改用深色字，
    深蓝 / 深紫这类暗底色用白字，任何配色下文字对比度都在 4.5:1 以上。
    """
    L = _rel_luma(bg)
    white = 1.05 / (L + 0.05)
    black = (L + 0.05) / 0.05
    return QColor("#ffffff") if white >= black else QColor("#101418")


class TreemapView(QWidget):
    """目录占用的方块图。不持有数据，数据来自 core.dirstat.DirTree。"""

    path_clicked = Signal(str)     # 单击某个块
    path_activated = Signal(str)   # 双击：下钻

    def __init__(self, parent=None, max_depth=2):
        super().__init__(parent)
        self.pal = {}
        self.tree = None
        self.root = None
        self.max_depth = max_depth
        self.rects = []
        self.sel = None
        self._hover = None
        self.color_mode = "type"          # type / folder / risk
        self._steps = {}                  # 文件类型 -> 明度档位（懒计算）
        self.ft = filetypes()
        self.risk_fn = None               # dir_page 传入：path -> {risk:...}
        self.kids_fn = None               # dir_page 传入：path -> [已排序+已过滤的子节点]
        self._folder_keys = []            # 本轮布局里出现过的顶层目录（分配颜色用）
        self._legend = []                 # [(名称, 颜色, 总大小)]
        self.setMouseTracking(True)
        self.setMinimumSize(260, 200)

    # ---------------- 数据 ----------------
    def set_palette(self, pal):
        """换主题配色。"""
        self.pal = pal or {}
        self.update()

    def set_color_mode(self, mode):
        """切换着色模式并重画。"""
        self.color_mode = mode
        self._layout()
        self.update()

    def set_tree(self, tree, root=None):
        """设置要画的目录树与根节点；传 None 表示清空。"""
        self.tree = tree
        self.root = os.path.normpath(root) if (tree is not None and root) else (
            tree.root if tree is not None else None)
        self.sel = None
        self._hover = None
        self._layout()
        self.update()

    def clear(self):
        """清空画布（回到「还没有数据」的占位状态）。"""
        self.set_tree(None)

    # ---------------- 布局 ----------------
    def _layout(self):
        self.rects = []
        self._folder_keys = []
        self._legend = []
        if not self.tree or not self.root:
            return
        nd = self.tree.node(self.root)
        if not nd:
            return
        m = 4
        w = self.width() - m * 2
        h = self.height() - m * 2 - LEGEND_H
        if w <= 8 or h <= 8:
            return
        # 「按子文件夹」模式先固定顶层目录的颜色顺序，保证同色系稳定
        if self.color_mode == "folder":
            self._folder_keys = [k.path for k in self._kids(self.root)]
        self._walk(self.root, m, m, w, h, 0, None)
        self._build_legend()

    def _kids(self, path):
        """取一个目录要铺开的子节点。

        默认直接问数据树要（按占用降序、去掉 0 字节的）；dir_page 会塞一个
        kids_fn 进来，让方块图跟着「排序 / 只看大于 / 隐藏已标记」三个筛选走。
        """
        if self.kids_fn is not None:
            try:
                return list(self.kids_fn(path) or [])
            except Exception:
                pass
        return [k for k in self.tree.children(path, sort_key="size")
                if k.agg_size > 0]

    def _walk(self, path, x, y, w, h, depth, folder_key):
        nd = self.tree.node(path)
        if not nd:
            return
        kids = []
        if depth < self.max_depth:
            kids = self._kids(path)
        if folder_key is None and self.color_mode == "folder":
            folder_key = path if path in self._folder_keys else None
        if kids:
            vals = [k.agg_size for k in kids]
            for rx, ry, rw, rh, i in squarify(vals, x, y, w, h):
                if rw <= 1 or rh <= 1:
                    continue
                fk = folder_key
                if fk is None and self.color_mode == "folder":
                    fk = kids[i].path if kids[i].path in self._folder_keys else None
                self._walk(kids[i].path, rx + 1, ry + 1, rw - 2, rh - 2,
                           depth + 1, fk)
        else:
            self.rects.append((x, y, w, h, path, nd.name, nd.agg_size, depth,
                               self._group_key(nd, folder_key)))

    def _group_key(self, nd, folder_key):
        """决定这块归到哪个颜色组。"""
        if self.color_mode == "folder":
            return folder_key or nd.path
        if self.color_mode == "risk":
            risk = "keep"
            if self.risk_fn:
                try:
                    risk = (self.risk_fn(nd.path) or {}).get("risk") or "keep"
                except Exception:                          # noqa: BLE001
                    risk = "keep"
            return risk
        # 默认：按文件类型（这块里体积占大头的是哪一类文件）
        try:
            g = self.ft.group(nd.agg_ext or {})
        except Exception:                                  # noqa: BLE001
            g = []
        return g[0][0] if g else "_none"

    # ---------------- 颜色 ----------------
    def _base_color(self, key):
        """按当前模式取这一组的底色。"""
        if self.color_mode == "folder":
            try:
                i = self._folder_keys.index(key)
            except ValueError:
                i = abs(hash(key)) % len(FOLDER_COLORS)
            return QColor(FOLDER_COLORS[i % len(FOLDER_COLORS)])
        if self.color_mode == "risk":
            risk = (self.pal or {}).get("risk", {}) or {}
            return QColor(risk.get(key) or risk.get("keep") or "#8b95a6")
        # 按文件类型
        if key == "_none":
            return QColor((self.pal or {}).get("text_faint", "#8b95a6"))
        return QColor(self.ft.color(key))

    def _group_name(self, key):
        if self.color_mode == "folder":
            return os.path.basename(key.rstrip("\\/")) or key
        if self.color_mode == "risk":
            return {"safe": "可清理", "caution": "谨慎", "danger": "危险",
                    "keep": "建议保留", "migrate": "建议迁移"}.get(key, key)
        if key == "_none":
            return "无文件 / 未统计"
        for c in self.ft.cats:
            if c["id"] == key:
                return c["name"]
        return self.ft.fallback.get("name", "其他")

    def _type_step(self, key):
        """按文件类型给一个明度档位（0/1/2）。

        文件类型里「三种灰」（文档 / 程序库 / 字体）、「两种绿」（压缩包 / 代码）
        色相天生接近，只靠色相分不开。这里把同一段色相的类别依次错开明度，
        视觉上就分得清了。
        """
        if not self._steps:
            buckets = {}
            for c in self.ft.cats:
                bucket = QColor(c["color"]).hue() // 30
                buckets.setdefault(bucket, []).append(c["id"])
            for ids in buckets.values():
                for i, cid in enumerate(ids):
                    self._steps[cid] = i % 3
        return self._steps.get(key, 0)

    def _tune(self, base, depth, key=None):
        """统一饱和度与明度：块与块之间区分明显，同时让文字有足够对比度。

        - 饱和度抬到 0.65 以上让颜色「跳」出来，纯灰的类别也给一点点彩；
        - 同一段色相的类别按明度阶梯错开（见 _type_step）；
        - 层级越深越暗，嵌套关系一眼看得出。
        """
        c = QColor(base)
        h, s, _v, _a = c.getHsvF()
        s = 0.22 if s < 0.22 else min(1.0, max(s, 0.65))
        v0 = 0.68 if self._is_dark() else 0.72
        if self.color_mode == "type" and key is not None:
            # 只往下错开明度：同色相的几个类别靠深浅拉开，整体也不会越调越亮
            v0 -= (0.0, 0.07, 0.14)[self._type_step(key)]
        v = max(0.30, min(0.95, v0 - min(depth, 3) * 0.055))
        c.setHsvF(h, s, v, 1.0)
        return c

    def _is_dark(self):
        bg = QColor((self.pal or {}).get("bg", "#12141a"))
        return _luma(bg) < 0.5

    def _text_color(self, bg):
        """按底色亮度自动选黑 / 白，保证任何配色下文字都读得清。"""
        return contrast_text(bg)

    # ---------------- 图例 ----------------
    def _build_legend(self):
        acc = {}
        for (_x, _y, _w, _h, _p, _n, size, _d, key) in self.rects:
            acc[key] = acc.get(key, 0) + size
        top = sorted(acc.items(), key=lambda kv: kv[1], reverse=True)[:7]
        self._legend = [(self._group_name(k),
                         self._tune(self._base_color(k), 0, k), v)
                        for k, v in top]

    # ---------------- 绘制 ----------------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.pal or {}
        p.fillRect(self.rect(), QColor(pal.get("panel", "#1a1e26")))
        if not self.rects:
            p.setPen(QColor(pal.get("text_dim", "#98a2b4")))
            p.drawText(self.rect(), Qt.AlignCenter,
                       "暂无可绘制的数据\n先扫描一个目录，再切到「方块图」")
            p.end()
            return

        f = QFont(self.font())
        f.setPointSize(max(8, self.font().pointSize() - 1))
        fb = QFont(f)
        fb.setBold(True)
        p.setFont(fb)

        for (x, y, w, h, path, name, size, depth, key) in self.rects:
            col = self._tune(self._base_color(key), depth, key)
            inner = QRectF(x + GAP, y + GAP, max(1.0, w - GAP * 2),
                           max(1.0, h - GAP * 2))
            p.fillRect(inner, col)

            if path == self.sel:
                p.setPen(QPen(QColor(pal.get("text", "#ffffff")), 2.4))
                p.drawRect(inner)
            elif path == self._hover:
                p.setPen(QPen(QColor(pal.get("accent", "#5b9dff")), 1.8))
                p.drawRect(inner)

            if inner.width() >= 46 and inner.height() >= 20:
                txt = self._text_color(col)
                # 文字反色的 1px 投影：任何底色上都有轮廓，又不会让字看起来发空
                shadow = QColor("#101418" if txt.lightness() > 128 else "#ffffff")
                shadow.setAlpha(120)
                box = inner.adjusted(5, 3, -5, -3)
                p.setPen(shadow)
                p.drawText(box.translated(1, 1), Qt.AlignLeft | Qt.AlignTop, name)
                p.setPen(txt)
                p.drawText(box, Qt.AlignLeft | Qt.AlignTop, name)
                if inner.height() >= 36:
                    p.setFont(f)
                    sub = fmt_size(size)
                    p.setPen(shadow)
                    p.drawText(box.translated(1, 1),
                               Qt.AlignLeft | Qt.AlignBottom, sub)
                    p.setPen(txt)
                    p.drawText(box, Qt.AlignLeft | Qt.AlignBottom, sub)
                    p.setFont(fb)

        self._draw_legend(p)
        p.end()

    def _draw_legend(self, p):
        """底部图例：说明当前颜色代表什么，省得猜。"""
        if not self._legend:
            return
        pal = self.pal or {}
        y0 = self.height() - LEGEND_H
        p.fillRect(QRectF(0, y0, self.width(), LEGEND_H),
                   QColor(pal.get("bg", "#12141a")))
        f = QFont(self.font())
        f.setPointSize(max(7, self.font().pointSize() - 2))
        p.setFont(f)
        dim = QColor(pal.get("text_dim", "#98a2b4"))
        x = 10.0
        cy = y0 + LEGEND_H / 2.0
        for (name, col, size) in self._legend:
            label = f"{name} {fmt_size(size)}"
            tw = p.fontMetrics().horizontalAdvance(label)
            if x + 12 + 5 + tw > self.width() - 8:
                break
            p.fillRect(QRectF(x, cy - 5, 11, 11), col)
            p.setPen(dim)
            p.drawText(QRectF(x + 15, cy - 9, tw + 6, 18),
                       Qt.AlignLeft | Qt.AlignVCenter, label)
            x += 15 + tw + 14

    # ---------------- 交互 ----------------
    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._layout()
        self.update()

    def leaveEvent(self, e):
        self._hover = None
        QToolTip.hideText()
        self.update()
        super().leaveEvent(e)

    def _hit(self, pos):
        for r in reversed(self.rects):
            x, y, w, h = r[0], r[1], r[2], r[3]
            if x <= pos.x() < x + w and y <= pos.y() < y + h:
                return r[4]
        return None

    def _tip_of(self, path):
        if not self.tree:
            return ""
        nd = self.tree.node(path)
        if not nd:
            return ""
        lines = [nd.path,
                 f"{fmt_size(nd.agg_size)}（{fmt_bytes(nd.agg_size)} 字节） · "
                 f"{nd.agg_count} 个文件"]
        try:
            g = self.ft.group(nd.agg_ext or {})[:3]
            if g:
                names = []
                for cid, (sz, _c) in g:
                    nm = self._group_name(cid) if self.color_mode != "type" \
                        else next((c["name"] for c in self.ft.cats
                                   if c["id"] == cid), self.ft.fallback["name"])
                    names.append(f"{nm} {fmt_size(sz)}")
                lines.append("主要类型：" + " · ".join(names))
        except Exception:                                  # noqa: BLE001
            pass
        return "\n".join(lines)

    def mouseMoveEvent(self, e):
        path = self._hit(_pos(e))
        if path != self._hover:
            self._hover = path
            self.update()
        if path and self.tree:
            QToolTip.showText(_gpos(e), self._tip_of(path), self)
            self.setCursor(Qt.PointingHandCursor)
            return
        QToolTip.hideText()
        self.setCursor(Qt.ArrowCursor)

    def mousePressEvent(self, e):
        path = self._hit(_pos(e))
        self.sel = path
        self.update()
        if path:
            self.path_clicked.emit(path)

    def mouseDoubleClickEvent(self, e):
        path = self._hit(_pos(e))
        if path:
            self.path_activated.emit(path)
