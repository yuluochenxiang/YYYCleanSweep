"""清理规则的读写、校验、备份与恢复。

刻意不碰 Qt：这些函数要能被界面、命令行和测试脚本共用。
写盘一律「先写 .tmp 再 os.replace」，与 core/config.py、core/paths.py 保持一套写法。

⚠️ 一条必须记着的事（见 G 卷设计决策 2.1）：
    读写的目标是 exe 旁边的 rules\\clean_rules.json（用户可编辑副本）。
    build.py 从 v3.20 起会把工程里的 rules\\*.json **覆盖同步**到 app\\rules\\，
    所以用户自己改的东西下次打包会被冲掉 —— 界面上要提示「备份当前」。
"""
import json
import os
import shutil
import time

from . import rules as R


def path_of():
    """当前实际生效的规则文件路径（exe 旁优先，没有就用内置的并释放一份）。"""
    return R._rules_path()


def load_raw():
    """读完整 json（含 categories / version / updated），不做字段补齐。"""
    with open(path_of(), "r", encoding="utf-8") as f:
        return json.load(f)


def save_raw(data):
    """原子写盘：先写 .tmp 再 replace，写一半断电不会留下半个文件。"""
    p = path_of()
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return True


def backup(tag=""):
    """保存前先备一份带时间戳的副本，返回备份文件路径（源文件不存在则返回空串）。"""
    src = path_of()
    if not os.path.exists(src):
        return ""
    name = os.path.basename(src).rsplit(".", 1)[0]
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(os.path.dirname(src), f"{name}_{stamp}{tag}.bak")
    shutil.copy2(src, dst)
    return dst


def list_backups(limit=20):
    """列出历史备份，新的在前。"""
    p = path_of()
    d = os.path.dirname(p)
    if not os.path.isdir(d):
        return []
    base = os.path.basename(p).rsplit(".", 1)[0]
    items = [os.path.join(d, n) for n in os.listdir(d)
             if n.startswith(base) and n.endswith(".bak")]
    items.sort(key=os.path.getmtime, reverse=True)
    return items[:limit]


def restore(backup_path):
    """从某个备份恢复（调用方负责二次确认）。"""
    shutil.copy2(backup_path, path_of())
    return True


def restore_builtin():
    """恢复出厂规则：从打包目录里那份拷回来。

    用户改过的东西会全部丢失，所以调用方必须先做二次确认。
    """
    src = os.path.join(R._bundled_rules_dir(), "clean_rules.json")
    if not os.path.exists(src):
        return False
    dst = path_of()
    if os.path.abspath(src) == os.path.abspath(dst):
        # 源码态下两者是同一个文件，再从别处找一份也没意义
        return False
    shutil.copy2(src, dst)
    return True


def validate(data):
    """校验规则文件，返回错误描述列表；空列表表示没问题。

    这里宁可严一点也不放行 —— 写坏 json 会导致下次启动 load_rules() 直接抛异常，
    界面连打开的机会都没有。校验是最后一道关。
    """
    errs = []
    if not isinstance(data, dict):
        return ["文件格式不是 JSON 对象"]
    cats = data.get("categories")
    rls = data.get("rules")
    if not isinstance(cats, list) or not isinstance(rls, list):
        return ["categories / rules 必须是数组"]

    cat_ids = set()
    for i, c in enumerate(cats):
        if not isinstance(c, dict):
            errs.append(f"分类 #{i + 1} 不是对象")
            continue
        cid = (c.get("id") or "").strip()
        if not cid:
            errs.append(f"分类 #{i + 1} 缺少 id")
        elif cid in cat_ids:
            errs.append(f"分类 id 重复：{cid}")
        cat_ids.add(cid)

    seen = set()
    for i, r in enumerate(rls):
        if not isinstance(r, dict):
            errs.append(f"第 {i + 1} 条不是对象")
            continue
        tag = f"第 {i + 1} 条"
        rid = (r.get("id") or "").strip()
        if not rid:
            errs.append(f"{tag}：缺少 id")
        elif rid in seen:
            errs.append(f"{tag}：id 重复（{rid}）")
        seen.add(rid)
        if not (r.get("name") or "").strip():
            errs.append(f"{tag}（{rid or '无 id'}）：缺少名称")
        if r.get("category") not in cat_ids:
            errs.append(f"{tag}（{rid}）：分类「{r.get('category')}」不在分类列表里")
        if r.get("risk") not in R.RISK_ORDER:
            errs.append(f"{tag}（{rid}）：风险「{r.get('risk')}」取值不合法")
        if r.get("mode") not in R.MODE_LABEL:
            errs.append(f"{tag}（{rid}）：方式「{r.get('mode')}」取值不合法")
        if not isinstance(r.get("paths", []), list):
            errs.append(f"{tag}（{rid}）：paths 必须是数组")
        if not isinstance(r.get("admin", False), bool):
            errs.append(f"{tag}（{rid}）：管理员标记必须是 true / false")
    return errs


def save_checked(data):
    """备份 + 校验 + 写盘，一条龙。返回 (ok, 错误列表, 备份路径)。

    界面只调这一个函数就够了，省得某处忘了先备份。
    """
    errs = validate(data)
    if errs:
        return False, errs, ""
    bak = backup("_auto")
    save_raw(data)
    return True, [], bak


def new_rule(cat_id, existing_ids):
    """生成一条空规则，id 形如 my_rule_1（不撞已有 id）。"""
    n = 1
    ids = set(existing_ids or ())
    while f"my_rule_{n}" in ids:
        n += 1
    return {
        "id": f"my_rule_{n}",
        "name": "我的清理项",
        "category": cat_id,
        "mode": "clear_contents",
        "risk": "safe",
        "admin": False,
        "paths": [],
        "patterns": [],
        "desc": "",
        "advice": "",
    }


def copy_rule(rule, existing_ids):
    """深拷贝一条规则，id 加 _copy 后缀并保证不重复。"""
    d = json.loads(json.dumps(rule, ensure_ascii=False))
    base = (d.get("id") or "rule") + "_copy"
    ids = set(existing_ids or ())
    rid = base
    n = 2
    while rid in ids:
        rid = f"{base}{n}"
        n += 1
    d["id"] = rid
    d["name"] = (d.get("name") or "") + "（副本）"
    return d
