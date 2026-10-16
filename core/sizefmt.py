"""全局统一的容量 / 百分比格式化。

以前界面、清理器、日志里各写了一份字节格式化，位数和单位口径不一致
（有的到 TB 就溢出成 PB，有的小数位数不一样）。这里定一套，三处都用它，
界面上出现的容量表现就完全一致了。

口径：
- 1024 进制，单位 B / KB / MB / GB / TB
- 小于 10 时保留 1 位小数（1.5 GB），大于等于 10 时取整（38 GB），
  这样列表里对齐得更好，也避免 "1.5 GB" 和 "1536 MB" 混排
- B 永远是整数
"""
UNITS = ("B", "KB", "MB", "GB", "TB")


def fmt_size(n, prec=1):
    """字节数 -> 人类可读字符串。全项目统一口径。"""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        n = 0.0
    if n < 0:
        n = 0.0
    if n < 1024:
        return f"{n:.0f} B"
    v = n
    unit = "B"
    for u in UNITS[1:]:
        v /= 1024.0
        unit = u
        if v < 1024 or u == "TB":
            break
    if v >= 100 or (unit == "TB" and v >= 10):
        return f"{v:.0f} {unit}"
    if v >= 10:
        return f"{v:.0f} {unit}"
    return f"{v:.{prec}f} {unit}"


def fmt_bytes(n):
    """精确字节数，带千分位。用于悬浮提示，让用户能看到到底多大。"""
    try:
        return f"{int(n or 0):,}".replace(",", ",")
    except (TypeError, ValueError):
        return "0"


def fmt_pct(part, total, prec=1):
    """占比，总数非法时返回 "—"。"""
    try:
        total = float(total or 0)
    except (TypeError, ValueError):
        total = 0.0
    if total <= 0:
        return "—"
    return f"{float(part or 0) * 100.0 / total:.{prec}f}%"


def parse_size(s):
    """人类可读容量 -> 字节（"1.2 GB" -> 1288490188.8）。解析不了返回 None。

    列表里给「点表头排序」用：容量列显示的是 "1.2 GB" 这种文本，
    直接按字符串排会排成 "1.2 GB" < "900 MB" 的错序，得还原成真实数值。
    """
    import re
    m = re.match(r"^\s*[≥~]?\s*([\d.]+)\s*([KMGT]?B|B)?\s*$",
                 str(s or ""), re.IGNORECASE)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except (TypeError, ValueError):
        return None
    unit = (m.group(2) or "B").upper()
    if unit not in UNITS:
        return None
    return v * (1024.0 ** UNITS.index(unit))


def parse_pct(s):
    """"12.3%" -> 12.3。解析不了返回 None。"""
    try:
        return float(str(s or "").strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def pct_of(part, total):
    """0.0 ~ 1.0 的占比，用于画条形图。"""
    try:
        total = float(total or 0)
    except (TypeError, ValueError):
        return 0.0
    if total <= 0:
        return 0.0
    x = float(part or 0) / total
    return 0.0 if x < 0 else (1.0 if x > 1 else x)
