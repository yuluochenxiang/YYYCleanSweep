"""亮 / 暗双主题。默认暗色。

所有颜色集中在一个调色板字典里，QSS 由模板生成，界面代码也可以直接
取用同一套颜色画富文本，保证配色始终一致。
"""

LIGHT = {
    "bg": "#eef1f5",
    "panel": "#ffffff",
    "panel2": "#f7f9fb",
    "input": "#ffffff",
    "border": "#d7dde5",
    "border_strong": "#b4bdc9",
    # 亮色主题下白底面积大，次级文字要够深才看得清：
    # text_dim 5.9→8.4:1，text_faint（提示小字）2.9→4.7:1，都过了 WCAG AA
    "text": "#1b2028",
    "text_dim": "#434c59",
    "text_faint": "#646d7a",
    "accent": "#1f6feb",
    "accent_hover": "#1a5fd0",
    "accent_text": "#ffffff",
    "accent_soft": "#e4edfd",
    "hover": "#eef2f7",
    # 选中态改成与主色同一族的深蓝字 + 更实的浅蓝底，不再发灰发飘
    "sel_bg": "#d5e5fc",
    "sel_fg": "#12439b",
    "sel_bar": "#1f6feb",
    "disabled": "#949daa",
    "track": "#e3e7ed",
    "risk": {
        "safe": "#0f7a55",
        "caution": "#a4650f",
        "danger": "#c33b32",
        "migrate": "#1f6feb",
        "gold": "#b07d12",        # 参与去重类型筛选：整类保留
        "purple": "#9b51e0",      # 用户手动取消勾选：手动保留
        "keep": "#6b7482",
        "info": "#6b7482",
    },
    "tint": {"keep": "#dff3e9", "junk": "#fbeed6", "note": "#e2ecfb"},
    "tint_fg": {"keep": "#0f6b4a", "junk": "#8a5608", "note": "#1a4d8f"},
    "mark": {"keep": "#0f7a55", "junk": "#a4650f"},
    # 按钮功能色：主操作 / 危险 / 警告 / 成功 / 次要
    "btn": {
        "primary_bg": "#1f6feb", "primary_fg": "#ffffff", "primary_hover": "#1a5fd0",
        "danger_bg": "#fbe9e7", "danger_fg": "#c33b32", "danger_hover": "#c33b32",
        "warn_bg": "#fdf0dc", "warn_fg": "#8a5608", "warn_hover": "#f0dcb8",
        "ok_bg": "#e3f5ec", "ok_fg": "#0f6b4a", "ok_hover": "#d0ecdf",
        "ghost_bg": "#ffffff", "ghost_fg": "#5b6472", "ghost_hover": "#f0f3f7",
    },
}

DARK = {
    "bg": "#12141a",
    "panel": "#1a1e26",
    "panel2": "#1e2330",
    "input": "#222834",
    "border": "#2a3140",
    "border_strong": "#3a4454",
    "text": "#e4e8f0",
    "text_dim": "#98a2b4",
    "text_faint": "#6b7484",
    "accent": "#5b9dff",
    "accent_hover": "#7cb0ff",
    "accent_text": "#0d1117",
    "accent_soft": "#1c2a44",
    "hover": "#232a35",
    "sel_bg": "#24344f",
    "sel_fg": "#dce8ff",
    "sel_bar": "#5b9dff",
    "disabled": "#545d6d",
    "track": "#2a3140",
    "risk": {
        "safe": "#3ecf95",
        "caution": "#e8b054",
        "danger": "#f4707a",
        "migrate": "#6fb2ff",
        "gold": "#e0b341",        # 参与去重类型筛选：整类保留
        "purple": "#b97bff",      # 用户手动取消勾选：手动保留
        "keep": "#8b95a6",
        "info": "#8b95a6",
    },
    "tint": {"keep": "#14342a", "junk": "#3b2f14", "note": "#152b45"},
    "tint_fg": {"keep": "#5fd6a4", "junk": "#e8b054", "note": "#7cb0ff"},
    "mark": {"keep": "#3ecf95", "junk": "#e8b054"},
    "btn": {
        "primary_bg": "#5b9dff", "primary_fg": "#0d1117", "primary_hover": "#7cb0ff",
        "danger_bg": "#3a1e22", "danger_fg": "#f4707a", "danger_hover": "#5d2a31",
        "warn_bg": "#3b2f14", "warn_fg": "#e8b054", "warn_hover": "#4d3d18",
        "ok_bg": "#14342a", "ok_fg": "#5fd6a4", "ok_hover": "#1b4536",
        "ghost_bg": "#1e2330", "ghost_fg": "#98a2b4", "ghost_hover": "#232a35",
    },
}

PALETTES = {"light": LIGHT, "dark": DARK}
DEFAULT_THEME = "dark"

_TEMPLATE = """
* {{ font-family: "Microsoft YaHei", "Segoe UI", sans-serif; font-size: 13px; }}

QWidget {{ background: {bg}; color: {text}; }}
/* 上面这条 QWidget 底色会顺带铺到 QLabel 上：标签文字会自带一整块方形底色
   （高度顶满父控件），把按钮的圆角底框从中间"切断"。标签一律不带底色，
   底色交给父控件，文字由布局在底框里自然居中。 */
QLabel {{ background: transparent; border: none; }}
QMainWindow, QDialog {{ background: {bg}; }}
QToolTip {{ background: {panel}; color: {text}; border: 1px solid {border_strong};
            padding: 4px 7px; border-radius: 5px; }}

/* ---------- 页签：选中项带强调色下划线，字号比正文大一档 ---------- */
QTabWidget::pane {{ border: none; background: {bg}; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ background: transparent; color: {text_dim}; font-size: 13px;
    padding: 9px 20px; border: none; margin-right: 2px;
    border-bottom: 2px solid transparent; border-radius: 7px 7px 0 0; }}
QTabBar::tab:hover {{ background: {hover}; color: {text}; }}
QTabBar::tab:selected {{ background: {panel}; color: {text}; font-size: 14px;
    font-weight: 600; border-bottom: 2px solid {accent}; }}

/* ---------- 列表 / 树 ----------
   选中态统一交给 RowDelegate 自绘（圆角背景 + 左侧色条），
   这里刻意设成透明，避免底层样式再叠一层导致「选中时文字跳动」。 */
QTreeWidget, QTableWidget, QListWidget {{
    background: {input}; border: 1px solid {border}; border-radius: 10px;
    outline: none; alternate-background-color: {panel2};
    selection-background-color: transparent; selection-color: {sel_fg};
}}
QTreeWidget::item, QTableWidget::item, QListWidget::item {{
    padding: 7px 9px; border-radius: 6px;
    border-bottom: 1px solid {row_line};
}}
QTreeWidget::item:hover, QTableWidget::item:hover, QListWidget::item:hover {{
    background: transparent;
}}
QTreeWidget::item:selected, QTableWidget::item:selected, QListWidget::item:selected {{
    background: transparent; color: {sel_fg}; font-weight: 600;
}}
QTreeWidget::item:selected:!active, QListWidget::item:selected:!active {{
    background: transparent; color: {sel_fg};
}}
/* 展开三角故意不写任何 QSS：一旦出现 QTreeWidget::branch 规则，
   QStyleSheetStyle 就会自己接管这一格的绘制、不再转发给底层样式，
   自绘的三角（BigCheckStyle.PE_IndicatorBranch）就再也画不出来。
   所以这里保持「零规则」，把绘制权完整留给自绘样式。 */

QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: {panel2}; color: {text_dim};
    border: none; border-bottom: 1px solid {border}; border-right: 1px solid {border};
    padding: 8px 10px; font-size: 12px; font-weight: 600; }}
QHeaderView::section:hover {{ background: {hover}; color: {text}; }}
/* 表头是一条铺满整宽的直角色带，会把列表自身的左上 / 右上圆角顶成方形，
   所以首尾两格单独切成圆角，跟列表的 10px 圆角对齐 */
QHeaderView::section:first {{ border-top-left-radius: 9px; }}
QHeaderView::section:last {{ border-top-right-radius: 9px; }}
/* 表格左上角那一格（corner），默认会露出一小块系统底色 */
QTableCornerButton::section {{ background: {panel2}; color: {text_dim};
    border: none; border-bottom: 1px solid {border};
    border-right: 1px solid {border}; border-top-left-radius: 10px; }}
QTableView {{ gridline-color: {border}; }}
QTableWidget {{ gridline-color: {border}; }}

/* ---------- 卡片 / 面板 ---------- */
QFrame#card {{ background: {panel}; border: 1px solid {border}; border-radius: 12px; }}
QFrame#sidebar {{ background: {panel2}; border: 1px solid {border}; border-radius: 12px; }}
QFrame#detail {{ background: {panel2}; border: 1px solid {border}; border-radius: 12px; }}
QGroupBox {{ background: {panel}; border: 1px solid {border}; border-radius: 10px;
    margin-top: 16px; padding: 16px 12px 12px 12px; font-weight: 600; color: {text}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 5px;
    color: {text}; font-size: 13px; font-weight: 600; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

/* ---------- 按钮：按功能分色 ---------- */
QPushButton {{ background: {btn_ghost_bg}; border: 1px solid {border_strong};
    border-radius: 7px; padding: 7px 15px; color: {btn_ghost_fg}; }}
QPushButton:hover {{ background: {btn_ghost_hover}; border-color: {accent}; color: {text}; }}
QPushButton:pressed {{ background: {accent_soft}; }}
QPushButton:disabled {{ color: {disabled}; background: {panel2};
    border-color: {border}; }}

/* 主操作：开始扫描 / 开始清理 / 确认 */
QPushButton#primary {{ background: {btn_primary_bg}; color: {btn_primary_fg};
    border: 1px solid {btn_primary_bg}; font-weight: 600; }}
QPushButton#primary:hover {{ background: {btn_primary_hover};
    border-color: {btn_primary_hover}; color: {btn_primary_fg}; }}
QPushButton#primary:disabled {{ background: {track}; color: {disabled};
    border-color: {border}; }}

/* 危险：停止 / 永久删除 / 清空历史 */
QPushButton#danger {{ background: {btn_danger_bg}; color: {btn_danger_fg};
    border: 1px solid {btn_danger_fg}; }}
QPushButton#danger:hover {{ background: {btn_danger_hover}; color: {btn_danger_fg};
    border-color: {btn_danger_fg}; }}
QPushButton#danger:disabled {{ background: {panel2}; color: {disabled};
    border-color: {border}; }}

/* 警告：继续扫描 / 标记可清理 */
QPushButton#warn {{ background: {btn_warn_bg}; color: {btn_warn_fg};
    border: 1px solid {btn_warn_fg}; }}
QPushButton#warn:hover {{ background: {btn_warn_hover}; color: {btn_warn_fg};
    border-color: {btn_warn_fg}; }}
QPushButton#warn:disabled {{ background: {panel2}; color: {disabled};
    border-color: {border}; }}

/* 正向：白名单 / 复用 */
QPushButton#ok {{ background: {btn_ok_bg}; color: {btn_ok_fg};
    border: 1px solid {btn_ok_fg}; }}
QPushButton#ok:hover {{ background: {btn_ok_hover}; color: {btn_ok_fg};
    border-color: {btn_ok_fg}; }}
QPushButton#ok:disabled {{ background: {panel2}; color: {disabled};
    border-color: {border}; }}

QPushButton#toggle {{ padding: 5px 12px; font-size: 12px; }}
QPushButton#mini {{ padding: 5px 9px; font-size: 12px; }}

/* ---------- 文本标签：按级别分层，字号 / 字重各不相同 ---------- */
QLabel#muted {{ color: {text_dim}; }}
QLabel#hint {{ color: {text_faint}; font-size: 12px; }}
QLabel#title {{ font-size: 15px; font-weight: 600; }}
QLabel#h2 {{ font-size: 13px; font-weight: 600; color: {text}; }}
QFrame#midpane {{ border: 1px solid transparent; }}

/* ---------- 去重页：保一筛选规则三合一可展开面板 ---------- */
QFrame#filterPanel {{ border: 1px solid {border}; border-radius: 9px;
    background: {panel2}; margin-bottom: 4px; }}
QPushButton#filterHdr {{ text-align: left; background: {panel}; border: none;
    border-radius: 8px; padding: 2px; }}
QPushButton#filterHdr:hover {{ background: {panel2}; }}
QLabel#filterArrow {{ font-size: 12px; color: {text_dim}; min-width: 12px; }}
QLabel#filterTitle {{ font-weight: 600; }}
QPushButton#popBtn {{ padding: 6px 4px; }}
/* 顶部三个入口的三角由 QLabel#filterArrow 统一画，这里把 Qt 自带的菜单
   指示箭头隐藏掉，否则按钮上会同时出现两个小三角 */
QPushButton#popBtn::menu-indicator {{ image: none; width: 0; height: 0; }}
/* 行底色全部走圆角：直角色块会把外层容器的圆角顶成方形 */
QTreeWidget::item, QListWidget::item, QTableWidget::item {{ border-radius: 6px; }}
QLabel#h1 {{ font-size: 18px; font-weight: 600; color: {text}; }}
QLabel#big {{ font-size: 26px; font-weight: 700; color: {accent}; }}
QLabel#num {{ font-size: 17px; font-weight: 600; color: {text}; }}
QLabel#chip {{ background: {accent_soft}; color: {accent}; border-radius: 5px;
    padding: 2px 8px; font-size: 12px; }}
QLabel#stat {{ background: {panel2}; border: 1px solid {border};
    border-radius: 9px; padding: 7px 11px; }}

/* ---------- 输入控件 ---------- */
QComboBox, QLineEdit, QSpinBox {{ background: {input}; border: 1px solid {border_strong};
    border-radius: 7px; padding: 6px 10px; color: {text}; selection-background-color: {sel_bg}; }}
QComboBox:hover, QLineEdit:hover, QSpinBox:hover {{ border-color: {accent}; }}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus {{ border-color: {accent}; }}
QComboBox QAbstractItemView {{ background: {panel}; border: 1px solid {border_strong};
    border-radius: 8px; selection-background-color: {sel_bg}; selection-color: {sel_fg};
    padding: 4px; }}
QComboBox QAbstractItemView::item {{ padding: 6px 10px; border-radius: 5px;
    border-left: 3px solid transparent; }}
QComboBox QAbstractItemView::item:selected {{ background: {sel_bg}; color: {sel_fg};
    border-left: 3px solid {sel_bar}; }}
/* 下拉框 / 微调框的箭头也故意不写子控件规则：
   只要出现 QComboBox::drop-down 或 QSpinBox::up-arrow 这类规则，
   QStyleSheetStyle 就自己接管这两个子控件的绘制，不再把 PE_IndicatorArrowDown /
   PE_IndicatorSpinUp 转发给底层样式，自绘的三角就画不出来（表现为"没有箭头"）。
   保持"零子控件规则"，箭头统一由 BigCheckStyle._draw_arrow 画。 */

QCheckBox {{ spacing: 7px; }}
QCheckBox::indicator {{ width: 17px; height: 17px; border-radius: 5px;
    border: 2px solid {border_strong}; background: {input}; }}
QCheckBox::indicator:hover {{ border-color: {accent}; }}
QCheckBox::indicator:checked {{ background: {accent}; border-color: {accent}; }}
QCheckBox::indicator:unchecked {{ background: {input}; }}
QRadioButton {{ spacing: 7px; }}
QRadioButton::indicator {{ width: 15px; height: 15px; border-radius: 8px;
    border: 2px solid {border_strong}; background: {input}; }}
QRadioButton::indicator:hover {{ border-color: {accent}; }}
QRadioButton::indicator:checked {{ background: {accent}; border-color: {accent}; }}

/* ---------- 进度条 / 文本区 / 滚动条 ---------- */
QProgressBar {{ border: none; background: {track}; border-radius: 5px; height: 9px; }}
QSlider::groove:horizontal {{ background: {track}; height: 5px; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {accent}; height: 5px; border-radius: 3px; }}
QSlider::add-page:horizontal {{ background: {track}; height: 5px; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {accent}; width: 13px; height: 13px;
    margin: -4px 0; border-radius: 7px; }}
QSlider::handle:horizontal:hover {{ background: {accent_hover}; }}
QProgressBar::chunk {{ background: {accent}; border-radius: 5px; }}
QTextBrowser, QTextEdit, QPlainTextEdit {{ background: {input}; border: 1px solid {border};
    border-radius: 9px; padding: 11px; color: {text};
    selection-background-color: {sel_bg}; selection-color: {sel_fg}; }}
QSplitter::handle {{ background: {bg}; }}
QSplitter::handle:hover {{ background: {accent_soft}; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {border_strong}; border-radius: 5px;
    min-height: 32px; }}
QScrollBar::handle:vertical:hover {{ background: {accent}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {border_strong}; border-radius: 5px;
    min-width: 32px; }}
QScrollBar::handle:horizontal:hover {{ background: {accent}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QMenu {{ background: {panel}; border: 1px solid {border_strong}; border-radius: 8px;
    padding: 4px; }}
QMenu::item {{ padding: 7px 16px; border-radius: 5px; }}
QMenu::item:selected {{ background: {sel_bg}; color: {sel_fg}; }}
"""


def qss(theme: str) -> str:
    """按主题名生成整套 QSS（模板 + 调色板拼字符串）。"""
    p = PALETTES.get(theme, DARK)
    d = dict(p)
    d["row_line"] = p["border"] if theme == "light" else "#232936"
    for k, v in p["risk"].items():
        d["risk_" + k] = v
    for k, v in p.get("btn", {}).items():
        d["btn_" + k] = v
    return _TEMPLATE.format(**d)


def palette(theme: str) -> dict:
    """取某个主题的调色板 dict，未知名字回落到暗色。"""
    return PALETTES.get(theme, DARK)


def build(theme: str) -> str:
    """兼容旧调用。"""
    return qss(theme)


THEMES = {k: qss(k) for k in PALETTES}
