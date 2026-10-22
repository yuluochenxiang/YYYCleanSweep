"""冒烟测试：校验勾选保持、子项勾选、扫描停止/继续、操作日志、分类高度。

日志同时写到 _smoke.log（管道会因缓冲吞掉输出）。
"""
import os
import sys
import time
import warnings

# 构造 QDragEnterEvent 在 PySide6 6.12 被标了 DeprecationWarning（功能正常），
# 测试输出里不必带着这行噪音
warnings.filterwarnings("ignore", category=DeprecationWarning)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_smoke.log")
_lines = []


def log(*a):
    """打一行并整份重写日志文件（不是追加）。

    整份重写是为了让日志随时可读：中途崩了也能看到已经跑到哪一步，
    而不是留一个没写完的半行。
    """
    s = " ".join(str(x) for x in a)
    _lines.append(s)
    print(s, flush=True)
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(_lines))


from PySide6.QtWidgets import (                      # noqa: E402
    QApplication, QTabWidget, QPushButton,
)
from PySide6.QtCore import Qt                        # noqa: E402

app = QApplication(sys.argv)
from ui.main_window import MainWindow                # noqa: E402
from core.journal import Journal, log_path           # noqa: E402

w = MainWindow()
w.show()
log("[0] 启动 OK | 主题:", w.theme, "| 规则数:", len(w.rules))
log("    扫描按钮可见性  scan/stop/resume:",
    w.btn_scan.isVisibleTo(w), w.btn_stop.isVisibleTo(w), w.btn_resume.isVisibleTo(w))
log("    按钮 objectName:", w.btn_scan.objectName(), w.btn_stop.objectName(),
    w.btn_resume.objectName(), w.btn_clean.objectName())

# ---------------------------------------------------------------- 造一批假结果
w.rules = w.rules[:10]
w.by_id = {r["id"]: r for r in w.rules}
w.results = {}
# 分类是持久化的（会记住上次选的），冒烟里固定成"全部"才测得到全部规则
w.current_cat = "all"
for i, r in enumerate(w.rules):
    n = 3 if i % 3 == 0 else 1                      # 每 3 条造一个多目录规则
    per = [{"path": rf"C:\fake\dir{i}_{k}", "size": (k + 1) * 200 * 1024 ** 2,
            "count": k + 1} for k in range(n)]
    w.results[r["id"]] = {
        "size": sum(x["size"] for x in per), "count": sum(x["count"] for x in per),
        "targets": [x["path"] for x in per], "per": per}
w._refresh_categories()
w._refresh_tree()
log("[1] 假数据渲染行数:", w.tree.topLevelItemCount())
multi = [i for i in range(w.tree.topLevelItemCount())
         if w.tree.topLevelItem(i).childCount() > 1]
log("    多目录规则（应有子项）:", len(multi))


def wait_scan(start_timeout=120, end_timeout=1200):
    """先等线程真正跑起来，再等它结束（否则一启动就判断会得到假结果）。"""
    for _ in range(start_timeout):
        app.processEvents()
        if w.scan_thread and w.scan_thread.isRunning():
            break
        time.sleep(0.05)
    for _ in range(end_timeout):
        app.processEvents()
        if not (w.scan_thread and w.scan_thread.isRunning()):
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------- 1 排序不清勾选
it0 = w.tree.topLevelItem(0)
rid0 = it0.data(0, Qt.UserRole)
it0.setCheckState(0, Qt.Checked)
it0.setExpanded(True)
before = set(w.checked)
before_sel = w.lb_sel.text()
w.tree_sorter._on_clicked(1)   # 按风险（等价于点表头）
w.tree_sorter._on_clicked(2)   # 按占用
w.tree_sorter._on_clicked(0)   # 按名称
after = set(w.checked)
ok_keep = before == after  # 多目录规则用 "rid|路径" 子键，排序后勾选集合应完全一致
hit = [i for i in range(w.tree.topLevelItemCount())
       if w.tree.topLevelItem(i).data(0, Qt.UserRole) == rid0]
state = w.tree.topLevelItem(hit[0]).checkState(0) if hit else None
log("[2] 排序后勾选保持:", ok_keep, "| 该项状态:", state,
    "| 选择标签:", w.lb_sel.text()[:40],
    "| before/after 键数:", len(before), len(after))
log("    排序前:", before_sel[:40])

# ---------------------------------------------------------------- 3 子项单独勾选
# 排序之后索引会变，这里重新找一次带子项的规则
multi = [i for i in range(w.tree.topLevelItemCount())
         if w.tree.topLevelItem(i).childCount() > 1]
if multi:
    parent = w.tree.topLevelItem(multi[0])
    prid = parent.data(0, Qt.UserRole)
    parent.setExpanded(True)
    n_child = parent.childCount()
    parent.setCheckState(0, Qt.Checked)             # 全选
    full = [p for rid, ps in w._selected_plan() if rid == prid for p in ps]
    parent.child(0).setCheckState(0, Qt.Unchecked)  # 只取消其中一个子目录
    st = parent.checkState(0)
    left = [p for rid, ps in w._selected_plan() if rid == prid for p in ps]
    log("[3] 子项勾选: 子项数", n_child, "| 全选后路径数", len(full),
        "| 取消一个后父项状态", st, "| 应为半选:", Qt.PartiallyChecked)
    log("    剩余路径数:", len(left), "(应为", n_child - 1, ")")
    log("    计划条目数:", len(w._selected_plan()), "| 预计释放:", w._plan_bytes())

# ---------------------------------------------------------------- 4 全不选 / 只选安全
w._select_safe()
log("[4] 只选安全项 ->", w.lb_sel.text()[:44])
w._select_none()
log("    全不选 ->", w.lb_sel.text()[:30])

# ---------------------------------------------------------------- 5 扫描停止 / 继续
w.rules = w.rules[:6]
w.by_id = {r["id"]: r for r in w.rules}
w.results = {}
w.scan_pending = []
w.start_scan(False)
time.sleep(0.25)
w.stop_scan()
wait_scan()
pending_after_stop = len(w.scan_pending)
done_after_stop = len(w.results)
log("[5] 停止后: 已扫", done_after_stop, "| 剩余", pending_after_stop,
    "| 继续按钮可见", w.btn_resume.isVisibleTo(w),
    "| 停止按钮可见", w.btn_stop.isVisibleTo(w))
w.start_scan(True)
wait_scan()
log("    继续后: 已扫", len(w.results), "| 剩余", len(w.scan_pending),
    "| 继续按钮可见", w.btn_resume.isVisibleTo(w))

# ---------------------------------------------------------------- 6 操作日志
jr = Journal()
p = log_path()
jr.begin([("测试项目", [r"C:\fake\cache"], 123456)], "移到回收站")
jr.rule_done("测试项目", 123456, 2, [r"C:\fake\cache\a.bin", r"C:\fake\cache\b.bin"],
             ["C:\\fake\\cache\\c.bin: 被占用"])
jr.end(123456, 2, "移到回收站")
txt = jr.tail(4000)
log("[6] 日志文件:", p, "| 存在:", os.path.exists(p))
log("    含目标路径:", r"C:\fake\cache" in txt,
    "| 含已处理路径:", "a.bin" in txt, "| 含失败记录:", "被占用" in txt)

# ---------------------------------------------------------------- 7 分类高度
h = w.cat_delegate.row_h
vh = w.cat_list.viewport().height()
n = w.cat_list.count()
log("[7] 分类项高:", h, "| 视口高:", vh, "| 项数:", n,
    "| 估算铺满:", f"{h * n}/{vh}")

# ---------------------------------------------------------------- 8 主题切换
w.toggle_theme()
log("[8] 切到:", w.theme, "| QSS 含 corner:",
    "QTableCornerButton" in app.styleSheet(),
    "| 含 warn 按钮:", "QPushButton#warn" in app.styleSheet())
w.toggle_theme()
log("    切回:", w.theme)

w.save_state()
log("[9] 保存配置 OK | checked 条数:", len(w.cfg.data.get("checked", [])))

# ---------------------------------------------------------------- 10 子项专属详情
# 选中一个多目录规则的子项，详情应显示该子目录的专属说明，而非父规则笼统描述
# 前面的真扫描会把树清空（这台机器上可能一个都没扫到），这里补一份假数据
if not any(w.tree.topLevelItem(i).childCount() > 1
           for i in range(w.tree.topLevelItemCount())):
    r0 = w.rules[0]
    per = [{"path": r"C:\Users\test\AppData\Local\Temp", "size": 300 * 1024 ** 2,
            "count": 10},
           {"path": r"C:\Users\test\AppData\Local\CrashDumps", "size": 200 * 1024 ** 2,
            "count": 4}]
    w.results[r0["id"]] = {"size": sum(x["size"] for x in per),
                           "count": sum(x["count"] for x in per),
                           "targets": [x["path"] for x in per], "per": per}
    w._refresh_tree()
multi = [i for i in range(w.tree.topLevelItemCount())
         if w.tree.topLevelItem(i).childCount() > 1]
if multi:
    pit = w.tree.topLevelItem(multi[0])
    prid = pit.data(0, Qt.UserRole)
    c0 = pit.child(0)
    cpath = c0.data(0, Qt.UserRole + 1)
    w._render_detail(prid, c0.data(0, Qt.UserRole + 1) or "")
    txt = w.tx_detail.toPlainText()
    leaf = (cpath or "").rstrip("\\").rsplit("\\", 1)[-1]
    log("[10] 子项详情: 含『这是什么』区块:", "这是什么" in txt,
        "| 含子目录名:", leaf in txt,
        "| 含完整路径:", "完整路径" in txt)
    log("     父规则描述是否被弱化为子目录用途:", "这是什么" in txt and "完整路径" in txt)
# ---------------------------------------------------------------- 11 音效
import tempfile                                                   # noqa: E402
from core.sound import Sfx                                        # noqa: E402
sfx = Sfx(enabled=True, volume=0.4)
sfx.prepare()
wavs = [n for n in ("click", "check_on", "clean_done")
        if os.path.exists(os.path.join(sfx.dir or "", n + ".wav"))]
sfx.configure(enabled=False)
log("[11] 音效: winsound 可用", Sfx.supported(), "| 已生成 wav:", wavs)
log("     主窗口已挂音效:", getattr(w, "sfx", None) is not None,
    "| 开关:", w.cfg.get("sound"))

# ---------------------------------------------------------------- 12 统一选中 + 三角
from ui.widgets import RowDelegate, BigCheckStyle                 # noqa: E402
from PySide6.QtGui import QPixmap                                 # noqa: E402
log("[12] 委托统一: tree", isinstance(w.tree.itemDelegate(), RowDelegate),
    "| 大文件", isinstance(w.tw_big.itemDelegate(), RowDelegate),
    "| 历史表", isinstance(w.tbl_hist.itemDelegate(), RowDelegate))
log("     树可展开(三角):", w.tree.rootIsDecorated(),
    "| 勾选框风格:", isinstance(w.check_style, BigCheckStyle))
pm = QPixmap(max(200, w.tree.width()), max(120, w.tree.height()))
pm.fill()
w.tree.render(pm)          # 真正走一遍绘制：行背景 / 三角 / 勾选框都会被调用
log("     整树渲染:", not pm.isNull(), "| 尺寸:", pm.width(), pm.height())

# ---------------------------------------------------------------- 13 关于 / 帮助
from ui.about import AboutDialog                                  # noqa: E402
try:
    dlg = AboutDialog(w, w)
    tabs = dlg.findChild(QTabWidget)
    log("[13] 关于对话框: 页签数", tabs.count() if tabs else -1,
        "| 标题:", dlg.windowTitle())
    dlg.close()
except Exception as e:                                            # noqa: BLE001
    log("[13] 关于对话框失败:", e)

# ---------------------------------------------------------------- 14 清理总结报告
from ui.report import CleanReportDialog                           # noqa: E402
try:
    st = {"freed": 3 * 1024 ** 3, "count": 42, "errors": [],
          "per_rule": [("浏览器缓存", 2 * 1024 ** 3, 30),
                       ("临时文件", 1024 ** 3, 12)]}
    rd = CleanReportDialog(st, "移到回收站", 12.5, 100 * 1024 ** 3,
                           103 * 1024 ** 3, None, w.theme, w)
    log("[14] 总结报告: 标题", rd.windowTitle(),
        "| 最小宽", rd.minimumWidth())
    rd.close()
except Exception as e:                                            # noqa: BLE001
    log("[14] 总结报告失败:", e)

# ---------------------------------------------------------------- 15 重复文件
from core.dupfinder import (DupFinder, MATCH_QUICK,                # noqa: E402
                            pick_keeper, RULE_CTIME_OLD,
                            RULE_MTIME_NEW)
try:
    tmp = tempfile.mkdtemp(prefix="dupsmoke_")
    sub = os.path.join(tmp, "sub")
    os.makedirs(sub, exist_ok=True)
    same = b"x" * 4096
    for p in (os.path.join(tmp, "a.bin"), os.path.join(sub, "b.bin")):
        with open(p, "wb") as f:
            f.write(same)
    with open(os.path.join(tmp, "c.bin"), "wb") as f:
        f.write(b"y" * 100)
    fd = DupFinder(tmp, tmp, 1, MATCH_QUICK)
    groups = fd.scan()
    log("[15] 重复文件: 组数", len(groups),
        "| 组内文件数", [len(g["files"]) for g in groups],
        "| 可释放", [g["waste"] for g in groups])
    if groups:
        g0 = groups[0]
        k = pick_keeper(g0["files"], RULE_CTIME_OLD, [])
        log("     保留(创建最早):", os.path.basename(k["path"]),
            "| 优先目录命中:",
            bool(pick_keeper(g0["files"], RULE_MTIME_NEW, [sub])
                 ["path"].startswith(sub)))
    log("     页签已加入:", w.tabs.indexOf(w.dup_page) >= 0,
        "| 页签名:", w.tabs.tabText(w.tabs.indexOf(w.dup_page)))
except Exception as e:                                            # noqa: BLE001
    log("[15] 重复文件失败:", e)

# ---------------------------------------------------------------- 16 图表控件
from ui.charts import Donut, TypeBars                             # noqa: E402
log("[16] 图表: 目录分析环形图", isinstance(w.dir_page.donut, Donut),
    "| 类型条", isinstance(w.dir_page.bars, TypeBars),
    "| 大文件环形图", isinstance(w.big_donut, Donut))
try:
    w.dir_page.bars.set_items([("视频", "#e06c75", 500, 2),
                               ("压缩包", "#61afef", 300, 5)], 800)
    log("     类型条行数:", len(w.dir_page.bars.rows))
except Exception as e:                                            # noqa: BLE001
    log("     类型条失败:", e)

# ---------------------------------------------------------------- 17 展开三角
from PySide6.QtGui import QPixmap, QPainter                        # noqa: E402
from PySide6.QtWidgets import QStyleOption, QStyle                 # noqa: E402
log("[17] 展开三角: 清理树", w.tree.rootIsDecorated(),
    "| 大文件树", w.tw_big.rootIsDecorated(),
    "| 目录分析树", w.dir_page.tw.rootIsDecorated(),
    "| 重复文件树", w.dup_page.tw.rootIsDecorated())
try:
    # 把三角画到一张离屏图上，确认自绘真的产出像素（不是全透明）
    def _draw_pe(pe, state):
        pm = QPixmap(24, 24)
        pm.fill(Qt.transparent)
        opt = QStyleOption()
        opt.rect = pm.rect().translated(0, 0)
        opt.state = state
        p = QPainter(pm)
        w.check_style.drawPrimitive(pe, opt, p)
        p.end()
        return not pm.toImage().allGray()

    branch_open = _draw_pe(QStyle.PE_IndicatorBranch,
                           QStyle.State_Children | QStyle.State_Open)
    branch_closed = _draw_pe(QStyle.PE_IndicatorBranch,
                             QStyle.State_Children)
    arrow_down = _draw_pe(QStyle.PE_IndicatorArrowDown, QStyle.State_Enabled)
    log("     自绘有像素: 展开三角", branch_open, "| 折叠三角", branch_closed,
        "| 下拉三角", arrow_down)
except Exception as e:                                            # noqa: BLE001
    log("     三角自绘失败:", e)

# ---------------------------------------------------------------- 18 右键菜单
try:
    from PySide6.QtWidgets import QTreeWidgetItem                  # noqa: E402
    w.current_cat = "all"
    w._refresh_tree()
    it_top = w.tree.topLevelItem(0)
    m_top = w._make_tree_menu(it_top)
    it_sub = None
    for i in range(w.tree.topLevelItemCount()):
        t = w.tree.topLevelItem(i)
        if t.childCount() > 1:
            it_sub = t.child(0)
            break
    m_sub = w._make_tree_menu(it_sub) if it_sub else None
    log("[18] 右键菜单: 清理父项", len(m_top.actions()),
        "| 清理子项", len(m_sub.actions()) if m_sub else -1)
    log("     子项菜单含『打开目录』:",
        any("打开目录" in a.text() for a in (m_sub.actions() if m_sub else [])))

    # 大文件树
    big = QTreeWidgetItem(["big.bin", "1 GB", "10%", "视频"])
    big.setData(0, Qt.UserRole, os.path.join(os.getcwd(), "main.py"))
    m_big = w._make_big_menu(big)
    log("     大文件菜单", len(m_big.actions()),
        "| 含『在资源管理器中显示』:",
        any("资源管理器" in a.text() for a in m_big.actions()))

    # 目录分析 / 我的标记
    m_dir = w.dir_page._make_menu(r"C:\Windows\Temp")
    m_marks = w.marks_page._make_menu(r"C:\Windows\Temp")
    log("     目录分析菜单", len(m_dir.actions()),
        "| 含『加入白名单』:",
        any("白名单" in a.text() for a in m_dir.actions()),
        "| 标记页菜单", len(m_marks.actions()))

    # 重复文件
    w.dup_page.groups = [{"size": 4096, "waste": 4096, "files": [
        {"path": os.path.join(os.getcwd(), "main.py"), "ctime": 0, "mtime": 0},
        {"path": os.path.join(os.getcwd(), "README.md"), "ctime": 0, "mtime": 0}]}]
    w.dup_page._auto_select()
    gi = w.dup_page.tw.topLevelItem(0)
    m_grp = w.dup_page._make_menu(gi)
    m_file = w.dup_page._make_menu(gi.child(0))
    log("     重复文件分组菜单", len(m_grp.actions()),
        "| 文件菜单", len(m_file.actions()))
    # 底部操作条：目录分析的标记按钮已挪到底部，不再埋在右侧滚动区
    # isHidden() 只看有没有被显式隐藏：页签没切过去时 isVisible() 恒为 False
    log("     目录分析底部条按钮数:",
        sum(1 for b in (w.dir_page.b_keep, w.dir_page.b_junk,
                        w.dir_page.b_note, w.dir_page.b_clear,
                        w.dir_page.b_open, w.dir_page.b_deep)
            if not b.isHidden()),
        "| 在底栏内:", w.dir_page.b_keep.parent() is not None)
except Exception as e:                                            # noqa: BLE001
    log("[18] 右键菜单失败:", e)

# ---------------------------------------------------------------- 19 扫描状态条
from ui.busy import ScanBanner                                     # noqa: E402
try:
    banners = {
        "清理页": getattr(w, "scan_banner", None),
        "大文件": getattr(w, "big_banner", None),
        "目录分析": w.dir_page.banner,
        "重复文件": w.dup_page.banner,
    }
    log("[19] 扫描状态条:",
        " ".join(f"{k}={isinstance(v, ScanBanner)}" for k, v in banners.items()))
    b = w.scan_banner
    b.set_palette(w.pal)
    b.start("正在扫描", "0/10", on_stop=lambda: None, maximum=10)
    b.set_sub("3/10 · Chrome 缓存", value=3, maximum=10)
    log("     运行中: 可见", b.isVisible(), "| 计时文本", b.lb_time.text(),
        "| 主文案", b.lb_main.text(), "| 停止按钮", b.btn_stop.isVisible())
    b.finish("扫描完成", "10 项")
    log("     完成后: 主文案", b.lb_main.text(), "| 副文案", b.lb_sub.text())
    b.hide()
except Exception as e:                                            # noqa: BLE001
    log("[19] 扫描状态条失败:", e)

# ---------------------------------------------------------------- 20 分级音效
from core.sound import VOICES                                      # noqa: E402
from ui.sound import sound_of_button, NullSfx                      # noqa: E402
try:
    names = {}
    for nm in ("primary", "danger", "warn", "ok", "toggle", "mini", ""):
        btn = QPushButton()
        btn.setObjectName(nm)
        names[nm or "(普通)"] = sound_of_button(btn)
    log("[20] 分级音效:", " ".join(f"{k}={v}" for k, v in names.items()))
    log("     级别数（不同音色）:", len(set(names.values())),
        "| 新音色都在表里:",
        all(v in VOICES for v in names.values()))
    log("     菜单音色齐全:",
        all(k in VOICES for k in
            ("menu", "nav", "confirm", "toggle", "delete", "keep", "copy")))
    NullSfx().play("click")
    log("     空音效器不报错: True")
except Exception as e:                                            # noqa: BLE001
    log("[20] 分级音效失败:", e)

# ---------------------------------------------------------------- 21 方块图 Treemap
import os                                                         # noqa: E402
from core.dirstat import DirTree                                  # noqa: E402
from ui.treemap import TreemapView, squarify                      # noqa: E402
try:
    vals = [50, 25, 15, 10]
    rects = squarify(vals, 0, 0, 200, 100)
    area = sum(w * h for _x, _y, w, h, _i in rects)
    log("[21] 方块图: squarify 矩形数", len(rects),
        "| 面积守恒:", abs(area - 200 * 100) < 1)

    page = w.dir_page
    log("     目录分析页挂了方块图:", isinstance(page.treemap, TreemapView),
        "| 切换按钮:", page.btn_view.text())

    # 真扫一个小目录，确认能按占用切出方块
    small = DirTree(os.getcwd())
    small.scan(max_depth=3, workers=8)
    tv = TreemapView()
    tv.resize(420, 300)
    tv.set_palette(page.pal)
    tv.set_tree(small, small.root)
    log("     扫描目录数:", small.scanned_dirs, "| 生成方块数:", len(tv.rects))

    # 三种配色模式：都要算得出图例，且底色与文字对比度必须达标（否则看不清）
    from ui.treemap import COLOR_MODES, _rel_luma                 # noqa: E402
    from PySide6.QtGui import QColor                              # noqa: E402

    def _ctr(bg, fg):
        a, b = _rel_luma(bg), _rel_luma(QColor(fg))
        hi, lo = (a, b) if a >= b else (b, a)
        return (hi + 0.05) / (lo + 0.05)

    def _best_ok(bg):
        pick = tv._text_color(bg).name()
        best = max(_ctr(bg, "#ffffff"), _ctr(bg, "#101418"))
        return pick in ("#ffffff", "#101418") and best >= 4.5

    for mode, label in COLOR_MODES:
        tv.set_color_mode(mode)
        lg = tv._legend
        log(f"     配色[{label}]: 图例 {len(lg)} 项 | 文字对比度 ≥4.5:1:",
            all(_best_ok(c) for _n, c, _s in lg))
    tv.set_color_mode("type")

    # 亮色主题下也要能配色
    from ui.theme import palette as _pal                          # noqa: E402
    tv.set_palette(_pal("light"))
    tv.set_tree(small, small.root)
    log("     亮色主题: 方块", len(tv.rects), "| 图例", len(tv._legend),
        "| 文字对比度 ≥4.5:1:", all(_best_ok(c) for _n, c, _s in tv._legend))
    tv.set_palette(page.pal)
    tv.set_tree(small, small.root)

    from PySide6.QtCore import QPoint                             # noqa: E402
    hit = None
    if tv.rects:
        x, y, ww, hh, p = tv.rects[0][:5]
        hit = tv._hit(QPoint(int(x + ww / 2), int(y + hh / 2)))
    log("     命中测试:", hit is not None,
        "| 命中即该矩形路径:", hit == (tv.rects[0][4] if tv.rects else None))

    # 视图切换：树表 -> 方块图 -> 切回
    i1 = page.stack.currentIndex()
    page._toggle_view()
    i2 = page.stack.currentIndex()
    page._toggle_view()
    log("     视图切换: 树表", i1, "-> 方块图", i2,
        "-> 切回", page.stack.currentIndex())
    tv.clear()
    log("     清空后方块数:", len(tv.rects))
except Exception as e:                                            # noqa: BLE001
    log("[21] 方块图失败:", e)

# ---------------------------------------------------------------- 22 系统瘦身页
from core import system                                           # noqa: E402
from core.sizefmt import fmt_bytes                                # noqa: E402
try:
    sc = w.sysclean_page
    log("[22] 系统瘦身: 页签已挂", sc is not None,
        "| 项目数:", sc.tbl.rowCount())
    ids = []
    for r in range(sc.tbl.rowCount()):
        nm = sc.tbl.item(r, 0)
        ids.append(str(nm.data(Qt.UserRole)) if nm else "?")
    log("     项目 id:", ", ".join(ids))
    log("     底部按钮组:", sc.btn_run.text(), "/", sc.btn_copy.text(), "/",
        sc.btn_refresh.text())
    log("     未选中时执行按钮禁用:", not sc.btn_run.isEnabled())
    sc.tbl.selectRow(0)
    log("     选中首行:", sc.btn_run.text(), "| 已启用:", sc.btn_run.isEnabled(),
        "| 级别:", sc.btn_run.objectName())
    sc.tbl.selectRow(2)      # 系统还原点 = 高风险，按钮应变成危险色
    log("     选中还原点:", sc.btn_run.text(), "| 级别:", sc.btn_run.objectName())
    rk = [sc.tbl.item(r, 2).text() for r in range(sc.tbl.rowCount())]
    rc = [sc.tbl.item(r, 2).foreground().color().name()
          for r in range(sc.tbl.rowCount())]
    log("     风险文本:", "/".join(rk))
    log("     风险颜色:", "/".join(rc), "| 分级着色:",
        len(set(rc)) > 1)
    sc.tbl.clearSelection()
    # 只做只读探测，绝不执行任何瘦身动作
    _on, hsize = system.hibernate_status()
    log("     只读探测: 休眠占用", fmt_bytes(hsize),
        "| 管理员:", system.is_admin(),
        "| 管理员提示:", sc.lb_admin.text())
except Exception as e:                                            # noqa: BLE001
    log("[22] 系统瘦身失败:", e)


# ---------------------------------------------------------------- 23 点表头排序
def col_values(view, col):
    """取一列显示值（表格 / 树通用，树只取顶层）。"""
    out = []
    if hasattr(view, "rowCount"):
        for r in range(view.rowCount()):
            it = view.item(r, col)
            out.append(it.text() if it else "")
    else:
        for i in range(view.topLevelItemCount()):
            out.append(view.topLevelItem(i).text(col))
    return out


def numbers(view, col):
    """把一列显示值换算成可比较的数字（容量 / 百分比 / 纯数字）。"""
    vals = []
    for t in col_values(view, col):
        n = parse_size(t)
        if n is None:
            n = parse_pct(t)
        if n is None:
            digits = "".join(ch for ch in t if ch.isdigit())
            n = float(digits or 0)
        vals.append(float(n))
    return vals


def noninc(a):
    """是不是「从大到小」（非递增）。排序冒烟用它验证降序结果。"""
    return all(a[i] >= a[i + 1] for i in range(len(a) - 1))


def nondec(a):
    """是不是「从小到大」（非递减）。"""
    return all(a[i] <= a[i + 1] for i in range(len(a) - 1))


try:
    from core.sizefmt import parse_size, parse_pct             # noqa: E402
    from PySide6.QtWidgets import (                            # noqa: E402
        QTreeWidget, QTreeWidgetItem, QTableWidget, QTableWidgetItem,
    )
    from ui.widgets import HeaderSorter                        # noqa: E402

    log("[23] 点表头排序:")
    numeric_cases = [
        ("清理项目/占用", w.tree, w.tree_sorter, 2),
        ("大文件/大小", w.tw_big, w.big_sorter, 1),
        ("重复文件/大小", w.dup_page.tw, w.dup_page.sorter, 1),
        ("目录分析/占用", w.dir_page.tw, w.dir_page.sorter, 1),
        ("系统瘦身/占用", w.sysclean_page.tbl, w.sysclean_page.sorter, 1),
        ("历史/释放", w.tbl_hist, w.hist_sorter, 2),
    ]
    for name, view, sorter, col in numeric_cases:
        n = len(col_values(view, col))
        if n < 2:
            log(f"     {name}: 行数 {n}（不足 2 行，顺序校验跳过）")
            continue
        sorter._on_clicked(col)                 # 第一次点：数值列默认降序
        d1 = noninc(numbers(view, col))
        sorter._on_clicked(col)                 # 再点一次：反向升序
        d2 = nondec(numbers(view, col))
        log(f"     {name}: 行数 {n} | 一次点击=降序 {d1} | 再点=升序 {d2}")

    # 合成的真实数值场景：验证 "1.2 GB > 900 MB"、层级与展开状态都保住
    tw = QTreeWidget()
    tw.setColumnCount(2)
    tw.setHeaderLabels(["名称", "大小"])
    st = HeaderSorter(tw, numeric_cols=(1,), default_col=1, default_desc=True)
    for nm, sz in (("a", "900 MB"), ("b", "1.2 GB"), ("c", "12 KB")):
        it = QTreeWidgetItem([nm, sz])
        it.setData(1, Qt.UserRole, parse_size(sz))       # 数值存在第 1 列
        tw.addTopLevelItem(it)
        if nm == "b":
            for cn, cs in (("b1", "5 MB"), ("b2", "40 MB")):
                c = QTreeWidgetItem([cn, cs])
                c.setData(1, Qt.UserRole, parse_size(cs))
                it.addChild(c)
    tw.topLevelItem(1).setExpanded(True)
    st.sort_now()
    order = [tw.topLevelItem(i).text(0) for i in range(tw.topLevelItemCount())]
    kids = [tw.topLevelItem(0).child(i).text(0)
            for i in range(tw.topLevelItem(0).childCount())]
    log("     合成树: 1.2GB 排最前", order == ["b", "a", "c"], order)
    log("     合成树: 层级保留 + 子层同样降序", kids == ["b2", "b1"], kids)
    log("     合成树: 展开状态保留", tw.topLevelItem(0).isExpanded())
    st.set_state(1, False)
    st.sort_now()
    log("     合成树: 再点转升序", [tw.topLevelItem(i).text(0)
                                  for i in range(tw.topLevelItemCount())]
        == ["c", "a", "b"])

    # 表格：default_col=None 时先不动，点表头才排
    tb = QTableWidget(0, 2)
    tb.setRowCount(4)
    st2 = HeaderSorter(tb, numeric_cols=(1,), default_col=None)
    for r, (nm, sz) in enumerate((("x", "2 GB"), ("y", "300 MB"),
                                  ("z", "1 GB"), ("w", "15 MB"))):
        tb.setItem(r, 0, QTableWidgetItem(nm))
        cell = QTableWidgetItem(sz)
        cell.setData(Qt.UserRole, parse_size(sz))
        tb.setItem(r, 1, cell)
    untouched = [tb.item(r, 0).text() for r in range(4)] == ["x", "y", "z", "w"]
    st2.set_state(1, True)
    st2.sort_now()
    after = [tb.item(r, 0).text() for r in range(4)]
    log("     合成表: default_col=None 时保持原顺序", untouched)
    log("     合成表: 点表头后按真实字节降序", after == ["x", "z", "y", "w"], after)
except Exception as e:                                            # noqa: BLE001
    log("[23] 表头排序失败:", e)


# ---------------------------------------------------------------- 24 统一目标 / 分页记忆
try:
    log("[24] 顶部统一目标:")
    for i in range(w.tabs.count()):
        w.tabs.setCurrentIndex(i)
        app.processEvents()
        name = w.tabs.tabText(i)
        log(f"     {name}: 目标区可见 {w.w_target.isVisibleTo(w.tabs)}"
            f" | 标签 {w.lb_target.text() if w.w_target.isVisibleTo(w.tabs) else '—'}"
            f" | 浏览按钮 {w.btn_target.isVisibleTo(w.tabs)}")
    # 各页独立记忆：改目录分析的目标，不能影响清理页
    dir_i = w.tabs.indexOf(w.dir_page)              # 目录分析（页序会调，别写死下标）
    w.tabs.setCurrentIndex(dir_i)
    w.dir_page.set_root(r"C:\Windows")
    w.cfg.set("target_dir", r"C:\Windows")
    w.tabs.setCurrentIndex(0)                       # 清理项目
    clean_now = w.cb_target.currentText()
    w.tabs.setCurrentIndex(dir_i)
    dir_now = w.cb_target.currentText()
    log("     独立记忆: 清理页", clean_now, "| 目录分析页", dir_now,
        "| 互不影响:", clean_now.rstrip("\\") != dir_now.rstrip("\\"))
    log("     目录分析已用新位置:", w.dir_page.root_path)
except Exception as e:                                            # noqa: BLE001
    log("[24] 统一目标失败:", e)


# ---------------------------------------------------------------- 25 亮色主题对比度
try:
    def _lum(hexv):
        hexv = hexv.lstrip("#")
        ch = [int(hexv[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(ch[0]) + 0.7152 * f(ch[1]) + 0.0722 * f(ch[2])

    def _ratio(a, b):
        l1, l2 = sorted([_lum(a), _lum(b)], reverse=True)
        return (l1 + 0.05) / (l2 + 0.05)

    from ui.theme import LIGHT as _L                            # noqa: E402
    rows = [("正文", _L["text"], "#ffffff", 7.0),
            ("次级", _L["text_dim"], "#ffffff", 4.5),
            ("提示", _L["text_faint"], "#ffffff", 4.5),
            ("次级(卡片)", _L["text_dim"], _L["panel2"], 4.5),
            ("提示(卡片)", _L["text_faint"], _L["panel2"], 4.5),
            ("选中字/底", _L["sel_fg"], _L["sel_bg"], 4.5),
            ("提示(页面底)", _L["text_faint"], _L["bg"], 4.5)]
    log("[25] 亮色主题对比度:")
    allok = True
    for name, fg, bg, need in rows:
        r = _ratio(fg, bg)
        if r < need:
            allok = False
        log(f"     {name}: {r:.2f}:1 (需 ≥{need}) {fg} on {bg}")
    log("     全部达标:", allok)
except Exception as e:                                            # noqa: BLE001
    log("[25] 对比度检查失败:", e)

# ------------------------------------------------- 26 报告导出 HTML（P2 方向6）
try:
    import tempfile
    from ui.report import CleanReportDialog                     # noqa: E402
    stats = {"freed": 1234567890, "count": 7,
             "errors": ["示例：某文件被占用，已跳过"],
             "per_rule": [("微信内置浏览器缓存", 800 * 1024 * 1024, 120),
                          ("豆包缓存", 400 * 1024 * 1024, 60)]}
    dlg = CleanReportDialog(stats, mode="移到回收站", secs=12.5,
                            free_before=10 * 1024 ** 3,
                            free_after=11 * 1024 ** 3,
                            theme=w.theme, parent=w)
    html = dlg._build_html()
    left = [k for k in ("{head}", "{detail}", "{err}", "{log}", "{now}", "{app}")
            if k in html]
    log("[26] 报告导出 HTML: 长度", len(html),
        "| 占位符残留:", left or "无",
        "| 含项目明细:", "微信内置浏览器缓存" in html,
        "| 含清理方式:", "移到回收站" in html)
    fp = os.path.join(tempfile.gettempdir(), "_smoke_report.html")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(html)
    log("     写盘:", fp, os.path.getsize(fp), "字节")
    os.remove(fp)
    dlg.close()
except Exception as e:                                          # noqa: BLE001
    log("[26] 报告导出失败:", e)


# ------------------------------------------------- 27 拖拽目录分析（P2 方向7）
try:
    from PySide6.QtCore import QMimeData, QUrl, QPoint, QPointF  # noqa: E402
    from PySide6.QtGui import QDragEnterEvent, QDropEvent        # noqa: E402
    called = []
    w.dir_page.start_scan = lambda *a, **k: called.append(1)     # 别真扫，太慢
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(r"C:\Users")])
    ev = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction, md,
                         Qt.LeftButton, Qt.NoModifier)
    w.dragEnterEvent(ev)
    log("[27] 拖拽目录分析: 窗口接收拖放", w.acceptDrops(),
        "| dragEnter 已接受:", ev.isAccepted())
    dv = QDropEvent(QPointF(10, 10), Qt.CopyAction, md,
                    Qt.LeftButton, Qt.NoModifier)
    w.dropEvent(dv)
    app.processEvents()
    dir_i = w.tabs.indexOf(w.dir_page)          # 页序会调，别写死下标
    log("     落到目录分析页:", w.tabs.currentIndex() == dir_i,
        "| 目标:", w.dir_page.root_path,
        "| 已自动开扫:", bool(called))
    # 拖入文件时取父目录（用真实存在的文件，别用不存在的路径：那会走弹提示分支）
    here = os.path.dirname(os.path.abspath(__file__))
    md2 = QMimeData()
    md2.setUrls([QUrl.fromLocalFile(os.path.join(here, "README.md"))])
    w.dropEvent(QDropEvent(QPointF(10, 10), Qt.CopyAction, md2,
                           Qt.LeftButton, Qt.NoModifier))
    app.processEvents()
    log("     拖入文件时取父目录:", w.dir_page.root_path,
        "| 与所在目录一致:", os.path.normcase(w.dir_page.root_path or "")
        == os.path.normcase(here))
    # 无效路径：应给出提示而不是崩（提示框是模态的，打桩挡住，否则测试会卡住）
    from PySide6.QtWidgets import QMessageBox as _MB      # noqa: E402
    _orig_info = _MB.information
    _tips = []
    _MB.information = staticmethod(lambda *a, **k: _tips.append(a[-1] if a else ""))
    md3 = QMimeData()
    md3.setUrls([QUrl.fromLocalFile(r"Z:\不存在的目录\nope")])
    w.dropEvent(QDropEvent(QPointF(10, 10), Qt.CopyAction, md3,
                           Qt.LeftButton, Qt.NoModifier))
    app.processEvents()
    _MB.information = _orig_info
    log("     无效路径有提示不崩:", bool(_tips), "| 提示:", _tips or "—")
except Exception as e:                                          # noqa: BLE001
    log("[27] 拖拽检查失败:", e)

log("SMOKE_DONE")
