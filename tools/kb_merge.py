# 把 _kb_new*.py 里的条目合并进 rules/dir_knowledge.json。
# 只写规则文件本身，不触碰磁盘上任何用户文件。
import io
import json
import os
import sys

# 本脚本放在 tools/ 下：规则文件要按工程根解析，kb_new*.py 与本脚本同级
# （搬进 tools/ 时去掉了下划线前缀，导入名必须跟着改，否则 ModuleNotFoundError）。
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import kb_new
import kb_new2

PATH = os.path.join(ROOT, "rules", "dir_knowledge.json")
data = json.load(io.open(PATH, encoding="utf-8"))
entries = data["entries"]
have_ids = {e.get("id") for e in entries}

# 已被已有条目占用的名字（避免新条目抢匹配、造成歧义）
used_names = {}
for e in entries:
    for n in e.get("names", []):
        used_names.setdefault(n.lower(), e.get("id"))

added, skipped, trimmed = [], [], []
for new in list(_kb_new.NEW) + list(_kb_new2.NEW):
    if new.get("id") in have_ids:
        skipped.append(new.get("id"))
        continue
    names = list(new.get("names", []))
    keep = [n for n in names if n.lower() not in used_names]
    dropped = [n for n in names if n.lower() in used_names]
    if dropped:
        trimmed.append((new.get("id"), dropped))
    if not keep:
        skipped.append(new.get("id") + "（名字全被占用，跳过）")
        continue
    new = dict(new)
    new["names"] = keep
    entries.append(new)
    have_ids.add(new["id"])
    for n in keep:
        used_names[n.lower()] = new["id"]
    added.append(new["id"])

data["version"] = 2.0
data["说明"] = data["说明"].split("。可自由增改")[0] + \
    "。可自由增改（v2.0 起补入本机实测的软件 / 工具 / 游戏 / 开发环境目录）。"

with io.open(PATH, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=1)

print(f"新增 {len(added)} 条，跳过 {len(skipped)} 条，剔除重名 {len(trimmed)} 处")
if skipped:
    print("  跳过:", skipped)
if trimmed:
    for i, d in trimmed[:12]:
        print(f"  {i} 剔除重名: {d}")
print("条目总数:", len(entries))
