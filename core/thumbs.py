"""去重文件的缩略图预览（零依赖：Qt 解码 + Windows 外壳缩略图）。

三条通路，按代价从低到高依次尝试：
  1. 图片 —— QImageReader 边解码边降采样到 160px，不把整张原图读进内存。
  2. 视频 / PDF / Office / 音频 / 压缩包 —— Windows 外壳
     IShellItemImageFactory（ctypes 直接调，不引第三方库、不加包体），
     拿到的就是资源管理器里显示的那张封面/首页。
  3. 全类型兜底 —— QFileIconProvider 给系统文件图标。

任何一步失败都静默降级（最多退化成图标），**绝不让预览功能把界面搞崩**；
外壳通道连续失败若干次后本会话内直接关闭它，不再反复踩同一个坑。

缓存与 core/cache.py 同构：模块级 Lock + 每次操作短连接 + 异常静默。
存的是 PNG 二进制，键 = 路径 + mtime + 大小，文件一改就失效。
**只对「当前可见 / 选中的行」生成**（由调用方保证），不做全量预生成；
条数与总字节双重上限，超限按 LRU 淘汰，磁盘占用有硬约束。

注意：本模块依赖 PySide6（QImage），只被 UI 层引用，core 的其它模块不用 Qt。
"""
import os
import time
import sqlite3
import threading

from PySide6.QtCore import QBuffer, QByteArray, QFileInfo, QIODevice, QSize, Qt
from PySide6.QtGui import QImage, QImageReader
from PySide6.QtWidgets import QFileIconProvider

from .paths import store_dir

_lock = threading.Lock()
_DB_PATH = None

# 表结构版本戳：改结构就整体失效
SCHEMA_VERSION = 1

# 统一长边 160px：详情面板直接用，列表 24px 由同一张缩放得到，不做第二套缓存
THUMB_SIZE = 160

# 缓存上限（先到先限）。设置页可调，改完立即生效
MAX_ITEMS = 2000
MAX_BYTES = 48 * 1024 * 1024

# 外壳缩略图连续失败多少次就本会话内放弃（个别机器缺 shell 组件）
_SHELL_FAIL_LIMIT = 5
_shell_fails = 0
_shell_off = False
_tls = threading.local()               # 记录本线程是否已 CoInitialize

IMG_EXT = frozenset((
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff",
    ".ico", ".jfif", ".heic", ".heif", ".avif", ".psd", ".svg",
))
VID_EXT = frozenset((
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".mpg", ".mpeg", ".ts", ".rmvb", ".rm", ".3gp", ".vob", ".m2ts",
))
DOC_EXT = frozenset((
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".rtf",
))
AUD_EXT = frozenset((".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".wma", ".ape"))
ARC_EXT = frozenset((".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".iso", ".cab"))

# 值得生成缩略图的类型。.dll/.sys/.log 之类给了也没意义，直接显示图标
WANTED_EXT = IMG_EXT | VID_EXT | DOC_EXT | AUD_EXT | ARC_EXT | frozenset(
    (".exe", ".msi"))


# --------------------------------------------------------------- 库
def _db_path():
    global _DB_PATH
    if _DB_PATH is None:
        _DB_PATH = os.path.join(store_dir(), "thumbs.db")
    return _DB_PATH


def db_file():
    """缓存数据库的完整路径（设置页显示给用户、给他自己核大小用）。"""
    return _db_path()


def _connect():
    try:
        return sqlite3.connect(_db_path(), timeout=30)
    except sqlite3.Error:
        return None


def _ensure_schema(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS thumb (
            tkey    TEXT PRIMARY KEY,
            png     BLOB NOT NULL,
            bytes   INTEGER NOT NULL,
            used_at REAL NOT NULL
        )"""
    )
    conn.commit()


def _key_of(path):
    """键 = 规范化路径 + mtime + 大小：文件被改动就自然失效，不会显示过期图。"""
    p = os.path.normpath(path).replace("/", "\\")
    if len(p) >= 2 and p[1] == ":":
        p = p[0].upper() + p[1:]
    try:
        st = os.stat(path)
        return "%s|%d|%d" % (p, int(st.st_mtime), st.st_size)
    except OSError:
        return p + "|0|0"


# --------------------------------------------------------------- 对外：缓存读写
def cached(path):
    """取缓存里的缩略图 QImage；没有 / 异常一律返回 None（调用方再异步生成）。"""
    key = _key_of(path)
    with _lock:
        conn = _connect()
        if conn is None:
            return None
        try:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT png FROM thumb WHERE tkey=?", (key,)).fetchone()
            if not row:
                return None
            data = row[0]
            conn.execute("UPDATE thumb SET used_at=? WHERE tkey=?",
                         (time.time(), key))
            conn.commit()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
    try:
        img = QImage.fromData(QByteArray(bytes(data)))
        return img if not img.isNull() else None
    except Exception:
        return None


def store(path, img):
    """把生成好的缩略图写进缓存（PNG）。超限先按 LRU 淘汰。"""
    if img is None or img.isNull():
        return
    try:
        ba = QByteArray()
        buf = QBuffer(ba)
        if not buf.open(QIODevice.WriteOnly):
            return
        ok = img.save(buf, "PNG")
        buf.close()
        if not ok:
            return
        blob = bytes(ba.data())
    except Exception:
        return
    if not blob:
        return
    key = _key_of(path)
    with _lock:
        conn = _connect()
        if conn is None:
            return
        try:
            _ensure_schema(conn)
            conn.execute(
                """INSERT INTO thumb (tkey, png, bytes, used_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(tkey) DO UPDATE SET
                     png=excluded.png, bytes=excluded.bytes,
                     used_at=excluded.used_at""",
                (key, sqlite3.Binary(blob), len(blob), time.time()))
            conn.commit()
            _trim(conn)
        except sqlite3.Error:
            pass
        finally:
            conn.close()


def _trim(conn):
    """条数 / 总字节任一超限就按最久未访问淘汰（调用方已持锁且已建表）。"""
    try:
        n, total = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(bytes),0) FROM thumb").fetchone()
        if n <= MAX_ITEMS and total <= MAX_BYTES:
            return
        rows = conn.execute(
            "SELECT tkey, bytes FROM thumb ORDER BY used_at ASC").fetchall()
        drop = []
        for k, b in rows:
            # n/total 循环内已经同步递减，这里不能再减一次 len(drop)
            if n <= MAX_ITEMS and total <= MAX_BYTES:
                break
            drop.append(k)
            n -= 1
            total -= (b or 0)
        if drop:
            conn.executemany("DELETE FROM thumb WHERE tkey=?",
                             [(k,) for k in drop])
            conn.commit()
    except sqlite3.Error:
        pass


def clear():
    """清空缩略图缓存（设置页「清除」按钮用）。"""
    with _lock:
        conn = _connect()
        if conn is None:
            return
        try:
            conn.execute("DROP TABLE IF EXISTS thumb")
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            conn.close()


def stats():
    """返回 (条目数, 磁盘占用字节)。"""
    with _lock:
        conn = _connect()
        if conn is None:
            return 0, 0
        try:
            _ensure_schema(conn)
            n = conn.execute("SELECT COUNT(*) FROM thumb").fetchone()[0]
        except sqlite3.Error:
            n = 0
        finally:
            conn.close()
    try:
        sz = os.path.getsize(_db_path())
    except OSError:
        sz = 0
    return n, sz


def set_limits(max_items=None, max_bytes=None):
    """设置页改上限：立即生效，下次写入时按新上限淘汰。"""
    global MAX_ITEMS, MAX_BYTES
    if max_items:
        MAX_ITEMS = max(100, int(max_items))
    if max_bytes:
        MAX_BYTES = max(4 * 1024 * 1024, int(max_bytes))


# --------------------------------------------------------------- 对外：生成
def wanted(path):
    """这个文件值得生成缩略图吗？不在白名单里就直接显示系统图标。"""
    if not path:
        return False
    return os.path.splitext(path)[1].lower() in WANTED_EXT


def render(path, size=THUMB_SIZE):
    """真正生成一张缩略图（较慢，只在后台线程调用）。失败返回 None。

    图片走 Qt 解码；其它类型问 Windows 外壳要封面/首页；都拿不到就退到
    系统文件图标——保证「永远有东西可显示」。
    """
    if not path or not os.path.exists(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    img = None
    try:
        if ext in IMG_EXT:
            img = _render_image(path, size)
        if img is None:
            img = _shell_thumb(path, size)
        if img is None:
            img = _icon_image(path, size)
    except Exception:
        img = None
    return img


def fit(img, size):
    """等比缩到长边 size，保持比例不变形（列表小图由同一张 160px 缩放得到）。"""
    if img is None or img.isNull():
        return None
    if img.width() <= size and img.height() <= size:
        return img
    return img.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _render_image(path, size):
    """图片：让解码器直接降采样输出小图，避免把几十 MB 的原图整个读进来。"""
    try:
        r = QImageReader(path)
        if not r.canRead():
            return None
        w, h = r.size().width(), r.size().height()
        if w > 0 and h > 0:
            k = size / float(max(w, h))
            if k < 1.0:
                r.setScaledSize(QSize(max(1, int(w * k)), max(1, int(h * k))))
        img = r.read()
        return None if img.isNull() else img.convertToFormat(
            QImage.Format_ARGB32)
    except Exception:
        return None


def _icon_image(path, size):
    """兜底：系统文件图标。"""
    try:
        icon = QFileIconProvider().icon(QFileInfo(path))
        if icon.isNull():
            return None
        pm = icon.pixmap(size, size)
        return None if pm.isNull() else pm.toImage().convertToFormat(
            QImage.Format_ARGB32)
    except Exception:
        return None


# --------------------------------------------------------------- Windows 外壳
def _shell_thumb(path, size=THUMB_SIZE):
    """问 Windows 外壳要缩略图：视频封面帧、PDF 首页、Office 文档、程序图标等。

    走 IShellItemImageFactory::GetImage，拿到的 HBITMAP 再用 GetDIBits 转成
    QImage。全程 try/except——机器缺 shell 组件、文件被占用、解码器不支持
    都只是「没图」，不影响别的功能。
    """
    global _shell_fails, _shell_off
    if os.name != "nt" or _shell_off:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)

        # COM 单线程套间：外壳缩略图接口要求 STA。每个工作线程只初始化一次
        if not getattr(_tls, "co", False):
            ole32.CoInitializeEx(None, 0x2)      # COINIT_APARTMENTTHREADED
            _tls.co = True

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                        ("Data3", ctypes.c_ushort),
                        ("Data4", ctypes.c_ubyte * 8)]

        # IShellItemImageFactory: {bcc18b79-ba16-442f-80c4-8a59c30c463b}
        iid = GUID(0xBCC18B79, 0xBA16, 0x442F,
                   (ctypes.c_ubyte * 8)(0x80, 0xC4, 0x8A, 0x59,
                                        0xC3, 0x0C, 0x46, 0x3B))

        class SIZE(ctypes.Structure):
            _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

        shell32.SHCreateItemFromParsingName.argtypes = [
            ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p)]
        shell32.SHCreateItemFromParsingName.restype = ctypes.c_long

        pobj = ctypes.c_void_p()
        hr = shell32.SHCreateItemFromParsingName(path, None,
                                                 ctypes.byref(iid),
                                                 ctypes.byref(pobj))
        if hr != 0 or not pobj:
            return None

        # pobj 是「接口指针」，指向对象；对象的第一个字段才是虚表指针，
        # 所以要多解一层。（只解一层会把对象的数据字段当成函数指针，
        # 一调用就 access violation —— 踩过。）
        first = ctypes.cast(pobj.value, ctypes.POINTER(ctypes.c_void_p))[0]
        vt = ctypes.cast(ctypes.cast(first, ctypes.c_void_p).value,
                         ctypes.POINTER(ctypes.c_void_p))
        # 虚表布局：IShellItemImageFactory 直接继承 IUnknown，自身只有 GetImage
        # 一个方法 —— 虚表共 4 项 [QI, AddRef, Release, GetImage]，索引 3。
        GETIMAGE = 3
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, SIZE,
                                   ctypes.c_int,
                                   ctypes.POINTER(wintypes.HBITMAP))
        fn = ctypes.cast(vt[GETIMAGE], ctypes.c_void_p).value
        get_image = proto(fn)
        hbmp = wintypes.HBITMAP()
        # 0 = SIIGBF_RESIZETOFIT：按我们要的尺寸缩放；拿不到缩略图时外壳
        # 会退化成图标，正好当兜底用
        hr = get_image(pobj, SIZE(size, size), 0, ctypes.byref(hbmp))
        img = None
        if hr == 0 and hbmp:
            img = _hbitmap_to_image(gdi32, user32, hbmp)
            gdi32.DeleteObject(hbmp)
        try:                                   # Release
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
                ctypes.cast(vt[2], ctypes.c_void_p).value)(pobj)
        except Exception:
            pass
        if img is None:
            _shell_fails += 1
            if _shell_fails >= _SHELL_FAIL_LIMIT:
                _shell_off = True
        else:
            _shell_fails = 0
        return img
    except Exception:
        _shell_fails += 1
        if _shell_fails >= _SHELL_FAIL_LIMIT:
            _shell_off = True
        return None


def _hbitmap_to_image(gdi32, user32, hbmp):
    """HBITMAP -> QImage：拿 DIB 位图数据自己拼 BGRA。"""
    import ctypes
    from ctypes import wintypes

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                    ("bmHeight", ctypes.c_long),
                    ("bmWidthBytes", ctypes.c_long),
                    ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                    ("bmBits", ctypes.c_void_p)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    bm = BITMAP()
    if not gdi32.GetObjectW(hbmp, ctypes.sizeof(bm), ctypes.byref(bm)):
        return None
    w, h = int(bm.bmWidth), int(abs(bm.bmHeight))
    if w <= 0 or h <= 0 or w > 4096 or h > 4096:
        return None

    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(bmi)
    bmi.biWidth = w
    bmi.biHeight = -h                  # 负号 = 自上而下，省掉翻转
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0              # BI_RGB
    stride = ((w * 4 + 3) // 4) * 4
    buf = (ctypes.c_ubyte * (stride * h))()

    hdc = user32.GetDC(None)
    if not hdc:
        return None
    try:
        got = gdi32.GetDIBits(hdc, hbmp, 0, h, buf, ctypes.byref(bmi), 0)
    finally:
        user32.ReleaseDC(None, hdc)
    if not got:
        return None

    img = QImage(bytes(buf), w, h, stride, QImage.Format_ARGB32)
    return img.copy()                  # 脱离 buf 的生命周期
