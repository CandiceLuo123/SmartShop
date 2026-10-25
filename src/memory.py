# -*- coding: utf-8 -*-
"""
三层记忆系统：
  L1 会话记忆 —— AgentSession.messages（短期，全量保留最近轮次）
  L2 会话摘要 —— user 轮数超过阈值时，把最早的若干轮用 LLM 压缩成摘要，
                 替换原消息并持久化到 chat_sessions.summary（中期）
  L3 用户画像 —— 跨会话长期记忆：偏好、历史诉求、重要事实，存 memory_profiles 表，
                 每完成 N 轮对话由 LLM 增量更新，注入 system prompt（长期）

零第三方依赖，全部落 SQLite。
"""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "shop.db"

# 触发参数（config.json agent 段可覆盖）
COMPRESS_AFTER_USER_TURNS = 15   # user 消息超过此数触发压缩
COMPRESS_USER_TURNS = 10         # 每次压缩最早的多少轮 user 消息
PROFILE_UPDATE_EVERY = 3         # 每完成多少轮对话增量更新一次画像

PROFILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_profiles (
    user_id     INTEGER PRIMARY KEY,
    customer_id TEXT,
    profile     TEXT DEFAULT '{}',   -- JSON: {identity, preferences, history_issues, notes}
    updated_at  TEXT
);
"""


def ensure_memory_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(PROFILE_SCHEMA)


# ---------------------------------------------------------------- L2 会话摘要
def _count_user_turns(messages: list) -> int:
    return sum(1 for m in messages if m.get("role") == "user"
               and not str(m.get("content") or "").startswith("【历史对话摘要】"))


def find_compress_range(messages: list, after: int, take: int):
    """
    返回 (start, end) 切片：messages[start:end] 为需压缩的最早 take 轮
    （从第一条真实 user 消息到第 take+1 条 user 消息之前，工具消息随所属轮一起走）。
    不需要压缩时返回 None。
    """
    user_idx = [i for i, m in enumerate(messages)
                if m.get("role") == "user"
                and not str(m.get("content") or "").startswith("【历史对话摘要】")]
    if len(user_idx) <= after:
        return None
    start = user_idx[0]
    end = user_idx[take] if len(user_idx) > take else len(messages)
    return start, end


def compress_summary(llm, messages: list, existing_summary: str,
                     start: int, end: int) -> str:
    """把 messages[start:end] 与已有摘要合并压缩成一段新的摘要文本。"""
    dialogue = []
    for m in messages[start:end]:
        role = {"user": "顾客", "assistant": "客服", "tool": "工具结果"}.get(
            m.get("role"), m.get("role"))
        content = str(m.get("content") or "")
        if m.get("role") == "tool":
            content = content[:150]
        if content.strip():
            dialogue.append(f"{role}: {content[:500]}")
    prompt = (
        "请把下面的客服对话片段压缩成一段简洁的会话摘要（300字以内），"
        "必须保留：涉及的具体订单号/商品/金额、顾客的诉求与情绪变化、"
        "客服已做的承诺与处理结果（工单号等）、尚未解决的问题。\n"
        f"{'已有前情摘要（请合并）：%s\\n' % existing_summary if existing_summary else ''}"
        "对话片段：\n" + "\n".join(dialogue))
    resp = llm.complete([
        {"role": "system", "content": "你是对话摘要助手，只输出摘要正文，不要任何解释。"},
        {"role": "user", "content": prompt},
    ], tools=None, max_tokens=4096)
    return (resp.get("content") or existing_summary or "").strip()[:800]


def apply_compression(llm, session) -> dict | None:
    """
    检查并执行会话记忆压缩。返回 {summary, compressed_turns} 或 None。
    压缩后 messages 变为：[system, ...剩余消息]（原位置插入一条摘要消息）。
    """
    cfg = getattr(session, "cfg", {})
    agent_cfg = cfg.get("agent", {})
    after = agent_cfg.get("compress_after_user_turns", COMPRESS_AFTER_USER_TURNS)
    take = agent_cfg.get("compress_user_turns", COMPRESS_USER_TURNS)

    messages = session.messages
    rng = find_compress_range(messages, after, take)
    if rng is None:
        return None
    start, end = rng
    existing = getattr(session, "session_summary", "") or ""
    summary = compress_summary(llm, messages, existing, start, end)
    if not summary:
        return None
    compressed_turns = sum(
        1 for m in messages[start:end] if m.get("role") == "user")
    session.session_summary = summary
    # 用一条 system 摘要消息替换被压缩的片段（保留在原位置，靠前）
    messages[start:end] = [{
        "role": "system",
        "content": f"【历史对话摘要】（已压缩 {compressed_turns} 轮早期对话）\n{summary}",
    }]
    return {"summary": summary, "compressed_turns": compressed_turns}


# ---------------------------------------------------------------- L3 用户画像
DEFAULT_PROFILE = {
    "identity": "",        # 称呼、身份线索（如"姓陈"）
    "preferences": [],     # 偏好（品类、预算、沟通风格）
    "history_issues": [],  # 历史诉求与处理结果
    "notes": [],           # 其他值得记住的事实
}


def get_profile(user_id: int) -> dict:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        ensure_memory_tables(conn)
        row = conn.execute(
            "SELECT profile FROM memory_profiles WHERE user_id=?", (user_id,)
        ).fetchone()
    if not row:
        return dict(DEFAULT_PROFILE)
    try:
        p = json.loads(row[0] or "{}")
    except json.JSONDecodeError:
        p = {}
    return {**DEFAULT_PROFILE, **p}


def save_profile(user_id: int, customer_id: str | None, profile: dict) -> None:
    from datetime import datetime
    with closing(sqlite3.connect(DB_PATH)) as conn:
        ensure_memory_tables(conn)
        conn.execute(
            "INSERT INTO memory_profiles (user_id, customer_id, profile, updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "customer_id=excluded.customer_id, profile=excluded.profile, "
            "updated_at=excluded.updated_at",
            (user_id, customer_id, json.dumps(profile, ensure_ascii=False),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()


def render_profile_block(profile: dict) -> str:
    """把画像渲染成 system prompt 片段（空画像返回空串）。"""
    parts = []
    if profile.get("identity"):
        parts.append(f"- 身份：{profile['identity']}")
    if profile.get("preferences"):
        parts.append("- 偏好：" + "；".join(profile["preferences"][:6]))
    if profile.get("history_issues"):
        parts.append("- 历史诉求：" + "；".join(profile["history_issues"][:6]))
    if profile.get("notes"):
        parts.append("- 其他记忆：" + "；".join(profile["notes"][:6]))
    if not parts:
        return ""
    return "\n【长期记忆（用户画像）】\n" + "\n".join(parts) + \
           "\n（自然地运用这些记忆，无需刻意声明；与本次对话冲突时以对话为准。）"


def update_profile(llm, user_id: int, customer_id: str | None,
                   user_msg: str, assistant_msg: str) -> dict:
    """用最新一轮对话增量更新用户画像（LLM 合并，JSON 输出，失败静默保留原画像）。"""
    current = get_profile(user_id)
    prompt = (
        "请根据最新对话轮更新用户画像。现有画像(JSON)：\n"
        f"{json.dumps(current, ensure_ascii=False)}\n\n"
        f"最新对话：\n顾客: {user_msg[:400]}\n客服: {assistant_msg[:400]}\n\n"
        "输出更新后的完整画像 JSON（不要 markdown 代码块），字段固定为：\n"
        '{"identity": "称呼/身份线索，无则空串", '
        '"preferences": ["偏好，最多8条，去重合并"], '
        '"history_issues": ["历史诉求及处理结果，最多8条，重要事件不丢"], '
        '"notes": ["其他值得长期记住的事实，最多8条"]}\n'
        "只补充确实值得长期记住的信息，不要记录一次性细节（如某次物流正常）。")
    try:
        resp = llm.complete([
            {"role": "system", "content":
                "你是用户画像维护助手，只输出一个 JSON 对象。"},
            {"role": "user", "content": prompt},
        ], tools=None, max_tokens=4096)
        text = (resp.get("content") or "").strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        merged = {**DEFAULT_PROFILE, **json.loads(text)}
        merged = {
            "identity": str(merged.get("identity") or "")[:100],
            "preferences": [str(x) for x in (merged.get("preferences") or [])][:8],
            "history_issues": [str(x) for x in (merged.get("history_issues") or [])][:8],
            "notes": [str(x) for x in (merged.get("notes") or [])][:8],
        }
        save_profile(user_id, customer_id, merged)
        return merged
    except Exception:  # noqa: BLE001 —— 画像更新失败不影响对话
        return current
