# -*- coding: utf-8 -*-
"""一次性脚本：给规则表补充「跟随所选盘」的条目，给知识库补充 D/E/F/G 盘实测目录。"""
import json
import io
import os

BASE = os.path.dirname(os.path.abspath(__file__))
RULES = os.path.join(BASE, "rules", "clean_rules.json")
KB = os.path.join(BASE, "rules", "dir_knowledge.json")

# ---------------------------------------------------------------- 清理规则
NEW_RULES = [
    {
        "id": "drive_recycle", "category": "temp", "name": "回收站（当前盘）",
        "paths": ["%TARGETDRIVE%\\$Recycle.Bin"], "mode": "clear_contents",
        "risk": "safe", "admin": False,
        "desc": "当前所选磁盘的回收站。删除的文件在这里继续占着空间，直到手动清空。",
        "advice": "可直接清理。清空后文件无法还原，先确认回收站里没有还要用的东西。",
        "operation": "清空当前盘回收站内的全部内容",
        "recovery": "不可恢复",
    },
    {
        "id": "drive_dump", "category": "temp", "name": "根目录崩溃转储",
        "paths": ["%TARGETDRIVE%\\"], "mode": "delete_files",
        "patterns": ["*.dmp", "DumpStack.log.tmp", "MEMORY.DMP", "*.mdmp"],
        "risk": "safe", "admin": False,
        "desc": "程序崩溃时写在磁盘根目录的内存转储文件，用于排查故障，日常用不到。",
        "advice": "可直接清理。如果正在排查某个频繁崩溃的问题，先留一份。",
        "operation": "删除当前盘根目录下匹配的转储文件",
        "recovery": "无需恢复",
    },
    {
        "id": "drive_config_msi", "category": "temp", "name": "安装残留 Config.Msi",
        "paths": ["%TARGETDRIVE%\\Config.Msi"], "mode": "delete_dir",
        "risk": "safe", "admin": False,
        "desc": "Windows Installer 安装 / 卸载软件时留下的临时文件夹，正常结束后应自动删除。",
        "advice": "可直接清理。若某个安装程序正在运行，会删除失败并自动跳过。",
        "operation": "删除整个目录",
        "recovery": "无需恢复",
    },
    {
        "id": "drive_windows_old", "category": "update", "name": "旧系统残留 Windows.old",
        "paths": ["%TARGETDRIVE%\\Windows.old"], "mode": "delete_dir",
        "risk": "caution", "admin": True,
        "desc": "升级或重装系统时保留的上一份 Windows，体积通常在 10 GB 以上。",
        "advice": "确认新系统已用顺手、不需要回退后再删。删掉就无法通过系统恢复回到旧版本。",
        "operation": "删除整个目录（需要管理员）",
        "recovery": "不可恢复",
    },
    {
        "id": "drive_win_upgrade", "category": "update", "name": "升级临时目录 $Windows.~BT / ~WS",
        "paths": ["%TARGETDRIVE%\\$Windows.~BT", "%TARGETDRIVE%\\$Windows.~WS"],
        "mode": "delete_dir", "risk": "caution", "admin": True,
        "desc": "Windows 大版本升级过程中的下载与回滚缓存，升级失败或中断后常残留。",
        "advice": "系统已升级完成且运行正常即可删除；正在等待重启完成升级时不要动。",
        "operation": "删除整个目录（需要管理员）",
        "recovery": "不可恢复",
    },
    {
        "id": "drive_perflogs", "category": "temp", "name": "性能日志 PerfLogs",
        "paths": ["%TARGETDRIVE%\\PerfLogs"], "mode": "clear_contents",
        "risk": "safe", "admin": False,
        "desc": "Windows 性能监视器生成的日志，一般只有排查性能问题时才需要。",
        "advice": "可直接清理。",
        "operation": "清空目录内容",
        "recovery": "无需恢复",
    },
]

# ---------------------------------------------------------------- 知识库
NEW_KB = [
    # ---- Steam ----
    dict(id="steam_common", name="Steam 游戏本体", kind="game", risk="keep",
         paths=["steamapps\\common"], purpose="Steam 已安装游戏的文件，是游戏库里真正的主体。",
         advice="不要删。要腾空间请在 Steam 里右键游戏→卸载，或把整个库迁移到其他盘。"),
    dict(id="steam_workshop", name="Steam 创意工坊内容", kind="game", risk="caution",
         paths=["steamapps\\workshop"], purpose="订阅的模组、地图、皮肤等创意工坊内容，按 AppID 分目录。",
         advice="可以删：删掉只是取消本地文件，重新订阅会自动下载。正在用的模组别删。"),
    dict(id="steam_shadercache", name="Steam 着色器缓存", kind="cache", risk="safe",
         paths=["steamapps\\shadercache"], purpose="预先编译好的显卡着色器，用来加快游戏加载。",
         advice="可以删，下次进游戏会重新编译，首次加载略慢几秒。"),
    dict(id="steam_downloading", name="Steam 下载暂存", kind="temp", risk="safe",
         paths=["steamapps\\downloading"],
         purpose="正在下载 / 更新时的临时分片文件，下载中断后会残留。",
         advice="可以删，没有正在进行的下载时清空最安全。"),
    dict(id="steam_depotcache", name="Steam depot 缓存", kind="cache", risk="safe",
         paths=["steamapps\\depotcache"], purpose="游戏更新包的本地缓存，用于增量更新。",
         advice="可以删，下次更新会重新下载。"),
    dict(id="steam_compatdata", name="Steam Proton 兼容层数据", kind="game", risk="caution",
         paths=["steamapps\\compatdata"],
         purpose="Linux 游戏在 Windows 上通过 Proton 运行时生成的兼容层，每个游戏一份。",
         advice="删除等于删掉对应游戏在 Proton 下的存档与设置。确认不再玩再删。"),
    dict(id="steam_temp", name="Steam 临时目录", kind="temp", risk="safe",
         paths=["steamapps\\temp", "steam\\temp"], purpose="Steam 自身的临时文件。",
         advice="可直接清理。"),
    dict(id="steam_userdata", name="Steam 用户数据（存档 / 截图）", kind="user", risk="keep",
         paths=["steam\\userdata"],
         purpose="按账号 ID 存放的云存档、截图、个人配置，是 Steam 里最不能丢的部分。",
         advice="不要删。截图太多可以进 Steam 里单独清理截图，不要整目录删除。"),
    dict(id="steam_appcache", name="Steam 商店与界面缓存", kind="cache", risk="safe",
         paths=["steam\\appcache"], purpose="商店页面、库存、好友列表的本地缓存。",
         advice="可直接清理，Steam 会自动重建。"),
    dict(id="steam_logs", name="Steam 日志", kind="log", risk="safe",
         paths=["steam\\logs"], purpose="客户端运行日志，排查登录或下载问题时用。",
         advice="可直接清理。"),

    # ---- Epic ----
    dict(id="epic_launcher", name="Epic 启动器", kind="install", risk="keep",
         paths=["epic games\\launcher"], purpose="Epic Games 启动器本体。",
         advice="不要删，要移除请走系统卸载。"),
    dict(id="epic_vaultcache", name="Epic 已安装游戏的下载缓存", kind="cache", risk="safe",
         paths=["epic games\\launcher\\vaultcache"],
         purpose="Epic 用来还原 / 修复已安装游戏的本地缓存，安装完就不再需要，体积常常几个 GB。",
         advice="可以删。删后「验证游戏」会重新下载这部分数据，但正常游玩不受影响。"),
    dict(id="epic_patching", name="Epic 更新补丁缓存", kind="temp", risk="safe",
         paths=["epic games\\launcher\\patching"], purpose="游戏更新时的补丁暂存目录。",
         advice="没有正在更新的游戏时可直接清理。"),
    dict(id="epic_egstore", name="Epic 游戏存档 .egstore", kind="user", risk="caution",
         names=["egstore"], purpose="Epic 版游戏的存档与授权数据目录。",
         advice="删了进度会丢。要腾空间请整体卸载游戏，不要单独删这个目录。"),
    dict(id="epic_directx", name="Epic 运行库安装包", kind="install", risk="safe",
         paths=["epic games\\directxredist"], purpose="DirectX 运行库安装器，安装游戏时调用一次。",
         advice="可以删。万一以后有游戏需要，系统会自己再装。"),

    # ---- GOG / 其他平台 ----
    dict(id="gog_downloads", name="GOG 下载缓存", kind="temp", risk="safe",
         paths=["gog galaxy\\downloads"], purpose="GOG Galaxy 的游戏安装包下载缓存。",
         advice="安装完即可删除，离线安装包另存他处的话先确认。"),
    dict(id="wegame", name="WeGame 游戏", kind="game", risk="keep",
         paths=["wegameapps"], names=["wegame"], purpose="WeGame 平台的游戏安装目录。",
         advice="不要手动删，在 WeGame 里卸载或迁移。"),

    # ---- Unity 游戏 ----
    dict(id="unity_data", name="Unity 游戏资源目录", kind="game", risk="keep",
         names=[], keywords=["_data"], purpose="Unity 游戏的资源与程序集目录（形如 Xxx_Data）。",
         advice="游戏本体的一部分，删了游戏就打不开。"),
    dict(id="unity_mono", name="Unity Mono 运行时", kind="install", risk="keep",
         names=["monobleedingedge"], purpose="Unity 游戏自带的 Mono 运行环境。",
         advice="不要删，删了游戏无法启动。"),
    dict(id="unity_burst", name="Unity Burst 调试信息", kind="log", risk="safe",
         keywords=["burstdebuginformation"], purpose="Unity 生成的调试符号，只有排查崩溃时有用。",
         advice="可以删，不影响游戏运行。"),
    dict(id="unity_streaming", name="Unity StreamingAssets", kind="game", risk="keep",
         names=["streamingassets"], purpose="游戏运行时直接读取的原始资源（视频、配置、Mod 入口）。",
         advice="不要删。"),

    # ---- Unreal 游戏 ----
    dict(id="unreal_saved", name="游戏 Saved 目录", kind="user", risk="caution",
         names=["saved"], parents=["binaries", "content"],
         purpose="Unreal 游戏的存档、日志与崩溃报告。里面常混着几个 GB 的历史日志。",
         advice="不要整删。只清 Saved\\Logs 与 Saved\\Crashes，存档在 SaveGames 里要保留。"),
    dict(id="unreal_logs", name="游戏日志", kind="log", risk="safe",
         paths=["saved\\logs"], purpose="游戏运行日志，随时间累积。",
         advice="可直接清理。"),
    dict(id="unreal_crashes", name="游戏崩溃报告", kind="log", risk="safe",
         paths=["saved\\crashes"], purpose="崩溃时生成的报告与内存快照。",
         advice="可直接清理。"),
    dict(id="unreal_intermediate", name="Unreal 构建中间产物", kind="temp", risk="safe",
         paths=["intermediate"], purpose="引擎编译着色器与资源时的中间文件，可随时重建。",
         advice="可以删，下次启动或打包时会自动重新生成（首次会慢一些）。"),
    dict(id="unreal_content", name="游戏资源 Content", kind="game", risk="keep",
         names=["content"], parents=["binaries"], purpose="游戏的核心资源包。",
         advice="不要删。"),

    # ---- 游戏通用 ----
    dict(id="redist", name="游戏运行库安装包", kind="install", risk="safe",
         names=["_commonredist", "_redist", "redist", "vcredist", "directxredist",
                "dotnet", "_redist"],
         purpose="随游戏附带的 DirectX / VC++ / .NET 运行库安装器，首次启动时装一次。",
         advice="可以删。系统里通常已经装过；万一缺了，个别游戏可能要重新运行一次安装。"),
    dict(id="uninstall_files", name="卸载程序文件", kind="install", risk="caution",
         names=["uninstall", "uninsfiles", "uninst", "unins000"],
         purpose="存放卸载程序与日志，删了就不能用软件自带的卸载入口。",
         advice="建议保留。确定要在「设置→应用」里卸载或直接删目录时再一起处理。"),
    dict(id="game_save", name="游戏存档目录", kind="user", risk="keep",
         names=["save", "saves", "savedata", "savesdir", "savesbackupdir", "savegames",
                "savedata_backup"],
         purpose="游戏进度存档，体积不大但完全不可再生。",
         advice="绝对不要删。找到这种目录建议顺手设为白名单。"),
    dict(id="game_logs_dir", name="游戏 / 软件日志目录", kind="log", risk="safe",
         names=["logs", "log"], purpose="运行日志，出问题排查用，平时没价值。",
         advice="可直接清理。"),
    dict(id="game_ost", name="游戏原声带 OST", kind="user", risk="keep",
         names=["ost", "soundtrack"], purpose="随游戏附带的音乐文件。",
         advice="删了不影响游戏，但音乐也没了。按个人需要决定。"),
    dict(id="crack_game", name="免安装 / 破解游戏目录", kind="game", risk="keep",
         keywords=["3dm", "codex", "plaza", "tenoke", "rune", "flt", "darksiders",
                   "skidrow", "razor1911", "tinyiso", "fitgirl", "p2p", "doge",
                   "chronos", "dinobytes", "unleashed", "simplex", "r_g", "i_know"],
         purpose="解压即玩的免安装游戏本体，目录名通常带破解组或版本号标识。",
         advice="这是游戏本体，不要删。要腾空间就把整个游戏目录移走或删除，别只删里面的子目录。"),
    dict(id="fitgirl_repack", name="FitGirl 压制包解压目录", kind="install", risk="caution",
         keywords=["fitgirl"], purpose="FitGirl Repack 解压出来的安装临时文件与游戏本体。",
         advice="安装完成并确认能玩之后，可以删掉安装包与临时目录；但游戏本体目录要留着。"),
    dict(id="game_record", name="游戏录屏 / 录像", kind="user", risk="caution",
         names=["游戏录屏", "0000游戏录屏"], keywords=["录屏", "replay", "record"],
         purpose="游戏内置或录屏软件保存的录像文件，体积通常很大。",
         advice="看过的可以删。确认没有想留的精彩片段再批量清理。"),

    # ---- Android / 开发工具 ----
    dict(id="sdk_emulator", name="Android 模拟器", kind="install", risk="caution",
         paths=["sdk\\emulator"], purpose="Android 模拟器程序本体。",
         advice="不用模拟器调试可以整个删掉；删了 Android Studio 会提示重新下载。"),
    dict(id="sdk_system_images", name="Android 模拟器系统镜像", kind="install", risk="caution",
         paths=["sdk\\system-images"], purpose="模拟器的 Android 系统镜像，每个版本 1～3 GB。",
         advice="不用的镜像可以删。删了模拟器里对应的设备会启动不了，重新下载即可。"),
    dict(id="sdk_ndk", name="Android NDK", kind="install", risk="caution",
         paths=["sdk\\ndk"], purpose="原生 C/C++ 开发工具链，只有做 JNI 开发才需要。",
         advice="不做 native 开发可以删，体积通常 2～5 GB。"),
    dict(id="sdk_sources", name="Android SDK 源码", kind="install", risk="safe",
         paths=["sdk\\sources"], purpose="Android 平台源码，用于开发时跳转查看。",
         advice="可以删，随时能在 SDK Manager 里重新下载。"),
    dict(id="sdk_dl_tmp", name="SDK 下载中间文件", kind="temp", risk="safe",
         paths=["sdk\\.downloadintermediates", "sdk\\.temp"],
         purpose="SDK Manager 下载过程中产生的中间文件，中断后会残留。",
         advice="可直接清理。"),
    dict(id="sdk_platforms", name="Android SDK 平台", kind="install", risk="keep",
         paths=["sdk\\platforms", "sdk\\build-tools", "sdk\\platform-tools"],
         purpose="编译 Android 应用必需的平台库、构建工具与 adb。",
         advice="不要删。可以在 SDK Manager 里删除不用的旧版本。"),
    dict(id="gradle_cache", name="Gradle 缓存", kind="cache", risk="caution",
         names=[".gradle", "gradle"], purpose="Gradle 的依赖与构建缓存，往往几个 GB。",
         advice="可以删，下次构建会重新下载依赖（首次会慢）。有 Gradle 守护进程运行时删不干净。"),
    dict(id="avd", name="Android 虚拟设备", kind="install", risk="caution",
         names=["avd"], parents=[".android"], purpose="模拟器创建的虚拟设备镜像。",
         advice="不用的设备可以删，在 AVD Manager 里删最干净。"),

    # ---- Cocos / Godot ----
    dict(id="cocos_library", name="Cocos 导入缓存 library", kind="cache", risk="safe",
         names=["library"], parents=["assets", "settings"],
         purpose="Cocos Creator 对资源的导入结果，可完全由 assets 重建。",
         advice="可以删。下次打开项目会重新导入，大项目首次导入可能要几分钟。"),
    dict(id="cocos_temp", name="Cocos 项目临时目录", kind="temp", risk="safe",
         names=["temp"], parents=["assets", "library"], purpose="编辑器运行时的临时文件。",
         advice="可直接清理。"),
    dict(id="cocos_build", name="构建产物 build", kind="temp", risk="safe",
         names=["build"], parents=["assets", "library"], purpose="打包出来的发布目录，随时可重新构建。",
         advice="可以删，需要时重新构建即可。"),
    dict(id="cocos_assets", name="项目资源 assets", kind="user", risk="danger",
         names=["assets"], parents=["library", "settings"],
         purpose="项目的原始资源：图片、脚本、场景，是整个项目的核心。",
         advice="绝对不要删。删了项目就废了，务必设为白名单。"),
    dict(id="godot_import", name="Godot 导入缓存 .godot", kind="cache", risk="safe",
         names=[".godot"], purpose="Godot 4 的导入缓存与编辑器状态，可由源文件重建。",
         advice="可以删，下次打开项目会重新导入。"),

    # ---- Stable Diffusion / AI ----
    dict(id="sd_outputs", name="SD 生成结果 outputs", kind="user", risk="caution",
         names=["outputs", "output"], purpose="AI 绘图生成的历史图片，是实际产出物。",
         advice="自己筛选。不要整目录删——里面是你生成过的图，先挑出要留的。"),
    dict(id="sd_grids", name="SD 网格预览图", kind="cache", risk="safe",
         names=["txt2img-grids", "img2img-grids", "grids"],
         purpose="批量生成时的拼接预览图，原图已单独保存，网格图只是索引。",
         advice="可以删，不影响单张原图。"),
    dict(id="sd_models", name="AI 大模型", kind="model", risk="caution",
         paths=["models\\stable-diffusion", "models\\checkpoints"],
         purpose="Stable Diffusion 底模，单个 2～10 GB。",
         advice="不用的模型可以删，但重新下载很费时间。删前确认不再需要这个风格。"),
    dict(id="sd_lora", name="LoRA / ControlNet 模型", kind="model", risk="caution",
         paths=["models\\lora", "models\\controlnet", "models\\lycoris"],
         purpose="微调模型与控制网络，体积从几十 MB 到数 GB 不等。",
         advice="不用的可以删，重启 WebUI 后列表自动更新。"),
    dict(id="sd_vae", name="VAE / embeddings", kind="model", risk="keep",
         paths=["models\\vae", "embeddings"],
         purpose="很小的解码与文本反转模型，通常只有几十到几百 MB。",
         advice="占空间不多，建议保留。"),
    dict(id="sd_repositories", name="SD 依赖仓库", kind="install", risk="keep",
         names=["repositories"], purpose="WebUI 运行必需的第三方依赖源码。",
         advice="不要删，删了 WebUI 起不来。"),
    dict(id="sd_extensions", name="SD 插件 extensions", kind="install", risk="keep",
         names=["extensions", "custom_nodes"],
         purpose="安装的插件与自定义节点。", advice="不要删，删了功能会缺失。"),
    dict(id="sd_venv", name="AI 环境虚拟环境", kind="install", risk="keep",
         names=["venv", "system\\python"], purpose="Python 依赖环境，通常 2～8 GB。",
         advice="不要删。重装环境很麻烦；要腾空间可以考虑换到别的盘重装。"),
    dict(id="sd_tmp", name="AI 临时输出", kind="temp", risk="safe",
         names=["tmp"], parents=["stable-diffusion-webui"],
         purpose="生成过程中的临时文件。", advice="可直接清理。"),
    dict(id="comfy_input", name="ComfyUI 输入目录", kind="cache", risk="safe",
         paths=["comfyui\\input"], purpose="上传到工作流的素材，用完可清。",
         advice="可以清，但工作流里引用的图片要重新上传。"),

    # ---- Python / 开发通用 ----
    dict(id="venv_dir", name="Python 虚拟环境", kind="install", risk="caution",
         names=["venv", ".venv", "env"], keywords=["venv", "_env"],
         purpose="项目独立的 Python 依赖环境，通常 1～10 GB。",
         advice="删了要重新 pip 安装依赖。不用的旧项目环境可以删，正在用的保留。"),
    dict(id="pycache", name="Python 字节码缓存", kind="cache", risk="safe",
         names=["__pycache__"], purpose="Python 自动生成的 .pyc 缓存，可随时重建。",
         advice="可直接清理，运行时会自动重新生成。"),
    dict(id="build_dist", name="构建产物", kind="temp", risk="safe",
         names=["build", "dist", "*.egg-info"], purpose="打包 / 编译产生的中间与最终产物。",
         advice="可以删，重新构建即可。确认没有正在分发的文件。"),
    dict(id="node_modules", name="Node 依赖目录", kind="install", risk="safe",
         names=["node_modules"], purpose="npm 安装的依赖，以大量小文件著称。",
         advice="可以删，npm install 会还原。删之前确认项目还要用。"),
    dict(id="git_dir", name="Git 仓库数据", kind="db", risk="danger",
         names=[".git"], purpose="版本历史与对象数据库，删除后无法回退任何提交。",
         advice="不要删。想瘦身用 git gc，不要手动删目录。"),

    # ---- 下载 / 网盘 ----
    dict(id="baidu_dl", name="百度网盘下载目录", kind="temp", risk="caution",
         names=["baidunetdiskdownload"], purpose="百度网盘默认的下载落地目录。",
         advice="下载完并转存后的安装包可以删。确认没有还在同步的任务。"),
    dict(id="xunlei_dl", name="迅雷下载目录", kind="temp", risk="caution",
         names=["迅雷下载", "迅雷云盘"], purpose="迅雷默认下载目录。",
         advice="已看完 / 已转存的文件可以删。任务还在做种时删除会中断上传。"),
    dict(id="browser_dl", name="浏览器下载目录", kind="temp", risk="caution",
         names=["360安全浏览器下载"], purpose="浏览器的默认下载目录。",
         advice="装完软件后的安装包可以删，注意别删掉里面的种子或未完成的下载。"),
    dict(id="weflow_out", name="WeFlow 输出目录", kind="user", risk="caution",
         names=["weflowout"], purpose="微信相关工具导出 / 输出的文件。",
         advice="确认已备份后清理。"),

    # ---- 系统 / 杂项 ----
    dict(id="recycle_bin", name="回收站", kind="system", risk="safe",
         names=["$recycle.bin"], purpose="各分区的回收站，删除的文件在这里继续占用空间。",
         advice="可以直接清空，但清空后无法还原。"),
    dict(id="sys_vol_info", name="系统卷信息", kind="system", risk="danger",
         names=["system volume information"],
         purpose="存放系统还原点、卷影副本与磁盘索引，普通用户看不到内容。",
         advice="不要手动删除。要回收空间用「系统属性→系统保护→配置」删除旧的还原点。"),
    dict(id="config_msi", name="安装残留 Config.Msi", kind="temp", risk="safe",
         names=["config.msi"], purpose="安装程序留下的回滚信息。",
         advice="可直接清理。"),
    dict(id="dump_stack", name="崩溃堆栈日志", kind="log", risk="safe",
         names=["dumpstack.log.tmp"], purpose="程序崩溃时写的堆栈日志。",
         advice="可直接清理。"),
    dict(id="windows_apps", name="WindowsApps 应用商店目录", kind="install", risk="caution",
         names=["windowsapps", "modifiablewindowsapps"],
         purpose="微软商店安装的应用本体，权限受系统保护。",
         advice="不要手动删。在「设置→应用」里卸载，或用「移动」功能迁到别的盘。"),

    # ---- 常用软件 ----
    dict(id="adobe_ps", name="Adobe Photoshop", kind="install", risk="keep",
         paths=["adobe photoshop 2022"], purpose="Photoshop 安装目录。",
         advice="不要删。里面的 Presets 是自己的预设，别误删。"),
    dict(id="bilibili_client", name="哔哩哔哩客户端", kind="install", risk="keep",
         paths=["program files\\bilibili"],
         purpose="B 站桌面客户端本体。", advice="不要删，要移除走卸载。"),
    dict(id="bilibili_dl", name="B 站下载的视频", kind="user", risk="caution",
         names=["bilibili"],
         purpose="用 B 站客户端或工具下载到本地的番剧、视频与字幕。",
         advice="自己判断：看完的可以删，注意别删掉还在追的剧集。"),
    dict(id="bililive", name="直播姬", kind="install", risk="caution",
         paths=["bililive\\livehime"], purpose="B 站直播姬，含直播录像与素材缓存。",
         advice="软件本体别删；过去直播的录像与素材可以在软件设置里清理。"),
    dict(id="sunlogin", name="向日葵远程控制", kind="install", risk="keep",
         names=["sunlogin", "sunloginclient"], purpose="向日葵远程控制客户端。",
         advice="不要删目录。它的日志子目录可以清。"),
    dict(id="clash", name="Clash for Windows", kind="install", risk="keep",
         keywords=["clash"], purpose="代理工具，含配置与订阅缓存。",
         advice="不要删。日志与旧的订阅缓存在软件里可以清。"),
    dict(id="fdm", name="Free Download Manager", kind="install", risk="keep",
         names=["free download manager"], purpose="下载工具本体。",
         advice="不要删。下载记录与临时分片在软件里清。"),
    dict(id="apache_ant", name="Apache Ant", kind="install", risk="keep",
         keywords=["apache-ant"], purpose="Java 构建工具，被 Cocos 等工具链依赖。",
         advice="保留。确定不再用旧工具链时可以整目录删。"),

    # ---- 个人资料 ----
    dict(id="personal_lib", name="个人资料库", kind="user", risk="keep",
         names=["我的文档", "我的图片", "我的视频", "我的音乐", "我的图书",
                "我的学习", "我的工作", "我的绘画", "我的手机", "我的剧集",
                "我的软件", "我的手机22"],
         purpose="你自己整理的文档、影视、图片与学习资料。",
         advice="这是个人文件，工具不会也不敢替你判断。建议在里面按年份/类型手动整理，或整体迁移到大容量盘。"),
    dict(id="personal_ai", name="AI 素材与工作流", kind="user", risk="caution",
         names=["0000ai", "aⅲ教程"], purpose="AI 教程、工作流与生成素材。",
         advice="教程和工作流保留；生成的中间图可以按批次清理。"),
    dict(id="game_library", name="游戏库分类目录", kind="game", risk="keep",
         names=["0000gggg", "0000games", "games_3dm", "games_rusia2", "game_2023",
                "game_2024", "games2023", "games2024", "games2025", "games2026"],
         keywords=["0000games", "games_3dm", "games2023", "games2024", "0000gggg"],
         purpose="你自己整理的游戏库，多半是解压即玩的免安装版。",
         advice="这是游戏本体，不要删。想腾空间就整个游戏目录移走或删除。"),
]


def merge_rules():
    """把 NEW_RULES 并进 clean_rules.json。同 id 视为已存在，用新条目覆盖旧字段。"""
    with io.open(RULES, "r", encoding="utf-8") as f:
        data = json.load(f)
    exist = {r["id"]: r for r in data["rules"]}
    added = 0
    for r in NEW_RULES:
        if r["id"] in exist:
            exist[r["id"]].update(r)
        else:
            data["rules"].append(r)
            added += 1
    with io.open(RULES, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("规则：新增 %d 条，总计 %d 条" % (added, len(data["rules"])))


def merge_kb():
    """把 NEW_KB 并进 dir_knowledge.json，规则同 merge_rules（同 id 覆盖）。"""
    with io.open(KB, "r", encoding="utf-8") as f:
        data = json.load(f)
    exist = {e["id"]: e for e in data["entries"]}
    added = 0
    for e in NEW_KB:
        e.setdefault("paths", [])
        e.setdefault("names", [])
        e.setdefault("keywords", [])
        e.setdefault("parents", [])
        if e["id"] in exist:
            exist[e["id"]].update(e)
        else:
            data["entries"].append(e)
            added += 1
    with io.open(KB, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("知识库：新增 %d 条，总计 %d 条" % (added, len(data["entries"])))


if __name__ == "__main__":
    merge_rules()
    merge_kb()
