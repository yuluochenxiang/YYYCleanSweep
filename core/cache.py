"""扫描结果缓存（目录 / 单文件级）。

用 Python 内置 sqlite3（零依赖），把「一个目录或文件的占用汇总」按
(规范化路径, inode, mtime) 缓存到 %APPDATA%\\YYYCleanSweep\\cache.db。
下一次扫描时，如果 inode + mtime 都没变，直接复用上次算好的
(size, count, newest)，免去大目录的重复 stat——项目清理页第二次扫描近乎瞬时。

线程安全：模块级 Lock 串行化访问 + 每次操作都开一个短连接（sqlite 自身有
文件锁，跨线程也安全）。扫描在 ThreadPool 多线程里跑，这里必须稳。

降级：APPDATA 不可写、或建库/查询异常时，一律安全返回 None / 静默，
扫描照常走真扫，绝不因为缓存坏了而崩。
"""
import os
import time
import sqlite3
import threading

from .paths import store_dir

_lock = threading.Lock()
_DB_PATH = None

# 缓存结构版本戳：升大版本或表结构变了就整体失效
SCHEMA_VERSION = 1

# 缓存有效期（秒）：默认 7 天。
# 为什么必须限时——缓存存的是 dir_size() 递归整个子树的汇总，而失效判定只比对
# 目标目录「自身」的 inode + mtime。NTFS 下子目录里的文件变化不会更新祖先目录的
# mtime（典型如浏览器 Cache：数据写在下一层 data_0 里），光靠 mtime 会让汇总长期
# 停在首次扫描的数字上，用户会觉得「重新扫描结果不变」。加 TTL 兜底：再久也会重扫。
TTL_SECONDS = 7 * 24 * 3600

# ---- 空间快照：每次完整扫描留一份「每个目录多大」，用来对比「谁涨了」。
# 与 dir_cache 同库、另开一张表；**故意不加 TTL**——快照越老越有价值。
# 滚动保留：同一个扫描根只留最近这么多批，否则 db 会随时间无限膨胀。
MAX_SNAPSHOTS = 30
_snap_keep = MAX_SNAPSHOTS


def _db_path():
    global _DB_PATH
    if _DB_PATH is None:
        d = store_dir()                       # %APPDATA%\\YYYCleanSweep
        _DB_PATH = os.path.join(d, "cache.db")
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
        """CREATE TABLE IF NOT EXISTS dir_cache (
            pkey     TEXT PRIMARY KEY,
            norm_path TEXT NOT NULL,
            size     INTEGER NOT NULL,
            fcount   INTEGER NOT NULL,
            newest   REAL NOT NULL,
            mtime    REAL NOT NULL,
            inode    TEXT NOT NULL,
            ver      INTEGER NOT NULL,
            scan_time REAL NOT NULL
        )"""
    )
    conn.commit()


def _norm_key(path):
    """规范化成一个跨会话稳定的 key：消斜杠、盘符大写（Windows 不区分大小写）。"""
    p = os.path.normpath(path).replace("/", "\\")
    if len(p) >= 2 and p[1] == ":":
        p = p[0].upper() + p[1:]
    return p


def _ident(path):
    """返回 (key, inode_str, mtime)。取不到身份（已删/无权限）则 inode 为空串。"""
    key = _norm_key(path)
    try:
        st = os.stat(path)
        return key, f"{st.st_dev}:{st.st_ino}", st.st_mtime
    except OSError:
        return key, "", 0.0


def get_dir(path):
    """取目录/文件的缓存汇总。

    命中且未失效返回 (size, count, newest)；否则（无记录 / inode 变了 /
    mtime 变了超过 1 秒 / 超过 TTL / 版本不对 / 任何异常）一律返回 None，
    让扫描走真扫。
    """
    key, ino, mtime = _ident(path)
    with _lock:
        conn = _connect()
        if conn is None:
            return None
        try:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT size, fcount, newest, mtime, inode, ver, scan_time "
                "FROM dir_cache WHERE pkey=?", (key,)).fetchone()
            if not row:
                return None
            size, fcount, newest, cmtime, cino, ver, cscan = row
            if ver != SCHEMA_VERSION:
                return None
            # 太旧也作废：子树里的变化可能压根没反映到目录自身 mtime 上，
            # 靠 TTL 保证「再久也会重新真扫一次」，不会永远停在旧数字。
            if time.time() - (cscan or 0.0) > TTL_SECONDS:
                return None
            # inode 变了（目录被重建/移动）或 mtime 变了（内容增删改名）即失效
            if cino != ino or abs(cmtime - mtime) > 1.0:
                return None
            return size, fcount, newest
        except sqlite3.Error:
            return None
        finally:
            conn.close()


def put_dir(path, size, count, newest):
    """写回 / 覆盖目录或文件的缓存汇总。取不到身份、或任何异常都静默跳过。"""
    key, ino, mtime = _ident(path)
    if not ino:
        return
    now = time.time()
    with _lock:
        conn = _connect()
        if conn is None:
            return
        try:
            _ensure_schema(conn)
            conn.execute(
                """INSERT INTO dir_cache
                       (pkey, norm_path, size, fcount, newest, mtime, inode, ver, scan_time)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(pkey) DO UPDATE SET
                     size=excluded.size, fcount=excluded.fcount,
                     newest=excluded.newest, mtime=excluded.mtime,
                     inode=excluded.inode, ver=excluded.ver,
                     scan_time=excluded.scan_time""",
                (key, _norm_key(path), size, count, newest, mtime,
                 ino, SCHEMA_VERSION, now))
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            conn.close()


def clear():
    """清空全部缓存（设置页「清除缓存」用）。"""
    with _lock:
        conn = _connect()
        if conn is None:
            return
        try:
            conn.execute("DROP TABLE IF EXISTS dir_cache")
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            conn.close()


def stats():
    """返回 (条目数, 磁盘占用字节)；库不存在时 (0, 0)。"""
    with _lock:
        conn = _connect()
        if conn is None:
            return 0, 0
        try:
            _ensure_schema(conn)
            n = conn.execute("SELECT COUNT(*) FROM dir_cache").fetchone()[0]
        except sqlite3.Error:
            n = 0
        finally:
            conn.close()
    try:
        sz = os.path.getsize(_db_path())
    except OSError:
        sz = 0
    return n, sz


# ------------------------------------------------------------------ 空间快照
# 与 dir_cache 同库、独立一张表。用来回答「相比上次，哪些目录变大了」。
# 设计要点：
#   1) 不加 TTL——快照的价值恰恰在于它足够旧，过期清理会让这个功能失效；
#   2) 按 (root, ts) 分批改量淘汰，避免 db 随时间无限膨胀；
#   3) 任何异常都静默降级：快照坏了不能影响扫描本身。
def _ensure_snapshot_schema(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS snapshot (
            root   TEXT NOT NULL,
            ts     REAL NOT NULL,
            path   TEXT NOT NULL,
            size   INTEGER NOT NULL,
            fcount INTEGER NOT NULL,
            PRIMARY KEY (root, ts, path)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_snap_root_ts ON snapshot(root, ts)")
    conn.commit()


def set_snapshot_keep(n):
    """设置每个扫描根保留多少批快照（立即生效，下次写入时淘汰）。"""
    global _snap_keep
    try:
        n = int(n)
    except (TypeError, ValueError):
        return
    _snap_keep = max(1, min(500, n))


def snapshot_batches(root, limit=None):
    """这个扫描根下已有的批次时间戳（新的在前）。库不可用时返回 []。"""
    key = _norm_key(root)
    with _lock:
        conn = _connect()
        if conn is None:
            return []
        rows = []
        try:
            _ensure_snapshot_schema(conn)
            rows = conn.execute(
                "SELECT DISTINCT ts FROM snapshot WHERE root=? ORDER BY ts DESC",
                (key,)).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
    ts = [r[0] for r in rows]
    return ts[:limit] if limit else ts


def put_snapshot(root, rows):
    """写一批快照，返回本次批次的时间戳。

    rows : [(path, size, count)]，一条一个目录。
    写完按保留份数滚动淘汰旧批次——这是本表唯一可能失控的地方。
    """
    key = _norm_key(root)
    ts = time.time()
    data = [(key, ts, _norm_key(p), int(s), int(c)) for p, s, c in rows]
    with _lock:
        conn = _connect()
        if conn is None:
            return ts
        try:
            _ensure_snapshot_schema(conn)
            conn.executemany(
                "INSERT OR REPLACE INTO snapshot (root, ts, path, size, fcount) "
                "VALUES (?,?,?,?,?)", data)
            stale = conn.execute(
                "SELECT DISTINCT ts FROM snapshot WHERE root=? "
                "ORDER BY ts DESC LIMIT -1 OFFSET ?", (key, _snap_keep)).fetchall()
            for (old,) in stale:
                conn.execute(
                    "DELETE FROM snapshot WHERE root=? AND ts=?", (key, old))
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return ts


def diff_snapshots(root, ts_old, ts_new, limit=200):
    """对比两个批次，返回 [(path, old_size, new_size, delta)]，按涨幅降序。

    只报「两次都在」的路径：新增和删除的目录不算增长。
    大小没变的直接跳过，免得用户看一屏 0。

    ⚠️ **扫描根自身必须排除**——根的占用是所有子目录之和，把它算进来的话
    「总共增加了多少」会被重复叠加一次（根 +200 里本来就含子目录的 +200），
    界面上还会冒出一个等于整个范围的假目录，点进去毫无意义。
    """
    key = _norm_key(root)
    old_d, new_d = {}, {}
    with _lock:
        conn = _connect()
        if conn is None:
            return []
        try:
            _ensure_snapshot_schema(conn)
            old_d = dict(conn.execute(
                "SELECT path, size FROM snapshot WHERE root=? AND ts=?",
                (key, ts_old)).fetchall())
            new_d = dict(conn.execute(
                "SELECT path, size FROM snapshot WHERE root=? AND ts=?",
                (key, ts_new)).fetchall())
        except sqlite3.Error:
            return []
        finally:
            conn.close()
    out = []
    for p, ns in new_d.items():
        if p == key:
            continue
        old_sz = old_d.get(p)
        if old_sz is None or old_sz == ns:
            continue
        out.append((p, old_sz, ns, ns - old_sz))
    out.sort(key=lambda x: x[3], reverse=True)
    return out[:limit]


def snapshot_stats():
    """返回 (批次数, 行数)。批次数按 (root, ts) 去重计。"""
    with _lock:
        conn = _connect()
        if conn is None:
            return 0, 0
        try:
            _ensure_snapshot_schema(conn)
            b = conn.execute(
                "SELECT COUNT(*) FROM (SELECT DISTINCT root, ts FROM snapshot)"
            ).fetchone()[0]
            n = conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0]
        except sqlite3.Error:
            b, n = 0, 0
        finally:
            conn.close()
    return b, n


def clear_snapshots():
    """清空全部空间快照。只删 snapshot 表，绝不碰 dir_cache。"""
    with _lock:
        conn = _connect()
        if conn is None:
            return
        try:
            conn.execute("DROP TABLE IF EXISTS snapshot")
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            conn.close()
