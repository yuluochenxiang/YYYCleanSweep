"""目录分析页（类 WizTree）与标记管理页。

目录分析：扫描任意目录/盘符，按层展示占用，标注每个目录的作用与处置建议，
并按文件类型统计构成；可对目录打白名单 / 可清理 / 备注标记。

右侧区域以前塞了 8 列 + 一张类型表 + 6 个按钮，挤得看不清。现在改成
「图表 + 悬浮提示」：表格只留 6 列，作用/建议/最近修改全部收进行悬浮提示；
右侧换成可滚动的卡片流（环形占比 → 指标 → 作用建议 → 类型构成 → 标记）。
"""
import os
import time
import subprocess

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QTreeWidgetItem, QComboBox, QSplitter, QFrame, QTextBrowser,
    QHeaderView, QMessageBox, QInputDialog, QAbstractItemView,
    QSpinBox, QCheckBox, QScrollArea, QTableWidget,
    QTableWidgetItem, QMenu, QApplication, QStackedWidget,
)

from core.dirstat import DirTree
from core import cache as scan_cache
from core.knowledge import knowledge, filetypes, KIND_LABEL, RISK_LABEL
from core.marks import MarkStore, MARK_KEEP, MARK_JUNK, MARK_LABEL
from core.sizefmt import fmt_bytes, fmt_pct, pct_of, parse_pct
from ui.theme import palette, DEFAULT_THEME
from ui.charts import Donut, TypeBars
from ui.treemap import TreemapView, COLOR_MODES
from ui.busy import ScanBanner
from ui.sound import attach_menu, act, NullSfx
from ui.widgets import (
    fmt, fmt_datetime, chip, esc, short_path,
    RowDelegate, PathLabel, enable_hover, setup_tree, HeaderSorter,
    FirstColFit, page_layout, top_bar, PathTreeWidget,
)

SORT_KEYS = ["size", "count", "name", "mtime", "atime", "risk"]
SORT_LABELS = ["按占用", "按文件数", "按名称", "按最近修改", "按最后访问", "按风险"]
# 「多久没打开」下拉对应的月数（与下拉文案一一对应，0 = 不限）
IDLE_MONTHS = [0, 6, 12, 36]
# 按风险排序时：越值得动手的排越前
RISK_PRI = {"safe": 3, "caution": 2, "migrate": 2, "keep": 1, "danger": 0}


class ScanThread(QThread):
    """后台建目录树。扫一整个盘很慢，进度按「已扫目录数」回报。"""
    progress = Signal(int, str)
    finished_all = Signal(object)

    def __init__(self, root, depth, parent=None):
        super().__init__(parent)
        self.root = root
        self.depth = depth
        self.tree = None
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        self.tree = DirTree(self.root)
        self.tree.scan(
            max_depth=self.depth,
            on_progress=lambda n, p: self.progress.emit(n, p),
            should_cancel=lambda: self._cancel)
        self.finished_all.emit(self.tree)


class DeepenThread(QThread):
    """对某个目录再往下挖三层（用户双击「还有子目录」时触发）。"""
    finished_all = Signal(bool)

    def __init__(self, tree, path, parent=None):
        super().__init__(parent)
        self.tree = tree
        self.path = path

    def run(self):
        ok = self.tree.deepen(self.path, extra_depth=3)
        self.finished_all.emit(ok)


class DirScanPage(QWidget):
    """目录分析页：扫出一棵目录树，逐个说明这个目录是干什么的、能不能动。

    目录说明来自离线知识库（core/knowledge.py，400+ 条），匹配不到时给基于
    位置和名称的兜底推断——所以任何目录都有解释，不会显示「未知」。

    列数从 8 减到 6：作用/建议与最近修改改成悬浮提示，不再占横向空间。
    """
    COLS = ["目录", "占用", "占比", "文件数", "最后访问", "主要类型", "标记"]

    def __init__(self, marks: MarkStore, cfg=None, parent=None):
        super().__init__(parent)
        self.marks = marks
        self.cfg = cfg
        self.sfx = getattr(parent, "sfx", None) or NullSfx()
        self.kb = knowledge()
        self.ft = filetypes()
        self.pal = palette(DEFAULT_THEME)
        self.tree = None
        self.thread = None
        self.deep_thread = None
        self.sort_key = "size"
        self.min_mb = 0
        self.hide_marked = False
        self._cache = {}
        # 空间快照：本次 / 上一次的批次时间戳，供顶部「空间变化」卡片对比
        self._snap_prev = None
        self._snap_cur = None
        if self.cfg:
            self.sort_key = SORT_KEYS[self.cfg.get("sort_dir", 0)] \
                if self.cfg.get("sort_dir", 0) < len(SORT_KEYS) else "size"
            self.min_mb = int(self.cfg.get("dir_min_mb", 0) or 0)
            self.hide_marked = bool(self.cfg.get("dir_hide_marked", False))
            self.idle_months = int(self.cfg.get("dir_idle_months", 0) or 0)
        else:
            self.idle_months = 0
        # 扫描位置改由主窗口顶部的统一「目标」选择器提供（各页独立记忆），
        # 这里只保存当前值，兼容旧的 dir_root 配置键。
        self.root_path = ""
        if self.cfg:
            self.root_path = (self.cfg.get("target_dir")
                              or self.cfg.get("dir_root") or "")
        self._build()

    def set_root(self, path):
        """顶部统一目标改了本页的扫描位置。"""
        self.root_path = (path or "").strip()

    # ---------------- UI ----------------
    def _build(self):
        lay = page_layout(self)

        bar, bl = top_bar()
        bl.addWidget(QLabel("扫描位置在窗口顶部选，深度决定展开几层。"))
        bl.addStretch(1)
        bl.addWidget(QLabel("深度"))
        self.cb_depth = QComboBox()
        self.cb_depth.addItems(["2 层", "3 层", "4 层", "5 层", "6 层"])
        self.cb_depth.setCurrentIndex(int(self.cfg.get("dir_depth", 2) or 2)
                                      if self.cfg else 2)
        bl.addWidget(self.cb_depth)
        self.btn_scan = QPushButton("开始扫描")
        self.btn_scan.setObjectName("primary")
        self.btn_scan.clicked.connect(self.start_scan)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_scan)
        bl.addWidget(self.btn_scan)
        bl.addWidget(self.btn_stop)
        lay.addWidget(bar)

        bar2 = QFrame()
        bl2 = QHBoxLayout(bar2)
        bl2.setContentsMargins(0, 0, 0, 0)
        bl2.setSpacing(10)
        bl2.addWidget(QLabel("排序"))
        self.cb_sort = QComboBox()
        self.cb_sort.addItems(SORT_LABELS)
        self.cb_sort.setCurrentIndex(
            SORT_KEYS.index(self.sort_key) if self.sort_key in SORT_KEYS else 0)
        self.cb_sort.currentIndexChanged.connect(self._on_sort_changed)
        bl2.addWidget(self.cb_sort)
        bl2.addWidget(QLabel("只看大于"))
        self.sp_min = QSpinBox()
        self.sp_min.setRange(0, 100000)
        self.sp_min.setSuffix(" MB")
        self.sp_min.setSingleStep(50)
        self.sp_min.setValue(self.min_mb)
        self.sp_min.valueChanged.connect(self._on_min_changed)
        bl2.addWidget(self.sp_min)
        self.chk_hide = QCheckBox("隐藏已标记的目录")
        self.chk_hide.setChecked(self.hide_marked)
        self.chk_hide.stateChanged.connect(self._on_hide_changed)
        bl2.addWidget(self.chk_hide)

        bl2.addWidget(QLabel("多久没打开"))
        self.cb_idle = QComboBox()
        self.cb_idle.addItems(["不限", "6 个月没打开", "1 年没打开", "3 年没打开"])
        self.cb_idle.setCurrentIndex(
            IDLE_MONTHS.index(self.idle_months)
            if self.idle_months in IDLE_MONTHS else 0)
        self.cb_idle.setToolTip(
            "只看该目录下所有文件都已经这么久没被打开过（按最近一次访问时间算）")
        self.cb_idle.currentIndexChanged.connect(self._on_idle_changed)
        bl2.addWidget(self.cb_idle)
        self.btn_view = QPushButton("方块图")
        self.btn_view.setToolTip("在树形列表与方块图（面积=占用）之间切换")
        self.btn_view.clicked.connect(self._toggle_view)
        bl2.addWidget(self.btn_view)
        self.btn_up = QPushButton("返回上级")
        self.btn_up.setToolTip("方块图下钻后回到上一层目录")
        self.btn_up.setEnabled(False)
        self.btn_up.clicked.connect(self._treemap_up)
        bl2.addWidget(self.btn_up)
        self.btn_color = QPushButton("配色：文件类型")
        self.btn_color.setToolTip(
            "方块图的颜色代表什么：文件类型 / 子文件夹 / 风险等级（点一下换一种）")
        self.btn_color.clicked.connect(self._cycle_color_mode)
        self.btn_color.setEnabled(False)      # 只有切到方块图才有意义
        bl2.addWidget(self.btn_color)
        bl2.addStretch(1)
        lb_hover = QLabel("把鼠标停在行上可看完整说明 · 右键可直接标记")
        lb_hover.setObjectName("hint")
        bl2.addWidget(lb_hover)
        lay.addWidget(bar2)

        # ---- 空间变化对比卡片（B）：直接回答「是谁涨上去的」
        self.box_snap = QFrame()
        self.box_snap.setObjectName("card")
        snl = QHBoxLayout(self.box_snap)
        snl.setContentsMargins(14, 9, 14, 9)
        snl.setSpacing(10)
        lb_sn = QLabel("空间变化")
        lb_sn.setObjectName("muted")
        snl.addWidget(lb_sn)
        self.lb_snap = QLabel("尚未对比")
        self.lb_snap.setTextInteractionFlags(Qt.TextBrowserInteraction)
        snl.addWidget(self.lb_snap)
        snl.addSpacing(6)
        # 增长最快的几个做成可点的按钮：点一下右侧详情就跳过去
        self.w_snap_top = QWidget()
        self.snap_top_lay = QHBoxLayout(self.w_snap_top)
        self.snap_top_lay.setContentsMargins(0, 0, 0, 0)
        self.snap_top_lay.setSpacing(6)
        snl.addWidget(self.w_snap_top)
        snl.addStretch(1)
        lay.addWidget(self.box_snap)
        self.box_snap.hide()          # 有扫描结果才露面，避免开着图上空白卡片

        split = QSplitter(Qt.Horizontal)

        # 用带「路径高亮」的树：展开几层之后，当前分支的层级线会变成强调色
        self.tw = PathTreeWidget()
        self.tw.setHeaderLabels(self.COLS)
        self.tw.setAlternatingRowColors(True)
        # 顶层也要有展开三角：不开 RootIsDecorated 的话用户看不出能点开
        setup_tree(self.tw)
        self.tw.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tw.setItemDelegate(
            RowDelegate(self.tw, elide_col=0, bar_col=1, level_bold=True))
        enable_hover(self.tw)
        hh = self.tw.header()
        hh.setMinimumSectionSize(70)
        hh.setSectionResizeMode(0, QHeaderView.Interactive)   # 文件名列：可手动调宽看全
        for c in (1, 2, 3, 4, 5):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(6, QHeaderView.Stretch)        # 末列吸收剩余宽度
        self.tw.setColumnWidth(0, 300)
        # 拉宽 / 缩窄窗口时第一列（目录）跟着伸缩，其余列各有最小宽不被挤扁
        FirstColFit(self.tw, min_w=280,
                    others={1: 104, 2: 92, 3: 84, 4: 104, 5: 124, 6: 84})
        self.tw.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # 点表头排序：占用按真实字节、占比按百分比、文件数按整数
        self.sorter = HeaderSorter(self.tw, key_fn=self._sort_key,
                                   numeric_cols=(1, 2),
                                   default_col=1, default_desc=True,
                                   asc_first=(0,), on_sorted=self._on_header_sorted)
        self.tw.itemExpanded.connect(self._on_expanded)
        self.tw.currentItemChanged.connect(self._on_select)
        self.tw.setMouseTracking(True)
        self.tw.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tw.customContextMenuRequested.connect(self._menu)

        # 方块图（Treemap）：面积=占用，与树表同位切换，共用右侧详情
        self.treemap = TreemapView(self)
        self.treemap.set_palette(self.pal)
        # 「按风险」配色要用到知识库结论，直接复用本页的 _info（带缓存）
        self.treemap.risk_fn = self._info
        # 方块图取子节点走这里，才能跟上「排序 / 只看大于 / 隐藏已标记」三个筛选
        self.treemap.kids_fn = self._treemap_kids
        self.treemap.setContextMenuPolicy(Qt.CustomContextMenu)
        self.treemap.customContextMenuRequested.connect(self._treemap_menu)
        self.treemap.path_clicked.connect(self._show_detail)
        self.treemap.path_activated.connect(self._treemap_drill)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.tw)
        self.stack.addWidget(self.treemap)
        split.addWidget(self.stack)

        split.addWidget(self._build_side())
        split.setSizes([760, 360])
        lay.addWidget(split, 1)

        # ---- 底部通用操作条：标记按钮组原来在右侧滚动区里，默认尺寸下要滚到底
        # 才看得见，很多人根本不知道有这功能。挪到底部后和项目清理页 / 文件去重页一致。
        self.lb_stat = QLabel("选择目录后点击「开始扫描」")
        self.lb_stat.setObjectName("muted")
        lay.addWidget(self._build_bottom())
        # 扫描中：与项目清理页 / 大文件 / 文件去重页同一个状态条、同一个位置（底部）
        self.banner = ScanBanner(self)
        lay.addWidget(self.banner)

    def _build_side(self):
        """右侧：可滚动的卡片流，图表打头，文字与按钮不挤在一起。"""
        side = QFrame()
        side.setObjectName("detail")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(12, 12, 12, 12)
        il.setSpacing(10)

        # ---- 卡片 1：目录概要
        c1 = QFrame()
        c1.setObjectName("card")
        c1l = QVBoxLayout(c1)
        c1l.setContentsMargins(14, 12, 14, 12)
        c1l.setSpacing(8)
        self.lb_title = QLabel("目录详情")
        self.lb_title.setObjectName("h1")
        c1l.addWidget(self.lb_title)
        self.lb_path = PathLabel("—")
        self.lb_path.setObjectName("hint")
        c1l.addWidget(self.lb_path)
        row = QHBoxLayout()
        row.setSpacing(12)
        self.donut = Donut(c1, size=112)
        row.addWidget(self.donut)
        gl = QGridLayout()
        gl.setSpacing(6)
        self.lb_self = QLabel("—")
        self.lb_all = QLabel("—")
        self.lb_count = QLabel("—")
        self.lb_mtime = QLabel("—")
        for i, (cap, w) in enumerate(
                (("自身文件", self.lb_self), ("含子目录", self.lb_all),
                 ("文件数", self.lb_count), ("最近修改", self.lb_mtime))):
            lb = QLabel(cap)
            lb.setObjectName("hint")
            w.setObjectName("muted")
            gl.addWidget(lb, i, 0)
            gl.addWidget(w, i, 1)
        row.addLayout(gl, 1)
        c1l.addLayout(row)
        il.addWidget(c1)

        # ---- 卡片 2：作用与建议
        c2 = QFrame()
        c2.setObjectName("card")
        c2l = QVBoxLayout(c2)
        c2l.setContentsMargins(14, 12, 14, 12)
        c2l.setSpacing(8)
        t2 = QLabel("作用与建议")
        t2.setObjectName("h2")
        c2l.addWidget(t2)
        self.tx_info = QTextBrowser()
        self.tx_info.setMinimumHeight(80)
        self.tx_info.setMaximumHeight(150)     # 别让它把整栏撑满、把下面的卡挤下去
        c2l.addWidget(self.tx_info)
        self.lb_mark = QLabel("未标记")
        self.lb_mark.setWordWrap(True)
        self.lb_mark.setObjectName("muted")
        c2l.addWidget(self.lb_mark)
        il.addWidget(c2)

        # ---- 卡片 3：类型构成
        c3 = QFrame()
        c3.setObjectName("card")
        c3l = QVBoxLayout(c3)
        c3l.setContentsMargins(14, 12, 14, 12)
        c3l.setSpacing(8)
        t3 = QLabel("文件类型构成")
        t3.setObjectName("h2")
        c3l.addWidget(t3)
        self.bars = TypeBars(max_rows=8)
        c3l.addWidget(self.bars)
        il.addWidget(c3)

        il.addStretch(1)
        scroll.setWidget(inner)
        sl.addWidget(scroll)
        side.setFixedWidth(384)      # 略宽一点，"最近修改"这类日期才不会被截断
        return side

    def _build_bottom(self):
        """底部通用操作条：对当前选中目录的标记操作。

        放在底部而不是右侧滚动区里，是因为右侧要滚到底才看得到，
        默认窗口高度下几乎没人会发现它。
        """
        bot = QFrame()
        bot.setObjectName("card")
        bbl = QHBoxLayout(bot)
        bbl.setContentsMargins(14, 9, 14, 9)
        bbl.setSpacing(8)
        bbl.addWidget(self.lb_stat)
        bbl.addStretch(1)

        self.b_keep = QPushButton("白名单")
        self.b_keep.setObjectName("ok")
        self.b_keep.setToolTip("标记为我认定不可删除的目录，清理时自动跳过")
        self.b_junk = QPushButton("可清理")
        self.b_junk.setObjectName("warn")
        self.b_junk.setToolTip("标记为确认可以清理的目录")
        self.b_note = QPushButton("写备注")
        self.b_note.setToolTip("给这个目录写一句说明，方便以后回想起来")
        self.b_clear = QPushButton("清除标记")
        self.b_clear.setObjectName("danger")
        self.b_open = QPushButton("打开位置")
        self.b_deep = QPushButton("深入扫描")
        self.b_deep.setToolTip("这个目录下面还有更深层没扫到，点这里继续往下挖 3 层")
        for b in (self.b_keep, self.b_junk, self.b_note):
            b.clicked.connect(self._on_mark)
        self.b_clear.clicked.connect(self._clear_mark)
        self.b_open.clicked.connect(self._open_dir)
        self.b_deep.clicked.connect(self._deepen)
        for b in (self.b_keep, self.b_junk, self.b_note, self.b_clear,
                  self.b_open, self.b_deep):
            bbl.addWidget(b)
        return bot

    def on_theme(self, theme):
        """主题切换：重取调色板，刷新行委托与图表配色。"""
        self.pal = palette(theme)
        self.donut.set_palette(self.pal)
        self.bars.set_palette(self.pal)
        self.banner.set_palette(self.pal)
        self.treemap.set_palette(self.pal)
        self._rebuild()
        self._sync_treemap()
        cur = self.tw.currentItem()
        if cur:
            self._show_detail(cur.data(0, Qt.UserRole))

    # ---------------- 扫描 ----------------
    def start_scan(self):
        """开始扫描：起线程、显示扫描条，结束后刷新整棵树。"""
        if self.thread and self.thread.isRunning():
            return
        root = os.path.expandvars((self.root_path or "").strip())
        if not root:
            QMessageBox.information(
                self, "先选位置", "请在窗口顶部「扫描位置」里选择要分析的盘或目录。")
            return
        root = os.path.normpath(root)
        if not os.path.isdir(root):
            QMessageBox.warning(self, "路径无效", f"目录不存在：{root}")
            return
        if self.cfg:
            self.cfg.set("target_dir", root)
        self.btn_scan.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.tw.clear()
        self._cache.clear()
        self.treemap.clear()
        self.btn_up.setEnabled(False)
        # 新结果还没出来，右侧详情留着上一次的目录最容易让人看错，先恢复初始态
        self._clear_detail()
        # 旧的空间变化同样失效，先收起来，等这次扫完再刷新
        self.box_snap.hide()
        self._snap_prev = None
        self._snap_cur = None
        depth = self.cb_depth.currentIndex() + 2
        if self.cfg:
            self.cfg.set("dir_depth", self.cb_depth.currentIndex())
        self.banner.start(f"正在扫描 {root}", "准备中…", on_stop=self.stop_scan)
        self.thread = ScanThread(root, depth, self)
        self.thread.progress.connect(
            lambda n, p: self.banner.set_sub(f"已扫描 {n} 个目录 · {p}"))
        self.thread.finished_all.connect(self._on_done)
        self.thread.start()

    def stop_scan(self):
        """停止扫描：置取消标志，线程会在下一个检查点收手。"""
        if self.thread:
            self.thread.cancel()
        self.btn_stop.setEnabled(False)

    def _on_done(self, tree):
        self.tree = tree
        self.btn_scan.setEnabled(True)
        self.btn_stop.setEnabled(False)
        total = tree.total()
        self.banner.finish(
            "扫描完成" if not tree.stopped else "已中止",
            f"{tree.scanned_dirs} 个目录 · {fmt(total)}")
        self.lb_stat.setText(
            f"共 <b>{tree.scanned_dirs}</b> 个目录 · 总计 <b>{fmt(total)}</b>"
            + ("（已中止）" if tree.stopped else ""))
        QTimer.singleShot(1600, self.banner.hide)
        self._rebuild()
        self._sync_treemap()
        # 中止的扫描不留快照：残缺的树偏小，拿它当基准下次会虚报一堆「暴涨」
        if not tree.stopped:
            self._save_snapshot(tree)
        self._refresh_snap_card()

    # ---------------- 空间快照（B）----------------
    def _save_snapshot(self, tree):
        """把这次扫描的每个目录占用留一份，并记住「上一次」的批次时间戳。

        注意顺序：先记下旧的批次，再写入新的——反过来的话 prev 会等于自己，
        对比结果永远全是 0。
        """
        prev_list = scan_cache.snapshot_batches(tree.root, limit=1)
        prev = prev_list[0] if prev_list else None
        rows = [(nd.path, nd.agg_size, nd.agg_count)
                for nd in tree.nodes.values() if nd.agg_size > 0]
        cur = None
        if rows:
            scan_cache.set_snapshot_keep(
                int(self.cfg.get("snap_keep", 30) or 30) if self.cfg else 30)
            cur = scan_cache.put_snapshot(tree.root, rows)
        self._snap_prev = prev
        self._snap_cur = cur

    def _clear_snap_top(self):
        """清掉「增长最快」那几个按钮，防止多次扫描叠加堆积。"""
        lay = self.snap_top_lay
        while lay.count():
            it = lay.takeAt(0)
            wd = it.widget()
            if wd is not None:
                wd.setParent(None)
                wd.deleteLater()

    def _refresh_snap_card(self):
        """刷新顶部「空间变化」卡片。"""
        self._clear_snap_top()
        if not self.tree:
            self.box_snap.hide()
            return
        prev = self._snap_prev
        cur = self._snap_cur
        if not prev or not cur:
            self.lb_snap.setText(
                "已记录本次扫描结果，<b>下次再扫一次</b>就能看出哪些目录在变大。")
            self.box_snap.show()
            return
        rows = scan_cache.diff_snapshots(self.tree.root, prev, cur, limit=200)
        grown = [r for r in rows if r[3] > 0]
        up = sum(r[3] for r in grown)
        down = -sum(r[3] for r in rows if r[3] < 0)
        dt = time.strftime("%m月%d日", time.localtime(prev))
        # 间隔文案：同一天连扫两次时若写死「1 天前」很荒唐，按时长自动降档
        gap = max(0.0, cur - prev)
        if gap < 3600:
            ago = f"{max(1, int(round(gap / 60.0)))} 分钟前"
        elif gap < 86400:
            ago = f"{max(1, int(round(gap / 3600.0)))} 小时前"
        else:
            ago = f"{max(1, int(round(gap / 86400.0)))} 天前"
        if up <= 0:
            self.lb_snap.setText(
                f"相比 {dt}（{ago}），<b>没有变大的目录</b>"
                + (f"，反倒少了 {fmt(down)}。" if down > 0 else "。"))
            self.box_snap.show()
            return
        txt = (f"相比 {dt}（{ago}），<b>{short_path(self.tree.root)}</b> "
               f"增加 <b>{fmt(up)}</b>")
        if down > 0:
            txt += f"，同时减少 {fmt(down)}"
        self.lb_snap.setText(txt)
        for p, _osz, _nsz, delta in grown[:3]:
            btn = QPushButton(
                f"{os.path.basename(p.rstrip('\\/')) or p} +{fmt(delta)}")
            btn.setObjectName("mini")
            btn.setToolTip(p)
            btn.clicked.connect(lambda _c=False, p=p: self._show_detail(p))
            self.snap_top_lay.addWidget(btn)
        self.box_snap.show()

    # 表头列 -> 排序方式（点表头时让上面的「排序」下拉也跟着变，避免两处打架）
    COL_SORT = {0: "name", 1: "size", 2: "size", 3: "count", 4: "atime"}

    def _sort_key(self, col, item):
        """占比按百分比、文件数按整数、最后访问按时间戳，其余交给默认规则。"""
        if item is None:
            return None
        if col == 2:
            return parse_pct(item.text(2))
        if col == 3:
            digits = "".join(ch for ch in item.text(3) if ch.isdigit())
            return int(digits or 0)
        if col == 4:
            v = item.data(4, Qt.UserRole)
            return float(v) if v else 0.0
        return None

    def _on_header_sorted(self, col, desc):
        key = self.COL_SORT.get(col)
        if not key or key == self.sort_key:
            return
        i = SORT_KEYS.index(key)
        self.sort_key = key
        self.cb_sort.blockSignals(True)
        self.cb_sort.setCurrentIndex(i)
        self.cb_sort.blockSignals(False)
        if self.cfg:
            self.cfg.set("sort_dir", i)
        self._rebuild()

    def _on_sort_changed(self, i):
        if 0 <= i < len(SORT_KEYS):
            self.sort_key = SORT_KEYS[i]
            if self.cfg:
                self.cfg.set("sort_dir", i)
            self._rebuild()

    def _on_min_changed(self, v):
        self.min_mb = v
        if self.cfg:
            self.cfg.set("dir_min_mb", v)
        self._rebuild()

    def _on_hide_changed(self, _s):
        self.hide_marked = self.chk_hide.isChecked()
        if self.cfg:
            self.cfg.set("dir_hide_marked", self.hide_marked)
        self._rebuild()

    def _on_idle_changed(self, i):
        self.idle_months = IDLE_MONTHS[i] if 0 <= i < len(IDLE_MONTHS) else 0
        if self.cfg:
            self.cfg.set("dir_idle_months", self.idle_months)
        self._rebuild()

    # ---------------- 方块图（Treemap）----------------
    def _toggle_view(self):
        """在树形列表与方块图之间切换（同一位置，共用右侧详情）。"""
        to_map = self.stack.currentIndex() == 0
        self.stack.setCurrentIndex(1 if to_map else 0)
        self.btn_view.setText("树表" if to_map else "方块图")
        self.btn_color.setEnabled(to_map)
        if to_map:
            self._sync_treemap()
            self.btn_up.setEnabled(
                bool(self.tree and self.treemap.root
                     and self.treemap.root != self.tree.root))
        else:
            self.btn_up.setEnabled(False)

    def _cycle_color_mode(self):
        """轮换方块图的颜色含义：文件类型 → 子文件夹 → 风险。"""
        ids = [m[0] for m in COLOR_MODES]
        labels = dict(COLOR_MODES)
        cur = self.treemap.color_mode
        i = ids.index(cur) if cur in ids else 0
        nxt = ids[(i + 1) % len(ids)]
        self.treemap.set_color_mode(nxt)
        self.btn_color.setText("配色：" + labels[nxt].replace("按", ""))

    def _treemap_kids(self, path):
        """方块图取子节点：复用树表的排序，并套用「只看大于 / 隐藏已标记」。"""
        if not self.tree:
            return []
        limit = (self.min_mb or 0) * 1024 * 1024
        # 方块图必须与树表同源：树表有的筛选这里一个都不能少，否则两视图结果不一致
        idle_cut = 0.0
        if self.idle_months > 0:
            idle_cut = time.time() - self.idle_months * 30.5 * 86400
        out = []
        for k in self._sorted_children(path):
            if k.agg_size <= 0:
                continue
            if limit and k.agg_size < limit:
                continue
            if idle_cut and k.agg_atime > 0 and k.agg_atime > idle_cut:
                continue
            if self.hide_marked and self.marks.effective(k.path)[0]:
                continue
            out.append(k)
        return out

    def _sync_treemap(self):
        """扫描完成 / 换主题后，把最新树同步给方块图。"""
        if not self.tree:
            return
        self.treemap.set_palette(self.pal)
        self.treemap.set_tree(self.tree, self.treemap.root or self.tree.root)

    def _treemap_drill(self, path):
        """双击方块：以该目录为根重新布局（下钻）。"""
        if not self.tree:
            return
        self.treemap.set_tree(self.tree, path)
        self.btn_up.setEnabled(path != self.tree.root)
        self._show_detail(path)

    def _treemap_up(self):
        """返回上级：回到父目录为根。"""
        if not self.tree or not self.treemap.root:
            return
        nd = self.tree.node(self.treemap.root)
        parent = getattr(nd, "parent", None)
        if parent:
            self._treemap_drill(parent)

    def _treemap_menu(self, pos):
        """方块图右键：复用树表那套菜单（按路径构造，不用先选中再点按钮）。"""
        path = self.treemap.sel
        if not path:
            return
        m = self._make_menu(path)
        m.exec(self.treemap.mapToGlobal(pos))

    def _rebuild(self):
        self.tw.blockSignals(True)
        self.tw.clear()
        self.tw.blockSignals(False)
        if not self.tree:
            return
        total = self.tree.total() or 1
        self.tw.itemDelegate().set_bar_max(total)
        self._add_children(None, self.tree.root)
        # 扫到的目录本身没有子目录时，列表会是空的；把根目录自己列出来，
        # 免得用户以为扫描失败
        if self.tw.topLevelItemCount() == 0:
            node = self.tree.node(self.tree.root)
            if node and (node.own_count or node.agg_count):
                it = self._make_item(node, total)
                if it:
                    self.tw.addTopLevelItem(it)
        # 重建后按当前表头排序恢复顺序（懒加载出来的子层也跟着排）
        if getattr(self, "sorter", None):
            self.sorter.sort_now()
        # 方块图正显示着的话，排序 / 筛选一变就得跟着重铺
        if getattr(self, "stack", None) and self.stack.currentIndex() == 1:
            self._sync_treemap()

    def _sorted_children(self, path):
        kids = self.tree.children(path, sort_key="size")
        if self.sort_key == "count":
            kids.sort(key=lambda x: x.agg_count, reverse=True)
        elif self.sort_key == "name":
            kids.sort(key=lambda x: x.name.lower())
        elif self.sort_key == "mtime":
            kids.sort(key=lambda x: x.agg_mtime, reverse=True)
        elif self.sort_key == "atime":
            kids.sort(key=lambda x: x.agg_atime, reverse=True)
        elif self.sort_key == "risk":
            kids.sort(key=lambda x: (-RISK_PRI.get(
                self._info(x.path)["risk"], 0), -x.agg_size))
        return kids

    def _row_tip(self, path, nd, info, mark, note, inherited, total):
        """行悬浮提示：把被精简掉的列都放进来，鼠标一停就看得全。"""
        lines = [nd.name, path, ""]
        lines.append(f"含子目录 {fmt(nd.agg_size)}（{fmt_bytes(nd.agg_size)} 字节）"
                     f"　占本次扫描 {fmt_pct(nd.agg_size, total)}")
        lines.append(f"自身文件 {fmt(nd.own_size)}　文件数 {nd.agg_count}")
        lines.append(f"最近修改 {fmt_datetime(nd.agg_mtime)}")
        if nd.agg_atime > 0:
            tail_a = f"最后访问 {fmt_datetime(nd.agg_atime)}"
            if self.tree and not self.tree.atime_reliable:
                tail_a += "（本机未记录访问时间，显示的是修改时间）"
            lines.append(tail_a)
        lines.append("")
        lines.append(f"作用：{info['purpose']}")
        lines.append(f"建议：{info['advice']}")
        tail = []
        if mark:
            tail.append(MARK_LABEL.get(mark, ""))
        if note:
            tail.append("备注：" + note)
        if inherited:
            tail.append("（继承自上级目录）")
        if tail:
            lines.append("")
            lines.append(" · ".join(x for x in tail if x))
        return "\n".join(lines)

    def _make_item(self, nd, total):
        """给一个目录节点造列表行。"""
        mark, note, _owner, inherited = self.marks.effective(nd.path)
        info = self._info(nd.path)
        it = QTreeWidgetItem([
            nd.name,
            ("≥ " if nd.truncated else "") + fmt(nd.agg_size),
            fmt_pct(nd.agg_size, total),
            str(nd.agg_count),
            fmt_datetime(nd.agg_atime) if nd.agg_atime > 0 else "—",
            self._main_cat(nd),
            self._mark_text(mark, note, inherited),
        ])
        it.setData(0, Qt.UserRole, nd.path)
        it.setData(1, Qt.UserRole, nd.agg_size)          # 给占比条用
        it.setData(4, Qt.UserRole, float(nd.agg_atime))  # 给「按最后访问」排序用
        it.setToolTip(0, self._row_tip(nd.path, nd, info, mark, note,
                                       inherited, total))
        it.setToolTip(1, f"{fmt(nd.agg_size)}（{fmt_bytes(nd.agg_size)} 字节）")
        for c in (1, 2, 3, 4):
            it.setTextAlignment(c, Qt.AlignRight | Qt.AlignVCenter)
        self._paint(it, mark, note, info["risk"])
        if nd.children or nd.truncated:
            it.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        else:
            it.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicator)
        return it

    def _add_children(self, parent_item, path):
        node = self.tree.node(path)
        if not node:
            return
        kids = self._sorted_children(path)
        min_b = self.min_mb * 1024 * 1024
        total = self.tree.total() or 1
        # 「多久没打开」：目录整体的最后访问时间早于这个时刻才算命中。
        # 完全没扫到文件的目录（agg_atime 为 0）不筛选——它们往往读不了，
        # 不能因为读不到时间就把它算成「几年没打开」而错误地推荐给用户。
        idle_cut = 0.0
        if self.idle_months > 0:
            idle_cut = time.time() - self.idle_months * 30.5 * 86400
        for nd in kids:
            if nd.agg_size < min_b:
                continue
            if idle_cut and nd.agg_atime > 0 and nd.agg_atime > idle_cut:
                continue
            mark, note, _owner, _inherited = self.marks.effective(nd.path)
            if self.hide_marked and (mark or note):
                continue
            if nd.agg_count == 0 and not nd.children:
                continue
            it = self._make_item(nd, total)
            if parent_item is None:
                self.tw.addTopLevelItem(it)
            else:
                parent_item.addChild(it)

    def _paint(self, it, mark, note, risk):
        p = self.pal
        kind = mark if mark else ("note" if note else "")
        if kind:
            c = QColor(p["tint"][kind])
            fg = QColor(p["tint_fg"][kind])
            for col in range(len(self.COLS)):
                it.setBackground(col, c)
            it.setForeground(6, fg)          # 6 = 标记列（加列后从 5 改过来）
            it.setForeground(0, QColor(p["text"]))
        else:
            rc = p["risk"].get(risk, p["risk"]["keep"])
            it.setForeground(2, QColor(p["text_dim"]))
            it.setForeground(5, QColor(rc))  # 5 = 主要类型列（加列后从 4 改过来）

    def _info(self, path):
        if path not in self._cache:
            self._cache[path] = self.kb.lookup(path)
        return self._cache[path]

    def _main_cat(self, nd):
        if not nd.agg_ext or nd.agg_size == 0:
            return "—"
        groups = self.ft.group(nd.agg_ext)
        if not groups:
            return "—"
        cid, (size, _c) = groups[0]
        name = (next((c["name"] for c in self.ft.cats if c["id"] == cid), None)
                or self.ft.fallback["name"])
        return f"{name} {size * 100 / nd.agg_size:.0f}%"

    def _mark_text(self, mark, note, inherited):
        t = MARK_LABEL.get(mark, "")
        if inherited and t:
            t += "（继承）"
        if note:
            t = (t + " " if t else "") + "📝 " + note
        return t or ""

    def _on_expanded(self, item):
        if item.childCount() > 0:
            return
        path = item.data(0, Qt.UserRole)
        node = self.tree.node(path) if self.tree else None
        if not node:
            return
        if node.truncated and not node.children:
            self._deepen_path(path)
            return
        self._add_children(item, path)
        if getattr(self, "sorter", None):
            self.sorter.sort_children(item)

    # ---------------- 详情 ----------------
    def _clear_detail(self):
        """右侧详情回到初始态（开始新一次扫描时调用）。

        树已经清了、详情还挂着上一个目录的图表与说明，很容易被误读成
        「这是新扫出来的」，所以一起重置。
        """
        self.lb_title.setText("目录详情")
        self.lb_path.set_path("")      # 传空串才会回到「—」占位，别传"—"
        self.tx_info.clear()
        self.lb_mark.setText("未标记")
        self.donut.set(0, "—", "", self.pal["text_faint"])
        for w in (self.lb_self, self.lb_all, self.lb_count, self.lb_mtime):
            w.setText("—")
        self.bars.set_items([], 0)

    def _on_select(self, cur, prev):
        if not cur:
            return
        self._show_detail(cur.data(0, Qt.UserRole))

    def _cur_path(self):
        it = self.tw.currentItem()
        return it.data(0, Qt.UserRole) if it else None

    def _show_detail(self, path):
        if not path:
            return
        p = self.pal
        nd = self.tree.node(path) if self.tree else None
        info = self._info(path)
        mark, note, owner, inherited = self.marks.effective(path)
        rc = p["risk"].get(info["risk"], p["risk"]["keep"])
        total = self.tree.total() if self.tree else 0

        self.lb_title.setText(info["name"])
        self.lb_path.set_path(path)

        h = []
        h.append("<div style='margin:2px 0 8px'>"
                 + chip(RISK_LABEL.get(info["risk"], ""), rc, p)
                 + chip(KIND_LABEL.get(info["kind"], info["kind"] or "其他"),
                        p["accent"], p)
                 + (chip("知识库未收录 · 推断", p["risk"]["caution"], p)
                    if not info["exact"] else chip("知识库已收录",
                                                   p["risk"]["safe"], p))
                 + "</div>")
        h.append(f"<p style='line-height:1.75;margin:0 0 7px'>"
                 f"<b style='color:{p['risk']['safe']}'>作用 ▸</b> "
                 f"{esc(info['purpose'])}</p>")
        h.append(f"<p style='line-height:1.75;margin:0'>"
                 f"<b style='color:{p['risk']['caution']}'>建议 ▸</b> "
                 f"{esc(info['advice'])}</p>")
        if nd and nd.truncated:
            h.append(f"<p style='line-height:1.7;margin:8px 0 0;"
                     f"color:{p['risk']['caution']}'>"
                     f"该目录下还有更深层未扫描，可点「深入扫描」。</p>")
        self.tx_info.setHtml("".join(h))

        # 环形图 + 指标
        if nd:
            self.donut.set(pct_of(nd.agg_size, total), fmt(nd.agg_size),
                           f"占扫描总量 {fmt_pct(nd.agg_size, total)}",
                           p["accent"])
            self.lb_self.setText(fmt(nd.own_size))
            self.lb_all.setText(fmt(nd.agg_size))
            self.lb_count.setText(f"{nd.agg_count} 个")
            self.lb_mtime.setText(fmt_datetime(nd.agg_mtime))
        else:
            self.donut.set(0, "—", "", p["text_faint"])
            for w in (self.lb_self, self.lb_all, self.lb_count, self.lb_mtime):
                w.setText("—")

        self._show_types(nd)

        mk = []
        if mark:
            col = p["risk"]["safe"] if mark == MARK_KEEP else p["risk"]["caution"]
            mk.append(f"<b style='color:{col}'>{MARK_LABEL.get(mark)}</b>")
        if note:
            mk.append("备注：" + esc(note))
        if inherited and owner:
            mk.append(f"<span style='opacity:.7'>继承自 "
                      f"{esc(short_path(owner, 1, 2, 60))}</span>")
        self.lb_mark.setText(" · ".join(mk) if mk else "未标记")

    def _show_types(self, nd):
        if not nd or not nd.agg_ext:
            self.bars.set_items([], 0)
            return
        groups = self.ft.group(nd.agg_ext)
        items = []
        for cid, (size, count) in groups:
            cat = next((c for c in self.ft.cats if c["id"] == cid),
                       self.ft.fallback)
            items.append((cat["name"], cat["color"], size, count))
        self.bars.set_items(items, nd.agg_size or 1)

    # ---------------- 标记 ----------------
    def _on_mark(self):
        """底部操作条上的三个按钮共用这一个槽。"""
        path = self._cur_path()
        if not path:
            return
        sender = self.sender()
        if sender is self.b_keep:
            self._set_mark(path, MARK_KEEP)
        elif sender is self.b_junk:
            self._set_mark(path, MARK_JUNK)
        else:
            self._edit_note(path)

    def _clear_mark(self):
        path = self._cur_path()
        if path:
            self._clear_mark_at(path)

    def _set_mark(self, path, mark):
        self.marks.set(path, mark=mark)
        self._after_mark(path)

    def _clear_mark_at(self, path):
        self.marks.remove(path)
        self._after_mark(path)

    def _after_mark(self, path):
        self._cache.pop(path, None)
        self._rebuild()
        self._show_detail(path)

    def _edit_note(self, path=None):
        path = path or self._cur_path()
        if not path:
            return
        cur = self.marks.exact(path) or {}
        note, ok = QInputDialog.getText(
            self, "写备注", f"为目录写备注：\n{path}",
            text=cur.get("note", ""))
        if not ok:
            return
        self.marks.set(path, note=note.strip())
        self._after_mark(path)

    # ---------------- 右键菜单 ----------------
    def _menu(self, pos):
        it = self.tw.itemAt(pos)
        if not it:
            return
        path = it.data(0, Qt.UserRole)
        if not path:
            return
        self.tw.setCurrentItem(it)
        m = self._make_menu(path)
        m.exec(self.tw.viewport().mapToGlobal(pos))

    def _make_menu(self, path):
        """结果行右键：直接在行上打标记，不用先选中再去底部点按钮。"""
        m = QMenu(self)
        head = m.addAction(os.path.basename(path) or path)
        head.setEnabled(False)
        m.addSeparator()
        act(m, "加入白名单", "keep", lambda: self._set_mark(path, MARK_KEEP))
        act(m, "标记为可清理", "warn", lambda: self._set_mark(path, MARK_JUNK))
        act(m, "写备注…", "menu", lambda: self._edit_note(path))
        m.addSeparator()
        act(m, "打开位置", "open", lambda: self._open_at(path))
        act(m, "复制完整路径", "copy",
            lambda: QApplication.clipboard().setText(path))
        act(m, "深入扫描", "toggle", lambda: self._deepen_path(path))
        m.addSeparator()
        cur = self.marks.exact(path) or {}
        has = bool(cur)
        act(m, "清除标记", "delete", lambda: self._clear_mark_at(path), has)
        return attach_menu(m, self.sfx)

    def _open_dir(self):
        self._open_at(self._cur_path())

    def _open_at(self, path):
        if not path:
            return
        try:
            if os.path.isdir(path):
                os.startfile(path)
            else:
                subprocess.Popen(["explorer", "/select,", path])
        except Exception as e:
            QMessageBox.warning(self, "无法打开", str(e))

    def _deepen_path(self, path):
        if not self.tree or (self.deep_thread and self.deep_thread.isRunning()):
            return
        self.banner.start(f"正在深入扫描 {os.path.basename(path) or path}",
                          "往下挖 3 层…")
        self.deep_thread = DeepenThread(self.tree, path, self)
        self.deep_thread.finished_all.connect(self._on_deep_done)
        self.deep_thread.start()

    def _on_deep_done(self, ok):
        self.banner.finish("深入扫描完成", "已更新列表")
        QTimer.singleShot(1400, self.banner.hide)
        self._rebuild()

    def _deepen(self):
        path = self._cur_path()
        if path:
            self._deepen_path(path)

    def refresh_marks(self):
        """刷新标记列表（标记在别处改过之后同步过来）。"""
        self._cache.clear()
        self._rebuild()


class MarksPage(QWidget):
    """我的标记页：集中查看 / 编辑 / 删除白名单与备注。

    白名单一旦标记，清理时会默认跳过、目录分析里绿色高亮；子目录继承上级。
    """

    def __init__(self, marks: MarkStore, parent=None):
        super().__init__(parent)
        self.marks = marks
        self.sfx = getattr(parent, "sfx", None) or NullSfx()
        self.pal = palette(DEFAULT_THEME)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(8)
        lb = QLabel("白名单 = 你主观认定不可删除；标记后目录分析里会用绿色高亮，清理时也默认跳过。")
        lb.setObjectName("muted")
        top.addWidget(lb)
        top.addStretch(1)
        b_ref = QPushButton("刷新")
        b_note = QPushButton("编辑备注")
        b_del = QPushButton("删除标记")
        b_del.setObjectName("danger")
        b_open = QPushButton("打开位置")
        b_ref.clicked.connect(self.refresh)
        b_note.clicked.connect(self._edit_note)
        b_del.clicked.connect(self._delete)
        b_open.clicked.connect(self._open)
        for b in (b_ref, b_note, b_del, b_open):
            top.addWidget(b)
        lay.addLayout(top)

        self.tbl = QTableWidget(0, 4)
        self.tbl.setHorizontalHeaderLabels(["路径", "标记", "备注", "时间"])
        hth = self.tbl.horizontalHeader()
        hth.setMinimumSectionSize(70)
        hth.setSectionResizeMode(0, QHeaderView.Interactive)    # 路径列：可手动调宽看全
        hth.setSectionResizeMode(3, QHeaderView.Stretch)        # 末列吸收剩余宽度
        # 拉宽 / 缩窄窗口时第一列（路径）跟着伸缩，其余列各有最小宽
        FirstColFit(self.tbl, min_w=280, others={1: 76, 2: 130, 3: 132})
        self.tbl.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        # 点表头排序（默认按路径升序，和原来 Qt 自带排序的表现一致）
        self.sorter = HeaderSorter(self.tbl, asc_first=(0, 1, 2),
                                   default_col=0, default_desc=False)
        self.tbl.setItemDelegate(RowDelegate(self.tbl, elide_col=0))
        enable_hover(self.tbl)
        self.tbl.doubleClicked.connect(self._edit_note)
        self.tbl.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tbl.customContextMenuRequested.connect(self._menu)
        lay.addWidget(self.tbl, 1)
        self.refresh()

    def on_theme(self, theme):
        """主题切换：重取调色板并刷新本页。"""
        self.pal = palette(theme)
        self.refresh()

    def refresh(self):
        """重新加载并刷新标记列表。"""
        rows = self.marks.all()
        self.tbl.setRowCount(len(rows))
        for i, r in enumerate(rows):
            a = QTableWidgetItem(r["path"])
            a.setToolTip(r["path"])
            m = QTableWidgetItem(MARK_LABEL.get(r["mark"], "仅备注"))
            m.setTextAlignment(Qt.AlignCenter)
            col = self.pal["mark"].get(r["mark"], self.pal["text_dim"])
            m.setForeground(QColor(col))
            n = QTableWidgetItem(r["note"])
            t = QTableWidgetItem(r["time"])
            t.setTextAlignment(Qt.AlignCenter)
            self.tbl.setItem(i, 0, a)
            self.tbl.setItem(i, 1, m)
            self.tbl.setItem(i, 2, n)
            self.tbl.setItem(i, 3, t)
        self.sorter.sort_now()

    def _row_path(self):
        r = self.tbl.currentRow()
        if r < 0:
            return None
        return self.tbl.item(r, 0).text()

    def _edit_note(self):
        p = self._row_path()
        if not p:
            return
        cur = self.marks.exact(p) or {}
        note, ok = QInputDialog.getText(self, "编辑备注", p, text=cur.get("note", ""))
        if ok:
            self.marks.set(p, note=note.strip())
            self.refresh()

    def _delete(self):
        p = self._row_path()
        if p:
            self._delete_at(p)

    def _delete_at(self, p):
        if QMessageBox.question(self, "删除标记", f"确定删除这条标记？\n{p}") == QMessageBox.Yes:
            self.marks.remove(p)
            self.refresh()

    def _open(self):
        p = self._row_path()
        if not p:
            return
        try:
            os.startfile(p)
        except Exception:
            pass

    # ---------------- 右键菜单 ----------------
    def _menu(self, pos):
        idx = self.tbl.indexAt(pos)
        if not idx.isValid():
            return
        row = idx.row()
        self.tbl.selectRow(row)
        item = self.tbl.item(row, 0)
        p = item.text() if item else None
        if not p:
            return
        self._make_menu(p).exec(self.tbl.viewport().mapToGlobal(pos))

    def _make_menu(self, p):
        """标记列表右键：直接改标记，不用先选中再点上面那排按钮。"""
        cur = self.marks.exact(p) or {}
        m = QMenu(self)
        head = m.addAction(p)
        head.setEnabled(False)
        m.addSeparator()
        act(m, "设为白名单", "keep", lambda: self._set(p, MARK_KEEP))
        act(m, "设为可清理", "warn", lambda: self._set(p, MARK_JUNK))
        act(m, "编辑备注…", "menu", lambda: self._edit_note())
        m.addSeparator()
        act(m, "打开位置", "open", lambda: self._open())
        act(m, "复制完整路径", "copy",
            lambda: QApplication.clipboard().setText(p))
        m.addSeparator()
        act(m, "删除标记", "delete", lambda: self._delete_at(p), bool(cur))
        return attach_menu(m, self.sfx)

    def _set(self, p, mark):
        cur = self.marks.exact(p) or {}
        self.marks.set(p, mark=mark, note=cur.get("note", ""))
        self.refresh()
