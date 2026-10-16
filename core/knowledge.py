"""目录作用知识库加载与匹配。

匹配优先级：路径后缀 > (父目录名 + 目录名) > 目录名 > 路径关键词。
匹配不到时给出基于位置和名称的兜底推断，保证任何目录都有说明。
"""
import os
import json

KIND_LABEL = {
    "cache": "缓存", "settings": "配置", "data": "数据", "install": "程序",
    "log": "日志", "system": "系统", "user": "个人文件", "game": "游戏",
    "temp": "临时", "backup": "备份", "model": "模型", "db": "数据库",
}
RISK_LABEL = {
    "safe": "可清理", "caution": "谨慎", "danger": "禁止删除",
    "keep": "建议保留", "migrate": "建议迁移",
}
RISK_COLOR = {
    "safe": "#1D9E75", "caution": "#BA7517", "danger": "#E24B4A",
    "keep": "#888780", "migrate": "#378ADD",
}
RISK_ORDER = {"safe": 0, "caution": 1, "migrate": 2, "keep": 3, "danger": 4}


def norm(p: str) -> str:
    """统一成小写反斜杠形式，便于匹配。"""
    try:
        p = os.path.normpath(p)
    except Exception:
        pass
    return p.lower().replace("/", "\\")


def leaf(p: str) -> str:
    """取路径最后一段（目录名），用于按名字匹配。"""
    p = norm(p)
    return p.rsplit("\\", 1)[-1]


def parent_name(p: str) -> str:
    """取上一层目录名。同名目录在不同父目录下可能完全是两回事
    （例如 Steam 下的 userdata 和浏览器下的 User Data），所以要一起比对。"""
    p = norm(p)
    parts = p.rsplit("\\", 1)
    if len(parts) < 2:
        return ""
    return parts[-2]


def _bundled_rules():
    """打包后随 exe 内置的 rules 目录（兼容 PyInstaller 6.x 的 _internal 内容布局）。"""
    import sys
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        base = getattr(sys, "_MEIPASS", exe_dir)
        for cand in (base, os.path.join(base, "_internal"), exe_dir):
            p = os.path.join(cand, "rules")
            if os.path.isdir(p):
                return p
        return os.path.join(base, "rules")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rules")


def _external_rules():
    """exe 旁边的 rules 目录（用户可直接编辑）。"""
    import sys
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "rules")
    return _bundled_rules()


def _resolvable(name):
    """优先用 exe 旁的可编辑副本；首次运行时自动释放一份默认文件出来。"""
    ext_dir = _external_rules()
    ext = os.path.join(ext_dir, name)
    if os.path.exists(ext):
        return ext
    src = os.path.join(_bundled_rules(), name)
    try:
        os.makedirs(ext_dir, exist_ok=True)
        if os.path.exists(src) and ext != src:
            import shutil
            shutil.copy2(src, ext)
            return ext
    except OSError:
        pass
    return src


def _kb_path():
    return _resolvable("dir_knowledge.json")


def _ft_path():
    return _resolvable("file_types.json")


class FileTypes:
    """扩展名 -> 类型分类。"""

    def __init__(self, path=None):
        with open(path or _ft_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        self.cats = data["categories"]
        self.fallback = data["fallback"]
        self._map = {}
        for c in self.cats:
            for e in c["ext"]:
                self._map[e.lower()] = c
        self._color = {c["id"]: c["color"] for c in self.cats}
        self._color[self.fallback["id"]] = self.fallback["color"]

    def of_ext(self, ext: str):
        """扩展名 -> 分类（影音 / 文档 / 压缩包…）。没登记过的落进「其它」。"""
        return self._map.get((ext or "").lower(), self.fallback)

    def color(self, cat_id: str) -> str:
        """分类 id -> 图表配色。未知 id 给中性灰。"""
        return self._color.get(cat_id, "#9CA3AF")

    def group(self, ext_hist):
        """把 {ext: [size, count]} 聚合成 {cat_id: [size, count]}，按大小降序。"""
        acc = {}
        for ext, (size, count) in ext_hist.items():
            c = self.of_ext("" if ext.startswith("(") else ext)
            a = acc.setdefault(c["id"], [0, 0])
            a[0] += size
            a[1] += count
        return sorted(acc.items(), key=lambda kv: kv[1][0], reverse=True)


class Knowledge:
    """目录作用知识库。

    把 400 多条目录说明预先建成四张索引（路径后缀 / 名字 / 父目录+名字 /
    关键词），查一个目录时按优先级依次命中，全都命不中时给基于位置和
    名称的兜底推断——保证任何目录在界面上都不会显示「未知」。
    """

    def __init__(self, path=None):
        self.path = path or _kb_path()
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.entries = data["entries"]

        self._paths = []            # [(suffix, entry)]  普通条目
        self._containers = []       # [(suffix, entry)]  容器目录（只描述自身，不覆盖子目录）
        self._by_name = {}          # name -> [entry]
        self._by_parent = {}        # (parent, name) -> [entry]
        self._keywords = []         # [(kw, entry)]
        for e in self.entries:
            for p in e.get("paths", []):
                (self._containers if e.get("container") else self._paths).append((norm(p), e))
            for n in e.get("names", []):
                self._by_name.setdefault(n.lower(), []).append(e)
                for pa in e.get("parents", []):
                    self._by_parent.setdefault((pa.lower(), n.lower()), []).append(e)
            for k in e.get("keywords", []):
                self._keywords.append((k.lower(), e))

    def lookup(self, path):
        """返回匹配结果 dict：{id,name,kind,purpose,advice,risk,score,exact}"""
        p = norm(path)
        name = leaf(p)
        par = parent_name(p)
        best = None
        best_score = 0

        # 1) 路径尾部精确匹配（最高优先级）
        for suffix, e in self._paths:
            if p == suffix or p.endswith("\\" + suffix):
                s = 1000 + len(suffix)
                if s > best_score:
                    best, best_score = e, s
        # 2) 路径中段匹配（如 ...\SteamLibrary\steamapps\common\xxx）
        #    越靠近路径末端越优先，这样 WeChat\FileStorage 不会被 Tencent 抢走
        if best_score < 900:
            for suffix, e in self._paths:
                needle = "\\" + suffix + "\\"
                idx = p.find(needle)
                if idx >= 0:
                    # 越靠近路径末端越优先；同样位置时，描述得更具体的条目胜出
                    s = 450 + min(idx, 100) + len(suffix)
                    if s > best_score:
                        best, best_score = e, s
        # 3) 容器目录：只在目录本身就是它时才命中，避免盖住更具体的条目
        for suffix, e in self._containers:
            if p == suffix or p.endswith("\\" + suffix):
                s = 100 + len(suffix)
                if s > best_score:
                    best, best_score = e, s

        if best_score < 600:
            for e in self._by_parent.get((par, name), []):
                s = 800
                if s > best_score:
                    best, best_score = e, s

        if best_score < 700:
            for e in self._by_name.get(name, []):
                ps = [x.lower() for x in e.get("parents", [])]
                s = 600 if (not ps or par in ps or any(x in p for x in ps)) else 500
                if s > best_score:
                    best, best_score = e, s

        if best_score < 400:
            segs = p.split("\\")
            for kw, e in self._keywords:
                # 关键词必须出现在某个路径分段的开头，或被 - _ . 空格 分隔，
                # 避免 "folder" 里的 "old"、"dialogs" 里的 "logs" 这类误判
                hit = False
                for seg in segs:
                    if (seg == kw or seg.startswith(kw)
                            or (len(kw) >= 4 and seg.endswith(kw))
                            or any((d + kw) in seg for d in ("-", "_", ".", " "))):
                        hit = True
                        break
                if hit:
                    s = 300 + len(kw)
                    if s > best_score:
                        best, best_score = e, s

        if best is None:
            return self._guess(path, p, name, par)

        return {
            "id": best["id"], "name": best["name"],
            "kind": best.get("kind", ""),
            "purpose": best.get("purpose", ""),
            "advice": best.get("advice", ""),
            "risk": best.get("risk", "caution"),
            "score": best_score,
            "exact": True,
        }

    def _guess(self, path, p, name, par):
        """知识库没收录时的兜底推断。"""
        lp = p
        in_roaming = "\\appdata\\roaming\\" in lp
        in_local = "\\appdata\\local\\" in lp or lp.endswith("\\appdata\\local")
        in_locallow = "\\appdata\\locallow\\" in lp
        in_pf = "\\program files" in lp
        in_pd = "programdata" in lp
        in_win = "\\windows\\" in lp

        n = name.lower()
        if in_win:
            return self._mk("Windows 系统目录", "system",
                            "Windows 系统自身的组件、驱动或资源目录。",
                            "系统目录一律不要手动删除，用系统自带的「磁盘清理」处理。", "danger")
        if in_pf:
            return self._mk("已安装软件", "install",
                            "安装在 Program Files 下的软件本体。",
                            "不要直接删文件夹，要移除请在「设置→应用」里卸载。", "keep")
        if in_roaming:
            return self._mk("软件配置数据", "settings",
                            f"位于 AppData\\Roaming 的软件数据目录（{name}），通常存放配置、账号状态和插件。",
                            "Roaming 里多为配置，删除等于重置软件。除非确认是缓存，否则保留。", "caution")
        if in_locallow:
            return self._mk("低权限程序数据 / 游戏存档", "user",
                            f"位于 AppData\\LocalLow 的目录（{name}），多为 Unity 等游戏的存档与设置。",
                            "删除等于删游戏进度，建议保留或先备份。", "caution")
        if in_pd:
            return self._mk("程序共享数据", "install",
                            f"ProgramData 下 {name} 的软件公共数据、日志或下载缓存。",
                            "不要整体删。日志与下载缓存子目录通常可清。", "caution")
        if in_local:
            if any(k in n for k in ("cache", "log", "temp", "tmp", "crash", "dump")):
                return self._mk("软件缓存 / 日志", "cache",
                                f"AppData\\Local 下 {name} 的缓存或日志目录。",
                                "缓存日志类目录一般可安全清理，程序会自动重建。", "safe")
            return self._mk("软件本地数据", "data",
                            f"AppData\\Local 下 {name} 的数据目录，多为缓存、日志或本地数据仓库。",
                            "需结合目录名判断：带 Cache/Logs/Temp 的可清，带 Data/Config 的保留。", "caution")
        if os.path.isdir(path) and any(k in n for k in ("cache", "temp", "tmp", "log")):
            return self._mk("缓存 / 临时目录", "cache",
                            f"名称含缓存/临时关键字的目录（{name}）。",
                            "一般可清理，程序会在需要时重建。", "safe")
        return self._mk("未识别目录", "data",
                        f"知识库暂未收录该目录（{name}）的说明。",
                        "先在右侧看文件构成：若以日志/缓存/临时文件为主可清；若含数据库或个人文件请保留。"
                        "你可以把它的说明补进 rules\\dir_knowledge.json，下次就能自动识别。", "caution")

    @staticmethod
    def _mk(name, kind, purpose, advice, risk):
        return {"id": "_guess", "name": name, "kind": kind, "purpose": purpose,
                "advice": advice, "risk": risk, "score": 0, "exact": False}


_KNOWLEDGE = None
_FILETYPES = None


def knowledge():
    """全局单例。知识库有 400 多条、建索引要花一点时间，只加载一次。"""
    global _KNOWLEDGE
    if _KNOWLEDGE is None:
        _KNOWLEDGE = Knowledge()
    return _KNOWLEDGE


def filetypes():
    """全局单例。扩展名分类表，给 treemap / 类型条形图配色用。"""
    global _FILETYPES
    if _FILETYPES is None:
        _FILETYPES = FileTypes()
    return _FILETYPES
