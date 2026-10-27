# -*- coding: utf-8 -*-
"""
性能监控与路由降权（借鉴 EchoMind 4.7 Monitor 与降权）。

  采集（无需埋点，直接读 STATS 实时累积的计数）→ 阈值告警 →
  计算降权系数写回 PENALTIES → 路由时绕开被降权的专家。

降权路由：AgentSession 每轮意图分类后查 penalty(specialist)，
  penalty ≥ 0.8 时自动 fallback 到 general 兜底专家并记录告警，
  避免"分类正确但该专家此刻持续失败"的流量继续损失。
"""
import threading
import time
from datetime import datetime


class Stats:
    """全局单例：Agent/工具调用实时计数（供 Monitor 周期读取）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.agents: dict[str, dict] = {}
        self.tools: dict[str, dict] = {}

    @staticmethod
    def _bucket(d: dict, key: str) -> dict:
        return d.setdefault(key, {"total": 0, "success": 0, "failed": 0,
                                  "total_ms": 0, "consecutive_fails": 0})

    def record_agent(self, specialist: str, ok: bool, elapsed_ms: int) -> None:
        with self._lock:
            b = self._bucket(self.agents, specialist)
            b["total"] += 1
            b["total_ms"] += elapsed_ms
            if ok:
                b["success"] += 1
                b["consecutive_fails"] = 0
            else:
                b["failed"] += 1
                b["consecutive_fails"] += 1

    def record_tool(self, tool: str, ok: bool, elapsed_ms: int) -> None:
        with self._lock:
            b = self._bucket(self.tools, tool)
            b["total"] += 1
            b["total_ms"] += elapsed_ms
            if ok:
                b["success"] += 1
                b["consecutive_fails"] = 0
            else:
                b["failed"] += 1
                b["consecutive_fails"] += 1

    def summary(self) -> tuple[dict, dict]:
        with self._lock:
            def derive(d: dict) -> dict:
                out = {}
                for k, b in d.items():
                    total = b["total"] or 1
                    out[k] = {**b,
                              "success_rate": round(b["success"] / total, 3),
                              "avg_ms": round(b["total_ms"] / total)}
                return out
            return derive(self.agents), derive(self.tools)


STATS = Stats()
PENALTIES: dict[str, float] = {}
ALERTS: list[dict] = []   # 最近告警（线程安全由 GIL + append 保证，容量截断）

THRESHOLDS = {  # (指标, 阈值, 级别)
    "agent_success_rate": (0.90, "ERROR"),
    "agent_avg_ms": (3000, "WARNING"),
    "tool_success_rate": (0.95, "WARNING"),
    "tool_avg_ms": (5000, "ERROR"),
}


def _alert(level: str, title: str, detail: str) -> None:
    ALERTS.append({"level": level, "title": title, "detail": detail,
                   "time": datetime.now().strftime("%H:%M:%S")})
    del ALERTS[:-50]  # 只保留最近 50 条


def routing_penalty(success_rate: float, avg_ms: float) -> float:
    """成功率每低于 0.90 一点扣分翻倍（≤0.5）；延迟每超 3000ms 按比例扣（≤0.4）；
    合计封顶 0.9（保留 10% 放行机会，不至于彻底禁用某个专家）。"""
    penalty = 0.0
    if success_rate < 0.90:
        penalty += min(0.5, (0.90 - success_rate) * 2)
    if avg_ms > 3000:
        penalty += min(0.4, (avg_ms - 3000) / 10000)
    return min(penalty, 0.9)


class PerformanceMonitor(threading.Thread):
    """后台周期采集：读 STATS → 告警 → 计算降权写回 PENALTIES。"""

    def __init__(self, interval_s: int = 10):
        super().__init__(daemon=True, name="perf-monitor")
        self.interval_s = interval_s
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            self._collect()
            self._stop.wait(self.interval_s)

    def stop(self) -> None:
        self._stop.set()

    def _collect(self) -> None:
        try:
            agent_stats, tool_stats = STATS.summary()
            penalties = {}
            for name, s in agent_stats.items():
                if s["total"] < 5:   # 样本太少不降权，避免冷启动误伤
                    continue
                sr, ms = s["success_rate"], s["avg_ms"]
                if sr < THRESHOLDS["agent_success_rate"][0]:
                    _alert("ERROR", f"专家 {name} 成功率过低",
                           f"成功率 {sr:.0%}（阈值 90%），连续失败 {s['consecutive_fails']}")
                if ms > THRESHOLDS["agent_avg_ms"][0]:
                    _alert("WARNING", f"专家 {name} 响应变慢",
                           f"平均 {ms}ms（阈值 3000ms）")
                penalties[name] = routing_penalty(sr, ms)
            for name, s in tool_stats.items():
                if s["total"] < 5:
                    continue
                sr, ms = s["success_rate"], s["avg_ms"]
                if sr < THRESHOLDS["tool_success_rate"][0]:
                    _alert("WARNING", f"工具 {name} 成功率下降",
                           f"成功率 {sr:.0%}，连续失败 {s['consecutive_fails']}")
                if ms > THRESHOLDS["tool_avg_ms"][0]:
                    _alert("ERROR", f"工具 {name} 响应超慢", f"平均 {ms}ms")
            PENALTIES.clear()
            PENALTIES.update(penalties)
        except Exception as e:  # noqa: BLE001 —— 监控自身绝不能拖垮主服务
            _alert("ERROR", "Monitor 采集异常", str(e)[:120])


def penalty(specialist: str) -> float:
    return PENALTIES.get(specialist, 0.0)


MONITOR: PerformanceMonitor | None = None


def start_monitor(cfg: dict) -> PerformanceMonitor:
    global MONITOR
    if MONITOR is None:
        MONITOR = PerformanceMonitor(interval_s=10)
        MONITOR.start()
    return MONITOR


def monitor_summary() -> dict:
    agent_stats, tool_stats = STATS.summary()
    return {
        "agent_stats": agent_stats,
        "tool_stats": tool_stats,
        "penalties": dict(PENALTIES),
        "active_alerts": list(reversed(ALERTS))[-10:],
    }
