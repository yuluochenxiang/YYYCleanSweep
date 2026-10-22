r"""把源码打成一份「换台电脑解压就能跑」的精简 zip（发版备份用）。

只收运行/二次开发真正需要的东西，过程产物一律不收：
不收 `_shot*.png` 截图、`_*.py` 调试脚本、`build_*.log` / `smoke_*.log` 日志、
`__pycache__`、设计稿 html、配置备份。实测 43 个文件、原始 731 KB → 压缩后 233 KB。

用法：
    python tools/make_src_archive.py                  # 出到 E:\work\WB_\
    python tools/make_src_archive.py D:\\backup        # 指定输出目录
"""
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 需要收进去的目录（只收指定后缀，自动跳过 __pycache__ 等子目录）
DIRS = {
    "core": (".py",),
    "ui": (".py",),
    "rules": (".json",),
    "docs": (".md",),
    "tools": (".py",),
}
# 需要收进去的根目录文件
FILES = [
    "main.py", "run.py", "run.bat", "requirements.txt",
    "README.md",
    "build.py", "build.bat",
    "smoke_test.py",
    "YYYCleanSweep.spec", "YYYCleanSweepFast.spec",
]


def read_version():
    """从 core/version.py 读出版本号，避免手抄错。"""
    path = os.path.join(HERE, "core", "version.py")
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("APP_VERSION"):
                return line.split("=", 1)[1].strip().strip('"\'')
    return "unknown"


def main():
    """打包源码 zip。输出目录可跟一个参数，默认出到 E:\\work\\WB_\\。"""
    out_dir = sys.argv[1] if len(sys.argv) > 1 else r"E:\work\WB_"
    ver = read_version()
    top = f"YYYCleanSweep_src_v{ver}"
    zip_path = os.path.join(out_dir, top + ".zip")
    os.makedirs(out_dir, exist_ok=True)

    n = 0
    raw = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for d, exts in DIRS.items():
            base = os.path.join(HERE, d)
            for name in sorted(os.listdir(base)):
                if not name.endswith(exts):
                    continue
                src = os.path.join(base, name)
                if not os.path.isfile(src):
                    continue
                zf.write(src, os.path.join(top, d, name))
                raw += os.path.getsize(src)
                n += 1
        for name in FILES:
            src = os.path.join(HERE, name)
            if not os.path.exists(src):
                print("  [跳过] 不存在：", name)
                continue
            zf.write(src, os.path.join(top, name))
            raw += os.path.getsize(src)
            n += 1

    comp = os.path.getsize(zip_path)
    print(f"版本：{ver}   收录 {n} 个文件")
    print(f"原始 {raw/1024:.0f} KB  →  压缩后 {comp/1024:.0f} KB")
    print("输出：", zip_path)


if __name__ == "__main__":
    main()
