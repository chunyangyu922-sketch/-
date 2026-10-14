#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
资产管理系统 · 数据库一键导入工具
====================================
把其他版本 / 其他机器 / 历史备份的数据库文件（.db）导入到本机正式库 ams_data.db。

两种导入模式：
  [1] 整体替换 —— 当前库先自动备份，然后整体换成源库内容（换机迁移、恢复备份）
  [2] 合并并入 —— 源库各机构并入当前库（按机构名对齐；资产编号冲突自动重排，
                  同名用户账号保留当前库，不破坏现有登录状态）

支持导入旧版本数据库（自动规范化）：
  - users 表缺 dept / org 列的旧库 → 自动补默认值
  - 单库版（kv 仅有 'db' 键、无机构）→ 按服务器升级逻辑挂入「白云」机构
  - 旧角色值 → 统一归为「用户」

用法：双击「一键导入数据库.bat」，或命令行  python 数据库导入工具.py
"""
import sqlite3, json, os, sys, secrets, glob, copy

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "ams_data.db")
BACKUP_DIR = os.path.join(BASE_DIR, "backup")
PORT = 8300
ROLE_FIX = {"admin": "admin", "user": "user"}  # 其余一律归为 user


# ---------- 基础 ----------
def conn_rw(path=None):
    c = sqlite3.connect(path or DB_FILE)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=5000")
    return c


def now_str():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def ensure_schema(c):
    """目标库不存在时建空结构（与服务器 init_db 一致）。"""
    c.execute("""CREATE TABLE IF NOT EXISTS kv(
                 k TEXT PRIMARY KEY, v TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1)""")
    c.execute("""CREATE TABLE IF NOT EXISTS orgs(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 name TEXT NOT NULL UNIQUE,
                 created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS users(
                 username TEXT PRIMARY KEY, display_name TEXT NOT NULL, role TEXT NOT NULL,
                 salt TEXT NOT NULL, pwd TEXT NOT NULL, created_at TEXT NOT NULL,
                 dept TEXT NOT NULL DEFAULT '', org TEXT NOT NULL DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS sessions(
                 token TEXT PRIMARY KEY, username TEXT NOT NULL, created_at TEXT NOT NULL)""")


def default_org_data():
    return {"depts": ["信息科"],
            "cats": [["计算机设备", "IT"], ["办公设备", "BG"], ["医疗设备", "YL"],
                     ["检验设备", "JY"], ["网络设备", "NW"], ["家具设备", "JJ"]],
            "assets": [], "records": [], "stocktakes": [],
            "seq": {"RK": 1, "CK": 1, "ZY": 1, "TH": 1, "PD": 1, "asset": 1},
            "role": "系统管理员"}


def backup_target():
    """导入前自动备份当前正式库（sqlite 在线备份，服务器运行中也安全）。"""
    if not os.path.exists(DB_FILE):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    from datetime import datetime
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(BACKUP_DIR, "ams_backup_%s_导入前.db" % stamp)
    src = sqlite3.connect(DB_FILE)
    dstc = sqlite3.connect(dst)
    with dstc:
        src.backup(dstc)
    dstc.close(); src.close()
    return dst


def server_running():
    """探测本机服务器是否在运行（默认 8300）。"""
    import socket
    try:
        s = socket.socket(); s.settimeout(0.6)
        s.connect(("127.0.0.1", PORT)); s.close()
        return True
    except Exception:
        return False


# ---------- 源库读取与规范化 ----------
def read_source(path):
    """只读打开源库 → 统一结构 {orgs, users, kv, legacy}。
    兼容旧版：缺 dept/org 列自动补默认值；单库版 kv['db'] 按服务器升级逻辑挂「白云」。"""
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    c.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "kv" not in tables or "users" not in tables:
            raise ValueError("不是本系统的数据库文件（缺少 kv / users 表）")
        users = []
        for r in c.execute("SELECT * FROM users"):
            d = dict(r)
            d.setdefault("dept", "")
            d.setdefault("org", "")
            d["role"] = ROLE_FIX.get(str(d.get("role") or ""), "user")
            users.append(d)
        orgs = [dict(r) for r in c.execute("SELECT * FROM orgs")] if "orgs" in tables else []
        kv = {r["k"]: r["v"] for r in c.execute("SELECT k,v FROM kv")}
    finally:
        c.close()

    legacy = []
    has_old = "db" in kv
    has_new = any(k.startswith("db:") for k in kv)
    if has_old and has_new:
        kv.pop("db")
        legacy.append("检测到遗留单库键 db（与机构数据并存，已忽略）")
    elif has_old and not has_new:
        # 单库版 → 模拟服务器多机构升级：建「白云」「康复」，历史数据整体迁入白云
        stamp = now_str()
        orgs = [{"id": 1, "name": "白云", "created_at": stamp},
                {"id": 2, "name": "康复", "created_at": stamp}]
        kv["db:1"] = kv.pop("db")
        kv["db:2"] = json.dumps(default_org_data(), ensure_ascii=False)
        for u in users:
            if u["username"] != "admin" and not u.get("org"):
                u["org"] = "1"
        legacy.append("单库版：已按服务器升级逻辑迁入机构「白云」，并创建空机构「康复」")
    elif not has_new and not has_old:
        raise ValueError("数据库 kv 表中没有本系统数据（既无 db: 键也无 db 键）")
    # 机构缺 kv 数据（如手工删过）补空
    for o in orgs:
        key = "db:%s" % o["id"]
        if key not in kv:
            kv[key] = json.dumps(default_org_data(), ensure_ascii=False)
    return {"orgs": orgs, "users": users, "kv": kv, "legacy": legacy}


def src_stats(src):
    """源库统计：每机构资产/单据/盘点数。"""
    out = []
    for o in src["orgs"]:
        try:
            d = json.loads(src["kv"].get("db:%s" % o["id"], "{}"))
        except Exception:
            d = {}
        out.append({"name": o["name"], "assets": len(d.get("assets") or []),
                    "records": len(d.get("records") or []),
                    "stocktakes": len(d.get("stocktakes") or [])})
    return out


def load_target():
    """读当前正式库现状（不存在 → 空结构）。"""
    if not os.path.exists(DB_FILE):
        return {"orgs": [], "users": [], "kv": {}, "exists": False}
    c = conn_rw()
    try:
        orgs = [dict(r) for r in c.execute("SELECT * FROM orgs")]
        users = [dict(r) for r in c.execute("SELECT * FROM users")]
        kv = {r["k"]: r["v"] for r in c.execute("SELECT k,v FROM kv")}
        return {"orgs": orgs, "users": users, "kv": kv, "exists": True}
    finally:
        c.close()


# ---------- 合并核心 ----------
def merge_org_data(src_data, tgt_data):
    """把源机构数据并入目标机构数据（纯数据操作，便于测试）。
    冲突策略：资产编号冲突→自动重排（履历记录原编号）；资产 id 冲突→重生成并同步单据/盘点引用；
    单据与盘点追加；分类/部门并集；流水号取较大。返回统计。"""
    stats = {"assets": 0, "recode": [], "reid": 0, "records": 0, "stocktakes": 0}
    t_assets = tgt_data.setdefault("assets", [])
    t_codes = {str(a.get("code") or "") for a in t_assets}
    t_ids = {a.get("id") for a in t_assets}
    s_assets = src_data.get("assets") or []
    seq = tgt_data.setdefault("seq", {}) or {}
    yr = now_str()[:4]
    code_map, id_map = {}, {}
    for a in s_assets:
        a = copy.deepcopy(a)          # 深拷贝：绝不改动源数据（履历/盘点条目亦不共享）
        stats["assets"] += 1
        old_code = str(a.get("code") or "")
        recoded = reided = False
        if old_code and old_code in t_codes:            # 编号冲突 → 重排
            n = int(seq.get("asset") or 1)
            new_code = "PRL%s%04d" % (yr, n)
            while new_code in t_codes:
                n += 1
                new_code = "PRL%s%04d" % (yr, n)
            seq["asset"] = n + 1
            code_map[old_code] = new_code
            a["code"] = new_code
            t_codes.add(new_code)
            recoded = True
        elif old_code:
            t_codes.add(old_code)
        old_id = a.get("id")
        if not old_id or old_id in t_ids:                # id 冲突 / 缺失 → 重生成
            new_id = "a%s" % secrets.token_hex(8)
            if old_id:
                id_map[old_id] = new_id
                stats["reid"] += 1
            a["id"] = new_id
            old_id = new_id
            reided = True
        t_ids.add(old_id)
        # 履历：先替换旧编号文本，再头插合并记录（避免留痕的原编号被误替换）
        hist = a.get("history") or []
        if code_map:
            for h in hist:
                if isinstance(h.get("desc"), str):
                    for old, new in code_map.items():
                        h["desc"] = h["desc"].replace(old, new)
        if recoded or reided:
            desc = "由数据库导入工具并入本机构"
            if recoded:
                desc += "；原编号 %s → %s" % (old_code, code_map[old_code])
            hist.insert(0, {"t": now_str(), "act": "合并导入", "desc": desc, "by": "导入工具"})
        a["history"] = hist
        t_assets.append(a)
    tgt_data["assets"] = t_assets
    # 单据：追加 + 资产引用同步 + 编号文本替换 + 单据号 id 去重
    recs = tgt_data.setdefault("records", [])
    t_rec_ids = {r.get("id") for r in recs}
    for r in (src_data.get("records") or []):
        r = copy.deepcopy(r)
        if r.get("assetId") and r["assetId"] in id_map:
            r["assetId"] = id_map[r["assetId"]]
        if code_map and isinstance(r.get("detail"), str):
            for old, new in code_map.items():
                r["detail"] = r["detail"].replace(old, new)
        if not r.get("id") or r["id"] in t_rec_ids:
            r["id"] = "r%s" % secrets.token_hex(8)
        t_rec_ids.add(r["id"])
        recs.append(r)
        stats["records"] += 1
    # 盘点计划：追加 + 资产引用同步 + id 去重
    sts = tgt_data.setdefault("stocktakes", [])
    t_st_ids = {s.get("id") for s in sts}
    for s in (src_data.get("stocktakes") or []):
        s = copy.deepcopy(s)
        if not s.get("id") or s["id"] in t_st_ids:
            s["id"] = "st%s" % secrets.token_hex(8)
        t_st_ids.add(s["id"])
        for it in (s.get("items") or []):
            if it.get("assetId") in id_map:
                it["assetId"] = id_map[it["assetId"]]
        sts.append(s)
        stats["stocktakes"] += 1
    # 部门 / 分类并集（目标优先）
    tgt_data["depts"] = list(dict.fromkeys((tgt_data.get("depts") or []) + (src_data.get("depts") or [])))
    t_cats = {c[0]: c for c in (tgt_data.get("cats") or []) if isinstance(c, list) and len(c) >= 2}
    for c in (src_data.get("cats") or []):
        if isinstance(c, list) and len(c) >= 2 and c[0] not in t_cats:
            t_cats[c[0]] = c
    tgt_data["cats"] = list(t_cats.values())
    # 流水号取较大（各前缀）
    for k, v in (src_data.get("seq") or {}).items():
        try:
            seq[k] = max(int(seq.get(k) or 0), int(v))
        except (TypeError, ValueError):
            pass
    return stats


def do_merge(src):
    """合并并入：按机构名对齐（同名合并 / 异名新增）。返回结果文本行列表。"""
    tgt = load_target()
    name2tid = {o["name"]: o["id"] for o in tgt["orgs"]}   # 机构名 → 目标机构 id
    org_map = {}                                            # 源机构 id → 目标机构 id
    lines, skipped_users = [], 0
    c = conn_rw()
    try:
        ensure_schema(c)
        with c:
            for o in src["orgs"]:
                sd = json.loads(src["kv"].get("db:%s" % o["id"], "{}"))
                if o["name"] in name2tid:                   # 同名机构 → 合并
                    tid = name2tid[o["name"]]
                    key = "db:%s" % tid
                    if key in tgt["kv"]:
                        td = json.loads(tgt["kv"][key])
                    else:
                        td = default_org_data()
                    st = merge_org_data(sd, td)
                    c.execute("UPDATE kv SET v=?, version=version+1 WHERE k=?",
                              (json.dumps(td, ensure_ascii=False), key))
                    if c.execute("SELECT changes()").fetchone()[0] == 0:
                        c.execute("INSERT INTO kv(k,v,version) VALUES(?,?,1)",
                                  (key, json.dumps(td, ensure_ascii=False)))
                    lines.append("机构「%s」：并入现有同名机构（资产 %d 台，单据 %d 条%s%s）" % (
                        o["name"], st["assets"], st["records"],
                        "，编号重排 %d 台" % len(st["recode"]) if st["recode"] else "",
                        "，资产标识重建 %d 台" % st["reid"] if st["reid"] else ""))
                else:                                       # 新机构 → 新增
                    cur = c.execute("INSERT INTO orgs(name,created_at) VALUES(?,?)",
                                    (o["name"], now_str()))
                    tid = cur.lastrowid
                    name2tid[o["name"]] = tid
                    c.execute("INSERT INTO kv(k,v,version) VALUES(?,?,1)",
                              ("db:%s" % tid, json.dumps(sd, ensure_ascii=False)))
                    lines.append("机构「%s」：新增（资产 %d 台，单据 %d 条）" % (
                        o["name"], len(sd.get("assets") or []), len(sd.get("records") or [])))
                org_map[str(o["id"])] = str(tid)
            # 用户：同名保留当前库，其余导入（机构归属按名字映射）
            t_users = {u["username"] for u in tgt["users"]}
            n_new = 0
            for u in src["users"]:
                if u["username"] in t_users:
                    skipped_users += 1
                    continue
                org = str(u.get("org") or "")
                org = org_map.get(org, org)
                c.execute("INSERT INTO users(username,display_name,role,salt,pwd,created_at,dept,org) VALUES(?,?,?,?,?,?,?,?)",
                          (u["username"], u["display_name"], u["role"], u["salt"], u["pwd"],
                           u.get("created_at") or now_str(), u.get("dept") or "", org))
                t_users.add(u["username"])
                n_new += 1
            lines.append("用户：新增 %d 个%s" % (n_new, "，同名账号保留当前库 %d 个" % skipped_users if skipped_users else ""))
    finally:
        c.close()
    return lines


def do_replace(src):
    """整体替换：备份当前库 → 清空 → 写入源库（orgs 保留原 id，sessions 清空）。"""
    with conn_rw() as c:
        ensure_schema(c)
        c.execute("DELETE FROM kv")
        c.execute("DELETE FROM orgs")
        c.execute("DELETE FROM users")
        c.execute("DELETE FROM sessions")
        for o in src["orgs"]:
            c.execute("INSERT INTO orgs(id,name,created_at) VALUES(?,?,?)",
                      (o["id"], o["name"], o.get("created_at") or now_str()))
        for u in src["users"]:
            c.execute("INSERT INTO users(username,display_name,role,salt,pwd,created_at,dept,org) VALUES(?,?,?,?,?,?,?,?)",
                      (u["username"], u["display_name"], u["role"], u["salt"], u["pwd"],
                       u.get("created_at") or now_str(), u.get("dept") or "", str(u.get("org") or "")))
        for k, v in src["kv"].items():
            c.execute("INSERT INTO kv(k,v,version) VALUES(?,?,1)", (k, v))


# ---------- 预览与交互 ----------
def scan_candidates():
    """扫描可导入的 .db：项目目录（排除正式库）+ backup 目录，按修改时间倒序。"""
    seen, files = set(), []
    for pat in (os.path.join(BASE_DIR, "*.db"), os.path.join(BACKUP_DIR, "*.db")):
        for f in glob.glob(pat):
            rp = os.path.realpath(f)
            if rp == os.path.realpath(DB_FILE):
                continue
            if rp not in seen:
                seen.add(rp)
                files.append(f)
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    out = []
    for f in files[:12]:
        try:
            s = read_source(f)
            names = "、".join(x["name"] for x in src_stats(s))
            out.append({"path": f, "desc": "%d 机构 / %d 用户 / %d 台资产（%s）" % (
                len(s["orgs"]), len(s["users"]), sum(x["assets"] for x in src_stats(s)), names)})
        except Exception as e:
            out.append({"path": f, "desc": "（无法识别：%s）" % e})
    return out


def print_preview(src, path, mode):
    print()
    print("─" * 56)
    print("  源库预览：%s" % os.path.basename(path))
    print("─" * 56)
    for note in src["legacy"]:
        print("  ※ %s" % note)
    for s in src_stats(src):
        print("  机构「%s」：资产 %d 台 / 单据 %d 条 / 盘点计划 %d 个" % (s["name"], s["assets"], s["records"], s["stocktakes"]))
    admins = [u["username"] for u in src["users"] if u["role"] == "admin"]
    print("  用户 %d 个（管理员：%s）" % (len(src["users"]), "、".join(admins) or "无"))
    if mode == "1":
        print()
        print("  【整体替换】当前库将先自动备份，然后整体换为以上内容；")
        print("  所有登录状态将被清除，导入后请用源库的账号密码登录（如 %s）。" % (admins[0] if admins else "源库管理员"))
    else:
        tgt = load_target()
        t_names = {o["name"] for o in tgt["orgs"]}
        same = [o["name"] for o in src["orgs"] if o["name"] in t_names]
        new = [o["name"] for o in src["orgs"] if o["name"] not in t_names]
        t_users = {u["username"] for u in tgt["users"]}
        n_skip = sum(1 for u in src["users"] if u["username"] in t_users)
        n_code = 0
        for o in src["orgs"]:
            if o["name"] in t_names:
                tid = next(t["id"] for t in tgt["orgs"] if t["name"] == o["name"])
                key = "db:%s" % tid
                td = json.loads(tgt["kv"][key]) if key in tgt["kv"] else {}
                t_codes = {str(a.get("code") or "") for a in (td.get("assets") or [])}
                sd = json.loads(src["kv"].get("db:%s" % o["id"], "{}"))
                n_code += sum(1 for a in (sd.get("assets") or []) if str(a.get("code") or "") in t_codes)
        print()
        print("  【合并并入】同名机构合并：%s；新增机构：%s" % ("、".join(same) or "无", "、".join(new) or "无"))
        print("  同名用户账号保留当前库 %d 个；预计编号冲突 %d 台（导入时自动重排，履历留痕）" % (n_skip, n_code))
        n_src_assets = 0
        for o in src["orgs"]:
            try:
                sd = json.loads(src["kv"].get("db:%s" % o["id"], "{}"))
            except Exception:
                sd = {}
            n_src_assets += len(sd.get("assets") or [])
        if n_src_assets and n_code >= n_src_assets * 0.5:
            print("  ⚠ 警告：源库与当前库数据高度重合（%d/%d 台编号冲突）。合并会产生大量重复资产，" % (n_code, n_src_assets))
            print("    若目的是恢复到源库状态，建议改用「整体替换」模式。")
        print("  现有登录状态不受影响，导入后刷新页面即可。")


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def main():
    print("=" * 56)
    print("   资产管理系统 · 数据库一键导入工具")
    print("=" * 56)
    if not os.path.exists(DB_FILE):
        print("  当前目录暂无 ams_data.db（新环境）：导入将创建全新数据库。")
    print("  扫描可导入的数据库文件...")
    cands = scan_candidates()
    for i, cd in enumerate(cands, 1):
        print("   [%2d] %s" % (i, os.path.basename(cd["path"])))
        print("        %s" % cd["desc"])
    print("    [ 0] 其他路径（手动输入 / 直接拖拽文件到此窗口）")
    sel = ask("请选择要导入的数据库 [0-%d]：" % len(cands))
    path = ""
    if sel.isdigit() and 1 <= int(sel) <= len(cands):
        path = cands[int(sel) - 1]["path"]
    else:
        path = ask("请输入数据库文件完整路径：").strip('"').strip("'")
    if not path or not os.path.exists(path):
        print("未找到文件，已退出。")
        return
    if os.path.realpath(path) == os.path.realpath(DB_FILE):
        print("所选文件就是当前正式库本身，无需导入。")
        return
    try:
        src = read_source(path)
    except Exception as e:
        print("读取失败：%s" % e)
        return
    mode = ask("请选择导入模式：[1] 整体替换（换机/恢复备份）  [2] 合并并入（多库合一）：")
    while mode not in ("1", "2"):
        mode = ask("请输入 1 或 2：")
    print_preview(src, path, mode)
    sure = ask("\n确认执行导入？此操作会先自动备份当前库（输入 yes 回车确认）：")
    if sure.lower() != "yes":
        print("已取消，未做任何修改。")
        return
    bak = backup_target()
    if bak:
        print("已备份当前库 → %s" % os.path.relpath(bak, BASE_DIR))
    try:
        if mode == "1":
            do_replace(src)
            print("\n[完成] 整体替换成功：当前库已换为源库内容（机构 %d / 用户 %d / 资产 %d 台）。" % (
                len(src["orgs"]), len(src["users"]), sum(s["assets"] for s in src_stats(src))))
            print("登录状态已清除，请用源库的账号密码重新登录。")
        else:
            lines = do_merge(src)
            print("\n[完成] 合并并入成功：")
            for ln in lines:
                print("  · " + ln)
    except Exception as e:
        print("\n[失败] 导入出错（当前库已回滚，未受影响）：%s" % e)
        return
    if server_running():
        print("\n服务器正在运行：数据即时生效，浏览器刷新页面即可查看%s。" % ("（替换模式需重新登录）" if mode == "1" else ""))
    else:
        print("\n服务器未运行：下次双击「启动资产系统服务器.bat」即使用导入后的数据。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消。")
    finally:
        try:
            ask("\n按回车键退出...")
        except EOFError:
            pass
