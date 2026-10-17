"""主窗口：项目清理 / 目录分析 / 大文件定位 / 文件去重 / 系统信息 / 标记 / 设置。"""
import os
import time
import math
import subprocess

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QRectF, QSize
from PySide6.QtGui import (
    QColor, QPainter, QFont, QKeySequence, QShortcut,
)
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTreeWidget, QTreeWidgetItem, QListWidget, QListWidgetItem, QFrame,
    QTabWidget, QTextBrowser, QProgressBar, QHeaderView, QMessageBox,
    QDialog, QRadioButton, QFileDialog, QLineEdit, QGroupBox, QInputDialog,
    QAbstractItemView, QSplitter, QTableWidget, QTableWidgetItem, QCheckBox,
    QApplication, QStyle, QStyledItemDelegate, QComboBox, QSpinBox, QSlider,
    QGridLayout, QMenu, QLayout, QSizePolicy,
)

from core import system as sysinfo
from core import rules as rulesmod
from core.rules import load_rules, RISK_LABEL, RISK_ORDER
from core.scanner import Scanner, find_large_files
from core.cleaner import Cleaner, RECYCLE, BACKUP, PERMANENT, MODE_LABEL, collect_entries
from core.marks import MarkStore, MARK_KEEP
from core.config import Config
from core.journal import Journal, log_path
from core.knowledge import knowledge, KIND_LABEL, filetypes
from core.sound import Sfx
from core.sizefmt import fmt_bytes, fmt_pct, pct_of
from core.version import APP_NAME
from core import cache as scan_cache
from core import thumbs as thumb_mod
from ui.theme import qss, palette, DEFAULT_THEME
from ui.widgets import (
    fmt, chip, esc, html_path, short_path, elide_middle,
    RowDelegate, PathLabel, BigCheckStyle, PathTreeWidget,
    enable_hover, setup_tree, HeaderSorter, FirstColFit, refresh_delegates,
)
from ui.sound import SoundFilter, attach_menu, act
from ui.about import AboutDialog
from ui.report import CleanReportDialog
from ui.busy import ScanBanner, DeleteBanner
from ui.charts import Donut, TypeBars
from ui.dir_page import DirScanPage, MarksPage
from ui.sysclean_page import SysCleanPage
from ui.dup_page import DupPage
from ui.dupdir_page import DupDirPage


def dir_page_roots():
    """顶部目标为「目录」时的常用位置候选（目录分析页 / 大文件定位页共用）。

    大文件定位以前只能选盘，扫大容量盘很慢；解锁目录后这些快捷位置就是
    最高频的目标——挑一个文件夹比扫整盘快得多。
    """
    user = os.path.expanduser("~")
    cand = [os.path.join(user, "Downloads"),
            os.path.join(user, "Desktop"),
            os.path.join(user, "Documents"),
            os.path.join(user, "Videos"),
            os.path.join(user, "Pictures"),
            os.path.join(user, "AppData"),
            os.path.join(user, "AppData", "Local"),
            os.path.join(user, "AppData", "Roaming"),
            "C:\\ProgramData",
            "C:\\Program Files"]
    out, seen = [], set()
    for p in cand:
        k = p.lower()
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


# 清理项目页「风险」列的排序权重：越值得动手的越靠前。
# 直接从 core.rules 的 RISK_ORDER / RISK_LABEL 派生，避免以后改了风险文案而这里没跟上
# （曾经手写成一组和真实文案对不上的键，导致点风险表头排序几乎无效）。
CLEAN_RISK_RANK = {txt: w for txt, w in
                   ((v, RISK_ORDER.get(k, 9)) for k, v in RISK_LABEL.items())}
CLEAN_RISK_RANK["白名单"] = 9          # 白名单永远沉到最后


# ============================================================ 线程
class ScanThread(QThread):
    """后台扫描各条清理规则的占用，逐条回报进度。"""
    progress = Signal(int, int, str)
    finished_all = Signal(object)

    def __init__(self, rules, base=0, total=None, parent=None):
        super().__init__(parent)
        self.rules = rules
        self.base = base
        self.total = total if total is not None else len(rules)
        self.scanner = None
        self.stopped = False

    def run(self):
        self.scanner = Scanner(self.rules)
        res = self.scanner.run(
            base=self.base, total=self.total,
            on_progress=lambda d, t, n: self.progress.emit(d, t, n))
        self.finished_all.emit(res)

    def stop(self):
        self.stopped = True
        if self.scanner:
            self.scanner.cancel()


class CleanThread(QThread):
    """后台执行清理。删除动作必须离开 UI 线程，否则大目录删起来界面会卡死。"""
    progress = Signal(int, int, str)
    finished_all = Signal(object)

    def __init__(self, cleaner, plan, mode, backup_root, journal=None, parent=None):
        super().__init__(parent)
        self.cleaner = cleaner
        self.plan = plan
        self.mode = mode
        self.backup_root = backup_root
        self.journal = journal

    def run(self):
        stats = self.cleaner.clean(
            self.plan, self.mode, self.backup_root,
            on_progress=lambda i, t, n: self.progress.emit(i, t, n),
            journal=self.journal)
        self.finished_all.emit(stats)


class LargeFileThread(QThread):
    """后台找大文件。整盘遍历很慢，必须能中途取消（接扫描条上的停止按钮）。

    除了最后一次性给结果，还会边扫边通过 batch 吐中途快照——扫大容量盘
    时界面能一路长出结果，而不是进度条空转到最后。
    """
    progress = Signal(int)
    batch = Signal(object)          # 中途累计结果快照 [(size, path)]
    finished_all = Signal(object)

    def __init__(self, root, min_bytes, parent=None):
        super().__init__(parent)
        self.root = root
        self.min_bytes = min_bytes
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        self.finished_all.emit(
            find_large_files(self.root, self.min_bytes,
                             on_progress=lambda n: self.progress.emit(n),
                             should_cancel=lambda: self._cancel,
                             on_batch=lambda rows: self.batch.emit(rows)))


# ============================================================ 对话框
class ConfirmDialog(QDialog):
    """清理前的确认框：选删除方式（回收站 / 备份 / 永久），并列出将要动的项目。"""

    def __init__(self, names, total_bytes, has_caution, has_danger, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} · 确认清理")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        lay.addWidget(QLabel(
            f"即将清理 <b>{len(names)}</b> 项，预计释放 <b>{fmt(total_bytes)}</b>"))

        preview = "、".join(names[:8]) + ("…" if len(names) > 8 else "")
        lb = QLabel(preview)
        lb.setWordWrap(True)
        lb.setObjectName("muted")
        lay.addWidget(lb)

        pal = getattr(parent, "pal", palette("dark"))
        if has_danger:
            w = QLabel("包含「危险」级别项目，误操作可能影响系统稳定性，请确认已阅读说明。")
            w.setWordWrap(True)
            w.setStyleSheet(f"color:{pal['risk']['danger']};")
            lay.addWidget(w)
        elif has_caution:
            w = QLabel("包含「谨慎」级别项目，可能清除登录状态或需要重新下载。")
            w.setWordWrap(True)
            w.setStyleSheet(f"color:{pal['risk']['caution']};")
            lay.addWidget(w)

        grp = QGroupBox("删除方式")
        gl = QVBoxLayout(grp)
        self.rb_recycle = QRadioButton("先移到回收站（可还原，推荐）")
        self.rb_backup = QRadioButton("备份到其他盘后再删除")
        self.rb_perm = QRadioButton("直接永久删除（不可恢复）")
        self.rb_recycle.setChecked(True)
        for b in (self.rb_recycle, self.rb_backup, self.rb_perm):
            gl.addWidget(b)
        lay.addWidget(grp)

        self.backup_row = QWidget()
        br = QHBoxLayout(self.backup_row)
        br.setContentsMargins(0, 0, 0, 0)
        self.ed_backup = QLineEdit()
        drives = [d for d, _, f in sysinfo.get_drives()
                  if f > 2 * 1024 ** 3 and not d.upper().startswith(
                      rulesmod.current_drive().upper())]
        self.ed_backup.setText((drives[0] if drives else "E:\\") + "YYYCleanSweep备份")
        self.ed_backup.setEnabled(False)
        btn_browse = QPushButton("选择")
        btn_browse.setEnabled(False)
        btn_browse.clicked.connect(self._pick)
        br.addWidget(QLabel("备份位置"))
        br.addWidget(self.ed_backup, 1)
        br.addWidget(btn_browse)
        lay.addWidget(self.backup_row)
        self.rb_backup.toggled.connect(lambda on: (self.ed_backup.setEnabled(on),
                                                   btn_browse.setEnabled(on)))

        row = QHBoxLayout()
        b_cancel = QPushButton("取消")
        b_ok = QPushButton("确认执行")
        b_ok.setObjectName("primary")
        b_cancel.clicked.connect(self.reject)
        b_ok.clicked.connect(self.accept)
        row.addStretch(1)
        row.addWidget(b_cancel)
        row.addWidget(b_ok)
        lay.addLayout(row)

    def _pick(self):
        d = QFileDialog.getExistingDirectory(self, "选择备份目录")
        if d:
            self.ed_backup.setText(d)

    def result_mode(self):
        """对话框的最终结果：(删除方式, 备份目录)。"""
        if self.rb_backup.isChecked():
            return BACKUP, self.ed_backup.text().strip()
        if self.rb_perm.isChecked():
            return PERMANENT, None
        return RECYCLE, None


# ============================================================ 分类列表委托
class CategoryDelegate(QStyledItemDelegate):
    """左侧分类：主名 + 占用小字，选中态带左侧色条。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pal = palette("dark")
        self.row_h = 48          # 由主窗口按可用高度自适应调整

    def set_palette(self, p):
        """换主题配色。"""
        self.pal = p

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.row_h)

    def paint(self, painter, option, index):
        p = self.pal
        data = index.data(Qt.UserRole) or {}
        name = data.get("name", index.data(Qt.DisplayRole) or "")
        sub = data.get("sub", "")
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        r = QRectF(option.rect).adjusted(6, 3, -6, -3)
        if selected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(p["sel_bg"]))
            painter.drawRoundedRect(r, 8, 8)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(p["sel_bar"]))
            painter.drawRoundedRect(QRectF(r.x(), r.y() + 5, 3, r.height() - 10), 2, 2)
        elif hover:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(p["hover"]))
            painter.drawRoundedRect(r, 8, 8)
        painter.restore()

        painter.save()
        inner = r.height() - 12                 # 上下各留 6
        top_h = inner * 0.56
        gap = 3                                  # 两行之间的呼吸间隙
        bot_h = inner - top_h - gap

        f = QFont(option.font)
        f.setPointSize(9)
        f.setBold(selected)
        painter.setFont(f)
        painter.setPen(QColor(p["sel_fg"] if selected else p["text"]))
        tr = QRectF(r.x() + 13, r.y() + 6, r.width() - 21, top_h)
        painter.drawText(tr, Qt.AlignLeft | Qt.AlignVCenter,
                         elide_middle(name, int(tr.width()), f))

        f2 = QFont(option.font)
        f2.setPointSize(8)
        painter.setFont(f2)
        painter.setPen(QColor(p["sel_fg"] if selected else p["text_faint"]))
        br = QRectF(r.x() + 13, r.y() + 6 + top_h + gap, r.width() - 21, bot_h)
        painter.drawText(br, Qt.AlignLeft | Qt.AlignVCenter, sub)
        painter.restore()


# ============================================================ 主窗口
class MainWindow(QMainWindow):
    """主窗口：顶部工具栏 + 八个页签。

    职责边界：
    - 本窗口自己负责「项目清理 / 大文件定位 / 系统信息 / 设置与历史」四个页，
      它们是主窗口的一部分；
    - 「目录分析 / 文件去重 / 系统瘦身 / 我的标记」各自是独立模块
      （ui/dir_page.py 等），这里只负责创建、加进页签、转发主题切换。
    - 扫描与清理一律走 QThread（ScanThread / CleanThread），UI 线程不干重活。

    历史原因导致本类偏大（2000+ 行），拆分的优先级见 tools/code_audit.py
    的规模排行；目前靠 `# =====` 分区注释保持可读性。
    """

    def _apply_window_size(self):
        """初始宽高：默认走偏扁的比例（原来 1160×730 偏正方形，小尺寸
        显示器容易超出显示范围），并且不超过当前屏幕的可用区域；
        用户上次手动调过窗口大小就以记忆的宽高为准。"""
        from PySide6.QtGui import QGuiApplication
        try:
            ag = QGuiApplication.primaryScreen().availableGeometry()
            avail_w, avail_h = ag.width(), ag.height()
        except Exception:
            avail_w, avail_h = 1366, 768
        w = int(self.cfg.get("win_w") or 0)
        h = int(self.cfg.get("win_h") or 0)
        # 记忆值明显偏小（早期版本写进去的或异常值）就不采用，免得开成小窗
        if w < 900 or h < 560:
            w, h = 0, 0
        if not w or not h:
            # 默认比原先大一圈：1160×730 → 1500×880，页面不再挤成一团；
            # 屏幕装不下就按可用区域收一点
            w = min(1500, max(1120, avail_w - 40))
            h = min(880, max(660, avail_h - 40))
        self.resize(min(w, avail_w - 20), min(h, avail_h - 20))
        # 解除"窗口不得小于内容最小尺寸"的硬约束：内容区靠滚动条和列宽
        # 自适应消化，小尺寸显示器上窗口才不会超出屏幕
        try:
            lay = self.layout()
            if lay is not None:
                lay.setSizeConstraint(QLayout.SetNoConstraint)
        except Exception:
            pass

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)

        self.cfg = Config()
        self._apply_window_size()
        self.marks = MarkStore()
        self.categories, self.rules = load_rules()
        self.results = {}
        self.by_id = {r["id"]: r for r in self.rules}
        self.current_cat = self.cfg.get("category", "all")
        self.sort_col, self.sort_desc = self.cfg.get("sort_clean", [2, True])
        self.theme = self.cfg.get("theme") or DEFAULT_THEME
        self.pal = palette(self.theme)
        self.scan_thread = None
        # 扫描中断后剩余没扫的规则，「继续」时从这里接着扫
        self.scan_pending = []
        # 持久勾选集合："rid" 表示整条规则全选，"rid|路径" 表示只选某个子目录。
        # 排序 / 切分类 / 刷新列表都从这里恢复，不会被清空。
        self.checked = set()
        # 上次的选择不会在启动时自动勾选（避免误清理），
        # 只保留在配置里，通过「复用上次选择」按钮一键恢复。
        self._pending_selection = set()
        # 勾选回调的再入保护：父子勾选的级联设置会触发 itemChanged，
        # 用这个标记挡掉重入，避免把 self.checked 清乱。
        self._in_item_changed = False
        self.journal = Journal()
        # 清理前的可用空间，用于总结报告里显示"释放前后"
        self._free_before = 0

        rulesmod.set_drive(self.cfg.get("drive", "C:\\"))
        self.drive = rulesmod.current_drive() + "\\"

        # ---- 音效：波形是现场合成的，不联网也不额外打包资源
        self.sfx = Sfx(enabled=bool(self.cfg.get("sound", True)),
                       volume=float(self.cfg.get("sound_volume", 42)) / 100.0)
        self.sfx.prepare()
        self._sound_filter = SoundFilter(self.sfx, self)
        QApplication.instance().installEventFilter(self._sound_filter)

        self.check_style = BigCheckStyle()
        self.check_style.set_palette(self.pal)
        QApplication.instance().setStyle(self.check_style)

        self._build()
        self._apply_theme()
        self._build_shortcuts()
        self._update_scan_buttons(False)
        # 顶部栏高度等首次布局完成后再钉（构造时样式还没套上，量出来不准）
        QTimer.singleShot(0, self._fix_bar_height)
        QTimer.singleShot(150, lambda: self.start_scan(False))
        # 空间提醒延后一拍：等 self.drive / 调色板都就位，也避免「开屏就是黄条」
        QTimer.singleShot(400, lambda: self._check_disk_space(False))

    def _fix_bar_height(self):
        """把顶部栏的实测高度钉成下限，避免切换页签时整条栏上下跳。

        有「目标选择器」的页面（前三个）这条栏本来更高，没有的页面会矮几像素，
        一切页签下面的内容就跟着上下错位。量准确了一次就定住。
        """
        try:
            h = self.bar.height()
            if h > 0:
                self.bar.setMinimumHeight(h)
        except Exception:
            pass

    # ---------------------------------------------------------- 构建
    def _build_shortcuts(self):
        QShortcut(QKeySequence("F1"), self, activated=self._open_about)
        QShortcut(QKeySequence("F5"), self,
                  activated=lambda: self.start_scan(False))
        QShortcut(QKeySequence("Esc"), self, activated=self.stop_scan)

    def _open_about(self):
        dlg = AboutDialog(self, self)
        dlg.exec()

    # ---------------------------------------------------------- 规则编辑（G）
    def _refresh_rule_path(self):
        """设置页那行「当前生效：路径 共 N 条」。"""
        if not hasattr(self, "lb_rule_path"):
            return
        from core import ruleedit as ruleedit_mod
        try:
            p = ruleedit_mod.path_of()
        except Exception:                                       # noqa: BLE001
            p = ""
        self.lb_rule_path.setText(f"当前生效：{p}　共 {len(self.rules)} 条")
        self.lb_rule_path.setToolTip(
            f"{p}\n（这个文件在 exe 旁边，可以直接编辑；改完重启或在这里保存即生效。"
            f"发新版时打包会覆盖它，改过的内容建议用「备份当前」留一份。）")

    def _open_rule_editor(self):
        from ui.rule_editor import RuleEditorDialog
        dlg = RuleEditorDialog(self.cfg, self, theme=self.theme)
        if dlg.exec():
            self._reload_rules()

    def _reload_rules(self):
        """规则改完立刻重新加载并重扫，省得让用户重启软件。

        同时把「已勾选」「扫描结果」里属于被删规则的残留清掉 ——
        不清的话后面 _selected_plan() 会对着不存在的 id 取路径。
        """
        self.categories, self.rules = load_rules()
        self.by_id = {r["id"]: r for r in self.rules}
        self.checked = {k for k in self.checked
                        if k.split("|", 1)[0] in self.by_id}
        self.results = {k: v for k, v in self.results.items()
                        if k in self.by_id}
        self._fill_categories()
        self._refresh_categories()
        self._refresh_rule_path()
        self.start_scan(False)

    def _build(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        # 顶部栏
        bar = QFrame()
        bar.setObjectName("card")
        self.bar = bar
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(14, 9, 14, 9)
        self.lb_title = QLabel(APP_NAME)
        self.lb_title.setObjectName("title")
        bl.addWidget(self.lb_title)
        bl.addSpacing(14)

        # 目标选择器：位置统一放在标题右边，但**每个页面各自记住自己的目标**
        # （清理页记住盘、目录分析页记住目录、大文件页记住盘），互不覆盖。
        # 没有目标的页面（系统信息 / 系统瘦身 / 我的标记 / 设置）直接整块隐藏。
        self.w_target = QWidget()
        tl = QHBoxLayout(self.w_target)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(6)
        self.lb_target = QLabel("扫描盘")
        tl.addWidget(self.lb_target)
        self.cb_target = QComboBox()
        self.cb_target.setMinimumWidth(150)
        self.cb_target.setMaximumWidth(300)
        tl.addWidget(self.cb_target)
        self.btn_target = QPushButton("浏览")
        self.btn_target.setObjectName("mini")
        self.btn_target.setToolTip("直接挑一个目录当扫描目标")
        self.btn_target.clicked.connect(self._browse_target)
        tl.addWidget(self.btn_target)
        self.cb_target.currentTextChanged.connect(self._on_target_changed)
        bl.addWidget(self.w_target)

        self.pb_disk = QProgressBar()
        self.pb_disk.setFixedWidth(140)
        self.pb_disk.setTextVisible(False)
        bl.addWidget(self.pb_disk)
        self.lb_free = QLabel("剩余 —")
        self.lb_free.setObjectName("muted")
        bl.addWidget(self.lb_free)
        bl.addStretch(1)

        if not sysinfo.is_admin():
            lb_admin = QLabel("未以管理员运行")
            lb_admin.setObjectName("muted")
            lb_admin.setToolTip("部分系统级项目需要管理员权限")
            lb_admin.setStyleSheet("color:#BA7517;")
            bl.addWidget(lb_admin)
        self.btn_theme = QPushButton()
        self.btn_theme.setObjectName("toggle")
        self.btn_theme.clicked.connect(self.toggle_theme)
        bl.addWidget(self.btn_theme)
        self.btn_about = QPushButton("关于 / 帮助")
        self.btn_about.setObjectName("toggle")
        self.btn_about.setToolTip("使用说明、常见问题、更新与反馈（F1）")
        self.btn_about.clicked.connect(self._open_about)
        bl.addWidget(self.btn_about)
        outer.addWidget(bar)

        # ---- 空间不足提醒：非模态卡片，来了不打断任何操作。
        # （刻意不用 QMessageBox：构造末尾会自动起扫描，模态框会把界面按住；
        #   而且离屏冒烟测试里没人点它，进程能挂住不退出——踩过。）
        self.warn_card = QFrame()
        self.warn_card.setObjectName("warncard")
        self.warn_card.setVisible(False)      # 必须在 addWidget 之前，否则启动闪一下空条
        wl = QHBoxLayout(self.warn_card)
        wl.setContentsMargins(14, 8, 14, 8)
        wl.setSpacing(10)
        self.lb_warn = QLabel("")
        self.lb_warn.setObjectName("warn")
        wl.addWidget(self.lb_warn, 1)
        self.btn_warn_clean = QPushButton("去清理")
        self.btn_warn_clean.setObjectName("warn")
        self.btn_warn_clean.clicked.connect(self._goto_clean_tab)
        wl.addWidget(self.btn_warn_clean)
        self.btn_warn_ok = QPushButton("知道了")
        self.btn_warn_ok.setObjectName("mini")
        self.btn_warn_ok.clicked.connect(self._dismiss_warn_card)
        wl.addWidget(self.btn_warn_ok)
        outer.addWidget(self.warn_card)

        # 页签
        self.tabs = QTabWidget()
        self.tab_clean = self._build_clean_tab()
        self.tabs.addTab(self.tab_clean, "项目清理")
        self.dir_page = DirScanPage(self.marks, self.cfg, self)
        self.marks_page = MarksPage(self.marks, self)
        self.dup_page = DupPage(self.cfg, self.marks, self.journal, self.sfx, self)
        self.tab_big = self._build_bigfile_tab()
        # 第 2 / 第 3 页的位置：大文件定位 在前，目录分析 在后
        self.tabs.addTab(self.tab_big, "大文件定位")
        self.tabs.addTab(self.dir_page, "目录分析")
        self.tabs.addTab(self.dup_page, "文件去重")
        self.dupdir_page = DupDirPage(self.cfg, self)
        self.tabs.addTab(self.dupdir_page, "重复文件夹")
        self.tabs.addTab(self._build_sys_tab(), "系统信息")
        self.sysclean_page = SysCleanPage(self.cfg, self)
        self.tabs.addTab(self.sysclean_page, "系统瘦身")
        self.tabs.addTab(self.marks_page, "我的标记")
        self.tabs.addTab(self._build_settings_tab(), "设置与历史")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        outer.addWidget(self.tabs, 1)

        # 哪些页有「目标」：页面 -> (配置键, 类型, 标签)
        # 类型 driver = 只能选盘；dir = 盘或目录（可浏览）。配置键各页独立，
        # 所以切页时顶部只换显示，不会把你另一页选好的目标改掉。
        self.target_of = {
            self.tab_clean: ("target_clean", "drive", "扫描盘"),
            self.dir_page: ("target_dir", "dir", "扫描位置"),
            # 大文件定位：底层 find_large_files 本来就吃任意目录，这里必须是
            # dir 才会显示「浏览」按钮、且选中的目录不被截成盘符
            self.tab_big: ("target_big", "dir", "扫描位置"),
            self.dupdir_page: ("target_dupdir", "dir", "扫描位置"),
        }
        self._sync_target()

        self.setCentralWidget(root)

        # 页面全部建好之后再按当前主题刷新一遍：目录分析 / 文件去重 / 系统瘦身
        # 这些页面是在首次应用主题之后才创建的，它们内部的自绘控件（圆环、条纹、
        # 状态条、行委托）默认拿到的是暗色调色板 —— 亮色主题启动时就会出现
        # 「框架是亮的、列表里还是暗块」的错乱。
        self._apply_theme()
        self.setAcceptDrops(True)

    # ---------------------------------------------------------- 拖拽目录分析（方向7）
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        """拖动过程中也要持续接受：只重写 enter/drop 的话，个别环境下
        光标会显示禁止、dropEvent 干脆不触发。"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        """把文件夹（或文件）拖进窗口：自动跳到目录分析并开始扫描。"""
        urls = event.mimeData().urls()
        if not urls:
            return
        path = urls[0].toLocalFile()
        if not path:
            return
        if os.path.isfile(path):
            path = os.path.dirname(path)
        if not os.path.isdir(path):
            QMessageBox.information(self, "拖拽分析", f"不是有效目录：{path}")
            return
        self.dir_page.set_root(path)
        self.tabs.setCurrentWidget(self.dir_page)
        QTimer.singleShot(0, self.dir_page.start_scan)
        event.acceptProposedAction()

    # ---------------------------------------------------------- 目标（盘 / 目录）
    def _target_of(self, w=None):
        return (self.target_of or {}).get(w or self.tabs.currentWidget())

    def _sync_target(self):
        """切页时把顶部目标换成该页自己记住的那个。"""
        info = self._target_of()
        if not info:
            self.w_target.setVisible(False)
            self.pb_disk.setVisible(False)
            self.lb_free.setVisible(False)
            return
        key, kind, label = info
        self.w_target.setVisible(True)
        self.pb_disk.setVisible(True)
        self.lb_free.setVisible(True)
        self.lb_target.setText(label)
        self.btn_target.setVisible(kind == "dir")
        cur = self.cfg.get(key) or self.drive
        self.cb_target.blockSignals(True)
        self.cb_target.clear()
        self.cb_target.setEditable(kind == "dir")
        for letter, _t, _f in sysinfo.get_drives():
            self.cb_target.addItem(letter)
        if kind == "dir":
            for p in dir_page_roots():
                self.cb_target.addItem(p)
            self.cb_target.setEditText(cur)
        else:
            i = self.cb_target.findText(cur)
            self.cb_target.setCurrentIndex(max(0, i))
            cur = self.cb_target.currentText()
        self.cb_target.blockSignals(False)
        if (self.tabs.currentWidget() is self.tab_clean
                and kind == "drive" and cur):
            # 清理页的目标就是 self.drive，两者必须始终一致
            rulesmod.set_drive(cur)
            self.drive = rulesmod.current_drive() + "\\"
        self._refresh_disk(cur)

    def _on_target_changed(self, text):
        info = self._target_of()
        text = (text or "").strip()
        if not info or not text:
            return
        key, kind, _label = info
        if kind == "dir":
            text = os.path.normpath(os.path.expandvars(text))
        else:
            text = text[:2].upper() if len(text) >= 2 else text
            if not text.endswith("\\"):
                text += "\\"
        self.cfg.set(key, text)
        w = self.tabs.currentWidget()
        if w is self.tab_clean:
            rulesmod.set_drive(text)
            self.drive = rulesmod.current_drive() + "\\"
            self.cfg.set("drive", self.drive)
            self.start_scan()
        elif w is self.dir_page:
            self.dir_page.set_root(text)
        elif w is self.tab_big:
            pass                      # 值已存进配置，点「开始扫描」时读取
        elif w is self.dupdir_page:
            self.dupdir_page.set_root(text)
        self._refresh_disk(text)

    def _browse_target(self):
        info = self._target_of()
        if not info:
            return
        d = QFileDialog.getExistingDirectory(
            self, "选择扫描目标", self.cb_target.currentText() or self.drive)
        if not d:
            return
        self.cb_target.setEditText(d)
        self._on_target_changed(d)

    @staticmethod
    def _pane_title(text):
        """三栏标题统一样式：同字号、同字重、同颜色（QSS 里的 #h2）。"""
        lb = QLabel(text)
        lb.setObjectName("h2")
        return lb

    def _clean_sort_key(self, col, item):
        """清理项目页的排序 key。返回 None 就交给默认规则（数值 / 文本）。"""
        if item is None:
            return None
        if col == 1:                      # 风险列：按「越该动手越靠前」
            # 权重在建行时已存进 UserRole（白名单项与子项没有风险文本，只能靠它），
            # 取不到才回退到按显示文本查表。
            w = item.data(1, Qt.UserRole)
            if isinstance(w, int):
                return w
            return CLEAN_RISK_RANK.get(item.text(1), 9)
        if col == 3:                      # 推荐列：分越高越靠前
            # 必须用「正分」：HeaderSorter 对 0 列之外默认 desc=True，正分配合
            # reverse=desc 即高分在前，与 _visible_rules 的排序方向一致。
            # （曾写成负分，点表头后变成最低分排最前，和刷新时的顺序正好相反。）
            return item.data(3, Qt.UserRole) or 0
        return None

    def _recommend_score(self, r, size, newest):
        """智能推荐分（0-100）：占用 × 风险 × 最近修改，归一化后加权。

        占用越大、风险越高（可清理 / 建议迁移）、越久没改（缓存 / 临时通常旧）
        越值得清理；白名单锁定、建议保留 / 禁止删除的项一律 0 分，绝不推荐。
        """
        if self._rule_protected(r) or size <= 0:
            return 0
        risk = r.get("risk", "safe")
        risk_w = {"safe": 1.0, "migrate": 0.8, "caution": 0.5,
                  "keep": 0.0, "danger": 0.0}.get(risk, 0.5)
        if risk_w == 0:
            return 0
        size_n = min(math.log10(size + 1) / 11.0, 1.0)   # 10^11 B ≈ 100GB 满格
        now = time.time()
        if newest:
            days = (now - newest) / 86400.0
            rec_n = min(max(days / 365.0, 0.0), 1.0)
        else:
            rec_n = 0.4
        return int((0.5 * size_n + 0.3 * risk_w + 0.2 * rec_n) * 100)

    def _build_clean_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(10)

        self.tip = QFrame()
        self.tip.setObjectName("card")
        tl = QHBoxLayout(self.tip)
        tl.setContentsMargins(14, 8, 14, 8)
        self.lb_tip = QLabel()
        self.lb_tip.setWordWrap(True)
        tl.addWidget(self.lb_tip)
        lay.addWidget(self.tip)
        self.tip.hide()

        split = QSplitter(Qt.Horizontal)

        # 三栏（分类 / 项目 / 选中项详情）的标题统一走 _pane_title：
        # 同一个字号字重、同一套内边距，三栏对齐看起来才是一套
        side = QFrame()
        side.setObjectName("sidebar")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(12, 10, 12, 10)
        sl.setSpacing(8)
        sl.addWidget(self._pane_title("分类"))
        self.cat_list = QListWidget()
        self.cat_list.setObjectName("nav")        # 供音效过滤识别为"导航级"
        self.cat_list.setFrameShape(QFrame.Shape.NoFrame)
        self.cat_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.cat_delegate = CategoryDelegate()
        self.cat_list.setItemDelegate(self.cat_delegate)
        enable_hover(self.cat_list)
        self._fill_categories()
        self.cat_list.currentItemChanged.connect(self._on_cat_changed)
        sl.addWidget(self.cat_list, 1)
        side.setFixedWidth(186)
        split.addWidget(side)

        mid = QFrame()
        mid.setObjectName("midpane")
        ml = QVBoxLayout(mid)
        ml.setContentsMargins(0, 10, 0, 10)
        ml.setSpacing(8)
        trow = QWidget()
        trl = QHBoxLayout(trow)
        trl.setContentsMargins(12, 0, 12, 0)
        trl.addWidget(self._pane_title("项目清单"))
        trl.addStretch(1)
        self.lb_mid_hint = QLabel("点表头可排序")
        self.lb_mid_hint.setObjectName("hint")
        trl.addWidget(self.lb_mid_hint)
        ml.addWidget(trow)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["项目", "风险", "占用", "推荐"])
        # 必须常开：关掉的话顶层（也就是所有规则）都不会有展开三角，
        # 一条规则对应多个目录时用户根本看不出还能展开
        setup_tree(self.tree)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # 统一行样式：顶层加粗放大 + 占用列画占比条
        # bar_col 必须指向「占用」列（下标 2）：归一化基准 bar_max 是最大占用字节，
        # 指到「推荐」列会把 0~100 的分数除以数亿字节、画成一根 3px 残根。
        self.tree_delegate = RowDelegate(self.tree, elide_col=0,
                                         bar_col=2, level_bold=True)
        self.tree.setItemDelegate(self.tree_delegate)
        enable_hover(self.tree)
        self.tree.header().setMinimumSectionSize(70)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Interactive)  # 项目(文件名)列：可手动调宽看全
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.Stretch)     # 末列吸收剩余宽度
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.tree.header().resizeSection(0, 420)
        # 拉宽 / 缩窄窗口时第一列（项目名）跟着伸缩，风险列与占用列有各自最小宽
        FirstColFit(self.tree, min_w=280, others={1: 96, 2: 124, 3: 80})
        # 点表头排序：占用按真实字节、风险按「越该动手越靠前」
        self.tree_sorter = HeaderSorter(
            self.tree, key_fn=self._clean_sort_key,
            default_col=self.sort_col, default_desc=self.sort_desc,
            asc_first=(0,), on_sorted=self._on_sort_changed)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.currentItemChanged.connect(self._on_select)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        ml.addWidget(self.tree, 1)
        split.addWidget(mid)

        detail = QFrame()
        detail.setObjectName("detail")
        dl = QVBoxLayout(detail)
        dl.setContentsMargins(12, 10, 12, 10)
        dl.setSpacing(8)
        head = QHBoxLayout()
        self.lb_d_title = self._pane_title("选中项详情")
        head.addWidget(self.lb_d_title)
        head.addStretch(1)
        dl.addLayout(head)
        self.tx_detail = QTextBrowser()
        self.tx_detail.setOpenExternalLinks(False)
        dl.addWidget(self.tx_detail, 1)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_preview = QPushButton("预览内容")
        self.btn_clean_one = QPushButton("清理此项")
        self.btn_clean_one.setObjectName("warn")
        self.btn_preview.clicked.connect(self._preview_one)
        self.btn_clean_one.clicked.connect(self._clean_one)
        row.addWidget(self.btn_preview)
        row.addWidget(self.btn_clean_one)
        dl.addLayout(row)
        detail.setFixedWidth(310)
        split.addWidget(detail)

        split.setStretchFactor(1, 3)
        split.setSizes([176, 640, 310])
        lay.addWidget(split, 1)

        bottom = QFrame()
        bottom.setObjectName("card")
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(14, 10, 14, 10)
        bl.setSpacing(8)
        self.lb_sel = QLabel("已选 0 项 · 预计释放 0 B")
        bl.addWidget(self.lb_sel)
        bl.addStretch(1)

        # 选择组
        b_safe = QPushButton("只选安全项")
        b_safe.setObjectName("ok")
        b_reco = QPushButton("只选推荐项")
        b_reco.setObjectName("ok")
        b_last = QPushButton("复用上次选择")
        b_none = QPushButton("全不选")
        b_script = QPushButton("导出清理脚本")
        b_script.setToolTip(
            "把当前勾选的项目导出成一个 .bat，可以拿去管理员运行或放进计划任务")
        b_script.clicked.connect(self._export_script)
        self.btn_expand = QPushButton("展开子目录")
        self.btn_expand.setObjectName("toggle")
        self.btn_expand.setCheckable(True)
        self.btn_expand.setToolTip("一条规则对应多个目录时，展开后能只勾选其中一部分")
        self.btn_expand.toggled.connect(self._toggle_expand)
        for b in (b_safe, b_reco, b_last, b_none, b_script, self.btn_expand):
            bl.addWidget(b)
        bl.addSpacing(16)

        # 扫描组：开始 / 停止 / 继续
        self.btn_scan = QPushButton("开始扫描")
        self.btn_scan.setObjectName("primary")
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("danger")
        self.btn_resume = QPushButton("继续扫描")
        self.btn_resume.setObjectName("warn")
        for b in (self.btn_scan, self.btn_stop, self.btn_resume):
            bl.addWidget(b)
        # 空闲时只显示「开始扫描」，另外两个在进入对应状态时再出现
        self.btn_stop.setVisible(False)
        self.btn_resume.setVisible(False)
        bl.addSpacing(16)

        self.btn_clean = QPushButton("开始清理")
        self.btn_clean.setObjectName("primary")
        bl.addWidget(self.btn_clean)

        b_safe.clicked.connect(self._select_safe)
        b_reco.clicked.connect(self._select_recommend)
        b_last.clicked.connect(self._reselect_last)
        b_none.clicked.connect(self._select_none)
        self.btn_scan.clicked.connect(lambda: self.start_scan(False))
        self.btn_stop.clicked.connect(self.stop_scan)
        self.btn_resume.clicked.connect(lambda: self.start_scan(True))
        self.btn_clean.clicked.connect(self._clean_selected)
        lay.addWidget(bottom)

        # 扫描 / 清理时的状态条：与目录分析、大文件、重复文件页共用同一个组件，
        # 也放在同一个位置（底部操作条下面），四个页面表现完全一致
        self.scan_banner = ScanBanner(self)
        lay.addWidget(self.scan_banner)
        # 删除时单独用红色"删除中"状态条，和扫描条在视觉上区分开
        self.del_banner = DeleteBanner(self, "正在删除")
        lay.addWidget(self.del_banner)
        return w

    def _toggle_expand(self, on):
        self.btn_expand.setText("折叠子目录" if on else "展开子目录")
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.childCount():
                it.setExpanded(on)

    def _fill_categories(self):
        self.cat_list.blockSignals(True)
        self.cat_list.clear()
        items = [("all", "全部项目", 0)]
        for c in self.categories:
            n = sum(1 for r in self.rules if r["category"] == c["id"])
            items.append((c["id"], c["name"], n))
        if self.current_cat not in [x[0] for x in items]:
            self.current_cat = "all"
        cur_idx = 0
        for i, (cid, name, n) in enumerate(items):
            it = QListWidgetItem(name)
            it.setData(Qt.UserRole, {"id": cid, "name": name, "sub": f"{n} 项"})
            it.setSizeHint(QSize(160, self.cat_delegate.row_h))
            self.cat_list.addItem(it)
            if cid == self.current_cat:
                cur_idx = i
        self.cat_list.setCurrentRow(cur_idx)
        self.cat_list.blockSignals(False)
        self._fit_categories()

    def _fit_categories(self):
        """让分类项的高度随可用高度自适应，把整块高度铺满，不留大片空白。"""
        n = self.cat_list.count()
        if not n:
            return
        avail = self.cat_list.viewport().height()
        if avail <= 0:
            return
        # 留 4px 余量，避免刚好超出一点导致出现滚动条
        h = max(46, min(74, (avail - 4) // n))
        if h == self.cat_delegate.row_h:
            return
        self.cat_delegate.row_h = h
        for i in range(n):
            self.cat_list.item(i).setSizeHint(QSize(160, h))
        self.cat_list.viewport().update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "cat_list"):
            self._fit_categories()

    def _build_bigfile_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 0)
        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(QLabel("只报告不删除。扫描范围由顶部「扫描位置」决定，"
                             "点「浏览」可挑任意目录，比整盘快得多。"))
        top.addStretch(1)
        top.addWidget(QLabel("大于"))
        self.sp_big = QSpinBox()
        self.sp_big.setRange(10, 100000)
        self.sp_big.setSuffix(" MB")
        self.sp_big.setSingleStep(100)
        # 说明文案变长后这个微调框会被挤扁，给个最小宽度兜住
        self.sp_big.setMinimumWidth(104)
        self.sp_big.setValue(int(self.cfg.get("big_min_mb", 500)))
        self.sp_big.valueChanged.connect(
            lambda v: self.cfg.set("big_min_mb", v))
        top.addWidget(self.sp_big)
        self.btn_big = QPushButton("开始扫描")
        self.btn_big.setObjectName("primary")
        self.btn_big.clicked.connect(self._scan_big)
        top.addWidget(self.btn_big)
        lay.addLayout(top)

        split = QSplitter(Qt.Horizontal)

        # 同样换成带「路径高亮」的树：分组树展开几层后，
        # 当前这一支的层级线会变成强调色，不容易看岔到别的分支上去
        self.tw_big = PathTreeWidget()
        # 列从 5 减到 4：最近修改改成悬浮提示，避免右侧被挤爆
        self.tw_big.setHeaderLabels(["名称 / 文件", "大小", "占比", "类型"])
        self.tw_big.setAlternatingRowColors(True)
        setup_tree(self.tw_big)
        self.tw_big.header().setMinimumSectionSize(70)
        self.tw_big.header().setSectionResizeMode(0, QHeaderView.Interactive)  # 名称/文件列：可手动调宽看全
        for c in (1, 2):
            self.tw_big.header().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.tw_big.header().setSectionResizeMode(3, QHeaderView.Stretch)      # 末列吸收剩余宽度
        self.tw_big.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.tw_big.header().resizeSection(0, 420)
        # 同上：多出来的宽度给第一列，其余列保底不被挤扁
        FirstColFit(self.tw_big, min_w=280,
                    others={1: 104, 2: 92, 3: 116})
        self.tw_big.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.big_delegate = RowDelegate(self.tw_big, elide_col=0, bar_col=1,
                                        level_bold=True)
        self.tw_big.setItemDelegate(self.big_delegate)
        enable_hover(self.tw_big)
        # 点表头排序：大小按真实字节、占比按百分比数值、名称/类型按文本
        self.big_sorter = HeaderSorter(self.tw_big, numeric_cols=(1, 2),
                                       asc_first=(0, 3))
        self.tw_big.itemDoubleClicked.connect(self._big_open)
        self.tw_big.currentItemChanged.connect(self._big_select)
        self.tw_big.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tw_big.customContextMenuRequested.connect(self._big_menu)
        split.addWidget(self.tw_big)

        side = QFrame()
        side.setObjectName("detail")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(12, 12, 12, 12)
        sl.setSpacing(10)
        self.lb_big_title = QLabel("大文件详情")
        self.lb_big_title.setObjectName("title")
        sl.addWidget(self.lb_big_title)
        self.lb_big_path = PathLabel("—")
        self.lb_big_path.setObjectName("hint")
        sl.addWidget(self.lb_big_path)
        top = QHBoxLayout()
        top.setSpacing(12)
        self.big_donut = Donut(side, size=112)
        top.addWidget(self.big_donut)
        gl = QGridLayout()
        gl.setSpacing(6)
        self.lb_big_size = QLabel("—")
        self.lb_big_cnt = QLabel("—")
        self.lb_big_mt = QLabel("—")
        for i, (cap, wid) in enumerate((("大小", self.lb_big_size),
                                        ("文件数", self.lb_big_cnt),
                                        ("最近修改", self.lb_big_mt))):
            lb = QLabel(cap)
            lb.setObjectName("hint")
            wid.setObjectName("muted")
            gl.addWidget(lb, i, 0)
            gl.addWidget(wid, i, 1)
        top.addLayout(gl, 1)
        sl.addLayout(top)
        t = QLabel("类型构成")
        t.setObjectName("h2")
        sl.addWidget(t)
        self.big_bars = TypeBars(max_rows=8)
        sl.addWidget(self.big_bars)
        b_open = QPushButton("打开所在位置")
        b_open.clicked.connect(lambda: self._big_open(
            self.tw_big.currentItem(), 0))
        sl.addWidget(b_open)
        sl.addStretch(1)
        side.setFixedWidth(340)
        split.addWidget(side)
        split.setSizes([740, 340])
        lay.addWidget(split, 1)

        self.lb_big = QLabel("尚未扫描")
        self.lb_big.setObjectName("muted")
        lay.addWidget(self.lb_big)
        # 与清理页 / 目录分析 / 重复文件页同一个扫描状态条、同一个位置（底部）
        self.big_banner = ScanBanner(self)
        lay.addWidget(self.big_banner)
        return w

    def _build_sys_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 0)
        self.tx_sys = QTextBrowser()
        lay.addWidget(self.tx_sys, 1)
        row = QHBoxLayout()
        b1 = QPushButton("打开虚拟内存设置")
        b2 = QPushButton("打开存储感知设置")
        b3 = QPushButton("刷新")
        b1.clicked.connect(lambda: self._run("SystemPropertiesPerformance.exe"))
        b2.clicked.connect(lambda: self._run("ms-settings:storagesense"))
        b3.clicked.connect(self._fill_sys)
        for b in (b1, b2, b3):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        return w

    def _build_settings_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(10)

        box = QFrame()
        box.setObjectName("card")
        bl = QHBoxLayout(box)
        bl.setContentsMargins(14, 10, 14, 10)
        bl.addWidget(QLabel("主题"))
        self.cb_theme = QComboBox()
        self.cb_theme.addItems(["暗色", "亮色"])
        self.cb_theme.setCurrentIndex(0 if self.theme == "dark" else 1)
        self.cb_theme.currentIndexChanged.connect(self._on_theme_combo)
        bl.addWidget(self.cb_theme)
        bl.addSpacing(20)
        bl.addWidget(QLabel("默认扫描盘"))
        self.cb_def_drive = QComboBox()
        for letter, _t, _f in sysinfo.get_drives():
            self.cb_def_drive.addItem(letter)
        i = self.cb_def_drive.findText(self.drive)
        self.cb_def_drive.setCurrentIndex(max(0, i))
        self.cb_def_drive.currentTextChanged.connect(
            lambda t: self.cfg.set("drive", t))
        bl.addWidget(self.cb_def_drive)
        bl.addStretch(1)
        b_exp = QPushButton("导出配置")
        b_imp = QPushButton("导入配置")
        b_exp.clicked.connect(self._export_cfg)
        b_imp.clicked.connect(self._import_cfg)
        bl.addWidget(b_exp)
        bl.addWidget(b_imp)
        lay.addWidget(box)

        # ---- 音效
        sb = QFrame()
        sb.setObjectName("card")
        sbl = QHBoxLayout(sb)
        sbl.setContentsMargins(14, 10, 14, 10)
        sbl.setSpacing(10)
        self.chk_sound = QCheckBox("界面音效")
        self.chk_sound.setChecked(bool(self.cfg.get("sound", True)))
        self.chk_sound.setToolTip("按钮点击、勾选、扫描完成、清理完成都会有短促提示音")
        self.chk_sound.stateChanged.connect(self._on_sound_changed)
        sbl.addWidget(self.chk_sound)
        sbl.addWidget(QLabel("音量"))
        self.sl_vol = QSlider(Qt.Horizontal)
        self.sl_vol.setRange(0, 100)
        self.sl_vol.setFixedWidth(150)
        self.sl_vol.setValue(int(self.cfg.get("sound_volume", 42)))
        self.sl_vol.valueChanged.connect(self._on_volume_changed)
        sbl.addWidget(self.sl_vol)
        self.lb_vol = QLabel(f"{self.sl_vol.value()}%")
        self.lb_vol.setObjectName("muted")
        sbl.addWidget(self.lb_vol)
        b_test = QPushButton("试听")
        b_test.setObjectName("mini")
        b_test.clicked.connect(lambda: self.sfx.play("clean_done"))
        sbl.addWidget(b_test)
        sbl.addStretch(1)
        lb_snd = QLabel("声音由程序现场合成，不联网、不额外占用体积；"
                        "不想要就取消勾选。")
        lb_snd.setObjectName("hint")
        sbl.addWidget(lb_snd)
        if not self.sfx.supported():
            self.chk_sound.setEnabled(False)
            lb_snd.setText("当前环境不支持播放音效（需要 Windows）。")
        lay.addWidget(sb)

        # ---- 扫描缓存：占用写成可读容量（附精确字节），并把库文件路径摆出来，
        # 让人知道自己机器上到底多了个什么东西、在哪儿
        cb = QFrame()
        cb.setObjectName("card")
        cbl = QVBoxLayout(cb)
        cbl.setContentsMargins(14, 10, 14, 10)
        cbl.setSpacing(6)
        r1 = QHBoxLayout()
        r1.setSpacing(10)
        r1.addWidget(QLabel("扫描缓存"))
        self.lb_cache = QLabel("")
        self.lb_cache.setObjectName("hint")
        r1.addWidget(self.lb_cache)
        r1.addStretch(1)
        b_open_cache = QPushButton("打开位置")
        b_open_cache.setToolTip("在资源管理器里选中这个缓存文件")
        b_open_cache.clicked.connect(lambda: self._reveal_file(
            scan_cache.db_file(), "扫描缓存"))
        b_clr_cache = QPushButton("清除缓存")
        b_clr_cache.setObjectName("danger")
        b_clr_cache.clicked.connect(self._clear_cache)
        r1.addWidget(b_open_cache)
        r1.addWidget(b_clr_cache)
        cbl.addLayout(r1)
        self.lb_cache_path = PathLabel("")
        self.lb_cache_path.setObjectName("hint")
        cbl.addWidget(self.lb_cache_path)
        lay.addWidget(cb)

        # ---- 缩略图缓存（去重页的文件预览用；可一键清除 + 调上限）
        cb2 = QFrame()
        cb2.setObjectName("card")
        cbl2 = QVBoxLayout(cb2)
        cbl2.setContentsMargins(14, 10, 14, 10)
        cbl2.setSpacing(6)
        r2 = QHBoxLayout()
        r2.setSpacing(10)
        r2.addWidget(QLabel("缩略图缓存"))
        self.lb_thumb_cache = QLabel("")
        self.lb_thumb_cache.setObjectName("hint")
        r2.addWidget(self.lb_thumb_cache)
        r2.addStretch(1)
        r2.addWidget(QLabel("上限"))
        self.sp_thumb_max = QSpinBox()
        self.sp_thumb_max.setRange(200, 20000)
        self.sp_thumb_max.setSingleStep(500)
        self.sp_thumb_max.setSuffix(" 张")
        self.sp_thumb_max.setMinimumWidth(116)
        # 先按配置填好值再接信号，免得启动时就把配置写一遍
        self.sp_thumb_max.setValue(
            int(self.cfg.get("thumb_max_items", 2000) or 2000))
        thumb_mod.set_limits(max_items=int(self.sp_thumb_max.value()))
        self.sp_thumb_max.valueChanged.connect(self._on_thumb_max_changed)
        r2.addWidget(self.sp_thumb_max)
        b_open_thumb = QPushButton("打开位置")
        b_open_thumb.setToolTip("在资源管理器里选中这个缓存文件")
        b_open_thumb.clicked.connect(lambda: self._reveal_file(
            thumb_mod.db_file(), "缩略图缓存"))
        b_clr_thumb = QPushButton("清除缓存")
        b_clr_thumb.setObjectName("danger")
        b_clr_thumb.clicked.connect(self._clear_thumb_cache)
        r2.addWidget(b_open_thumb)
        r2.addWidget(b_clr_thumb)
        cbl2.addLayout(r2)
        self.lb_thumb_path = PathLabel("")
        self.lb_thumb_path.setObjectName("hint")
        cbl2.addWidget(self.lb_thumb_path)
        lay.addWidget(cb2)

        # ---- 空间快照（B）：留了几次、占多少，以及清掉重来
        cb3 = QFrame()
        cb3.setObjectName("card")
        cbl3 = QVBoxLayout(cb3)
        cbl3.setContentsMargins(14, 10, 14, 10)
        cbl3.setSpacing(6)
        r3 = QHBoxLayout()
        r3.setSpacing(10)
        r3.addWidget(QLabel("空间快照"))
        self.lb_snap_cache = QLabel("")
        self.lb_snap_cache.setObjectName("hint")
        r3.addWidget(self.lb_snap_cache)
        r3.addStretch(1)
        r3.addWidget(QLabel("保留"))
        self.sp_snap_keep = QSpinBox()
        self.sp_snap_keep.setRange(5, 200)
        self.sp_snap_keep.setSingleStep(5)
        self.sp_snap_keep.setSuffix(" 次")
        self.sp_snap_keep.setMinimumWidth(110)
        # 先按配置填值再接信号，免得启动时就把配置写一遍
        self.sp_snap_keep.setValue(int(self.cfg.get("snap_keep", 30) or 30))
        scan_cache.set_snapshot_keep(int(self.sp_snap_keep.value()))
        self.sp_snap_keep.valueChanged.connect(self._on_snap_keep_changed)
        r3.addWidget(self.sp_snap_keep)
        b_clr_snap = QPushButton("清除快照")
        b_clr_snap.setObjectName("danger")
        b_clr_snap.clicked.connect(self._clear_snapshots)
        r3.addWidget(b_clr_snap)
        cbl3.addLayout(r3)
        self.lb_snap_path = PathLabel("")
        self.lb_snap_path.setObjectName("hint")
        cbl3.addWidget(self.lb_snap_path)
        lay.addWidget(cb3)

        # ---- 磁盘空间提醒（F）：只在启动时查一次，不做后台常驻
        cb4 = QFrame()
        cb4.setObjectName("card")
        cbl4 = QVBoxLayout(cb4)
        cbl4.setContentsMargins(14, 10, 14, 10)
        cbl4.setSpacing(6)
        r4 = QHBoxLayout()
        r4.setSpacing(10)
        r4.addWidget(QLabel("磁盘空间提醒"))
        self.ck_warn = QCheckBox("可用空间不足时提醒")
        r4.addWidget(self.ck_warn)
        r4.addStretch(1)
        self.lb_warn_tip = QLabel("")
        self.lb_warn_tip.setObjectName("hint")
        r4.addWidget(self.lb_warn_tip)
        cbl4.addLayout(r4)

        r4b = QHBoxLayout()
        r4b.setSpacing(6)
        r4b.addWidget(QLabel("低于"))
        self.sp_warn_gb = QSpinBox()
        self.sp_warn_gb.setRange(1, 100)
        self.sp_warn_gb.setSuffix(" GB")
        self.sp_warn_gb.setMinimumWidth(96)
        r4b.addWidget(self.sp_warn_gb)
        r4b.addWidget(QLabel("或"))
        self.sp_warn_pct = QSpinBox()
        self.sp_warn_pct.setRange(1, 50)
        self.sp_warn_pct.setSuffix(" %")
        self.sp_warn_pct.setMinimumWidth(84)
        r4b.addWidget(self.sp_warn_pct)
        r4b.addWidget(QLabel("时提醒"))
        r4b.addSpacing(14)
        r4b.addWidget(QLabel("提醒频率"))
        self.cb_warn_freq = QComboBox()
        self.cb_warn_freq.addItems(["每天一次", "每次启动"])
        self.cb_warn_freq.setMinimumWidth(110)
        r4b.addWidget(self.cb_warn_freq)
        r4b.addStretch(1)
        b_warn_check = QPushButton("立即检查")
        b_warn_check.clicked.connect(lambda: self._check_disk_space(True))
        r4b.addWidget(b_warn_check)
        cbl4.addLayout(r4b)
        self.lb_warn_hint = QLabel(
            "只在启动程序时查一次，不常驻后台。想定期检查，可用命令行模式配 Windows 计划任务。")
        self.lb_warn_hint.setObjectName("hint")
        cbl4.addWidget(self.lb_warn_hint)
        lay.addWidget(cb4)

        # 先按配置填好值再接信号，免得启动时就把配置写一遍
        self.ck_warn.setChecked(bool(self.cfg.get("warn_enabled", True)))
        self.sp_warn_gb.setValue(int(self.cfg.get("warn_gb", 5) or 5))
        self.sp_warn_pct.setValue(int(self.cfg.get("warn_pct", 3) or 3))
        self.cb_warn_freq.setCurrentIndex(
            1 if str(self.cfg.get("warn_freq", "daily")) == "always" else 0)
        self.ck_warn.toggled.connect(self._on_warn_enabled)
        self.sp_warn_gb.valueChanged.connect(
            lambda v: self.cfg.set("warn_gb", int(v)))
        self.sp_warn_pct.valueChanged.connect(
            lambda v: self.cfg.set("warn_pct", int(v)))
        self.cb_warn_freq.currentIndexChanged.connect(self._on_warn_freq)
        self._sync_warn_tip()

        # ---- 清理规则（改的是 exe 旁那份可编辑副本，保存前自动备份）
        cb5 = QFrame()
        cb5.setObjectName("card")
        cbl5 = QHBoxLayout(cb5)
        cbl5.setContentsMargins(14, 10, 14, 10)
        cbl5.setSpacing(10)
        cbl5.addWidget(QLabel("清理规则"))
        self.lb_rule_path = QLabel("")
        self.lb_rule_path.setObjectName("hint")
        cbl5.addWidget(self.lb_rule_path, 1)
        b_edit_rule = QPushButton("编辑规则")
        b_edit_rule.setToolTip("增删改清理规则，不用手改 JSON")
        b_edit_rule.clicked.connect(self._open_rule_editor)
        cbl5.addWidget(b_edit_rule)
        lay.addWidget(cb5)
        self._refresh_rule_path()

        gb = QGroupBox("历史操作（可一键复用当时的选择）")
        gl = QVBoxLayout(gb)
        gl.setSpacing(10)
        self.tbl_hist = QTableWidget(0, 5)
        self.tbl_hist.setHorizontalHeaderLabels(
            ["时间", "项目", "释放", "方式", ""])
        hhist = self.tbl_hist.horizontalHeader()
        hhist.setMinimumSectionSize(70)
        hhist.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hhist.setSectionResizeMode(1, QHeaderView.Interactive)  # 项目(名称)列：可手动调宽看全
        hhist.setSectionResizeMode(4, QHeaderView.Stretch)       # 末列(空)吸收剩余宽度
        self.tbl_hist.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.tbl_hist.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_hist.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.hist_delegate = RowDelegate(self.tbl_hist, elide_col=1)
        self.tbl_hist.setItemDelegate(self.hist_delegate)
        enable_hover(self.tbl_hist)
        self.hist_sorter = HeaderSorter(self.tbl_hist, key_fn=self._hist_sort_key,
                                        numeric_cols=(2,), asc_first=(0, 1))
        gl.addWidget(self.tbl_hist)
        gl.addSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(8)
        b_re = QPushButton("复用选中记录的选择")
        b_re.setObjectName("primary")
        b_del = QPushButton("清空历史")
        b_del.setObjectName("danger")
        b_re.clicked.connect(self._reuse_history)
        b_del.clicked.connect(self._clear_history)
        row.addWidget(b_re)
        row.addWidget(b_del)
        row.addStretch(1)
        gl.addLayout(row)
        lay.addWidget(gb, 1)

        gb2 = QGroupBox("清理日志")
        g2 = QVBoxLayout(gb2)
        g2.setSpacing(10)
        self.lb_log = QLabel(
            "每次清理动过的<b>具体路径</b>都会追加写入独立日志文件，"
            "误删后可据此回溯（备份模式还会记录文件被挪到了哪里）。\n"
            f"{esc(log_path())}")
        self.lb_log.setObjectName("hint")
        self.lb_log.setWordWrap(True)
        g2.addWidget(self.lb_log)
        r2 = QHBoxLayout()
        r2.setSpacing(8)
        b_open_log = QPushButton("打开日志文件")
        b_open_log.setObjectName("warn")
        b_clear_log = QPushButton("清空日志")
        b_clear_log.setObjectName("danger")
        b_open_log.clicked.connect(self._open_log)
        b_clear_log.clicked.connect(self._clear_log)
        r2.addWidget(b_open_log)
        r2.addWidget(b_clear_log)
        r2.addStretch(1)
        g2.addLayout(r2)
        lay.addWidget(gb2)

        lb = QLabel("配置、标记与日志都保存在 %APPDATA%\\YYYCleanSweep 下，"
                    "与程序本体分离，重新打包或换版本都不会丢失。")
        lb.setObjectName("hint")
        lay.addWidget(lb)
        w._is_settings = True
        self._fill_history()
        self._refresh_cache_label()
        self._refresh_thumb_cache_label()
        return w

    # ---------------------------------------------------------- 缓存
    def _cache_usage_html(self, n, sz, unit):
        """缓存占用文案：带单位的可读值打头，精确字节放在后面的灰色括号里。

        只写 fmt_bytes（千分位）不带单位，用户还得自己数位数换算；
        只写 fmt_size 又看不到确切数字。两个都给，各自的位置不打架。
        """
        p = self.pal
        return (
            f"已缓存 <b style='color:{p['accent']}'>{n}</b> {unit} · 占用 "
            f"<b style='color:{p['accent']}'>{fmt(sz)}</b>"
            f"<span style='color:{p['text_dim']}'>"
            f"（{fmt_bytes(sz)} 字节）</span>")

    def _set_cache_path(self, label, path):
        """把缓存库文件的真实路径摆在卡片第二行，双击可整条复制。

        宽度策略设成 Ignored：整条路径的 sizeHint 很宽，参与布局的话会把
        设置页横向撑出滚动条，交给 PathLabel 自己按可用宽度省略就行。
        """
        try:
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        except Exception:
            pass
        label.set_path(path)

    def _refresh_cache_label(self):
        try:
            n, sz = scan_cache.stats()
        except Exception:
            n, sz = 0, 0
        self.lb_cache.setText(self._cache_usage_html(n, sz, "个目录"))
        self._set_cache_path(self.lb_cache_path, scan_cache.db_file())
        # 两张表共用一个库文件，一起刷新，用户清完缓存也能立刻看到快照还在
        self._refresh_snap_cache_label()

    def _clear_cache(self):
        scan_cache.clear()
        self._refresh_cache_label()

    # ---------------------------------------------------------- 空间快照
    def _refresh_snap_cache_label(self):
        """快照计数 + 库文件路径。两个数字分别用强调色，和上面两张卡片观感一致。"""
        try:
            b, n = scan_cache.snapshot_stats()
        except Exception:
            b, n = 0, 0
        p = self.pal
        self.lb_snap_cache.setText(
            f"已留 <b style='color:{p['accent']}'>{b}</b> 次 · "
            f"<b style='color:{p['accent']}'>{n}</b> 条")
        # 快照与扫描缓存共用同一个 db 文件，路径照给，方便用户自己核大小
        self._set_cache_path(self.lb_snap_path, scan_cache.db_file())

    def _clear_snapshots(self):
        try:
            scan_cache.clear_snapshots()
        except Exception:
            pass
        self._refresh_snap_cache_label()

    def _on_snap_keep_changed(self, v):
        self.cfg.set("snap_keep", int(v))
        try:
            scan_cache.set_snapshot_keep(int(v))
        except Exception:
            pass

    # ---------------------------------------------------------- 缩略图缓存
    def _refresh_thumb_cache_label(self):
        try:
            n, sz = thumb_mod.stats()
        except Exception:
            n, sz = 0, 0
        self.lb_thumb_cache.setText(self._cache_usage_html(n, sz, "张"))
        self._set_cache_path(self.lb_thumb_path, thumb_mod.db_file())

    def _clear_thumb_cache(self):
        try:
            thumb_mod.clear()
        except Exception:
            pass
        self._refresh_thumb_cache_label()

    def _reveal_file(self, path, what="缓存"):
        """在资源管理器里选中某个文件：让用户亲眼看到缓存落在哪、多大。"""
        if not os.path.exists(path):
            QMessageBox.information(
                self, f"还没有{what}文件",
                f"这个文件还没有生成（用得上时才会自动创建）：\n{path}")
            return
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        except Exception as e:
            QMessageBox.warning(self, "无法打开", str(e))

    def _on_thumb_max_changed(self, v):
        """缩略图缓存上限：改完立即生效，下次写入时按新上限淘汰。"""
        self.cfg.set("thumb_max_items", int(v))
        try:
            thumb_mod.set_limits(max_items=int(v))
        except Exception:
            pass

    # ---------------------------------------------------------- 主题
    def _apply_theme(self):
        self.pal = palette(self.theme)
        QApplication.instance().setStyleSheet(qss(self.theme))
        self.check_style.set_palette(self.pal)
        self.cat_delegate.set_palette(self.pal)
        # 各页列表里散落的行委托统一换色：漏掉的话，切成亮色主题后
        # 列表里还会留着一块块暗色的选中 / 斑马底
        refresh_delegates(self.pal)
        self.btn_theme.setText("亮色" if self.theme == "dark" else "暗色")
        for d in (getattr(self, "tree_delegate", None),
                  getattr(self, "big_delegate", None),
                  getattr(self, "hist_delegate", None)):
            if d:
                d.set_palette(self.pal)
        for c in (getattr(self, "big_donut", None),):
            if c:
                c.set_palette(self.pal)
        if hasattr(self, "big_bars"):
            self.big_bars.set_palette(self.pal)
        for b in (getattr(self, "scan_banner", None),
                  getattr(self, "big_banner", None),
                  getattr(self, "del_banner", None)):
            if b:
                b.set_palette(self.pal)
        for w in (self.cat_list, self.tree, self.tw_big):
            w.viewport().update()
        if hasattr(self, "dir_page"):
            self.dir_page.on_theme(self.theme)
        if hasattr(self, "sysclean_page"):
            self.sysclean_page.on_theme(self.theme)
        if hasattr(self, "marks_page"):
            self.marks_page.on_theme(self.theme)
        if hasattr(self, "dup_page"):
            self.dup_page.on_theme(self.theme)
        if hasattr(self, "dupdir_page"):
            self.dupdir_page.on_theme(self.theme)
        # 缓存那两行的数字用了强调色的富文本，换主题得跟着重排一遍
        if hasattr(self, "lb_cache"):
            self._refresh_cache_label()
            self._refresh_thumb_cache_label()
        if self.tx_detail.toPlainText():
            cur = self.tree.currentItem()
            if cur:
                self._render_detail(cur.data(0, Qt.UserRole))
        if self.tx_sys.toPlainText():
            self._fill_sys()

    def toggle_theme(self):
        """切换明暗主题，并把新配色推送到每一个页面。"""
        self.theme = "light" if self.theme == "dark" else "dark"
        self.cfg.set("theme", self.theme)
        self._apply_theme()
        if hasattr(self, "cb_theme"):
            self.cb_theme.blockSignals(True)
            self.cb_theme.setCurrentIndex(0 if self.theme == "dark" else 1)
            self.cb_theme.blockSignals(False)

    def _on_sound_changed(self, _s):
        on = self.chk_sound.isChecked()
        self.cfg.set("sound", on)
        self.sfx.configure(enabled=on)
        self.sfx.prepare()

    def _on_volume_changed(self, v):
        self.lb_vol.setText(f"{v}%")
        self.cfg.set("sound_volume", v)
        self.sfx.configure(volume=v / 100.0)
        self.sfx.prepare()

    def _on_theme_combo(self, i):
        want = "dark" if i == 0 else "light"
        if want != self.theme:
            self.toggle_theme()

    # ---------------------------------------------------------- 扫描
    def start_scan(self, resume=False):
        """resume=True 时只扫上次中断后剩下的规则，已扫到的结果保留。"""
        if self.scan_thread and self.scan_thread.isRunning():
            return
        if not resume:
            self.results = {}
            self.scan_pending = list(self.rules)
        if not self.scan_pending:
            return
        base = len(self.results)
        total = len(self.rules)

        self.tree.blockSignals(True)
        self.tree.clear()
        self.tree.blockSignals(False)
        if not resume:
            # 全新一轮扫描：右侧详情留着上一次选中项的介绍会被当成新结果。
            # 「继续扫描」不清 —— 那时已扫到的行还在，详情是有效的。
            self.tx_detail.clear()
        # 状态条放在页面顶部：旋转指示 + 当前项 + 计时 + 条纹进度，一眼看得出在扫
        self.scan_banner.start(
            f"正在扫描 {self.drive} 的清理项",
            f"{base}/{total} · 准备中…", on_stop=self.stop_scan,
            maximum=max(total, 1))
        self.scan_banner.strip.set_value(base, max(total, 1))
        self.scan_thread = ScanThread(self.scan_pending, base, total, self)
        self.scan_thread.progress.connect(self._on_scan_progress)
        self.scan_thread.finished_all.connect(self._on_scan_done)
        self.scan_thread.start()
        self._update_scan_buttons(True)
        self._refresh_disk()

    def stop_scan(self):
        """停止扫描：置取消标志，已扫完的部分照常显示。"""
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.stop()
            self.btn_stop.setEnabled(False)
            self.scan_banner.set_sub("正在停止…")
            self.lb_free.setText(
                f"<span style='color:{self.pal['risk']['caution']}'>正在停止…</span>")

    def _update_scan_buttons(self, running=None):
        if running is None:
            running = bool(self.scan_thread and self.scan_thread.isRunning())
        pending = len(self.scan_pending)
        scanned = len(self.results)
        self.btn_scan.setVisible(not running)
        self.btn_stop.setVisible(running)
        self.btn_resume.setVisible((not running) and pending > 0 and scanned > 0)
        self.btn_scan.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_resume.setEnabled((not running) and pending > 0)
        if not running:
            self.btn_scan.setText("重新扫描" if scanned else "开始扫描")
        self.btn_clean.setEnabled(not running and self._has_selection())

    def _on_scan_progress(self, done, total, name):
        short = name if len(name) <= 18 else name[:17] + "…"
        self.scan_banner.set_sub(f"{done}/{total} · {short}",
                                 value=done, maximum=max(total, 1))

    def _on_scan_done(self, results):
        got = results or {}
        self.results.update(got)
        done_ids = set(got.keys())
        self.scan_pending = [r for r in self.scan_pending
                             if r["id"] not in done_ids]
        stopped = bool(getattr(self.scan_thread, "stopped", False))
        self.scan_thread = None

        total_found = sum(self.results.get(r["id"], {}).get("size", 0)
                          for r in self.rules)
        self.scan_banner.finish(
            "已停止" if stopped else "扫描完成",
            f"可清理 {len(self.results)} 项 · 合计 {fmt(total_found)}"
            + (f" · 还剩 {len(self.scan_pending)} 项未扫" if stopped else ""))
        if not stopped:
            QTimer.singleShot(1500, self.scan_banner.hide)
        self._refresh_categories()
        self._refresh_tree()
        self._refresh_disk()
        self._update_scan_buttons(False)
        if stopped:
            self.lb_free.setText(
                f"<span style='color:{self.pal['risk']['caution']}'>已停止</span>"
                f" <span style='color:{self.pal['text_dim']}'>· 还剩 "
                f"{len(self.scan_pending)} 项未扫，可点「继续扫描」</span>")
        else:
            self._refresh_disk()
        if self.sfx and not stopped:
            self.sfx.play("scan_done")

    def _refresh_disk(self, target=None):
        want = (target or self.drive)[:2].upper()
        for letter, total, free in sysinfo.get_drives():
            if letter.upper().startswith(want):
                used = total - free
                self.pb_disk.setMaximum(100)
                self.pb_disk.setValue(int(used * 100 / max(total, 1)))
                pct = used * 100 / max(total, 1)
                col = (self.pal["risk"]["danger"] if pct > 92
                       else self.pal["risk"]["caution"] if pct > 78
                       else self.pal["accent"])
                self.lb_free.setText(
                    f"<span style='color:{self.pal['text']}'>{letter} 剩余 "
                    f"<b>{fmt(free)}</b></span> <span style='color:{col}'>"
                    f"（已用 {pct:.0f}%）</span> / 共 {fmt(total)}")
                return

    # ---------------------------------------------------------- 空间提醒（F）
    def _goto_clean_tab(self):
        """提醒条上的「去清理」：跳到项目清理页，并顺手把卡片收起来。"""
        self.tabs.setCurrentIndex(self.tabs.indexOf(self.tab_clean))
        self.warn_card.setVisible(False)

    def _dismiss_warn_card(self):
        """「知道了」：本次运行不再出现（配置里的当日记录已经写过，重启也不重复）。"""
        self.warn_card.setVisible(False)

    def _warn_freq(self):
        """提醒频率：daily = 每天每盘一次；always = 每次启动都提示。"""
        v = str(self.cfg.get("warn_freq", "daily") or "daily").lower()
        return v if v in ("daily", "always") else "daily"

    def _on_warn_enabled(self, v):
        self.cfg.set("warn_enabled", bool(v))
        self._sync_warn_tip()

    def _on_warn_freq(self, idx):
        self.cfg.set("warn_freq", "always" if idx == 1 else "daily")
        self._sync_warn_tip()

    def _sync_warn_tip(self):
        """设置页那张卡片右上角的一句话状态，跟着开关与频率实时变。"""
        if not hasattr(self, "lb_warn_tip"):
            return
        if not self.ck_warn.isChecked():
            self.lb_warn_tip.setText("提醒已关闭")
        elif self._warn_freq() == "always":
            self.lb_warn_tip.setText("每次启动都提示")
        else:
            self.lb_warn_tip.setText("每个盘每天提示一次")

    def _check_disk_space(self, manual=False):
        """启动时的空间提醒。只查当前目标盘，绝不后台常驻。

        manual=True 由设置页的「立即检查」触发：忽略「今天已提醒过」，
        也不会把今天的提醒名额用掉。
        """
        if not self.cfg.get("warn_enabled", True):
            if manual:
                self._show_warn_says("提醒已关闭")
            return
        want = (self.drive or "")[:2].upper()
        hit = None
        for letter, total, free in sysinfo.get_drives():
            if letter.upper().startswith(want):
                hit = (letter, total, free)
                break
        if not hit:
            if manual:
                self._show_warn_says("读不到磁盘信息")
            return

        letter, total, free = hit
        tag = letter[:2].upper()             # "C:" —— get_drives 给的是 "C:\"，别带斜杠显示
        gb_line = float(self.cfg.get("warn_gb", 5) or 0) * (1024 ** 3)
        pct_line = float(self.cfg.get("warn_pct", 3) or 0)

        by_gb = gb_line > 0 and free < gb_line
        pct_now = free * 100.0 / max(total, 1)
        by_pct = pct_line > 0 and pct_now < pct_line

        if not (by_gb or by_pct):
            if manual:
                self._show_warn_says("当前空间充足")
            return

        today = time.strftime("%Y-%m-%d")
        last = self.cfg.get("warn_last", "") or ""
        if not manual and self._warn_freq() == "daily" \
                and last.startswith(today + "|" + tag):
            return                       # 今天这个盘已经提醒过了
        self.cfg.set("warn_last", today + "|" + tag)

        self.btn_warn_clean.setVisible(True)   # 「当前空间充足」时藏过，这里要恢复
        self._warn_transient = False           # 真提醒，别被「2 秒后收起」误关
        if by_gb:
            self.lb_warn.setText(
                f"⚠ {tag} 盘可用空间只剩 {fmt(free)}"
                f"（不足提醒线 {fmt(gb_line)}）")
        else:
            self.lb_warn.setText(
                f"⚠ {tag} 盘可用空间只剩 {pct_now:.1f}%"
                f"（不足提醒线 {pct_line:g}%）")
        self.warn_card.setVisible(True)

    def _show_warn_says(self, text):
        """借提醒条显示一句短反馈（设置页「立即检查」用），2 秒后自动收起。

        用 _warn_transient 做记号：这 2 秒里要是真触发了提醒，那条不该被
        定时器顺手关掉。
        """
        self._warn_transient = True
        self.btn_warn_clean.setVisible(False)
        self.lb_warn.setText(text)
        self.warn_card.setVisible(True)
        QTimer.singleShot(2000, self._hide_warn_says)

    def _hide_warn_says(self):
        """只收起「一句反馈」状态；真触发的提醒条不归它管。"""
        if not getattr(self, "_warn_transient", False):
            return
        self._warn_transient = False
        self.warn_card.setVisible(False)
        self.btn_warn_clean.setVisible(True)

    def _refresh_categories(self):
        """分类下标注条数与合计占用。"""
        agg = {}
        for r in self.rules:
            s = self.results.get(r["id"], {}).get("size", 0)
            a = agg.setdefault(r["category"], [0, 0])
            a[0] += 1
            a[1] += s
        tot_n, tot_s = len(self.rules), sum(
            self.results.get(r["id"], {}).get("size", 0) for r in self.rules)
        self.cat_list.blockSignals(True)
        for i in range(self.cat_list.count()):
            it = self.cat_list.item(i)
            d = it.data(Qt.UserRole) or {}
            cid = d.get("id")
            n, s = agg.get(cid, (0, 0)) if cid != "all" else (tot_n, tot_s)
            d["sub"] = f"{n} 项 · {fmt(s)}" if s else f"{n} 项"
            it.setData(Qt.UserRole, d)
        self.cat_list.blockSignals(False)
        self.cat_list.viewport().update()

    # ---------------------------------------------------------- 列表
    def _visible_rules(self):
        sysdrive = (os.environ.get("SystemDrive") or "C:")[:2].upper()
        on_system = self.drive[:2].upper() == sysdrive
        out = []
        for r in self.rules:
            if self.current_cat != "all" and r["category"] != self.current_cat:
                continue
            size = self.results.get(r["id"], {}).get("size", 0)
            if size <= 0 and r.get("mode") == "command":
                # DISM / 磁盘清理 / 休眠之类只针对系统盘，切到别的盘就不显示
                if not on_system:
                    continue
                out.append((r, size, "—"))
            elif size <= 0:
                continue
            else:
                out.append((r, size, fmt(size)))
        key = {0: "name", 1: "risk", 2: "size", 3: "recommend"}.get(
            self.sort_col, "size")
        desc = self.sort_desc
        if key == "size":
            out.sort(key=lambda x: x[1], reverse=desc)
        elif key == "risk":
            out.sort(key=lambda x: (RISK_ORDER.get(x[0].get("risk"), 9), -x[1]),
                     reverse=False)
            if desc:
                out.reverse()
        elif key == "recommend":
            def _reco_key(x):
                r, size, _t = x
                res = self.results.get(r["id"])
                if not res or res.get("size", 0) <= 0:
                    return 0.0  # 无扫描数据项视为 0 分，沉到最低一侧
                return float(self._recommend_score(
                    r, size, res.get("newest", 0.0)))
            out.sort(key=_reco_key, reverse=desc)
        else:
            out.sort(key=lambda x: x[0]["name"], reverse=desc)
        return out

    def _refresh_tree(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        sorter = getattr(self, "tree_sorter", None)
        if sorter is not None:
            sorter.set_state(self.sort_col, self.sort_desc)
        else:
            self.tree.header().setSortIndicator(
                self.sort_col,
                Qt.DescendingOrder if self.sort_desc else Qt.AscendingOrder)

        has_kids = False
        for r, size, size_txt in self._visible_rules():
            rid = r["id"]
            risk = r.get("risk", "safe")
            lock = self._rule_protected(r)
            per = self._rule_per(r)
            multi = len(per) > 1

            reco = (0 if lock else self._recommend_score(
                r, size, self.results.get(r["id"], {}).get("newest", 0.0)))
            it = QTreeWidgetItem([("🔒 " if lock else "") + r["name"],
                                  "白名单" if lock else RISK_LABEL.get(risk, risk),
                                  size_txt, "" if lock else str(reco)])
            it.setData(0, Qt.UserRole, rid)
            it.setData(0, Qt.UserRole + 1, "")          # 空 = 父项
            it.setData(3, Qt.UserRole, reco)
            # 风险权重存进第 1 列，点「风险」表头排序时直接用（白名单=9 沉底）
            it.setData(1, Qt.UserRole,
                       9 if lock else RISK_ORDER.get(risk, 9))
            if multi:
                # 父项需要 ItemIsAutoTristate 才能显示 半选 状态；但点击/级联完全由我们
                # 在 _on_item_changed 里控制，并忽略 Qt 自动派生的 PartiallyChecked 信号。
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsAutoTristate)
                sel = self._checked_paths(r)
                if not sel:
                    st = Qt.Unchecked
                elif len(sel) >= len(per):
                    st = Qt.Checked
                else:
                    st = Qt.PartiallyChecked
            else:
                st = (Qt.Checked if (rid in self.checked
                                     or rid in self._pending_selection)
                      else Qt.Unchecked)
            it.setCheckState(0, st)
            it.setForeground(1, self._color(self._risk_color(risk, lock)))
            it.setForeground(2, self._color(
                self.pal["text"] if size else self.pal["text_faint"]))
            it.setToolTip(0, (r.get("desc", "") +
                              "\n\n已在白名单中，清理时会跳过。") if lock else r.get("desc", ""))
            it.setData(2, Qt.UserRole, 0 if lock else size)
            if lock:
                for c in range(3):
                    it.setBackground(c, self._color(self.pal["tint"]["keep"]))
            self.tree.addTopLevelItem(it)

            # 一条规则对应多个目录时展开成子项，可以只勾选其中一部分
            if multi:
                has_kids = True
                for p, psz in per:
                    plock = self.marks.is_protected(p)
                    creco = (0 if plock else self._recommend_score(
                        r, psz, self.results.get(rid, {}).get("newest", 0.0)))
                    c = QTreeWidgetItem([("🔒 " if plock else "")
                                         + short_path(p, 1, 2, 90),
                                         "白名单" if plock else "",
                                         fmt(psz), "" if plock else str(creco)])
                    c.setData(0, Qt.UserRole, rid)
                    c.setData(0, Qt.UserRole + 1, p)     # 非空 = 子项，值是完整路径
                    c.setData(3, Qt.UserRole, creco)
                    # 子项沿用所属规则的风险权重，排序时才不会因为没有风险文本而乱排
                    c.setData(1, Qt.UserRole,
                              9 if plock else RISK_ORDER.get(risk, 9))
                    c.setToolTip(0, p + ("\n\n已在白名单中。" if plock else ""))
                    on = (rid in self.checked
                          or f"{rid}|{p.lower()}" in self.checked) and not plock
                    c.setCheckState(0, Qt.Checked if on else Qt.Unchecked)
                    # 渲染时把子键补齐：否则「整条规则全选」状态下单独取消一个子项，
                    # 会因为集合里没有其它子键而把整条规则一起取消掉
                    if on and not plock:
                        self.checked.add(f"{rid}|{p.lower()}")
                    c.setForeground(0, self._color(self.pal["text_dim"]))
                    c.setForeground(2, self._color(self.pal["text_dim"]))
                    c.setForeground(1, self._color(
                        self.pal["risk"]["safe"] if plock else self.pal["text_faint"]))
                    if plock:
                        for cc in range(3):
                            c.setBackground(cc, self._color(self.pal["tint"]["keep"]))
                    it.addChild(c)
        self.tree.blockSignals(False)
        # 占比条按当前列表里最大的一项归一化
        self.tree_delegate.set_bar_max(
            max([self.results.get(r["id"], {}).get("size", 0)
                 for r, _s, _t in self._visible_rules()] or [1]))
        if self.btn_expand.isChecked():
            for i in range(self.tree.topLevelItemCount()):
                it = self.tree.topLevelItem(i)
                if it.childCount():
                    it.setExpanded(True)
        self._pending_selection.clear()
        self._update_sel()
        self._update_tip()

    def _update_tip(self):
        sysdrive = (os.environ.get("SystemDrive") or "C:")[:2].upper()
        n = self.tree.topLevelItemCount()
        if self.drive[:2].upper() != sysdrive and n < 8:
            self.lb_tip.setText(
                f"<b>{self.drive}</b> 不是系统盘，多数清理规则（临时文件、更新缓存、"
                f"浏览器缓存）都在系统盘上，这里只剩回收站、崩溃转储这类通用项。<br>"
                f"要分析这个盘的空间去向，请用上面的「<b>目录分析</b>」或"
                f"「<b>大文件定位</b>」，它们对任意盘都完全有效。")
            self.tip.show()
        else:
            self.tip.hide()

    def _risk_color(self, risk, lock=False):
        if lock:
            return self.pal["risk"]["safe"]
        return self.pal["risk"].get(risk, self.pal["risk"]["keep"])

    def _on_sort_changed(self, col, desc):
        """点表头排序后的落库与重排（排序状态会被记住）。"""
        self.sort_col, self.sort_desc = col, bool(desc)
        self.cfg.set("sort_clean", [self.sort_col, self.sort_desc])
        self._refresh_tree()

    def _on_cat_changed(self, cur, prev):
        if not cur:
            return
        d = cur.data(Qt.UserRole) or {}
        self.current_cat = d.get("id", "all")
        self.cfg.set("category", self.current_cat)
        self._refresh_tree()

    def _color(self, hex_color):
        return QColor(hex_color)

    def _rule_targets(self, r):
        return self.results.get(r["id"], {}).get("targets") or []

    def _rule_per(self, r):
        """[(路径, 字节)] —— 优先用扫描时记录的逐个目录占用。"""
        res = self.results.get(r["id"], {})
        per = res.get("per")
        if per:
            return [(x["path"], x.get("size", 0)) for x in per]
        return [(t, 0) for t in (res.get("targets") or [])]

    def _rule_protected(self, r):
        return any(self.marks.is_protected(t) for t in self._rule_targets(r))

    # ---------------------------------------------------------- 勾选
    def _checked_paths(self, r):
        """这条规则当前被选中的具体路径列表（考虑白名单）。

        多目录规则一律用「每个路径一个键」表示，全选 = 所有子键齐全。
        不用单独的 rid 表示全选，是因为 Qt 三态下设置父项会补发子项信号，
        用 rid 当全选标记会被这些补发的信号冲掉。
        """
        per = self._rule_per(r)
        if not per:
            return []
        rid = r["id"]
        if len(per) <= 1:
            return [p for p, _ in per
                    if rid in self.checked and not self.marks.is_protected(p)]
        out = []
        for p, _s in per:
            if self.marks.is_protected(p):
                continue
            if f"{rid}|{p.lower()}" in self.checked:
                out.append(p)
        return out

    def _set_checked_rules(self, ids):
        """批量设置勾选（含多目录规则的每一个子路径）。"""
        self.checked = set()
        for rid in ids:
            r = self.by_id.get(rid)
            if not r:
                continue
            self.checked.add(rid)
            for p, _ in self._rule_per(r):
                self.checked.add(f"{rid}|{p.lower()}")

    def _selected_plan(self):
        """[(规则 id, [要清理的路径])] —— 交给 Cleaner 执行的计划。"""
        plan = []
        for it in self._iter_items():
            rid = it.data(0, Qt.UserRole)
            r = self.by_id.get(rid)
            if not r or self._rule_protected(r):
                continue
            per = self._rule_per(r)
            if len(per) > 1:
                paths = self._checked_paths(r)
            else:
                paths = [p for p, _ in per] if rid in self.checked else []
            if paths:
                plan.append((rid, paths))
        return plan

    def _has_selection(self):
        return bool(self._selected_plan())

    # ---------------------------------------------------------- 导出清理脚本（D）
    def _rule_is_checked(self, rid):
        """这条规则当前是否被勾中（单目录规则存裸 id，多目录存 id|path）。"""
        if rid in self.checked:
            return True
        pref = rid + "|"
        return any(k.startswith(pref) for k in self.checked)

    def _selected_plan_for_export(self):
        """[(规则 dict, [路径])] —— 导出脚本专用。

        和 _selected_plan() 的差别：后者要求「必须有要删的路径」，会把
        command / manual 类规则整个过滤掉。导出时恰恰要把它们列出来
        （command 写成 REM 注释、manual 只写建议），否则用户勾了 WinSxS
        却导不出去，等于这个功能在最需要它的场景失效。

        遍历顺序按规则表本身，不按界面可见项——用户勾完再切分类筛选时，
        被筛掉的项也得照样导出，不能悄悄少几条。
        """
        order = {r["id"]: i for i, r in enumerate(self.rules)}
        rids = []
        for key in self.checked:
            rid = key.split("|", 1)[0]
            if rid not in rids:
                rids.append(rid)
        rids.sort(key=lambda x: order.get(x, 10 ** 9))
        out = []
        for rid in rids:
            r = self.by_id.get(rid)
            if not r or self._rule_protected(r):
                continue
            out.append((r, self._checked_paths(r)))
        return out

    def _export_script(self):
        """把勾选的项目导出成一个可直接运行的批处理。"""
        from core import exporter
        pairs = self._selected_plan_for_export()
        if not pairs:
            QMessageBox.information(
                self, "没有可导出项",
                "先在左边的清单里勾选要清理的项目，再点导出。")
            return
        modes = [exporter.MODE_TITLE[exporter.RECYCLE],
                 exporter.MODE_TITLE[exporter.PERMANENT]]
        mode_text, ok = QInputDialog.getItem(
            self, "导出清理脚本",
            "脚本用什么方式处理这些条目：", modes, 0, False)
        if not ok or not mode_text:
            return
        mode = (exporter.RECYCLE
                if mode_text == exporter.MODE_TITLE[exporter.RECYCLE]
                else exporter.PERMANENT)
        path, _ = QFileDialog.getSaveFileName(
            self, "导出清理脚本", "清理脚本.bat", "批处理文件 (*.bat)")
        if not path:
            return
        if not path.lower().endswith(".bat"):
            path += ".bat"
        try:
            exporter.save_bat(pairs, path, mode=mode)
        except Exception as e:                                  # noqa: BLE001
            QMessageBox.warning(self, "导出失败", str(e))
            return
        if QMessageBox.question(
                self, "导出完成",
                f"已导出 {len(pairs)} 个清理项到：\n{path}\n\n"
                "现在打开文件夹看看？（建议先看一遍内容再运行）",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self._reveal_file(path, "清理脚本")

    def _plan_bytes(self):
        total = 0
        for rid, paths in self._selected_plan():
            sizes = {p.lower(): s for p, s in self._rule_per(self.by_id[rid])}
            total += sum(sizes.get(p.lower(), 0) for p in paths)
        return total

    def _on_item_changed(self, item, col):
        if col != 0 or self._in_item_changed:
            return
        self._in_item_changed = True
        try:
            rid = item.data(0, Qt.UserRole)
            sub = item.data(0, Qt.UserRole + 1)     # 子项这里是完整路径
            r = self.by_id.get(rid)
            if not r:
                return
            self.tree.blockSignals(True)
            try:
                multi = item.childCount() > 1
                on = item.checkState(0) == Qt.Checked
                if sub:
                    key = f"{rid}|{sub.lower()}"
                    if on:
                        self.checked.add(key)
                    else:
                        self.checked.discard(key)
                    if item.parent():
                        self._sync_parent(item.parent(), r)
                    self.sfx.play("check_on" if on else "check_off")
                else:
                    # 父项：Qt 的 ItemIsAutoTristate 会在子项变化时把父项自动设成
                    # PartiallyChecked 并触发本回调。这种「派生态」绝不能当作用户意图
                    # 去级联改子项（否则会把整条规则误清空），只有明确的 全选/未选 才级联。
                    if item.checkState(0) == Qt.PartiallyChecked:
                        return
                    self.sfx.play("check_on" if on else "check_off")
                    for i in range(item.childCount()):
                        ch = item.child(i)
                        p = ch.data(0, Qt.UserRole + 1)
                        if not p or self.marks.is_protected(p):
                            continue
                        ch.setCheckState(0, Qt.Checked if on else Qt.Unchecked)
                        k = f"{rid}|{p.lower()}"
                        if on:
                            self.checked.add(k)
                        else:
                            self.checked.discard(k)
                    if not multi:
                        if on:
                            self.checked.add(rid)
                        else:
                            self.checked.discard(rid)
            finally:
                self.tree.blockSignals(False)
        finally:
            self._in_item_changed = False
        self._update_sel()

    def _sync_parent(self, parent, r):
        """子项变化后把父项设成 全选 / 半选 / 未选。"""
        per = self._rule_per(r)
        sel = self._checked_paths(r)
        if not sel:
            st = Qt.Unchecked
        elif len(sel) >= len(per):
            st = Qt.Checked
        else:
            st = Qt.PartiallyChecked
        parent.setCheckState(0, st)

    def _update_sel(self):
        plan = self._selected_plan()
        n = len(plan)
        total = self._plan_bytes()
        sub = ""
        if n:
            paths = sum(len(p) for _, p in plan)
            if paths != n:
                sub = f" <span style='color:{self.pal['text_faint']}'>"
                sub += f"（共 {paths} 个位置）</span>"
        self.lb_sel.setText(
            f"已选 <b>{n}</b> 项 · 预计释放 "
            f"<b style='color:{self.pal['accent']}'>{fmt(total)}</b>{sub}")
        self.btn_clean.setEnabled(n > 0)

    # ---------------------------------------------------------- 详情
    def _on_select(self, cur, prev):
        if not cur:
            return
        rid = cur.data(0, Qt.UserRole)
        sub = cur.data(0, Qt.UserRole + 1) or ""
        self._render_detail(rid, sub)

    def _render_detail(self, rid, sub=""):
        r = self.by_id.get(rid)
        if not r:
            return
        p = self.pal
        res = self.results.get(rid, {})
        size = res.get("size", 0)
        mode = r.get("mode")
        risk = r.get("risk")
        lock = self._rule_protected(r)
        rc = p["risk"].get(risk, p["risk"]["keep"])

        # ---- 子项（多目录规则下的某个具体目录）：显示该子目录的专属详情
        if sub:
            self._render_sub_detail(r, sub, res)
            return

        mode_txt = {"clear_contents": "清空目录内容", "delete_files": "删除匹配文件",
                    "delete_dir": "删除整个目录", "command": "执行系统命令",
                    "glob_dirs": "批量清空匹配目录", "manual": "需手动操作"}.get(mode, mode or "—")

        h = []
        h.append(f"<div style='font-size:15px;font-weight:600;color:{p['text']}'>"
                 f"{esc(r['name'])}</div>")
        h.append("<div style='margin:9px 0 12px'>"
                 + chip(RISK_LABEL.get(risk, risk), rc, p)
                 + chip(fmt(size) if size else "无占用",
                        p["accent"] if size else p["text_faint"], p)
                 + chip("需管理员" if r.get("admin") else "无需管理员",
                        p["risk"]["danger"] if r.get("admin") else p["text_faint"], p)
                 + ("</div><div style='margin:-4px 0 10px'>"
                    + chip("🔒 白名单", p["risk"]["safe"], p) + "</div>" if lock
                    else "</div>"))
        h.append(f"<p style='line-height:1.75;margin:0 0 10px'>{esc(r.get('desc',''))}</p>")
        hr = f"<hr style='border:none;border-top:1px solid {p['border']};margin:10px 0'>"
        h.append(hr)
        h.append(f"<p style='line-height:1.7;margin:0 0 8px'>"
                 f"<b style='color:{p['risk']['safe']}'>建议 ▸</b> "
                 f"{esc(r.get('advice',''))}</p>")
        h.append(f"<p style='line-height:1.7;margin:0 0 8px'>"
                 f"<b style='color:{p['text_dim']}'>方式 ▸</b> {esc(mode_txt)}</p>")
        h.append(f"<p style='line-height:1.7;margin:0'>"
                 f"<b style='color:{p['text_dim']}'>恢复 ▸</b> "
                 f"{esc(r.get('recovery','') or '—')}</p>")
        if lock:
            h.append(f"<p style='line-height:1.7;color:{p['risk']['safe']}'>"
                     f"<b>🔒 已在你的白名单中，清理时会被跳过。</b></p>")

        tgts = self._rule_targets(r)
        if tgts:
            h.append(hr)
            h.append(f"<div style='color:{p['text_dim']};font-size:12px;"
                     f"margin-bottom:5px'>涉及路径（{len(tgts)} 处）</div>")
            for t in tgts[:12]:
                h.append(f"<div style='font-size:12px;line-height:1.9'>"
                         f"{html_path(t, p)}</div>")
            if len(tgts) > 12:
                h.append(f"<div style='font-size:12px;color:{p['text_faint']}'>"
                         f"…另有 {len(tgts) - 12} 处</div>")
        if r.get("command"):
            h.append(hr)
            h.append(f"<div style='color:{p['text_dim']};font-size:12px;"
                     f"margin-bottom:4px'>命令</div>"
                     f"<div style='font-family:Consolas,monospace;font-size:12px'>"
                     f"{esc(r['command'])}</div>")
        self.tx_detail.setHtml("".join(h))
        self.btn_clean_one.setEnabled(mode not in ("manual",))
        self.btn_preview.setEnabled(mode not in ("manual", "command"))

    def _render_sub_detail(self, r, sub, res):
        """子项详情：显示该具体子目录（而不是整条规则）的专属用途/建议。"""
        p = self.pal
        kb = knowledge().lookup(sub)
        # 这个子目录单独占用的体积（扫描时按目录记录）
        sub_size = 0
        for x in res.get("per", []):
            if x.get("path", "").lower() == sub.lower():
                sub_size = x.get("size", 0)
                break
        leaf_name = sub.rstrip("\\").rsplit("\\", 1)[-1]
        krisk = kb.get("risk", "caution")
        kc = p["risk"].get(krisk, p["risk"]["keep"])
        kind_txt = KIND_LABEL.get(kb.get("kind", ""), kb.get("kind", "") or "目录")
        mode_txt = {"clear_contents": "清空目录内容", "delete_files": "删除匹配文件",
                    "delete_dir": "删除整个目录", "command": "执行系统命令",
                    "glob_dirs": "批量清空匹配目录", "manual": "需手动操作"}.get(
                        r.get("mode"), r.get("mode") or "—")
        protected = self.marks.is_protected(sub)

        h = []
        h.append(f"<div style='font-size:15px;font-weight:600;color:{p['text']}'>"
                 f"📂 {esc(leaf_name)}</div>")
        h.append("<div style='margin:9px 0 12px'>"
                 + chip(kind_txt, p["accent"], p)
                 + chip(RISK_LABEL.get(krisk, krisk), kc, p)
                 + chip(fmt(sub_size) if sub_size else "无占用",
                        p["accent"] if sub_size else p["text_faint"], p)
                 + ("</div><div style='margin:-4px 0 10px'>"
                    + chip("🔒 白名单", p["risk"]["safe"], p) + "</div>" if protected
                    else "</div>"))
        h.append(f"<p style='line-height:1.75;margin:0 0 10px'>"
                 f"<b style='color:{p['text_dim']}'>这是什么 ▸</b><br>"
                 f"{esc(kb.get('purpose', ''))}</p>")
        hr = f"<hr style='border:none;border-top:1px solid {p['border']};margin:10px 0'>"
        h.append(hr)
        h.append(f"<p style='line-height:1.7;margin:0 0 8px'>"
                 f"<b style='color:{p['risk']['safe']}'>建议 ▸</b> "
                 f"{esc(kb.get('advice', ''))}</p>")
        h.append(f"<p style='line-height:1.7;margin:0 0 8px'>"
                 f"<b style='color:{p['text_dim']}'>完整路径 ▸</b><br>"
                 f"{html_path(sub, p)}</p>")
        h.append(f"<p style='line-height:1.7;margin:0'>"
                 f"<b style='color:{p['text_dim']}'>所属规则 ▸</b> {esc(r['name'])}"
                 f"（{esc(mode_txt)}）</p>")
        if protected:
            h.append(f"<p style='line-height:1.7;color:{p['risk']['safe']}'>"
                     f"<b>🔒 已在你的白名单中，清理时会跳过。</b></p>")
        self.tx_detail.setHtml("".join(h))
        self.btn_clean_one.setEnabled(r.get("mode") not in ("manual",))
        self.btn_preview.setEnabled(r.get("mode") not in ("manual", "command"))

    # ---------------------------------------------------------- 右键菜单
    def _open_path(self, path):
        """打开目录；传进来的是文件就在资源管理器里选中它。"""
        if not path:
            return
        try:
            if os.path.isdir(path):
                os.startfile(path)
            else:
                subprocess.Popen(
                    ["explorer", "/select,", os.path.normpath(path)])
        except Exception as e:
            QMessageBox.warning(self, "无法打开", str(e))

    def _mark_path(self, path, on):
        if on:
            self.marks.set(path, mark=MARK_KEEP)
        else:
            self.marks.remove(path)
        self._refresh_tree()
        if hasattr(self, "marks_page"):
            self.marks_page.refresh()

    def _toggle_item(self, item):
        cur = item.checkState(0)
        # 半选状态按"勾上"处理，由 _on_item_changed 里的逻辑决定具体级联
        item.setCheckState(0, Qt.Unchecked if cur == Qt.Checked else Qt.Checked)

    def _tree_menu(self, pos):
        it = self.tree.itemAt(pos)
        if not it:
            return
        self.tree.setCurrentItem(it)
        m = self._make_tree_menu(it)
        if m:
            m.exec(self.tree.viewport().mapToGlobal(pos))

    def _make_tree_menu(self, it):
        """清理结果的右键菜单。

        子项（多目录规则里的具体目录）能确定路径，所以直接给出
        「打开 / 复制路径 / 加白名单」；父项只有路径能确定时才给这些操作。
        """
        rid = it.data(0, Qt.UserRole)
        r = self.by_id.get(rid)
        if not r:
            return
        sub = it.data(0, Qt.UserRole + 1) or ""
        if sub:
            paths = [sub]
        else:
            per = self._rule_per(r)
            # 路径太多就不逐个列了，直接在子项里操作
            paths = [p for p, _ in per] if 0 < len(per) <= 3 else []

        m = QMenu(self)
        head = m.addAction(it.text(0)[:56] or r["name"])
        head.setEnabled(False)
        m.addSeparator()

        if len(paths) == 1:
            p = paths[0]
            act(m, "打开目录", "open", lambda: self._open_path(p),
                os.path.isdir(p))
            act(m, "复制完整路径", "copy",
                lambda: QApplication.clipboard().setText(p))
            if self.marks.is_protected(p):
                act(m, "移出白名单", "delete", lambda: self._mark_path(p, False))
            else:
                act(m, "加入白名单", "keep", lambda: self._mark_path(p, True))
        elif paths:
            sm = m.addMenu("打开目录")
            for p in paths:
                act(sm, short_path(p, 1, 2, 58), "open",
                    lambda p=p: self._open_path(p), os.path.isdir(p))
            attach_menu(sm, self.sfx)
        m.addSeparator()

        if not sub and it.childCount():
            exp = it.isExpanded()
            act(m, "折叠子目录" if exp else "展开子目录", "toggle",
                lambda: it.setExpanded(not exp))
        checked = it.checkState(0) == Qt.Checked
        act(m, "取消勾选" if checked else "勾选此项",
            "check_off" if checked else "check_on",
            lambda: self._toggle_item(it))
        m.addSeparator()

        mode = r.get("mode")
        act(m, "预览内容", "menu", self._preview_one,
            mode not in ("manual", "command"))
        act(m, "清理此项…", "warn", self._clean_one, mode not in ("manual",))
        return attach_menu(m, self.sfx)

    def _big_menu(self, pos):
        it = self.tw_big.itemAt(pos)
        if not it:
            return
        self.tw_big.setCurrentItem(it)
        m = self._make_big_menu(it)
        if m:
            m.exec(self.tw_big.viewport().mapToGlobal(pos))

    def _make_big_menu(self, it):
        """大文件结果右键：主要就是"打开目录看看这是个什么东西"。"""
        full = it.data(0, Qt.UserRole)
        if not full:
            return
        is_file = os.path.isfile(full)
        m = QMenu(self)
        head = m.addAction(os.path.basename(full) or full)
        head.setEnabled(False)
        m.addSeparator()
        act(m, "打开文件" if is_file else "打开目录", "open",
            lambda: self._open_path(full))
        act(m, "在资源管理器中显示", "open",
            lambda: self._big_open(it, 0))
        act(m, "复制完整路径", "copy",
            lambda: QApplication.clipboard().setText(full))
        act(m, "复制文件名", "copy",
            lambda: QApplication.clipboard().setText(os.path.basename(full)))
        m.addSeparator()
        if it.childCount():
            exp = it.isExpanded()
            act(m, "折叠" if exp else "展开", "toggle",
                lambda: it.setExpanded(not exp))
            act(m, "展开全部下级", "toggle",
                lambda: (self.tw_big.expandItem(it), self._expand_all(it)))
        return attach_menu(m, self.sfx)

    def _expand_all(self, item):
        stack = [item]
        while stack:
            cur = stack.pop()
            cur.setExpanded(True)
            for i in range(cur.childCount()):
                stack.append(cur.child(i))

    # ---------------------------------------------------------- 选择
    def _iter_items(self):
        for i in range(self.tree.topLevelItemCount()):
            yield self.tree.topLevelItem(i)

    def _selected_ids(self):
        return [it.data(0, Qt.UserRole) for it in self._iter_items()
                if it.checkState(0) == Qt.Checked]

    def _select_safe(self):
        self._set_checked_rules([r["id"] for r in self.rules
                                 if r.get("risk") == "safe"
                                 and not self._rule_protected(r)])
        self._refresh_tree()

    def _select_recommend(self):
        """一键勾选推荐分 ≥ 阈值的「可清理 / 建议迁移 / 谨慎」非锁定项。

        阈值 50：综合分里大占用 + 低风险 + 长期未改的缓存 / 临时类会轻松超过，
        零散小项则不会，避免「一键勾了一堆无关紧要的东西」造成误清理。
        """
        sel = [r["id"] for r in self.rules
               if not self._rule_protected(r)
               and r.get("risk") in ("safe", "migrate", "caution")
               and self.results.get(r["id"], {}).get("size", 0) > 0
               and self._recommend_score(
                   r, self.results[r["id"]]["size"],
                   self.results[r["id"]].get("newest", 0.0)) >= 50]
        self._set_checked_rules(sel)
        self._refresh_tree()

    def _select_none(self):
        self.checked.clear()
        self._refresh_tree()

    def _reselect_last(self):
        ids = set(self.cfg.get("checked") or self.cfg.get("selection") or [])
        if not ids:
            QMessageBox.information(self, "提示", "还没有保存过选择。清理一次后会自动记录。")
            return
        keep = {i for i in ids
                if i in self.by_id and not self._rule_protected(self.by_id[i])}
        keep |= {i for i in ids if "|" in i}      # 子目录级的勾选
        self.checked = keep
        self._refresh_tree()

    def _reuse_history(self):
        row = self.tbl_hist.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "先在上方表格里选中一条历史记录。")
            return
        # ids 存在「项目」列（第 1 列）的 UserRole 上；之前这里读的是第 0 列，
        # 所以按钮点了没反应。顺手修掉。
        ids = self.tbl_hist.item(row, 1).data(Qt.UserRole) or []
        if not ids:
            return
        self._set_checked_rules([i for i in ids
                                 if i in self.by_id
                                 and not self._rule_protected(self.by_id[i])])
        self.tabs.setCurrentIndex(0)
        self._refresh_tree()
        self.lb_free.setText(f"已复用 {len(ids)} 项的历史选择")

    # ---------------------------------------------------------- 清理
    def _preview_one(self):
        cur = self.tree.currentItem()
        if not cur:
            return
        r = self.by_id.get(cur.data(0, Qt.UserRole))
        if not r:
            return
        entries = collect_entries(r, self._rule_targets(r))
        txt = "\n".join(short_path(e, 1, 2, 110) for e in entries[:200]) or "（无可预览内容）"
        QMessageBox.information(self, f"预览 · {r['name']}",
                                f"共 {len(entries)} 个条目（最多显示 200 条）：\n\n{txt}")

    def _clean_one(self):
        cur = self.tree.currentItem()
        if not cur:
            return
        rid = cur.data(0, Qt.UserRole)
        r = self.by_id.get(rid)
        if not r:
            return
        sub = cur.data(0, Qt.UserRole + 1)
        per = self._rule_per(r)
        if sub:
            paths = [sub]
        elif len(per) > 1:
            paths = self._checked_paths(r) or [p for p, _ in per]
        else:
            paths = [p for p, _ in per]
        if not paths:
            QMessageBox.information(self, "提示", "这个项目当前没有可清理的位置。")
            return
        self._do_clean([(rid, paths)])

    def _clean_selected(self):
        plan = self._selected_plan()
        if not plan:
            return
        self._do_clean(plan)

    def _do_clean(self, plan):
        """plan: [(规则 id, [要清理的路径])]"""
        if not plan:
            return
        pairs = [(self.by_id[rid], paths) for rid, paths in plan if rid in self.by_id]
        blocked = [(r, p) for r, p in pairs if self._rule_protected(r)]
        if blocked:
            QMessageBox.information(
                self, "已跳过白名单项目",
                "以下项目被你标记为白名单，本次不会清理：\n\n"
                + "\n".join("🔒 " + r["name"] for r, _ in blocked)
                + "\n\n要清理请先在「我的标记」里移除白名单。")
        pairs = [x for x in pairs if x not in blocked]
        if not pairs:
            return

        size_of = {}
        for r, _ in pairs:
            size_of[r["id"]] = {p.lower(): s for p, s in self._rule_per(r)}
        total = sum(sum(size_of[r["id"]].get(p.lower(), 0) for p in paths)
                    for r, paths in pairs)
        has_caution = any(r.get("risk") == "caution" for r, _ in pairs)
        has_danger = any(r.get("risk") == "danger" for r, _ in pairs)
        if has_danger:
            ok = QMessageBox.warning(
                self, "危险操作",
                "选中的项目包含「危险」级别，误操作可能导致系统无法启动。确定继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ok != QMessageBox.Yes:
                return

        dlg = ConfirmDialog([r["name"] for r, _ in pairs], total,
                            has_caution, has_danger, self)
        if dlg.exec() != QDialog.Accepted:
            return
        mode, backup_root = dlg.result_mode()
        self._last_mode = MODE_LABEL.get(mode, "")
        self._last_backup = backup_root

        # 写入独立日志：先记下本次要动哪些路径，误删后可逐条回溯
        self.journal.begin(
            [(r["name"], paths,
              sum(size_of[r["id"]].get(p.lower(), 0) for p in paths))
             for r, paths in pairs],
            self._last_mode)

        self.btn_clean.setEnabled(False)
        self.btn_scan.setEnabled(False)
        self._clean_start = time.time()
        self._free_before = self._free_bytes()
        self._cleaning = [(r["id"], paths) for r, paths in pairs]
        self.del_banner.start(
            f"正在删除 · {self._last_mode}",
            f"0/{len(pairs)} · 准备中…", maximum=max(len(pairs), 1))
        self.clean_thread = CleanThread(Cleaner(self.rules, self.results),
                                        self._cleaning, mode, backup_root,
                                        self.journal, self)
        self.clean_thread.progress.connect(
            lambda i, t, n: self.del_banner.set_sub(
                f"{i}/{t} · {n}", value=i, maximum=max(t, 1)))
        self.clean_thread.finished_all.connect(self._on_clean_done)
        self.clean_thread.start()

    def _free_bytes(self):
        """当前扫描盘的可用空间（字节），用于报告里的"前后对比"。"""
        want = self.drive[:2].upper()
        for letter, _t, free in sysinfo.get_drives():
            if letter.upper().startswith(want):
                return free
        return 0

    def _on_clean_done(self, stats):
        self.journal.end(stats["freed"], stats["count"],
                         getattr(self, "_last_mode", ""))
        secs = time.time() - getattr(self, "_clean_start", time.time())
        free_after = self._free_bytes()
        self.del_banner.finish(
            "删除完成", f"释放 {fmt(stats['freed'])} · 处理 {stats['count']} 个条目")
        QTimer.singleShot(1200, self.del_banner.hide)
        if self.sfx:
            self.sfx.play("clean_done" if not stats["errors"] else "warn")
        dlg = CleanReportDialog(
            stats, getattr(self, "_last_mode", ""), secs,
            getattr(self, "_free_before", 0), free_after,
            backup_root=getattr(self, "_last_backup", None),
            theme=self.theme, parent=self)
        dlg.exec()
        ids = [rid for rid, _ in self._cleaning]
        names = [self.by_id[i]["name"] for i in ids if i in self.by_id]
        self.cfg.add_history(ids, names, stats["freed"],
                             getattr(self, "_last_mode", ""))
        self.cfg.set("selection", ids)
        self._fill_history()
        self._update_scan_buttons(False)
        self.start_scan()

    # ---------------------------------------------------------- 大文件
    def _clear_big_detail(self):
        """右侧详情回到初始态（开始新一次扫描时调用）。

        列表已经 clear 了，详情还挂着上一个文件的图表与指标，很容易被读成
        「这是刚扫出来的」，所以一起重置。
        """
        self.lb_big_title.setText("大文件详情")
        self.lb_big_path.set_path("")
        for w in (self.lb_big_size, self.lb_big_cnt, self.lb_big_mt):
            w.setText("—")
        self.big_donut.set(0, "—", "", self.pal["text_faint"])
        self.big_bars.set_items([], 0)

    def _scan_big(self):
        self.btn_big.setEnabled(False)
        self._big_stopping = False
        self.tw_big.clear()
        self._big_found = []
        self._clear_big_detail()
        root = self.cfg.get("target_big") or self.drive
        mb = max(10, int(self.sp_big.value()))
        self.big_banner.start(f"正在扫描 {root}",
                              f"查找大于 {mb} MB 的文件…",
                              on_stop=self._stop_big)
        self.big_thread = LargeFileThread(root, mb * 1024 * 1024, self)
        # on_progress 传的是「已找到的文件数」，不是目录数
        self.big_thread.progress.connect(
            lambda n: self.big_banner.set_sub(f"已找到 {n} 个符合条件的文件…"))
        self.big_thread.batch.connect(self._on_big_batch)
        self.big_thread.finished_all.connect(self._on_big_done)
        self.big_thread.start()

    def _stop_big(self):
        if getattr(self, "big_thread", None):
            # 点了停止就别再让中途批次刷新列表，免得刚停下又被重画一次
            self._big_stopping = True
            self.big_thread.cancel()
            self.big_banner.set_sub("正在停止…")

    def _on_big_batch(self, found):
        """边扫边出：每攒够一批就把当前结果画出来，不用干等到全部扫完。"""
        if getattr(self, "_big_stopping", False) or not found:
            return
        grand = self._show_big(found, partial=True)
        self.big_banner.set_sub(
            f"已找到 {len(found)} 个文件 · 合计 {fmt(grand)}，还在扫…")

    def _show_big(self, found, partial=False):
        """把 [(size, path)] 画进树表与右侧图表，返回合计字节数。

        partial=True 表示还在扫：只刷列表、并且把用户已经展开的顶层节点
        原样恢复，不收尾横幅、不播提示音。
        """
        found = found or []
        self._big_found = found
        if not found:
            return 0
        grand = sum(sz for sz, _ in found) or 1
        tree = self._group_tree(found)
        # 只有一个盘符根节点时直接省掉它，从下一层开始展示
        if len(tree) == 1:
            _k, v = next(iter(tree.items()))
            if not v[3] and len(_k) <= 2:
                tree = v[2]
        opened = set()
        if partial:
            for i in range(self.tw_big.topLevelItemCount()):
                it = self.tw_big.topLevelItem(i)
                if it.isExpanded():
                    opened.add(it.text(0))
        self.tw_big.clear()
        self.big_delegate.set_bar_max(grand)
        self._fill_big_tree(tree, grand)
        if opened:
            for i in range(self.tw_big.topLevelItemCount()):
                if self.tw_big.topLevelItem(i).text(0) in opened:
                    self.tw_big.topLevelItem(i).setExpanded(True)
        else:
            self.tw_big.expandToDepth(1)
        self.big_sorter.sort_now()
        self.lb_big.setText(
            f"共 <b>{len(found)}</b> 个文件 · 合计 <b>{fmt(grand)}</b> · "
            f"按目录树分组，双击可打开所在位置、右键可打开所在目录")
        return grand

    def _on_big_done(self, found):
        self.btn_big.setEnabled(True)
        found = found or []
        if not found:
            self.tw_big.clear()
            self._big_found = []
            self._clear_big_detail()
            self.big_banner.finish("扫描完成", "没有符合条件的文件")
            QTimer.singleShot(1500, self.big_banner.hide)
            self.lb_big.setText("未找到符合条件的文件，可以调低阈值再试。")
            return
        grand = self._show_big(found)
        self.big_banner.finish("扫描完成",
                               f"{len(found)} 个文件 · 合计 {fmt(grand)}")
        QTimer.singleShot(1500, self.big_banner.hide)
        if self.sfx:
            self.sfx.play("found")

    def _group_tree(self, found):
        """按路径分段构建嵌套树：{name: [size, count, kids, is_file, full, ext]}

        ext 是「扩展名 -> [字节, 文件数]」，在遍历时同步累加到所有祖先节点，
        这样选中任意一层目录都能立刻画出它的类型构成。
        """
        root = {}
        for sz, path in found:
            parts = [x for x in path.replace("/", "\\").split("\\") if x]
            cur = root
            chain = []
            for i, seg in enumerate(parts):
                node = cur.setdefault(seg, [0, 0, {}, False, path, {}])
                node[0] += sz
                chain.append(node)
                if i == len(parts) - 1:
                    node[1] += 1
                    node[3] = True
                    ext = os.path.splitext(seg)[1].lstrip(".").lower() or "(无扩展名)"
                    rec = node[5].setdefault(ext, [0, 0])
                    rec[0] += sz
                    rec[1] += 1
                    for anc in chain[:-1]:
                        r2 = anc[5].setdefault(ext, [0, 0])
                        r2[0] += sz
                        r2[1] += 1
                cur = node[2]
        return root

    def _fill_big_tree(self, node, grand, parent=None):
        items = sorted(node.items(), key=lambda kv: kv[1][0], reverse=True)
        for name, nd in items:
            size, count, kids, is_file, full, ext = nd
            # 把只有一条分支的目录链压成一行（C:/Users/XIANG/.../pip），
            # 避免顶层只剩一个盘符、要展开十几层才看到文件
            chain = [name]
            while not is_file and len(kids) == 1:
                k2, v2 = next(iter(kids.items()))
                if v2[3]:
                    break
                chain.append(k2)
                size, count, kids, is_file, full, ext = v2
            label = (" / ".join(chain)) if not is_file else name
            if is_file:
                cols = [label, fmt(size), fmt_pct(size, grand),
                        self._type_of(name)]
                tip = (f"{full}\n{fmt(size)}（{fmt_bytes(size)} 字节）\n"
                       f"类型 {self._type_of(name)}\n"
                       f"最近修改 {self._mtime_of(full)}")
            else:
                cols = [label + "/", fmt(size), fmt_pct(size, grand),
                        f"{count} 个文件"]
                tip = (f"{full}\n含子目录 {fmt(size)}（{fmt_bytes(size)} 字节）\n"
                       f"{count} 个文件 · 占本次结果 {fmt_pct(size, grand)}")
            it = QTreeWidgetItem(cols)
            it.setData(0, Qt.UserRole, full)
            it.setData(1, Qt.UserRole, size)         # 给占比条用
            it.setToolTip(0, tip)
            if is_file:
                it.setForeground(0, self._color(self.pal["text"]))
                it.setForeground(1, self._color(self.pal["accent"]))
            else:
                it.setForeground(0, self._color(self.pal["text_dim"]))
                it.setForeground(3, self._color(self.pal["text_faint"]))
            for c in (1, 2):
                it.setTextAlignment(c, Qt.AlignRight | Qt.AlignVCenter)
            if parent is None:
                self.tw_big.addTopLevelItem(it)
            else:
                parent.addChild(it)
            if kids:
                self._fill_big_tree(kids, grand, it)

    def _big_select(self, cur, prev):
        """选中大文件树的一行，右侧用图表 + 指标展示，不再挤在表格列里。"""
        if not cur:
            return
        p = self.pal
        full = cur.data(0, Qt.UserRole)
        if not full:
            return
        size = cur.data(1, Qt.UserRole) or 0
        grand = sum(sz for sz, _ in getattr(self, "_big_found", [])) or 1
        is_file = os.path.isfile(full)
        self.lb_big_title.setText(os.path.basename(full) or full)
        self.lb_big_path.set_path(full)
        self.lb_big_size.setText(fmt(size))
        self.big_donut.set(pct_of(size, grand), fmt(size),
                           f"占结果 {fmt_pct(size, grand)}", p["accent"])
        if is_file:
            self.lb_big_cnt.setText("单个文件")
            self.lb_big_mt.setText(self._mtime_of(full))
            ext = os.path.splitext(full)[1].lstrip(".").lower() or "(无扩展名)"
            self._big_bars_from_ext({ext: [size, 1]})
        else:
            node = self._big_node(full)
            cnt = node[1] if node else 0
            self.lb_big_cnt.setText(f"{cnt} 个文件")
            self.lb_big_mt.setText(self._mtime_of(full))
            self._big_bars_from_ext(node[5] if node else {})

    def _big_node(self, full):
        """从当前大文件树里取回某个路径对应的节点。"""
        found = getattr(self, "_big_found", [])
        tree = self._group_tree(found)
        key = (full or "").replace("/", "\\").strip("\\")
        node = None
        cur = tree
        for seg in [x for x in key.split("\\") if x]:
            if seg not in cur:
                return None
            node = cur[seg]
            cur = node[2]
        return node

    def _big_bars_from_ext(self, ext):
        if not ext:
            self.big_bars.set_items([], 0)
            return
        ft = filetypes()
        items = []
        for cid, (size, count) in ft.group(ext):
            cat = next((c for c in ft.cats if c["id"] == cid), ft.fallback)
            items.append((cat["name"], cat["color"], size, count))
        self.big_bars.set_items(items, sum(s for _n, _c, s, _k in items) or 1)

    def _type_of(self, name):
        from core.knowledge import filetypes
        ext = os.path.splitext(name)[1].lstrip(".").lower()
        return filetypes().of_ext(ext)["name"]

    def _mtime_of(self, path):
        try:
            import time as _t
            return _t.strftime("%Y-%m-%d", _t.localtime(os.stat(path).st_mtime))
        except OSError:
            return ""

    def _big_open(self, item, col):
        path = item.data(0, Qt.UserRole)
        if not path:
            return
        try:
            if os.path.isdir(path):
                os.startfile(path)
            else:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        except Exception as e:
            QMessageBox.warning(self, "无法打开", str(e))

    # ---------------------------------------------------------- 系统信息
    def _fill_sys(self):
        p = self.pal
        h = []
        h.append(f"<div style='font-size:14px;font-weight:600;margin-bottom:10px'>"
                 f"环境</div>")
        adm = sysinfo.is_admin()
        h.append(chip("管理员权限" if adm else "非管理员",
                      p["risk"]["safe"] if adm else p["risk"]["caution"], p))
        h.append(chip(f"内存 {sysinfo.total_ram_gb():.1f} GB", p["accent"], p))
        h.append("<div style='height:8px'></div>")
        h.append(f"<div style='font-size:13px;font-weight:600;margin:12px 0 6px'>"
                 f"分区空间</div>")
        for letter, total, free in sysinfo.get_drives():
            pct = (total - free) * 100 / max(total, 1)
            col = (p["risk"]["danger"] if pct > 92 else
                   p["risk"]["caution"] if pct > 78 else p["accent"])
            w = max(2, int(pct))
            h.append(
                f"<div style='margin-bottom:9px'>"
                f"<b>{esc(letter)}</b> 可用 <b>{fmt(free)}</b> / 共 {fmt(total)}"
                f" <span style='color:{col}'>{pct:.0f}%</span><br>"
                f"<span style='display:inline-block;width:320px;height:7px;"
                f"background:{p['track']};border-radius:4px;margin-top:3px'>"
                f"<span style='display:inline-block;width:{w}%;height:7px;"
                f"background:{col};border-radius:4px'></span></span></div>")
        h.append(f"<div style='font-size:13px;font-weight:600;margin:14px 0 6px'>"
                 f"页面文件（虚拟内存）</div>")
        for x in sysinfo.pagefile_info() or ["未能读取"]:
            h.append(f"<div style='font-size:12px;line-height:1.9'>{esc(x)}</div>")
        hb, hsize = sysinfo.hibernate_status()
        h.append(f"<div style='font-size:13px;font-weight:600;margin:14px 0 6px'>"
                 f"休眠</div>")
        h.append(f"<div style='font-size:12px'>"
                 f"{'已启用，占用 ' + fmt(hsize) if hb else '未启用'}</div>")
        h.append(f"<div style='margin-top:14px;color:{p['text_dim']};font-size:12px'>"
                 f"页面文件不要直接删除，应在「虚拟内存设置」里迁移到其他分区。</div>")
        self.tx_sys.setHtml("".join(h))

    def _run(self, target):
        try:
            if target.startswith("ms-"):
                os.startfile(target)
            else:
                subprocess.Popen(target, shell=True)
        except Exception as e:
            QMessageBox.warning(self, "无法打开", str(e))

    # ---------------------------------------------------------- 历史 / 配置
    def _hist_sort_key(self, col, item):
        """历史表：释放量按字节、「N 项」按条数，其余走默认。"""
        if item is None:
            return None
        if col == 2:
            return item.data(Qt.UserRole)
        if col == 4:
            digits = "".join(ch for ch in item.text() if ch.isdigit())
            return int(digits or 0)
        return None

    def _fill_history(self):
        rows = self.cfg.get("history", [])
        self.tbl_hist.setRowCount(len(rows))
        for i, h in enumerate(rows):
            a = QTableWidgetItem(h.get("time", ""))
            b = QTableWidgetItem("、".join(h.get("names", [])[:6]) or "—")
            b.setData(Qt.UserRole, h.get("ids", []))
            b.setToolTip("、".join(h.get("names", [])))
            c = QTableWidgetItem(fmt(h.get("freed", 0)))
            c.setData(Qt.UserRole, h.get("freed", 0))     # 排序用真实字节
            d = QTableWidgetItem(h.get("mode", ""))
            e = QTableWidgetItem(f"{len(h.get('ids', []))} 项")
            for x in (a, c, d, e):
                x.setTextAlignment(Qt.AlignCenter)
            self.tbl_hist.setItem(i, 0, a)
            self.tbl_hist.setItem(i, 1, b)
            self.tbl_hist.setItem(i, 2, c)
            self.tbl_hist.setItem(i, 3, d)
            self.tbl_hist.setItem(i, 4, e)
        if not rows:
            self.tbl_hist.setRowCount(0)
        if getattr(self, "hist_sorter", None):
            self.hist_sorter.sort_now()

    def _clear_history(self):
        if QMessageBox.question(self, "清空历史", "确定清空所有历史记录？") == QMessageBox.Yes:
            self.cfg.clear_history()
            self._fill_history()

    def _open_log(self):
        p = log_path()
        if not os.path.exists(p):
            QMessageBox.information(self, "还没有日志",
                                    "清理一次之后会自动生成：\n" + p)
            return
        try:
            os.startfile(p)
        except Exception as e:
            QMessageBox.warning(self, "无法打开", str(e))

    def _clear_log(self):
        if QMessageBox.question(
                self, "清空日志",
                "日志是误删后回溯的唯一依据，确定清空？\n（建议先打开看一眼再决定）"
        ) == QMessageBox.Yes:
            self.journal.clear()
            QMessageBox.information(self, "完成", "清理日志已清空。")

    def _export_cfg(self):
        p, _ = QFileDialog.getSaveFileName(
            self, "导出配置与标记", "YYYCleanSweep配置.json", "JSON (*.json)")
        if not p:
            return
        try:
            self.cfg.export_to(p, self.marks)
            QMessageBox.information(self, "完成", "已导出配置与标记。")
        except OSError as e:
            QMessageBox.warning(self, "失败", str(e))

    def _import_cfg(self):
        p, _ = QFileDialog.getOpenFileName(self, "导入配置与标记", "", "JSON (*.json)")
        if not p:
            return
        try:
            n = self.cfg.import_from(p, self.marks)
            self.theme = self.cfg.get("theme", self.theme)
            self.current_cat = self.cfg.get("category", "all")
            self.sort_col, self.sort_desc = self.cfg.get("sort_clean", [2, True])
            rulesmod.set_drive(self.cfg.get("drive", "C:\\"))
            self.drive = rulesmod.current_drive() + "\\"
            self._apply_theme()
            self._fill_categories()
            self._fill_history()
            self.marks_page.refresh()
            QMessageBox.information(self, "完成", f"已导入配置，标记 {n} 条。")
            self.start_scan()
        except Exception as e:
            QMessageBox.warning(self, "失败", str(e))

    # ---------------------------------------------------------- 事件
    def _on_tab_changed(self, i):
        w = self.tabs.widget(i)
        self._sync_target()
        if w is self.marks_page:
            self.marks_page.refresh()
        elif w is self.dir_page:
            self.dir_page.refresh_marks()
        elif getattr(w, "_is_settings", False):
            self._fill_history()
            # 扫完再切回设置页，缓存条目数多半已经变了，顺手刷新成最新值
            self._refresh_cache_label()
            self._refresh_thumb_cache_label()

    def showEvent(self, e):
        super().showEvent(e)
        if not self.tx_sys.toPlainText():
            self._fill_sys()
        self._fill_history()
        self._fit_categories()

    def save_state(self):
        """退出前把窗口大小、勾选状态等写回配置。"""
        try:
            # 完整勾选（含子目录级）单独存一份，「复用上次选择」时能精确还原
            self.cfg.data["checked"] = sorted(self.checked)
            self.cfg.set("selection", sorted(i for i in self.checked if "|" not in i))
            self.cfg.set("category", self.current_cat)
            self.cfg.set("sort_clean", [self.sort_col, self.sort_desc])
            self.cfg.set("theme", self.theme)
            self.cfg.set("drive", self.drive)
            # 窗口尺寸：docstring 里一直说要记，但原先这段写在另一个被覆盖的
            # closeEvent 里（死代码），所以尺寸从来没真正存过。并入这里才生效。
            g = self.normalGeometry() if self.isMaximized() else self.geometry()
            self.cfg.set("win_w", g.width())
            self.cfg.set("win_h", g.height())
            self.cfg.save()
            return True
        except Exception:
            return False

    def closeEvent(self, e):
        self.save_state()
        if e is not None:
            super().closeEvent(e)
