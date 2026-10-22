"""重复文件夹页（C）：找整棵被复制过去的目录树。

和「文件去重」的区别：那一页找的是**单个重复文件**，这一页找的是
**整棵目录树被复制过去**的情况（备份多份、项目拷来拷去、旧版本没删）。

结果分两块：
  完全相同   —— Merkle 签名一致，结构逐层对得上，可以放心删副本；
  大部分相同 —— 大小/文件数/顶层名字高度接近，只是**疑似**，必须自己判断。

界面骨架沿用其它扫描页的写法（page_layout + top_bar + ScanBanner），
保证切页签时条高、边距都不跳动。
"""
import os
import subprocess

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTreeWidget,
    QTreeWidgetItem, QFrame, QSpinBox, QCheckBox, QSplitter, QMessageBox,
    QMenu, QApplication, QAbstractItemView, QHeaderView,
)

from core import dupdirs
from core.sizefmt import fmt_size as fmt
from ui.widgets import page_layout, top_bar, setup_tree, RowDelegate, enable_hover
from ui.busy import ScanBanner
from ui.theme import palette, DEFAULT_THEME

COLS = ["路径", "占用", "文件数"]


class DupDirThread(QThread):
    """后台：先遍历建索引，再比对。两段都能中途喊停。"""

    progress = Signal(int, str)
    finished_all = Signal(object, str)

    def __init__(self, root, min_bytes, overlap, skip, parent=None):
        super().__init__(parent)
        self.root = root
        self.min_bytes = min_bytes
        self.overlap = overlap
        self.skip = skip
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        idx = dupdirs.scan_index(
            self.root,
            skip_names=dupdirs.DEFAULT_SKIP if self.skip else frozenset(),
            should_cancel=lambda: self._cancel,
            on_progress=lambda n, p: self.progress.emit(n, p))
        if idx is None:
            self.finished_all.emit(None, self.root)
            return
        exact, near = dupdirs.find_duplicates(
            idx, min_bytes=self.min_bytes,
            near_overlap=self.overlap / 100.0,
            should_cancel=lambda: self._cancel)
        self.finished_all.emit((exact, near, len(idx)), self.root)


class DupDirPage(QWidget):
    """重复文件夹页。"""

    def __init__(self, cfg=None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.pal = palette(DEFAULT_THEME)
        self.thread = None
        self.root_path = (cfg.get("target_dupdir") if cfg else "") or ""
        self._build()

    def set_root(self, path):
        """顶部统一目标改到本页时，记下扫描位置。"""
        self.root_path = (path or "").strip()

    # ---------------- UI ----------------
    def _build(self):
        lay = page_layout(self)

        bar, bl = top_bar()
        lb = QLabel("扫描位置在窗口顶部选，用来找整棵被复制过的目录树。")
        lb.setObjectName("hint")
        bl.addWidget(lb)
        bl.addStretch(1)
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
        bl2.addWidget(QLabel("最低占用"))
        self.sp_min = QSpinBox()
        self.sp_min.setRange(1, 100000)
        self.sp_min.setSingleStep(50)
        self.sp_min.setSuffix(" MB")
        self.sp_min.setMinimumWidth(116)
        self.sp_min.setValue(int(self.cfg.get("dupdir_min_mb", 50) or 50)
                             if self.cfg else 50)
        self.sp_min.valueChanged.connect(self._on_min_changed)
        bl2.addWidget(self.sp_min)
        bl2.addWidget(QLabel("重合率"))
        self.sp_ovl = QSpinBox()
        self.sp_ovl.setRange(50, 100)
        self.sp_ovl.setSingleStep(5)
        self.sp_ovl.setSuffix(" %")
        self.sp_ovl.setMinimumWidth(100)
        self.sp_ovl.setToolTip(
            "「大部分相同」的判定松紧：顶层文件名的重合比例达到多少才算相似")
        self.sp_ovl.setValue(int(self.cfg.get("dupdir_overlap", 80) or 80)
                             if self.cfg else 80)
        self.sp_ovl.valueChanged.connect(self._on_ovl_changed)
        bl2.addWidget(self.sp_ovl)
        self.chk_skip = QCheckBox("跳过 node_modules 与虚拟环境")
        self.chk_skip.setToolTip(
            "这类目录数量多、内容高度雷同，勾上能少掉大量噪音；"
            "想专门找它们就取消勾选")
        self.chk_skip.setChecked(bool(self.cfg.get("dupdir_skip", True))
                                 if self.cfg else True)
        self.chk_skip.stateChanged.connect(self._on_skip_changed)
        bl2.addWidget(self.chk_skip)
        bl2.addStretch(1)
        tip = QLabel("右键路径可打开位置 · 复制路径")
        tip.setObjectName("hint")
        bl2.addWidget(tip)
        lay.addWidget(bar2)

        self.tw_exact = self._make_tree()
        self.tw_near = self._make_tree()
        split = QSplitter(Qt.Vertical)

        top_w = QWidget()
        tl = QVBoxLayout(top_w)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(4)
        self.lb_exact = QLabel("完全相同")
        self.lb_exact.setObjectName("h2")
        tl.addWidget(self.lb_exact)
        tl.addWidget(self.tw_exact)

        bot_w = QWidget()
        btl = QVBoxLayout(bot_w)
        btl.setContentsMargins(0, 0, 0, 0)
        btl.setSpacing(4)
        self.lb_near = QLabel("大部分相同")
        self.lb_near.setObjectName("h2")
        btl.addWidget(self.lb_near)
        btl.addWidget(self.tw_near)

        split.addWidget(top_w)
        split.addWidget(bot_w)
        split.setSizes([560, 340])
        lay.addWidget(split, 1)

        self.lb_stat = QLabel("选择位置后点击「开始扫描」")
        self.lb_stat.setObjectName("muted")
        lay.addWidget(self.lb_stat)
        self.banner = ScanBanner(self)
        lay.addWidget(self.banner)

    def _make_tree(self):
        tw = QTreeWidget()
        tw.setHeaderLabels(COLS)
        tw.setAlternatingRowColors(True)
        setup_tree(tw)
        tw.setSelectionMode(QAbstractItemView.ExtendedSelection)
        tw.setItemDelegate(RowDelegate(tw, elide_col=0, bar_col=1,
                                       level_bold=True))
        enable_hover(tw)
        hh = tw.header()
        hh.setMinimumSectionSize(70)
        hh.setSectionResizeMode(0, QHeaderView.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        tw.setColumnWidth(0, 460)
        tw.setMouseTracking(True)
        tw.setContextMenuPolicy(Qt.CustomContextMenu)
        tw.customContextMenuRequested.connect(
            lambda pos, t=tw: self._menu(t, pos))
        return tw

    def on_theme(self, theme):
        """切主题：换调色板 + 刷状态条 + 重画两棵树。

        只 update() 不重排——树里的数据没变，重画一遍就够，
        重新填充会把用户的展开状态弄丢。
        """
        self.pal = palette(theme)
        self.banner.set_palette(self.pal)
        for tw in (self.tw_exact, self.tw_near):
            tw.viewport().update()

    # ---------------- 扫描 ----------------
    def start_scan(self):
        """开一轮扫描。已经在跑就直接返回（按钮此时也该是禁用的，双保险）。

        状态机：开跑前置 `btn_scan` 灰、`btn_stop` 亮、两棵树清空；
        结束由 `_on_done` 负责翻回来，中间出错也要翻（那里有 finish + 提示）。
        """
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
        self.btn_scan.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.tw_exact.clear()
        self.tw_near.clear()
        self.lb_exact.setText("完全相同")
        self.lb_near.setText("大部分相同")
        self.banner.start(f"正在查找重复文件夹 {root}", "准备中…",
                          on_stop=self.stop_scan)
        self.thread = DupDirThread(
            root,
            int(self.sp_min.value()) * 1024 * 1024,
            int(self.sp_ovl.value()),
            self.chk_skip.isChecked(),
            self)
        self.thread.progress.connect(
            lambda n, p: self.banner.set_sub(f"已扫描 {n} 个目录 · {p}"))
        self.thread.finished_all.connect(self._on_done)
        self.thread.start()

    def stop_scan(self):
        """请求中断。只是发 cancel 信号，线程自己检查到才退，
        所以按钮立刻置灰、结果要等 `_on_done`（那里会收到 res=None）。"""
        if self.thread:
            self.thread.cancel()
        self.btn_stop.setEnabled(False)

    def _on_done(self, res, root):
        self.btn_scan.setEnabled(True)
        self.btn_stop.setEnabled(False)
        if res is None:
            self.banner.finish("扫描失败", root)
            QTimer.singleShot(1600, self.banner.hide)
            self.lb_stat.setText("扫描失败：目录不存在或无法读取。")
            return
        exact, near, ndirs = res
        self._fill(self.tw_exact, exact)
        self._fill(self.tw_near, near)
        ex = dupdirs.groups_total(exact)
        nr = dupdirs.groups_total(near)
        self.lb_exact.setText(
            f"完全相同（{len(exact)} 组 · 可释放 {fmt(ex)}）")
        self.lb_near.setText(
            f"大部分相同（{len(near)} 组 · 可释放 {fmt(nr)}）")
        self.lb_stat.setText(
            f"扫描 <b>{ndirs}</b> 个目录 · 完全相同 <b>{len(exact)}</b> 组"
            f"（可释放 {fmt(ex)}） · 大部分相同 <b>{len(near)}</b> 组"
            f"（可释放 {fmt(nr)}）")
        self.banner.finish("查找完成", f"{ndirs} 个目录")
        QTimer.singleShot(1600, self.banner.hide)

    def _fill(self, tw, groups):
        tw.clear()
        for g in groups:
            n = len(g["paths"])
            # 留一份、删其余才是「可释放」，绝不能写成 size * n
            free = g["size"] * (n - 1)
            head = QTreeWidgetItem([
                f"{n} 份拷贝" if n > 1 else "1 份",
                fmt(free),
                str(g["count"]),
            ])
            head.setData(0, Qt.UserRole, "")
            head.setData(1, Qt.UserRole, free)
            head.setToolTip(0, "\n".join(g["paths"]))
            head.setToolTip(
                1, f"留一份、删其余可释放 {fmt(free)}（{free:,} 字节）")
            for c in (1, 2):
                head.setTextAlignment(c, Qt.AlignRight | Qt.AlignVCenter)
            tw.addTopLevelItem(head)
            for p in g["paths"]:
                it = QTreeWidgetItem([p, fmt(g["size"]), str(g["count"])])
                it.setData(0, Qt.UserRole, p)
                it.setToolTip(0, p)
                for c in (1, 2):
                    it.setTextAlignment(c, Qt.AlignRight | Qt.AlignVCenter)
                head.addChild(it)
        tw.expandAll()

    # ---------------- 右键 ----------------
    def _menu(self, tw, pos):
        it = tw.itemAt(pos)
        if not it:
            return
        path = it.data(0, Qt.UserRole)
        m = QMenu(tw)
        if path:
            m.addAction("打开位置", lambda: self._reveal(path))
            m.addAction("复制路径", lambda: self._copy(path))
        else:
            m.addAction("展开当前组", lambda: it.setExpanded(True))
        m.exec(tw.viewport().mapToGlobal(pos))

    def _reveal(self, path):
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        except Exception:
            pass

    def _copy(self, path):
        cb = QApplication.clipboard()
        if cb:
            cb.setText(path)

    # ---------------- 配置回调 ----------------
    def _on_min_changed(self, v):
        if self.cfg:
            self.cfg.set("dupdir_min_mb", int(v))

    def _on_ovl_changed(self, v):
        if self.cfg:
            self.cfg.set("dupdir_overlap", int(v))

    def _on_skip_changed(self, _s):
        if self.cfg:
            self.cfg.set("dupdir_skip", self.chk_skip.isChecked())
