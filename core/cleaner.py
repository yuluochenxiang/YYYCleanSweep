"""清理执行器。三种模式：回收站 / 备份到指定盘 / 永久删除。"""
import os
import shutil
import fnmatch
import subprocess
from datetime import datetime

from .sizefmt import fmt_size

try:
    from send2trash import send2trash
    HAS_TRASH = True
except Exception:
    HAS_TRASH = False

RECYCLE = "recycle"
BACKUP = "backup"
PERMANENT = "permanent"

MODE_LABEL = {RECYCLE: "移到回收站", BACKUP: "备份后删除", PERMANENT: "永久删除"}


def collect_entries(rule, targets):
    """列出一条规则将要处理的具体条目（预览用，最多 300 条）。"""
    mode = rule.get("mode")
    if mode in ("command", "manual"):
        return []
    patterns = rule.get("patterns") or []
    entries = []
    for t in targets:
        if len(entries) >= 300:
            break
        if os.path.isfile(t):
            if not patterns or any(fnmatch.fnmatch(os.path.basename(t), p) for p in patterns):
                entries.append(t)
            continue
        try:
            with os.scandir(t) as it:
                for e in it:
                    if len(entries) >= 300:
                        break
                    if patterns and not any(
                        fnmatch.fnmatch(e.name, p) for p in patterns
                    ):
                        continue
                    entries.append(e.path)
        except OSError:
            continue
    return entries


def _remove_one(path, mode, backup_root, stats, removed=None):
    """删除单个条目，按模式分派。返回释放的字节数。

    removed：可选的列表，成功后把「来源 -> 去向」写进去，供日志回溯。
    """
    size = 0
    try:
        if os.path.isfile(path):
            size = os.stat(path).st_size
        elif os.path.isdir(path):
            size, _ = _dir_bytes(path)
    except OSError:
        size = 0

    dest = None
    try:
        if mode == BACKUP and backup_root:
            rel = os.path.basename(path.rstrip(os.sep)) or "item"
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest_dir = os.path.join(backup_root, stamp)
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, rel)
            n = 1
            while os.path.exists(dest):
                dest = os.path.join(dest_dir, f"{rel}_{n}")
                n += 1
            shutil.move(path, dest)
        elif mode == RECYCLE and HAS_TRASH:
            send2trash(path)
            dest = "回收站"
        else:
            if os.path.isfile(path) or os.path.islink(path):
                if not os.access(path, os.W_OK):
                    try:
                        os.chmod(path, 0o666)
                    except OSError:
                        pass
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            dest = "已永久删除"
    except Exception as e:
        stats["errors"].append(f"{path}: {e}")
        return 0

    stats["count"] += 1
    if removed is not None:
        removed.append(path if not dest or dest in ("回收站", "已永久删除")
                       else f"{path}  ->  {dest}")
    return size


def _dir_bytes(path):
    total = 0
    count = 0
    for dp, dn, fn in os.walk(path):
        for f in fn:
            try:
                total += os.stat(os.path.join(dp, f)).st_size
                count += 1
            except OSError:
                continue
    return total, count


def remove_path(path, mode=RECYCLE, backup_root=None):
    """对外暴露的「删一个条目」接口（重复文件去重等功能直接用它）。

    返回 (释放字节数, 错误信息列表)。失败时释放为 0。
    """
    stats = {"freed": 0, "count": 0, "errors": []}
    freed = _remove_one(path, mode, backup_root, stats)
    return freed, stats["errors"]


class Cleaner:
    """按规则执行清理。

    只负责「删」，不负责决定删什么——勾选项是界面上的事，传进来的 plan
    就是最终决定。三种模式（回收站 / 备份后删 / 永久删）在这里分派，
    逐条统计释放空间、失败原因，并可选写日志。
    """

    def __init__(self, rules, results=None):
        self.rules = {r["id"]: r for r in rules}
        self.results = results or {}

    def _targets(self, rule):
        from .rules import resolve_targets
        res = self.results.get(rule["id"])
        if res and res.get("targets"):
            return res["targets"]
        return resolve_targets(rule)

    def clean(self, plan, mode=RECYCLE, backup_root=None, on_progress=None,
              journal=None):
        """执行清理，返回统计 dict。

        plan 可以是：
          - [(规则 id, [指定目标路径])] —— 子项勾选时用，只清理选定的那些路径
          - [(规则 id, None)] 或直接给 id 列表 —— 按规则解析出的全部路径清理
        journal 不为 None 时，会把每一条被处理的路径写进独立日志文件。
        """
        stats = {"freed": 0, "count": 0, "errors": [], "log": [], "removed": [],
                 "per_rule": []}
        items = []
        for x in plan:
            if isinstance(x, (list, tuple)):
                items.append((x[0], x[1] if len(x) > 1 else None))
            else:
                items.append((x, None))
        total = len(items)

        for i, (rid, want) in enumerate(items, 1):
            rule = self.rules.get(rid)
            if not rule:
                continue
            name = rule.get("name", rid)
            m = rule.get("mode")

            if on_progress:
                on_progress(i, total, name)

            if m == "manual":
                stats["log"].append(f"{name}：需手动操作，已跳过")
                if journal:
                    journal.rule_skipped(name, "该规则需要手动操作，程序不代劳")
                continue

            if m == "command":
                cmd = rule.get("command")
                if not cmd:
                    continue
                ok, err = True, None
                try:
                    subprocess.run(cmd, shell=True, timeout=1800,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception as e:
                    ok, err = False, str(e)
                    stats["errors"].append(f"{name} 命令执行失败：{e}")
                stats["log"].append(f"{name}：{'已执行' if ok else '执行失败'}命令 {cmd}")
                if journal:
                    journal.command(name, cmd, ok, err)
                continue

            targets = want if want else self._targets(rule)
            if not targets:
                continue

            removed = []
            err_before = len(stats["errors"])
            cnt_before = stats["count"]
            freed = 0

            if m == "delete_dir":
                for t in targets:
                    freed += _remove_one(t, mode, backup_root, stats, removed)
            else:
                entries = collect_entries(rule, targets)
                for e in entries:
                    freed += _remove_one(e, mode, backup_root, stats, removed)

            stats["freed"] += freed
            stats["removed"].extend(removed)
            rule_errors = stats["errors"][err_before:]
            rule_count = stats["count"] - cnt_before

            if journal:
                journal.rule_done(name, freed, rule_count, removed, rule_errors)
            if freed or rule_count:
                stats["log"].append(f"{name}：释放 {fmt_size(freed)}")
            # 每个项目分别释放了多少，用于清理总结报告里的明细条
            stats["per_rule"].append((name, freed, rule_count))

        return stats
