"""清理方案导出：把用户勾选好的清理项变成一个可以直接运行的 .bat。

设计三条：
  1. 每条都带中文注释，说明这是什么、风险等级——导出的目的是「让人看懂再执行」，
     不是一个点了就删的黑盒子；
  2. command 类规则（DISM / powercfg / vssadmin 这类系统命令）原样写成 REM 注释，
     默认不自动执行；manual 类只写建议，不产出任何命令；
  3. 只支持「移到回收站」与「永久删除」两种口径，不支持备份模式
     （robocopy /MOVE 在目标已存在、路径过长时行为不一致，容易在别人机器上跑歪）。

⚠️ 编码铁律（本项目踩过两次，别改）：
     GBK + CRLF + 无 BOM，且脚本里绝不能出现 chcp 命令。
     违反任一条的表现都是「双击一闪就没」或中文全乱码。
"""
import os
import time

RECYCLE = "recycle"
PERMANENT = "permanent"

MODE_TITLE = {RECYCLE: "移到回收站", PERMANENT: "永久删除"}

HEAD = """@echo off
REM ============================================================
REM  YYYCleanSweep 清理脚本
REM  生成时间：{now}
REM  处理方式：{mode}
REM  条目数：{n}
REM
REM  用法：右键本文件 -> 以管理员身份运行
REM        本脚本只处理你在软件里勾选过的项目，执行前请再看一遍。
REM ============================================================

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 提示：当前不是管理员，涉及系统目录的条目会失败。
    echo      建议关闭本窗口后，右键本文件、选择「以管理员身份运行」。
    echo.
)

echo 即将处理以下 {n} 个条目，按任意键开始；想终止请按 Ctrl+C 并输入 N。
pause >nul
echo.
"""

TAIL = """
echo.
echo ============================================================
echo  全部执行完毕。建议回到 YYYCleanSweep 重新扫描确认效果。
echo ============================================================
pause
"""


def _bat_escape(path):
    """批处理里的路径转义。

    `%` 在 bat 里是变量前缀，路径含 `%`（例如 `C:\\100%完成`）会被当成变量展开，
    轻则路径错、重则删错目录。写进脚本前必须翻倍成 `%%`。
    引号内的 `&` `;` `^` 不会被 cmd 拆开，不用额外处理。
    """
    return (path or "").replace("%", "%%")


def _ps_recycle(path, is_dir):
    """一条「移到回收站」的 PowerShell 命令。

    用 .NET 的 FileSystem 方法而不是 Remove-Item：后者是永久删除，
    和用户选的「移到回收站」不是一回事。
    路径里的单引号按 PowerShell 字符串规则翻倍转义。
    """
    p = _bat_escape(path).replace("'", "''")
    call = "DeleteDirectory" if is_dir else "DeleteFile"
    return ('powershell -NoProfile -Command "Add-Type -AssemblyName '
            'Microsoft.VisualBasic; [Microsoft.VisualBasic.FileIO.FileSystem]::'
            "%s('%s','OnlyErrorDialogs','SendToRecycleBin')\"" % (call, p))


def _cmd_permanent(path, is_dir):
    """一条永久删除命令。路径必须加引号，否则含空格的路径会把命令拆开。"""
    q = '"%s"' % _bat_escape(path)
    return ("RD /S /Q %s" % q) if is_dir else ("DEL /F /Q %s" % q)


def _risk_of(rule):
    """规则风险的中文标签。

    这里不 import core.rules，是为了让本模块保持零依赖——
    命令行模式（E 卷）会直接调用它，不该被 UI 层或规则模块拖进来。
    """
    return {"safe": "安全", "caution": "谨慎", "danger": "危险",
            "migrate": "迁移", "info": "提示"}.get(rule.get("risk"), "—")


def build_bat(pairs, mode=RECYCLE):
    """生成批处理文本（换行统一用 \\n，写盘时再转 CRLF）。

    pairs : [(规则 dict, [路径列表])]
    mode  : RECYCLE / PERMANENT
    """
    lines = [HEAD.format(now=time.strftime("%Y-%m-%d %H:%M"),
                         mode=MODE_TITLE.get(mode, mode),
                         n=len(pairs))]
    for i, (rule, paths) in enumerate(pairs, 1):
        name = rule.get("name") or rule.get("id", "")
        m = rule.get("mode")
        lines.append("REM " + "-" * 60)
        lines.append("REM [%d/%d] %s" % (i, len(pairs), name))
        lines.append("REM       规则 id：%s    风险：%s"
                     % (rule.get("id", ""), _risk_of(rule)))
        if m == "command":
            cmd = (rule.get("command") or "").strip()
            if cmd:
                lines.append("REM       这是系统命令项：已注释，不会自动执行。")
                lines.append("REM       想执行就删掉下面这行开头的 REM")
                for cl in cmd.splitlines():
                    lines.append("REM " + cl)
            else:
                lines.append("REM       条目不完整（规则里没有 command 字段），已跳过")
            lines.append("")
            continue
        if m == "manual":
            lines.append("REM       这一项需要手动在系统设置里操作，不产出任何命令。")
            adv = (rule.get("advice") or "").strip()
            if adv:
                lines.append("REM       建议：%s" % adv)
            lines.append("")
            continue
        if not paths:
            lines.append("REM       这次没有要处理的路径（可能已被白名单拦下）")
            lines.append("")
            continue
        lines.append("echo [%d/%d] %s" % (i, len(pairs), name))
        for p in paths:
            is_dir = os.path.isdir(p)
            if mode == RECYCLE:
                lines.append(_ps_recycle(p, is_dir))
            else:
                lines.append(_cmd_permanent(p, is_dir))
        lines.append("")
    lines.append(TAIL)
    return "\n".join(lines)


def save_bat(pairs, path, mode=RECYCLE):
    """按 GBK + CRLF + 无 BOM 写出。

    换行显式做两段替换并用 newline="" 关闭平台转换：
    如果同时又用 newline="\\r\\n"，文本里已有的 \\r\\n 会被再翻一次变成 \\r\\r\\n，
    导出的脚本虽然多半还能跑，但已经不干净了。
    """
    text = build_bat(pairs, mode=mode)
    text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    with open(path, "w", encoding="gbk", errors="replace", newline="") as f:
        f.write(text)
    return len(text)
