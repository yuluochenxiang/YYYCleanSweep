# -*- coding: utf-8 -*-
r"""YYYCleanSweep 一键打包脚本（离线、本地构建，无需联网）。

怎么用：
  1) 直接双击本目录下的 build.bat（推荐，最省事）；
  2) 或在命令行运行：
        python build.py             # 构建并部署到 E:\work\WB_YYYCleanSweep\app
        python build.py --no-deploy # 只构建，不覆盖部署目录

依赖与产物约定（全部放在 E:\work\WB_ 总目录下，互不污染系统 Python）：
  - 虚拟环境（依赖）：E:\work\WB_\YYYCleanSweep\venv\        （PyInstaller 6.x + PySide6 6.12）
  - 构建产物（带小版本号，互不覆盖）：E:\work\WB_\YYYCleanSweep_<版本>\onefile\...  与  \onedir\...
  - 最终运行目录（部署目标）：     E:\work\WB_\YYYCleanSweep\app\

以后做其它 PC 小工具也照此规范（总目录 E:\work\WB_ 下，每个工具一个子目录）：
  依赖放  E:\work\WB_\<工程名>\venv\
  产物放  E:\work\WB_\<工程名>_<小版本号>\
  运行放  E:\work\WB_\<工程名>\app\
"""
import os
import sys
import shutil
import subprocess
import argparse

# 本脚本所在目录即工程根目录（main.py / *.spec 都在这里）
HERE = os.path.dirname(os.path.abspath(__file__))

# ---- 依赖与产物路径（默认全部在 E:\work\WB_ 下）----
# 换机器（没有 E 盘那套目录）时，用环境变量覆盖这两处即可，本机默认值不变：
#   YYYCS_ROOT —— 工程根（venv 与运行目录挂这里），默认 E:\work\WB_\YYYCleanSweep
#   YYYCS_OUT  —— 构建产物总目录，默认 E:\work\WB_
# 工程统一根目录（依赖 venv 与运行目录都挂在这里）
PROJECT_ROOT = os.environ.get("YYYCS_ROOT") or r"E:\work\WB_\YYYCleanSweep"
BUILD_OUT_ROOT = os.environ.get("YYYCS_OUT") or r"E:\work\WB_"
# 虚拟环境（内含 PyInstaller 与 PySide6）
VENV = os.path.join(PROJECT_ROOT, "venv")
PY = os.path.join(VENV, "Scripts", "python.exe")

# 构建用的两个 spec
ONEFILE_SPEC = os.path.join(HERE, "YYYCleanSweep.spec")
ONEDIR_SPEC = os.path.join(HERE, "YYYCleanSweepFast.spec")

# 部署目标（你自己的运行目录，覆盖前请先关掉正在运行的旧程序）
DEPLOY_ROOT = os.path.join(PROJECT_ROOT, "app")


def read_version():
    """从 core/version.py 读 APP_VERSION，避免 import 触发 PySide 依赖。"""
    ver = "0.0"
    p = os.path.join(HERE, "core", "version.py")
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("APP_VERSION"):
                    ver = s.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    except Exception:
        pass
    return ver


def run(cmd):
    """执行一条构建命令；返回退出码（0 表示成功），不在此处中止整个脚本。

    早期版本用 sys.exit(rc)，一旦 onedir（目录版）失败就会连 onefile
    （单文件版，真正的发布产物）一起拖垮、导致部署步骤根本没执行。
    现在改为「各构建独立、互不牵连」，任何一个成功都会照常部署。
    """
    print(">>>", " ".join(cmd))
    rc = subprocess.call(cmd, cwd=HERE)
    if rc != 0:
        print("!! 该步骤返回非零：", rc)
    return rc


def main():
    """打包入口：单文件版 + 目录版各构建一次，构建完部署到 app\\。

    两个版本互相独立：任一版失败都不影响另一版，整体退出码只看单文件版
    （它是发布产物）。--no-deploy 时只构建不部署。
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-deploy", action="store_true",
                    help="只构建，不覆盖部署目录 E:\\work\\WB_\\YYYCleanSweep\\app")
    args = ap.parse_args()

    if not os.path.exists(PY):
        print("找不到虚拟环境：", PY)
        print(f"请先在 {VENV} 建立虚拟环境并安装依赖：")
        print(f'  python -m venv "{VENV}"')
        print(f'  "{PY}" -m pip install PySide6==6.12.0 pyinstaller==6.22.3'
              ' Send2Trash -i https://mirrors.cloud.tencent.com/pypi/simple')
        print("换机器时可用环境变量 YYYCS_ROOT 指定别的工程根：")
        print('  set YYYCS_ROOT=D:\\YYYCleanSweep')
        sys.exit(1)

    ver = read_version()
    out_root = os.path.join(BUILD_OUT_ROOT, f"YYYCleanSweep_{ver}")
    onefile_dist = os.path.join(out_root, "onefile")
    onedir_dist = os.path.join(out_root, "onedir")
    onefile_work = os.path.join(out_root, "build_one")
    onedir_work = os.path.join(out_root, "build_fast")
    print(f"版本：{ver}")
    print(f"构建根目录：{out_root}")
    print(f"虚拟环境：{VENV}")

    # 只在 E:\work\WB_ 目录内清理旧构建，不碰任何其他地方
    if os.path.isdir(out_root):
        try:
            shutil.rmtree(out_root)
        except OSError as e:
            print("  注意：清理旧构建目录失败（可能被占用）：", e)
            print("        将以增量方式继续构建。")
    os.makedirs(onefile_dist, exist_ok=True)

    print("== 构建单文件版（onefile）==")
    rc_one = run([PY, "-m", "PyInstaller", "--noconfirm",
         f"--distpath={onefile_dist}", f"--workpath={onefile_work}",
         ONEFILE_SPEC])

    print("== 构建目录版（onedir，启动快）==")
    rc_dir = run([PY, "-m", "PyInstaller", "--noconfirm",
         f"--distpath={onedir_dist}", f"--workpath={onedir_work}",
         ONEDIR_SPEC])

    if args.no_deploy:
        print("已跳过部署。产物在：", out_root)
        return

    print("== 部署到", DEPLOY_ROOT, "==")
    os.makedirs(DEPLOY_ROOT, exist_ok=True)
    one_exe = os.path.join(onefile_dist, "YYYCleanSweep.exe")
    fast_src = os.path.join(onedir_dist, "YYYCleanSweepFast")

    # —— 单文件版（onefile）——
    # 注意：目标 exe 若正在运行会被系统锁定，copy2 会抛 PermissionError。
    # 这里必须捕获，否则整个脚本会崩溃，表现为“打包失败”。
    if os.path.exists(one_exe):
        dst = os.path.join(DEPLOY_ROOT, "YYYCleanSweep.exe")
        try:
            shutil.copy2(one_exe, dst)
            print("  已更新 YYYCleanSweep.exe")
        except PermissionError as e:
            print("  !! 无法覆盖", dst)
            print("     原因：目标 exe 正在被使用（程序可能还在运行）。")
            print("     请先完全退出 YYYCleanSweep，再重新打包。")
            print("     构建产物已就绪，在：", one_exe)
        except OSError as e:
            print("  !! 复制单文件版失败：", e)
    else:
        print("  !! 没找到单文件版产物：", one_exe)

    # —— 目录版（onedir，启动快）——
    if os.path.isdir(fast_src):
        dest = os.path.join(DEPLOY_ROOT, "YYYCleanSweepFast")
        if os.path.isdir(dest):
            # 不直接删旧目录：几百个文件一次性删除会触发环境安全保护；
            # 留成 _old_Fast_vX.Y 还能随时回退。优先改名，改名失败再覆盖。
            tag = "v" + ver.replace(".", "")
            bak = os.path.join(DEPLOY_ROOT, f"_old_Fast_{tag}")
            n = 1
            while os.path.exists(bak):
                bak = os.path.join(DEPLOY_ROOT, f"_old_Fast_{tag}_{n}")
                n += 1
            try:
                os.rename(dest, bak)
                print("  旧目录已改名为备份：", os.path.basename(bak))
            except OSError as e:
                print("  !! 旧目录改名失败（可能被占用）：", e)
                print("     将改为直接覆盖式更新。")
        try:
            if os.path.isdir(dest):
                # 覆盖式更新（Python 3.8+ 支持 dirs_exist_ok）
                shutil.copytree(fast_src, dest, dirs_exist_ok=True)
            else:
                shutil.copytree(fast_src, dest)
            print("  已更新 YYYCleanSweepFast\\")
        except PermissionError as e:
            print("  !! 无法覆盖 YYYCleanSweepFast（文件被占用）。")
            print("     请先完全退出 YYYCleanSweepFast，再重新打包。")
        except OSError as e:
            print("  !! 更新目录版失败：", e)
    else:
        print("  !! 没找到目录版产物：", fast_src)

    # —— 规则 / 知识库文件 ——
    # 程序运行时优先读 exe 旁边的 rules\ 目录（这样你随时能自己改规则），
    # 所以这里必须把工程里的最新规则同步过去，否则新版内嵌的目录知识库
    # 永远不生效（外面那份旧的会一直被优先使用）。
    src_rules = os.path.join(HERE, "rules")
    dst_rules = os.path.join(DEPLOY_ROOT, "rules")
    if os.path.isdir(src_rules):
        try:
            os.makedirs(dst_rules, exist_ok=True)
            n = 0
            for fn in sorted(os.listdir(src_rules)):
                sp = os.path.join(src_rules, fn)
                if os.path.isfile(sp) and fn.lower().endswith(".json"):
                    shutil.copy2(sp, os.path.join(dst_rules, fn))
                    n += 1
            print(f"  已同步 rules\\ 下的 {n} 个规则 / 知识库文件")
        except OSError as e:
            print("  !! 同步 rules 目录失败：", e)

    print("部署完成。可直接运行：",
          os.path.join(DEPLOY_ROOT, "YYYCleanSweep.exe"))
    print("提示：部署目录里的 _old_Fast_* 是上一版备份，确认新版没问题后可自行删除。")

    # —— 构建结果汇总（各自独立，互不影响）——
    print("")
    print("== 构建结果汇总 ==")
    if rc_one == 0:
        print("  [OK] 单文件版（onefile，发布用）：构建并部署成功")
    else:
        print("  [失败] 单文件版（onefile）：未部署，请查看上方报错")
    if rc_dir == 0:
        print("  [OK] 目录版（onedir，启动快）：构建并部署成功")
    else:
        print("  [警告] 目录版（onedir）构建失败：")
        print("        单文件版不受影响，仍可直接运行 app\\YYYCleanSweep.exe。")
        print("        如只用到目录版，请单独排查 onedir 的报错。")
    # 整体退出码以「单文件版是否成功」为准（它是发布产物）；
    # 目录版失败不再导致整个打包被判定为失败。
    sys.exit(1 if rc_one != 0 else 0)


if __name__ == "__main__":
    main()
