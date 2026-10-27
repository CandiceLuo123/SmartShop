# -*- coding: utf-8 -*-
"""
Headless 客服 Agent：
  加载资料（系统提示词 + 顾客上下文）→ GLM 工具调用循环（默认全权限，工具免审批）
  → 会话轨迹与结果落盘 JSON。

用法：
  python -m src.agent -m "我的订单SO2610180003到哪了" --customer C003 --trace
  python -m src.agent -i --customer C001          # 交互模式
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from . import memory, monitor, router
from . import skills as skills_mod
from . import tools
from .llm import build_llm
from .tools import execute_tool, get_customer_profile

ROOT = Path(__file__).resolve().parents[1]

FALLBACK_ANSWER = ("非常抱歉，系统刚才开小差了，请您再描述一下问题？"
                   "如需人工服务，回复\"转人工\"，工作日 9:00-21:00 为您接入。")

_SKILL_MANAGER: skills_mod.SkillManager | None = None


def get_skill_manager(cfg: dict) -> skills_mod.SkillManager:
    global _SKILL_MANAGER
    if _SKILL_MANAGER is None:
        _SKILL_MANAGER = skills_mod.SkillManager(
            ROOT / cfg["paths"].get("skills_dir", "skills"))
    return _SKILL_MANAGER


def load_config() -> dict:
    with open(ROOT / "config.json", encoding="utf-8") as f:
        return json.load(f)


def build_system_prompt(cfg: dict, customer_id: str | None,
                        specialist: str = "general",
                        profile: dict | None = None,
                        message: str = "") -> str:
    """系统提示词 = 通用守则 + 专家 persona + Skills（按角色/关键词匹配）+ 画像。"""
    text = (ROOT / cfg["paths"]["system_prompt"]).read_text(encoding="utf-8")
    text = text.replace("{today}", cfg["today"])
    if customer_id:
        block = get_customer_profile(customer_id, cfg)
    else:
        block = "（未识别顾客身份；顾客查询订单时请其提供订单号，勿猜测订单归属）"
    text = text.replace("{customer_block}", block)
    spec = router.SPECIALISTS.get(specialist) or router.SPECIALISTS["general"]
    text += (f"\n\n# 当前场景模式（意图路由结果）\n"
             f"当前由 {spec['emoji']} {spec['label']} 接管本轮回答。\n"
             f"{spec['persona'].strip()}")
    skills_block = get_skill_manager(cfg).prompt_for(message, specialist)
    if skills_block:
        text += "\n\n" + skills_block
    text += memory.render_profile_block(profile or {})
    return text


class AgentSession:
    """一次会话（多轮共享 messages）。run() 返回结构化结果并维护轨迹。

    路由：每轮先意图分类，切换到对应专家（专属 persona + 工具白名单）。
    记忆：L1 messages 全量；L2 超阈值自动压缩成摘要；L3 用户画像由外部注入。
    """

    def __init__(self, cfg: dict, llm, customer_id: str | None = None,
                 profile: dict | None = None):
        self.cfg = cfg
        self.llm = llm
        self.customer_id = customer_id
        self.max_rounds = cfg["agent"]["max_rounds"]
        self.specialist = "general"
        self.profile = profile or {}
        self.profile_turns = 0          # 距上次画像更新的轮数（Web 层读写）
        self.session_summary = ""
        self.messages = [
            {"role": "system", "content": build_system_prompt(
                cfg, customer_id, self.specialist, self.profile)}]

    def _refresh_system(self, message: str = "") -> None:
        self.messages[0] = {"role": "system", "content": build_system_prompt(
            self.cfg, self.customer_id, self.specialist, self.profile, message)}

    def run(self, user_message: str) -> dict:
        """同步接口（CLI/评测用）：消费事件流并返回最终结果。"""
        result = None
        for event in self.run_iter(user_message):
            if event["type"] == "result":
                result = event["result"]
        return result or {"answer": FALLBACK_ANSWER, "status": "error",
                          "error": "未产生结果", "rounds": 0, "tool_calls": [],
                          "trace": [], "model": None, "route": None}

    def run_iter(self, user_message: str):
        """
        事件流接口（Web SSE 用）：逐轮 yield 进度事件，最后 yield result。
        事件类型：route / user_echo / llm_thinking / tool_start / tool_end /
                  compress / generating / answer / result
        """
        # 1) 意图路由 + 专家切换（含监控降权：被降权专家自动 fallback general）
        route = router.classify(self.llm, user_message,
                                context_hint=self.session_summary[:120])
        self.specialist = route["specialist"]
        degraded = False
        if monitor.penalty(self.specialist) >= 0.8 and self.specialist != "general":
            degraded = True
            self.specialist = "general"
        self._refresh_system(user_message)  # 传入消息让 Skills 按关键词匹配
        tools.set_rag_llm(self.llm)
        spec = router.SPECIALISTS[self.specialist]
        yield {"type": "route", "intent": route["intent"],
               "specialist": self.specialist, "label": spec["label"],
               "emoji": spec["emoji"], "via": route["via"],
               "degraded": degraded}

        # 2) 记忆压缩（在追加新消息前处理历史）
        compression = memory.apply_compression(self.llm, self)
        if compression:
            yield {"type": "compress", **compression}

        self.messages.append({"role": "user", "content": user_message})
        yield {"type": "user_echo", "message": user_message}
        trace, tool_names = [], []
        status, answer, error = "ok", None, None
        rounds = 0
        t_run = time.time()
        tools_ = router.tools_for(self.specialist)

        for rnd in range(1, self.max_rounds + 1):
            rounds = rnd
            yield {"type": "llm_thinking", "round": rnd}
            try:
                resp = self.llm.complete(self.messages, tools_)
            except Exception as e:  # noqa: BLE001 —— headless 运行，失败也要落盘
                status, error = "llm_error", f"{type(e).__name__}: {e}"
                break

            calls = resp.get("tool_calls") or []
            if calls:
                self.messages.append({
                    "role": "assistant",
                    "content": resp.get("content") or "",
                    "tool_calls": [
                        {"id": tc["id"], "type": "function",
                         "function": {
                             "name": tc["name"],
                             "arguments": json.dumps(
                                 tc["arguments"], ensure_ascii=False)}}
                        for tc in calls],
                })
                for tc in calls:
                    yield {"type": "tool_start", "round": rnd, "tool": tc["name"],
                           "arguments": tc["arguments"]}
                    t0 = time.time()
                    result, err = execute_tool(
                        tc["name"], tc["arguments"], self.cfg)
                    elapsed_ms = int((time.time() - t0) * 1000)
                    content = json.dumps(result, ensure_ascii=False)
                    self.messages.append({
                        "role": "tool", "tool_call_id": tc["id"],
                        "content": content})
                    trace.append({
                        "round": rnd, "tool": tc["name"],
                        "arguments": tc["arguments"], "ok": err is None,
                        "elapsed_ms": elapsed_ms, "result": content[:500]})
                    tool_names.append(tc["name"])
                    yield {"type": "tool_end", "round": rnd, "tool": tc["name"],
                           "ok": err is None, "elapsed_ms": elapsed_ms,
                           "summary": content[:200]}
                continue  # 工具结果回传后进入下一轮

            yield {"type": "generating"}
            answer = (resp.get("content") or "").strip()
            if not answer:
                status, error = "empty_answer", "模型返回空内容"
            break
        else:
            status = "max_rounds"
            error = f"达到最大工具轮次({self.max_rounds})，未能给出最终答复"

        if answer:
            self.messages.append({"role": "assistant", "content": answer})
        final_answer = answer if (answer and status == "ok") else (answer or FALLBACK_ANSWER)

        yield {"type": "answer", "answer": final_answer}
        # 监控采集：本次会话运行结果记入当前专家的统计（供降权与告警）
        ok = status == "ok"
        monitor.STATS.record_agent(self.specialist, ok,
                                   int((time.time() - t_run) * 1000))
        yield {"type": "result", "result": {
            "answer": final_answer,
            "status": status,
            "error": error,
            "rounds": rounds,
            "tool_calls": tool_names,
            "trace": trace,
            "model": getattr(self.llm, "model_in_use", None),
            "route": {"intent": route["intent"], "specialist": self.specialist,
                      "label": spec["label"], "emoji": spec["emoji"],
                      "via": route["via"]},
            "compressed_turns": (compression or {}).get("compressed_turns", 0),
        }}


# ---------------------------------------------------------------- CLI
def _save_session(out_dir: Path, record: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"sess_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return path


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="小铺智答 headless 客服 Agent")
    ap.add_argument("-m", "--message", help="一次性咨询（headless 单轮）")
    ap.add_argument("-i", "--interactive", action="store_true", help="交互模式")
    ap.add_argument("--customer", default=None, help="顾客ID，如 C003")
    ap.add_argument("--backend", default="auto", choices=["auto", "zhipu", "mock"])
    ap.add_argument("--trace", action="store_true", help="打印工具调用轨迹")
    args = ap.parse_args()

    cfg = load_config()
    llm, backend = build_llm(cfg, args.backend)
    if args.backend == "auto" and backend == "mock":
        print("[提示] 未检测到 API Key，使用离线自检 Mock 后端（仅验证管道）。"
              "配置 ZHIPUAI_API_KEY 后即可接入真实 GLM。")

    session = AgentSession(cfg, llm, customer_id=args.customer)
    out_dir = ROOT / "results" / "sessions"

    if args.message:
        t0 = time.time()
        result = session.run(args.message)
        elapsed = int((time.time() - t0) * 1000)
        print(f"\n顾客: {args.message}")
        print(f"客服: {result['answer']}\n")
        if args.trace:
            for t in result["trace"]:
                mark = "ok " if t["ok"] else "ERR"
                print(f"  [round{t['round']} {mark}] {t['tool']}"
                      f"({json.dumps(t['arguments'], ensure_ascii=False)}) "
                      f"{t['elapsed_ms']}ms")
            print(f"  status={result['status']} rounds={result['rounds']} "
                  f"model={result['model']} elapsed={elapsed}ms")
        path = _save_session(out_dir, {
            "meta": {"time": datetime.now().isoformat(timespec="seconds"),
                     "backend": backend, "model": result["model"],
                     "customer_id": args.customer, "elapsed_ms": elapsed},
            "result": result, "messages": session.messages})
        print(f"会话已保存: {path}")
        return

    if args.interactive or not args.message:
        print(f"小铺智答 交互模式（backend={backend}, "
              f"model={getattr(llm, 'model_in_use', 'pending')}），输入 quit 退出。")
        while True:
            try:
                text = input("顾客> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text.lower() in ("quit", "exit", "q"):
                break
            t0 = time.time()
            result = session.run(text)
            print(f"客服> {result['answer']}")
            if args.trace and result["trace"]:
                for t in result["trace"]:
                    print(f"    [{t['tool']}] {json.dumps(t['arguments'], ensure_ascii=False)}")
            if result["status"] != "ok":
                print(f"    (status={result['status']} {result['error']})")
        path = _save_session(out_dir, {
            "meta": {"time": datetime.now().isoformat(timespec="seconds"),
                     "backend": backend, "customer_id": args.customer},
            "messages": session.messages})
        print(f"\n会话已保存: {path}")


if __name__ == "__main__":
    main()
