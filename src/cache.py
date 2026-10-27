# -*- coding: utf-8 -*-
"""
缓存管理器：防穿透 / 防击穿 / 防雪崩（借鉴 EchoMind 的 MCPToolManager.call 缓存层）。

  穿透（查询根本不存在的数据，缓存永远不命中，每次都打到数据库）
    → 空值缓存：loader 返回 None/空 时写入"空值标记"，用短 TTL；
      另加 validate_query 拒绝恶意畸形查询。

  击穿（热点 key 过期瞬间，大量并发同时回源）
    → per-key 互斥锁：同一 key 只允许一个线程执行回源函数，其余线程
      等锁后 double-check 缓存直接拿结果。

  雪崩（大量 key 设置相同 TTL，同时集体过期）
    → TTL 抖动：写入时 ttl = base + random(0, jitter)，把过期时间打散。

纯内存实现（进程级），TTL 到期惰性删除 + 超量全清。
"""
import hashlib
import random
import re
import threading
import time

# 空值标记（防穿透哨兵）
NULL_SENTINEL = "__NULL_RESULT__"
ILLEGAL_QUERY = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]|(--\s)|(/\*)|(;\s*drop\b)|(;\s*delete\b)", re.I)


class CacheManager:
    def __init__(self, cfg: dict):
        cc = cfg.get("cache", {})
        self.ttl_base = cc.get("ttl_base_seconds", 300)
        self.ttl_jitter = cc.get("ttl_jitter_seconds", 120)
        self.null_ttl = cc.get("null_ttl_seconds", 60)
        self.max_items = cc.get("max_items", 10000)
        self._store: dict[str, tuple[float, object, bool]] = {}  # key -> (expires, value, is_null)
        self._key_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._global_lock = threading.Lock()
        self.stats = {"hits": 0, "misses": 0, "null_cached": 0, "evictions": 0}

    # ---------------- 基础读写 ----------------
    def get(self, key: str):
        with self._global_lock:
            item = self._store.get(key)
        if item is None:
            with self._global_lock:
                self.stats["misses"] += 1
            return False, None
        expires, value, is_null = item
        if time.time() > expires:
            with self._global_lock:
                self._store.pop(key, None)
            return False, None
        with self._global_lock:
            self.stats["hits"] += 1
        if is_null:
            return True, None  # 空值缓存命中：告诉调用方"确实没有"
        return True, value

    def set(self, key: str, value, ttl: int | None = None,
            is_null: bool | None = None) -> None:
        """写入。is_null=True 走短 TTL 空值缓存（防穿透，value 保留完整提示供返回）。
        其余 TTL = base + jitter（防雪崩）。"""
        if is_null is None:
            is_null = value is None or value == [] or value == {}
        if is_null:
            ttl = self.null_ttl + random.randint(0, 10)
            with self._global_lock:
                self.stats["null_cached"] += 1
        else:
            ttl = (ttl or self.ttl_base) + random.randint(0, self.ttl_jitter)
        with self._global_lock:
            if len(self._store) >= self.max_items and key not in self._store:
                self._store.clear()  # 简易全清淘汰
                self.stats["evictions"] += 1
            self._store[key] = (time.time() + ttl, value, is_null)

    # ---------------- 回源（防击穿核心） ----------------
    def get_or_load(self, key: str, loader, ttl: int | None = None):
        hit, value = self.get(key)
        if hit:
            return value
        lock = self._key_lock(key)
        with lock:
            hit, value = self.get(key)  # double-check：等锁期间可能已被别的线程写入
            if hit:
                return value
            value = loader()
            self.set(key, value, ttl)
            return value

    def _key_lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            if key not in self._key_locks:
                self._key_locks[key] = threading.Lock()
            return self._key_locks[key]

    # ---------------- 防穿透第二道：查询合法性校验 ----------------
    @staticmethod
    def validate_query(text: str, max_len: int = 200) -> str | None:
        """非法查询返回错误说明，合法返回 None。"""
        if not text or not str(text).strip():
            return "查询不能为空"
        s = str(text)
        if len(s) > max_len:
            return f"查询过长（>{max_len} 字符）"
        if ILLEGAL_QUERY.search(s):
            return "查询包含非法字符"
        return None

    @staticmethod
    def cache_key(*parts) -> str:
        return hashlib.md5(
            "|".join(str(p) for p in parts).encode("utf-8")).hexdigest()

    def summary(self) -> dict:
        with self._global_lock:
            total = self.stats["hits"] + self.stats["misses"]
            return {**self.stats, "entries": len(self._store),
                    "hit_rate": round(self.stats["hits"] / total, 3) if total else 0.0}
