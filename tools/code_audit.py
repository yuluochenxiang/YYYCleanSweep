"""YYYCleanSweep 代码体检工具（离线，仅用标准库）。

用途：在每次改版后快速扫一遍源码，找出「文档缺失 / 未使用的导入 / 超长函数
/ 疑似重复代码块」等可维护性问题，输出一份中文报告。不改动任何文件，退出码
恒为 0（真正的门禁交给 smoke_test.py）。

用法：
    python tools/code_audit.py            # 扫全部源码
    python tools/code_audit.py ui core    # 只扫指定目录
"""

import ast
import os
import sys
from collections import defaultdict

# 需要扫描的源码根目录（相对工程根）。
# ⚠️ tools/ 与 run.py、smoke_test.py 别漏：这些脚本平时没人跑，最容易藏着
# 「改名漏改」这类一次性 bug（v3.41 修的 kb_merge.py NameError 就是这么漏掉的——
# 当时 tools/ 不在扫描范围内，脚本坏了半年没人知道）。
SRC_DIRS = ["core", "ui", "tools"]
EXTRA_FILES = ["main.py", "build.py", "run.py", "smoke_test.py"]

# 超过这么多行就认为函数/方法偏长，值得提示
LONG_FUNC = 80
# 超过这么多行就认为类偏大，值得提示
LONG_CLASS = 600

# 这些名字不需要 docstring：Qt 的事件重写（方法名本身已说明一切）、
# 构造 / 析构，以及纯转发的一行式访问器。报它们只会淹没有价值的提示。
NO_DOCSTRING = {
    "__init__", "__del__", "__enter__", "__exit__",
    "run", "cancel", "stop", "start", "setData", "data",
    "paintEvent", "resizeEvent", "showEvent", "hideEvent", "closeEvent",
    "keyPressEvent", "wheelEvent", "enterEvent", "leaveEvent",
    "mousePressEvent", "mouseReleaseEvent", "mouseMoveEvent",
    "mouseDoubleClickEvent", "contextMenuEvent", "dragEnterEvent",
    "dropEvent", "eventFilter", "sizeHint", "minimumSizeHint",
    "pixelMetric", "drawPrimitive", "drawControl", "paint",
}


def iter_files(roots):
    """按给定根目录/文件收集待扫描的 .py 文件（跳过 __pycache__）。"""
    for root in roots:
        if os.path.isfile(root) and root.endswith(".py"):
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in sorted(filenames):
                if name.endswith(".py"):
                    yield os.path.join(dirpath, name)


def module_docstring(tree):
    """返回模块级 docstring（没有则返回 None）。"""
    return ast.get_docstring(tree)


def collect_imports(tree):
    """收集 import 进来的名字（含 as 别名），用于后续比对是否被使用。"""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                names.add((alias.asname or alias.name, node.lineno))
    return names


def collect_used_names(tree):
    """收集源码里实际出现的标识符（粗略：所有 Name / Attribute 的根名）。"""
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            cur = node
            while isinstance(cur, ast.Attribute):
                cur = cur.value
            if isinstance(cur, ast.Name):
                used.add(cur.id)
    # 字符串形式的类型注解（from __future__ import annotations 场景）也扫一遍
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            used.add(node.value)
    return used


def scan_file(path):
    """扫描单个文件，返回问题描述列表。"""
    issues = []
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as exc:            # 语法错不在这里修，只报出来
        return ["语法错误：%s（第 %s 行）" % (exc.msg, exc.lineno)]

    if not module_docstring(tree) and os.path.basename(path) != "__init__.py":
        issues.append("缺少模块 docstring")

    used = collect_used_names(tree)
    for name, lineno in sorted(collect_imports(tree), key=lambda x: x[1]):
        if name not in used and name not in src.split("import")[0]:
            issues.append("未使用的导入：%s（第 %d 行）" % (name, lineno))

    # 函数内部定义的辅助类（例如 ctypes 的结构体）不算独立类型，跳过
    local_classes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.ClassDef) and sub is not node:
                    local_classes.add(id(sub))

    # 函数 / 方法：缺 docstring 或过长
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body_len = (node.end_lineno or node.lineno) - node.lineno
            prefix = "方法" if _is_method(tree, node) else "函数"
            if (ast.get_docstring(node) is None
                    and not node.name.startswith("_")
                    and node.name not in NO_DOCSTRING):
                issues.append("%s %s() 缺少 docstring（第 %d 行）"
                              % (prefix, node.name, node.lineno))
            if body_len > LONG_FUNC:
                issues.append("%s %s() 过长：%d 行（第 %d 行）"
                              % (prefix, node.name, body_len, node.lineno))
        elif isinstance(node, ast.ClassDef) and id(node) not in local_classes:
            body_len = (node.end_lineno or node.lineno) - node.lineno
            if body_len > LONG_CLASS:
                issues.append("类 %s 过大：%d 行（第 %d 行）"
                              % (node.name, body_len, node.lineno))
            if ast.get_docstring(node) is None:
                issues.append("类 %s 缺少 docstring（第 %d 行）"
                              % (node.name, node.lineno))
    return issues


def _is_method(tree, func_node):
    """判断函数是否为类里的方法（用于提示文案）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if item is func_node:
                    return True
    return False


def find_dup_blocks(roots, min_lines=8):
    """粗查重复代码块：把每个函数体按语句源码归一化后比对，找出完全相同的段。

    只做「连续 N 行且出现在两处以上」的检测，用于发现复制粘贴式重复，
    不做语义等价判断，避免误报。
    """
    seen = defaultdict(list)
    for path in iter_files(roots):
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        try:
            tree = ast.parse(src, filename=path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            lines = []
            start = None
            for stmt in node.body:
                seg = ast.get_source_segment(src, stmt) or ""
                seg = "\n".join(ln.strip() for ln in seg.splitlines() if ln.strip())
                if not seg:
                    continue
                if start is None:
                    start = stmt.lineno
                lines.append(seg)
                if len(lines) == min_lines:
                    key = "\n".join(lines)
                    seen[key].append("%s:%s():%d" % (path, node.name, start))
                    lines = lines[1:]
                    start = stmt.lineno + 1
    return {k: v for k, v in seen.items() if len(v) > 1}


def report_sizes(roots, top=8):
    """按规模排行：哪些文件 / 类 / 函数最值得优先拆分。"""
    files, classes, funcs = [], [], []
    for path in iter_files(roots):
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        files.append((len(src.splitlines()), path))
        try:
            tree = ast.parse(src, filename=path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                classes.append(((node.end_lineno or 0) - node.lineno,
                                "%s::%s" % (path, node.name)))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.append(((node.end_lineno or 0) - node.lineno,
                              "%s::%s()" % (path, node.name)))
    print("\n" + "=" * 68)
    print("规模排行（拆分优先级参考）")
    print("\n最长的文件 top%d：" % min(top, 5))
    for n, name in sorted(files, reverse=True)[:5]:
        print("  %5d 行  %s" % (n, name))
    print("\n最大的类 top%d：" % top)
    for n, name in sorted(classes, reverse=True)[:top]:
        print("  %5d 行  %s" % (n, name))
    print("\n最长的函数 top%d：" % top)
    for n, name in sorted(funcs, reverse=True)[:top]:
        print("  %5d 行  %s" % (n, name))


def main(argv):
    """入口：命令行给了目录就只扫那些目录，没给就扫工程默认的那几处。"""
    roots = argv[1:] or (SRC_DIRS + [f for f in EXTRA_FILES if os.path.exists(f)])
    roots = [r for r in roots if os.path.exists(r)]
    files = list(iter_files(roots))
    print("=" * 68)
    print("YYYCleanSweep 代码体检报告")
    print("扫描 %d 个文件" % len(files))
    print("=" * 68)

    total = 0
    for path in files:
        issues = scan_file(path)
        if issues:
            total += len(issues)
            print("\n【%s】" % path)
            for item in issues:
                print("  · " + item)
    print("\n小计：%d 条提示" % total)

    dups = find_dup_blocks(roots)
    print("\n" + "-" * 68)
    print("疑似重复代码块（连续 8 条语句完全相同，出现在多处）：%d 组" % len(dups))
    print("注：界面骨架（如各页顶部卡片条、扫描按钮那几行）的相似属于正常，")
    print("    值得抽的是出现了两三遍的「业务逻辑」段。")
    for key, where in list(dups.items())[:12]:
        print("  · 出现在：" + " | ".join(sorted(set(where))))
        print("    首行：" + key.splitlines()[0][:70])
    print("-" * 68)
    report_sizes(roots)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
