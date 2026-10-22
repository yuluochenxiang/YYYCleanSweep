# -*- coding: utf-8 -*-
"""一次性脚本：往 clean_rules.json 补「AppData 应用缓存大户」规则（扩展方向 3）。

- 幂等：已存在的 id 会跳过，重复运行不会重复添加。
- 新增规则全部归入已有分类 app（应用缓存），不新增分类、不引入任何依赖。
- 写回时保持原文件缩进风格。
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
# 规则文件在工程根的 rules/ 下，不是 tools/rules/（HERE 再往上一级），
# 指错路径的话脚本一跑就 FileNotFoundError。
ROOT = os.path.dirname(HERE)
P = os.path.join(ROOT, "rules", "clean_rules.json")

# 说明：Python 字符串里 "\\" 表示一个反斜杠，写进 JSON 后是 "\\"（即路径分隔符）。
NEW_RULES = [
    {
        "id": "doubao_cache",
        "category": "app",
        "name": "豆包缓存",
        "paths": ["%LOCALAPPDATA%\\Doubao"],
        "mode": "clear_contents",
        "risk": "safe",
        "admin": False,
        "desc": "豆包（字节跳动 AI 应用）的本地缓存、下载与日志。长期使用可累积到数 GB，"
                "是 AppData 里常见的隐形大户。",
        "advice": "可直接清理。下次启动会自动重建，不影响登录状态。",
        "operation": "清空目录内全部文件与子文件夹，保留目录本身",
        "recovery": "无需恢复",
    },
    {
        "id": "wechat_appex_cache",
        "category": "app",
        "name": "微信内置浏览器缓存",
        "paths": ["%APPDATA%\\Tencent\\WeChatAppEx"],
        "mode": "clear_contents",
        "risk": "safe",
        "admin": False,
        "desc": "微信内置浏览器与小程序运行时产生的网页缓存。",
        "advice": "可直接清理，不影响聊天记录。聊天文件请在微信「设置 → 文件管理」里单独处理。",
        "operation": "清空目录内全部文件与子文件夹，保留目录本身",
        "recovery": "自动重建",
    },
    {
        "id": "bilibili_cache",
        "category": "app",
        "name": "哔哩哔哩客户端缓存",
        "paths": ["%APPDATA%\\bilibili\\Cache"],
        "mode": "clear_contents",
        "risk": "safe",
        "admin": False,
        "desc": "B 站客户端的图片、播放与网页缓存。",
        "advice": "可放心清理。注意只清 Cache 子目录——下载的视频在别的目录，"
                  "不会被这条规则删掉。",
        "operation": "清空 Cache 目录内容",
        "recovery": "自动重建",
    },
    {
        "id": "jianying_cache",
        "category": "app",
        "name": "剪映缓存",
        "paths": ["%LOCALAPPDATA%\\JianyingPro\\Cache"],
        "mode": "clear_contents",
        "risk": "caution",
        "admin": False,
        "desc": "剪映（视频剪辑）的预览与素材缓存。",
        "advice": "清缓存不影响你的草稿与导出工程。若正在剪辑，建议先关闭剪映再清理。",
        "operation": "清空 Cache 目录内容",
        "recovery": "自动重建",
    },
    {
        "id": "wps_cache",
        "category": "app",
        "name": "WPS Office 缓存",
        "paths": ["%APPDATA%\\Kingsoft"],
        "mode": "glob_dirs",
        "glob_pattern": "*\\cache",
        "risk": "caution",
        "admin": False,
        "desc": "WPS / Kingsoft 套件运行时产生的缓存目录。",
        "advice": "只清 cache 子目录，不会动你的文档与模板。建议先关闭 WPS。",
        "operation": "删除匹配的 cache 目录内容",
        "recovery": "自动重建",
    },
    {
        "id": "app_cache_dirs",
        "category": "app",
        "name": "应用缓存目录（Local）",
        "paths": ["%LOCALAPPDATA%"],
        "mode": "glob_dirs",
        "glob_pattern": "*\\Cache",
        "risk": "caution",
        "admin": False,
        "desc": "扫描 %LOCALAPPDATA% 下所有名为 Cache 的子目录，把各软件藏起来的缓存一次性找出来——"
                "这是 AppData 里最容易被忽略、也最容易攒到几个 GB 的空间大户。",
        "advice": "缓存删了会自动重建，但这是跨软件的通配匹配，建议展开看一眼具体是哪个软件再决定。",
        "operation": "删除匹配的 Cache 目录内容",
        "recovery": "自动重建",
    },
    {
        "id": "app_cache_dirs_roaming",
        "category": "app",
        "name": "应用缓存目录（Roaming）",
        "paths": ["%APPDATA%"],
        "mode": "glob_dirs",
        "glob_pattern": "*\\Cache",
        "risk": "caution",
        "admin": False,
        "desc": "扫描 %APPDATA%（Roaming）下所有名为 Cache 的子目录。",
        "advice": "同上，建议展开确认具体软件后再清理。",
        "operation": "删除匹配的 Cache 目录内容",
        "recovery": "自动重建",
    },
    {
        "id": "app_codecache_dirs",
        "category": "app",
        "name": "应用脚本缓存（Code Cache）",
        "paths": ["%LOCALAPPDATA%"],
        "mode": "glob_dirs",
        "glob_pattern": "*\\Code Cache",
        "risk": "safe",
        "admin": False,
        "desc": "Electron / Chromium 系桌面应用（各类客户端）的脚本缓存目录。",
        "advice": "纯缓存，可放心清理，下次启动重建。",
        "operation": "删除匹配的 Code Cache 目录内容",
        "recovery": "自动重建",
    },
    {
        "id": "app_gpucache_dirs",
        "category": "app",
        "name": "应用 GPU 缓存",
        "paths": ["%LOCALAPPDATA%"],
        "mode": "glob_dirs",
        "glob_pattern": "*\\GPUCache",
        "risk": "safe",
        "admin": False,
        "desc": "应用的图形渲染缓存。",
        "advice": "纯缓存，可放心清理。",
        "operation": "删除匹配的 GPUCache 目录内容",
        "recovery": "自动重建",
    },
]


def main():
    """往 clean_rules.json 追加「应用缓存」类规则，已存在的 id 跳过。"""
    raw = open(P, encoding="utf-8").read()
    data = json.loads(raw)
    rules = data["rules"]
    have = {r.get("id") for r in rules}

    added = []
    for r in NEW_RULES:
        if r["id"] in have:
            continue
        rules.append(r)
        added.append(r["id"])

    if added:
        data["updated"] = "2026-10-11"
        indent = 2 if "\n  " in raw[:500] else None
        with open(P, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.write("\n")

    print("新增规则:", added)
    print("规则总数:", len(rules))
    print("app 分类条数:", sum(1 for r in rules if r.get("category") == "app"))


if __name__ == "__main__":
    main()
