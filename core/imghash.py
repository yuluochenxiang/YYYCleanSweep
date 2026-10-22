"""图片感知哈希（dHash）与相似聚类 —— 「相似查找」的算法核心。

⚠️ **这是全项目唯一一个 import QtGui 的 core 模块，属于登记在案的例外。**

例外理由：dHash 要先把 JPEG / PNG 解码并缩到 9×8 灰度图。Qt 自带的 `QImage`
是全项目唯一「不引入第三方依赖」的解码方案 —— 用 Pillow 的话成品 exe 要多 5 MB
左右。论证见 `docs/扩展计划_相似图片去重.md`；「守住零新增依赖」比省这几行代码重要。

约束：本模块**只允许 import QtGui**（不含 QtWidgets、不含任何控件类），
并且**永远不创建 QApplication / 任何窗口**。它只在后台线程里被调用。

算法：dHash（差值哈希）
    1. 解码时直接按目标尺寸降采样（QImageReader.setScaledSize），不必先把原图读进内存
    2. 转成 8 位灰度
    3. 逐行比较相邻像素：右边比左边亮记 1，否则记 0 —— 得到 64 位整数
    4. 两张图的哈希做异或，数 1 的个数（汉明距离）；≤ 阈值判定为相似
    对「缩放、加水印、轻微裁剪、重新压缩」都耐操，但对旋转 > 几度不敏感（见风险清单）。
"""
from PySide6.QtCore import QSize
from PySide6.QtGui import QImage, QImageReader

HASH_BITS = 64
_SAMPLE_W = 9          # 比 SAMPLE_H 多一列：相邻列差分要用
_SAMPLE_H = 8

# 参与相似查找的图片格式。刻意不放 .ico / .svg：
# 图标是 16~256px 的方块图，哈希极易撞；svg 是矢量，缩放后像素完全不是一回事。
SIM_EXTS = frozenset((
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff",
    ".jfif",
))
SKIP_EXTS = frozenset((".ico", ".svg"))

# 边长门槛：太小的图是图标/表情，太大的是全景图/扫描件，都不适合做像素比对
MIN_EDGE = 128
MAX_EDGE = 6000

# 系统自带墙纸 / 主题目录：里面成百上千张微软官方图，互相之间确实"相似"，
# 但删它们毫无意义，还徒增误判噪音
SKIP_DIR_KEYS = (
    "\\windows\\web\\",
    "\\windows\\resources\\",
    "web\\wallpaper\\",
    "\\microsoft\\windows\\themes\\",
)

# 三档阈值（用户 2026-10-17 拍板：默认严格）
LEVEL_STRICT = 6
LEVEL_NORMAL = 10
LEVEL_LOOSE = 16
LEVEL_LABELS = ["严格", "标准", "宽松"]
LEVEL_VALUES = [LEVEL_STRICT, LEVEL_NORMAL, LEVEL_LOOSE]
LEVEL_HINTS = [
    "几乎同一张图（推荐）",
    "同一张图的明显改版",
    "构图相近即算（误判多）",
]


def is_candidate(path):
    """扩展名是否参与相似查找。只做后缀判断，不碰磁盘。"""
    ext = _ext_of(path)
    return ext in SIM_EXTS and ext not in SKIP_EXTS


def is_vector(path):
    """是不是「看着像图、但不该参与比对」的那类（.ico / .svg）。

    单独拉出来是为了统计口径：这两类要能出现在「已跳过矢量图」里，
    而 .txt / .mp4 这些压根不算图片，不该占跳过数。
    """
    return _ext_of(path) in SKIP_EXTS


def skip_reason(path, w, h):
    """返回跳过原因；返回 None 表示这张图可以算哈希。

    四条排除规则（默认全部生效）：
      1. .ico / .svg —— 矢量图与小图标
      2. 边长 < 128px —— 表情、头像缩略图
      3. 边长 > 6000px —— 扫描件、全景图，比对没意义且很慢
      4. 系统自带墙纸 / 主题目录
    """
    ext = _ext_of(path)
    if ext in SKIP_EXTS:
        return "矢量或不适合比对的图"
    if w and h and (w < MIN_EDGE or h < MIN_EDGE):
        return "尺寸过小"
    if w and h and (w > MAX_EDGE or h > MAX_EDGE):
        return "尺寸过大"
    low = (path or "").lower().replace("/", "\\")
    while "\\\\" in low:                 # 折叠连续分隔符，跨环境路径写法都吃得住
        low = low.replace("\\\\", "\\")
    for key in SKIP_DIR_KEYS:
        if key in low:
            return "系统自带图片目录"
    return None


def size_of(path):
    """读图片原始像素尺寸（不解码内容）。读不到返回 (0, 0)。"""
    try:
        rd = QImageReader(path)
        s = rd.size()
        return int(s.width()), int(s.height())
    except Exception:                                            # noqa: BLE001
        return 0, 0


def dhash(path):
    """算一张图的感知哈希。返回 64 位整数；失败返回 None。"""
    try:
        rd = QImageReader(path)
        rd.setAutoTransform(True)        # ← 手机竖拍的 EXIF 旋转，漏了会全错
        rd.setScaledSize(QSize(_SAMPLE_W, _SAMPLE_H))
        img = rd.read()
        if img.isNull():
            return None
        img = img.convertToFormat(QImage.Format_Grayscale8)
        if img.isNull():
            return None

        raw = img.constBits()
        if isinstance(raw, memoryview):
            raw = raw.tobytes()
        else:
            raw = bytes(raw)
        stride = int(img.bytesPerLine())   # ← 行有内存对齐，绝不能用宽度当步长

        h = 0
        for y in range(_SAMPLE_H):
            base = y * stride
            row = raw[base:base + _SAMPLE_W]
            if len(row) < _SAMPLE_W:
                return None
            for x in range(_SAMPLE_H):
                h = (h << 1) | (1 if row[x] < row[x + 1] else 0)
        return h
    except Exception:                                            # noqa: BLE001
        return None


def hamming(a, b):
    """两个哈希的汉明距离（0 ~ 64）。"""
    return bin((a ^ b) & ((1 << HASH_BITS) - 1)).count("1")


def similarity(a, b):
    """相似度百分比（0 ~ 100）。"""
    return (HASH_BITS - hamming(a, b)) * 100.0 / HASH_BITS


def _segments(h):
    """把 64 位切成 4 段 16 位，给下面的分桶用。"""
    return [(h >> 48) & 0xFFFF, (h >> 32) & 0xFFFF,
            (h >> 16) & 0xFFFF, h & 0xFFFF]


def group_similar(items, max_dist=LEVEL_STRICT, on_progress=None):
    """把 [(path, hash), ...] 聚类成相似组。

    items      : [(path, hash)]，hash 为空的会被自动跳过
    max_dist   : 汉明距离阈值
    on_progress: 可选回调 (done, total)

    返回 [[idx, ...], ...]，每个子列表是一组（至少 2 个）。

    ⚠️ 朴素两两比对是 O(n²)：5000 张图要算 1250 万次，
    必须先用「位切片」候选筛选 —— 64 位切 4 段 16 位，
    只有至少一段完全相同才可能是距离 ≤ 6 的一对。这样候选量掉几个数量级。
    """
    total = len(items)
    buckets = [{} for _ in range(4)]
    for i, (_p, h) in enumerate(items):
        if not h:
            continue
        for seg_i, v in enumerate(_segments(h)):
            buckets[seg_i].setdefault(v, []).append(i)

    cand = set()
    for b in buckets:
        for idxs in b.values():
            if len(idxs) < 2:
                continue
            if len(idxs) > 400:
                # 超大桶（纯色图、同一张图的大量副本）不能两两比：n² 会炸。
                # 但也不能整个跳过 —— 那样 500 张真副本会全部落单。
                # 折中：桶内按完整哈希再分组，同哈希的串成一条链（O(n)）。
                # 不同哈希的不在这里比，免得纯色图们被硬凑成一组。
                by_hash = {}
                for i in idxs:
                    by_hash.setdefault(items[i][1], []).append(i)
                for same in by_hash.values():
                    for k in range(len(same) - 1):
                        cand.add((same[k], same[k + 1]))
                continue
            for a in range(len(idxs)):
                for c in range(a + 1, len(idxs)):
                    cand.add((idxs[a], idxs[c]))

    parent = list(range(total))

    def find(x):
        """并查集查找（带路径压缩）。"""
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        """并查集合并。这里统一挂到**下标更小**的那个上，
        保证同一组无论怎么合，代表元都是固定的，组号才稳定。"""
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    done = 0
    for a, b in cand:
        done += 1
        if on_progress and done % 2000 == 0:
            on_progress(done, len(cand))
        ha, hb = items[a][1], items[b][1]
        if ha and hb and hamming(ha, hb) <= max_dist:
            union(a, b)

    groups = {}
    for i in range(total):
        if not items[i][1]:
            continue
        groups.setdefault(find(i), []).append(i)
    return [g for g in groups.values() if len(g) > 1]


def _ext_of(path):
    return "." + (path or "").rsplit(".", 1)[-1].lower() if "." in (path or "") else ""
