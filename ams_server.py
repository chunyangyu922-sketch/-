#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
固定资产管理系统 · 网络版服务器
================================
单文件后端，仅使用 Python 标准库（无需 pip 安装任何东西）。

功能：
  - 静态页面服务：GET /        -> 同目录下的 资产管理系统_网络版.html
  - 登录认证：    POST /api/login /api/logout，GET /api/me
  - 数据同步：    GET  /api/db（拉取最新数据）
                  POST /api/db（保存数据，带版本号乐观锁，防止互相覆盖）
  - 用户管理：    GET/POST /api/users、POST /api/users/update、POST /api/users/delete、POST /api/users/rename-dept（仅管理员；后者用于部门改名时批量同步账号部门）
  - 机构管理：    GET /api/orgs、POST /api/orgs/add、POST /api/orgs/rename、POST /api/orgs/delete（仅管理员）；各机构数据独立存储（kv 键 db:<机构id>）
  - 系统重置：    POST /api/wipe-all（仅管理员；清空所有机构数据/机构/用户（内置 admin 除外），自动备份后重建空白机构）
  - 修改密码：    POST /api/changepwd（任何登录用户改自己的密码）

数据存储：与本文件同目录的 ams_data.db（SQLite 单文件数据库）。
首次启动自动创建数据库，并生成管理员账号：admin / admin123（请尽快修改密码）。

启动方式：  python ams_server.py   （或双击 启动资产系统.bat）
停止方式：  在命令行窗口按 Ctrl+C
"""

import json
import os
import re
import io
import secrets
import hashlib
import sqlite3
import sys
import base64
import zipfile
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ---------- 基础配置 ----------
HOST = "0.0.0.0"          # 监听所有网卡，公司内网其他电脑可通过 http://本机IP:8300 访问
PORT = 8300
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "ams_data.db")
WEB_FILE = os.path.join(BASE_DIR, "资产管理系统_网络版.html")
MAX_BODY = 32 * 1024 * 1024  # 请求体上限 32MB

# 服务器对内网暴露的访问基地址（启动时自动探测，注入页面用于生成二维码）
LAN_BASE = ""

ROLE_NAME = {"admin": "管理员", "user": "用户"}
STATUS_NAME = {"idle": "闲置", "inuse": "在用", "pend": "待发放", "back": "退回未进库"}


def log(msg):
    try:
        print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg))
    except Exception:
        pass  # 日志打印失败绝不能影响业务响应


# ---------- 数据库 ----------
def conn():
    c = sqlite3.connect(DB_FILE)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=5000")
    return c


def hash_pwd(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000).hex()


def default_data():
    """全新数据库的初始数据（空库，可在页面里载入演示数据）
    部门默认仅保留「信息科」一个：清空全部数据 / 新增机构后均从干净的基础开始。"""
    return {
        "depts": ["信息科"],
        "cats": [["计算机设备", "IT"], ["办公设备", "BG"], ["医疗设备", "YL"],
                 ["检验设备", "JY"], ["网络设备", "NW"], ["家具设备", "JJ"]],
        "assets": [], "records": [], "stocktakes": [],
        "seq": {"RK": 1, "CK": 1, "ZY": 1, "TH": 1, "PD": 1, "asset": 1},
        "role": "系统管理员",
    }


def create_org(c, name):
    """创建机构（仅机构表记录，不动 kv），返回自增 id。"""
    cur = c.execute("INSERT INTO orgs(name,created_at) VALUES(?,?)",
                    (name, datetime.now().strftime("%Y-%m-%d %H:%M")))
    return cur.lastrowid


def init_org_db(c, org_id):
    """为机构初始化一份数据（kv 键 db:<机构id>）。"""
    c.execute("INSERT INTO kv(k,v,version) VALUES(?,?,1)",
              (org_db_key(org_id), json.dumps(default_data(), ensure_ascii=False)))


def org_db_key(org_id):
    return "db:%s" % org_id


def init_db():
    with conn() as c:
        # 日志模式用 DELETE：事务直接写入主库文件，杜绝数据滞留在 -wal 日志文件的隐患
        c.execute("PRAGMA journal_mode=DELETE")
        c.execute("""CREATE TABLE IF NOT EXISTS kv(
                     k TEXT PRIMARY KEY, v TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1)""")
        c.execute("""CREATE TABLE IF NOT EXISTS orgs(
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     name TEXT NOT NULL UNIQUE,
                     created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS users(
                     username TEXT PRIMARY KEY,
                     display_name TEXT NOT NULL,
                     role TEXT NOT NULL,
                     salt TEXT NOT NULL,
                     pwd TEXT NOT NULL,
                     created_at TEXT NOT NULL,
                     dept TEXT NOT NULL DEFAULT '')""")
        # 旧库迁移：无 dept 列则补上（默认空，表示未分配部门）
        cols = [r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()]
        if "dept" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN dept TEXT NOT NULL DEFAULT ''")
            log("用户表已升级：新增部门字段")
        # 旧库迁移：无 org 列则补上（空 = 不限机构：内置 admin 专用，登录后可看全部机构总览）
        if "org" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN org TEXT NOT NULL DEFAULT ''")
            log("用户表已升级：新增机构字段")
        c.execute("""CREATE TABLE IF NOT EXISTS sessions(
                     token TEXT PRIMARY KEY,
                     username TEXT NOT NULL,
                     created_at TEXT NOT NULL)""")
        # ---- 多机构升级（幂等）：单库 kv['db'] → 各机构独立 kv['db:<机构id>] ----
        has_old_db = c.execute("SELECT 1 FROM kv WHERE k='db'").fetchone() is not None
        org_cnt = c.execute("SELECT COUNT(*) n FROM orgs").fetchone()["n"]
        if org_cnt == 0:
            # 首次机构化：建立「白云」「康复」两个机构
            baiyun_id = create_org(c, "白云")
            kangfu_id = create_org(c, "康复")
            init_org_db(c, kangfu_id)
            if has_old_db:
                # 历史单库数据整体迁入白云（键名 db → db:白云id）；用户除内置 admin 外全部归入白云
                c.execute("UPDATE kv SET k=? WHERE k='db'", (org_db_key(baiyun_id),))
                c.execute("UPDATE users SET org=? WHERE username<>'admin'", (str(baiyun_id),))
                log("多机构升级完成：历史数据已整体迁入机构「白云」，并创建空机构「康复」；内置 admin 保持不限机构（可看总览）")
            else:
                init_org_db(c, baiyun_id)
                log("多机构初始化完成：已创建机构「白云」「康复」（均为空库）")
        elif has_old_db:
            # 兑底：orgs 已存在但仍有旧键（异常中断的升级），迁入第一个机构
            orow = c.execute("SELECT id,name FROM orgs ORDER BY id LIMIT 1").fetchone()
            c.execute("UPDATE kv SET k=? WHERE k='db'", (org_db_key(orow["id"]),))
            c.execute("UPDATE users SET org=? WHERE org='' AND username<>'admin'", (str(orow["id"]),))
            log("检测到遗留单库数据，已迁入机构「%s」" % orow["name"])
        if c.execute("SELECT 1 FROM users").fetchone() is None:
            add_user_row(c, "admin", "管理员", "admin", "admin123")
            log("已创建默认管理员账号：admin / admin123 （请登录后尽快修改密码）")
        # 旧版角色兼容：统一迁移为 管理员(admin)/用户(user) 两类
        n = c.execute("UPDATE users SET role='user' WHERE role NOT IN ('admin','user')").rowcount
        if n:
            log("已将 %d 个旧角色账号迁移为「用户」" % n)


def add_user_row(c, username, display_name, role, password, dept="", org=""):
    salt = secrets.token_hex(16)
    c.execute("INSERT INTO users(username,display_name,role,salt,pwd,created_at,dept,org) VALUES(?,?,?,?,?,?,?,?)",
              (username, display_name, role, salt, hash_pwd(password, salt),
               datetime.now().strftime("%Y-%m-%d %H:%M"), dept, org))


# ---------- 自动备份 ----------
BACKUP_DIR = os.path.join(BASE_DIR, "backup")
MAX_BACKUPS = 30            # 备份最多保留份数，超出自动清理最旧的
_last_backup_day = [""]


def backup_db(reason):
    """在线备份整个数据库（含用户表），不中断服务；自动清理超出份数的旧备份。"""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(BACKUP_DIR, "ams_backup_%s_%s.db" % (stamp, reason))
        src = sqlite3.connect(DB_FILE)
        dstc = sqlite3.connect(dst)
        with dstc:
            src.backup(dstc)
        dstc.close()
        src.close()
        log("已备份数据库（%s）→ backup/%s" % (reason, os.path.basename(dst)))
        olds = sorted(f for f in os.listdir(BACKUP_DIR)
                      if f.startswith("ams_backup_") and f.endswith(".db"))
        while len(olds) > MAX_BACKUPS:
            try:
                os.remove(os.path.join(BACKUP_DIR, olds.pop(0)))
            except OSError:
                break
        return dst
    except Exception as e:
        log("备份失败（不影响业务）：%s" % e)
        return None


def ensure_daily_backup():
    """每个自然日的第一次数据写入前自动备份一次。"""
    today = datetime.now().strftime("%Y-%m-%d")
    if _last_backup_day[0] != today:
        _last_backup_day[0] = today
        backup_db("每日")


# ---------- 多机构 ----------
def list_orgs(c):
    return [dict(r) for r in c.execute("SELECT id,name,created_at FROM orgs ORDER BY id").fetchall()]


def org_list_simple():
    """精简机构列表（id 统一转字符串），随 /api/db 下发供前端下拉使用。"""
    with conn() as c:
        return [{"id": str(r["id"]), "name": r["name"]}
                for r in c.execute("SELECT id,name FROM orgs ORDER BY id").fetchall()]


def load_org_db(c, org_id):
    """读取某机构数据，返回 (data, version)；不存在返回 (None, 0)。"""
    row = c.execute("SELECT v,version FROM kv WHERE k=?", (org_db_key(org_id),)).fetchone()
    if not row:
        return None, 0
    return json.loads(row["v"]), row["version"]


def strip_org_marks(data):
    """写入前剥去总览合并时注入的 _org 标记，避免污染库内数据。"""
    for key in ("assets", "records", "stocktakes"):
        for item in data.get(key) or []:
            if isinstance(item, dict):
                item.pop("_org", None)
    return data


def merged_db_all():
    """总览（只读）：合并所有机构数据；资产/单据/盘点每条注入 _org=机构名。"""
    merged = {"depts": [], "cats": [], "locs": [], "assets": [], "records": [], "stocktakes": [],
              "seq": {"RK": 0, "CK": 0, "ZY": 0, "TH": 0, "PD": 0, "asset": 0}, "role": "系统管理员"}
    seen_depts, seen_cats, seen_locs = set(), set(), set()
    with conn() as c:
        for org in list_orgs(c):
            data, _v = load_org_db(c, org["id"])
            if data is None:
                continue
            for key in ("assets", "records", "stocktakes"):
                for item in data.get(key) or []:
                    if isinstance(item, dict):
                        item = dict(item)
                        item["_org"] = org["name"]
                        merged[key].append(item)
            for d in data.get("depts") or []:
                if d not in seen_depts:
                    seen_depts.add(d)
                    merged["depts"].append(d)
            for cat in data.get("cats") or []:
                ck = tuple(cat) if isinstance(cat, list) else str(cat)
                if ck not in seen_cats:
                    seen_cats.add(ck)
                    merged["cats"].append(cat)
            for l in data.get("locs") or []:
                if l not in seen_locs:
                    seen_locs.add(l)
                    merged["locs"].append(l)
            for k in merged["seq"]:
                merged["seq"][k] = max(merged["seq"][k], (data.get("seq") or {}).get(k, 0) or 0)
    return merged


def resolve_scope(user, q_org):
    """解析数据范围：('ALL', None)＝总览；(机构id字符串, 机构名)＝单机构；(None, None)＝无效。"""
    if user["role"] == "admin" and not user.get("org"):
        # 超级管理员（未绑定机构）：可看总览，也可指定任意机构
        if not q_org or q_org == "ALL":
            return "ALL", None
        if not str(q_org).isdigit():
            return None, None
        with conn() as c:
            row = c.execute("SELECT id,name FROM orgs WHERE id=?", (int(q_org),)).fetchone()
        return (str(row["id"]), row["name"]) if row else (None, None)
    # 普通用户 / 已绑定机构的管理员：只能看本机构
    org = str(user.get("org") or "")
    if not org or not org.isdigit():
        return None, None
    with conn() as c:
        row = c.execute("SELECT id,name FROM orgs WHERE id=?", (int(org),)).fetchone()
    return (str(row["id"]), row["name"]) if row else (None, None)


# ---------- 会话 ----------
def create_session(username):
    token = secrets.token_hex(32)
    with conn() as c:
        c.execute("INSERT INTO sessions(token,username,created_at) VALUES(?,?,?)",
                  (token, username, datetime.now().strftime("%Y-%m-%d %H:%M")))
    return token


def get_user_by_token(token):
    if not token:
        return None
    with conn() as c:
        row = c.execute("""SELECT u.username,u.display_name AS displayName,u.role,
                                  u.org AS org, o.name AS orgName
                           FROM sessions s JOIN users u ON u.username=s.username
                           LEFT JOIN orgs o ON o.id=CAST(u.org AS INTEGER)
                           WHERE s.token=?""", (token,)).fetchone()
    if not row:
        return None
    u = dict(row)
    u["org"] = str(u["org"] or "")
    return u


def drop_session(token):
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))


# ---------- Excel(.xlsx) 解析（纯标准库，供前端导入入库 / Excel回填导出使用） ----------
SS_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
ODR_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _xlsx_col_index(ref):
    """单元格引用（如 B2）→ 0 基列号"""
    n = 0
    for ch in str(ref or ""):
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n - 1


def _xlsx_date_str(serial):
    """Excel 1900 日期序列值 → YYYY-MM-DD 字符串（失败返回 None）"""
    try:
        d = datetime(1899, 12, 30) + timedelta(days=float(serial))
        return d.strftime("%Y-%m-%d")
    except Exception:
        return None


def parse_xlsx_bytes(data):
    """解析 .xlsx 字节流 → [{name, rows, cols}]
    rows 为二维数组：字符串（sharedStrings/inlineStr）或数字原文；
    日期样式单元格自动转为 YYYY-MM-DD；按单元格引用还原稀疏空行空列。"""
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())

    # 1) 共享字符串表
    shared = []
    if "xl/sharedStrings.xml" in names:
        for si in ET.fromstring(zf.read("xl/sharedStrings.xml")).iter("{%s}si" % SS_NS):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % SS_NS)))

    # 2) 日期样式索引（cellXfs 中 numFmt 为日期格式的 xf 序号）
    date_styles = set()
    if "xl/styles.xml" in names:
        sroot = ET.fromstring(zf.read("xl/styles.xml"))
        custom = {}
        for fmt in sroot.iter("{%s}numFmt" % SS_NS):
            custom[fmt.get("numFmtId")] = (fmt.get("formatCode") or "").lower()
        builtin_date = {14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47}
        for xfs in sroot.iter("{%s}cellXfs" % SS_NS):
            for i, xf in enumerate(xfs.findall("{%s}xf" % SS_NS)):
                fid = xf.get("numFmtId") or "0"
                try:
                    fidn = int(fid)
                except Exception:
                    fidn = 0
                code = custom.get(fid, "")
                looks_date = fidn in builtin_date or (
                    ("y" in code or "年" in code) and ("m" in code or "d" in code or "月" in code or "日" in code))
                if looks_date:
                    date_styles.add(i)

    # 3) 工作表顺序（workbook.xml + rels）
    rels = {}
    if "xl/_rels/workbook.xml.rels" in names:
        for rel in ET.fromstring(zf.read("xl/_rels/workbook.xml.rels")).iter(
                "{%s}Relationship" % PKG_REL_NS):
            rels[rel.get("Id")] = rel.get("Target") or ""
    sheets = []
    for sh in ET.fromstring(zf.read("xl/workbook.xml")).iter("{%s}sheet" % SS_NS):
        target = rels.get(sh.get("{%s}id" % ODR_NS), "")
        if not target:
            continue
        spath = target.lstrip("/") if target.startswith("/") else (
            target if target.startswith("xl/") else "xl/" + target)
        sheets.append((sh.get("name") or "Sheet", spath))

    # 4) 逐表解析单元格
    result = []
    for sname, spath in sheets:
        if spath not in names:
            continue
        root = ET.fromstring(zf.read(spath))
        picked = []          # (行号, {列号: 值})
        max_col = -1
        for row in root.iter("{%s}row" % SS_NS):
            try:
                ri = int(row.get("r") or (len(picked) + 1))
            except Exception:
                ri = len(picked) + 1
            cells = {}
            for c in row.findall("{%s}c" % SS_NS):
                ci = _xlsx_col_index(c.get("r"))
                if ci < 0:
                    continue
                t = c.get("t")
                style = c.get("s")
                v = ""
                if t == "inlineStr":
                    is_el = c.find("{%s}is" % SS_NS)
                    if is_el is not None:
                        v = "".join(x.text or "" for x in is_el.iter("{%s}t" % SS_NS))
                else:
                    ve = c.find("{%s}v" % SS_NS)
                    if ve is not None and ve.text is not None:
                        raw = ve.text
                        if t == "s":                      # 共享字符串
                            try:
                                v = shared[int(raw)]
                            except Exception:
                                v = raw
                        elif t == "b":                    # 布尔
                            v = "TRUE" if raw == "1" else "FALSE"
                        else:                             # 数字 / 公式结果 / 日期序列
                            try:
                                si = int(style) if style and style.isdigit() else -1
                            except Exception:
                                si = -1
                            if si in date_styles:
                                v = _xlsx_date_str(raw) or raw
                            else:
                                v = raw
                cells[ci] = v
                if ci > max_col:
                    max_col = ci
            if cells:
                picked.append((ri, cells))
        if max_col < 0:
            result.append({"name": sname, "rows": [], "cols": 0})
            continue
        # 按行号升序并补齐中间空行
        picked.sort(key=lambda x: x[0])
        width = max_col + 1
        dense = []
        for ri, cells in picked:
            while len(dense) < ri - 1:
                dense.append([""] * width)
            arr = [""] * width
            for k, val in cells.items():
                arr[k] = val
            dense.append(arr)
        result.append({"name": sname, "rows": dense, "cols": width})
    return result


# ---------- HTTP 处理 ----------
class Handler(SimpleHTTPRequestHandler):
    server_version = "AMS-Server/1.0"

    # ---- 工具 ----
    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return None

    def current_user(self):
        auth = self.headers.get("Authorization") or ""
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        return get_user_by_token(token)

    def log_message(self, fmt, *args):
        pass  # 关闭默认访问日志，避免刷屏

    # ---- 静态页面 ----
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/health":
            return self.send_json({"ok": True, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        if path == "/api/db":
            user = self.current_user()
            if not user:
                return self.send_json({"ok": False, "error": "未登录"}, 401)
            q = parse_qs(urlparse(self.path).query)
            q_org = (q.get("org") or [""])[0].strip()
            org_id, org_name = resolve_scope(user, q_org)
            if not org_id:
                if user["role"] == "admin" and not user.get("org"):
                    return self.send_json({"ok": False, "error": "机构不存在"}, 404)
                return self.send_json({"ok": False, "error": "该账号未分配机构，请联系系统管理员在「系统设置 → 用户管理」中分配"}, 403)
            orgs = org_list_simple()
            if org_id == "ALL":
                return self.send_json({"ok": True, "version": 0, "data": merged_db_all(),
                                       "me": user, "org": "ALL", "orgs": orgs, "orgName": ""})
            with conn() as c:
                data, version = load_org_db(c, org_id)
            if data is None:
                return self.send_json({"ok": False, "error": "机构数据缺失，请检查数据库"}, 500)
            return self.send_json({"ok": True, "version": version, "data": data,
                                   "me": user, "org": org_id, "orgs": orgs, "orgName": org_name})
        if path == "/api/scan":
            q = parse_qs(urlparse(self.path).query)
            code = (q.get("c") or [""])[0].strip()
            if not code:
                return self.send_json({"ok": False, "error": "missing code"}, 400)
            hit, org_name = None, ""
            with conn() as c:
                # 扫码免登录：遍历全部机构查找（各机构编号独立流水，命中即返回并标注所属机构）
                for org in list_orgs(c):
                    data, _v = load_org_db(c, org["id"])
                    if data is None:
                        continue
                    x = next((a for a in data.get("assets", [])
                              if a.get("code") == code or a.get("productCode") == code), None)
                    if x:
                        hit, org_name = x, org["name"]
                        break
            if not hit:
                return self.send_json({"ok": False, "error": "not found"}, 404)
            x = hit
            return self.send_json({"ok": True, "asset": {
                "name": x.get("name"), "code": x.get("code"), "status": x.get("status"),
                "statusName": STATUS_NAME.get(x.get("status"), x.get("status")),
                "user": x.get("user") or "", "userDept": x.get("userDept") or "",
                "receiveDate": x.get("receiveDate") or "", "location": x.get("location") or "",
                "inDept": x.get("inDept") or "", "useDept": x.get("useDept") or "",
                "cat": x.get("cat") or "", "spec": x.get("spec") or "",
                "org": org_name,
            }})
        if path == "/api/orgs":
            user = self.current_user()
            if not user:
                return self.send_json({"ok": False, "error": "未登录"}, 401)
            if user["role"] != "admin":
                return self.send_json({"ok": False, "error": "仅系统管理员可管理机构"}, 403)
            orgs = []
            with conn() as c:
                for org in list_orgs(c):
                    data, _v = load_org_db(c, org["id"])
                    d = data or {}
                    orgs.append({"id": str(org["id"]), "name": org["name"],
                                 "created_at": org["created_at"],
                                 "assets": len(d.get("assets") or []),
                                 "records": len(d.get("records") or []),
                                 "stocktakes": len(d.get("stocktakes") or [])})
            return self.send_json({"ok": True, "orgs": orgs})
        if path == "/api/users/directory":
            # 所有登录用户可访问的注册用户名单（含部门，用于先选部门再选人的级联选择），不泄露账号信息
            # 多机构：普通用户 / 已绑定机构的管理员只看到本机构同事；超级管理员默认全部（总览），可按 ?org= 过滤
            user = self.current_user()
            if not user:
                return self.send_json({"ok": False, "error": "未登录"}, 401)
            q = parse_qs(urlparse(self.path).query)
            q_org = (q.get("org") or [""])[0].strip()
            with conn() as c:
                if user["role"] == "admin" and not user.get("org"):
                    if q_org and q_org != "ALL" and q_org.isdigit():
                        rows = c.execute("SELECT display_name,dept FROM users WHERE org=? ORDER BY display_name",
                                         (q_org,)).fetchall()
                    else:
                        rows = c.execute("SELECT display_name,dept FROM users ORDER BY display_name").fetchall()
                else:
                    rows = c.execute("SELECT display_name,dept FROM users WHERE org=? ORDER BY display_name",
                                     (user.get("org") or "",)).fetchall()
            return self.send_json({"ok": True,
                                   "names": [r["display_name"] for r in rows],
                                   "dir": [{"name": r["display_name"], "dept": r["dept"] or ""} for r in rows]})
        if path in ("/", "/index.html"):
            try:
                with open(WEB_FILE, "rb") as f:
                    body = f.read()
                if LAN_BASE:
                    body = body.replace(b"__AMS_BASE__", LAN_BASE.encode("utf-8"))
            except FileNotFoundError:
                body = "未找到页面文件：请将 资产管理系统_网络版.html 与本程序放在同一目录。".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/s", "/s.html"):
            body = SCAN_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        return self.send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        data = self.read_json()
        if data is None:
            return self.send_json({"ok": False, "error": "请求体无效"}, 400)

        # ---- 登录 ----
        if path == "/api/login":
            username = str(data.get("username") or "").strip()
            password = str(data.get("password") or "")
            with conn() as c:
                row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if not row or hash_pwd(password, row["salt"]) != row["pwd"]:
                log("登录失败：%s" % username)
                return self.send_json({"ok": False, "error": "用户名或密码不正确"}, 401)
            token = create_session(username)
            org_name = ""
            if row["org"]:
                with conn() as c:
                    orow = c.execute("SELECT name FROM orgs WHERE id=?", (int(row["org"]),)).fetchone()
                    org_name = orow["name"] if orow else ""
            log("登录成功：%s（%s）%s" % (username, ROLE_NAME.get(row["role"], row["role"]),
                ("· 机构「%s」" % org_name) if org_name else ""))
            return self.send_json({"ok": True, "token": token,
                                   "user": {"username": row["username"],
                                            "displayName": row["display_name"],
                                            "role": row["role"],
                                            "roleName": ROLE_NAME.get(row["role"], row["role"]),
                                            "org": str(row["org"] or ""),
                                            "orgName": org_name}})

        # ---- 登出 ----
        if path == "/api/logout":
            auth = self.headers.get("Authorization") or ""
            token = auth[7:].strip() if auth.startswith("Bearer ") else ""
            drop_session(token)
            return self.send_json({"ok": True})

        # ---- 以下接口均需登录 ----
        user = self.current_user()
        if not user:
            return self.send_json({"ok": False, "error": "未登录或登录已过期"}, 401)
        # ---- 数据保存（按机构存取 + 乐观锁防覆盖） ----
        if path == "/api/db":
            if user["role"] not in ROLE_NAME:
                return self.send_json({"ok": False, "error": "当前账号角色无权修改数据"}, 403)
            body = data.get("data")
            if not isinstance(body, dict) or not isinstance(body.get("assets"), list) \
               or not isinstance(body.get("depts"), list):
                return self.send_json({"ok": False, "error": "数据格式不正确"}, 400)
            # 目标机构：超级管理员（未绑定机构）须指定具体机构；普通用户 / 已绑定机构的管理员强制写本机构
            if user["role"] == "admin" and not user.get("org"):
                q_org = str(data.get("org") or "").strip()
                if not q_org or q_org == "ALL":
                    return self.send_json({"ok": False,
                                           "error": "「全部机构」为只读汇总视图，请先切换到具体机构再修改"}, 400)
                if not q_org.isdigit():
                    return self.send_json({"ok": False, "error": "机构不存在"}, 404)
                with conn() as c:
                    orow = c.execute("SELECT id,name FROM orgs WHERE id=?", (int(q_org),)).fetchone()
                if not orow:
                    return self.send_json({"ok": False, "error": "机构不存在"}, 404)
                org_id, org_name = str(orow["id"]), orow["name"]
            else:
                org_id, org_name = resolve_scope(user, "")
                if not org_id:
                    return self.send_json({"ok": False, "error": "该账号未分配机构，无法保存数据"}, 403)
            ensure_daily_backup()
            strip_org_marks(body)
            key = org_db_key(org_id)
            with conn() as c:
                row = c.execute("SELECT version FROM kv WHERE k=?", (key,)).fetchone()
                if not row:
                    return self.send_json({"ok": False, "error": "机构数据缺失，请检查数据库"}, 500)
                server_v = row["version"]
                client_v = data.get("version")
                if client_v != server_v:
                    log("保存冲突（%s）：客户端版本 %s，服务器版本 %s" % (org_name, client_v, server_v))
                    return self.send_json({"ok": False, "error": "conflict",
                                           "version": server_v}, 409)
                c.execute("UPDATE kv SET v=?, version=version+1 WHERE k=?",
                          (json.dumps(body, ensure_ascii=False), key))
                new_v = server_v + 1
            log("%s 保存数据（%s · 版本 %s），当前 %d 台资产" %
                (user["displayName"], org_name, new_v, len(body.get("assets", []))))
            return self.send_json({"ok": True, "version": new_v, "org": org_id})

        # ---- 修改自己的密码 ----
        if path == "/api/changepwd":
            old = str(data.get("oldPassword") or "")
            new = str(data.get("newPassword") or "")
            if len(new) < 6:
                return self.send_json({"ok": False, "error": "新密码至少 6 位"}, 400)
            with conn() as c:
                row = c.execute("SELECT * FROM users WHERE username=?", (user["username"],)).fetchone()
                if hash_pwd(old, row["salt"]) != row["pwd"]:
                    return self.send_json({"ok": False, "error": "原密码不正确"}, 400)
                salt = secrets.token_hex(16)
                c.execute("UPDATE users SET salt=?,pwd=? WHERE username=?",
                          (salt, hash_pwd(new, salt), user["username"]))
            log("%s 修改了自己的密码" % user["displayName"])
            return self.send_json({"ok": True})

        # ---- 机构管理（仅管理员） ----
        if path == "/api/orgs/add":
            if user["role"] != "admin":
                return self.send_json({"ok": False, "error": "仅系统管理员可管理机构"}, 403)
            name = str(data.get("name") or "").strip()
            if not name:
                return self.send_json({"ok": False, "error": "请填写机构名称"}, 400)
            if len(name) > 30:
                return self.send_json({"ok": False, "error": "机构名称过长（最多 30 字）"}, 400)
            with conn() as c:
                if c.execute("SELECT 1 FROM orgs WHERE name=?", (name,)).fetchone():
                    return self.send_json({"ok": False, "error": "该机构名称已存在"}, 400)
                org_id = create_org(c, name)
                init_org_db(c, org_id)
            log("新增机构：%s（id=%s），已初始化空数据" % (name, org_id))
            return self.send_json({"ok": True, "id": str(org_id), "name": name})

        # ---- 重命名机构（仅管理员；数据按机构 id 存储，改名不影响任何业务数据） ----
        if path == "/api/orgs/rename":
            if user["role"] != "admin":
                return self.send_json({"ok": False, "error": "仅系统管理员可管理机构"}, 403)
            org_id = str(data.get("id") or "").strip()
            name = str(data.get("name") or "").strip()
            if not org_id.isdigit():
                return self.send_json({"ok": False, "error": "参数不正确"}, 400)
            if not name:
                return self.send_json({"ok": False, "error": "请填写机构名称"}, 400)
            if len(name) > 30:
                return self.send_json({"ok": False, "error": "机构名称过长（最多 30 字）"}, 400)
            with conn() as c:
                org = c.execute("SELECT id,name FROM orgs WHERE id=?", (int(org_id),)).fetchone()
                if not org:
                    return self.send_json({"ok": False, "error": "机构不存在，请刷新后重试"}, 404)
                if name == org["name"]:
                    return self.send_json({"ok": True, "oldName": org["name"], "newName": name})
                if c.execute("SELECT 1 FROM orgs WHERE name=? AND id<>?",
                             (name, org["id"])).fetchone():
                    return self.send_json({"ok": False, "error": "该机构名称已存在"}, 400)
                c.execute("UPDATE orgs SET name=? WHERE id=?", (name, org["id"]))
            log("机构重命名：%s -> %s（id=%s，业务数据不受影响）" % (org["name"], name, org_id))
            return self.send_json({"ok": True, "oldName": org["name"], "newName": name})

        # ---- 删除单个机构（仅管理员；先自动备份，再连数据一起删） ----
        if path == "/api/orgs/delete":
            if user["role"] != "admin":
                return self.send_json({"ok": False, "error": "仅系统管理员可管理机构"}, 403)
            org_id = str(data.get("id") or "").strip()
            if not org_id.isdigit():
                return self.send_json({"ok": False, "error": "参数不正确"}, 400)
            with conn() as c:
                org = c.execute("SELECT id,name FROM orgs WHERE id=?", (int(org_id),)).fetchone()
                if not org:
                    return self.send_json({"ok": False, "error": "机构不存在，请刷新后重试"}, 404)
                # 该机构下还有用户时不允许删（用户账号不会被连带删除），先在用户管理中迁移或删除
                n_users = c.execute("SELECT COUNT(*) n FROM users WHERE org=?",
                                    (str(org["id"]),)).fetchone()["n"]
                if n_users:
                    return self.send_json({"ok": False,
                                           "error": "该机构下有 %d 个用户，请先在「用户管理」中删除或迁移这些用户，再删除机构" % n_users}, 400)
                # 系统至少保留一个机构（全删后普通账号与业务录入均无处归属）
                org_cnt = c.execute("SELECT COUNT(*) n FROM orgs").fetchone()["n"]
                if org_cnt <= 1:
                    return self.send_json({"ok": False, "error": "系统至少需要保留一个机构"}, 400)
                d, _v = load_org_db(c, org["id"])
                d = d or {}
                n_assets = len(d.get("assets") or [])
                n_records = len(d.get("records") or [])
                n_st = len(d.get("stocktakes") or [])
            backup_db("删除机构_%s" % org["name"])  # 事务提交前备份，保留删除前完整状态
            with conn() as c:
                c.execute("DELETE FROM kv WHERE k=?", (org_db_key(org["id"]),))
                c.execute("DELETE FROM orgs WHERE id=?", (org["id"],))
            log("删除机构：%s（含 %d 台资产、%d 条单据、%d 个盘点计划），已先自动备份" %
                (org["name"], n_assets, n_records, n_st))
            return self.send_json({"ok": True})

        # ---- 跨机构转移（仅管理员）：默认资产留在原机构仅换领用人；migrate=true 时资产一并转入目标机构（归属变更） ----
        if path == "/api/assets/cross-transfer":
            if user["role"] != "admin":
                return self.send_json({"ok": False, "error": "仅系统管理员可执行跨机构转移"}, 403)
            from_org = str(data.get("fromOrg") or "").strip()
            to_org = str(data.get("toOrg") or "").strip()
            asset_id = str(data.get("assetId") or "").strip()
            new_user = str(data.get("user") or "").strip()
            new_dept = str(data.get("dept") or "").strip()
            new_loc = str(data.get("loc") or "").strip()
            migrate = bool(data.get("migrate"))
            if not from_org.isdigit() or not to_org.isdigit() or not asset_id or not new_user:
                return self.send_json({"ok": False, "error": "参数不完整"}, 400)
            if from_org == to_org:
                return self.send_json({"ok": False, "error": "目标机构不能与当前机构相同"}, 400)
            with conn() as c:
                fo = c.execute("SELECT id,name FROM orgs WHERE id=?", (int(from_org),)).fetchone()
                to = c.execute("SELECT id,name FROM orgs WHERE id=?", (int(to_org),)).fetchone()
                urow = c.execute("SELECT dept FROM users WHERE org=? AND display_name=?",
                                 (int(to_org), new_user)).fetchone()
            if not fo or not to:
                return self.send_json({"ok": False, "error": "机构不存在"}, 404)
            if not urow:
                return self.send_json({"ok": False, "error": "新领用人须为「%s」的注册用户" % to["name"]}, 400)
            new_dept = new_dept or (urow["dept"] or "")
            with conn() as c:
                d_from, v_from = load_org_db(c, from_org)
            if d_from is None:
                return self.send_json({"ok": False, "error": "原机构数据缺失"}, 500)
            x = next((a for a in d_from.get("assets", []) if a.get("id") == asset_id), None)
            if not x:
                return self.send_json({"ok": False, "error": "资产不存在或已被删除"}, 404)
            if x.get("status") != "inuse":
                return self.send_json({"ok": False, "error": "仅「在用」状态资产可转移"}, 400)
            old = "%s（%s）" % (x.get("user") or "—", x.get("userDept") or "—")
            new_loc = new_loc or (x.get("location") or "")
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            today = datetime.now().strftime("%Y-%m-%d")
            # 转移单据号：沿用前端格式 ZY-YYYYMMDD-NNNN，流水取自原机构 seq
            seq = d_from.get("seq") or {}
            n_zy = int(seq.get("ZY") or 1)
            zy_no = "ZY-%s-%04d" % (today.replace("-", ""), n_zy)
            seq["ZY"] = n_zy + 1
            d_from["seq"] = seq
            if migrate:
                hist_desc = ("由「%s」转移至「%s（%s），%s」；资产一并转入「%s」，退回时在「%s」退回；"
                             "新地点：%s；二维码扫码信息已自动更新；编号 %s（跨机构）") % (
                    old, new_user, new_dept, to["name"], to["name"], to["name"], new_loc or "—", zy_no)
            else:
                hist_desc = ("由「%s」转移至「%s（%s），%s」；跨机构转移：资产仍归 %s 所有，退回时退回 %s；"
                             "新地点：%s；二维码扫码信息已自动更新；编号 %s") % (
                    old, new_user, new_dept, to["name"], fo["name"], fo["name"], new_loc or "—", zy_no)
            x["user"] = new_user
            x["userDept"] = new_dept
            x["useDept"] = new_dept
            x["location"] = new_loc
            x.setdefault("history", []).insert(0, {"t": now, "act": "部门转移", "desc": hist_desc,
                                                   "by": user["displayName"]})
            rec = {"id": "r%s" % secrets.token_hex(8), "no": zy_no, "type": "转移", "date": today,
                   "by": user["displayName"], "dept": "→%s（%s）" % (new_dept, to["name"]),
                   "detail": "%s（%s） → %s·%s" % (x.get("name"), x.get("code"), to["name"], new_user),
                   "status": "已完成"}
            d_from.setdefault("records", []).insert(0, rec)
            if migrate:
                d_from["assets"] = [a for a in d_from["assets"] if a.get("id") != asset_id]
                with conn() as c:
                    d_to, v_to = load_org_db(c, to_org)
                if d_to is None:
                    return self.send_json({"ok": False, "error": "目标机构数据缺失"}, 500)
                x2 = dict(x)
                x2["company"] = to["name"]  # 归属机构变更为目标机构
                seq_to = d_to.get("seq") or {}
                n_zy_to = int(seq_to.get("ZY") or 1)
                zy_to = "ZY-%s-%04d" % (today.replace("-", ""), n_zy_to)
                seq_to["ZY"] = n_zy_to + 1
                d_to["seq"] = seq_to
                d_to.setdefault("assets", []).append(x2)
                d_to.setdefault("records", []).insert(0, dict(rec, id="r%s" % secrets.token_hex(8), no=zy_to,
                                                              dept="自「%s」转入" % fo["name"]))
                backup_db("跨机构转移（含资产迁移）：%s 从「%s」转入「%s」" % (x.get("code"), fo["name"], to["name"]))
                with conn() as c:
                    c.execute("UPDATE kv SET v=?, version=version+1 WHERE k=?",
                              (json.dumps(d_to, ensure_ascii=False), org_db_key(to_org)))
                    c.execute("UPDATE kv SET v=?, version=version+1 WHERE k=?",
                              (json.dumps(d_from, ensure_ascii=False), org_db_key(from_org)))
            else:
                backup_db("跨机构转移：%s 由 %s 转给「%s·%s」" % (x.get("code"), old, to["name"], new_user))
                with conn() as c:
                    c.execute("UPDATE kv SET v=?, version=version+1 WHERE k=?",
                              (json.dumps(d_from, ensure_ascii=False), org_db_key(from_org)))
            log("%s 跨机构转移：%s（%s）由 %s 转给「%s·%s」%s" % (
                user["displayName"], x.get("name"), x.get("code"), old, to["name"], new_user,
                "（资产一并迁移）" if migrate else "（资产留在原机构）"))
            return self.send_json({"ok": True, "migrate": migrate, "docNo": zy_no})

        # ---- Excel 解析（.xlsx）：供「Excel批量导入入库」「Excel回填导出」使用；仅解析返回行列数据，不改库 ----
        if path == "/api/excel-parse":
            b64 = str(data.get("b64") or "")
            if not b64:
                return self.send_json({"ok": False, "error": "未收到文件内容"}, 400)
            try:
                raw = base64.b64decode(b64)
            except Exception:
                return self.send_json({"ok": False, "error": "文件内容编码无效"}, 400)
            if len(raw) > 20 * 1024 * 1024:
                return self.send_json({"ok": False, "error": "文件过大（超过 20MB）"}, 400)
            if raw[:2] == b"\xd0\xcf":  # OLE2 头 = .xls 老格式
                return self.send_json({"ok": False,
                    "error": "暂不支持 .xls 老格式：请用 Excel 打开后「另存为」.xlsx 再导入"}, 400)
            try:
                sheets = parse_xlsx_bytes(raw)
            except zipfile.BadZipFile:
                return self.send_json({"ok": False, "error": "文件不是有效的 .xlsx（请用 Excel 另存为 .xlsx 后重试）"}, 400)
            except Exception as e:
                return self.send_json({"ok": False, "error": "解析失败：%s" % e}, 400)
            log("%s 解析 Excel：%s" % (user["displayName"],
                "、".join("%s(%d行)" % (s["name"], len(s["rows"])) for s in sheets[:5]) or "空文件"))
            return self.send_json({"ok": True, "sheets": sheets})

        # ---- 清空系统全部数据（仅管理员；除内置 admin 外的机构/用户/数据全部清除，重建空白机构） ----
        if path == "/api/wipe-all":
            if user["role"] != "admin":
                return self.send_json({"ok": False, "error": "仅系统管理员可执行此操作"}, 403)
            if str(data.get("confirm") or "") != "WIPE":
                return self.send_json({"ok": False, "error": "参数不正确"}, 400)
            with conn() as c:
                n_orgs = c.execute("SELECT COUNT(*) n FROM orgs").fetchone()["n"]
                n_users = c.execute("SELECT COUNT(*) n FROM users WHERE username<>'admin'").fetchone()["n"]
                n_admins = c.execute("SELECT COUNT(*) n FROM users WHERE username<>'admin' AND role='admin'").fetchone()["n"]
            backup_db("清空全部数据")  # 事务提交前备份，保留清空前完整状态
            with conn() as c:
                c.execute("DELETE FROM kv WHERE k LIKE 'db:%'")   # 所有机构的数据
                c.execute("DELETE FROM orgs")                     # 所有机构
                c.execute("DELETE FROM users WHERE username<>'admin'")   # 除内置管理员外全部用户
                c.execute("DELETE FROM sessions WHERE username<>'admin'")  # 这些用户的登录会话
                new_id = create_org(c, "白云")                    # 重建一个空白机构，保持系统立即可用
                init_org_db(c, new_id)
            log("系统数据已全部清空：删除 %d 个机构、%d 个用户（含 %d 个管理员账号），已自动备份并重建空白机构「白云」" %
                (n_orgs, n_users, n_admins))
            return self.send_json({"ok": True, "orgs": n_orgs, "users": n_users})

        # ---- 部门重命名：批量同步用户账号的部门字段（仅管理员；配合前端部门改名） ----
        if path == "/api/users/rename-dept":
            if user["role"] != "admin":
                return self.send_json({"ok": False, "error": "仅系统管理员可执行此操作"}, 403)
            old_dept = str(data.get("oldDept") or "").strip()
            new_dept = str(data.get("newDept") or "").strip()
            if not old_dept or not new_dept:
                return self.send_json({"ok": False, "error": "参数不正确"}, 400)
            if len(new_dept) > 20:
                return self.send_json({"ok": False, "error": "部门名称过长（最多 20 字）"}, 400)
            if old_dept == new_dept:
                return self.send_json({"ok": True, "updated": 0})
            with conn() as c:
                n = c.execute("UPDATE users SET dept=? WHERE dept=?",
                              (new_dept, old_dept)).rowcount
            log("部门重命名：%s -> %s（同步 %d 个用户账号）" % (old_dept, new_dept, n))
            return self.send_json({"ok": True, "updated": n})

        # ---- 用户管理（仅管理员） ----
        if path.startswith("/api/users"):
            if user["role"] != "admin":
                return self.send_json({"ok": False, "error": "仅系统管理员可管理用户"}, 403)

            if path == "/api/users" and self.command == "POST" or path == "/api/users/add":
                username = str(data.get("username") or "").strip()
                display_name = str(data.get("displayName") or "").strip() or username
                role = data.get("role")
                password = str(data.get("password") or "")
                dept = str(data.get("dept") or "").strip()
                org = str(data.get("org") or "").strip()
                if not username or role not in ROLE_NAME or len(password) < 6:
                    return self.send_json({"ok": False,
                                           "error": "参数不完整：用户名必填，密码至少 6 位，角色须为 admin/manager/viewer"}, 400)
                if role != "admin" and not org:
                    return self.send_json({"ok": False, "error": "普通用户必须分配一个机构"}, 400)
                with conn() as c:
                    if org:
                        if not org.isdigit() or not c.execute("SELECT 1 FROM orgs WHERE id=?", (int(org),)).fetchone():
                            return self.send_json({"ok": False, "error": "机构不存在，请刷新后重选"}, 400)
                    if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                        return self.send_json({"ok": False, "error": "用户名已存在"}, 400)
                    add_user_row(c, username, display_name, role, password, dept, org)
                log("新增用户：%s（%s）%s%s" % (username, ROLE_NAME[role], ("· " + dept) if dept else "",
                    ("· 机构「%s」" % org) if org else "· 不限机构"))
                return self.send_json({"ok": True})

            if path == "/api/users/list":
                with conn() as c:
                    rows = c.execute("""SELECT u.username,u.display_name,u.role,u.created_at,u.dept,
                                               u.org, o.name AS org_name
                                        FROM users u LEFT JOIN orgs o ON o.id=CAST(u.org AS INTEGER)
                                        ORDER BY u.created_at""").fetchall()
                users = []
                for r in rows:
                    u = dict(r)
                    u["org"] = str(u["org"] or "")
                    users.append(u)
                return self.send_json({"ok": True, "users": users})

            if path == "/api/users/update":
                username = str(data.get("username") or "").strip()
                new_username = str(data.get("newUsername") or "").strip()
                with conn() as c:
                    row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
                    if not row:
                        return self.send_json({"ok": False, "error": "用户不存在"}, 404)
                    display_name = str(data.get("displayName") or row["display_name"]).strip()
                    role = data.get("role") or row["role"]
                    dept = str(data.get("dept") if data.get("dept") is not None else (row["dept"] or "")).strip()
                    org = str(data.get("org") if data.get("org") is not None else (row["org"] or "")).strip()
                    if role not in ROLE_NAME:
                        return self.send_json({"ok": False, "error": "角色不正确"}, 400)
                    if username == "admin" and role != "admin":
                        return self.send_json({"ok": False, "error": "不能降级内置管理员账号"}, 400)
                    if role != "admin" and not org:
                        return self.send_json({"ok": False, "error": "普通用户必须分配一个机构"}, 400)
                    if username == "admin" and org:
                        return self.send_json({"ok": False, "error": "内置管理员账号保持不限机构（可看全部机构总览）"}, 400)
                    if org:
                        if not org.isdigit() or not c.execute("SELECT 1 FROM orgs WHERE id=?", (int(org),)).fetchone():
                            return self.send_json({"ok": False, "error": "机构不存在，请刷新后重选"}, 400)
                    # 用户名修改：内置 admin 不允许改；新名仅限英文/数字且不能与他人冲突；已登录会话同步保持有效
                    if new_username and new_username != username:
                        if username == "admin":
                            return self.send_json({"ok": False, "error": "内置管理员账号用户名不可修改"}, 400)
                        if not re.match(r"^[A-Za-z0-9_\-]{2,32}$", new_username):
                            return self.send_json({"ok": False, "error": "用户名仅限英文 / 数字（2-32 位），不支持中文"}, 400)
                        if c.execute("SELECT 1 FROM users WHERE username=?", (new_username,)).fetchone():
                            return self.send_json({"ok": False, "error": "该用户名已被使用，请换一个"}, 400)
                        c.execute("UPDATE users SET username=? WHERE username=?", (new_username, username))
                        c.execute("UPDATE sessions SET username=? WHERE username=?", (new_username, username))
                        log("用户改名：%s -> %s" % (username, new_username))
                        username = new_username
                    c.execute("UPDATE users SET display_name=?,role=?,dept=?,org=? WHERE username=?",
                              (display_name, role, dept, org, username))
                    password = str(data.get("password") or "")
                    if password:
                        if len(password) < 6:
                            return self.send_json({"ok": False, "error": "密码至少 6 位"}, 400)
                        salt = secrets.token_hex(16)
                        c.execute("UPDATE users SET salt=?,pwd=? WHERE username=?",
                                  (salt, hash_pwd(password, salt), username))
                log("更新用户：%s" % username)
                return self.send_json({"ok": True})

            if path == "/api/users/delete":
                username = str(data.get("username") or "").strip()
                if username == user["username"]:
                    return self.send_json({"ok": False, "error": "不能删除当前登录账号"}, 400)
                with conn() as c:
                    admins = c.execute("SELECT COUNT(*) n FROM users WHERE role='admin'").fetchone()["n"]
                    target = c.execute("SELECT role,display_name FROM users WHERE username=?", (username,)).fetchone()
                    if not target:
                        return self.send_json({"ok": False, "error": "用户不存在"}, 404)
                    if target["role"] == "admin" and admins <= 1:
                        return self.send_json({"ok": False, "error": "系统至少需要保留一个管理员"}, 400)
                    # 名下有在用资产的用户不允许删除（资产按领用人姓名关联，需遍历全部机构），需先转移或退回
                    cnt = 0
                    for org in list_orgs(c):
                        d, _v = load_org_db(c, org["id"])
                        if d is None:
                            continue
                        cnt += sum(1 for a in d.get("assets", [])
                                   if a.get("user") == target["display_name"])
                    if cnt:
                        return self.send_json({"ok": False,
                                               "error": "该用户名下有 %d 台资产，请先转移或退回后再删除" % cnt}, 400)
                    c.execute("DELETE FROM users WHERE username=?", (username,))
                    c.execute("DELETE FROM sessions WHERE username=?", (username,))
                log("删除用户：%s" % username)
                return self.send_json({"ok": True})

            return self.send_json({"ok": False, "error": "not found"}, 404)

        return self.send_json({"ok": False, "error": "not found"}, 404)


# ---------- 用户列表合并到 GET（此处单独支持 GET /api/users/list） ----------
def api_users_list(self):
    user = self.current_user()
    if not user:
        return self.send_json({"ok": False, "error": "未登录"}, 401)
    if user["role"] != "admin":
        return self.send_json({"ok": False, "error": "仅系统管理员可管理用户"}, 403)
    with conn() as c:
        rows = c.execute("""SELECT u.username,u.display_name,u.role,u.created_at,u.dept,
                                   u.org, o.name AS org_name
                            FROM users u LEFT JOIN orgs o ON o.id=CAST(u.org AS INTEGER)
                            ORDER BY u.created_at""").fetchall()
    users = []
    for r in rows:
        u = dict(r)
        u["org"] = str(u["org"] or "")
        users.append(u)
    return self.send_json({"ok": True, "users": users})


# ---------- 微信扫码信息页（/s.html?c=资产编号，免登录只读） ----------
SCAN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>资产信息查询</title>
<style>
:root{--pri:#2a78d6;--ink:#1f2937;--gray:#6b7280;--line:#e5e7eb}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;background:#eef2f7;color:var(--ink);font-size:15px;line-height:1.7}
.wrap{max-width:480px;margin:0 auto;padding:18px 14px 40px}
.card{background:#fff;border-radius:14px;padding:22px 20px;margin-bottom:14px;box-shadow:0 4px 18px -8px rgba(16,24,40,.2)}
.hd{text-align:center;padding-bottom:6px}
.hd h1{font-size:20px;margin:10px 0 2px}
.hd .cd{font-size:13px;color:var(--gray);letter-spacing:.04em}
.bd{display:inline-block;font-size:12px;font-weight:700;border-radius:99px;padding:4px 14px;color:#fff}
.bd.inuse{background:#0ca30c}.bd.idle{background:#6b7280}.bd.pend{background:#e8930c}.bd.back{background:#d03b3b}
.lead{background:#eaf2fc;border-radius:10px;padding:12px 14px;margin:14px 0}
.li{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-bottom:1px dashed #e5e7eb}
.lead .li{border-bottom:1px dashed #c8dcf4}
.li:last-child{border-bottom:none}
.li span{color:var(--gray);flex:none}
.li b{font-weight:600;text-align:right;word-break:break-all}
.grid{margin-top:4px}
.sec{font-size:14px;margin:18px 0 8px;color:var(--gray)}
.tl{list-style:none}
.tl li{display:flex;gap:10px;padding:8px 0;border-bottom:1px dashed #e5e7eb}
.tl li:last-child{border-bottom:none}
.tl .t{color:var(--gray);font-size:12px;flex:none;width:92px;padding-top:2px}
.tl b{font-size:13.5px}
.tl .d{font-size:12.5px;color:var(--gray)}
.err{text-align:center;background:#fff;border-radius:14px;padding:40px 20px;color:#d03b3b;font-weight:600;box-shadow:0 4px 18px -8px rgba(16,24,40,.2)}
.ld{text-align:center;padding:60px 0;color:var(--gray)}
.ft{text-align:center;color:#9aa1ab;font-size:11.5px;margin-top:10px}
</style>
</head>
<body>
<div class="wrap"><div id="box"><div class="ld">正在加载…</div></div>
<div class="ft">固定资产管理系统 · 扫码查询</div></div>
<script>
var code=new URLSearchParams(location.search).get('c')||'';
var ST={idle:'闲置',inuse:'在用',pend:'待发放',back:'退回未进库'};
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(m){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]})}
var box=document.getElementById('box');
if(!code){box.innerHTML='<div class="err">链接缺少资产编号</div>'}
else fetch('/api/scan?c='+encodeURIComponent(code)).then(function(r){return r.json()}).then(function(j){
  if(!j.ok){box.innerHTML='<div class="err">'+(j.error==='not found'?'未找到该资产，可能编号有误或资产已删除':'查询失败，请稍后再试')+'</div>';return}
  var a=j.asset;
  var h='<div class="card"><div class="hd"><span class="bd '+esc(a.status)+'">'+esc(a.statusName||ST[a.status]||a.status)+'</span>'
     +'<h1>'+esc(a.name)+'</h1><p class="cd">'+esc(a.code)+'</p></div>';
  if(a.status==='inuse'){
    h+='<div class="lead"><div class="li"><span>领用人</span><b>'+esc(a.user||'—')+'</b></div>'
      +'<div class="li"><span>领用部门</span><b>'+esc(a.userDept||'—')+'</b></div>'
      +'<div class="li"><span>领取时间</span><b>'+esc(a.receiveDate||'—')+'</b></div>'
      +'<div class="li"><span>存放地点</span><b>'+esc(a.location||'—')+'</b></div></div>';
  }else{
     h+='<div class="lead"><div class="li"><span>当前状态</span><b>'+esc(a.statusName||'')+(a.status==='pend'?'（已审批通过，待管理员发放）':a.status==='back'?'（退回未进库，待点收入库）':a.status==='idle'?'（闲置在库，可申请领用）':'')+'</b></div>'
      +'<div class="li"><span>存放地点</span><b>'+esc(a.location||'—')+'</b></div></div>';
  }
  var rows=[['资产编号',a.code],['分类规格',(a.cat||'—')+(a.spec?' · '+a.spec:'')],['入库部门',a.inDept||'—'],['所属机构',a.org||'—']];
  h+='<div class="grid">'+rows.map(function(r){return '<div class="li"><span>'+r[0]+'</span><b>'+esc(r[1])+'</b></div>'}).join('')+'</div>';
  h+='</div>';
  box.innerHTML=h;
}).catch(function(){box.innerHTML='<div class="err">无法连接服务器，请确认手机与服务器在同一网络</div>'});
</script>
</body>
</html>
"""


# 让 GET 也支持 /api/users/list
_orig_do_GET = Handler.do_GET


def do_GET_with_users(self):
    path = self.path.split("?")[0]
    if path == "/api/users/list":
        return api_users_list(self)
    return _orig_do_GET(self)


Handler.do_GET = do_GET_with_users


# ---------- 启动 ----------
class AmsHTTPServer(ThreadingHTTPServer):
    """独占绑定端口，禁止双开。
    Windows 上若允许端口共用，两个实例会同时写同一个数据库导致数据错乱
    （本系统曾因此出现过数据丢失，此处从根源上禁止第二个实例绑定成功）。"""
    allow_reuse_address = False

    def server_bind(self):
        try:
            import socket
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except Exception:
            pass
        ThreadingHTTPServer.server_bind(self)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    init_db()
    try:
        srv = AmsHTTPServer((HOST, PORT), Handler)
    except OSError:
        print("启动失败：端口 %d 已被占用。" % PORT)
        print("通常表示资产系统服务器已经在运行 —— 本程序禁止同时开多个实例，")
        print("两个实例同时写数据库会导致数据错乱甚至丢失。请直接使用已运行的实例。")
        input("按回车键退出...")
        return
    backup_db("启动")   # 绑定成功后先做一次完整备份
    ip_lan = get_lan_ip()
    global LAN_BASE
    if ip_lan:
        LAN_BASE = "http://%s:%d" % (ip_lan, PORT)
        log("二维码将使用内网地址：%s" % LAN_BASE)
    print("=" * 56)
    print("  固定资产管理系统 · 网络版服务器已启动")
    print("=" * 56)
    print("  本机访问：    http://127.0.0.1:%d" % PORT)
    if ip_lan:
        print("  内网访问：    http://%s:%d   （供公司其他同事使用）" % (ip_lan, PORT))
    print("  默认管理员：  admin / admin123  （请尽快修改密码）")
    print("  数据文件：    %s" % DB_FILE)
    print("  自动备份：    启动时 + 每日首次保存前，保留最近 %d 份（%s）" % (MAX_BACKUPS, BACKUP_DIR))
    print("  停止服务：    在本窗口按 Ctrl+C")
    print("=" * 56)
    try:
        threading.Timer(1.0, lambda: _open_browser(PORT)).start()
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止。")
        srv.server_close()


def _open_browser(port):
    try:
        import webbrowser
        webbrowser.open("http://127.0.0.1:%d" % port)
    except Exception:
        pass


def get_lan_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


if __name__ == "__main__":
    main()
