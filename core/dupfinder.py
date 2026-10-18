"""重复文件查找：先按大小分组，再可选做内容校验。

设计取舍：
- 只按大小分组最快，但两个不同文件正好一样大时会误判；
- 加一次「首 64KB + 尾 64KB」的快速校验，绝大多数情况就够准了；
- 完整 MD5 最准，代价是大文件要整读一遍，慢。

两条目录可以填同一个——那就是在一棵树内部找重复。
"""
import os
import hashlib
from concurrent.futures import ThreadPoolExecutor

MATCH_SIZE = 0
MATCH_QUICK = 1
MATCH_FULL = 2
# ⚠️ 新值只能加在末尾：这个数字会写进用户配置（dup_match），
# 插在中间会让老用户的「完整 MD5」突然变成别的意思。
MATCH_SIMILAR = 3
MATCH_LABELS = [
    "仅按大小（最快，可能误判）",
    "大小 + 快速校验（推荐）",
    "大小 + 完整 MD5（最准，慢）",
    "相似查找（仅图片，看内容）",
]

# 「优先保留目录」命中时永远排第一；没命中时按下面的规则决定留哪个
RULE_CTIME_OLD = 0
RULE_CTIME_NEW = 1
RULE_MTIME_OLD = 2
RULE_MTIME_NEW = 3
RULE_SHORTEST = 4
RULES = [RULE_CTIME_OLD, RULE_CTIME_NEW, RULE_MTIME_OLD, RULE_MTIME_NEW,
         RULE_SHORTEST]
RULE_LABELS = [
    "创建时间最早", "创建时间最晚", "修改时间最早", "修改时间最晚", "路径最短",
]

SKIP_DIRS = {
    "$recycle.bin", "system volume information", "windows.old",
    "$windows.~ws", "$windows.~bt", "perflogs", "winsxs",
}

_HAS_JUNCTION = hasattr(os.DirEntry, "is_junction")


def _walk(root, min_bytes, on_progress=None, should_cancel=None, seen=None):
    """遍历一个根目录下的所有文件，返回 [(path, size, mtime, ctime)]。"""
    out = []
    root = os.path.normpath(root)
    seen = seen if seen is not None else set()

    def key_of(p):
        """文件身份（设备号 + inode）。硬链接指向同一份数据，只算一次。"""
        try:
            st = os.stat(p)
            return (st.st_dev, st.st_ino)
        except OSError:
            return None

    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        if should_cancel and should_cancel():
            return out
        # 过滤 junction / 符号链接目录，避免无限递归；过滤系统保留目录
        keep = []
        for d in dirnames:
            if d.lower() in SKIP_DIRS:
                continue
            full = os.path.join(dirpath, d)
            try:
                if os.path.islink(full):
                    continue
                if _HAS_JUNCTION and os.path.isjunction(full):
                    continue
            except OSError:
                continue
            keep.append(d)
        dirnames[:] = keep

        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p, follow_symlinks=False)
            except OSError:
                continue
            if st.st_size < min_bytes:
                continue
            k = (st.st_dev, st.st_ino)
            if k in seen:
                continue
            seen.add(k)
            out.append({"path": p, "size": st.st_size,
                        "mtime": st.st_mtime, "ctime": st.st_ctime})
        if on_progress:
            on_progress(len(out))
    return out


def _hash(path, match):
    h = hashlib.md5()
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if match == MATCH_QUICK:
                h.update(f.read(65536))
                if size > 131072:
                    f.seek(max(0, size - 65536))
                    h.update(f.read(65536))
                h.update(str(size).encode("ascii"))
            else:
                while True:
                    b = f.read(1 << 20)
                    if not b:
                        break
                    h.update(b)
    except OSError:
        return None
    return h.hexdigest()


class DupFinder:
    """扫描两个目录（可以相同），返回重复文件分组。"""

    def __init__(self, dir_a, dir_b=None, min_bytes=1, match=MATCH_QUICK,
                 sim_dist=None, sim_gif=True):
        self.dir_a = os.path.normpath(dir_a) if dir_a else ""
        self.dir_b = os.path.normpath(dir_b or "") if dir_b else ""
        self.match = match
        # 相似查找不吃「大于 N MB」这道门：一张 4000×3000 的照片往往只有 400 KB，
        # 按 1 MB 筛会把绝大多数真实照片直接漏掉。这一路改成按像素筛
        # （imghash.MIN_EDGE / MAX_EDGE），比按文件大小合理得多。
        self.min_bytes = 0 if match == MATCH_SIMILAR else max(0, int(min_bytes))
        self.sim_dist = sim_dist          # 只有相似查找用；None = 严格档
        self.sim_gif = bool(sim_gif)      # 关掉就完全不看 GIF
        self.stopped = False
        self.scanned_files = 0
        self.sim_skipped = {}             # 相似查找：{"原因": 数量}，给界面报数

    def cancel(self):
        """请求停止。scan() 会在下一个检查点收手（不会硬杀线程）。"""
        self.stopped = True

    # ------------------------------------------------------------ 扫描
    def scan(self, on_progress=None, should_cancel=None):
        """返回 [{"size", "hash", "files":[...], "waste"}]，按可释放空间降序。"""
        self.stopped = False
        roots = []
        for r in (self.dir_a, self.dir_b):
            if r and os.path.isdir(r) and r.lower() not in [
                    x.lower() for x in roots]:
                roots.append(r)

        seen = set()
        all_files = []
        for r in roots:
            if should_cancel and should_cancel():
                break
            part = _walk(r, self.min_bytes,
                         on_progress=(lambda n: on_progress(
                             "扫描文件", len(all_files) + n)) if on_progress else None,
                         should_cancel=should_cancel, seen=seen)
            all_files.extend(part)
        self.scanned_files = len(all_files)
        if self.stopped:
            return []

        # 相似查找不走「按大小分组」——相似的两张图大小往往差很多，
        # 先按大小筛就把真正的相似对筛没了。这条路单独走。
        if self.match == MATCH_SIMILAR:
            return self._scan_similar(all_files, on_progress, should_cancel)

        # ---- 按大小分组
        by_size = {}
        for f in all_files:
            by_size.setdefault(f["size"], []).append(f)
        cand = [v for v in by_size.values() if len(v) > 1]
        cand.sort(key=lambda g: -g[0]["size"] * (len(g) - 1))

        groups = []
        if self.match == MATCH_SIZE:
            for g in cand:
                groups.append({"size": g[0]["size"], "hash": "", "files": g})
            return self._finish(groups)

        # ---- 内容校验：先算 hash，再按 hash 二次分组
        todo = []
        for g in cand:
            for f in g:
                todo.append((f, g[0]["size"]))

        def job(item):
            """线程池里的一个任务：算一个文件的 hash（可被取消打断）。"""
            f, _sz = item
            if should_cancel and should_cancel():
                return None
            return _hash(f["path"], self.match)

        with ThreadPoolExecutor(max_workers=6) as ex:
            hashes = list(ex.map(job, todo))

        tmp = {}
        for (f, _sz), hv in zip(todo, hashes):
            if hv is None:
                continue
            tmp.setdefault((f["size"], hv), []).append(f)
        for (size, hv), files in tmp.items():
            if len(files) > 1:
                groups.append({"size": size, "hash": hv, "files": files})
        return self._finish(groups)

    def _scan_similar(self, all_files, on_progress, should_cancel):
        """相似查找：算每张图的感知哈希，再聚类。

        这一路不复用 MD5 通路，因为「内容完全相同」和「看起来像」是两件事：
        缩放、重压缩过的同一张图，MD5 天差地别，dHash 却只差几位。
        """
        from core import imghash as IH

        dist = self.sim_dist if self.sim_dist is not None else IH.LEVEL_STRICT
        skipped = {}

        def skip(reason):
            skipped[reason] = skipped.get(reason, 0) + 1

        # ---- 1. 筛出能比对的图（只看后缀与像素尺寸，不解码）
        pics = []
        for i, f in enumerate(all_files):
            if should_cancel and should_cancel():
                return []
            if IH.is_vector(f["path"]):
                skip("矢量或不适合比对的图")
                continue
            if not IH.is_candidate(f["path"]):
                continue          # 非图片不计数：几十万个非图片文件数了也没用
            if not self.sim_gif and f["path"].lower().endswith(".gif"):
                skip("已关闭 GIF")
                continue
            w, h = IH.size_of(f["path"])
            why = IH.skip_reason(f["path"], w, h)
            if why:
                skip(why)
                continue
            pics.append(f)
            if on_progress and i % 200 == 0:
                on_progress("筛选图片", i)
        self.sim_skipped = skipped

        if len(pics) < 2:
            return []

        # ---- 2. 算哈希。解码是 CPU 活，开 4 个线程压一压
        def job(f):
            if should_cancel and should_cancel():
                return None
            return IH.dhash(f["path"])

        items = []
        done = 0
        with ThreadPoolExecutor(max_workers=4) as ex:
            for f, hv in zip(pics, ex.map(job, pics)):
                done += 1
                if hv is None:
                    skip("读取失败")
                    continue
                items.append((f, hv))
                if on_progress and done % 20 == 0:
                    on_progress("计算图片指纹", done)
        if self.stopped:
            return []

        # ---- 3. 聚类
        def on_group(d, total):
            if on_progress:
                on_progress("比对相似度", d)

        idx_groups = IH.group_similar(
            [(f["path"], hv) for f, hv in items], dist, on_group)

        groups = []
        for idxs in idx_groups:
            files = [items[i][0] for i in idxs]
            hs = [items[i][1] for i in idxs]
            # 每张图记「与组内最像的另一张有多像」，详情面板直接拿去显示。
            # 不比就显示不了百分比，用户只能面对一个「疑似」无从判断。
            for i, (f, hv) in enumerate(zip(files, hs)):
                best = 0.0
                for j, other in enumerate(hs):
                    if i == j:            # 用下标比，别用 ==：整数哈希会误判自身
                        continue
                    s = IH.similarity(hv, other)
                    if s > best:
                        best = s
                f["sim"] = best
            # size / hash 先占位，_finish 里按相似组的规矩重算
            groups.append({"size": 0, "hash": "", "files": files,
                           "kind": "similar"})
        return self._finish(groups)

    def _finish(self, groups):
        for g in groups:
            if g.get("kind") == "similar":
                # 相似组内每张大小都不同，套「大小 × (n-1)」会算出离谱的数。
                # 按「留最大的、其余算冗余」估：sum - max。
                g["files"].sort(key=lambda x: (-x["size"], x["path"].lower()))
                sizes = [x["size"] for x in g["files"]]
                g["size"] = sizes[0]
                g["waste"] = sum(sizes) - sizes[0]
                continue
            g["files"].sort(key=lambda x: x["path"].lower())
            g["waste"] = g["size"] * (len(g["files"]) - 1)
        groups.sort(key=lambda g: (-g["waste"], -g["size"]))
        return groups


# ---------------------------------------------------------------- 保留决策
def in_priority(path, priority_dirs):
    """该路径是否落在某个「优先保留目录」下。"""
    low = (path or "").lower()
    for d in priority_dirs or []:
        d = (d or "").strip().rstrip("\\/").lower()
        if d and (low == d or low.startswith(d + "\\") or low.startswith(d + "/")):
            return True
    return False


def pick_keeper(files, rule=RULE_CTIME_OLD, priority_dirs=None):
    """在一组重复文件里挑出「要保留的那个」。

    规则：位于优先保留目录的先留下（多个命中时再按时间规则），
    否则按 rule 指定的时间 / 路径规则挑。
    """
    if not files:
        return None
    pri = [f for f in files if in_priority(f["path"], priority_dirs)]
    pool = pri if pri else files

    if rule == RULE_CTIME_NEW:
        pool = sorted(pool, key=lambda x: x["ctime"], reverse=True)
    elif rule == RULE_MTIME_OLD:
        pool = sorted(pool, key=lambda x: x["mtime"])
    elif rule == RULE_MTIME_NEW:
        pool = sorted(pool, key=lambda x: x["mtime"], reverse=True)
    elif rule == RULE_SHORTEST:
        pool = sorted(pool, key=lambda x: (len(x["path"]), x["path"].lower()))
    else:
        pool = sorted(pool, key=lambda x: (x["ctime"], x["path"].lower()))
    return pool[0]
