#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
应急工具：把管理员 admin 的密码重置为 admin123
用法：把本文件与 ams_data.db 放在同一目录，执行  python 重置管理员密码.py
"""
import sqlite3, secrets, hashlib, os, sys

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ams_data.db")

def main():
    if not os.path.exists(DB):
        print("未找到数据库文件 ams_data.db，请把本文件放在服务器同一目录后再试。")
        input("按回车键退出...")
        return
    salt = secrets.token_hex(16)
    pwd = hashlib.pbkdf2_hmac("sha256", b"admin123", salt.encode(), 100000).hex()
    with sqlite3.connect(DB) as c:
        c.execute("UPDATE users SET salt=?,pwd=? WHERE username='admin'", (salt, pwd))
        n = c.execute("SELECT changes()").fetchone()[0]
    if n:
        print("完成！管理员 admin 的密码已重置为 admin123，请登录后尽快修改。")
    else:
        print("未找到内置管理员账号 admin。")
    input("按回车键退出...")

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
