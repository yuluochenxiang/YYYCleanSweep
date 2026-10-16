"""界面音效：运行时合成 WAV，用 winsound 异步播放。

不打包任何音频文件，也不联网——所有声音都是几行正弦波现场合成出来的，
所以整个程序依旧是纯离线的。生成结果按音量缓存成小 wav（每个几 KB），
放在 %APPDATA%\\YYYCleanSweep\\sfx 下，改音量时才重新生成。

用 winsound 而不是 QtMultimedia，是为了不额外拖进 Qt 的多媒体插件，
打包体积和启动速度都不受影响。
"""
import os
import math
import struct
import wave
import threading

from .paths import store_dir

try:
    import winsound
except Exception:                                    # 非 Windows 环境直接静音
    winsound = None

SR = 22050          # 采样率：音效很短，22kHz 足够且生成更快

# 每个音效 = 一串 (频率 Hz, 时长秒)。两个音之间是"上行"或"下行"的短促提示。
#
# 按「级别」分组，界面元素的级别越高（越主要、越需要确认），声音越长越饱满：
#   一级 nav      导航（页签 / 分类）—— 温和的上行二音，用来"换了个位置"
#   一级 menu     菜单项（右键菜单）—— 极短促，只是"收到了"
#   二级 click    普通按钮
#   二级 tick     勾选 / 小开关
#   三级 confirm  主操作（开始扫描 / 开始清理）—— 三音上行，最饱满
#   三级 warn     危险 / 删除类
VOICES = {
    "click":      [(1180, 0.030)],
    "tick":       [(1520, 0.020)],
    "check_on":   [(784, 0.035), (1175, 0.055)],
    "check_off":  [(880, 0.040), (588, 0.060)],
    "scan_done":  [(523, 0.070), (659, 0.070), (784, 0.110)],
    "clean_done": [(523, 0.060), (659, 0.060), (784, 0.060), (1047, 0.150)],
    "found":      [(880, 0.050), (1047, 0.080)],
    "warn":       [(392, 0.100), (330, 0.150)],
    "error":      [(294, 0.110), (233, 0.170)],
    "open":       [(698, 0.045), (932, 0.060)],
    # ---- 分级新增
    "nav":        [(494, 0.035), (659, 0.055)],          # 页签 / 分类切换
    "menu":       [(1319, 0.024)],                        # 右键菜单项
    "confirm":    [(440, 0.045), (660, 0.045), (880, 0.095)],   # 主操作
    "toggle":     [(988, 0.030)],                         # 展开 / 折叠
    "delete":     [(330, 0.060), (262, 0.095)],           # 删除类菜单
    "keep":       [(659, 0.040), (880, 0.060)],           # 白名单 / 保留
    "copy":       [(1568, 0.028)],                        # 复制
}

LABELS = {
    "click": "点击", "tick": "轻点", "check_on": "勾选", "check_off": "取消勾选",
    "scan_done": "扫描完成", "clean_done": "清理完成", "found": "有发现",
    "warn": "警告", "error": "出错", "open": "打开",
    "nav": "切换页面", "menu": "菜单项", "confirm": "主操作",
    "toggle": "展开折叠", "delete": "删除", "keep": "加入白名单", "copy": "复制",
}


def _store_dir():
    """音效缓存目录（store_dir 的 sfx 子目录）。拿不到就返回 None 直接降级。"""
    return store_dir("sfx", fallback=None)


def _tone(freq, dur, vol):
    """合成一个带包络的音：快速起振 + 指数衰减，加一点二次谐波让它不发闷。"""
    n = max(1, int(SR * dur))
    buf = bytearray()
    attack = max(1, int(SR * 0.002))
    peak = 21000.0
    for i in range(n):
        t = i / SR
        env = (i / attack) if i < attack else 1.0
        env *= math.exp(-4.0 * (t / dur))
        v = math.sin(2 * math.pi * freq * t)
        v += 0.26 * math.sin(4 * math.pi * freq * t)
        s = int(max(-1.0, min(1.0, v * env * vol)) * peak)
        buf += struct.pack("<h", s)
    return bytes(buf)


def _silence(dur=0.012):
    return b"\x00\x00" * int(SR * dur)


class Sfx:
    """音效播放器。enabled=False 时所有调用都是空操作。"""

    def __init__(self, directory=None, enabled=True, volume=0.42):
        self.dir = directory or _store_dir()
        self.enabled = bool(enabled) and winsound is not None
        self.volume = float(volume)
        self._files = {}
        self._lock = threading.Lock()
        self._built = False
        self._built_vol = None

    # ------------------------------------------------------------ 配置
    def configure(self, enabled=None, volume=None):
        """改开关 / 音量。音量变化超过 0.01 就作废缓存，下次播放前重新合成。"""
        if enabled is not None:
            self.enabled = bool(enabled) and winsound is not None
        if volume is not None:
            v = max(0.0, min(1.0, float(volume)))
            if abs(v - self.volume) > 0.01:
                self.volume = v
                self._built = False          # 音量变了，波形要重算

    @staticmethod
    def supported():
        """当前平台能不能出声（非 Windows 没有 winsound，直接静音降级）。"""
        return winsound is not None

    # ------------------------------------------------------------ 生成
    def prepare(self):
        """预生成全部音效。开销很小（几万次浮点循环），启动时调一次即可。"""
        if not self.enabled or not self.dir:
            return
        if self._built and self._built_vol == self.volume:
            return
        with self._lock:
            for name, seq in VOICES.items():
                self._files[name] = self._make(name, seq)
            self._built = True
            self._built_vol = self.volume

    def _make(self, name, seq=None):
        if not self.dir:
            return None
        seq = seq or VOICES.get(name) or []
        data = bytearray()
        for i, (f, d) in enumerate(seq):
            if i:
                data += _silence()
            data += _tone(f, d, self.volume)
        path = os.path.join(self.dir, f"{name}.wav")
        try:
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes(bytes(data))
            return path
        except OSError:
            return None

    # ------------------------------------------------------------ 播放
    def play(self, name):
        """异步播一个音效。找不到缓存就现生成一次；任何异常都吞掉，不影响界面。"""
        if not self.enabled or winsound is None:
            return
        path = self._files.get(name)
        if not path or not os.path.exists(path):
            if not self._built:
                self.prepare()
            path = self._files.get(name)
        if not path or not os.path.exists(path):
            return
        try:
            winsound.PlaySound(
                path,
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception:
            pass

    def stop(self):
        """立刻掐掉正在响的声音（SND_ASYNC 是异步的，只能靠 PURGE 停）。"""
        if winsound is not None:
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
