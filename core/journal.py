"""清理操作日志（纯文本，独立文件）。

与 config.json 里的「历史记录」不同：这个日志文件记录**每一次清理动过的具体路径**，
用于在误删后回溯「当时到底删了什么、删到哪里去了」。

位置：%APPDATA%\\YYYCleanSweep\\clean_history.log
只做追加写，不自动轮转；界面上提供「打开日志」「清空日志」。
"""
import os
import time
import threading

from .paths import store_dir
from .sizefmt import fmt_size

MAX_ENTRIES_PER_RULE = 60      # 每条规则最多记录多少个被处理路径
KEEP_BYTES = 4 * 1024 * 1024   # 超过 4 MB 时归档一份旧日志，重新开始


def log_dir():
    """日志文件所在目录（沿用 store_dir，与其它数据同处一个位置）。"""
    return store_dir()


def log_path():
    """清理日志的完整路径。界面上的「打开日志文件」直接用它。"""
    return os.path.join(store_dir(), "clean_history.log")


class Journal:
    """追加写的清理日志。每次 begin() 一块、end() 收尾，中间按规则逐条记。"""

    def __init__(self, path=None):
        self.path = path or log_path()
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 写
    def _write(self, lines):
        with self._lock:
            try:
                self._rotate_if_needed()
                with open(self.path, "a", encoding="utf-8") as f:
                    for ln in lines:
                        f.write(ln + "\n")
                return True
            except OSError:
                return False

    def _rotate_if_needed(self):
        try:
            if os.path.getsize(self.path) < KEEP_BYTES:
                return
            stamp = time.strftime("%Y%m%d_%H%M%S")
            os.replace(self.path, self.path.replace(".log", f"_{stamp}.log"))
        except OSError:
            pass

    def _ts(self):
        return time.strftime("%Y-%m-%d %H:%M:%S")

    # ---------------------------------------------------------------- API
    def begin(self, plan, mode):
        """plan: [(规则名, [目标路径...], 预计字节)]"""
        total = sum(x[2] for x in plan)
        lines = [
            "",
            "=" * 72,
            f"[{self._ts()}] 开始清理",
            f"  方式      : {mode}",
            f"  项目数    : {len(plan)}",
            f"  预计释放  : {total / 1024 ** 3:.2f} GB" if total else "  预计释放  : 0",
        ]
        for name, targets, size in plan:
            lines.append(f"  ▸ {name}（{len(targets)} 个位置）")
            for t in targets[:10]:
                lines.append(f"      {t}")
            if len(targets) > 10:
                lines.append(f"      …另有 {len(targets) - 10} 个位置")
        self._write(lines)
        return True

    def rule_done(self, name, freed, count, removed=None, errors=None):
        """记一条规则的清理结果：释放多少、动了哪些路径、失败了几条。

        路径最多记 MAX_ENTRIES_PER_RULE 条，再多只记数量——避免日志把
        几万条的缓存目录全列出来，反而看不清重点。
        """
        lines = [f"  ✔ {name}"]
        lines.append(f"      释放 {fmt_size(freed)} · 处理 {count} 个条目")
        removed = removed or []
        if removed:
            lines.append(f"      已处理路径（记录前 {min(len(removed), MAX_ENTRIES_PER_RULE)} 条）：")
            for p in removed[:MAX_ENTRIES_PER_RULE]:
                lines.append(f"        - {p}")
            if len(removed) > MAX_ENTRIES_PER_RULE:
                lines.append(f"        …另有 {len(removed) - MAX_ENTRIES_PER_RULE} 条未逐条记录")
        errors = errors or []
        if errors:
            lines.append(f"      ! 失败 {len(errors)} 条（多是被占用）：")
            for e in errors[:20]:
                lines.append(f"        ! {e}")
        self._write(lines)

    def rule_skipped(self, name, why):
        """记一条被跳过的规则（一般是需要手动操作，程序不代劳）。"""
        self._write([f"  ⃝ {name} — 跳过：{why}"])

    def command(self, name, cmd, ok=True, err=None):
        """记一次系统命令的执行结果（执行的是什么、成功没有）。"""
        if ok:
            self._write([f"  ✔ {name} — 已执行系统命令：{cmd}"])
        else:
            self._write([f"  ✖ {name} — 命令失败：{err}", f"      命令：{cmd}"])

    def end(self, freed, count, mode):
        """收尾：这次一共释放多少、处理多少条目、用的哪种方式。"""
        lines = [
            f"[{self._ts()}] 清理结束 · 共释放 {fmt_size(freed)} · "
            f"处理 {count} 个条目 · 方式：{mode}",
            "=" * 72,
        ]
        self._write(lines)

    def note(self, text):
        """随手记一行说明（例如「用户取消了本次清理」）。"""
        self._write([f"[{self._ts()}] {text}"])

    # ---------------------------------------------------------------- 读
    def tail(self, max_bytes=200_000):
        """读日志末尾（默认最后 200 KB）。只读尾部，日志再大也不会卡界面。"""
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - max_bytes))
                return f.read()
        except OSError:
            return ""

    def clear(self):
        """清空日志（界面上的「清空日志」按钮）。"""
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write("")
            return True
        except OSError:
            return False
