"""数据存放位置（唯一入口）。

配置、白名单、清理日志、合成音效缓存都放在 %APPDATA%\\YYYCleanSweep 下，
与程序本体分离——重打包 exe、覆盖升级都不会把用户数据弄丢。

以前 config / marks / journal / sound 各写了一份「取目录」的逻辑（四份几乎
一样的代码），现在统一由本模块的 store_dir() 提供，改路径只需改一处。

便携场景（APPDATA 不可写，例如放在只读目录/U 盘上直接跑）会退回到工程
目录；sound 这类可有可无的缓存则允许退回 None，由调用方自行降级。
"""
import os

# 目录名同时用作配置文件前缀、日志文件名前缀
APP_ID = "YYYCleanSweep"


def _fallback_dir():
    """APPDATA 不可写时的退路：工程根目录（core/ 的上两级）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def store_dir(sub=None, fallback=None):
    """返回数据目录（不存在时创建），失败时返回 fallback。

    sub      : 可选子目录名，例如 "sfx"（音效缓存）
    fallback : 创建失败时的返回值；默认退到工程根目录，传 None 则表示
               「失败就不要目录了」，调用方自行降级
    """
    if fallback is None:
        fallback = _fallback_dir()
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_ID)
    if sub:
        d = os.path.join(d, sub)
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        return fallback


def appdata_root():
    """只算路径、不创建目录，用于「关于」页展示配置目录在哪。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_ID)
