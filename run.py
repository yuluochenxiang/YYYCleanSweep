"""YYYCleanSweep 一键运行器 —— 换台电脑解压后双击 `run.bat` 即可。

做的事按顺序：
1. 找可用的 Python：优先用工程自己的 `venv\\Scripts\\python.exe`，没有就用当前 python。
2. 检查 PySide6 在不在：不在就自动用腾讯云镜像装（装失败会给出手动命令）。
3. 启动 `main.py`（用同一个解释器起子进程，输出直接透传）。

只依赖标准库，不联网也能跑（除了第一次装 PySide6 那次）。
"""

import os
import subprocess
import sys

# Windows 控制台可能是 GBK，中文提示先切成 UTF-8，免得乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_PY = os.path.join(HERE, "venv", "Scripts", "python.exe")
MIRROR = "https://mirrors.cloud.tencent.com/pypi/simple"


def pick_python():
    """挑一个能用的解释器：工程 venv 优先，其次当前 python。"""
    if os.path.exists(VENV_PY):
        print(f"[1/3] 使用工程虚拟环境：{VENV_PY}")
        return VENV_PY
    print(f"[1/3] 没有找到工程虚拟环境，改用当前解释器：{sys.executable}")
    return sys.executable


def has_pyside6(py):
    """目标解释器里是否已经装了 PySide6。"""
    try:
        out = subprocess.run(
            [py, "-c", "import PySide6; print(PySide6.__version__)"],
            capture_output=True, text=True, timeout=60)
        return out.returncode == 0, out.stdout.strip()
    except Exception:
        return False, ""


def ensure_pyside6(py):
    """没装就装，装不上就把手动命令打出来。"""
    ok, ver = has_pyside6(py)
    if ok:
        print(f"[2/3] PySide6 已就绪（{ver}）")
        return True
    print("[2/3] 缺少 PySide6，正在用腾讯云镜像安装……")
    cmd = [py, "-m", "pip", "install", "-r",
           os.path.join(HERE, "requirements.txt"), "-i", MIRROR]
    try:
        r = subprocess.run(cmd, timeout=900)
        if r.returncode == 0:
            print("      安装完成。")
            return True
    except subprocess.TimeoutExpired:
        print("      安装超时。")
    print("\n自动安装失败，请手动执行下面两条命令（管理员不需要）：")
    print(f'  "{py}" -m pip install -r "{os.path.join(HERE, "requirements.txt")}" '
          f'-i {MIRROR}')
    return False


def launch(py):
    """启动主程序，输出直接透传，退出码原样返回。"""
    print("[3/3] 启动 YYYCleanSweep……")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([py, os.path.join(HERE, "main.py")],
                          cwd=HERE, env=env).returncode


def main():
    py = pick_python()
    if not ensure_pyside6(py):
        input("\n按回车键退出……")
        return 1
    code = launch(py)
    if code != 0:
        print(f"\n程序退出码：{code}")
        input("按回车键退出……")
    return code


if __name__ == "__main__":
    sys.exit(main())
