"""目录树扫描：多线程按层遍历，自底向上汇总大小 / 文件数 / 扩展名构成 / 最近修改。

设计要点：
- 用 os.scandir 逐层并行，比 os.walk 快数倍；
- 用 (dev, ino) 去重 + is_junction 判断，避免 AppData\\Local\\Application Data 这类
  junction 造成无限递归；
- 汇总在遍历完成后一次性做，避免锁竞争；
- 支持深度限制与「对指定目录加深扫描」。
"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

SKIP_NAMES = {
    "$recycle.bin", "system volume information", "windows.old",
    "$windows.~ws", "$windows.~bt", "perflogs", "winsxs",
}

_HAS_JUNCTION = hasattr(os.DirEntry, "is_junction")


class DirNode:
    """树上的一个目录节点。

    own_*  是本层自己的数据（不含子目录）；
    agg_*  是含所有子孙的汇总，由 DirTree.aggregate() 自底向上算出来；
    truncated 表示到达扫描深度上限、下面还有子目录没展开。
    """

    __slots__ = ("path", "name", "depth", "parent",
                 "own_size", "own_count", "own_ext", "own_mtime", "own_atime",
                 "agg_size", "agg_count", "agg_ext", "agg_mtime", "agg_atime",
                 "children", "truncated", "error")

    def __init__(self, path, depth, parent):
        self.path = path
        self.name = os.path.basename(path.rstrip("\\/")) or path
        self.depth = depth
        self.parent = parent
        self.own_size = 0
        self.own_count = 0
        self.own_ext = {}
        self.own_mtime = 0.0
        self.own_atime = 0.0
        self.agg_size = 0
        self.agg_count = 0
        self.agg_ext = {}
        self.agg_mtime = 0.0
        self.agg_atime = 0.0
        self.children = []
        self.truncated = False
        self.error = None


def _scan_one(path):
    """扫描单层目录，返回 (size, count, ext_dict, subdirs, newest_mtime,
    newest_atime, error)。"""
    size = 0
    count = 0
    newest = 0.0
    anewer = 0.0
    ext = {}
    subs = []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if _HAS_JUNCTION and e.is_junction():
                            continue
                        if os.path.islink(e.path):
                            continue
                        subs.append(e.path)
                        continue
                    st = e.stat(follow_symlinks=False)
                    size += st.st_size
                    count += 1
                    if st.st_mtime > newest:
                        newest = st.st_mtime
                    at = st.st_atime
                    # Windows 可能压根不更新访问时间（NTFS 的
                    # NtfsDisableLastAccessUpdate 策略），这时 atime 会停在
                    # 创建那一刻、甚至早于修改时间。检测到就用 mtime 兜底，
                    # 绝不让「最后访问」列出现早得离谱的时间。
                    if at + 1.0 < st.st_mtime:
                        at = st.st_mtime
                    if at > anewer:
                        anewer = at
                    x = os.path.splitext(e.name)[1].lstrip(".").lower()
                    key = x if x else "(无扩展名)"
                    rec = ext.get(key)
                    if rec is None:
                        ext[key] = [st.st_size, 1]
                    else:
                        rec[0] += st.st_size
                        rec[1] += 1
                except OSError:
                    continue
    except OSError as ex:
        return size, count, ext, subs, newest, anewer, str(ex)
    return size, count, ext, subs, newest, anewer, None


class DirTree:
    """一棵可增量加深的目录树。"""

    def __init__(self, root):
        self.root = os.path.normpath(root)
        self.nodes = {}
        self.scanned_dirs = 0
        self.stopped = False
        # 这台机器有没有真的在记录「访问时间」。False 时界面要如实说明来源。
        self.atime_reliable = False

    # ---------- 扫描 ----------
    def scan(self, max_depth=4, workers=8, max_dirs=400000,
             on_progress=None, should_cancel=None):
        """按层并行扫描到 max_depth 层。

        中途取消（should_cancel 返回 True）会停在当前层，已经扫到的部分
        依然有效——界面据此直接展示「扫了一半的树」而不是白屏。
        """
        root = self.root
        n = DirNode(root, 0, None)
        self.nodes[root] = n
        level = [root]
        seen = set()
        lock = threading.Lock()

        def key_of(p):
            """目录的唯一身份（设备号 + inode）。junction 会指向同一个身份，
            用它去重就能避免 AppData\\Local\\Application Data 这类无限递归。"""
            try:
                st = os.stat(p)
                return (st.st_dev, st.st_ino)
            except OSError:
                return None

        seen.add(key_of(root))

        for depth in range(0, max_depth + 1):
            if should_cancel and should_cancel():
                self.stopped = True
                break
            if not level or self.scanned_dirs > max_dirs:
                break

            batch = [(p, self.nodes[p]) for p in level if p in self.nodes]
            with ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(lambda t: (_scan_one(t[0])), batch))

            next_level = []
            last = (depth == max_depth)
            for (path, node), res in zip(batch, results):
                size, count, ext, subs, mtime, atime, err = res
                node.own_size = size
                node.own_count = count
                node.own_ext = ext
                node.own_mtime = mtime
                node.own_atime = atime
                node.error = err
                self.scanned_dirs += 1
                if last:
                    node.truncated = bool(subs)
                    continue
                for s in subs:
                    if os.path.basename(s).lower() in SKIP_NAMES:
                        continue
                    k = key_of(s)
                    if k is None:
                        continue
                    with lock:
                        if k in seen:
                            continue
                        seen.add(k)
                    if s in self.nodes:
                        continue
                    nd = DirNode(s, depth + 1, path)
                    self.nodes[s] = nd
                    node.children.append(s)
                    next_level.append(s)
                if on_progress and self.scanned_dirs % 200 == 0:
                    on_progress(self.scanned_dirs, path)

            level = next_level

        self.aggregate()
        if on_progress:
            on_progress(self.scanned_dirs, "")
        return self

    def deepen(self, path, extra_depth=3, workers=8, on_progress=None):
        """对某个已存在的目录做更深的扫描，并合并回树。"""
        path = os.path.normpath(path)
        if path not in self.nodes:
            return False
        base = self.nodes[path]
        prefix = path.rstrip("\\") + "\\"
        for k in [k for k in self.nodes if k.startswith(prefix)]:
            del self.nodes[k]
        base.children = []

        sub = DirTree(path)
        sub.scan(max_depth=extra_depth, workers=workers, on_progress=on_progress)
        for k, nd in sub.nodes.items():
            if k == path:
                continue
            nd.parent = nd.parent
            self.nodes[k] = nd
        top = sub.nodes.get(path)
        if top:
            base.children = list(top.children)
            base.truncated = top.truncated
            base.error = top.error
        self.scanned_dirs += sub.scanned_dirs
        self.aggregate()
        return True

    # ---------- 汇总 ----------
    def aggregate(self):
        """自底向上汇总：把每个节点的 agg_* 累加到父节点。

        按 depth 倒序处理，保证算某个节点时它的子孙已经算完。
        扫描和 deepen 之后都要调一次。
        """
        nodes = self.nodes
        for nd in nodes.values():
            nd.agg_size = nd.own_size
            nd.agg_count = nd.own_count
            nd.agg_ext = {k: list(v) for k, v in nd.own_ext.items()}
            nd.agg_mtime = nd.own_mtime
            nd.agg_atime = nd.own_atime
        ordered = sorted(nodes.values(), key=lambda x: x.depth, reverse=True)
        for nd in ordered:
            p = nd.parent
            if not p or p not in nodes:
                continue
            par = nodes[p]
            par.agg_size += nd.agg_size
            par.agg_count += nd.agg_count
            par.agg_mtime = max(par.agg_mtime, nd.agg_mtime)
            par.agg_atime = max(par.agg_atime, nd.agg_atime)
            dst = par.agg_ext
            for k, v in nd.agg_ext.items():
                d = dst.get(k)
                if d is None:
                    dst[k] = [v[0], v[1]]
                else:
                    d[0] += v[0]
                    d[1] += v[1]
        # 判断本机有没有真的在记访问时间：只要有一个文件的访问时间明显晚于
        # 修改时间（超过 1 小时），就说明系统在记。一个都没有，多半被策略关了。
        # 放在 aggregate 末尾，scan 与 deepen 两条路径都能覆盖到。
        self.atime_reliable = any(
            nd.own_atime > nd.own_mtime + 3600.0 for nd in nodes.values())

    # ---------- 查询 ----------
    def node(self, path):
        """取节点，不存在返回 None。"""
        return self.nodes.get(os.path.normpath(path))

    def children(self, path, sort_key="size", reverse=True):
        """取子目录列表，支持按占用 / 文件数 / 名称 / 最近修改 / 最后访问排序。"""
        nd = self.node(path)
        if not nd:
            return []
        out = [self.nodes[c] for c in nd.children if c in self.nodes]
        if sort_key == "size":
            out.sort(key=lambda x: x.agg_size, reverse=reverse)
        elif sort_key == "count":
            out.sort(key=lambda x: x.agg_count, reverse=reverse)
        elif sort_key == "name":
            out.sort(key=lambda x: x.name.lower(), reverse=reverse)
        elif sort_key == "mtime":
            out.sort(key=lambda x: x.agg_mtime, reverse=reverse)
        elif sort_key == "atime":
            out.sort(key=lambda x: x.agg_atime, reverse=reverse)
        return out

    def total(self):
        """整棵树的汇总占用（字节）。"""
        nd = self.node(self.root)
        return nd.agg_size if nd else 0
