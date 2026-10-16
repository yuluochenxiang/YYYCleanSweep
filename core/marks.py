"""用户标记：白名单（主观认可不可删除）、已确认可清理、自由备注。

标记保存在 %APPDATA%\\YYYCleanSweep\\marks.json，与程序本体分离，
重装或重打包 exe 都不会丢失。子目录会继承父目录的标记。
"""
import os
import json
import time
import threading

from .paths import store_dir

MARK_KEEP = "keep"
MARK_JUNK = "junk"

MARK_LABEL = {MARK_KEEP: "白名单", MARK_JUNK: "可清理"}
MARK_ICON = {MARK_KEEP: "[保留]", MARK_JUNK: "[可清]"}


def normkey(p: str) -> str:
    """标记的主键：规范化 + 小写 + 去掉末尾反斜杠，保证 C:\\A 和 c:\\a\\ 同一条。"""
    p = os.path.normpath(p or "")
    return p.lower().rstrip("\\")


class MarkStore:
    """标记库：路径 -> {mark, note, name, time}，按规范化的小写路径索引。"""

    def __init__(self, path=None):
        self.path = path or os.path.join(store_dir(), "marks.json")
        self._lock = threading.Lock()
        self.items = {}
        self.load()

    def load(self):
        """读盘。文件不存在或格式不对时当作「还没有任何标记」。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.items = data.get("items", {})
        except Exception:
            self.items = {}

    def save(self):
        """写盘。同样先写 .tmp 再替换，避免写坏白名单。"""
        with self._lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"version": 1, "items": self.items},
                              f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
                return True
            except OSError:
                return False

    def set(self, path, mark=None, note=None):
        """mark: keep / junk / None（只写备注）。"""
        k = normkey(path)
        rec = self.items.get(k, {})
        rec["display"] = os.path.normpath(path)
        rec["name"] = os.path.basename(os.path.normpath(path).rstrip("\\")) or path
        if mark is not None:
            rec["mark"] = mark
        if note is not None:
            rec["note"] = note
        rec["time"] = time.strftime("%Y-%m-%d %H:%M")
        self.items[k] = rec
        self.save()

    def remove(self, path):
        """删除一条标记，返回是否真的删掉了（本来就没有则返回 False）。"""
        k = normkey(path)
        if k in self.items:
            del self.items[k]
            self.save()
            return True
        return False

    def exact(self, path):
        """只查这一层自己的标记，不向上继承。"""
        return self.items.get(normkey(path))

    def effective(self, path):
        """返回 (mark, note, owner_path, inherited)。逐层向上找最近的标记。"""
        p = os.path.normpath(path or "")
        cur = p
        first = True
        while cur:
            rec = self.items.get(normkey(cur))
            if rec:
                return rec.get("mark") or "", rec.get("note", ""), cur, (not first)
            nxt = os.path.dirname(cur)
            if nxt == cur:
                break
            cur = nxt
            first = False
        return "", "", "", False

    def all(self):
        """列出全部标记，白名单排前面，其余按路径排序——给「我的标记」页用。"""
        out = []
        for k, v in self.items.items():
            out.append({
                "key": k,
                "path": v.get("display", k),
                "name": v.get("name", ""),
                "mark": v.get("mark", ""),
                "note": v.get("note", ""),
                "time": v.get("time", ""),
            })
        out.sort(key=lambda x: (x["mark"] != MARK_KEEP, x["path"].lower()))
        return out

    def is_protected(self, path):
        """是否在白名单里（自己或任一上级目录被标记过）。清理前用它兜一道。"""
        m, _n, _o, _i = self.effective(path)
        return m == MARK_KEEP
