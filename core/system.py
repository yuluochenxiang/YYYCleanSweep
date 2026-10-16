"""系统信息：管理员权限、磁盘分区、页面文件与休眠状态。全部本地查询，无网络。"""
import os
import re
import ctypes
import subprocess
import shutil


def is_admin():
    """是否以管理员身份运行。不是管理员时部分规则（如休眠文件）会标灰提示。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def get_drives():
    """返回 [(盘符, 总字节, 可用字节)]。"""
    out = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if bitmask & (1 << i):
            letter = chr(65 + i) + ":\\"
            try:
                total, used, free = shutil.disk_usage(letter)
                out.append((letter, total, free))
            except OSError:
                continue
    return out


def pagefile_info():
    """读注册表获取页面文件配置，再统计实际文件体积。不依赖 PowerShell。"""
    lines = []
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management")
        val, _ = winreg.QueryValueEx(key, "PagingFiles")
        for entry in val:
            parts = entry.split()
            path = parts[0] if parts else ""
            lo = parts[1] if len(parts) > 1 else "0"
            hi = parts[2] if len(parts) > 2 else lo
            actual = ""
            p = os.path.join(os.path.dirname(path), os.path.basename(path)) if path else ""
            if p and os.path.exists(p):
                try:
                    gb = os.stat(p).st_size / 1024 ** 3
                    actual = f"，当前实际 {gb:.1f} GB"
                except OSError:
                    pass
            lines.append(f"{path}  初始 {int(lo)/1024:.1f} GB / 上限 {int(hi)/1024:.1f} GB{actual}")
    except Exception:
        pass

    for letter, _, _ in get_drives():
        p = os.path.join(letter, "pagefile.sys")
        if os.path.exists(p):
            try:
                gb = os.stat(p).st_size / 1024 ** 3
                lines.append(f"磁盘现存：{p}  占用 {gb:.1f} GB"
                             f"（配置改动需重启后生效）")
            except OSError:
                pass
    return lines or ["未检测到页面文件"]


def hibernate_status():
    """返回休眠是否启用（hiberfil.sys 是否存在）。"""
    for d, _, _ in get_drives():
        p = os.path.join(d, "hiberfil.sys")
        try:
            if os.path.exists(p):
                size = os.stat(p).st_size
                return True, size
        except OSError:
            continue
    return False, 0


def total_ram_gb():
    """用 GlobalMemoryStatusEx 读物理内存，不依赖 PowerShell。"""
    try:
        # GlobalMemoryStatusEx 要求的输入结构（MSDN 定义的字段顺序不能改）
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return st.ullTotalPhys / 1024 ** 3
    except Exception:
        return 0.0


def dir_size(path, cancel=None):
    """统计目录占用（字节）。跳过无权限目录与 junction，避免死循环。

    大目录（WinSxS、回收站）比较慢，调用方应放到后台线程里跑。
    """
    total = 0
    seen = set()
    stack = [path]
    while stack:
        if cancel and cancel():
            break
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if (hasattr(os.DirEntry, "is_junction")
                                    and e.is_junction()):
                                continue
                            if os.path.islink(e.path):
                                continue
                            st = e.stat(follow_symlinks=False)
                            key = (st.st_dev, st.st_ino)
                            if key in seen:
                                continue
                            seen.add(key)
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def temp_size():
    """用户临时目录 + 系统临时目录的合计占用。"""
    total = 0
    for p in (os.environ.get("TEMP", ""), os.environ.get("TMP", ""),
              os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Temp")):
        if p and os.path.isdir(p):
            total += dir_size(p)
    return total


def recycle_size():
    """各盘回收站（$Recycle.Bin）占用合计。"""
    total = 0
    for letter, _, _ in get_drives():
        p = os.path.join(letter, "$Recycle.Bin")
        if os.path.isdir(p):
            total += dir_size(p)
    return total


def update_cache_size():
    """Windows Update 下载缓存（SoftwareDistribution\\Download）占用。"""
    windir = os.environ.get("WINDIR", r"C:\Windows")
    return dir_size(os.path.join(windir, "SoftwareDistribution", "Download"))


def shadow_storage():
    """系统还原点（卷影副本）占用。返回 (字节, 提示文本)。

    vssadmin 需要管理员权限才能列出占用，拿不到就返回 0 并说明原因。
    """
    try:
        # vssadmin 的输出是系统 ANSI（中文系统 cp936），不指定编码会按 UTF-8 解，
        # 直接抛 UnicodeDecodeError 导致这一项统计不出来。errors=replace 兜底。
        out = subprocess.run(
            ["vssadmin", "list", "shadowstorage"],
            capture_output=True, text=True, timeout=20,
            encoding="mbcs", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        txt = out.stdout or ""
        total = 0
        for line in txt.splitlines():
            if "used space" in line.lower() or "已用空间" in line:
                # 形如 "Used Space: 12.34 GB (13,246,234,567 bytes)"
                m = re.search(r"\(([\d,\s]+)", line)
                if m:
                    try:
                        total += int(m.group(1).replace(",", "").replace(" ", ""))
                    except ValueError:
                        pass
        if total:
            return total, ""
        return 0, "需要管理员权限才能查看还原点占用"
    except Exception as e:                                        # noqa: BLE001
        return 0, f"读取失败：{e}"
