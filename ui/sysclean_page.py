"""系统瘦身页：把「系统信息」里只能看、不能动的项目做成可操作项。

设计约定（与程序整体一致）：
- 所有动作都是**用户点了才执行**，程序绝不自动改系统设置、不常驻、不写注册表；
- 一律用 Windows 自带命令（powercfg / vssadmin / Dism / net / del）与系统 API
  （SHEmptyRecycleBin），**不引入任何第三方依赖**，打包体积不受影响；
- 需要管理员的动作用 ShellExecute(runas) 弹 UAC 提权，失败时明确提示；
- 占用统计放后台线程（临时文件、回收站可能很大），统计中显示与扫描页同一套状态条。

v3.9 的三处调整（用户反馈）：
1. 风险列改成和「项目清理」页同款的分级彩色文本（可清理绿 / 谨慎黄 / 高风险红）；
2. 占用列统一带单位，并在数字下面画占比条，同时把「命令」「说明」都摊开显示，
   不再出现没有单位的裸数字；
3. 表格里不再放「一点就开始清理」的按钮——改成底部统一的操作按钮组
   （选中一行 → 看清楚是什么 → 点执行 → 再确认一次），和目录分析页 / 文件去重页一致。
"""
import os
import ctypes
import subprocess

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QFrame,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
    QAbstractItemView, QApplication,
)

from core import system
from core.sizefmt import fmt_size, fmt_bytes
from ui.theme import palette, DEFAULT_THEME
from ui.busy import ScanBanner
from ui.widgets import (RowDelegate, enable_hover, HeaderSorter, FirstColFit,
                        page_layout, top_bar)
from ui.sound import NullSfx

# 风险等级 -> 显示文本。颜色沿用主题里的 risk 配色（与「项目清理」页同一套）
RISK_TEXT = {"safe": "可清理", "caution": "谨慎", "danger": "高风险",
             "migrate": "建议迁移", "keep": "保留"}


# ---------------- 各项的占用统计（都在后台线程里跑）----------------
def _size_hibernate():
    return system.hibernate_status()[1]


def _size_pagefile():
    total = 0
    for letter, _, _ in system.get_drives():
        p = os.path.join(letter, "pagefile.sys")
        try:
            if os.path.exists(p):
                total += os.stat(p).st_size
        except OSError:
            pass
    return total


def _size_temp():
    return system.temp_size()


def _size_recycle():
    return system.recycle_size()


def _size_update():
    return system.update_cache_size()


def _size_shadow():
    return system.shadow_storage()[0]


def _size_winsxs():
    return system.dir_size(
        os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "WinSxS"))


ITEMS = [
    {
        "id": "hibernate",
        "name": "休眠文件 hiberfil.sys",
        "risk": "safe",
        "size_fn": _size_hibernate,
        "desc": "休眠会在系统盘根目录保留一个接近内存大小的文件。不用休眠功能的话可以直接关掉。",
        "action": "关闭休眠",
        "admin": True,
        "cmd": "powercfg /h off",
        "confirm": "关闭休眠会删除 hiberfil.sys 并释放相应空间。\n"
                   "以后需要时用 powercfg /h on 可以重新开启。\n\n确定关闭吗？",
    },
    {
        "id": "pagefile",
        "name": "页面文件 pagefile.sys",
        "risk": "caution",
        "size_fn": _size_pagefile,
        "desc": "虚拟内存文件。想腾空间就迁到别的盘或调小，不建议整个禁用（部分软件会异常）。",
        "action": "打开虚拟内存设置",
        "open": True,
        "cmd": "sysdm.cpl",
        "confirm": "将打开 Windows 的「虚拟内存」设置窗口，\n"
                   "在里面自行修改各盘的分页文件大小（改完需重启生效）。\n\n"
                   "现在打开吗？",
    },
    {
        "id": "shadow",
        "name": "系统还原点",
        "risk": "danger",
        "size_fn": _size_shadow,
        "desc": "系统还原点（卷影副本）占用的空间。删除后无法再把系统还原到旧状态。",
        "action": "删除全部还原点",
        "admin": True,
        "cmd": "vssadmin delete shadows /all /quiet",
        "confirm": "将删除本机的全部系统还原点，删除后无法撤销，\n"
                   "以后也无法再把系统恢复到之前的某个时间点。\n\n确定继续吗？",
    },
    {
        "id": "update",
        "name": "Windows 更新缓存",
        "risk": "safe",
        "size_fn": _size_update,
        "desc": "Windows Update 下载的安装包缓存（SoftwareDistribution\\Download）。",
        "action": "清理更新缓存",
        "admin": True,
        "cmd": 'net stop wuauserv & net stop bits & rd /s /q '
               '%WINDIR%\\SoftwareDistribution\\Download & net start bits & net start wuauserv',
        "confirm": "会停掉 Windows 更新服务、清空下载缓存再启动服务。\n"
                   "不影响已安装的更新，只清掉下载的安装包。\n\n确定清理吗？",
    },
    {
        "id": "winsxs",
        "name": "组件存储 WinSxS",
        "risk": "caution",
        "size_fn": _size_winsxs,
        "note": "含大量硬链接，实际可释放远小于这个数",
        "desc": "系统组件的历史版本，供更新回滚使用。目录里大量是硬链接，资源管理器看到的数字虚高，"
                "只能用 DISM 让系统自己清理，不能手动删目录。",
        "action": "DISM 清理组件存储",
        "admin": True,
        "cmd": "Dism.exe /Online /Cleanup-Image /StartComponentCleanup",
        "confirm": "将运行 DISM 组件清理，首次运行可能需要几分钟。\n"
                   "这是系统官方的清理方式，不会动到已安装的更新。\n\n确定继续吗？",
    },
    {
        "id": "temp",
        "name": "临时文件",
        "risk": "safe",
        "size_fn": _size_temp,
        "desc": "用户临时目录与系统临时目录里的残留文件，占用中的会被跳过。",
        "action": "清理临时文件",
        "admin": True,
        "cmd": 'del /f /s /q "%TEMP%\\*" & del /f /s /q "%WINDIR%\\Temp\\*"',
        "confirm": "将删除临时目录里的文件（正在使用的会自动跳过）。\n"
                   "不影响已安装的软件。\n\n确定清理吗？",
    },
    {
        "id": "recycle",
        "name": "回收站",
        "risk": "safe",
        "size_fn": _size_recycle,
        "desc": "各盘回收站里还没真正删除的文件。",
        "action": "清空回收站",
        "recycle": True,
        "cmd": "（由系统 API 直接清空，不需要管理员权限）",
        "confirm": "将清空所有盘的回收站，里面的文件无法再还原。\n\n确定清空吗？",
    },
]

ITEM_BY_ID = {it["id"]: it for it in ITEMS}


def _restyle(w):
    """改了 objectName（按钮级别）之后让 QSS 立刻生效。"""
    st = w.style()
    st.unpolish(w)
    st.polish(w)


class StatThread(QThread):
    """逐项统计占用，每算完一项就回传，界面不会卡住。"""

    # 必须用 qlonglong：占用动辄好几 GB，超 2^31 的 int 会直接抛 OverflowError，
    # 那一项就永远停在「统计中…」
    one = Signal(str, "qlonglong")
    done = Signal()

    def __init__(self, items, parent=None):
        super().__init__(parent)
        self.items = items
        self._stop = False

    def cancel(self):
        self._stop = True

    def run(self):
        for it in self.items:
            if self._stop:
                break
            fn = it.get("size_fn")
            size = fn() if fn else 0
            self.one.emit(it["id"], size)
        self.done.emit()


class SysCleanPage(QWidget):
    """系统瘦身：看得到占用，也点得动。"""

    def __init__(self, cfg=None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.sfx = getattr(parent, "sfx", None) or NullSfx()
        self.pal = palette(DEFAULT_THEME)
        self.thread = None
        self.sizes = {}
        self._first = True
        self._build()
        self._sync_bottom()

    # ---------------- UI ----------------
    def _build(self):
        lay = page_layout(self)

        bar, bl = top_bar()
        t = QLabel("系统瘦身")
        t.setObjectName("h2")
        bl.addWidget(t)
        self.lb_admin = QLabel()
        self.lb_admin.setObjectName("muted")
        bl.addWidget(self.lb_admin)
        bl.addStretch(1)
        self.lb_total = QLabel("—")
        self.lb_total.setObjectName("muted")
        bl.addWidget(self.lb_total)
        lay.addWidget(bar)

        self.tbl = QTableWidget(0, 4)
        self.tbl.setHorizontalHeaderLabels(["项目", "占用", "风险", "权限"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SingleSelection)
        # 占用列既显示带单位的数值，也在下面画占比条（和目录分析页同一套画法）
        self.delegate = RowDelegate(self.tbl, elide_col=0, bar_col=1)
        self.tbl.setItemDelegate(self.delegate)
        enable_hover(self.tbl)
        hh = self.tbl.horizontalHeader()
        hh.setMinimumSectionSize(70)
        hh.setSectionResizeMode(0, QHeaderView.Interactive)  # 项目(文件名)列：可手动调宽看全
        for c in (1, 2):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)       # 末列吸收剩余宽度
        self.tbl.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.tbl.setColumnWidth(0, 300)
        # 拉宽 / 缩窄窗口时第一列（项目名）跟着伸缩，其余列各有最小宽不被挤扁
        FirstColFit(self.tbl, min_w=280, others={1: 112, 2: 92, 3: 92})
        # 点表头排序：占用按真实字节、风险/权限按等级存进 UserRole 后按数值排。
        # default_col=None —— 默认保持「从安全到危险」的原始顺序，点表头才重排。
        self.sorter = HeaderSorter(self.tbl, numeric_cols=(1,), default_col=None)
        self.tbl.itemSelectionChanged.connect(self._sync_bottom)
        lay.addWidget(self.tbl, 1)

        # ---- 底部操作条：选中一行后在这里执行，不在表格里放一点就跑的按钮
        self.lb_sel = QLabel("在上面选中一项，右侧按钮会告诉你将要做什么")
        self.lb_sel.setObjectName("muted")
        self.lb_sel.setWordWrap(True)
        self.btn_run = QPushButton("执行操作")
        self.btn_run.setEnabled(False)
        self.btn_run.clicked.connect(self._run_selected)
        self.btn_copy = QPushButton("复制命令")
        self.btn_copy.setToolTip("把这个操作实际执行的命令复制到剪贴板，自己核对")
        self.btn_copy.setEnabled(False)
        self.btn_copy.clicked.connect(self._copy_cmd)
        self.btn_refresh = QPushButton("重新统计")
        self.btn_refresh.setObjectName("primary")
        self.btn_refresh.clicked.connect(self.refresh)
        self.bot = QFrame()
        self.bot.setObjectName("card")
        bbl = QHBoxLayout(self.bot)
        bbl.setContentsMargins(14, 9, 14, 9)
        bbl.setSpacing(8)
        bbl.addWidget(self.lb_sel, 1)
        bbl.addWidget(self.btn_run)
        bbl.addWidget(self.btn_copy)
        bbl.addWidget(self.btn_refresh)
        lay.addWidget(self.bot)

        self.lb_hint = QLabel(
            "占用统计在后台进行。所有操作都要先选中一项、再点「执行」并确认，"
            "程序不会自己动手；带管理员标记的操作会弹 UAC 提权。")
        self.lb_hint.setObjectName("hint")
        self.lb_hint.setWordWrap(True)
        lay.addWidget(self.lb_hint)

        self.banner = ScanBanner(self)
        self.banner.set_palette(self.pal)
        lay.addWidget(self.banner)

        self._fill_rows()
        self.lb_admin.setText(
            "当前：管理员" if system.is_admin()
            else "当前：非管理员（需要权限的操作会弹 UAC）")

    def _fill_rows(self):
        self.tbl.setRowCount(len(ITEMS))
        for r, it in enumerate(ITEMS):
            name = QTableWidgetItem(it["name"])
            name.setData(Qt.UserRole, it["id"])
            name.setToolTip(it["desc"])
            self.tbl.setItem(r, 0, name)

            sz = QTableWidgetItem("统计中…")
            sz.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.tbl.setItem(r, 1, sz)

            self.tbl.setItem(r, 2, QTableWidgetItem())

            perm = QTableWidgetItem()
            perm.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.tbl.setItem(r, 3, perm)

            self._paint_row(r)

    # 排序用权重：风险「越能安全动手越靠前」；权限「普通 → 需要管理员 → 打开设置」
    RISK_RANK = {"safe": 0, "caution": 1, "migrate": 2, "keep": 3, "danger": 4}
    PERM_RANK = {"普通权限": 0, "需要管理员": 1, "打开设置": 2}

    def _paint_row(self, r):
        """风险 / 权限两列的彩色文本。主题切换时会整个重画一遍。

        行是按第 0 列的 id 反查的，不依赖行号 —— 这样点表头排序后
        重画也不会把风险文字画到别的行上。
        """
        nm = self.tbl.item(r, 0)
        it = ITEM_BY_ID.get(nm.data(Qt.UserRole)) if nm else None
        if not it:
            return
        p = self.pal
        risk = it.get("risk", "keep")
        rc = QColor((p.get("risk") or {}).get(risk) or "#8b95a6")

        rk = self.tbl.item(r, 2)
        if rk is None:
            rk = QTableWidgetItem()
            self.tbl.setItem(r, 2, rk)
        rk.setText(RISK_TEXT.get(risk, risk))
        rk.setForeground(rc)
        rk.setToolTip(it["desc"])
        rk.setTextAlignment(Qt.AlignCenter)
        rk.setData(Qt.UserRole, self.RISK_RANK.get(risk, 9))     # 排序用

        perm = self.tbl.item(r, 3)
        if perm is None:
            perm = QTableWidgetItem()
            self.tbl.setItem(r, 3, perm)
        if it.get("open"):
            txt, col = "打开设置", p.get("text_dim")
        elif it.get("admin"):
            txt, col = "需要管理员", (p.get("risk") or {}).get("caution")
        else:
            txt, col = "普通权限", p.get("text_dim")
        perm.setText(txt)
        perm.setForeground(QColor(col or "#8b95a6"))
        perm.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        perm.setData(Qt.UserRole, self.PERM_RANK.get(txt, 9))    # 排序用

        if it.get("note"):
            nm = self.tbl.item(r, 0)
            if nm is not None:
                nm.setToolTip(f"{it['desc']}\n\n注意：{it['note']}")

    # ---------------- 底部操作条 ----------------
    def _selected(self):
        rows = self.tbl.selectionModel().selectedRows()
        if not rows:
            return None
        it0 = self.tbl.item(rows[0].row(), 0)
        if not it0:
            return None
        return ITEM_BY_ID.get(it0.data(Qt.UserRole))

    def _sync_bottom(self):
        """选中项变了：更新说明、按钮文案与按钮级别（颜色随风险走）。"""
        it = self._selected()
        p = self.pal
        if not it:
            self.lb_sel.setText("在上面选中一项，右侧按钮会告诉你将要做什么")
            self.btn_run.setEnabled(False)
            self.btn_run.setText("执行操作")
            self.btn_run.setObjectName("")
            _restyle(self.btn_run)
            self.btn_copy.setEnabled(False)
            return
        size = self.sizes.get(it["id"])
        if size is None:
            sz_txt = "占用未统计"
        elif size == 0:
            sz_txt = "0 B"
        else:
            sz_txt = fmt_size(size)
        risk = it.get("risk", "keep")
        rc = (p.get("risk") or {}).get(risk) or "#8b95a6"
        note = f"（{it['note']}）" if it.get("note") else ""
        self.lb_sel.setText(
            f"<b>{it['name']}</b> · 占用 <b>{sz_txt}</b>{note}<br>"
            f"<span style='color:{rc}'>风险：{RISK_TEXT.get(risk, risk)}</span>"
            f"<span style='color:{p.get('text_dim')}'> — {it['desc']}</span>")
        self.btn_run.setText(it["action"])
        self.btn_run.setObjectName(
            "danger" if risk == "danger" else "warn" if risk == "caution" else "primary")
        _restyle(self.btn_run)
        self.btn_run.setEnabled(True)
        self.btn_copy.setEnabled(True)

    def _copy_cmd(self):
        it = self._selected()
        if not it:
            return
        cmd = it.get("cmd", "（无命令）")
        QApplication.clipboard().setText(cmd)
        QMessageBox.information(self, "已复制命令",
                                f"下面这条就是点「{it['action']}」时实际执行的命令：\n\n{cmd}")

    def _run_selected(self):
        it = self._selected()
        if not it:
            return
        self._run(it["id"])

    # ---------------- 统计 ----------------
    def showEvent(self, e):
        super().showEvent(e)
        # 第一次切到这页才统计，避免启动时就扫大目录
        if self._first:
            self._first = False
            QTimer.singleShot(200, self.refresh)

    def refresh(self):
        """重新统计各项占用并刷新列表。"""
        if self.thread and self.thread.isRunning():
            return
        self.sizes = {}
        self.btn_refresh.setEnabled(False)
        for r in range(self.tbl.rowCount()):
            sz = self.tbl.item(r, 1)
            if sz is not None:
                sz.setText("统计中…")
                sz.setData(Qt.UserRole, 0)
        self.delegate.set_bar_max(1)
        self.banner.start("正在统计系统占用", "准备中…", on_stop=self._stop)
        self.thread = StatThread(ITEMS, self)
        self.thread.one.connect(self._on_one)
        self.thread.done.connect(self._on_done)
        self.thread.start()

    def _stop(self):
        if self.thread:
            self.thread.cancel()
        self.btn_refresh.setEnabled(True)
        self.banner.hide()

    def _on_one(self, item_id, size):
        self.sizes[item_id] = size
        it = ITEM_BY_ID.get(item_id, {})
        for r in range(self.tbl.rowCount()):
            nm = self.tbl.item(r, 0)
            if nm and nm.data(Qt.UserRole) == item_id:
                sz = self.tbl.item(r, 1)
                if sz is None:
                    sz = QTableWidgetItem()
                    sz.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.tbl.setItem(r, 1, sz)
                sz.setText(fmt_size(size))
                sz.setToolTip(f"{fmt_bytes(size)} 字节")
                sz.setData(Qt.UserRole, size)      # 给占比条用
                break
        mx = max(self.sizes.values() or [1])
        self.delegate.set_bar_max(mx)
        self.tbl.viewport().update()
        self.banner.set_sub(f"已统计 {len(self.sizes)} / {len(ITEMS)} 项…")
        self._sync_bottom()

    def _on_done(self):
        self.btn_refresh.setEnabled(True)
        mx = max(self.sizes.values() or [1])
        self.delegate.set_bar_max(mx)
        self.tbl.viewport().update()
        total = sum(v for k, v in self.sizes.items()
                    if ITEM_BY_ID.get(k, {}).get("risk") != "danger")
        self.lb_total.setText(
            f"已统计占用合计 {fmt_size(sum(self.sizes.values()))}"
            f" · 其中低风险项 {fmt_size(total)}")
        # 统计期间不重排（行会乱跳），统计完再按当前排序收一次
        if self.sorter.active:
            self.sorter.sort_now()
        self.banner.finish("统计完成", f"{len(self.sizes)} 项")
        QTimer.singleShot(1500, self.banner.hide)
        self._sync_bottom()

    # ---------------- 执行 ----------------
    def _run(self, item_id):
        it = ITEM_BY_ID.get(item_id)
        if not it:
            return
        # 一律先确认：以前表格里直接放按钮，一点就跑，太突然
        conf = it.get("confirm") or f"即将执行：{it['action']}\n\n确定继续吗？"
        if QMessageBox.question(self, "确认操作", conf) != QMessageBox.Yes:
            return
        if it.get("recycle"):
            self._empty_recycle()
            return
        cmd = it.get("cmd", "")
        if it.get("open"):
            try:
                subprocess.Popen(f"start {cmd}", shell=True)
            except Exception as e:                                # noqa: BLE001
                QMessageBox.warning(self, "打开失败", str(e))
            return
        if it.get("admin"):
            self._runas(cmd)
        else:
            try:
                subprocess.Popen(cmd, shell=True)
            except Exception as e:                                # noqa: BLE001
                QMessageBox.warning(self, "执行失败", str(e))
        QTimer.singleShot(3000, self.refresh)

    def _runas(self, cmd):
        """用 ShellExecute(runas) 弹 UAC 提权执行，不常驻、不留后台进程。"""
        try:
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "cmd.exe", f"/c {cmd}", None, 1)
            if rc <= 32:
                QMessageBox.information(
                    self, "需要管理员权限",
                    f"没能以管理员身份启动（错误码 {rc}）。\n"
                    "可以右键「以管理员身份运行」本程序，再点这个按钮。")
        except Exception as e:                                    # noqa: BLE001
            QMessageBox.warning(self, "执行失败", str(e))

    def _empty_recycle(self):
        """清空各盘回收站：走系统 API，不需要管理员。"""
        flag = 0x0001 | 0x0002 | 0x0004   # 不确认 / 不显示进度 / 不出声
        try:
            for letter, _, _ in system.get_drives():
                ctypes.windll.shell32.SHEmptyRecycleBinW(None, letter, flag)
        except Exception as e:                                    # noqa: BLE001
            QMessageBox.warning(self, "清空回收站失败", str(e))
        QTimer.singleShot(1500, self.refresh)

    # ---------------- 主题 ----------------
    def on_theme(self, theme):
        """主题切换：重取调色板并刷新本页。"""
        self.pal = palette(theme)
        self.banner.set_palette(self.pal)
        self.delegate.set_palette(self.pal)
        for r in range(self.tbl.rowCount()):
            self._paint_row(r)
        self._sync_bottom()
