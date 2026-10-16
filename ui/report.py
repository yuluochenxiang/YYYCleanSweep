"""清理总结报告：每次清理结束后弹出的小结，替代原来的纯文本弹窗。"""
import os
import time

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QTextBrowser, QMessageBox, QFileDialog,
)

from ui.theme import palette, DEFAULT_THEME
from ui.charts import TypeBars
from ui.widgets import fmt, esc
from core.journal import log_path
from core.version import APP_NAME

# 自包含 HTML 报告模板：内联 CSS，不依赖任何外部资源，可直接分享 / 打开。
# 用显式 .replace 注入，不用 str.format，避免和 CSS 大括号打架。
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{app} · 清理总结</title>
<style>
* { box-sizing: border-box; }
body { font-family: -apple-system, "Microsoft YaHei", "Segoe UI", Roboto, sans-serif;
  background: #eef1f5; color: #1f2733; margin: 0; padding: 28px; }
.wrap { max-width: 760px; margin: 0 auto; }
.card { background: #fff; border-radius: 14px; padding: 18px 20px; margin-bottom: 16px;
  box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.muted { color: #7a8699; font-size: 13px; }
.big { font-size: 38px; font-weight: 700; margin: 4px 0 10px; color: #1f2733; }
.statrow { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 6px; }
.stat { background: #eef2fb; color: #3358b5; border-radius: 8px; padding: 5px 10px; font-size: 13px; }
.h2 { font-size: 16px; font-weight: 600; margin-bottom: 12px; }
.barrow { margin: 10px 0; }
.barname { font-size: 14px; margin-bottom: 4px; display: flex; justify-content: space-between; }
.barsz { color: #7a8699; font-size: 12px; }
.track { background: #eef1f5; border-radius: 6px; height: 14px; overflow: hidden; }
.fill { height: 100%; border-radius: 6px; }
.err { margin: 8px 0 0; padding-left: 20px; color: #9a5b00; font-size: 13px; }
.err li { margin: 3px 0; }
.hint { color: #7a8699; font-size: 12px; word-break: break-all; }
footer { text-align: center; color: #9aa5b5; font-size: 12px; margin-top: 8px; }
</style></head>
<body><div class="wrap">
{head}
{detail}
{err}
{log}
<footer>生成于 {now} · {app}</footer>
</div></body></html>"""


class CleanReportDialog(QDialog):
    """stats: cleaner 返回的统计；mode 方式文字；secs 耗时秒。"""

    def __init__(self, stats, mode="", secs=0.0, free_before=0, free_after=0,
                 backup_root=None, theme=DEFAULT_THEME, parent=None):
        super().__init__(parent)
        self.pal = palette(theme)
        self.backup_root = backup_root
        # 留着给「导出 HTML」复用，避免重复算
        self._stats = stats
        self._mode = mode
        self._secs = secs
        self._free_before = free_before
        self._free_after = free_after
        self.setWindowTitle(f"{APP_NAME} · 清理总结")
        self.setMinimumWidth(560)

        p = self.pal
        freed = int(stats.get("freed", 0) or 0)
        count = int(stats.get("count", 0) or 0)
        errors = stats.get("errors") or []
        per = stats.get("per_rule") or []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        # ---- 头图：释放了多少
        head = QFrame()
        head.setObjectName("card")
        hl = QVBoxLayout(head)
        hl.setContentsMargins(18, 16, 18, 16)
        hl.setSpacing(6)
        lb_t = QLabel("本次清理释放")
        lb_t.setObjectName("muted")
        hl.addWidget(lb_t)
        lb_big = QLabel(fmt(freed))
        lb_big.setObjectName("big")
        hl.addWidget(lb_big)
        row = QHBoxLayout()
        row.setSpacing(8)
        for txt in (f"处理 {count} 个条目",
                    f"方式：{mode or '—'}",
                    f"耗时 {secs:.1f} 秒"):
            lb = QLabel(txt)
            lb.setObjectName("stat")
            row.addWidget(lb)
        row.addStretch(1)
        hl.addLayout(row)
        if free_before and free_after and free_after > free_before:
            lb_delta = QLabel(
                f"磁盘可用空间：{fmt(free_before)} → "
                f"<b style='color:{p['risk']['safe']}'>{fmt(free_after)}</b>"
                f"　（+{fmt(free_after - free_before)}）")
            lb_delta.setObjectName("muted")
            hl.addWidget(lb_delta)
        lay.addWidget(head)

        # ---- 各项明细
        if per:
            box = QFrame()
            box.setObjectName("card")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(14, 12, 14, 12)
            bl.setSpacing(8)
            t = QLabel(f"各项目释放明细（{len(per)} 项）")
            t.setObjectName("h2")
            bl.addWidget(t)
            bars = TypeBars(max_rows=14)
            bars.set_palette(p)
            bars.set_items([(name, p["accent"], sz, cnt)
                            for name, sz, cnt in per], freed)
            bl.addWidget(bars)
            lay.addWidget(box)

        # ---- 错误 / 说明
        if errors:
            box2 = QFrame()
            box2.setObjectName("card")
            b2 = QVBoxLayout(box2)
            b2.setContentsMargins(14, 12, 14, 12)
            t2 = QLabel(f"有 {len(errors)} 项没处理（多是被占用）")
            t2.setObjectName("h2")
            t2.setStyleSheet(f"color:{p['risk']['caution']}")
            b2.addWidget(t2)
            tx = QTextBrowser()
            tx.setMaximumHeight(110)
            tx.setPlainText("\n".join(str(x) for x in errors[:20]))
            b2.addWidget(tx)
            lay.addWidget(box2)
        else:
            lb_ok = QLabel("全部处理完成，没有失败项。")
            lb_ok.setObjectName("muted")
            lay.addWidget(lb_ok)

        lb_log = QLabel(f"本次动过的路径已写入日志：{esc(log_path())}")
        lb_log.setObjectName("hint")
        lb_log.setWordWrap(True)
        lay.addWidget(lb_log)

        # ---- 按钮
        brow = QHBoxLayout()
        brow.setSpacing(8)
        b_log = QPushButton("打开日志")
        b_log.setObjectName("warn")
        b_log.clicked.connect(self._open_log)
        brow.addWidget(b_log)
        if backup_root and os.path.isdir(backup_root):
            b_bk = QPushButton("打开备份目录")
            b_bk.clicked.connect(lambda: self._open_dir(backup_root))
            brow.addWidget(b_bk)
        brow.addStretch(1)
        b_html = QPushButton("导出 HTML")
        b_html.setToolTip("把本次清理总结导出成一份自包含的网页（可分享）")
        b_html.clicked.connect(self._export_html)
        brow.addWidget(b_html)
        b_ok = QPushButton("完成")
        b_ok.setObjectName("primary")
        b_ok.setDefault(True)
        b_ok.clicked.connect(self.accept)
        brow.addWidget(b_ok)
        lay.addLayout(brow)

    def _open_log(self):
        p = log_path()
        if not os.path.exists(p):
            QMessageBox.information(self, "还没有日志", p)
            return
        try:
            os.startfile(p)
        except Exception as e:
            QMessageBox.warning(self, "无法打开", str(e))

    def _open_dir(self, d):
        try:
            os.startfile(d)
        except Exception as e:
            QMessageBox.warning(self, "无法打开", str(e))

    # ---------------- 导出 HTML（方向6）----------------
    def _export_html(self):
        """把本次清理总结导出成一份自包含的 HTML 网页（可分享）。"""
        path, _ = QFileDialog.getSaveFileName(
            self, "导出清理报告", "清理总结.html", "HTML 文件 (*.html)")
        if not path:
            return
        if not path.lower().endswith(".html"):
            path += ".html"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._build_html())
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))
            return
        if QMessageBox.question(
                self, "导出完成",
                f"已导出到：\n{path}\n\n是否现在打开查看？",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            try:
                os.startfile(path)
            except Exception:
                pass

    def _build_html(self):
        p = self.pal
        freed = int(self._stats.get("freed", 0) or 0)
        count = int(self._stats.get("count", 0) or 0)
        errors = self._stats.get("errors") or []
        per = self._stats.get("per_rule") or []
        accent = p["accent"]
        safe = p["risk"]["safe"]
        caution = p["risk"]["caution"]
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

        delta = ""
        if self._free_before and self._free_after and self._free_after > self._free_before:
            delta = (f"<div class='muted'>磁盘可用空间：{esc(fmt(self._free_before))} → "
                     f"<b style='color:{safe}'>{esc(fmt(self._free_after))}</b>"
                     f"　（+{esc(fmt(self._free_after - self._free_before))}）</div>")

        head_html = (
            "<div class='card'>"
            "<div class='muted'>本次清理释放</div>"
            f"<div class='big'>{esc(fmt(freed))}</div>"
            "<div class='statrow'>"
            f"<span class='stat'>处理 {count} 个条目</span>"
            f"<span class='stat'>方式：{esc(self._mode or '—')}</span>"
            f"<span class='stat'>耗时 {self._secs:.1f} 秒</span>"
            "</div>"
            f"{delta}"
            "</div>")

        if per:
            maxv = max((sz for _, sz, _ in per), default=1) or 1
            rows = []
            for name, sz, cnt in per:
                w = (sz / maxv * 100) if maxv else 0
                rows.append(
                    "<div class='barrow'>"
                    f"<div class='barname'>{esc(name)} "
                    f"<span class='barsz'>{esc(fmt(sz))} · {cnt} 项</span></div>"
                    f"<div class='track'><div class='fill' "
                    f"style='width:{w:.1f}%;background:{accent}'></div></div>"
                    "</div>")
            detail_html = (f"<div class='card'><div class='h2'>"
                           f"各项目释放明细（{len(per)} 项）</div>"
                           f"{''.join(rows)}</div>")
        else:
            detail_html = ""

        if errors:
            items = "".join(f"<li>{esc(str(x))}</li>" for x in errors[:20])
            err_html = (f"<div class='card'><div class='h2' "
                        f"style='color:{caution}'>有 {len(errors)} 项没处理"
                        f"（多是被占用）</div><ul class='err'>{items}</ul></div>")
        else:
            err_html = ("<div class='card'><div class='muted'>"
                        "全部处理完成，没有失败项。</div></div>")

        log_html = (f"<div class='hint'>本次动过的路径已写入日志："
                    f"{esc(log_path())}</div>")

        return (HTML_TEMPLATE
                .replace("{head}", head_html)
                .replace("{detail}", detail_html)
                .replace("{err}", err_html)
                .replace("{log}", log_html)
                .replace("{now}", now)
                .replace("{app}", esc(APP_NAME)))
