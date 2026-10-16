"""关于 / 帮助对话框：使用说明、常见问题、更新与反馈、关于。"""
import os
import platform
import subprocess
import time

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTabWidget,
    QTextBrowser, QWidget, QFrame, QMessageBox, QApplication,
)

from core import system as sysinfo
from core.version import (
    APP_NAME, APP_VERSION, APP_TAGLINE, APP_DESC,
    UPDATE_HOWTO, FEEDBACK_HOWTO, PRIVACY,
)
from core.journal import log_path
from core.knowledge import knowledge
from ui.theme import palette, DEFAULT_THEME
from ui.widgets import fmt, esc

# 开源仓库：关于页与更新页都会放这个链接，集中在这里改一处即可。
REPO_URL = "https://github.com/yuluochenxiang/YYYCleanSweep"


def _env_text(main=None):
    lines = [
        f"{APP_NAME} {APP_VERSION}",
        f"系统    : {platform.platform()}",
        f"管理员  : {'是' if sysinfo.is_admin() else '否'}",
        f"内存    : {sysinfo.total_ram_gb():.1f} GB",
    ]
    for letter, total, free in sysinfo.get_drives():
        lines.append(f"磁盘    : {letter} 可用 {fmt(free)} / 共 {fmt(total)}")
    if main is not None:
        try:
            lines.append(f"规则数  : {len(main.rules)}")
            from core.knowledge import knowledge
            lines.append(f"知识库  : {len(knowledge().entries)} 条")
        except Exception:
            pass
    lines.append(f"时间    : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)


class AboutDialog(QDialog):
    """关于 / 帮助：使用说明、常见问题、更新与反馈、隐私说明、环境信息。

    环境信息那一页的「复制环境信息」是给用户报问题时用的——版本号、系统、
    磁盘概况一次复制走，省得来回问。
    """

    def __init__(self, main=None, parent=None):
        super().__init__(parent or main)
        self.main = main
        self.pal = palette(getattr(main, "theme", DEFAULT_THEME))
        self.setWindowTitle(f"{APP_NAME} · 关于 / 帮助")
        self.resize(760, 600)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(12)

        head = QFrame()
        head.setObjectName("card")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(16, 12, 16, 12)
        lb = QLabel(f"{APP_NAME} <span style='opacity:.65'> v{APP_VERSION}</span>")
        lb.setObjectName("h1")
        hl.addWidget(lb)
        lb2 = QLabel(APP_TAGLINE)
        lb2.setObjectName("muted")
        hl.addWidget(lb2)
        hl.addSpacing(8)
        lb3 = QLabel(APP_DESC)
        lb3.setObjectName("hint")
        hl.addWidget(lb3)
        hl.addStretch(1)
        lay.addWidget(head)

        tabs = QTabWidget()
        tabs.addTab(self._usage(), "使用说明")
        tabs.addTab(self._faq(), "常见问题")
        tabs.addTab(self._update_feedback(), "更新与反馈")
        tabs.addTab(self._about(), "关于")
        lay.addWidget(tabs, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.accept)
        row.addWidget(b_close)
        lay.addLayout(row)

    # ------------------------------------------------------------ 富文本小工具
    # 说明类文字全是大段灰色正文，关键按钮名 / 默认值 / 路径全淹没在里面，
    # 用户其实是来找这些的。下面几个小工具负责把它们挑出来。
    def rich(self, text):
        """渲染带少量重点标记的说明文案。

        core/version.py 里不少文案本身就写了 <b>重点</b> 标记（比如
        「把新给的 <b>YYYCleanSweep.exe</b> 覆盖到现有位置」）。以前一律 esc
        转义，**用户看到的是一串字面 <b> 标签**——这里按富文本还原出来。

        标记就四个，够用且写起来直观：
            <b>强调</b>  <w>需要留意</w>  <k>可以放心</k>  <m>文件名/路径</m>
        """
        out = esc(text)
        p = self.pal
        pairs = {
            "b": (f"<b style='color:{p['accent']}'>", "</b>"),
            "w": (f"<b style='color:{p['risk']['caution']}'>", "</b>"),
            "k": (f"<b style='color:{p['risk']['safe']}'>", "</b>"),
            "d": (f"<b style='color:{p['risk']['danger']}'>", "</b>"),
            "m": (f"<span style='font-family:Consolas,monospace;"
                  f"background-color:{p['panel2']};color:{p['text']}'>"
                  f"&nbsp;", "&nbsp;</span>"),
        }
        for tag, (op, cl) in pairs.items():
            out = out.replace(f"&lt;{tag}&gt;", op).replace(
                f"&lt;/{tag}&gt;", cl)
        return out

    def em(self, text, role="accent"):
        """关键词：加粗 + 配色。四种角色：强调 / 谨慎 / 危险 / 安全。"""
        p = self.pal
        c = {"accent": p["accent"], "warn": p["risk"]["caution"],
             "bad": p["risk"]["danger"], "ok": p["risk"]["safe"]}.get(
                 role, p["accent"])
        return f"<b style='color:{c}'>{esc(text)}</b>"

    def mono(self, text):
        """文件名 / 路径：等宽 + 淡底色，从正文里一眼挑得出来。"""
        p = self.pal
        return (f"<span style='font-family:Consolas,monospace;"
                f"background-color:{p['panel2']};color:{p['text']}'>"
                f"&nbsp;{esc(text)}&nbsp;</span>")

    def kbd(self, text):
        """键帽：快捷键要看着像键盘上的键，而不是普通引号里的字。"""
        p = self.pal
        return (f"<span style='font-family:Consolas,monospace;font-weight:600;"
                f"background-color:{p['accent_soft']};color:{p['accent']}'>"
                f"&nbsp;{esc(text)}&nbsp;</span>")

    # ------------------------------------------------------------ 页面
    def _browser(self, html):
        t = QTextBrowser()
        # 关于页里有 GitHub 仓库链接，需要点击后在浏览器打开；
        # 其它页没有外部链接，开启这个也不会误触。
        t.setOpenExternalLinks(True)
        t.setHtml(html)
        return t

    def _usage(self):
        h = [
            f"<p style='line-height:1.9'>{esc(APP_DESC)}。"
            f"建议按下面的顺序走一遍，比直接全选清理更稳。</p>",
            self._h2("第一步：看清楚再动手"),
            "<ol style='line-height:1.95'>",
            f"<li>{self.em('项目清理')}：左边选分类，中间勾选要清理的项，"
            f"右边看它的作用、建议和恢复方式。带 🔒 的是你加过白名单的项目。</li>",
            f"<li>点表头的「项目 / 风险 / 占用」可以按该列排序；"
            f"「风险」列按 {self.em('安全', 'ok')} → {self.em('谨慎', 'warn')}"
            f" → {self.em('危险', 'bad')} 排，拿不准就只选安全项。</li>",
            f"<li>一条规则对应多个目录时，项目左边有 {self.em('三角')} 可以展开，"
            f"能只勾选其中一部分目录。</li>",
            "</ol>",
            self._h2("第二步：空间到底被谁吃了"),
            "<ol style='line-height:1.95'>",
            f"<li>{self.em('大文件定位')}：按目录树找出大于阈值的大文件，"
            f"双击直接定位到文件。</li>",
            f"<li>{self.em('目录分析')}：选一个目录（或整个盘）扫描，按层看占用，"
            f"每个目录都标注了作用与建议，可以给目录打白名单 / 可清理 / 备注。</li>",
            f"<li>{self.em('文件去重')}：在两个目录（可以是同一个）之间按大小找"
            f"文件去重，按保留规则只留一个，其余移到回收站。</li>",
            "</ol>",
            self._h2("第三步：清理与回溯"),
            "<ol style='line-height:1.95'>",
            f"<li>点「开始清理」，默认 {self.em('移到回收站', 'ok')}，"
            f"误删可从回收站还原；也可以选备份到其他盘。</li>",
            f"<li>清理完成会弹出{self.em('总结报告')}，可以看到每一项释放了多少，"
            f"还能导出成 HTML 存着。</li>",
            f"<li>每次清理动过的具体路径都会写进日志："
            f"设置页 → 打开日志文件。</li>",
            "</ol>",
            self._h2("安全约定"),
            "<ul style='line-height:1.95'>",
            f"<li>{self.em('危险', 'bad')}级项目会{self.em('二次确认')}，"
            f"且默认不勾选。</li>",
            f"<li>加了{self.em('白名单', 'ok')}的目录，清理时自动跳过。</li>",
            f"<li>不删系统目录、不动个人文档，只处理规则命中的缓存 / 临时目录。</li>",
            "</ul>",
            self._h2("快捷键"),
            f"<div style='line-height:2.2'>"
            f"{self.kbd('F1')} 打开本帮助　{self.kbd('F5')} 重新扫描　"
            f"{self.kbd('Ctrl+F')} 切换到目录分析　{self.kbd('Esc')} 停止当前扫描"
            f"</div>",
        ]
        return self._browser("".join(h))

    def _faq(self):
        # 答句里的关键结论用 rich() 的那四个标记挑出来：整段灰色正文里，
        # 用户真正要抓的就是「默认不勾选」「白名单」「完整 MD5」这一类词。
        qa = [
            ("为什么有些项目扫出来是 0？",
             "要么是这台机器上没生成过（比如没装某个软件），"
             "要么是需要<w>管理员权限</w>才能读到。顶部栏会提示"
             "「未以管理员运行」，右键以管理员身份启动就能扫全。"),
            ("清理会不会让我重新登录 / 丢配置？",
             "浏览器缓存、登录态、Cookie 这类项目被标成"
             "<w>谨慎</w>或<d>危险</d>，<k>默认不勾选</k>，且确认框里会再提示"
             "一次。清理前看一眼右侧的「建议」。"),
            ("删错了能恢复吗？",
             "默认模式是<k>移到回收站</k>，直接从回收站还原即可。"
             "如果选了「备份后删除」，文件会被搬到<b>备份目录</b>；"
             "两者的具体去向都写在<b>清理日志</b>里（设置页 → 打开日志文件）。"),
            ("目录分析扫得很慢？",
             "扫描深度越大越慢。默认 <b>3 层</b>够用了，找到可疑目录后用右侧"
             "<b>深入扫描</b>再往下挖 3 层，比一开始就用 <w>6 层</w>快得多。"),
            ("某个目录被误判了怎么办？",
             "在目录分析里选中它，点<k>白名单</k>，以后清理会跳过；"
             "也可以点<b>写备注</b>记一句为什么不能删。标记存在 "
             "<m>%APPDATA%\\YYYCleanSweep</m> 下，升级不丢。"),
            ("文件去重按大小找，会不会误判？",
             "两个文件正好一样大但内容不同是可能的。默认会再做一次"
             "<b>首部+尾部 64KB</b>的快速校验；想要绝对准确就选<b>完整 MD5</b>，"
             "<w>代价是大文件会读全盘，慢一些</w>。"),
            ("配置 / 白名单存在哪？换电脑能带走吗？",
             "设置页有<b>导出配置 / 导入配置</b>，会把主题、勾选习惯、白名单和"
             "备注一起打包成一个 <b>JSON</b>，换机器导入即可。"),
        ]
        h = []
        for q, a in qa:
            h.append(self._h2("❓ " + self.em(q)))
            h.append(f"<p style='line-height:1.85;margin:2px 0 4px'>"
                     f"{self.rich(a)}</p>")
        return self._browser("".join(h))

    def _update_feedback(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(12)

        lay.addWidget(self._h2w("如何更新"))
        box = QTextBrowser()
        box.setMaximumHeight(140)
        box.setHtml("".join(f"<div style='line-height:1.85'>{self.rich(x)}</div>"
                            for x in UPDATE_HOWTO))
        lay.addWidget(box)

        lay.addWidget(self._h2w("反馈方式"))
        box2 = QTextBrowser()
        box2.setMaximumHeight(150)
        box2.setHtml("".join(f"<div style='line-height:1.85'>{self.rich(x)}</div>"
                             for x in FEEDBACK_HOWTO))
        lay.addWidget(box2)

        lay.addWidget(self._h2w("源码仓库"))
        repo_box = QTextBrowser()
        repo_box.setMaximumHeight(56)
        repo_box.setHtml(
            "<div style='line-height:1.85'>本工具的源码与版本更新都在这里："
            f"<a href='{REPO_URL}' style='color:{self.pal['accent']};"
            f"text-decoration:underline'>{esc(REPO_URL)}</a></div>")
        lay.addWidget(repo_box)

        row = QHBoxLayout()
        row.setSpacing(8)
        b_env = QPushButton("复制环境信息")
        b_fb = QPushButton("写反馈文件")
        b_cfg = QPushButton("打开配置目录")
        b_log = QPushButton("打开清理日志")
        b_dup = QPushButton("打开程序所在目录")
        for b in (b_env, b_fb, b_cfg, b_log, b_dup):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        b_env.clicked.connect(self._copy_env)
        b_fb.clicked.connect(self._write_feedback)
        b_cfg.clicked.connect(self._open_cfg_dir)
        b_log.clicked.connect(self._open_log)
        b_dup.clicked.connect(self._open_app_dir)

        env = QTextBrowser()
        env.setMaximumHeight(120)
        env.setPlainText(_env_text(self.main))
        lay.addWidget(env)
        lay.addStretch(1)
        return w

    def _about(self):
        p = self.pal
        h = [
            f"<div style='font-size:17px;font-weight:600'>{esc(APP_NAME)}</div>",
            f"<div style='line-height:1.9;color:{p['text_dim']}'>"
            f"版本 {esc(APP_VERSION)} · {esc(APP_TAGLINE)}</div>",
            "<hr style='border:none;border-top:1px solid "
            f"{p['border']};margin:12px 0'>",
            self._h2("隐私"),
            "".join(f"<div style='line-height:1.85'>· {self.rich(x)}</div>"
                    for x in PRIVACY),
            self._h2("技术"),
            "<div style='line-height:1.85'>"
            f"· {self.em('Python 3.13 + PySide6')}，PyInstaller 打包，"
            "无第三方联网组件<br>"
            # 条数实时从知识库读，不再写死——改了 rules/dir_knowledge.json
            # 之后这里自动跟着变，不会出现「说明里写的还是旧数字」。
            f"· 目录知识库内置 {self.em(str(len(knowledge().entries)) + ' 条')}，"
            f"{self.em('离线', 'ok')}匹配说明与建议<br>"
            f"· 扫描用 os.scandir 逐层并行，汇总在遍历结束后一次完成</div>",
            self._h2("数据位置"),
            f"<div>{self.mono(os.path.dirname(log_path()))}</div>",
            f"<div style='line-height:1.85;color:{p['text_dim']}'>"
            f"配置、白名单、缓存与日志都在这里，"
            f"{self.em('和程序本体分开', 'ok')}，覆盖升级不会丢。</div>",
            self._h2("开源仓库"),
            "<div style='line-height:1.85'>"
            "本工具完全开源，源码、目录知识库与构建脚本都托管在 GitHub：<br>"
            f"<a href='{REPO_URL}' style='color:{p['accent']};"
            f"text-decoration:underline'>{esc(REPO_URL)}</a><br>"
            f"<span style='color:{p['text_dim']};margin-top:4px'>"
            f"欢迎提 Issue / Pull Request；也方便你本地自己改规则或重新构建。"
            f"</span></div>",
        ]
        return self._browser("".join(h))

    # ------------------------------------------------------------ 工具
    def _h2(self, text):
        """小节标题：前面加一根强调色竖条，一屏好几段时能一眼分出段落。"""
        p = self.pal
        return (f"<div style='font-size:13px;font-weight:600;"
                f"color:{p['text']};margin:14px 0 6px'>"
                f"<span style='color:{p['accent']}'>▍</span>{text}</div>")

    def _h2w(self, text):
        lb = QLabel(text)
        lb.setObjectName("h2")
        return lb

    def _copy_env(self):
        QApplication.clipboard().setText(_env_text(self.main))
        QMessageBox.information(self, "已复制",
                                "环境信息已复制到剪贴板，粘贴给开发者即可。")

    def _write_feedback(self):
        try:
            d = os.path.dirname(log_path())
            p = os.path.join(d, "feedback.md")
            if not os.path.exists(p):
                with open(p, "w", encoding="utf-8") as f:
                    f.write("# 反馈\n\n## 环境\n\n```\n" + _env_text(self.main)
                            + "\n```\n\n## 问题现象\n\n（这里写你遇到的情况）\n\n"
                              "## 期望结果\n\n（这里写你希望它怎么表现）\n")
            os.startfile(p)
        except Exception as e:
            QMessageBox.warning(self, "无法创建", str(e))

    def _open_cfg_dir(self):
        try:
            os.startfile(os.path.dirname(log_path()))
        except Exception as e:
            QMessageBox.warning(self, "无法打开", str(e))

    def _open_log(self):
        p = log_path()
        if not os.path.exists(p):
            QMessageBox.information(self, "还没有日志", "清理一次之后会自动生成。")
            return
        try:
            os.startfile(p)
        except Exception as e:
            QMessageBox.warning(self, "无法打开", str(e))

    def _open_app_dir(self):
        try:
            d = os.path.dirname(os.path.abspath(
                __import__("sys").executable))
            subprocess.Popen(["explorer", d])
        except Exception as e:
            QMessageBox.warning(self, "无法打开", str(e))
