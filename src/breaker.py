# -*- coding: utf-8 -*-
"""
熔断器（三态机）：CLOSED → OPEN → HALF_OPEN → CLOSED
借鉴 EchoMind 的 CircuitBreaker：连续失败达到阈值后打开熔断直接走降级，
避免故障期间继续把流量打进已经出问题的依赖（LLM/数据库/外部接口）。
"""
import threading
import time


class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF_OPEN"

    def __init__(self, name: str, failure_threshold: int = 5,
                 recovery_seconds: int = 60):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._state = self.CLOSED
        self._consecutive_fails = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == self.OPEN and \
                    time.time() - self._opened_at >= self.recovery_seconds:
                self._state = self.HALF_OPEN  # 冷却期满 → 放行一次探测
            return self._state

    def allow(self) -> bool:
        return self.state in (self.CLOSED, self.HALF_OPEN)

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_fails = 0
            self._state = self.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_fails += 1
            if self._state == self.HALF_OPEN or \
                    self._consecutive_fails >= self.failure_threshold:
                self._state = self.OPEN
                self._opened_at = time.time()

    def snapshot(self) -> dict:
        return {"name": self.name, "state": self.state,
                "consecutive_fails": self._consecutive_fails,
                "failure_threshold": self.failure_threshold}
