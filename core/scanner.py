"""占用空间扫描。纯 Python，可脱离 Qt 单独使用。

用 inode 去重防止 Windows  junction（如 AppData\\Local\\Application Data）造成无限递归。
"""
import os
import fnmatch
from concurrent.futures import ThreadPoolExecutor, as_completed

from .rules import resolve_targets
from . import cache

# 大文件定位时跳过的目录名（小写比较）：这些是「文件数量炸弹」——动辄几万
# 个小文件，遍历极慢，却几乎不可能藏得下「大于阈值」的单体大文件。
# 注意：不跳 Windows / Program Files 之类，系统盘上的 pagefile.sys、
# hiberfil.sys 恰恰是用户最想揪出来的大户。
SKIP_DIRS = frozenset((
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    ".svn", ".hg", ".gradle", ".idea", ".vs",
))

# 大文件定位：每多找到这么多个就往界面回吐一批（边扫边出）
LARGE_BATCH_EVERY = 25


def dir_size(path, max_depth=80):
    """返回 (总字节数, 文件数, 最新修改时间 epoch)。

    用 (dev, ino) 记录已访问目录，避免 junction（如 AppData\\Local\\Application Data）
    造成无限递归；遍历文件时顺手记下最新的 st_mtime，供「智能推荐」算分用，
    几乎零额外开销（本来就要 stat 每个文件）。
    """
    total = 0
    count = 0
    newest = 0.0
    seen = set()
    stack = [(path, 0)]
    while stack:
        cur, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            st = os.stat(cur)
            key = (st.st_dev, st.st_ino)
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append((e.path, depth + 1))
                        else:
                            est = e.stat(follow_symlinks=False)
                            total += est.st_size
                            count += 1
                            if est.st_mtime > newest:
                                newest = est.st_mtime
                    except OSError:
                        continue
        except OSError:
            continue
    return total, count, newest


def _size_of_file(path):
    try:
        st = os.stat(path)
        return st.st_size, 1, st.st_mtime
    except OSError:
        return 0, 0, 0.0


def _match_size(targets, patterns):
    """delete_files 模式：只统计顶层中命中通配符的文件，不会去遍历整个目录。"""
    total = 0
    count = 0
    newest = 0.0
    for t in targets:
        if os.path.isfile(t):
            n = os.path.basename(t)
            if any(fnmatch.fnmatch(n, p) for p in patterns):
                s, c, mt = _size_of_file(t)
                total += s
                count += c
                if mt > newest:
                    newest = mt
            continue
        try:
            with os.scandir(t) as it:
                for e in it:
                    try:
                        if not e.is_file(follow_symlinks=False):
                            continue
                        if not any(fnmatch.fnmatch(e.name, p) for p in patterns):
                            continue
                        st = e.stat(follow_symlinks=False)
                        total += st.st_size
                        count += 1
                        if st.st_mtime > newest:
                            newest = st.st_mtime
                    except OSError:
                        continue
        except OSError:
            continue
    return total, count, newest


def scan_rule(rule):
    """计算单条规则的占用。返回 dict。

    per：逐个目标的 [(path, size, count, newest)]，界面上用它把「一条规则对应多个目录」
    展开成可单独勾选的子项（例如不同浏览器、不同网盘各自的缓存目录）。
    newest：该规则所有目标里最新的修改时间（epoch），供「智能推荐」算分用。
    """
    targets = resolve_targets(rule)
    mode = rule.get("mode")

    if not targets:
        return {"rule_id": rule["id"], "size": 0, "count": 0, "targets": [],
                "per": [], "newest": 0.0, "error": None}

    if mode == "delete_files" and rule.get("patterns"):
        total, count, newest = _match_size(targets, rule["patterns"])
        per = [{"path": t, "size": 0, "count": 0, "newest": 0.0} for t in targets]
        return {"rule_id": rule["id"], "size": total, "count": count,
                "targets": targets, "per": per, "newest": newest, "error": None}

    total = 0
    count = 0
    newest = 0.0
    per = []
    for t in targets:
        # 先查缓存：inode + mtime 都不变就直接复用上次的汇总，省掉一次大目录扫描
        cached = cache.get_dir(t)
        if cached is not None:
            s, c, mt = cached
        elif os.path.isfile(t):
            s, c, mt = _size_of_file(t)
            cache.put_dir(t, s, c, mt)
        else:
            s, c, mt = dir_size(t)
            cache.put_dir(t, s, c, mt)
        total += s
        count += c
        if mt > newest:
            newest = mt
        per.append({"path": t, "size": s, "count": c, "newest": mt})

    return {"rule_id": rule["id"], "size": total, "count": count,
            "targets": targets, "per": per, "newest": newest, "error": None}


class Scanner:
    """多线程批量扫描，通过回调回报进度。支持中途取消并从剩下的规则继续。"""

    def __init__(self, rules, workers=6):
        self.rules = rules
        self.workers = workers
        self._cancel = False

    def cancel(self):
        """请求停止。已扫完的部分照常返回，调用方据此算出还剩哪些没扫。"""
        self._cancel = True

    def run(self, rules=None, base=0, total=None, on_progress=None, on_done=None):
        """扫描给定的规则子集（None 表示全部）。

        base  : 已完成的条数，用于让进度条从中断处接着走
        total : 总条数（含已完成的）
        cancel 后仍会返回已经扫完的那部分结果，调用方据此算出剩余规则。
        """
        todo = rules if rules is not None else self.rules
        results = {}
        done = base
        total = total if total is not None else len(todo)
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futures = {ex.submit(scan_rule, r): r for r in todo}
            for fu in as_completed(futures):
                if self._cancel:
                    break
                r = futures[fu]
                try:
                    res = fu.result()
                except Exception as e:
                    res = {"rule_id": r["id"], "size": 0, "count": 0,
                           "targets": [], "error": str(e)}
                results[res["rule_id"]] = res
                done += 1
                if on_progress:
                    on_progress(done, total, r.get("name", ""))
        if on_done:
            on_done(results)
        return results


def find_large_files(root, min_bytes=500 * 1024 * 1024, limit=200,
                     on_progress=None, should_cancel=None, on_batch=None):
    """大文件定位：只报告不删除。root 可以是盘（E:\\）也可以是任意目录。

    返回 [(size, path)]，按大小降序。

    should_cancel 是个 callable，返回 True 就中途收手——用来给扫描条上的
    「停止」按钮接上，扫一整个盘的时候能随时喊停。
    on_batch 也是 callable：每多攒够 LARGE_BATCH_EVERY 个就回吐一份
    「当前累计结果的快照」（已排序、已截断到 limit）。界面拿它边扫边长出
    列表，不用干等到全部扫完——扫大容量盘时体感差别很大。
    """
    found = []
    scanned = 0
    pushed = 0                      # 上次回吐时的条数，用来判断是否够一批
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        if len(found) >= limit * 4:
            break
        if should_cancel and should_cancel():
            break
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in SKIP_DIRS
                       and d not in ("$Recycle.Bin", "System Volume Information")]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                sz = os.stat(p).st_size
            except OSError:
                continue
            if sz >= min_bytes:
                found.append((sz, p))
                # 分批判断放在文件循环里：只在目录边界判断的话，一个装了几百个
                # 大文件的目录（典型如视频文件夹）要整个扫完才吐第一批，
                # 「边扫边出」就白做了
                if on_batch and len(found) >= pushed + LARGE_BATCH_EVERY:
                    pushed = len(found)
                    on_batch(sorted(found, reverse=True)[:limit])
        scanned += 1
        if on_progress and scanned % 200 == 0:
            on_progress(len(found))
    found.sort(reverse=True)
    return found[:limit]
