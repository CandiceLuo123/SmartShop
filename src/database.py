# -*- coding: utf-8 -*-
"""
统一数据访问层：业务数据（商品/订单/明细/顾客）支持 MySQL 与 SQLite 双后端。
  - backend 由 config.json database.backend 决定（sqlite / mysql）
  - MySQL 用 pymysql（需 pip install pymysql），SQLite 用标准库
  - 线程局部连接（每个线程复用一个连接），Agent/Web 均为多线程场景
  - 平台数据（users/chat_sessions/tickets 等）仍存 SQLite，与业务库分离：
    业务库可能换供应商/重灌数据，平台数据必须稳定。
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path


def _norm(v):
    """MySQL 驱动返回的 Decimal/datetime 转为 JSON 可序列化类型。"""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return str(v)
    return v

ROOT = Path(__file__).resolve().parents[1]
_STATE = threading.local()
_LOCK = threading.Lock()
_mysql_pool: list = []
_mysql_cfg: dict | None = None


def backend_name(cfg: dict) -> str:
    return cfg.get("database", {}).get("backend", "sqlite")


def _mysql_connect(cfg: dict):
    import pymysql
    mcfg = cfg["database"]["mysql"]
    password = os_environ_get(mcfg.get("password_env", "MYSQL_PASSWORD"))
    if not password:
        raise RuntimeError(
            "MySQL 未配置密码：请在项目根目录 .env 中添加 "
            f"{mcfg.get('password_env', 'MYSQL_PASSWORD')}=你的root密码")
    return pymysql.connect(
        host=mcfg["host"], port=int(mcfg["port"]), user=mcfg["user"],
        password=password, database=mcfg["database"], charset=mcfg.get("charset", "utf8mb4"),
        autocommit=False, cursorclass=pymysql.cursors.Cursor)


def os_environ_get(name: str) -> str | None:
    from src.llm import load_env_file
    load_env_file(ROOT / ".env")
    import os
    value = os.environ.get(name, "").strip()
    return value or None


@contextmanager
def business_conn(cfg: dict):
    """业务数据连接上下文：MySQL 连接池取用 / SQLite 每线程一连接。"""
    if backend_name(cfg) == "mysql":
        global _mysql_cfg
        with _LOCK:
            if _mysql_cfg is None:
                _mysql_cfg = cfg
            conn = _mysql_pool.pop() if _mysql_pool else _mysql_connect(cfg)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            with _LOCK:
                if len(_mysql_pool) < cfg["database"]["mysql"].get("pool_size", 8):
                    _mysql_pool.append(conn)
                else:
                    conn.close()
    else:
        conn = getattr(_STATE, "sqlite_conn", None)
        if conn is None:
            conn = sqlite3.connect(ROOT / cfg["paths"]["db"], timeout=30)
            conn.row_factory = sqlite3.Row
            _STATE.sqlite_conn = conn
        conn.row_factory = sqlite3.Row
        # 业务量级数据在独立库 shop.db-big（generate_big_data.py 生成），自动 ATTACH
        if not getattr(_STATE, "big_attached", False):
            big = ROOT / "data" / "shop.db-big"
            if big.exists():
                try:
                    conn.execute("ATTACH DATABASE ? AS big", (str(big),))
                    _STATE.big_attached = True
                except sqlite3.OperationalError:
                    _STATE.big_attached = True  # 已挂或失败均不再重试
            else:
                _STATE.big_attached = True
        try:
            yield conn
        finally:
            pass  # SQLite 连接线程内复用，不关闭


def btable(cfg: dict, name: str) -> str:
    """业务表名：SQLite 后端优先 big. 前缀（10 万行业务库），MySQL 后端原样。"""
    if backend_name(cfg) == "mysql":
        return name
    return "big." + name if (ROOT / "data" / "shop.db-big").exists() else name


def q(conn, sql: str, params=()) -> list[dict]:
    """执行查询返回 dict 列表（SQLite/pymysql 统一走 cursor，值统一规范化）。"""
    cur = conn.cursor()
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, (_norm(v) for v in row))) for row in cur.fetchall()]


def one(conn, sql: str, params=()) -> dict | None:
    rows = q(conn, sql, params)
    return rows[0] if rows else None


def ph(cfg: dict) -> str:
    """当前后端的参数占位符（MySQL=%s，SQLite=?）。"""
    return "%s" if backend_name(cfg) == "mysql" else "?"
