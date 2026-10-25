# -*- coding: utf-8 -*-
"""
Web 数据表（建在 data/shop.db 中，与业务表同库便于商家端联查）：
  users          平台账号（顾客/管理员）
  chat_sessions  聊天会话（含完整 Agent messages 快照，用于恢复上下文）
  chat_messages  聊天消息（顾客端历史 + 商家端审查视图）
首次启动自动建表并写入演示账号。
"""
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "shop.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'customer',   -- customer / admin
    customer_id   TEXT,                               -- 关联 customers 表
    display_name  TEXT,
    created_at    TEXT
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id            TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL,
    title         TEXT DEFAULT '新对话',
    messages_json TEXT DEFAULT '[]',   -- AgentSession.messages 完整快照
    created_at    TEXT,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,          -- user / assistant
    content     TEXT,
    tools       TEXT DEFAULT '[]',      -- 该轮调用的工具名列表
    trace_json  TEXT DEFAULT '[]',      -- 工具轨迹（商家端可查）
    created_at  TEXT
);
"""

DEMO_USERS = [
    # username, password, role, customer_id, display_name
    ("admin", "admin123", "admin", None, "店铺管理员"),
    ("c001", "demo1234", "customer", "C001", "张伟"),
    ("c003", "demo1234", "customer", "C003", "王芳"),
    ("c002", "demo1234", "customer", "C002", "李娜"),
]


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def next_customer_id(conn: sqlite3.Connection) -> str:
    """为新注册用户生成顾客ID（U 前缀，避开演示数据 C 前缀）。"""
    n = conn.execute(
        "SELECT COUNT(*) FROM customers WHERE customer_id LIKE 'U%'").fetchone()[0]
    return f"U{n + 1:04d}"


def ensure_schema() -> None:
    """建表 + 首次种子账号（幂等）。"""
    with closing(connect()) as conn:
        conn.executescript(SCHEMA)
        existing = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if existing == 0:
            from web.auth import hash_password, new_salt
            for username, password, role, cid, name in DEMO_USERS:
                salt = new_salt()
                conn.execute(
                    "INSERT INTO users (username, password_hash, salt, role, "
                    "customer_id, display_name, created_at) VALUES (?,?,?,?,?,?,?)",
                    (username, hash_password(password, salt), salt, role,
                     cid, name, now_str()))
        conn.commit()
