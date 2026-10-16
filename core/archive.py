"""只读地窥探压缩包内部清单（不解包、不落盘、只读清单）。

支持 zip / tar（含 tar.gz / tgz / tar.bz2 / tar.xz），**不支持 rar / 7z** ——
标准库搞不定这两种，硬做要引入 py7zr 之类的依赖，会把成品 exe 撑大几 MB，
不值得。遇到就明确返回"不支持"，让界面优雅地显示一句说明。

刻意不 import PySide6：这是纯逻辑，离 Larson 界面远得很，方便被任何页面复用。
"""
import os
import zipfile
import tarfile

ARCHIVE_EXTS = {
    ".zip", ".whl", ".jar", ".apk",
    ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
}
UNSUPPORTED_EXTS = {".rar", ".7z", ".cab", ".iso", ".lz", ".arj"}

MAX_MEMBERS = 20000        # 超过就不再统计（超大包的清单能有几百万行）
MAX_TAR_BYTES = 800 * 1024 * 1024   # tar 是流式格式，读清单要把整个文件过一遍


def ext_of(path):
    """取后缀；为了识别 .tar.gz 这类双后缀，特殊判断一下。"""
    name = os.path.basename(path or "").lower()
    for e in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if name.endswith(e):
            return e
    return os.path.splitext(name)[1]


def is_archive(path):
    """是不是「本模块能处理的」压缩包。不在 ARCHIVE_EXTS 里的一律不算，
    省得每选中一个文件都去撞一次 open。"""
    return ext_of(path) in ARCHIVE_EXTS


def peek(path, top=8, max_members=MAX_MEMBERS):
    """读压缩包内部清单，返回一份可以直接拿去画图的字典。

    返回 None 表示"这个文件不该我管"（比如压根不是压缩包）。
    失败时不抛异常，把原因写进 result["error"]，让界面显示一句人话。
    """
    if not path or not os.path.isfile(path):
        return None
    ext = ext_of(path)
    if ext in UNSUPPORTED_EXTS:
        return {"kind": ext, "members": 0, "total": 0, "compressed": None,
                "top": [], "exts": {}, "truncated": False, "encrypted": False,
                "error": f"暂不支持 {ext} 格式"}
    if ext not in ARCHIVE_EXTS:
        return None

    size_on_disk = os.path.getsize(path)
    kind = "tar" if "tar" in ext or ext == ".tgz" else "zip"

    try:
        if kind == "zip":
            return _peek_zip(path, ext, top, max_members, size_on_disk)
        return _peek_tar(path, ext, top, max_members, size_on_disk)
    except Exception as e:                                       # noqa: BLE001
        # 任何异常都不能让界面挂掉 —— 详情面板少一块内容而已
        return {"kind": ext, "members": 0, "total": 0, "compressed": None,
                "top": [], "exts": {}, "truncated": False, "encrypted": False,
                "error": f"读取失败：{type(e).__name__}: {e}"}


def _bump(map_, ext, size):
    """按扩展名累加 {ext: [bytes, count]}。"""
    slot = map_.setdefault(ext or "(无扩展名)", [0, 0])
    slot[0] += int(size or 0)
    slot[1] += 1


def _peek_zip(path, ext, top, max_members, size_on_disk):
    count = 0
    total = 0
    comp = 0
    enc = False
    truncated = False
    items = []
    map_ = {}
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            count += 1
            total += info.file_size
            comp += info.compress_size
            if info.flag_bits & 0x1:
                enc = True
            e = _member_ext(info.filename)
            _bump(map_, e, info.file_size)
            items.append((info.filename, info.file_size))
            if count >= max_members:
                truncated = True
                break
    return _pack(ext, items, map_, count, total, comp, truncated, enc,
                 size_on_disk)


def _peek_tar(path, ext, top, max_members, size_on_disk):
    if size_on_disk > MAX_TAR_BYTES:
        return _pack(ext, [], {}, 0, 0, None, False, False, size_on_disk,
                     error="文件过大（超过 800 MB），未读取内容清单")
    count = 0
    total = 0
    truncated = False
    items = []
    map_ = {}
    with tarfile.open(path, "r:*") as tf:
        for m in tf:
            if not m.isfile():
                continue
            count += 1
            total += m.size
            e = _member_ext(m.name)
            _bump(map_, e, m.size)
            items.append((m.name, m.size))
            if count >= max_members:
                truncated = True
                break
    # tar 是流式容器：整体体积就是压缩后大小
    return _pack(ext, items, map_, count, total, size_on_disk, truncated,
                 False, size_on_disk)


def _member_ext(name):
    return os.path.splitext(name or "")[1].lstrip(".").lower()


def _pack(ext, items, map_, count, total, compressed, truncated, encrypted,
          size_on_disk, error=None):
    """把两处 _peek_* 的结果整理成统一输出。"""
    items.sort(key=lambda t: t[1], reverse=True)
    return {
        "kind": ext,
        "members": count,
        "total": total,
        "compressed": compressed,
        "top": items[:8],
        "exts": map_,
        "truncated": truncated,
        "encrypted": encrypted,
        "size_on_disk": size_on_disk,
        "error": error,
    }
