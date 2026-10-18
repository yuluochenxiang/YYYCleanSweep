"""用户配置与历史操作持久化。

存在 %APPDATA%\\YYYCleanSweep\\config.json，与程序本体分离，重打包 exe 不会丢失。
保存的内容：主题、当前扫描盘、分类、各列表的排序方式、上次的勾选选择集、
目录分析的筛选条件，以及每次清理的历史记录（可一键复用当时的选择）。
支持整体导出 / 导入，换机器时把习惯一起带走。
"""
import os
import json
import time
import threading

from .paths import store_dir

DEFAULTS = {
    "version": 2,
    "theme": "dark",
    "drive": "C:\\",
    "category": "all",
    "sort_clean": [2, True],       # [列号, 是否降序]  0 项目 / 1 风险 / 2 占用
    "sort_dir": 0,                 # 目录分析排序下标
    "sort_big": 0,                 # 大文件排序下标
    "selection": [],               # 上次勾选的规则 id
    "dir_root": "",
    "dir_depth": 2,
    "dir_min_mb": 0,
    "dir_hide_marked": False,
    "dir_idle_months": 0,          # 目录分析：只看多久没打开过的目录（0=不限）
    "big_min_mb": 500,
    "history": [],                 # [{time, ids, names, freed, mode}]
    # ---- 音效
    "sound": True,
    "sound_volume": 42,            # 0 ~ 100
    # ---- 重复文件去重
    "dup_dir_a": "",
    "dup_dir_b": "",
    "dup_min_mb": 1,
    "dup_match": 1,                # 0 仅大小 / 1 快速校验 / 2 完整 MD5 / 3 相似查找
    "sim_level": 0,                # 相似度：0 严格 / 1 标准 / 2 宽松
    "sim_include_gif": True,       # 相似查找是否纳入 GIF（取第一帧）
    "sim_ignored": [],             # 「这组不是重复」忽略掉的路径组合指纹
    "dup_rule": 0,                 # 保留规则下标
    "dup_mode": "recycle",         # recycle / backup / permanent
    "dup_keep_dirs": [],           # 优先保留目录
    # ---- 空间快照（目录分析页「空间变化」）
    "snap_keep": 30,               # 每个扫描根保留多少批快照
    # ---- 重复文件夹
    "dupdir_min_mb": 50,           # 低于这个体积的目录不参与比对
    "dupdir_overlap": 80,          # 「大部分相同」的顶层名字重合率阈值（%）
    "dupdir_skip": True,           # 跳过 node_modules / venv 等噪音目录
    # ---- 磁盘空间提醒（只在启动时查一次，不做后台常驻）
    "warn_enabled": True,          # 关了就完全不查
    "warn_gb": 5,                  # 可用空间低于这个值（GB）提醒
    "warn_pct": 3,                 # 或低于这个百分比提醒
    "warn_freq": "daily",          # daily 每天一次 / always 每次启动
    "warn_last": "",               # "2026-10-17|C:" —— 当天该盘已提醒过
}


class Config:
    """一份 JSON 配置。读不到就用默认值，写不进去也不让程序崩。"""

    def __init__(self, path=None):
        self.path = path or os.path.join(store_dir(), "config.json")
        self._lock = threading.Lock()
        self.data = dict(DEFAULTS)
        self._migrate_keys = set()
        self.load()

    def load(self):
        """读盘。文件不存在或损坏时静默保留默认值，随后补齐新增字段。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return
        if isinstance(raw, dict):
            for k, v in raw.items():
                self.data[k] = v
        # 补齐新增字段
        for k, v in DEFAULTS.items():
            self.data.setdefault(k, v)

    def save(self):
        """写盘。先写 .tmp 再原子替换，避免写到一半断电留下半个配置文件。"""
        with self._lock:
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
                return True
            except OSError:
                return False

    def get(self, key, default=None):
        """取一个配置值；内存里没有就回落到 DEFAULTS，再没有才是 default。"""
        return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key, value):
        """写一个配置值并立即落盘（配置量很小，直接写比攒着写更省心）。"""
        self.data[key] = value
        self.save()

    # ---------- 历史操作 ----------
    def add_history(self, ids, names, freed, mode, limit=30):
        """追加一条清理历史（同一选择集只保留最新一条），最多留 limit 条。"""
        rec = {
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "ids": list(ids),
            "names": list(names)[:20],
            "freed": int(freed or 0),
            "mode": mode or "",
        }
        hist = self.data.setdefault("history", [])
        # 同选择集只保留最新一条
        hist[:] = [h for h in hist if h.get("ids") != rec["ids"]]
        hist.insert(0, rec)
        del hist[limit:]
        self.save()
        return rec

    def clear_history(self):
        """清空历史记录（不影响其它配置）。"""
        self.data["history"] = []
        self.save()

    # ---------- 导出 / 导入 ----------
    def export_to(self, path, marks=None):
        """导出配置（可连带白名单）到指定文件，用于换机器时把习惯一起带走。"""
        payload = {"kind": "YYYCleanSweepProfile", "version": 2,
                   "exported": time.strftime("%Y-%m-%d %H:%M"),
                   "config": self.data}
        if marks is not None:
            payload["marks"] = marks.items
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)

    def import_from(self, path, marks=None):
        """从导出文件恢复配置，返回导入的白名单条数。

        只认 DEFAULTS 里已有的键，避免旧版本或别的文件把脏字段写进来。
        """
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            raise ValueError("文件格式不正确")
        cfg = payload.get("config") or payload
        for k, v in cfg.items():
            if k in DEFAULTS or k == "history":
                self.data[k] = v
        for k, v in DEFAULTS.items():
            self.data.setdefault(k, v)
        n = 0
        if marks is not None and isinstance(payload.get("marks"), dict):
            marks.items.update(payload["marks"])
            marks.save()
            n = len(payload["marks"])
        self.save()
        return n
