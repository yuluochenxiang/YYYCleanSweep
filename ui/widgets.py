"""界面通用小部件：统一行样式委托、大号勾选框、三角展开箭头、路径富文本 / 中间省略。

选中效果以前靠 QSS 的 `border-left: 3px` 实现，一选中文字就会向右跳 3px，
而且列表 / 树 / 表格各画各的，观感不统一。现在统一由 RowDelegate 自绘：
圆角背景 + 左侧强调色条，选中与悬浮在所有视图里完全一致。
"""
import time
import weakref

from PySide6.QtCore import Qt, QRectF, QPointF, QObject, Slot, QEvent, QTimer
from PySide6.QtGui import (
    QColor, QPen, QPainter, QFontMetrics, QBrush, QPolygonF, QPalette,
)
from PySide6.QtWidgets import (
    QProxyStyle, QStyle, QStyledItemDelegate, QApplication, QLabel,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QAbstractSpinBox, QComboBox,
    QToolButton, QLineEdit, QVBoxLayout, QHBoxLayout, QFrame,
)

from ui.theme import palette, DEFAULT_THEME
from core.sizefmt import fmt_size as _fmt_size, parse_size, parse_pct

# 兼容旧调用：全项目的容量格式化统一走 core.sizefmt
fmt = _fmt_size


def fmt_time(ts):
    """时间戳 -> 日期时间文本；0 或空显示「—」。"""
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def fmt_datetime(ts):
    """时间戳 -> 含秒的日期时间文本（日志 / 标记时间用）。"""
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


# ---------------------------------------------------------------- 路径显示
def split_path(p):
    """拆成 (盘符, 中间段列表, 末段)。"""
    p = (p or "").replace("/", "\\").rstrip("\\")
    if not p:
        return "", [], ""
    parts = [x for x in p.split("\\") if x]
    if len(parts) == 1:
        return "", [], parts[0]
    drive = parts[0] + "\\"
    return drive, parts[1:-1], parts[-1]


def short_path(p, keep_head=1, keep_tail=2, max_chars=70):
    """长路径折叠成 头…尾 的形式，保留盘符与末段，避免整行都是省略号。"""
    p = (p or "").replace("/", "\\").rstrip("\\")
    if len(p) <= max_chars:
        return p
    drive, mid, tail = split_path(p)
    if not mid:
        return p
    head = mid[:keep_head]
    last = mid[-keep_tail:] if keep_tail else []
    mid_txt = "\\".join(head) + ("\\…\\" if (head or last) else "…\\")
    if last and head != last:
        mid_txt += "\\".join(last) + "\\"
    return (drive + mid_txt + tail).replace("\\\\", "\\")


def elide_middle(text, width, font=None):
    """按像素宽度做中间省略。"""
    fm = QFontMetrics(font or QApplication.font())
    return fm.elidedText(text or "", Qt.ElideMiddle, max(30, width))


def html_path(p, pal, strong=True, max_chars=999):
    """把路径渲染成富文本：盘符与中段弱化，末段高亮。"""
    drive, mid, tail = split_path(p)
    if not drive and not mid and not tail:
        return ""
    dim = pal["text_dim"]
    faint = pal["text_faint"]
    mid_txt = "\\".join(mid)
    if len(mid_txt) > max_chars and len(mid) > 3:
        mid_txt = "\\".join(mid[:1]) + "\\…\\" + "\\".join(mid[-2:])
    out = f"<span style='color:{faint}'>{esc(drive)}</span>"
    if mid_txt:
        out += f"<span style='color:{dim}'>{esc(mid_txt)}\\</span>"
    tag = ("b" if strong else "span")
    style = "" if strong else f" style='color:{dim}'"
    out += f"<{tag}{style}>{esc(tail)}</{tag}>"
    return out


def esc(s):
    """HTML 转义。路径里可能有 & < >，直接拼进富文本会截断。"""
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def chip(text, color, pal=None, soft=None):
    """一个彩色小标签（HTML）。"""
    soft = soft or hex_alpha(color, 0.14)
    return (f"<span style='background:{soft};color:{color};"
            f"border-radius:5px;padding:2px 8px;font-size:12px;"
            f"margin-right:6px;white-space:nowrap'>{esc(text)}</span>")


def hex_alpha(color, alpha):
    """#RRGGBB -> rgba(...) 字符串，给 QSS / 富文本用。"""
    c = QColor(color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha:.2f})"


def with_alpha(color, alpha):
    """#RRGGBB + 透明度 -> QColor，给 QPainter 用。

    别拿 hex_alpha() 的结果去构造 QColor：Qt 只认 "#AARRGGBB" / 颜色名，
    "rgba(226,75,74,0.24)" 这种函数式写法解析失败会得到无效色，
    画笔/画刷静默退化成纯黑（条形图整根变黑就是这么来的，v3.38 修）。
    """
    c = QColor(color)
    c.setAlphaF(max(0.0, min(1.0, float(alpha))))
    return c


# ---------------------------------------------------------------- 统一行样式
def page_layout(widget):
    """页面的主竖向布局。

    目录分析 / 文件去重 / 系统瘦身三页的骨架完全一样（同样的外边距和
    控件间距），以前各写一份，改一处忘了改另一处就会对不齐。统一在这里，
    三页的页边距和行距天然一致。
    """
    lay = QVBoxLayout(widget)
    lay.setContentsMargins(0, 6, 0, 0)
    lay.setSpacing(8)
    return lay


def top_bar():
    """页面顶部那条浅色卡片行，返回 (容器, 水平布局)。

    三个扫描页的顶部条外观必须完全一致（同样的圆角卡片、同样的内边距），
    以前各写一份，改一处忘了另一处就会出现「翻页签时条高跳动」这类问题。
    """
    box = QFrame()
    box.setObjectName("card")
    lay = QHBoxLayout(box)
    lay.setContentsMargins(12, 8, 12, 8)
    lay.setSpacing(8)
    return box, lay


def enable_hover(view):
    """让 item view 的行能收到鼠标悬浮状态（自绘 hover 背景的前提）。"""
    try:
        view.setMouseTracking(True)
        view.viewport().setMouseTracking(True)
    except Exception:
        pass
    return view


def setup_tree(view, indent=18):
    """树控件的统一设置：展开三角常开。

    必须显式打开 rootIsDecorated 与 itemsExpandable，否则顶层节点不会有三角，
    用户就看不出「这一行还能点开」。三角本身由 BigCheckStyle 自绘
    （PE_IndicatorBranch），所以 QSS 里绝不能出现 ::branch 规则。
    """
    try:
        view.setRootIsDecorated(True)
        view.setItemsExpandable(True)
        view.setIndentation(indent)
        view.setExpandsOnDoubleClick(True)
        view.setAnimated(False)          # 开动画时自绘三角会有残影
    except Exception:
        pass
    return view


class FirstColFit(QObject):
    """窗口宽度变化时，第一列（文件名 / 路径列）跟着一起伸缩。

    两个硬要求：
    1. **双向**：拉宽就变宽、缩窄就收回。以前按「宽度差」累加，只能拉不能收
       （先拉到很宽再缩回来，第一列还停在最大宽度），现在改成每次按视口实际
       宽度重算，天然双向、也不会累积误差。
    2. **其它列有各自的最小宽度**：多余宽度全给第一列，其余列保持自己的宽度
       且不小于设定值，像去重页的「处置」列不会被挤到看不全。

    空间实在不够时（各列最小宽之和已经超过视口），第一列停在自己的最小宽度，
    宁可出水平滚动条也不把列压扁。

    用法：`FirstColFit(self.tw, min_w=280, others={1: 90, 2: 130})`
    """

    def __init__(self, view, min_w=280, others=None):
        super().__init__(view)
        self.view = view
        self.min_w = min_w
        self.others = dict(others or {})
        # 末列自动拉伸会先把多余宽度吃掉，第一列就永远算不出富余，必须关掉
        try:
            view.header().setStretchLastSection(False)
            view.header().setMinimumSectionSize(56)
        except Exception:
            pass
        self._pending = False
        view.installEventFilter(self)
        # 视口才是内容实际可用的宽度所在，也一起监听
        try:
            view.viewport().installEventFilter(self)
        except Exception:
            pass
        QTimer.singleShot(0, self.fit)

    def _defer(self):
        self._pending = False
        self.fit()

    def fit(self):
        """按视口当前宽度重排：其它列保底，剩下的全给第一列。"""
        view = self.view
        try:
            head = view.header()
            vw = view.viewport().width()
        except Exception:
            return
        if vw <= 0 or head.count() < 1:
            return
        # 其它列：既不小于最小显示宽度，也不小于内容需要的宽度。
        # 原先把它们设成了「按内容算宽」+ 末列 Stretch，那样多余宽度会被末列
        # 吃掉、内容短的列还会被压到几十像素；这里统一改成可手动调整的交互
        # 模式，宽度只补不减，用户拖宽过的结果会保留。
        used = 0
        for col, mn in self.others.items():
            try:
                if head.sectionResizeMode(col) != QHeaderView.Interactive:
                    head.setSectionResizeMode(col, QHeaderView.Interactive)
                need = mn
                try:
                    need = max(need, head.sectionSizeHint(col))
                except Exception:
                    pass
                if head.sectionSize(col) < need:
                    head.resizeSection(col, need)
                used += head.sectionSize(col)
            except Exception:
                pass
        target = max(self.min_w, vw - used)
        try:
            if abs(target - head.sectionSize(0)) > 2:
                head.resizeSection(0, target)
        except Exception:
            pass

    def eventFilter(self, obj, e):
        # 收到 Resize 的瞬间视口宽度还没更新完（会慢一拍，表现为"只拉不收"），
        # 所以推迟到事件循环空闲再算；连续拖拽时多次事件只会合并成一次
        if e.type() == QEvent.Resize and not self._pending:
            self._pending = True
            QTimer.singleShot(0, self._defer)
        return False


class HeaderSorter(QObject):
    """「点表头就排序」——树与表格通用，且按真实数值排。

    没用 Qt 自带的 setSortingEnabled，原因有两个：
    1. 它按**显示文本**排，容量列会排成 "1.2 GB" < "900 MB" 这种错序；
    2. 树里一旦开启，懒加载往里插子节点会反复触发重排，展开时会看见行乱跳。

    这里改成点表头时用真实数值（或页面自定义的 key）**重排同级行**：
    父子关系不变、item 对象整体保留（勾选状态 / 前景色 / 悬浮提示都不丢），
    也不会打断懒加载。表格则是整行搬移，单元格 item 原样复用。

    用法：
        self.sorter = HeaderSorter(self.tw, numeric_cols=(1,))
        ...数据填充完后...
        self.sorter.sort_now()
    """

    def __init__(self, view, key_fn=None, default_col=0, default_desc=True,
                 numeric_cols=(), asc_first=(), on_sorted=None):
        """default_col=None 表示「先保持原顺序，等用户点了表头才排」，
        适合那些列表本身顺序就有含义的页面（系统瘦身、重复文件分组）。"""
        super().__init__(view)
        self.view = view
        self.key_fn = key_fn
        self.col = default_col
        self.desc = bool(default_desc)
        self.numeric_cols = set(numeric_cols or ())
        # 这些列第一次点按升序更自然（名称、路径等），其余默认降序（大的在前）
        self.asc_first = set(asc_first or ())
        self.on_sorted = on_sorted
        self._install()

    # ------------------------------------------------------ 安装
    def _install(self):
        hh = self._header()
        if hh is None:
            return
        try:
            hh.setSectionsClickable(True)
            # 没设默认排序列时先不显示箭头，否则表头第 0 列会挂一个
            # 与实际顺序不符的小箭头（用户点了之后才真正出现）
            hh.setSortIndicatorShown(self.col is not None)
            hh.sectionClicked.connect(self._on_clicked)
        except Exception:
            pass
        self._apply_indicator()

    @property
    def active(self):
        """当前是否处于「用户点过表头」的排序状态。"""
        return self.col is not None

    def _header(self):
        try:
            if isinstance(self.view, QTreeWidget):
                return self.view.header()
            return self.view.horizontalHeader()
        except Exception:
            return None

    def _apply_indicator(self):
        hh = self._header()
        if hh is None or self.col is None:
            return
        try:
            hh.setSortIndicatorShown(True)
            hh.setSortIndicator(self.col, Qt.DescendingOrder if self.desc
                                else Qt.AscendingOrder)
        except Exception:
            pass

    # ------------------------------------------------------ 点击
    @Slot(int)
    def _on_clicked(self, col):
        if self.col == col:
            self.desc = not self.desc
        else:
            self.col = col
            self.desc = col not in self.asc_first
        self._apply_indicator()
        self.sort_now()
        if self.on_sorted:
            self.on_sorted(self.col, self.desc)

    # ------------------------------------------------------ 排序键
    def _key(self, item):
        """统一返回 (分组, 数值, 文本) 三元键：数值型永远排在文本型前面。"""
        if self.key_fn is not None:
            try:
                v = self.key_fn(self.col, item)
            except Exception:
                v = None
            if v is not None:
                if isinstance(v, bool):
                    return (0, float(v), "")
                if isinstance(v, (int, float)):
                    return (0, float(v), "")
                return (0, 0.0, str(v))
        if item is None:
            return (1, 0.0, "")
        # 树节点要按「当前列」取值：容量这类数值存在第 1/2 列的 UserRole 上，
        # 用 item.data(Qt.UserRole)（= 第 0 列）会拿到路径 / id，排出来是错的。
        if isinstance(item, QTreeWidgetItem):
            v = item.data(self.col, Qt.UserRole)
            txt = item.text(self.col) or ""
        else:
            v = item.data(Qt.UserRole)
            txt = item.text() or ""
        if isinstance(v, bool):
            return (1, 0.0, txt)
        if isinstance(v, (int, float)):
            return (0, float(v), txt)
        if self.col in self.numeric_cols:
            n = parse_size(txt)
            if n is None:
                n = parse_pct(txt)
            if n is not None:
                return (0, float(n), txt)
        return (1, 0.0, txt)

    # ------------------------------------------------------ 执行
    def sort_now(self):
        """立刻按当前排序设置重排一次（数据刷新后调用）。"""
        if self.col is None:
            return
        try:
            if isinstance(self.view, QTreeWidget):
                self._sort_tree()
            else:
                self._sort_table()
        except Exception:
            pass

    def sort_children(self, parent_item):
        """只排某一层（懒加载树展开一层之后用，代价很小）。"""
        if self.col is None or parent_item is None:
            return
        try:
            self.sort_level(parent_item)
        except Exception:
            pass

    def _sort_tree(self):
        self.sort_level(None)

    def sort_level(self, parent):
        """把 parent 下的同级兄弟排序（parent=None 表示顶层），并递归下去。

        懒加载树在展开新一层之后单独调它，只排那一层，不用整棵树重排。
        """
        tw = self.view
        if parent is None:
            n = tw.topLevelItemCount()
            if n < 2:
                if n == 1:
                    self.sort_level(tw.topLevelItem(0))
                return
            siblings = [tw.topLevelItem(i) for i in range(n)]
            # 展开状态必须在 take 之前读：item 一旦脱离 model，
            # isExpanded() 就变回 False，读晚了整棵树会被折叠
            expanded = {id(it): it.isExpanded() for it in siblings}
            items = [tw.takeTopLevelItem(0) for _ in range(n)]
        else:
            n = parent.childCount()
            if n < 2:
                return
            siblings = [parent.child(i) for i in range(n)]
            expanded = {id(it): it.isExpanded() for it in siblings}
            items = [parent.takeChild(0) for _ in range(n)]

        items.sort(key=self._key, reverse=self.desc)
        for i, it in enumerate(items):
            if parent is None:
                tw.insertTopLevelItem(i, it)
            else:
                parent.insertChild(i, it)
        # 展开状态要还回去：take / insert 会把它清掉
        for it in items:
            it.setExpanded(expanded.get(id(it), False))
            self.sort_level(it)

    def _sort_table(self):
        t = self.view
        n, m = t.rowCount(), t.columnCount()
        if n < 2 or m < 1:
            return
        rows = [[t.takeItem(r, c) for c in range(m)] for r in range(n)]
        col = min(self.col, m - 1)
        rows.sort(key=lambda cells: self._key(cells[col]), reverse=self.desc)
        for r, cells in enumerate(rows):
            for c, it in enumerate(cells):
                if it is not None:
                    t.setItem(r, c, it)

    # ------------------------------------------------------ 状态
    def set_state(self, col, desc):
        """设置排序列与方向，并更新表头上的箭头指示。"""
        self.col, self.desc = col, bool(desc)
        self._apply_indicator()


def mark_expandable(item, expandable):
    """给单个树节点设置是否显示展开三角。

    dir_page 那种懒加载树，「还没扫过但下面肯定有东西」的节点也要显示三角，
    所以这里按 expandable 显式指定，而不依赖 Qt 的"没有子节点就不显示"。
    """
    from PySide6.QtWidgets import QTreeWidgetItem
    item.setChildIndicatorPolicy(
        QTreeWidgetItem.ShowIndicator if expandable
        else QTreeWidgetItem.DontShowIndicator)
    return item


# 所有「层级线高亮」树的弱引用集合：主题切换时统一推送新配色。
_PATH_TREES = set()


def refresh_path_trees(pal):
    """把新配色推给所有带路径高亮的树（挂在 refresh_delegates 里一起调用）。"""
    for ref in list(_PATH_TREES):
        o = ref()
        if o is None:
            _PATH_TREES.discard(ref)
            continue
        try:
            o.set_path_palette(pal)
        except RuntimeError:            # 底层对象已被 Qt 回收
            _PATH_TREES.discard(ref)


class _PathLineMixin(object):
    """给树加一层「当前查看范围」的高亮折线。

    多层树一旦展开十几行，滚轮一滚就分不清自己处在哪条分支下。这里把
    「当前项 → 父 → … → 根」这条路径上的层级线换成强调色并加粗，末端再点一个
    圆点标出当前行；点到另一条分支时，高亮立刻跟着切过去。

    绘制时机：重写 paintEvent，先让树自己画完（此时行背景、展开三角、文字
    都已就位），再在视口上叠加这条折线——所以永远压在最上层，不会被 delegate
    画的斑马纹 / 选中背景盖住。

    层的水平中心怎么算：visualItemRect 返回的行矩形 **已经带上缩进**（顶层也占
    一格），所以某一层的三角格就是 [行起点, 行起点 + indent]，取中点即可；行
    矩形天然带水平滚动偏移，不用自己算列位置。
    """

    def _path_line_init(self, width=2):
        self._lp_width = width
        self._lp_pal = palette(DEFAULT_THEME)
        try:
            _PATH_TREES.add(weakref.ref(self))
        except Exception:
            pass
        # 选中 / 展开状态一变就得重画，否则线会停在旧的那条路径上
        for sig in ("currentItemChanged", "itemExpanded", "itemCollapsed",
                    "itemSelectionChanged"):
            try:
                getattr(self, sig).connect(self._path_line_refresh)
            except Exception:
                pass

    def set_path_palette(self, pal):
        """主题切换：换强调色并立刻重画。"""
        self._lp_pal = pal
        self._path_line_refresh()

    def _path_line_refresh(self, *_a):
        try:
            self.viewport().update()
        except Exception:
            pass

    # ---------------------------------------------------------- 绘制
    def paint_path_line(self):
        """画出当前路径的高亮折线（由 paintEvent 在自身绘制之后调用）。"""
        try:
            cur = self.currentItem()
        except Exception:
            return
        if cur is None or self.topLevelItemCount() == 0:
            return
        try:
            indent = self.indentation()
            vh = self.viewport().height()
        except Exception:
            return
        vw = self.viewport().width()

        chain = []
        it = cur
        while it is not None:
            chain.append(it)
            it = it.parent()
        chain.reverse()

        painter = QPainter(self.viewport())
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            col = QColor(self._lp_pal["accent"])
            pen = QPen(col, self._lp_width)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            prev_x = None
            end_x = end_y = None
            for item in chain:
                r = self.visualItemRect(item)
                if not r.isValid():
                    return
                # 行矩形已含缩进：本层的三角格就是 [r.x(), r.x()+indent]
                x = r.x() + indent / 2.0
                cy = r.center().y()
                if x > vw + indent:               # 整条路径被横向滚出视野
                    return
                if prev_x is not None:
                    painter.drawLine(QPointF(prev_x, cy), QPointF(x, cy))
                # 祖先：竖线一路画到它展开子树的最后一行，把整段范围都框出来；
                # 当前项本身只画到自己行中心，末尾由圆点收口。
                tail = cy
                if item is not cur:
                    rb = self.visualItemRect(_last_visible(item))
                    if rb.isValid():
                        tail = min(max(rb.bottom(), cy), vh + r.height())
                painter.drawLine(QPointF(x, cy), QPointF(x, tail))
                prev_x, end_x, end_y = x, x, cy
            # 当前所在那一行的末端：一个小圆点，指明"你在这里"
            if end_x is not None:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(col))
                painter.drawEllipse(QPointF(end_x, end_y), 3.2, 3.2)
        finally:
            painter.end()


class PathTreeWidget(_PathLineMixin, QTreeWidget):
    """带「当前路径高亮」的树，接口与普通 QTreeWidget 完全一致。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._path_line_init()

    def paintEvent(self, e):
        super().paintEvent(e)
        try:
            self.paint_path_line()
        except Exception:
            pass


def _last_visible(item):
    """子树里最后一个肉眼可见的行（沿每个节点的最后一个子节点往下钻）。"""
    while item.childCount() and item.isExpanded():
        try:
            item = item.child(item.childCount() - 1)
        except Exception:
            break
    return item


# 所有行委托的弱引用集合：主题切换时统一推送新配色。
# 各个页面的列表里散落着不少临时委托，逐个保存引用太容易漏，
# 漏掉的表现就是「切成亮色主题后，列表里还留着一块块暗色底」。
_DELEGATES = set()


def refresh_delegates(pal):
    """把新的调色板推给所有仍然活着的行委托（顺带处理路径高亮树）。"""
    for ref in list(_DELEGATES):
        d = ref()
        if d is None:
            _DELEGATES.discard(ref)
            continue
        try:
            d.set_palette(pal)
        except RuntimeError:          # 底层对象已被 Qt 回收
            _DELEGATES.discard(ref)
    refresh_path_trees(pal)


class RowDelegate(QStyledItemDelegate):
    """所有列表 / 树 / 表格共用的行绘制器。

    解决的问题：
    - 选中态统一：圆角背景 + 左侧强调色条，不再靠 border 挤动文字；
    - 悬浮态统一；
    - 可选：顶层行加粗放大，子层正常，级别一眼可辨（level_bold）；
    - 可选：某一列做中间省略（elide_col）；
    - 可选：某一列在文字下方画一条占比条（bar_col）。
    """

    def __init__(self, parent=None, elide_col=None, bar_col=None,
                 level_bold=False, thumb_col=None, thumb_size=0):
        super().__init__(parent)
        self.pal = palette(DEFAULT_THEME)
        self.elide_col = elide_col
        self.bar_col = bar_col
        # thumb_col：这一列前面会画一张小缩略图（走 DecorationRole，由
        # 底层样式画）。省略宽度要把它占的位置扣掉，否则文字会顶到图标下面。
        self.thumb_col = thumb_col
        self.thumb_size = thumb_size
        self.bar_max = 1.0
        self.level_bold = level_bold
        self.radius = 7
        # 斑马纹改由本委托画成圆角小块：Qt 自带的交替行色是铺满整行的直角色块，
        # 首行 / 末行会把外层容器的圆角顶成方形，很难看。
        self.zebra = False
        if parent is not None and hasattr(parent, "setAlternatingRowColors"):
            try:
                if parent.alternatingRowColors():
                    self.zebra = True
                    parent.setAlternatingRowColors(False)
            except Exception:
                pass
        # 视口底色调成透明：否则它会用直角矩形铺满内容区，盖住外层圆角
        if parent is not None and hasattr(parent, "viewport"):
            try:
                vp = parent.viewport()
                vp.setAutoFillBackground(False)
                vp.setStyleSheet("background: transparent; border: none;")
            except Exception:
                pass
        try:
            _DELEGATES.add(weakref.ref(self))
        except Exception:
            pass

    def set_palette(self, pal):
        """换主题配色。"""
        self.pal = pal

    def set_bar_max(self, v):
        """设置占比条的最大值（画条形图时按它归一化）。"""
        self.bar_max = max(1.0, float(v or 1))

    # -------------------------------------------------- 绘制
    def paint(self, painter, option, index):
        opt = type(option)(option)
        self.initStyleOption(opt, index)
        p = self.pal
        selected = bool(opt.state & QStyle.State_Selected)
        hover = bool(opt.state & QStyle.State_MouseOver)
        top = self._is_top(index)

        # ---- 背景（选中 / 悬浮）
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        r = QRectF(opt.rect).adjusted(3, 1, -3, -1)
        if r.height() > 3 and r.width() > 3:
            if selected:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(p["sel_bg"]))
                painter.drawRoundedRect(r, self.radius, self.radius)
                painter.setBrush(QColor(p["sel_bar"]))
                painter.drawRoundedRect(
                    QRectF(r.x() + 1.5, r.y() + 4, 3, max(3, r.height() - 8)),
                    1.5, 1.5)
            elif hover:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(p["hover"]))
                painter.drawRoundedRect(r, self.radius, self.radius)
            elif self.zebra and (index.row() % 2) == 1:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(p.get("row_alt")
                                        or p.get("alt") or p["panel2"]))
                painter.drawRoundedRect(r, self.radius, self.radius)
        painter.restore()

        # ---- 层级字重：顶层加粗 + 大 1pt，子层保持常规
        f = opt.font
        if self.level_bold and top:
            f.setBold(True)
            f.setPointSize(f.pointSize() + 1)
        opt.font = f

        # ---- 文字：需要时做中间省略
        if self.elide_col is not None and index.column() == self.elide_col:
            pad = 16
            if (self.thumb_col is not None and index.column() == self.thumb_col
                    and self.thumb_size
                    and index.data(Qt.DecorationRole) is not None):
                pad += self.thumb_size + 8
            opt.text = elide_middle(opt.text, opt.rect.width() - pad, f)

        # ---- 占比条（画在文字下面，所以先画）
        if self.bar_col is not None and index.column() == self.bar_col:
            self._paint_bar(painter, opt, index)

        if selected:
            opt.palette.setColor(QPalette.Text, QColor(p["sel_fg"]))
        # 背景已经自己画了，清掉这两个状态，避免底层样式再叠一层
        opt.state &= ~QStyle.State_Selected
        opt.state &= ~QStyle.State_MouseOver

        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)

    def sizeHint(self, option, index):
        """让 ResizeToContents 算列宽时把「顶层加粗放大」也算进去，
        否则加粗的那一行会被挤得显示省略号。"""
        opt = type(option)(option)
        self.initStyleOption(opt, index)
        if self.level_bold and self._is_top(index):
            f = opt.font
            f.setBold(True)
            f.setPointSize(f.pointSize() + 1)
            opt.font = f
        hint = super().sizeHint(opt, index)
        if self.elide_col is not None and index.column() == self.elide_col:
            # 首列（路径/名称）不参与撑宽，交给 Stretch 自动分配
            hint.setWidth(0)
        return hint

    def _is_top(self, index):
        return not index.parent().isValid()

    def _paint_bar(self, painter, opt, index):
        v = index.data(Qt.UserRole)
        try:
            v = float(v or 0)
        except (TypeError, ValueError):
            return
        if v <= 0 or self.bar_max <= 0:
            return
        frac = v / self.bar_max
        frac = 0.0 if frac < 0 else (1.0 if frac > 1 else frac)
        p = self.pal
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        r = QRectF(opt.rect)
        bw = max(26.0, r.width() - 16)
        x = r.right() - 8 - bw
        h = 3.0
        y = r.bottom() - 5
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(p["track"]))
        painter.drawRoundedRect(QRectF(x, y, bw, h), 1.5, 1.5)
        if frac > 0:
            painter.setBrush(QColor(p["accent"]))
            painter.drawRoundedRect(QRectF(x, y, max(3.0, bw * frac), h), 1.5, 1.5)
        painter.restore()


class ElideMiddleDelegate(RowDelegate):
    """带中间省略的行委托（保持旧类名兼容）。"""

    def __init__(self, column=0, parent=None, level_bold=False):
        super().__init__(parent, elide_col=column, level_bold=level_bold)
        self.column = column


class PathLabel(QLabel):
    """单行路径标签：自动中间省略，悬停看完整路径，双击复制。"""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = text or ""
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setWordWrap(False)
        self.set_path(self._full)

    def set_path(self, p):
        """设置完整路径：显示时省略中间，悬浮提示里给全。"""
        self._full = p or ""
        self.setToolTip(self._full)
        self.setText("—" if not self._full else self._full)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._full:
            super().setText(elide_middle(self._full, self.width() - 4, self.font()))

    def mouseDoubleClickEvent(self, e):
        if self._full:
            QApplication.clipboard().setText(self._full)
            self.setToolTip("已复制到剪贴板：" + self._full)
        super().mouseDoubleClickEvent(e)


# ---------------------------------------------------------------- 勾选框 + 三角
class BigCheckStyle(QProxyStyle):
    """自绘：18×18 圆角勾选框 + 树形展开三角箭头。"""

    SIZE = 18

    def __init__(self, base=None):
        super().__init__(base)
        self.pal = palette(DEFAULT_THEME)

    def set_palette(self, pal):
        """换主题配色（路径标签的强调色跟着主题走）。"""
        self.pal = pal

    def pixelMetric(self, metric, option=None, widget=None):
        if metric in (QStyle.PM_IndicatorWidth, QStyle.PM_IndicatorHeight):
            return self.SIZE
        return super().pixelMetric(metric, option, widget)

    @staticmethod
    def _is_input_button(element, widget):
        """是不是输入框内部那枚按钮的底（下拉三角区 / 微调上下键 / 清除按钮）。

        系统样式会给它们铺一块**直角**底色，正好把输入框自己的圆角啃掉。
        识别出来后直接不画，让父级输入框的圆角背景透出来。
        """
        if element not in (QStyle.PE_PanelButtonTool, QStyle.PE_PanelButtonBevel,
                           QStyle.PE_PanelButtonCommand):
            return False
        if widget is None:
            return False
        if isinstance(widget, (QAbstractSpinBox, QComboBox)):
            return True
        if isinstance(widget, QToolButton):
            return isinstance(widget.parent(),
                              (QLineEdit, QAbstractSpinBox, QComboBox))
        return False

    def drawPrimitive(self, element, option, painter, widget=None):
        if self._is_input_button(element, widget):
            return
        if element == QStyle.PE_IndicatorItemViewItemCheck:
            self._draw_check(option, painter)
            return
        if element == QStyle.PE_IndicatorBranch:
            self._draw_branch(option, painter)
            return
        if element == QStyle.PE_IndicatorArrowDown:
            # 下拉框（单选框）的三角。系统自带的那个在深色主题下几乎看不见，
            # 这里统一画一个跟展开三角同风格的小箭头。
            self._draw_arrow(option, painter, 0, widget)
            return
        if element == QStyle.PE_IndicatorArrowUp:
            self._draw_arrow(option, painter, 180, widget)
            return
        if element == QStyle.PE_IndicatorSpinUp:
            self._draw_arrow(option, painter, 180, widget)
            return
        if element == QStyle.PE_IndicatorSpinDown:
            self._draw_arrow(option, painter, 0, widget)
            return
        if element == QStyle.PE_IndicatorSpinPlus:
            self._draw_arrow(option, painter, 180, widget)
            return
        if element == QStyle.PE_IndicatorSpinMinus:
            self._draw_arrow(option, painter, 0, widget)
            return
        super().drawPrimitive(element, option, painter, widget)

    def _draw_arrow(self, opt, painter, deg, widget=None):
        """画一个小三角。deg=0 朝下，180 朝上。下拉框 / 微调框共用。"""
        p = self.pal
        r = opt.rect
        # 有些分支（微调框的上下键）传进来的可能是整个控件的 rect 而不是按钮的，
        # 那时候画在正中间会跑到输入框上。用短边做个判断，过大就交给底层样式。
        if min(r.width(), r.height()) > 26:
            super().drawPrimitive(
                QStyle.PE_IndicatorArrowDown if deg == 0
                else QStyle.PE_IndicatorArrowUp, opt, painter, widget)
            return
        cx, cy = r.center().x(), r.center().y()
        s = max(4.0, min(9.0, min(r.width(), r.height()) - 2.0))
        hover = bool(opt.state & QStyle.State_MouseOver)
        enabled = bool(opt.state & QStyle.State_Enabled)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        col = QColor(p["accent"] if hover else (p["text_dim"] if enabled
                                                else p["disabled"]))
        painter.setBrush(QBrush(col))
        half = s * 0.42
        if deg == 0:                       # ▼
            pts = QPolygonF([QPointF(cx - half, cy - s * 0.24),
                             QPointF(cx + half, cy - s * 0.24),
                             QPointF(cx, cy + s * 0.30)])
        else:                              # ▲
            pts = QPolygonF([QPointF(cx - half, cy + s * 0.24),
                             QPointF(cx + half, cy + s * 0.24),
                             QPointF(cx, cy - s * 0.30)])
        painter.drawPolygon(pts)
        painter.restore()

    # ---------------------------------------------------------- 勾选框
    def _draw_check(self, opt, painter):
        p = self.pal
        state = opt.state
        checked = bool(state & QStyle.State_On)
        partial = bool(state & QStyle.State_NoChange)
        hover = bool(state & QStyle.State_MouseOver)
        enabled = bool(state & QStyle.State_Enabled)

        s = self.SIZE
        rect = QRectF(opt.rect.center().x() - s / 2.0,
                      opt.rect.center().y() - s / 2.0, s, s)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if checked or partial:
            accent = QColor(p["accent"]) if enabled else QColor(p["disabled"])
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(accent))
            painter.drawRoundedRect(rect, 5, 5)
            pen = QPen(QColor(p["accent_text"]), 2.3)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            x, y = rect.x(), rect.y()
            if partial:
                # 半选：一条横杠
                painter.drawLine(QPointF(x + 4.2, y + s / 2.0),
                                 QPointF(x + s - 4.2, y + s / 2.0))
            else:
                pts = QPolygonF([QPointF(x + 4.0, y + 9.3),
                                 QPointF(x + 7.4, y + 12.8),
                                 QPointF(x + 14.0, y + 5.4)])
                painter.drawPolyline(pts)
        else:
            border = QColor(p["accent"] if hover else p["border_strong"])
            if not enabled:
                border = QColor(p["disabled"])
            painter.setPen(QPen(border, 2.0))
            painter.setBrush(QBrush(QColor(p["input"])))
            painter.drawRoundedRect(rect, 5, 5)
            if hover:
                pen = QPen(with_alpha(p["accent"], 0.45), 2.0)
                pen.setCapStyle(Qt.RoundCap)
                painter.setPen(pen)
                x, y = rect.x(), rect.y()
                pts = QPolygonF([QPointF(x + 4.6, y + 9.3),
                                 QPointF(x + 7.6, y + 12.4),
                                 QPointF(x + 13.4, y + 5.8)])
                painter.drawPolyline(pts)
        painter.restore()

    # ---------------------------------------------------------- 展开三角
    def _draw_branch(self, opt, painter):
        """自绘三角：▶ 折叠 / ▼ 展开。QSS 里 image:none 会把系统箭头抹掉，
        所以这里必须自己画，否则用户看不出哪里还能展开。"""
        p = self.pal
        state = opt.state
        r = opt.rect
        if not (state & QStyle.State_Children):
            # 叶子节点：画一条淡淡的引导线，保留层次感但不抢视线
            painter.save()
            painter.setPen(QPen(QColor(p["border"]), 1))
            cx = r.center().x()
            cy = r.center().y()
            painter.drawLine(QPointF(cx, r.top()), QPointF(cx, r.bottom()))
            painter.drawLine(QPointF(cx, cy), QPointF(r.right() - 2, cy))
            painter.restore()
            return

        opened = bool(state & QStyle.State_Open)
        hover = bool(state & QStyle.State_MouseOver)
        cx, cy = r.center().x(), r.center().y()
        s = max(5.0, min(9.0, r.height() - 5.0, r.width() - 2.0))
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if hover:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(p["hover"]))
            painter.drawRoundedRect(
                QRectF(cx - s * 0.95, cy - s * 0.95, s * 1.9, s * 1.9), 4, 4)
        col = QColor(p["sel_bar"] if opened else p["text_dim"])
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(col))
        if opened:
            pts = QPolygonF([QPointF(cx - s * 0.52, cy - s * 0.26),
                             QPointF(cx + s * 0.52, cy - s * 0.26),
                             QPointF(cx, cy + s * 0.38)])
        else:
            pts = QPolygonF([QPointF(cx - s * 0.28, cy - s * 0.5),
                             QPointF(cx - s * 0.28, cy + s * 0.5),
                             QPointF(cx + s * 0.42, cy)])
            painter.drawPolygon(pts)
        painter.restore()


# ------------------------------------------------- 去重页专用：删除勾选框
class DeleteCheckStyle(BigCheckStyle):
    """去重页「结果树」专用的勾选框：勾上 = 标记删除，画成红色叉叉。

    为什么要单独一套：全局 BigCheckStyle 画的是蓝色对勾 ✓，而对勾的语义是
    「对 / 保留 / 选得好」，放在「这个文件要删掉」的场景里正好是反的。

    这里继承 BigCheckStyle **只重写勾选框**，展开三角、下拉箭头等自绘全部保留。
    注意只能挂在去重结果树上——类型列表、优先目录列表的复选框语义是
    「是否启用」，那些必须保持普通的蓝色对勾，否则会把「启用」也误导成「删除」。
    """

    def _draw_check(self, opt, painter):
        p = self.pal
        state = opt.state
        checked = bool(state & QStyle.State_On)
        partial = bool(state & QStyle.State_NoChange)
        hover = bool(state & QStyle.State_MouseOver)
        enabled = bool(state & QStyle.State_Enabled)

        s = self.SIZE
        rect = QRectF(opt.rect.center().x() - s / 2.0,
                      opt.rect.center().y() - s / 2.0, s, s)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if checked or partial:
            # 红底 + 叉。accent_text 是「强调色底上的文字色」：暗色主题下是深色、
            # 亮色主题下是白色，两种红底上的对比度都够（约 7:1 与 5.8:1）。
            bg = QColor(p["risk"]["danger"]) if enabled else QColor(p["disabled"])
            pen = QPen(QColor(p["accent_text"]), 2.3)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(bg))
            painter.drawRoundedRect(rect, 5, 5)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            x, y = rect.x(), rect.y()
            if partial:
                # 半选（组里只勾了一部分）：一条横杠
                painter.drawLine(QPointF(x + 4.2, y + s / 2.0),
                                 QPointF(x + s - 4.2, y + s / 2.0))
            else:
                m = 5.2                              # 叉到框边的留白
                painter.drawLine(QPointF(x + m, y + m),
                                 QPointF(x + s - m, y + s - m))
                painter.drawLine(QPointF(x + s - m, y + m),
                                 QPointF(x + m, y + s - m))
        else:
            # 未勾选：跟普通勾选框一样的空框；鼠标停上去时预览一个淡红叉
            border = QColor(p["border_strong"] if not hover
                            else p["risk"]["danger"])
            if not enabled:
                border = QColor(p["disabled"])
            painter.setPen(QPen(border, 2.0))
            painter.setBrush(QBrush(QColor(p["input"])))
            painter.drawRoundedRect(rect, 5, 5)
            if hover:
                pen = QPen(with_alpha(p["risk"]["danger"], 0.5), 2.0)
                pen.setCapStyle(Qt.RoundCap)
                painter.setPen(pen)
                x, y = rect.x(), rect.y()
                m = 5.6
                painter.drawLine(QPointF(x + m, y + m),
                                 QPointF(x + s - m, y + s - m))
                painter.drawLine(QPointF(x + s - m, y + m),
                                 QPointF(x + m, y + s - m))
        painter.restore()
