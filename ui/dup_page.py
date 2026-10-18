"""重复文件查找与去重页。

选两个目录（可以填同一个）→ 按大小（可选内容校验）找出重复文件 →
按「保留规则」自动挑出一个保留、其余标记删除 → 移到回收站 / 备份后删除。

保留规则里最优先的是你自己标过的「优先保留目录」：只要组里有文件落在
优先目录下，就一定留它，其余规则只在没命中优先目录时才起作用。
"""
import os
import time
import subprocess
import re

from PySide6.QtCore import (
    Qt, QThread, Signal, QTimer, QPoint, QSize, QObject, QRunnable, QThreadPool,
)
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTreeWidget,
    QTreeWidgetItem, QComboBox, QSplitter, QFrame, QHeaderView, QMessageBox,
    QFileDialog, QAbstractItemView, QSpinBox, QGroupBox, QListWidget,
    QListWidgetItem, QMenu, QApplication, QWidgetAction, QCheckBox,
)

from core.dupfinder import (
    DupFinder, MATCH_LABELS, MATCH_QUICK, MATCH_SIMILAR,
    RULE_LABELS, RULES, RULE_CTIME_OLD, pick_keeper, in_priority,
)
from core import imghash as IH
from core.cleaner import remove_path, RECYCLE, BACKUP, PERMANENT, MODE_LABEL
from core.journal import Journal
from core import system as sysinfo
from core import thumbs
from ui.theme import palette, DEFAULT_THEME
from ui.busy import ScanBanner, DeleteBanner
from ui.sound import attach_menu, act, NullSfx
from ui.widgets import (
    fmt, short_path, PathLabel, RowDelegate, enable_hover, setup_tree,
    HeaderSorter, FirstColFit, page_layout, top_bar, DeleteCheckStyle,
)
from ui.charts import Donut


# 列表小图 / 详情大图：都从同一张 160px 缓存缩放，不存第二套
THUMB_LIST_PX = 24
THUMB_DETAIL_PX = thumbs.THUMB_SIZE
# 内存里最多留多少张 QIcon（超出就丢最早的，避免长时间翻列表堆积）
THUMB_MEM_MAX = 300


class _ThumbSignals(QObject):
    """后台任务回主线程的唯一出口。"""
    done = Signal(str, object)          # path, QImage 或 None


class _ThumbTask(QRunnable):
    """后台生成一张缩略图：读盘 + 解码全在这里，主线程绝不碰。

    生成完顺手写进磁盘缓存，下次再看到这个文件就是纯读 sqlite。
    """

    def __init__(self, path, sig):
        super().__init__()
        self.path = path
        self.sig = sig

    def run(self):
        try:
            img = thumbs.render(self.path)      # 内部多级回退，不会抛
            if img is not None:
                thumbs.store(self.path, img)
        except Exception:
            img = None
        self.sig.done.emit(self.path, img)


class DupThread(QThread):
    """后台找重复文件。先按大小分组，再做内容校验（可选完整 MD5）。"""
    progress = Signal(str, int)
    finished_all = Signal(object)

    def __init__(self, finder, parent=None):
        super().__init__(parent)
        self.finder = finder

    def run(self):
        groups = self.finder.scan(
            on_progress=lambda what, n: self.progress.emit(what, n),
            should_cancel=lambda: self.finder.stopped)
        self.finished_all.emit(groups)


class DupCleanThread(QThread):
    """后台逐个删除重复的待删文件，实时回报进度，支持中途取消。

    放在线程里是为了避免一次删几百个文件时界面卡死；删除是文件 IO，无法在
    单个文件删除到一半时打断，所以取消是"处理完当前这个就停"，不是硬中断。
    """
    progress = Signal(int, int, str)
    # 第一个是释放的字节数，用 qlonglong，否则删掉好几 GB 时会溢出 32 位 int
    finished_all = Signal("qlonglong", list, int)

    def __init__(self, paths, mode, backup_root, journal, parent=None):
        super().__init__(parent)
        self.paths = paths
        self.mode = mode
        self.backup_root = backup_root
        self.journal = journal
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        freed = 0
        errs = []
        total = len(self.paths)
        for i, pth in enumerate(self.paths, 1):
            if self._cancel:
                break
            self.progress.emit(i, total, os.path.basename(pth))
            got, e = remove_path(pth, self.mode, self.backup_root)
            freed += got
            errs.extend(e)
        self.finished_all.emit(freed, errs, total)


class DupPage(QWidget):
    """文件去重页：找重复文件、按规则挑出保留的那份、其余标记待删除。

    整页的数据流是单向的：扫描得到 groups -> 按筛选规则算出 keeper_of /
    delete_set -> 刷列表 -> 刷左下角总结。任何一处筛选条件变化都重新走一遍
    _auto_select()，不去就地打补丁，免得出现"列表变了总结没变"这类不一致。

    几个容易看漏的状态：
    - ext_on     ：允许被删除的后缀集合，未勾选的类型永远不会被自动选上
    - manual_keep：用户手动取消勾选而留下的路径，重算时不再被自动选回去
    - _fw        ：顶部条与右侧面板两套筛选控件的引用，二者必须双向同步
    """

    COLS = ["文件 / 分组", "大小", "创建时间", "修改时间", "处置"]

    def __init__(self, cfg=None, marks=None, journal=None, sfx=None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.sfx = sfx or NullSfx()
        self.journal = journal or Journal()
        self.pal = palette(DEFAULT_THEME)
        self.groups = []
        self.delete_set = set()        # 勾选 = 待删除（小写路径）
        self.keeper_of = {}            # 组下标 -> 保留文件路径
        self.thread = None
        self.total_waste = 0
        self.ext_on = set()          # 允许被删除的后缀集合；未勾选的类型绝不删
        self.manual_keep = set()      # 用户手动取消勾选而保留的小写路径集合
        self._fw = {"top": {}, "right": {}}   # 顶部 / 右侧 两套筛选 UI 的控件引用
        self._filter_titles = []      # (标题标签, 配色键) 列表，主题切换时同步刷新

        d = cfg.data if cfg else {}
        self.min_mb = int(d.get("dup_min_mb", 1) or 1)
        self.match = int(d.get("dup_match", MATCH_QUICK))
        self.rule = int(d.get("dup_rule", RULE_CTIME_OLD))
        self.keep_dirs = list(d.get("dup_keep_dirs", []) or [])
        self.mode = d.get("dup_mode", RECYCLE)
        # ---- 相似查找（判定方式的第四项）
        self.sim_level = int(d.get("sim_level", 0) or 0)
        self.sim_gif = bool(d.get("sim_include_gif", True))
        self.sim_ignored = list(d.get("sim_ignored", []) or [])

        # ---- 缩略图预览（要放在 d 之后，它们读的是同一份配置）
        self.thumbs_on = bool(d.get("dup_thumbs", True))
        self._thumb_sig = _ThumbSignals()
        self._thumb_sig.done.connect(self._on_thumb_done)
        self._thumb_icons = {}         # path -> QIcon，内存缓存，避免反复构造
        self._thumb_pending = set()    # 已提交后台、还没回来的路径
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.setInterval(200)     # 滚动时防抖，别每像素都重算
        self._thumb_timer.timeout.connect(self._queue_visible_thumbs)
        self._detail_path = ""         # 详情面板当前显示的是哪个文件

        self._build()
        a = d.get("dup_dir_a", "")
        b = d.get("dup_dir_b", "")
        if a:
            self.ed_a.setEditText(a)
        if b:
            self.ed_b.setEditText(b)

    # ------------------------------------------------------------ UI
    def _build(self):
        """搭整页界面。自上而下四段：

        1. 第一行：目录 A / 目录 B / 浏览 / 同于 A
        2. 第二行：阈值、判定方式，右侧接三个弹出式筛选入口（A类型/B目录/C其它）
        3. 主体：左侧重复项树 + 右侧详情与筛选面板（QSplitter 分隔）
        4. 底部：总结、批量选择按钮、删除方式、删除按钮，以及扫描 / 删除状态条
        """
        lay = page_layout(self)

        # ---- 第一行：目录 / 阈值 / 匹配方式
        bar, bl = top_bar()
        bl.addWidget(QLabel("目录 A"))
        self.ed_a = QComboBox()
        self.ed_a.setEditable(True)
        self.ed_a.setMinimumWidth(180)
        # 限制按"最长内容"算出的最小宽度：否则长路径会把整个窗口的最小宽度
        # 顶到 1400+，小尺寸显示器上窗口缩不下来
        self.ed_a.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.ed_a.setMinimumContentsLength(14)
        self._fill_roots(self.ed_a)
        bl.addWidget(self.ed_a, 1)
        b_a = QPushButton("浏览")
        b_a.clicked.connect(lambda: self._browse(self.ed_a))
        bl.addWidget(b_a)
        bl.addSpacing(10)
        bl.addWidget(QLabel("目录 B"))
        self.ed_b = QComboBox()
        self.ed_b.setEditable(True)
        self.ed_b.setMinimumWidth(180)
        self.ed_b.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.ed_b.setMinimumContentsLength(14)
        self._fill_roots(self.ed_b)
        bl.addWidget(self.ed_b, 1)
        b_b = QPushButton("浏览")
        b_b.clicked.connect(lambda: self._browse(self.ed_b))
        bl.addWidget(b_b)
        b_same = QPushButton("同于 A")
        b_same.setToolTip("两个目录填同一个，就是在这棵树内部找重复")
        b_same.clicked.connect(lambda: self.ed_b.setEditText(
            self.ed_a.currentText()))
        bl.addWidget(b_same)
        lay.addWidget(bar)

        bar2 = QFrame()
        b2l = QHBoxLayout(bar2)
        b2l.setContentsMargins(0, 0, 0, 0)
        b2l.setSpacing(8)
        b2l.addWidget(QLabel("大于"))
        self.sp_min = QSpinBox()
        self.sp_min.setRange(0, 100000)
        self.sp_min.setSuffix(" MB")
        self.sp_min.setSingleStep(1)
        self.sp_min.setMinimumWidth(96)   # 别让「100000 MB」把整行撑宽
        self.sp_min.setValue(self.min_mb)
        self.sp_min.valueChanged.connect(self._on_min_changed)
        b2l.addWidget(self.sp_min)
        b2l.addWidget(QLabel("判定方式"))
        self.cb_match = QComboBox()
        # 之前为了压窗口最小宽度，把宽度按「8 个字符」估算，中文选项被截成半截。
        # 改回按内容自动调整（首次显示时按最长项算），保证文字完整可见。
        self.cb_match.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow)
        self.cb_match.setMinimumWidth(150)
        self.cb_match.addItems(MATCH_LABELS)
        self.cb_match.setCurrentIndex(self.match if self.match <= 3 else 1)
        self.cb_match.currentIndexChanged.connect(self._on_match_changed)
        b2l.addWidget(self.cb_match)
        # 「相似度」只在判定方式选了「相似查找」时才露面，平时整组藏起来，
        # 免得给不用这个功能的人多两个看不懂的下拉
        self.lb_sim = QLabel("相似度")
        self.cb_sim = QComboBox()
        self.cb_sim.addItems(IH.LEVEL_LABELS)
        self.cb_sim.setCurrentIndex(
            self.sim_level if 0 <= self.sim_level < len(IH.LEVEL_LABELS) else 0)
        self.cb_sim.setToolTip(IH.LEVEL_HINTS[self.cb_sim.currentIndex()])
        self.cb_sim.currentIndexChanged.connect(self._on_sim_level_changed)
        b2l.addWidget(self.lb_sim)
        b2l.addWidget(self.cb_sim)
        # 「判定方式」右侧同一行放弹出式的去重保一筛选规则入口
        b2l.addSpacing(14)
        self._build_top_filter(b2l)
        b2l.addStretch(1)
        # 「显示预览图」总开关：关掉后完全不读盘、不生成、不查缓存
        self.ck_thumb = QCheckBox("显示预览图")
        self.ck_thumb.setChecked(self.thumbs_on)
        self.ck_thumb.setToolTip(
            "给重复文件显示图片 / 视频封面小图（只对看得见的行生成，"
            "可在设置页清除缓存）")
        self.ck_thumb.toggled.connect(self._on_thumb_toggled)
        b2l.addWidget(self.ck_thumb)
        b2l.addSpacing(8)
        self.btn_scan = QPushButton("开始查找")
        self.btn_scan.setObjectName("primary")
        self.btn_scan.clicked.connect(self.start_scan)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_scan)
        b2l.addWidget(self.btn_scan)
        b2l.addWidget(self.btn_stop)
        lay.addWidget(bar2)

        # 相似查找的实话与排除说明。只在选了「相似查找」时出现 ——
        # 第一句必须写：dHash 对裁剪敏感（实测裁掉 5% 边缘距离就从 0 跳到 16），
        # 不写的话用户会认定「明明是同一张图怎么找不到」。
        self.lb_sim_tip = QLabel(
            "相似查找对「缩放、转发压缩」效果最好；裁剪或旋转过的图可能找不到。<br>"
            "已跳过矢量图、小于 128px、大于 6000px 与系统自带图片。")
        self.lb_sim_tip.setObjectName("hint")
        self.lb_sim_tip.setWordWrap(True)
        self.lb_sim_tip.setVisible(False)
        lay.addWidget(self.lb_sim_tip)
        self._sync_sim_ui()

        # ---- 主体
        split = QSplitter(Qt.Horizontal)

        self.tw = QTreeWidget()
        self.tw.setHeaderLabels(self.COLS)
        self.tw.setAlternatingRowColors(True)
        setup_tree(self.tw)
        # 结果树的勾选 = 「标记删除」，所以单独挂一套红叉勾选框
        # （全局样式是蓝色对勾，语义正好相反）。只挂在这棵树上，
        # 类型列表 / 优先目录列表仍用普通的对勾。
        self.del_check_style = DeleteCheckStyle()
        self.tw.setStyle(self.del_check_style)
        self.tw.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # 首列前面留一张 24px 小缩略图的位置（画在 DecorationRole 上，
        # 由 RowDelegate 把这块宽度从省略计算里扣掉）
        self.tw.setIconSize(QSize(THUMB_LIST_PX, THUMB_LIST_PX))
        self.tw.setItemDelegate(RowDelegate(self.tw, elide_col=0,
                                            level_bold=True,
                                            thumb_col=0,
                                            thumb_size=THUMB_LIST_PX))
        enable_hover(self.tw)
        # 滚动 / 展开分组后都会露出新行，防抖 200ms 再给它们补缩略图
        self.tw.verticalScrollBar().valueChanged.connect(
            lambda _v: self._thumb_timer.start())
        self.tw.itemExpanded.connect(lambda _it: self._thumb_timer.start())
        hh = self.tw.header()
        hh.setMinimumSectionSize(70)
        hh.setSectionResizeMode(0, QHeaderView.Interactive)   # 文件名列：可手动调宽看全
        for c in (1, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)        # 末列吸收剩余宽度
        self.tw.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.tw.setColumnWidth(0, 380)
        # 点表头排序：大小按真实字节、时间按时间、处置按处置状态。
        # default_col=None —— 默认保持「按大小分组的原顺序」，用户点了表头才排。
        self.sorter = HeaderSorter(self.tw, key_fn=self._sort_key,
                                   numeric_cols=(1,), default_col=None,
                                   asc_first=(0,))
        # 窗口拉宽时，多出来的宽度优先给第一列（路径列），方便看全目录
        # 拉宽窗口时多出来的宽度给第一列（路径列），收缩时同样会收回；
        # 括号里是其它各自的最小宽度，像「处置」列不会被挤到看不全
        FirstColFit(self.tw, min_w=280,
                    others={1: 96, 2: 132, 3: 132, 4: 120})
        self.tw.itemChanged.connect(self._on_item_changed)
        self.tw.currentItemChanged.connect(self._on_select)
        self.tw.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tw.customContextMenuRequested.connect(self._menu)
        split.addWidget(self.tw)

        side = QFrame()
        side.setObjectName("detail")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(12, 12, 12, 12)
        sl.setSpacing(10)

        self.lb_title = QLabel("重复项详情")
        self.lb_title.setObjectName("title")
        sl.addWidget(self.lb_title)
        self.lb_path = PathLabel("—")
        self.lb_path.setObjectName("hint")
        sl.addWidget(self.lb_path)

        # 预览区：选中文件后显示 160px 大图（图片直接显示、视频是封面帧、
        # 拿不到预览的类型退成系统图标）。没有选中文件时整块隐藏。
        self.lb_thumb = QLabel()
        self.lb_thumb.setFixedSize(THUMB_DETAIL_PX, THUMB_DETAIL_PX)
        self.lb_thumb.setAlignment(Qt.AlignCenter)
        self.lb_thumb.setVisible(False)
        tl = QHBoxLayout()
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addStretch(1)
        tl.addWidget(self.lb_thumb)
        tl.addStretch(1)
        sl.addLayout(tl)

        top = QHBoxLayout()
        self.donut = Donut(side, size=112)
        top.addWidget(self.donut)
        info = QVBoxLayout()
        info.setSpacing(4)
        self.lb_size = QLabel("—")
        self.lb_size.setObjectName("num")
        self.lb_ct = QLabel("—")
        self.lb_ct.setObjectName("muted")
        self.lb_mt = QLabel("—")
        self.lb_mt.setObjectName("muted")
        self.lb_dir = QLabel("—")
        self.lb_dir.setObjectName("hint")
        self.lb_dir.setWordWrap(True)
        for w in (self.lb_size, self.lb_ct, self.lb_mt, self.lb_dir):
            info.addWidget(w)
        info.addStretch(1)
        top.addLayout(info, 1)
        sl.addLayout(top)

        rowb = QHBoxLayout()
        rowb.setSpacing(8)
        self.btn_keep_this = QPushButton("仅保留此项")
        self.btn_keep_this.setObjectName("ok")
        self.btn_keep_this.setToolTip("同组其它文件全部标记为待删除")
        self.btn_keep_this.clicked.connect(self._keep_this)
        self.btn_open = QPushButton("打开位置")
        self.btn_open.clicked.connect(self._open_sel)
        rowb.addWidget(self.btn_keep_this)
        rowb.addWidget(self.btn_open)
        sl.addLayout(rowb)

        # （优先保留目录已并入下方「去重保一筛选规则」容器的 B 面板）
        sl.addWidget(self._build_filter_container())
        sl.addStretch(1)
        side.setFixedWidth(340)
        split.addWidget(side)
        split.setSizes([760, 340])
        lay.addWidget(split, 1)

        # ---- 底部
        bot = QFrame()
        bot.setObjectName("card")
        bbl = QHBoxLayout(bot)
        bbl.setContentsMargins(14, 10, 14, 10)
        bbl.setSpacing(8)
        self.lb_stat = QLabel("选择目录后点「开始查找」")
        bbl.addWidget(self.lb_stat)
        bbl.addStretch(1)
        b_auto = QPushButton("按规则自动选择")
        b_auto.setObjectName("warn")
        b_auto.clicked.connect(self._auto_select)
        b_none = QPushButton("全部取消")
        b_none.clicked.connect(self._select_none)
        b_invert = QPushButton("反选")
        b_invert.clicked.connect(self._invert)
        for b in (b_auto, b_none, b_invert):
            bbl.addWidget(b)
        bbl.addSpacing(16)
        bbl.addWidget(QLabel("删除方式"))
        self.cb_mode = QComboBox()
        self.cb_mode.addItems([MODE_LABEL[RECYCLE], MODE_LABEL[BACKUP],
                               MODE_LABEL[PERMANENT]])
        self.cb_mode.setCurrentIndex(
            {RECYCLE: 0, BACKUP: 1, PERMANENT: 2}.get(self.mode, 0))
        self.cb_mode.currentIndexChanged.connect(self._on_mode_changed)
        bbl.addWidget(self.cb_mode)
        self.btn_clean = QPushButton("删除重复项")
        # 删除是危险操作，用红底（和「停止扫描」同一档 danger 配色），
        # 与结果树的红叉、处置列的红色「待删除」是同一套语义色。
        self.btn_clean.setObjectName("danger")
        self.btn_clean.setEnabled(False)
        self.btn_clean.clicked.connect(self._do_clean)
        bbl.addWidget(self.btn_clean)
        lay.addWidget(bot)

        # 与其它扫描页同一个状态条：旋转指示 + 当前阶段 + 计时 + 流动条纹
        self.banner = ScanBanner(self)
        lay.addWidget(self.banner)
        # 删除时单独用红色"删除中"状态条，和扫描条在视觉上区分开
        self.del_banner = DeleteBanner(self, "正在删除重复项")
        lay.addWidget(self.del_banner)

        # 两套筛选 UI 都建好了，把已保存的优先保留目录填进去
        self._fill_keep_dirs()

    # ------------------------------------------------------------ 保一筛选规则三合一容器
    def _make_filter_panel(self, color_key, title, default_open=False):
        """生成一个可展开的内联子面板：标题前带右箭头 ▸，点击展开/折叠。
        父入口标题用对应理由的颜色标识（A 金 / B 蓝 / C 绿）。"""
        color_hex = self.pal["risk"][color_key]
        frame = QFrame()
        frame.setObjectName("filterPanel")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(0)
        hdr = QPushButton()
        hdr.setObjectName("filterHdr")
        hdr.setCursor(Qt.PointingHandCursor)
        hdr.setMinimumHeight(30)
        hbl = QHBoxLayout(hdr)
        hbl.setContentsMargins(10, 8, 10, 8)
        hbl.setSpacing(8)
        arrow = QLabel("▸" if not default_open else "▾")
        arrow.setObjectName("filterArrow")
        tlabel = QLabel(title)
        tlabel.setObjectName("filterTitle")
        # 文字不带底色：否则标签的方形底色会盖住按钮的圆角底框
        tlabel.setStyleSheet(f"color:{color_hex};background:transparent;")
        hbl.addWidget(arrow)
        hbl.addWidget(tlabel)
        hbl.addStretch(1)
        fl.addWidget(hdr)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(10, 8, 10, 8)
        bl.setSpacing(6)
        fl.addWidget(body)
        body.setVisible(default_open)
        # 记录标题标签与配色键，主题切换时同步刷新颜色
        self._filter_titles.append((tlabel, color_key))

        def toggle():
            """展开 / 折叠内容区，同时把三角图标翻转对应方向。"""
            vis = not body.isVisible()
            body.setVisible(vis)
            arrow.setText("▾" if vis else "▸")

        hdr.clicked.connect(toggle)
        return frame, bl, tlabel

    # ------------------------------------------------------------ 保一筛选规则三合一容器（顶部 / 右侧 各一份，状态同步）
    def _build_filter_set(self, tag, gb_title):
        """所有筛选整合到一处，返回带 A/B/C 三个可展开内联面板的 QGroupBox：
        A 参与去重类型（金）/ B 优先保留目录（蓝）/ C 保一筛选规则（绿），
        父入口标题按各自理由的颜色标识。tag 取 "top" / "right"，两份 UI 共享
        同一份状态（ext_on / keep_dirs / rule），改动一处另一处自动同步。"""
        p = self.pal
        gb = QGroupBox(gb_title)
        vb = QVBoxLayout(gb)
        vb.setSpacing(8)

        # ---- A 参与去重类型（金色）----
        # 默认折叠：右侧空间紧张，且顶部已有弹出入口，折叠起来也把整个
        # 窗口的最小高度压下来（小尺寸显示器才装得下）
        pa, pa_body, _unused = self._make_filter_panel(
            "gold", "参与类型", default_open=False)
        a_title = QLabel("参与去重类型")
        a_title.setObjectName("h2")
        pa_body.addWidget(a_title)
        lst = QListWidget()
        lst.setMinimumHeight(100)
        lst.setMaximumHeight(180)
        lst.setAlternatingRowColors(True)
        lst.setItemDelegate(RowDelegate(lst))
        lst.itemChanged.connect(lambda item: self._on_ext_toggled(item, tag))
        enable_hover(lst)
        pa_body.addWidget(lst)
        a_gr = QHBoxLayout()
        a_gr.setSpacing(8)
        a_all = QPushButton("全选")
        a_none = QPushButton("全不选")
        a_all.clicked.connect(lambda: self._set_ext(True, tag))
        a_none.clicked.connect(lambda: self._set_ext(False, tag))
        a_gr.addWidget(a_all)
        a_gr.addWidget(a_none)
        pa_body.addLayout(a_gr)
        a_tip = QLabel("勾选的类型才参与自动去重（每组留 1 个、其余自动标记"
                       "删除）；未勾选的类型不会被自动勾选，但你仍可手动勾选删除。")
        a_tip.setObjectName("hint")
        a_tip.setWordWrap(True)
        pa_body.addWidget(a_tip)
        vb.addWidget(pa)

        # ---- B 优先保留目录（蓝色）----
        pb, pb_body, _unused = self._make_filter_panel(
            "migrate", "优先目录")
        b_title = QLabel("优先保留目录")
        b_title.setObjectName("h2")
        pb_body.addWidget(b_title)
        lstk = QListWidget()
        lstk.setMaximumHeight(110)
        lstk.setAlternatingRowColors(True)
        lstk.setItemDelegate(RowDelegate(lstk))
        enable_hover(lstk)
        pb_body.addWidget(lstk)
        b_gr = QHBoxLayout()
        b_gr.setSpacing(8)
        b_cur = QPushButton("当前目录")
        b_cur.setToolTip("把选中文件所在的目录加入优先保留")
        b_add = QPushButton("选择…")
        b_del = QPushButton("移除")
        b_del.setObjectName("danger")
        b_cur.clicked.connect(lambda: self._add_keep_current(tag))
        b_add.clicked.connect(lambda: self._add_keep_browse(tag))
        b_del.clicked.connect(lambda: self._del_keep(tag))
        for b in (b_cur, b_add, b_del):
            b_gr.addWidget(b)
        pb_body.addLayout(b_gr)
        b_tip = QLabel("组里只要有文件落在这些目录下，就一定留它；"
                       "没命中时才按 C 保一筛选规则挑。")
        b_tip.setObjectName("hint")
        b_tip.setWordWrap(True)
        pb_body.addWidget(b_tip)
        vb.addWidget(pb)

        # ---- C 保一筛选规则（绿色）----
        # 【其它】选中某条规则后，标题直接显示该规则的文本
        pc, pc_body, lbl_c = self._make_filter_panel(
            "safe", self._rule_text(tag="panel"))
        c_title = QLabel("保一筛选规则")
        c_title.setObjectName("h2")
        pc_body.addWidget(c_title)
        cb = QComboBox()
        cb.addItems([x for x in RULE_LABELS])
        cb.setCurrentIndex(self.rule if self.rule in RULES else 0)
        cb.setToolTip("组里只要有文件位于「优先保留目录」就留它，"
                      "没命中时才按这里选的规则挑")
        cb.currentIndexChanged.connect(lambda i: self._on_rule_changed(i, tag))
        pc_body.addWidget(cb)
        c_tip = QLabel("扫描完成后点下方「按规则自动选择」，或手动勾选每行。")
        c_tip.setObjectName("hint")
        c_tip.setWordWrap(True)
        pc_body.addWidget(c_tip)
        vb.addWidget(pc)

        if tag == "right":
            self._lbl_rule_right = lbl_c     # 规则切换时刷新【其它】标题
        self._fw[tag] = {"ext": lst, "keep": lstk, "rule": cb, "gb": gb}
        return gb

    def _build_filter_container(self):
        """右侧（详情面板内）的筛选容器：三个可折叠分组框。"""
        return self._build_filter_set("right", "去重保一筛选规则")

    def _build_top_filter(self, lay):
        """顶部「判定方式」右侧同一行的弹出式入口，直接加进传入的布局：
        去重保一筛选规则：【A类型】→【B目录】→【C其它】

        点任一按钮弹出对应的子界面（与右侧三个可折叠分组框内容相同、
        颜色一致、状态双向同步）。顶部空间紧张，用弹出式子界面比再塞
        三个折叠面板省地方，也不会把下面结果表的高度挤掉。"""
        lb = QLabel("去重保一筛选规则：")
        lb.setObjectName("muted")
        lay.addWidget(lb)

        def sep():
            """三个入口之间的箭头分隔符（A → B → C）。"""
            a = QLabel("→")
            a.setObjectName("muted")
            lay.addWidget(a)

        def popbtn(text, key, menu):
            """造一个弹出式入口按钮：点它在下方弹出对应的筛选子界面。"""
            b = QPushButton()
            b.setObjectName("popBtn")
            b.setCursor(Qt.PointingHandCursor)
            # 不用 setMenu：Qt 会额外画一个独立的 menu-indicator 子控件，
            # 它自带底色、还会在按钮外面挤出一块方角（截图里那两个小方块就是这么来的），
            # QSS 里把它设成 0 宽都不一定压得住。改成点击后自己弹菜单，最干净。
            attach_menu(menu, self.sfx)
            b.clicked.connect(
                lambda _=False, m=menu, bb=b:
                m.exec(bb.mapToGlobal(QPoint(0, bb.height() + 2))))
            bl = QHBoxLayout(b)
            bl.setContentsMargins(11, 0, 11, 0)
            bl.setSpacing(6)
            tlabel = QLabel(text)
            # 文字不带底色，整行文字由布局在按钮底框里居中
            tlabel.setStyleSheet(
                f"color:{self.pal['risk'][key]};font-weight:600;"
                "background:transparent;")
            # 折叠 / 展开的小三角统一走 filterArrow 样式，跟右侧面板一个样；
            # Qt 自带的菜单指示箭头已在样式表里隐藏，免得叠出两个三角
            arrow = QLabel("▾")
            arrow.setObjectName("filterArrow")
            bl.addWidget(tlabel)
            bl.addWidget(arrow)
            # 老问题是：按钮本身没有文本（文字在 QLabel 里），布局会把它一路
            # 压到几十像素、把文字裁掉。这里按内容给死最小宽度，三个入口
            # 永远完整显示。
            tlabel.setMinimumWidth(tlabel.sizeHint().width())
            b.setMinimumWidth(tlabel.sizeHint().width()
                              + arrow.sizeHint().width() + 36)
            lay.addWidget(b)
            # 主题切换时同步刷新颜色；【其它】标题换文本时同步文字
            self._filter_titles.append((tlabel, key))
            return b, tlabel

        # ---- A 参与去重类型（金）----
        ma = QMenu(self)
        wa = QWidgetAction(ma)
        pa = QWidget()
        pa.setMinimumWidth(330)
        va = QVBoxLayout(pa)
        va.setContentsMargins(10, 10, 10, 10)
        va.setSpacing(6)
        ta = QLabel("参与去重类型")
        ta.setObjectName("h2")
        va.addWidget(ta)
        lst_ext = QListWidget()
        lst_ext.setMinimumHeight(170)
        lst_ext.setAlternatingRowColors(True)
        lst_ext.setItemDelegate(RowDelegate(lst_ext))
        lst_ext.itemChanged.connect(lambda it: self._on_ext_toggled(it, "top"))
        enable_hover(lst_ext)
        va.addWidget(lst_ext)
        ga = QHBoxLayout()
        ga.setSpacing(8)
        b_all = QPushButton("全选")
        b_none = QPushButton("全不选")
        b_all.clicked.connect(lambda: self._set_ext(True, "top"))
        b_none.clicked.connect(lambda: self._set_ext(False, "top"))
        ga.addWidget(b_all)
        ga.addWidget(b_none)
        ga.addStretch(1)
        va.addLayout(ga)
        tipa = QLabel("勾选的类型才参与自动去重（每组留 1 个、其余自动标记"
                      "删除）；未勾选的类型不会被自动勾选，但你仍可手动勾选删除。")
        tipa.setObjectName("hint")
        tipa.setWordWrap(True)
        va.addWidget(tipa)
        wa.setDefaultWidget(pa)
        ma.addAction(wa)
        popbtn("参与类型", "gold", ma)

        sep()

        # ---- B 优先保留目录（蓝）----
        mb = QMenu(self)
        wbg = QWidgetAction(mb)
        pb = QWidget()
        pb.setMinimumWidth(360)
        vb = QVBoxLayout(pb)
        vb.setContentsMargins(10, 10, 10, 10)
        vb.setSpacing(6)
        tb = QLabel("优先保留目录")
        tb.setObjectName("h2")
        vb.addWidget(tb)
        lst_keep = QListWidget()
        lst_keep.setMinimumHeight(110)
        lst_keep.setAlternatingRowColors(True)
        lst_keep.setItemDelegate(RowDelegate(lst_keep))
        enable_hover(lst_keep)
        vb.addWidget(lst_keep)
        gbk = QHBoxLayout()
        gbk.setSpacing(8)
        b_cur = QPushButton("当前目录")
        b_cur.setToolTip("把选中文件所在的目录加入优先保留")
        b_add = QPushButton("选择…")
        b_del = QPushButton("移除")
        b_del.setObjectName("danger")
        # 这几个按钮会开对话框或改动列表，先把菜单收起来再执行
        b_cur.clicked.connect(lambda: (mb.close(), self._add_keep_current("top")))
        b_add.clicked.connect(lambda: (mb.close(), self._add_keep_browse("top")))
        b_del.clicked.connect(lambda: (mb.close(), self._del_keep("top")))
        for b in (b_cur, b_add, b_del):
            gbk.addWidget(b)
        vb.addLayout(gbk)
        tipb = QLabel("组里只要有文件落在这些目录下，就一定留它；"
                      "没命中时才按 C 保一筛选规则挑。")
        tipb.setObjectName("hint")
        tipb.setWordWrap(True)
        vb.addWidget(tipb)
        wbg.setDefaultWidget(pb)
        mb.addAction(wbg)
        popbtn("优先目录", "migrate", mb)

        sep()

        # ---- C 保一筛选规则（绿）---- 直接做成规则选项菜单，像「判定方式」一样
        mc = QMenu(self)
        from PySide6.QtGui import QActionGroup
        grp = QActionGroup(mc)
        grp.setExclusive(True)
        rule_acts = []
        for i, label in enumerate(RULE_LABELS):
            a = mc.addAction(label)
            a.setCheckable(True)
            a.setChecked(i == self.rule)
            grp.addAction(a)
            a.triggered.connect(
                lambda _c=False, i=i: self._on_rule_changed(i, "top"))
            rule_acts.append(a)
        mc.addSeparator()
        # 说明文字按提示文本的字号与颜色（objectName=hint），不是菜单项样式
        tipw = QWidget()
        tipwl = QVBoxLayout(tipw)
        tipwl.setContentsMargins(10, 2, 10, 6)
        tipc = QLabel("没命中优先目录时，才按这里选的规则挑")
        tipc.setObjectName("hint")
        tipc.setWordWrap(True)
        tipwl.addWidget(tipc)
        wa_tip = QWidgetAction(mc)
        wa_tip.setDefaultWidget(tipw)
        mc.addAction(wa_tip)
        self._btn_rule, self._lbl_rule_top = popbtn(
            self._rule_text("btn"), "safe", mc)

        # 顶部这套控件与右侧共享同一份状态（ext_on / keep_dirs / rule）
        self._fw["top"] = {"ext": lst_ext, "keep": lst_keep, "rule": None,
                           "rule_acts": rule_acts}

    def _fill_roots(self, combo):
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            ctypes.windll.kernel32.GetLogicalDriveStringsW(260, buf)
            drives = [d for d in buf.value.split("\x00") if d]
        except Exception:
            drives = [d for d, _t, _f in sysinfo.get_drives()]
        user = os.path.expanduser("~")
        for p in list(drives) + [user, os.path.join(user, "Downloads"),
                                 os.path.join(user, "Pictures")]:
            combo.addItem(p)
        combo.setEditText(user)

    def _browse(self, combo):
        d = QFileDialog.getExistingDirectory(self, "选择目录")
        if d:
            combo.setEditText(d)

    # ------------------------------------------------------------ 配置回调
    def _on_min_changed(self, v):
        self.min_mb = v
        if self.cfg:
            self.cfg.set("dup_min_mb", v)

    def _on_match_changed(self, i):
        self.match = i
        if self.cfg:
            self.cfg.set("dup_match", i)
        self._sync_sim_ui()

    def _sync_sim_ui(self):
        """相似查找专属控件只在选中第四项时露面。"""
        on = (self.match == MATCH_SIMILAR)
        for w in (self.lb_sim, self.cb_sim, self.lb_sim_tip):
            w.setVisible(on)

    def _on_sim_level_changed(self, i):
        self.sim_level = i
        self.cb_sim.setToolTip(IH.LEVEL_HINTS[i])
        if self.cfg:
            self.cfg.set("sim_level", i)

    def _on_rule_changed(self, i, tag):
        self.rule = i
        if self.cfg:
            self.cfg.set("dup_rule", i)
        # 同步另一处（顶部菜单项 / 右侧组合框）的选中项，两处保持一致
        other = "right" if tag == "top" else "top"
        self._sync_rule_ui(i, other)
        self._refresh_rule_title()      # 【其它】标题显示当前规则文本
        if self.groups:
            self._auto_select()

    def _rule_text(self, tag="btn"):
        """「其它」入口的标题：选中某条规则后直接显示该规则文本，
        例如「其它：创建时间最早」。三角统一由 filterArrow 标签来显示，所以
        这里不再把 ▾ 写进文本里（写进去就会和自带的菜单箭头叠成两个三角）。"""
        label = (RULE_LABELS[self.rule]
                 if 0 <= self.rule < len(RULE_LABELS) else "")
        return f"其它：{label}" if label else "其它"

    def _refresh_rule_title(self):
        """规则切换后，把顶部按钮与右侧面板的「其它」标题一起刷新。"""
        lbl = getattr(self, "_lbl_rule_top", None)
        if lbl is not None:
            lbl.setText(self._rule_text("btn"))
            # 规则名长短不一，最小宽度得跟着改，否则长的还是会被裁
            lbl.setMinimumWidth(lbl.sizeHint().width())
            btn = getattr(self, "_btn_rule", None)
            if btn is not None:
                btn.setMinimumWidth(lbl.sizeHint().width() + 36)
        lbl = getattr(self, "_lbl_rule_right", None)
        if lbl is not None:
            lbl.setText(self._rule_text("panel"))

    def _sync_rule_ui(self, i, tag):
        """把保一规则的选中项同步到另一处：右侧是组合框，顶部是菜单项。"""
        box = self._fw.get(tag, {})
        cb = box.get("rule")
        if cb is not None and hasattr(cb, "setCurrentIndex"):
            cb.blockSignals(True)
            cb.setCurrentIndex(i)
            cb.blockSignals(False)
        for k, a in enumerate(box.get("rule_acts") or []):
            a.setChecked(k == i)

    def _on_mode_changed(self, i):
        self.mode = [RECYCLE, BACKUP, PERMANENT][i]
        if self.cfg:
            self.cfg.set("dup_mode", self.mode)

    def on_theme(self, theme):
        """主题切换：重取调色板，刷新行委托与三个筛选入口的配色。"""
        self.pal = palette(theme)
        self.donut.set_palette(self.pal)
        self.banner.set_palette(self.pal)
        self.del_banner.set_palette(self.pal)
        # 红叉勾选框跟着换色：明暗两套主题的红色取值不同
        self.del_check_style.set_palette(self.pal)
        # 同步刷新 A/B/C 入口标题 / 按钮的配色（随主题明暗变化）
        for w, key in self._filter_titles:
            w.setStyleSheet(
                f"color:{self.pal['risk'][key]};font-weight:600;")
        if self.tw.topLevelItemCount():
            self._refresh_tree()

    # ------------------------------------------------------------ 扫描
    def start_scan(self):
        """开始查找：起线程、显示扫描条，结束后自动按规则选一遍。"""
        if self.thread and self.thread.isRunning():
            return
        # ⚠️ 不能直接 normpath("")：它返回 "."，等于拿程序所在目录当扫描目标。
        # 用户把目录 B 清空是想「只扫一个目录」，不是想扫 exe 旁边那一堆文件。
        raw_a = self.ed_a.currentText().strip()
        raw_b = self.ed_b.currentText().strip()
        a = os.path.normpath(raw_a) if raw_a else ""
        b = os.path.normpath(raw_b) if raw_b else ""
        if not a or not os.path.isdir(a):
            QMessageBox.warning(self, "路径无效", f"目录 A 不存在：{a}")
            return
        if b and not os.path.isdir(b):
            QMessageBox.warning(self, "路径无效", f"目录 B 不存在：{b}")
            return
        if self.cfg:
            self.cfg.set("dup_dir_a", a)
            self.cfg.set("dup_dir_b", b)
        self.btn_scan.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.tw.clear()
        self.groups = []
        # 详情留着上一个文件的预览与指标，会被当成新结果，一并回到初始态
        self._clear_detail()
        for _t in ("top", "right"):
            self._fw[_t]["ext"].clear()
        same = (a.lower() == b.lower())
        where = a if (not b or same) else f"{a} ↔ {b}"
        title = ("正在查找相似图片" if self.match == MATCH_SIMILAR
                 else "正在查找重复文件")
        self.banner.start(f"{title} · {where}", "准备中…",
                          on_stop=self.stop_scan)
        self.finder = DupFinder(
            a, b, self.min_mb * 1024 * 1024, self.match,
            sim_dist=IH.LEVEL_VALUES[
                self.sim_level if 0 <= self.sim_level < 3 else 0],
            sim_gif=self.sim_gif)
        self.thread = DupThread(self.finder, self)
        self.thread.progress.connect(
            lambda what, n: self.banner.set_sub(f"{what} {n} 个…"))
        self.thread.finished_all.connect(self._on_done)
        self.thread.start()

    def stop_scan(self):
        """停止查找：置取消标志，已找到的分组仍然保留。"""
        if getattr(self, "finder", None):
            self.finder.cancel()
        self.btn_stop.setEnabled(False)

    def _on_done(self, groups):
        self.btn_scan.setEnabled(True)
        self.btn_stop.setEnabled(False)
        # 用户点过「这组不是重复」的组合不再出现
        if self.sim_ignored:
            groups = [g for g in (groups or [])
                      if self._group_fp(g) not in self.sim_ignored]
        self.groups = groups or []
        self._fill_ext_list()
        self.total_waste = sum(g.get("waste") or 0 for g in self.groups)
        if self.sfx:
            self.sfx.play("found" if self.groups else "scan_done")
        self._auto_select()
        if not self.groups:
            self.banner.finish("查找完成", self._empty_hint())
            QTimer.singleShot(1600, self.banner.hide)
            self.lb_stat.setText(self._empty_hint())
        else:
            self.banner.finish(
                "查找完成",
                f"{len(self.groups)} 组 · "
                f"涉及 {sum(len(g['files']) for g in self.groups)} 个文件")
            QTimer.singleShot(1600, self.banner.hide)

    def _empty_hint(self):
        """没找到结果时的落空提示：相似模式要说出「为什么可能找不到」。"""
        if self.match == MATCH_SIMILAR:
            # finder 可能还没建（比如结果是被注入的），要先取到 finder 本身
            finder = getattr(self, "finder", None)
            skipped = getattr(finder, "sim_skipped", None) or {}
            n = sum(skipped.values())
            tail = f"（已跳过 {n} 张）" if n else ""
            return ("没有找到相似图片" + tail +
                    "。可以切到「宽松」档，或换一个目录试试——"
                    "裁剪、旋转过的图这种算法认不出来。")
        return "没有找到重复文件，可以降低阈值或换一个目录试试。"

    @staticmethod
    def _group_fp(g):
        """一组文件的指纹：排序后的小写路径拼起来取 md5 前 16 位。

        「这组不是重复」就记这个，下次扫描时同组合直接过滤掉。
        文件增删后指纹会变、忽略自然失效 —— 这是可接受的：忽略本来就是
        针对「当时这一组」的临时决定。
        """
        import hashlib
        paths = sorted((f["path"] or "").lower() for f in g.get("files") or [])
        return hashlib.md5("\n".join(paths).encode("utf-8", "replace")).hexdigest()[:16]

    # ------------------------------------------------------------ 选择
    def _auto_select(self):
        """按保留规则每组留一个，其余全部标记为待删除。
        时间完全相同的组改用文件名判断（保留干净命名、删带副本标记的）；
        落在「优先保留目录」下的文件永远保留——即使整组都在优先目录，
        也不该有任何一项被勾选删除（修复「两个都在优先目录却随机勾一个」的 bug）；
        未勾选的文件类型一律不加入待删集合。"""
        self.delete_set.clear()
        self.keeper_of.clear()
        self.manual_keep.clear()      # 程序化重算，清空手动保留标记
        for gi, g in enumerate(self.groups):
            # 🔴 底线：相似组一律不自动勾选、也不指定「保留项」。
            # dHash 只是「看起来像」，不是「一模一样」——擅自替用户勾上，
            # 万一是两张不同的照片就被删了。这里宁可什么都不做。
            if g.get("kind") == "similar":
                self.keeper_of[gi] = None
                continue
            pref = self._copy_marker_keeper(g["files"])
            keeper = pref if pref else pick_keeper(g["files"], self.rule, self.keep_dirs)
            self.keeper_of[gi] = keeper["path"] if keeper else None
            for f in g["files"]:
                if keeper and f["path"] == keeper["path"]:
                    continue
                # 优先保留目录下的文件一定留它：整组都在优先目录时自然谁都不删
                if in_priority(f["path"], self.keep_dirs):
                    continue
                if self._ext_ok(f["path"]):
                    self.delete_set.add(f["path"].lower())
        # 左下角总结统一由 _update_stat 刷新（_refresh_tree 里已经调用）。
        # 以前这里又单独 setText 成「共 N 组…」，和勾选后显示的「已标记…」
        # 是两套文案，改筛选条件 / 改优先目录时就显得「总结没刷新」。
        self._refresh_tree()

    def _select_none(self):
        self.delete_set.clear()
        self.manual_keep.clear()
        self._refresh_tree()

    def _invert(self):
        allp = set()
        for g in self.groups:
            for f in g["files"]:
                allp.add(f["path"].lower())
        self.delete_set = allp - self.delete_set
        self.manual_keep.clear()
        self._refresh_tree()

    def _group_waste(self, g):
        """这一组按当前勾选能释放多少。

        精确组每份一样大，等价于 size × n_del；相似组每张大小不同，
        必须逐张累加 —— 拿 g["size"]（组内最大那份）去乘会算出离谱的数。
        一个式子同时管两种组：直接加勾选文件的实际大小。
        """
        return sum(f["size"] for f in g["files"]
                   if f["path"].lower() in self.delete_set)

    def _waste_total(self):
        return sum(f["size"] for g in self.groups for f in g["files"]
                   if f["path"].lower() in self.delete_set)

    def _group_col4(self, g):
        """组行最后一列的文案与颜色。

        相似组默认一张都不勾，这时写「可释放 0 B」会让人误以为「这组没什么用」。
        写「待选择」才是实话：值多少取决于你挑哪几张删。
        """
        w = self._group_waste(g)
        if g.get("kind") == "similar" and not w:
            return "待选择", self.pal["text_faint"]
        return f"可释放 {fmt(w)}", self.pal["accent"]

    def _on_item_changed(self, item, col):
        if col != 0:
            return
        path = item.data(0, Qt.UserRole)
        if not path:
            return
        on = item.checkState(0) == Qt.Checked
        # 注：类型筛选只管「自动选择时勾不勾」，不拦手动操作——
        # 未勾选的类型整类不会被自动标记，但用户手动勾选照样可以删。
        if on:
            self.delete_set.add(path.lower())
            self.manual_keep.discard(path.lower())
        else:
            self.delete_set.discard(path.lower())
            self.manual_keep.add(path.lower())   # 用户手动取消勾选 → 手动保留
        # 只就地重绘这一行，不再整树重建——避免高频勾选触发原生闪退。
        self._recolor_one(item)
        # 同步所属分组的「可释放」汇总
        parent = item.parent()
        gi = parent.data(0, Qt.UserRole + 1) if parent is not None else None
        if isinstance(gi, int) and gi < len(self.groups):
            g = self.groups[gi]
            txt, col = self._group_col4(g)
            parent.setText(4, txt)
            parent.setForeground(4, self._c(col))
        self._update_stat()

    def _row_reason(self, path):
        """返回某行的（处置文案, 文件名列颜色, 处置列颜色）三元组。

        颜色优先级（由高到低，高者覆盖）：手动取消(亮紫) > 待删除
        > 类型保留(金) > 优先目录(蓝) > 保一规则(绿) > 普通保留(绿)。
        待删除行只有「待删除」三个字是红的，文件名保持默认文字色——
        红色只出现在处置列，整屏不会五颜六色。"""
        p = self.pal
        low = path.lower()
        keeper = None
        for gi, g in enumerate(self.groups):
            if any(f["path"] == path for f in g["files"]):
                keeper = self.keeper_of.get(gi)
                break
        is_keeper = keeper is not None and keeper == path
        ext_protected = not self._ext_ok(path)
        pri = in_priority(path, self.keep_dirs)
        manual = low in self.manual_keep
        if manual:
            c = self._c(p["risk"]["purple"])
            return "手动保留", c, c
        # 手动勾选优先于类型保护：用户明确点了要删，就按待删除算
        if low in self.delete_set:
            return "待删除", self._c(p["text"]), self._c(p["risk"]["danger"])
        if ext_protected:
            c = self._c(p["risk"]["gold"])
            return "类型保留", c, c
        if pri:
            c = self._c(p["risk"]["migrate"])
            return "目录保留", c, c
        if is_keeper:
            c = self._c(p["risk"]["safe"])
            return "保一保留", c, c
        c = self._c(p["risk"]["safe"])
        return "保留", c, c

    def _recolor_one(self, item):
        """手动勾选变化后只重绘当前行（不重建整树）。"""
        path = item.data(0, Qt.UserRole)
        if not path:
            return
        st, c0, c4 = self._row_reason(path)
        item.setText(4, st)
        item.setForeground(0, c0)
        item.setForeground(4, c4)

    def _update_stat(self):
        """左下角总结：勾选、改筛选条件、改优先目录、全选/反选之后都走这里，
        保证任何操作后看到的都是同一句话的实时数字。"""
        n = len(self.delete_set)
        self.btn_clean.setEnabled(n > 0)
        if not self.groups:
            # 还没扫描出结果：保留「选择目录后点开始查找」这类提示语
            return
        files = sum(len(g["files"]) for g in self.groups)
        n_sim = sum(1 for g in self.groups if g.get("kind") == "similar")
        if n_sim:
            # 相似组和精确组混在一起报一个总数会误导（「这 5 组里哪些能放心删？」），
            # 所以分开说。精确模式的原有文案一个字符都不动。
            self.lb_stat.setText(
                f"已标记 <b>{n}</b> 个待删除 · "
                f"精确 <b>{len(self.groups) - n_sim}</b> 组 · "
                f"疑似相似 <b>{n_sim}</b> 组 · "
                f"可释放 <b>{fmt(self._waste_total())}</b>")
            return
        self.lb_stat.setText(
            f"已标记 <b>{n}</b> 个待删除 · "
            f"可释放 <b>{fmt(self._waste_total())}</b> · "
            f"共 <b>{len(self.groups)}</b> 组 / <b>{files}</b> 个文件")

    # ------------------------------------------------------------ 按类型筛选（未勾选的类型绝不删）
    _COPY_NUM_RE = re.compile(r"[\(（]\d+[\)）]$")

    def _ext_of(self, path):
        """返回小写后缀，如 '.dll'；无后缀返回空串。"""
        return os.path.splitext(path)[1].lower()

    def _ext_ok(self, path):
        """该文件类型是否允许被删除（在勾选的范围内）。"""
        return self._ext_of(path) in self.ext_on

    def _fill_ext_list(self):
        """扫描完成后列出所有出现的文件类型，默认全部勾选（都可删）。"""
        from collections import Counter
        c = Counter(self._ext_of(f["path"])
                    for g in self.groups for f in g["files"])
        self.ext_on = set()
        for tag in ("top", "right"):
            lst = self._fw[tag]["ext"]
            lst.blockSignals(True)
            lst.clear()
            for ext, n in sorted(c.items()):
                label = ext if ext else "(无后缀)"
                it = QListWidgetItem(f"{label}  ({n})")
                it.setData(Qt.UserRole, ext)
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked)
                lst.addItem(it)
                self.ext_on.add(ext)
            lst.blockSignals(False)

    def _on_ext_toggled(self, item, tag):
        # 注：QListWidget.itemChanged 信号只发 (item,)，列表项无列概念，
        # 故这里签名为 (item,) 且用 item.checkState()（不带列参数）。
        if not item:
            return
        ext = item.data(Qt.UserRole)
        if ext is None:
            return
        if item.checkState() == Qt.Checked:
            self.ext_on.add(ext)
        else:
            self.ext_on.discard(ext)
        # 类型开关会改变「哪些能删」，按当前保留规则重新派生待删集合；
        # 同步另一处 UI 的勾选状态（顶部 / 右侧两份保持一致）
        other = "right" if tag == "top" else "top"
        self._sync_ext_list(other)
        self._auto_select()

    def _sync_ext_list(self, tag):
        """把另一处 ext 列表的勾选状态同步到 self.ext_on。"""
        lst = self._fw[tag]["ext"]
        lst.blockSignals(True)
        for i in range(lst.count()):
            it = lst.item(i)
            ext = it.data(Qt.UserRole)
            it.setCheckState(Qt.Checked if ext in self.ext_on else Qt.Unchecked)
        lst.blockSignals(False)

    def _set_ext(self, on, tag):
        self.ext_on = set()
        src = self._fw[tag]["ext"]
        src.blockSignals(True)
        for i in range(src.count()):
            it = src.item(i)
            it.setCheckState(Qt.Checked if on else Qt.Unchecked)
            if on:
                self.ext_on.add(it.data(Qt.UserRole))
        src.blockSignals(False)
        # 同步另一处列表的勾选状态
        other = "right" if tag == "top" else "top"
        self._sync_ext_list(other)
        self._auto_select()

    # （保留类型 / 展开按钮已整合进 _build_filter_container 的 A 面板）

    def _looks_like_copy(self, path):
        """文件名尾部带 副本 / (1) / copy 等标记，明显是不小心复制的那份。"""
        stem = os.path.splitext(os.path.basename(path))[0].lower()
        if self._COPY_NUM_RE.search(stem):
            return True
        for s in (" - 副本", " -副本", "副本", " - 拷贝", "拷贝",
                 " - copy", " copy", "-copy", "的副本"):
            if stem.endswith(s):
                return True
        return False

    def _copy_marker_keeper(self, files):
        """当整组文件的创建 / 修改时间完全一致（时间无法区分谁更早）时，
        用文件名判断：带「副本 / (1) / copy」等标记的明显是不小心复制的那份，
        默认保留干净命名、删掉带标记的。时间不同则交回原保留规则。
        仅在时间相同时才跑文件名判断，几乎零开销。"""
        if len(files) < 2:
            return None
        if len({f["ctime"] for f in files}) != 1:
            return None
        if len({f["mtime"] for f in files}) != 1:
            return None
        copies = [f for f in files if self._looks_like_copy(f["path"])]
        normals = [f for f in files if f not in copies]
        if not copies or not normals:
            return None
        normals.sort(key=lambda f: len(os.path.basename(f["path"])))
        return {"path": normals[0]["path"]}

    # ------------------------------------------------------------ 渲染
    # 「处置」列的排序权重：保留类（保一/类型/目录/手动）→ 待删除
    DEAL_RANK = {"保一保留": 0, "类型保留": 0, "目录保留": 0,
                 "手动保留": 0, "保留": 0, "待删除": 1}

    def _sort_key(self, col, item):
        """处置列按状态权重排；其余列（大小/时间/可释放）都存了真实数值，
        交给默认规则即可。"""
        if item is None or col != 4:
            return None
        if not item.data(0, Qt.UserRole):      # 顶层是「分组行」，按可释放字节排
            return None
        return self.DEAL_RANK.get(item.text(4), 9)

    def _refresh_tree(self):
        p = self.pal
        self.tw.blockSignals(True)
        self.tw.clear()
        for gi, g in enumerate(self.groups):
            size = g["size"]
            files = g["files"]
            is_sim = g.get("kind") == "similar"
            if is_sim:
                # 相似组里每张大小都不同，标题不能写「N 个相同文件」；
                # 第一列给组内合计，让用户一眼知道这堆图一共占多少
                total = sum(f["size"] for f in files)
                head = f"疑似相似 · {len(files)} 张图片"
                col1, sort1 = fmt(total), total
            else:
                head = f"{fmt(size)} · {len(files)} 个相同文件"
                col1, sort1 = fmt(size), size
            col4, fg4 = self._group_col4(g)
            git = QTreeWidgetItem([
                head, col1, "", "", col4,
            ])
            git.setData(0, Qt.UserRole, "")
            git.setData(0, Qt.UserRole + 1, gi)
            git.setData(1, Qt.UserRole, sort1)                # 大小列排序用
            git.setData(4, Qt.UserRole, self._group_waste(g))  # 可释放列排序用
            git.setForeground(0, self._c(
                p["risk"]["gold"] if is_sim else p["text"]))
            git.setForeground(4, self._c(fg4))
            if is_sim:
                git.setToolTip(0, "算法判断这几张图像同一张图，但没有逐字节核对。"
                                  "请展开看一眼再决定删哪个。")
            self.tw.addTopLevelItem(git)
            for f in files:
                st, c0, c4 = self._row_reason(f["path"])
                it = QTreeWidgetItem([
                    f["path"],
                    fmt(f["size"]),                 # 相似组每张大小不同，逐行取自己的
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(f["ctime"])),
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(f["mtime"])),
                    st,
                ])
                it.setData(0, Qt.UserRole, f["path"])
                it.setData(1, Qt.UserRole, f["size"])     # 大小列排序用
                it.setData(2, Qt.UserRole, f["ctime"])    # 创建时间列排序用
                it.setData(3, Qt.UserRole, f["mtime"])    # 修改时间列排序用
                it.setToolTip(0, f["path"])
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(0, Qt.Checked if (low := f["path"].lower()) in self.delete_set else Qt.Unchecked)
                # 文件名列与处置列分开上色：待删除只有处置列的三个字是红的
                it.setForeground(0, c0)
                it.setForeground(4, c4)
                git.addChild(it)
        self.tw.blockSignals(False)
        self.tw.expandToDepth(0)
        # 重建后恢复用户点表头选的顺序（没点过就保持原有的分组顺序）
        self.sorter.sort_now()
        self._update_stat()
        # 延一拍再算「哪些行看得见」：刚重建完布局还没稳定，
        # visualItemRect 拿不到有效矩形，会白算一趟
        QTimer.singleShot(60, self._queue_visible_thumbs)

    def _c(self, hexv):
        from PySide6.QtGui import QColor
        return QColor(hexv)

    # ------------------------------------------------------------ 预览缩略图
    def _on_thumb_toggled(self, on):
        """总开关：关掉后不再读盘、不查缓存，并把已经贴上的小图撤掉。"""
        self.thumbs_on = bool(on)
        if self.cfg:
            self.cfg.set("dup_thumbs", self.thumbs_on)
        if not self.thumbs_on:
            self._thumb_pending.clear()
            self._thumb_icons.clear()
            self.lb_thumb.setVisible(False)
            self.tw.blockSignals(True)
            self._walk_items(lambda it: it.setIcon(0, QIcon()))
            self.tw.blockSignals(False)
        else:
            if self._detail_path:
                self._show_detail_thumb(self._detail_path)
            self._queue_visible_thumbs()

    def _walk_items(self, fn):
        """遍历树上每一行（顶层 + 已展开的子层）执行 fn。"""
        def walk(it):
            fn(it)
            for i in range(it.childCount()):
                walk(it.child(i))
        for i in range(self.tw.topLevelItemCount()):
            walk(self.tw.topLevelItem(i))

    def _path_items_map(self):
        """path -> [行] 的映射。批量贴图时用，避免每贴一张都全树遍历一遍。"""
        m = {}

        def walk(it):
            p = it.data(0, Qt.UserRole)
            if p:
                m.setdefault(p, []).append(it)
            for i in range(it.childCount()):
                walk(it.child(i))
        self._walk_items(walk)
        return m

    def _visible_paths(self, limit=60):
        """当前真正看得见的文件行——缩略图只给这些行生成，不做全量预生成。"""
        if self.tw.topLevelItemCount() == 0:
            return []
        vp = self.tw.viewport().rect()
        out = []

        def walk(it):
            if len(out) >= limit:
                return
            r = self.tw.visualItemRect(it)
            if r.isValid() and r.intersects(vp):
                p = it.data(0, Qt.UserRole)
                if p:
                    out.append(p)
                    if len(out) >= limit:
                        return
            for i in range(it.childCount()):
                walk(it.child(i))
        for i in range(self.tw.topLevelItemCount()):
            walk(self.tw.topLevelItem(i))
            if len(out) >= limit:
                break
        return out

    def _queue_visible_thumbs(self):
        """给可见行补缩略图：命中缓存就直接贴，否则丢一个后台任务。"""
        if not self.thumbs_on or self.tw.topLevelItemCount() == 0:
            return
        m = self._path_items_map()
        pool = QThreadPool.globalInstance()
        # setIcon 会触发 itemChanged，而那个处理器会改「待删除」集合，
        # 所以这一整段必须屏蔽信号
        self.tw.blockSignals(True)
        try:
            for p in self._visible_paths():
                ic = self._thumb_icons.get(p)
                if ic is None:
                    hit = thumbs.cached(p)          # 纯 sqlite 读，很快
                    if hit is not None:
                        self._remember(p, hit)
                        ic = self._thumb_icons.get(p)
                    else:
                        if thumbs.wanted(p) and p not in self._thumb_pending:
                            self._thumb_pending.add(p)
                            pool.start(_ThumbTask(p, self._thumb_sig))
                        continue
                for it in m.get(p, ()):
                    it.setIcon(0, ic)
        finally:
            self.tw.blockSignals(False)

    def _remember(self, path, img):
        """收进内存缓存（有上限，超了丢最早的那张）。"""
        if img is None or img.isNull():
            return
        if len(self._thumb_icons) >= THUMB_MEM_MAX:
            try:
                self._thumb_icons.pop(next(iter(self._thumb_icons)))
            except (StopIteration, KeyError):
                pass
        self._thumb_icons[path] = QIcon(QPixmap.fromImage(img))

    def _on_thumb_done(self, path, img):
        """后台生成完回到主线程：贴到列表上；若它还停在详情面板就也画上。"""
        self._thumb_pending.discard(path)
        if not self.thumbs_on:
            return
        if img is not None:
            self._remember(path, img)
            ic = self._thumb_icons.get(path)
            if ic is not None:
                self.tw.blockSignals(True)
                try:
                    for it in self._path_items_map().get(path, ()):
                        it.setIcon(0, ic)
                finally:
                    self.tw.blockSignals(False)
        if path == self._detail_path:
            self._paint_detail(img)

    def _show_detail_thumb(self, path):
        """详情面板大图：有缓存立刻显示，没有就排队生成（先给占位文字）。"""
        self._detail_path = path
        if not self.thumbs_on:
            self.lb_thumb.setVisible(False)
            return
        ic = self._thumb_icons.get(path)
        if ic is not None:
            self.lb_thumb.setPixmap(
                ic.pixmap(THUMB_DETAIL_PX, THUMB_DETAIL_PX))
            self.lb_thumb.setVisible(True)
            return
        hit = thumbs.cached(path)
        if hit is not None:
            self._remember(path, hit)
            self._paint_detail(hit)
            return
        if not thumbs.wanted(path):
            self.lb_thumb.setVisible(False)
            return
        self.lb_thumb.setPixmap(QPixmap())
        self.lb_thumb.setText("预览生成中…")
        self.lb_thumb.setVisible(True)
        if path not in self._thumb_pending:
            self._thumb_pending.add(path)
            QThreadPool.globalInstance().start(
                _ThumbTask(path, self._thumb_sig))

    def _paint_detail(self, img):
        """把一张 QImage 画进详情面板（等比缩到 160，不变形）。"""
        if img is None or img.isNull():
            self.lb_thumb.setPixmap(QPixmap())
            self.lb_thumb.setText("无预览")
            self.lb_thumb.setVisible(True)
            return
        self.lb_thumb.setPixmap(
            QPixmap.fromImage(thumbs.fit(img, THUMB_DETAIL_PX)))
        self.lb_thumb.setText("")
        self.lb_thumb.setVisible(True)

    # ------------------------------------------------------------ 详情
    def _clear_detail(self):
        """右侧详情回到初始态（开始新一次查找时调用）。

        结果树已经 clear 了，预览图 / 环形图 / 指标还停在上一轮选中的文件上，
        和新结果对不上号，所以一起重置。
        """
        self._detail_path = ""
        self.lb_title.setText("重复项详情")
        self.lb_path.set_path("")
        self.lb_thumb.setPixmap(QPixmap())
        self.lb_thumb.setText("")
        self.lb_thumb.setVisible(False)
        self.lb_size.setText("—")
        self.lb_ct.setText("—")
        self.lb_mt.setText("—")
        self.lb_dir.setText("—")
        self.donut.set(0, "—", "", self.pal["text_faint"])

    def _on_select(self, cur, prev):
        if not cur:
            return
        path = cur.data(0, Qt.UserRole)
        if not path:
            return
        p = self.pal
        try:
            st = os.stat(path)
        except OSError:
            return
        grp = None
        for g in self.groups:
            if any(f["path"] == path for f in g["files"]):
                grp = g
                break
        self.lb_title.setText(os.path.basename(path))
        self.lb_path.set_path(path)
        self._show_detail_thumb(path)
        self.lb_size.setText(fmt(st.st_size))
        self.lb_ct.setText("创建 " + time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(st.st_ctime)))
        self.lb_mt.setText("修改 " + time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(st.st_mtime)))
        self.lb_dir.setText("所在目录：" + os.path.dirname(path))
        if grp:
            is_sim = grp.get("kind") == "similar"
            # 相似组每张大小不同，套 size×(n-1) 会算出离谱的数；
            # 三个地方（列表汇总 / 详情环形图 / 底部统计）现在统一走这个口径
            waste = grp.get("waste")
            if waste is None:
                waste = grp["size"] * (len(grp["files"]) - 1)
            tot = self.total_waste or max(waste, 1)
            if is_sim:
                total_b = sum(f["size"] for f in grp["files"])
                mx = max((f.get("sim") or 0.0) for f in grp["files"])
                self.donut.set(waste / tot if tot else 0, fmt(total_b),
                               f"{len(grp['files'])} 张 · 最像 {mx:.0f}%",
                               p["risk"]["gold"])
            else:
                self.donut.set(waste / tot if tot else 0, fmt(grp["size"]),
                               f"{len(grp['files'])} 个副本",
                               p["accent"])
        else:
            self.donut.set(0, fmt(st.st_size), "单文件", p["text_faint"])

    def _cur_file(self):
        it = self.tw.currentItem()
        if not it:
            return None
        return it.data(0, Qt.UserRole) or None

    def _keep_this(self):
        self._keep_at(self._cur_file())

    def _keep_at(self, path):
        if not path:
            return
        low = path.lower()
        for gi, g in enumerate(self.groups):
            if any(f["path"] == path for f in g["files"]):
                self.keeper_of[gi] = path
                self.manual_keep.add(low)     # 用户「仅保留此项」= 手动保留
                for f in g["files"]:
                    if f["path"] != path:
                        # 优先保留目录下的文件永远留它，即使用户点了「仅保留此项」
                        if in_priority(f["path"], self.keep_dirs):
                            continue
                        if self._ext_ok(f["path"]):
                            self.delete_set.add(f["path"].lower())
                            self.manual_keep.discard(f["path"].lower())
                    else:
                        self.delete_set.discard(f["path"].lower())
                break
        self._refresh_tree()

    def _open_sel(self):
        self._open_at(self._cur_file())

    def _open_at(self, path):
        if not path:
            return
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        except Exception as e:
            QMessageBox.warning(self, "无法打开", str(e))

    # ------------------------------------------------------------ 右键菜单
    def _menu(self, pos):
        it = self.tw.itemAt(pos)
        if not it:
            return
        self.tw.setCurrentItem(it)
        m = self._make_menu(it)
        m.exec(self.tw.viewport().mapToGlobal(pos))

    def _make_menu(self, it):
        """结果行右键：文件行给处置类操作，分组行给整组操作。"""
        path = it.data(0, Qt.UserRole) or ""
        self.sfx = self.sfx or NullSfx()
        m = QMenu(self)
        if path:
            low = path.lower()
            head = m.addAction(os.path.basename(path) or path)
            head.setEnabled(False)
            m.addSeparator()
            act(m, "仅保留此项", "keep", lambda: self._keep_at(path))
            if low in self.delete_set:
                act(m, "取消待删除", "check_off",
                    lambda: self._toggle_del(path, False))
            else:
                act(m, "标记为待删除", "check_on",
                    lambda: self._toggle_del(path, True))
            m.addSeparator()
            act(m, "打开所在位置", "open", lambda: self._open_at(path))
            act(m, "复制完整路径", "copy",
                lambda: QApplication.clipboard().setText(path))
            act(m, "把所在目录设为优先保留", "keep",
                lambda: self._add_keep_dir(os.path.dirname(path)))
        else:
            gi = it.data(0, Qt.UserRole + 1)
            g = self.groups[gi] if isinstance(gi, int) and gi < len(self.groups) else None
            head = m.addAction(it.text(0))
            head.setEnabled(False)
            m.addSeparator()
            if g:
                act(m, "本组其余全部标记删除", "warn",
                    lambda: self._group_all(g, True))
                act(m, "本组全部取消", "check_off",
                    lambda: self._group_all(g, False))
                if g.get("kind") == "similar":
                    act(m, "这组不是重复", "keep",
                        lambda: self._ignore_group(g))
                m.addSeparator()
            exp = it.isExpanded()
            act(m, "折叠" if exp else "展开", "toggle",
                lambda: it.setExpanded(not exp))
            act(m, "展开全部分组", "toggle", lambda: self.tw.expandAll())
            act(m, "折叠全部分组", "toggle", lambda: self.tw.collapseAll())
        return attach_menu(m, self.sfx)

    def _toggle_del(self, path, on):
        low = path.lower()
        if on:
            self.delete_set.add(low)
            self.manual_keep.discard(low)
        else:
            self.delete_set.discard(low)
            self.manual_keep.add(low)     # 右键「取消待删除」= 手动保留
        self._refresh_tree()

    def _group_all(self, g, on):
        try:
            gi = self.groups.index(g)
        except ValueError:
            gi = -1
        keeper_path = self.keeper_of.get(gi)
        for f in g["files"]:
            low = f["path"].lower()
            if on:
                if keeper_path and f["path"] == keeper_path:
                    continue
                # 优先保留目录下的文件永远留它
                if in_priority(f["path"], self.keep_dirs):
                    continue
                if self._ext_ok(f["path"]):
                    self.delete_set.add(low)
                    self.manual_keep.discard(low)
            else:
                self.delete_set.discard(low)
                self.manual_keep.add(low)   # 整组取消 = 全部手动保留
        self._refresh_tree()

    def _ignore_group(self, g):
        """「这组不是重复」：记下指纹，这组从列表里消失，下次扫描也不再出现。

        删掉一组会让 self.groups 的下标整体前移，而 keeper_of 是按下标存的
        —— 所以必须整体重算一遍，不能只删列表项。重算会清掉用户的勾选，
        这里把手动勾过的再恢复回来。
        """
        fp = self._group_fp(g)
        if fp not in self.sim_ignored:
            self.sim_ignored.append(fp)
            if self.cfg:
                self.cfg.set("sim_ignored", self.sim_ignored)
        keep_sel = set(self.delete_set)
        self.groups = [x for x in self.groups if x is not g]
        alive = {f["path"].lower() for x in self.groups for f in x["files"]}
        self._auto_select()                       # 重建 keeper_of + 树
        self.delete_set = {p for p in keep_sel if p in alive}
        self._refresh_tree()

    # ------------------------------------------------------------ 优先目录
    def _fill_keep_dirs(self):
        for tag in ("top", "right"):
            lst = self._fw.get(tag, {}).get("keep")
            if lst is None:
                continue
            lst.clear()
            for d in self.keep_dirs:
                it = QListWidgetItem(d)
                it.setToolTip(d)
                lst.addItem(it)

    def _add_keep_dir(self, d):
        if not d:
            return
        d = os.path.normpath(d)
        for x in self.keep_dirs:
            if x.lower() == d.lower():
                return
        self.keep_dirs.append(d)
        if self.cfg:
            self.cfg.set("dup_keep_dirs", self.keep_dirs)
        self._fill_keep_dirs()
        if self.groups:
            self._auto_select()

    def _add_keep_current(self, tag):
        path = self._cur_file()
        self._add_keep_dir(os.path.dirname(path) if path else "")

    def _add_keep_browse(self, tag):
        d = QFileDialog.getExistingDirectory(self, "选择优先保留目录")
        if d:
            self._add_keep_dir(d)

    def _del_keep(self, tag):
        lst = self._fw[tag]["keep"]
        row = lst.currentRow()
        if row < 0:
            return
        self.keep_dirs.pop(row)
        if self.cfg:
            self.cfg.set("dup_keep_dirs", self.keep_dirs)
        self._fill_keep_dirs()
        if self.groups:
            self._auto_select()

    # ------------------------------------------------------------ 删除（后台线程 + 删除进度条）
    def _do_clean(self):
        # ---- 安全护栏：任何一组若被整体标记为删除，强制保留一个副本，
        # 杜绝「切换保留规则 / 误勾选后把整组重复文件全部删光」这类事故。
        unsafe = []
        for gi, g in enumerate(self.groups):
            files = g["files"]
            if len(files) < 2:
                continue
            marked = [f for f in files if f["path"].lower() in self.delete_set]
            if len(marked) == len(files):
                keeper = self.keeper_of.get(gi)
                if not keeper:
                    kp = pick_keeper(files, self.rule, self.keep_dirs)
                    keeper = kp["path"] if kp else None
                if keeper:
                    self.delete_set.discard(keeper.lower())
                    unsafe.append(keeper)
        if unsafe:
            self._refresh_tree()
            QMessageBox.warning(
                self, "已自动保护",
                "检测到有整组重复文件被全部标记为删除，已自动为每组保留一个副本，"
                "避免误删光：\n\n"
                + "\n".join(short_path(p, 1, 2, 90) for p in unsafe[:10]))
        # 只删用户确认过的待删项；手动勾上的不受类型筛选限制
        paths = [f["path"] for g in self.groups for f in g["files"]
                 if f["path"].lower() in self.delete_set]
        if not paths:
            return
        total = self._waste_total()
        mode = self.mode
        if mode == PERMANENT:
            ok = QMessageBox.warning(
                self, "危险操作",
                f"即将<b>永久删除</b> {len(paths)} 个文件（{fmt(total)}），"
                f"无法通过回收站恢复。确定继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ok != QMessageBox.Yes:
                return
        else:
            ok = QMessageBox.question(
                self, "确认删除",
                f"将{MODE_LABEL.get(mode, '')} {len(paths)} 个重复文件，"
                f"释放约 {fmt(total)}。\n每组会留下一个副本。确定继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ok != QMessageBox.Yes:
                return

        backup_root = None
        if mode == BACKUP:
            backup_root = QFileDialog.getExistingDirectory(self, "选择备份目录")
            if not backup_root:
                return

        # 交给后台线程逐个删除，避免大批量时界面卡死；同时用红色"删除中"
        # 状态条实时显示进度（i/总量），和扫描进度条一个套路。
        self._clean_paths = list(paths)
        self._clean_mode = mode
        self._clean_cancelled = False
        self.journal.begin([("重复文件去重", paths, total)],
                           MODE_LABEL.get(mode, ""))
        self.btn_clean.setEnabled(False)
        self.del_banner.start(
            f"正在删除重复项 · {MODE_LABEL.get(mode, '')}",
            f"0/{len(paths)} · 准备中…",
            on_stop=self._cancel_clean,
            maximum=max(len(paths), 1))
        self.thread_clean = DupCleanThread(paths, mode, backup_root,
                                           self.journal, self)
        self.thread_clean.progress.connect(
            lambda i, t, name: self.del_banner.set_sub(
                f"{i}/{t} · 正在删除 {name}", value=i, maximum=t))
        self.thread_clean.finished_all.connect(self._on_clean_done)
        self.thread_clean.start()

    def _cancel_clean(self):
        self._clean_cancelled = True
        if getattr(self, "thread_clean", None):
            self.thread_clean.cancel()
        self.del_banner.set_sub("正在取消，请稍候…")

    def _on_clean_done(self, freed, errs, _attempted):
        paths = getattr(self, "_clean_paths", [])
        removed = [p for p in paths if not os.path.exists(p)]
        failed = len(paths) - len(removed)
        mode = getattr(self, "_clean_mode", "")
        self.journal.end(freed, len(removed), MODE_LABEL.get(mode, ""))

        # 从结果里剔掉已经删掉的
        gone = {p.lower() for p in removed}
        for g in self.groups:
            g["files"] = [f for f in g["files"]
                          if f["path"].lower() not in gone]
        self.groups = [g for g in self.groups if len(g["files"]) > 1]
        self.delete_set.clear()
        self._auto_select()

        self.btn_clean.setEnabled(len(self.delete_set) > 0)
        if getattr(self, "_clean_cancelled", False):
            self.del_banner.finish(
                "已取消删除",
                f"已处理 {len(removed)} 个 · 释放 {fmt(freed)}")
        else:
            self.del_banner.finish(
                "删除完成",
                f"已处理 {len(removed)} 个 · 释放 {fmt(freed)}")
        QTimer.singleShot(1800, self.del_banner.hide)

        msg = f"已处理 {len(removed)} 个重复文件，释放 {fmt(freed)}。"
        if failed:
            msg += f"\n（另有 {failed} 个因被占用等原因未删除）"
        if errs:
            msg += "\n\n未处理（多是被占用）：\n" + "\n".join(
                short_path(str(x), 1, 2, 90) for x in errs[:10])
        msg += f"\n\n具体路径已写入日志：\n{self.journal.path}"
        QMessageBox.information(self, "去重完成", msg)
        if self.sfx:
            self.sfx.play("clean_done")
