"""重复文件夹检测（零依赖）。

思路：给每棵目录树算一个 **Merkle 签名**——父目录的签名由「本层文件名+大小」
和「子目录名+子目录签名」拼出来的摘要组成，自底向上一次算完。

好处：
  - 两份目录树里的文件结构完全相同 ⇒ 签名必然相同（精确判定，不必逐个文件比）；
  - 深度无限制、不用保存整棵树的清单，内存只随目录数线性增长；
  - 子目录改名会被发现（名字参与了摘要），文件内容改了也会被发现有位移/大小变化。

近似判定（同一项目的两个版本）则用「总大小接近 + 文件数接近 + 顶层名字集合重合率高」
三条件组合，比精确判定宽松，用于捞并处理。

本模块**不依赖 Qt**，命令行模式也能用。
"""
import os
import hashlib

# 默认跳过的目录名（小写比较）。这些是典型的「文件数量炸弹」，
# 既是用户最想找的东西，也最容易淹没其它结果——所以做成一个可关掉的开关。
DEFAULT_SKIP = frozenset((
    "node_modules", "__pycache__", ".git", ".svn", ".hg", ".gradle",
    "venv", ".venv", ".idea", ".vs", ".tox", "site-packages",
    "$recycle.bin", "system volume information",
))


def scan_index(root, skip_names=DEFAULT_SKIP, max_files=400000,
               should_cancel=None, on_progress=None):
    """遍历一遍目录树，返回 {目录路径: 记录}。

    每条记录是一个 dict：
        name    目录名（小写，用于稳定排序）
        parent  父目录路径（顶层为空串）
        own     [(文件名小写, 字节数)]  —— 只含本层
        kids    [(子目录名小写, 子目录路径)]
        size    含全部子孙的总字节数
        count   含全部子孙的总文件数
        digest  Merkle 签名
        depth   层深

    返回 None 表示扫不动（根不存在 / 无权限）。
    """
    root = os.path.normpath(root)
    if not os.path.isdir(root):
        return None

    idx = {}
    nfiles = 0
    stopped = False

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        if should_cancel and should_cancel():
            stopped = True
            break
        if skip_names:
            dirnames[:] = [d for d in dirnames if d.lower() not in skip_names]
        rec = idx.setdefault(dirpath, {"name": os.path.basename(dirpath).lower(),
                                       "parent": "", "own": [], "kids": [],
                                       "size": 0, "count": 0, "digest": "",
                                       "depth": 0})
        for d in dirnames:
            sub = os.path.join(dirpath, d)
            sub_rec = idx.setdefault(
                sub, {"name": d.lower(), "parent": "", "own": [], "kids": [],
                      "size": 0, "count": 0, "digest": "", "depth": 0})
            sub_rec["parent"] = dirpath
            rec["kids"].append((d.lower(), sub))
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                sz = os.stat(p).st_size
            except OSError:
                continue
            rec["own"].append((fn.lower(), sz))
            nfiles += 1
        if nfiles >= max_files:
            stopped = True
            break

    _aggregate(idx, root)
    # idx 里只允许放 dict：以前往里塞过布尔值，aggregate 遍历时会当场崩
    rr = idx.get(root)
    if rr is not None:
        rr["stopped"] = stopped
    return idx


def _aggregate(idx, root):
    """自底向上：算总大小、总文件数、Merkle 签名。"""
    items = [(p, r) for p, r in idx.items()]
    for p, r in items:
        r["size"] = sum(s for _n, s in r["own"])
        r["count"] = len(r["own"])
    # 按路径长度倒序 = 深的先算（子孙一定比祖先长）
    ordered = sorted(items, key=lambda t: len(t[0]), reverse=True)
    for p, r in ordered:
        r["digest"] = _digest(r["own"], r["kids"], idx)
        par = r["parent"]
        if par and par in idx:
            idx[par]["size"] += r["size"]
            idx[par]["count"] += r["count"]


def _digest(own, kids, idx):
    h = hashlib.md5()
    for name, size in sorted(own):
        h.update(("%s|%d\n" % (name, size)).encode("utf-8", "replace"))
    for kid_name, kid_path in sorted(kids):
        kr = idx.get(kid_path)
        h.update(("D|%s|%s\n" % (kid_name, kr["digest"] if kr else ""))
                 .encode("utf-8", "replace"))
    return h.hexdigest()


def _top_names(r):
    """顶层名字集合：本层文件名 + 直接子目录名（带前缀区分）。"""
    s = set(n for n, _sz in r["own"])
    s.update("d:" + k for k, _p in r["kids"])
    return s


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def _has_ancestor(path, paths):
    """path 的某个祖先目录是不是也在 paths 里。"""
    anc = os.path.dirname(path.rstrip("\\/"))
    while anc and os.path.dirname(anc) != anc:
        if anc in paths:
            return True
        anc = os.path.dirname(anc)
    return False


def _drop_nested(groups):
    """丢掉被更大的组完全包住的组。

    projA 与 projB 整体重复时，它们里面的 src/、docs/ 各自也必然重复。
    如果都报出来，同一份冗余就会被记三遍账、可释放空间虚高三倍。
    只保留「没有任何成员处在别的组的子目录里」的那些组。
    """
    allp = set()
    for g in groups:
        allp.update(g["paths"])
    out = []
    for g in groups:
        if any(not _has_ancestor(p, allp) for p in g["paths"]):
            out.append(g)
    return out


def find_duplicates(idx, min_bytes=10 * 1024 * 1024, min_count=2,
                    near_size=0.10, near_count=0.10, near_overlap=0.80,
                    should_cancel=None):
    """在索引里找重复目录。返回 (exact_groups, near_groups)。

    每组的形状：{"paths": [...], "size": 单个的大小, "count": 单个的文件数}
    可释放空间 = size * (len(paths) - 1)。

    min_bytes / min_count：太小太碎的目录不参与比对（噪音且没价值）。
    near_size / near_count：近似的容差（比例）。
    near_overlap：顶层名字集合的最小重合率（Jaccard）。
    """
    if not idx:
        return [], []

    used = set()
    exact = []

    def eligible(r):
        return (r["size"] >= min_bytes and r["count"] >= min_count
                and r["count"] > 0 and r["digest"])

    # ---- 精确：签名相同即为完全一致
    by_digest = {}
    for path, r in idx.items():
        if not eligible(r):
            continue
        by_digest.setdefault(r["digest"], []).append(path)
    for _d, paths in by_digest.items():
        if len(paths) < 2:
            continue
        paths.sort()
        used.update(paths)
        exact.append({"paths": paths, "size": idx[paths[0]]["size"],
                      "count": idx[paths[0]]["count"]})
    exact = _drop_nested(exact)
    exact.sort(key=lambda g: g["size"] * (len(g["paths"]) - 1), reverse=True)

    # ---- 近似：大小接近的放一起滑窗比，避免全表两两比对
    cand = [(p, r) for p, r in idx.items()
            if eligible(r) and p not in used]
    cand.sort(key=lambda t: t[1]["size"])

    parent = {p: p for p, _r in cand}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    sets = {}
    for p, r in cand:
        sets[p] = _top_names(r)

    for i, (pa, ra) in enumerate(cand):
        if should_cancel and should_cancel():
            break
        ceili = ra["size"] * (1.0 + near_size)
        for pb, rb in cand[i + 1:]:
            if rb["size"] > ceili:
                break                       # 后面的只会更大，停
            if abs(ra["size"] - rb["size"]) > ra["size"] * near_size:
                continue
            if abs(ra["count"] - rb["count"]) > max(1, ra["count"] * near_count):
                continue
            if _jaccard(sets[pa], sets[pb]) < near_overlap:
                continue
            union(pa, pb)

    groups = {}
    for p, _r in cand:
        root_p = find(p)
        groups.setdefault(root_p, []).append(p)

    near = []
    for _root, paths in groups.items():
        if len(paths) < 2:
            continue
        paths.sort()
        near.append({"paths": paths, "size": idx[paths[0]]["size"],
                     "count": idx[paths[0]]["count"]})
    near = _drop_nested(near)
    near.sort(key=lambda g: g["size"] * (len(g["paths"]) - 1), reverse=True)
    return exact, near


def groups_total(groups):
    """一组列表总共能释放多少字节（每组留一份，其余算可释放）。"""
    return sum(g["size"] * (len(g["paths"]) - 1) for g in groups)
