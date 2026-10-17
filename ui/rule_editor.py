"""清理规则编辑器（G 卷）。

界面上只做「展示 + 编辑 + 调度」，读写、校验、备份、恢复全部落在
`core/ruleedit.py`（那个模块不 import Qt，能被测试脚本直接调）。

一条隐性约定：**表格只是视图，`self.rules`（就是 json 里的 rules 数组本身）
才是唯一数据源**。任何会改动数据的动作（保存 / 新增 / 复制 / 删除 / 切行）
之前都先调 `_pull_from_table()` 把单元格内容写回，避免"改了没生效"。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QPlainTextEdit, QMessageBox, QStyledItemDelegate, QWidget,
)

from core import ruleedit as RE
from core.rules import MODE_LABEL, RISK_LABEL
from ui.theme import palette, DEFAULT_THEME
from ui.widgets import RowDelegate, enable_hover

COLS = ["id", "名称", "分类", "方式", "风险", "管理员", "路径"]
COL_ID, COL_NAME, COL_CAT, COL_MODE, COL_RISK, COL_ADMIN, COL_PATHS = range(7)

DETAILS = [("desc", "说明"), ("advice", "建议"),
           ("operation", "执行"), ("recovery", "恢复")]


class _ComboDelegate(QStyledItemDelegate):
    """给某一列挂下拉框。options: [(真实值, 显示文本), ...]

    真实值存 Qt.UserRole、中文存 DisplayRole —— 这样表格里看到的是人话，
    读回来的是 json 里认的取值，不用手写对照表（v3.27 的教训）。
    """

    def __init__(self, options, parent=None):
        super().__init__(parent)
        self.options = list(options)

    def createEditor(self, parent, option, index):
        cb = QComboBox(parent)
        for val, label in self.options:
            cb.addItem(label, val)
        return cb

    def setEditorData(self, editor, index):
        v = index.data(Qt.UserRole)
        if v is None:
            v = index.data(Qt.DisplayRole)
        i = editor.findData(v)
        editor.setCurrentIndex(i if i >= 0 else 0)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentData(), Qt.UserRole)
        model.setData(index, editor.currentText(), Qt.DisplayRole)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)


class RuleEditorDialog(QDialog):
    """清理规则编辑器。保存成功时 accept()，主窗口据此重新加载并重扫。"""

    def __init__(self, cfg=None, parent=None, theme=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("清理规则编辑器")
        self.resize(1120, 760)
        self.pal = palette(theme or DEFAULT_THEME)

        self.data = RE.load_raw()
        self.rules = self.data.setdefault("rules", [])
        self.cats = self.data.setdefault("categories", [])
        self._loading = False

        self._build()
        self._fill_table()
        self._update_count()

    # ------------------------------------------------------------ 构建
    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        # ---- 顶部：筛选 + 三个操作
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(QLabel("分类"))
        self.cb_cat = QComboBox()
        self.cb_cat.setMinimumWidth(150)
        self.cb_cat.addItem("全部", "")
        for c in self.cats:
            self.cb_cat.addItem(c.get("name") or c.get("id"), c.get("id"))
        self.cb_cat.currentIndexChanged.connect(self._apply_filter)
        top.addWidget(self.cb_cat)
        top.addSpacing(6)
        top.addWidget(QLabel("搜索"))
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("按 id / 名称 / 路径过滤")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.textChanged.connect(self._apply_filter)
        top.addWidget(self.ed_search, 1)
        top.addSpacing(6)
        b_add = QPushButton("新增")
        b_add.clicked.connect(self._on_add)
        b_copy = QPushButton("复制选中")
        b_copy.clicked.connect(self._on_copy)
        b_del = QPushButton("删除")
        b_del.setObjectName("danger")
        b_del.clicked.connect(self._on_delete)
        for b in (b_add, b_copy, b_del):
            top.addWidget(b)
        lay.addLayout(top)

        # ---- 表格
        self.t = QTableWidget(0, len(COLS))
        self.t.setHorizontalHeaderLabels(COLS)
        h = self.t.horizontalHeader()
        h.setMinimumSectionSize(52)
        for i, w in enumerate((150, 210, 110, 130, 76, 62)):
            h.setSectionResizeMode(i, QHeaderView.Interactive)
            self.t.setColumnWidth(i, w)
        h.setSectionResizeMode(COL_PATHS, QHeaderView.Stretch)
        self.t.setEditTriggers(QAbstractItemView.DoubleClicked
                               | QAbstractItemView.SelectedClicked
                               | QAbstractItemView.EditKeyPressed)
        self.t.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.t.setSelectionMode(QAbstractItemView.SingleSelection)
        self.t.verticalHeader().setVisible(False)
        self.t.setAlternatingRowColors(False)

        opts_cat = [(c.get("id"), c.get("name") or c.get("id")) for c in self.cats]
        opts_mode = list(MODE_LABEL.items())
        opts_risk = list(RISK_LABEL.items())
        self.t.setItemDelegateForColumn(COL_CAT, _ComboDelegate(opts_cat, self.t))
        self.t.setItemDelegateForColumn(COL_MODE, _ComboDelegate(opts_mode, self.t))
        self.t.setItemDelegateForColumn(COL_RISK, _ComboDelegate(opts_risk, self.t))
        self.row_delegate = RowDelegate(self.t, elide_col=COL_PATHS)
        self.row_delegate.set_palette(self.pal)
        self.t.setItemDelegate(self.row_delegate)
        enable_hover(self.t)
        self.t.currentCellChanged.connect(self._on_row_changed)
        lay.addWidget(self.t, 1)

        # ---- 下半：四个说明框
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 2, 0, 0)
        bl.setSpacing(6)
        self.ed_detail = {}
        for key, label in DETAILS:
            row = QHBoxLayout()
            row.setSpacing(8)
            lb = QLabel(label)
            lb.setFixedWidth(38)
            lb.setObjectName("hint")
            row.addWidget(lb)
            ed = QPlainTextEdit()
            ed.setFixedHeight(48)
            ed.setPlaceholderText("选中一行后在这里补充/修改「%s」" % label)
            row.addWidget(ed, 1)
            self.ed_detail[key] = ed
            bl.addLayout(row)
        lay.addWidget(box)

        # ---- 底部按钮
        bot = QHBoxLayout()
        bot.setSpacing(8)
        self.lb_count = QLabel("")
        self.lb_count.setObjectName("hint")
        bot.addWidget(self.lb_count)
        bot.addStretch(1)
        b_reset = QPushButton("恢复内置默认")
        b_reset.clicked.connect(self._on_restore_builtin)
        b_backup = QPushButton("备份当前")
        b_backup.clicked.connect(self._on_backup)
        b_save = QPushButton("保存")
        b_save.setObjectName("primary")
        b_save.clicked.connect(self._on_save)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.reject)
        for b in (b_reset, b_backup, b_save, b_close):
            bot.addWidget(b)
        lay.addLayout(bot)

    # ------------------------------------------------------------ 表格
    def _fill_table(self):
        """按 self.rules 重建整张表（行号与列表下标一一对应）。"""
        self._loading = True
        self.t.blockSignals(True)
        self.t.setRowCount(0)
        for i, r in enumerate(self.rules):
            self.t.insertRow(i)
            it_id = QTableWidgetItem(str(r.get("id", "")))
            it_id.setData(Qt.UserRole, i)          # 行 -> rules 下标
            self.t.setItem(i, COL_ID, it_id)
            self.t.setItem(i, COL_NAME, QTableWidgetItem(str(r.get("name", ""))))

            cid = r.get("category", "")
            it_cat = QTableWidgetItem(self._cat_name(cid))
            it_cat.setData(Qt.UserRole, cid)
            self.t.setItem(i, COL_CAT, it_cat)

            md = r.get("mode", "")
            it_mode = QTableWidgetItem(MODE_LABEL.get(md, md))
            it_mode.setData(Qt.UserRole, md)
            self.t.setItem(i, COL_MODE, it_mode)

            rk = r.get("risk", "")
            it_risk = QTableWidgetItem(RISK_LABEL.get(rk, rk))
            it_risk.setData(Qt.UserRole, rk)
            col = self.pal.get("risk", {}).get(rk)
            if col:
                from PySide6.QtGui import QColor
                it_risk.setForeground(QColor(col))
            self.t.setItem(i, COL_RISK, it_risk)

            it_adm = QTableWidgetItem("")
            it_adm.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled
                            | Qt.ItemIsUserCheckable)
            it_adm.setCheckState(Qt.Checked if r.get("admin") else Qt.Unchecked)
            self.t.setItem(i, COL_ADMIN, it_adm)
            paths = r.get("paths") or []
            it_p = QTableWidgetItem("; ".join(str(p) for p in paths))
            if len(paths) > 2:
                it_p.setToolTip("\n".join(str(p) for p in paths))
            self.t.setItem(i, COL_PATHS, it_p)
        self.t.blockSignals(False)
        self._loading = False
        self._apply_filter()

    def _cat_name(self, cid):
        for c in self.cats:
            if c.get("id") == cid:
                return c.get("name") or cid
        return str(cid)

    def _pull_from_table(self):
        """把单元格里的内容写回 self.rules（表格是视图，列表才是数据源）。"""
        if self._loading:
            return
        from PySide6.QtGui import QColor  # noqa: F401  (保持与 _fill_table 一致)
        for row in range(self.t.rowCount()):
            it0 = self.t.item(row, COL_ID)
            if it0 is None:
                continue
            idx = it0.data(Qt.UserRole)
            if not isinstance(idx, int) or not (0 <= idx < len(self.rules)):
                continue
            r = self.rules[idx]
            r["id"] = (self.t.item(row, COL_ID).text() or "").strip()
            r["name"] = (self.t.item(row, COL_NAME).text() or "").strip()
            it_cat = self.t.item(row, COL_CAT)
            r["category"] = it_cat.data(Qt.UserRole) or self._cat_id_by_name(
                it_cat.text())
            it_mode = self.t.item(row, COL_MODE)
            r["mode"] = it_mode.data(Qt.UserRole) or it_mode.text()
            it_risk = self.t.item(row, COL_RISK)
            r["risk"] = it_risk.data(Qt.UserRole) or it_risk.text()
            it_adm = self.t.item(row, COL_ADMIN)
            r["admin"] = bool(it_adm and it_adm.checkState() == Qt.Checked)
            txt = self.t.item(row, COL_PATHS).text() or ""
            r["paths"] = [p.strip() for p in txt.split(";") if p.strip()]

    def _cat_id_by_name(self, name):
        for c in self.cats:
            if (c.get("name") or "") == name:
                return c.get("id")
        return name

    def _apply_filter(self, *a):
        kw = (self.ed_search.text() or "").strip().lower()
        cid = self.cb_cat.currentData() or ""
        for row in range(self.t.rowCount()):
            it0 = self.t.item(row, COL_ID)
            if it0 is None:
                continue
            idx = it0.data(Qt.UserRole)
            r = self.rules[idx] if isinstance(idx, int) and idx < len(self.rules) else {}
            ok_cat = (not cid) or (r.get("category") == cid)
            if kw:
                blob = " ".join([str(r.get("id", "")), str(r.get("name", "")),
                                 " ".join(str(p) for p in (r.get("paths") or []))])
                ok_kw = kw in blob.lower()
            else:
                ok_kw = True
            self.t.setRowHidden(row, not (ok_cat and ok_kw))

    def _update_count(self):
        self.lb_count.setText("共 %d 条规则" % len(self.rules))

    # ------------------------------------------------------------ 说明框
    def _on_row_changed(self, row, col, prow, pcol):
        """切行前先把上一行的四个说明框落回内存，否则改了没存就切走会丢。"""
        self._flush_detail(prow)
        self._load_detail(row)

    def _load_detail(self, row):
        r = self._rule_of_row(row)
        for key, _label in DETAILS:
            ed = self.ed_detail[key]
            ed.blockSignals(True)
            ed.setPlainText("" if r is None else str(r.get(key, "") or ""))
            ed.setReadOnly(r is None)
            ed.blockSignals(False)

    def _flush_detail(self, row=None):
        if row is None:
            row = self.t.currentRow()
        r = self._rule_of_row(row)
        if r is None:
            return
        for key, _label in DETAILS:
            r[key] = self.ed_detail[key].toPlainText()

    def _rule_of_row(self, row):
        if row is None or row < 0:
            return None
        it0 = self.t.item(row, COL_ID)
        if it0 is None:
            return None
        idx = it0.data(Qt.UserRole)
        if isinstance(idx, int) and 0 <= idx < len(self.rules):
            return self.rules[idx]
        return None

    # ------------------------------------------------------------ 操作
    def _on_add(self):
        self._pull_from_table()
        self._flush_detail()
        cid = self.cb_cat.currentData() or (
            self.cats[0].get("id") if self.cats else "")
        r = RE.new_rule(cid, [x.get("id") for x in self.rules])
        self.rules.append(r)
        self._fill_table()
        self._update_count()
        row = len(self.rules) - 1
        self.t.selectRow(row)
        self.t.scrollToBottom()

    def _on_copy(self):
        self._pull_from_table()
        self._flush_detail()
        r = self._rule_of_row(self.t.currentRow())
        if r is None:
            QMessageBox.information(self, "复制选中", "先选中一行再复制。")
            return
        c = RE.copy_rule(r, [x.get("id") for x in self.rules])
        self.rules.insert(self.rules.index(r) + 1, c)
        self._fill_table()
        self._update_count()

    def _on_delete(self):
        self._pull_from_table()
        self._flush_detail()
        r = self._rule_of_row(self.t.currentRow())
        if r is None:
            QMessageBox.information(self, "删除", "先选中一行再删除。")
            return
        self.rules.remove(r)
        self._fill_table()
        self._update_count()

    def _on_backup(self):
        bak = RE.backup("_manual")
        if not bak:
            QMessageBox.warning(self, "备份当前", "读取规则文件失败，没备份成。")
            return
        QMessageBox.information(self, "备份当前", f"已备份到：\n{bak}")

    def _on_restore_builtin(self):
        if QMessageBox.question(
                self, "恢复内置默认",
                "这会用出厂规则覆盖当前规则文件，你改过的内容会全部丢失。\n\n"
                "（覆盖前建议先点一次「备份当前」）\n\n确定继续吗？",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        if not RE.restore_builtin():
            QMessageBox.warning(
                self, "恢复内置默认",
                "找不到内置的规则文件，没能恢复。\n"
                "源码运行时没有独立的出厂副本，这条只在打包版有效。")
            return
        self.data = RE.load_raw()
        self.rules = self.data.setdefault("rules", [])
        self.cats = self.data.setdefault("categories", [])
        self._fill_table()
        self._update_count()

    def _on_save(self):
        """校验 + 备份 + 写盘。返回 True 表示存成功（对话框已 accept）。"""
        self._pull_from_table()
        self._flush_detail()
        data = dict(self.data)
        data["rules"] = self.rules
        ok, errs, bak = RE.save_checked(data)
        if not ok:
            head = "\n".join(errs[:12])
            more = "" if len(errs) <= 12 else f"\n… 另有 {len(errs) - 12} 处问题"
            QMessageBox.warning(self, "规则有误，未保存",
                                head + more + "\n\n改好之后再点保存。")
            return False
        self.accept()
        return True
