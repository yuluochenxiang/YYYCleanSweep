"""清理规则加载与路径解析。数据驱动，规则全部来自 rules/clean_rules.json。"""
import os
import glob
import json

RISK_ORDER = {"safe": 0, "caution": 1, "migrate": 2, "info": 3, "danger": 4}
RISK_LABEL = {
    "safe": "安全",
    "caution": "谨慎",
    "danger": "危险",
    "migrate": "迁移",
    "info": "提示",
}
RISK_COLOR = {
    "safe": "#1D9E75",
    "caution": "#BA7517",
    "danger": "#E24B4A",
    "migrate": "#378ADD",
    "info": "#888780",
}


def _bundled_rules_dir():
    import sys
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        base = getattr(sys, "_MEIPASS", exe_dir)
        for cand in (base, os.path.join(base, "_internal"), exe_dir):
            p = os.path.join(cand, "rules")
            if os.path.isdir(p):
                return p
        return os.path.join(base, "rules")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "rules")


def _external_rules_dir():
    """exe 旁边的 rules 目录（用户可直接编辑）。"""
    import sys
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "rules")
    return _bundled_rules_dir()


def _rules_path():
    """优先用 exe 旁的可编辑副本，首次运行时自动释放一份默认文件出来。"""
    ext_dir = _external_rules_dir()
    ext = os.path.join(ext_dir, "clean_rules.json")
    if os.path.exists(ext):
        return ext
    src = os.path.join(_bundled_rules_dir(), "clean_rules.json")
    try:
        os.makedirs(ext_dir, exist_ok=True)
        if os.path.exists(src) and ext != src:
            import shutil
            shutil.copy2(src, ext)
            return ext
    except OSError:
        pass
    return src


# 当前扫描盘（形如 "C:"），规则里的 %TARGETDRIVE% 会替换成它，
# 让「回收站」「安装残留」这类规则能跟随用户在界面上选择的盘。
CURRENT_DRIVE = (os.environ.get("SystemDrive") or "C:")[:2].upper()


def set_drive(d):
    """传入 "C:\\" / "C:" / "D" 均可。"""
    global CURRENT_DRIVE
    d = (d or "").strip().rstrip("\\/")
    if len(d) >= 2 and d[1] == ":":
        CURRENT_DRIVE = d[:2].upper()
    elif len(d) == 1:
        CURRENT_DRIVE = d.upper() + ":"


def current_drive():
    """当前扫描盘（"C:" 形式）。规则里的 %TARGETDRIVE% 会替换成它。"""
    return CURRENT_DRIVE


def expand(p: str) -> str:
    """展开 %VAR%、%TARGETDRIVE% 与 ~，不做存在性判断。"""
    try:
        p = p.replace("%TARGETDRIVE%", CURRENT_DRIVE)
        p = os.path.expandvars(p)
        p = os.path.expanduser(p)
    except Exception:
        pass
    return os.path.normpath(p)


def load_rules(path=None):
    """读 clean_rules.json，返回 (分类列表, 规则列表)。

    分类按 order 排序；规则逐条补齐缺省字段，省得界面上到处写 .get()。
    """
    path = path or _rules_path()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    categories = sorted(data.get("categories", []), key=lambda c: c.get("order", 99))
    rules = data.get("rules", [])
    for r in rules:
        r.setdefault("paths", [])
        r.setdefault("patterns", [])
        r.setdefault("admin", False)
        r.setdefault("risk", "safe")
        r.setdefault("mode", "clear_contents")
    return categories, rules


def _redirect(p, drive):
    """把绝对路径的盘符换成当前选择的盘（如 C:\\Windows\\Temp -> D:\\Windows\\Temp）。"""
    if len(p) >= 2 and p[1] == ":":
        return drive + p[2:]
    return p


def resolve_targets(rule, drive=None):
    """把一条规则解析成实际存在的具体路径列表。
    声明了 %TARGETDRIVE% 的规则会跟随界面上选择的盘；其余写死盘符的规则
    在切换到别的盘时会同步重定向，不存在则自然淘汰。
    """
    out = []
    mode = rule.get("mode")
    drive = (drive or CURRENT_DRIVE).upper()
    sysdrive = (os.environ.get("SystemDrive") or "C:")[:2].upper()
    redirect = (drive != sysdrive)

    def conv(raw):
        """展开变量；切到非系统盘时把写死的盘符一起换掉。"""
        p = expand(raw)
        if redirect and "%TARGETDRIVE%" not in raw.upper():
            p = _redirect(p, drive)
        return p

    if mode == "glob_dirs":
        pattern = rule.get("glob_pattern", "")
        for root in rule.get("paths", []):
            r = conv(root)
            if not os.path.isdir(r):
                continue
            try:
                for m in glob.glob(os.path.join(r, pattern), recursive=True):
                    if os.path.isdir(m):
                        out.append(m)
            except OSError:
                continue
        return out

    if mode == "command":
        return out

    for raw in rule.get("paths", []):
        p = conv(raw)
        if "%" in p:
            continue
        if os.path.exists(p):
            out.append(p)
    return out
