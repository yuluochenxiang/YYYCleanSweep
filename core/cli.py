"""命令行扫描模式：无界面跑一次清理项扫描，输出控制台表格 / JSON / HTML。

这个模块刻意 **不 import PySide6** —— 它要能在完全没有 GUI 的环境里跑，
用来配合 Windows 计划任务做定时扫描。所以生成的 HTML 是自己拼的，
不去复用 ui/report.py（那个依赖 Qt 的调色板）。

用法示例：
    YYYCleanSweep.exe --version
    YYYCleanSweep.exe --scan
    YYYCleanSweep.exe --scan --target D: --min-size 50MB
    YYYCleanSweep.exe --scan --json report.json
    YYYCleanSweep.exe --scan --html report.html --top 30
"""
import argparse
import ctypes
import html
import json
import os
import shutil
import sys
import time

from core import rules as R
from core.scanner import Scanner
from core.sizefmt import fmt_size, parse_size

EXIT_OK = 0
EXIT_ARG = 2
EXIT_FAIL = 3
EXIT_INTERRUPT = 130

# 挂控制台时是否需要「按键后才关」（自己新开窗口时才有必要）
_CONSOLE_OWNED = False


# ------------------------------------------------------------------ 控制台
def attach_console():
    """给 windowed 模式的进程挂上控制台，让 print / input 可用。

    PyInstaller 用 console=False 打包时，程序属于 Windows 子系统，
    系统不给它分配控制台，`sys.stdout` 是 None——`print()` 不报错但什么都不输出。
    所以命令行模式第一件事就是把标准流补出来：

    1. 父进程（比如 cmd.exe）有控制台 -> AttachConsole 直接借用它的窗口
    2. 没有 -> AllocConsole 自己新建一个窗口（这种情况下退出前必须等一下，
       否则窗口一闪就没，用户啥也看不到）

    返回 True / False 只表示「能不能输出到控制台」，失败也不该中断扫描：
    写了 --json / --html 的话，文件照样能生成。
    """
    global _CONSOLE_OWNED
    import io

    # 已经在真控制台里（从 cmd 跑 python cli.py），什么都不用做
    try:
        if sys.stdout is not None and sys.stdout.fileno() >= 0:
            return True
    except Exception:                                           # noqa: BLE001
        pass

    if os.name != "nt":
        return sys.stdout is not None

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        import msvcrt
    except Exception:                                           # noqa: BLE001
        return False

    ATTACH_PARENT_PROCESS = -1
    if not kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
        if not kernel32.AllocConsole():
            return False
        _CONSOLE_OWNED = True

    # 输出走 UTF-8，否则中文在 GBK 代码页下会炸或变问号
    try:
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:                                           # noqa: BLE001
        pass

    def _reopen(which, mode, wrap_text=True):
        """Windows 上 CRT 的 stdout 是独立于 Win32 句柄的另一套东西，
        AllocConsole 之后必须显式绑一次，否则还是写不出去。"""
        const = {"stdin": -10, "stdout": -11, "stderr": -12}[which]
        h = kernel32.GetStdHandle(const)
        if not h or h == ctypes.c_void_p(-1).value:
            return False
        fd = msvcrt.open_osfhandle(h, os.O_BINARY)
        stream = io.open(fd, mode, buffering=1, encoding="utf-8",
                         errors="replace", closefd=True)
        setattr(sys, which, stream)
        return wrap_text

    try:
        _reopen("stdout", "w")
        _reopen("stderr", "w")
        try:
            _reopen("stdin", "r")
        except Exception:                                       # noqa: BLE001
            pass
    except Exception:                                           # noqa: BLE001
        # 到不了这里通常是因为进程本来就有控制台（比如从 cmd 直接跑源码），
        # 那时标准流本来就能用，按成功处理。
        return sys.stdout is not None
    return True


def _hold_if_owned():
    """自己开的控制台窗口，退出前停一下，别让用户什么都看不到。"""
    if not _CONSOLE_OWNED:
        return
    try:
        print()
        input("按 Enter 关闭窗口…")
    except Exception:                                           # noqa: BLE001
        pass


# ------------------------------------------------------------------ 参数
def build_parser():
    """参数定义。改这里就等于改命令行接口，帮助文本会自动跟着变。"""
    p = argparse.ArgumentParser(
        prog="YYYCleanSweep",
        description="YYYCleanSweep 命令行扫描（无界面）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  YYYCleanSweep.exe --version\n"
            "  YYYCleanSweep.exe --scan\n"
            "  YYYCleanSweep.exe --scan --target D: --min-size 50MB\n"
            "  YYYCleanSweep.exe --scan --json report.json\n"
            "  YYYCleanSweep.exe --scan --html report.html --quiet\n"
            "\n退出码：0 成功 / 2 参数错误 / 3 扫描失败 / 130 已中断\n"
        ),
    )
    p.add_argument("--scan", action="store_true",
                   help="执行一次扫描（不带该参数则打开图形界面）")
    p.add_argument("--target", default="",
                   help="扫描盘，形如 C: / D:（默认系统盘）")
    p.add_argument("--min-size", dest="min_size", default="0",
                   help="只报告大于该体积的项，如 50MB / 1GB（默认全部）")
    p.add_argument("--top", type=int, default=20,
                   help="控制台只显示前 N 条（默认 20，0 表示全部）")
    p.add_argument("--json", dest="json_path", default="",
                   help="结果写入 JSON 文件（UTF-8）")
    p.add_argument("--html", dest="html_path", default="",
                   help="结果写入自包含 HTML 报告")
    p.add_argument("--quiet", action="store_true", help="不打印逐条进度")
    p.add_argument("--version", action="store_true", help="显示版本号并退出")
    return p


def read_version():
    """读版本号。

    先直接读 core/version.py 的源码行：那个模块还带着给界面用的长文案，
    命令行用不着它。但打包成 exe 之后源码文件不一定在（PyInstaller 只留 .pyc），
    读不到就退回 import —— 否则 `--version` 会一本正经地打印 "0.0"。
    """
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "core", "version.py")
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("APP_VERSION"):
                    return s.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    try:
        from core.version import APP_VERSION
        return APP_VERSION
    except Exception:                                           # noqa: BLE001
        return "0.0"


# ------------------------------------------------------------------ 扫描
def collect_rows(results, min_bytes=0):
    """把 Scanner 吐出来的 {rule_id: {...}} 摊平成排好序的列表。

    只挑四条真正用得上的字段，省得 JSON 里塞一堆内部细节。
    """
    rows = []
    for rid, r in results.items():
        if r.get("error"):
            continue
        size = int(r.get("size") or 0)
        if size < min_bytes:
            continue
        rows.append({
            "id": rid,
            "name": r.get("name") or rid,
            "size": size,
            "size_text": fmt_size(size),
            "count": int(r.get("count") or 0),
            "targets": list(r.get("targets") or [])[:20],
        })
    rows.sort(key=lambda d: d["size"], reverse=True)
    return rows


def run_scan(args):
    """真正的扫描流程。返回退出码。"""
    target = (args.target or "").strip() or \
        (os.environ.get("SystemDrive") or "C:")
    target = target[:2].upper()

    min_bytes = 0
    if args.min_size and args.min_size != "0":
        v = parse_size(args.min_size)
        if v is None:
            print(f"错误：--min-size 看不懂「{args.min_size}」，"
                  f"请写成 50MB / 1GB 这种形式。")
            return EXIT_ARG
        min_bytes = int(v)

    # 规则里的 %TARGETDRIVE% 依赖这个全局值，必须先设
    R.set_drive(target)

    try:
        total, used, free = shutil.disk_usage(target + "\\")
    except OSError:
        print(f"错误：读不到磁盘 {target}，盘符不存在或不可访问。")
        return EXIT_ARG

    print(f"扫描目标：{target}    可用空间：{fmt_size(free)} / {fmt_size(total)}")
    started = time.time()
    last = [0.0]

    def on_progress(done, cnt, name=""):
        """扫描进度回调：同一行原地刷新，0.2 秒最多刷一次。

        `last` 用单元素列表是因为它是闭包里的可变状态——
        直接写 `last = now` 会被当成新建局部变量。--quiet 时整个不输出。
        """
        if args.quiet:
            return
        now = time.time()
        if now - last[0] < 0.2 and done != cnt:
            return                       # 限流：别把命令行刷成跑马灯
        last[0] = now
        print(f"\r  扫描中 {done}/{cnt}  {name[:30]:<30}", end="", flush=True)

    scanner = Scanner(R.load_rules()[1])
    try:
        results = scanner.run(on_progress=on_progress)
    except KeyboardInterrupt:
        print("\n已中断。")
        return EXIT_INTERRUPT
    except Exception as e:                                      # noqa: BLE001
        print(f"\n扫描失败：{type(e).__name__}: {e}")
        return EXIT_FAIL

    secs = time.time() - started
    if not args.quiet:
        print("\r" + " " * 60 + "\r", end="", flush=True)      # 擦掉进度行

    rows = collect_rows(results, min_bytes)
    total_bytes = sum(r["size"] for r in rows)
    item_count = sum(r["count"] for r in rows)

    print(f"扫描完成：{len(rows)} 个可清理项，"
          f"合计 {fmt_size(total_bytes)}，耗时 {secs:.1f} 秒"
          + (f"（已按 ≥{args.min_size} 过滤）" if min_bytes else ""))

    shown = rows if not args.top else rows[:args.top]
    if shown:
        print("-" * 62)
        print(f"{'体积':>10}  {'条目':>6}  {'名称'}")
        print("-" * 62)
        for r in shown:
            print(f"{r['size_text']:>10}  {r['count']:>6}  {r['name']}")
        if len(shown) < len(rows):
            print(f"… 另有 {len(rows) - len(shown)} 项未显示（用 --top 0 看全部）")
        print("-" * 62)

    payload = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "version": read_version(),
        "target": target,
        "disk_total": total,
        "disk_free": free,
        "total_bytes": total_bytes,
        "item_count": item_count,
        "elapsed_sec": round(secs, 1),
        "rules": rows,
    }

    if args.json_path:
        if _write_json(args.json_path, payload):
            print(f"JSON 已写入：{os.path.abspath(args.json_path)}")
        else:
            return EXIT_FAIL
    if args.html_path:
        if _write_html(args.html_path, payload):
            print(f"HTML 已写入：{os.path.abspath(args.html_path)}")
        else:
            return EXIT_FAIL
    if not shown and not args.json_path and not args.html_path:
        print("没有发现符合条件的清理项。")
    return EXIT_OK


# ------------------------------------------------------------------ 输出
def _write_json(path, payload):
    try:
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return True
    except OSError as e:
        print(f"写 JSON 失败：{e}")
        return False


def _write_html(path, payload):
    """自包含 HTML：内联 CSS，不引外部资源，双击就能看。

    刻意不用 str.format —— CSS 里的花括号会和占位符打架，
    统一走 .replace 注入（v3.26 做过一次，别再改成 format）。
    """
    rows = payload["rules"]
    trs = []
    for i, r in enumerate(rows, 1):
        bar = int(r["size"] * 100 / max(rows[0]["size"], 1))
        trs.append(
            "<tr><td class='no'>%d</td><td>%s</td><td class='num'>%s</td>"
            "<td class='num'>%d</td>"
            "<td class='bar'><span style='width:%d%%'></span></td></tr>"
            % (i, html.escape(r["name"]), html.escape(r["size_text"]),
               r["count"], max(bar, 1)))
    body = "\n".join(trs) or "<tr><td colspan='5'>没有发现清理项</td></tr>"

    css = """
    body{font-family:"Microsoft YaHei",system-ui,sans-serif;background:#f5f6f7;
         color:#202124;margin:0;padding:28px}
    .wrap{max-width:900px;margin:0 auto;background:#fff;border-radius:12px;
          padding:24px 28px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
    h1{margin:0 0 4px;font-size:20px}
    .muted{color:#6b7280;font-size:13px}
    .big{font-size:30px;font-weight:600;margin:10px 0 2px}
    .stats{margin:14px 0 20px}
    .stat{display:inline-block;background:#eef1f4;border-radius:6px;
          padding:4px 10px;margin:0 6px 6px 0;font-size:12px;color:#374151}
    table{width:100%;border-collapse:collapse;font-size:13px}
    th{text-align:left;color:#6b7280;font-weight:500;padding:6px 8px;
       border-bottom:1px solid #e5e7eb}
    td{padding:7px 8px;border-bottom:1px solid #f1f3f5;vertical-align:middle}
    td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
    td.no{color:#9ca3af;width:28px}
    .bar span{display:block;height:7px;border-radius:4px;background:#378ADD}
    """
    doc = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>__TARGET__ 扫描报告</title><style>__CSS__</style></head>
<body><div class="wrap">
<h1>YYYCleanSweep 扫描报告</h1>
<div class="muted">__GENERATED__ · 版本 __VERSION__</div>
<div class="big">__TOTAL__</div>
<div class="stats">
<span class="stat">目标 __TARGET__</span>
<span class="stat">__COUNT__ 个可清理项</span>
<span class="stat">条目数 __ITEMS__</span>
<span class="stat">耗时 __SECS__ 秒</span>
<span class="stat">可用空间 __FREE__</span>
</div>
<table><thead><tr><th></th><th>清理项</th><th class="num">体积</th>
<th class="num">条目</th><th>占比</th></tr></thead>
<tbody>__ROWS__</tbody></table>
</div></body></html>
"""
    doc = (doc.replace("__CSS__", css)
              .replace("__TARGET__", html.escape(payload["target"]))
              .replace("__GENERATED__", html.escape(payload["generated"]))
              .replace("__VERSION__", html.escape(payload["version"]))
              .replace("__TOTAL__", html.escape(fmt_size(payload["total_bytes"])))
              .replace("__COUNT__", str(len(rows)))
              .replace("__ITEMS__", "{:,}".format(payload["item_count"]))
              .replace("__SECS__", str(payload["elapsed_sec"]))
              .replace("__FREE__", html.escape(fmt_size(payload["disk_free"])))
              .replace("__ROWS__", body))
    try:
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(doc)
        return True
    except OSError as e:
        print(f"写 HTML 失败：{e}")
        return False


# ------------------------------------------------------------------ 入口
def has_cli_args(argv=None):
    """判断这次启动要不要走命令行模式。

    在 main.py 里先问这个函数、再决定要不要 import Qt——
    命令行的价值之一就是「不用加载 47MB 的界面库」，先起 Qt 就白搭了。
    """
    argv = sys.argv[1:] if argv is None else list(argv)
    for a in argv:
        if a in ("--version", "--scan", "-h", "--help"):
            return True
    return False


def main_cli(argv=None):
    """命令行模式入口。返回进程退出码。"""
    argv = sys.argv[1:] if argv is None else list(argv)
    attach_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.version:
            print(f"YYYCleanSweep {read_version()}")
            rc = EXIT_OK
        elif args.scan:
            rc = run_scan(args)
        else:
            parser.print_help()
            rc = EXIT_ARG
    finally:
        _hold_if_owned()
    return rc
