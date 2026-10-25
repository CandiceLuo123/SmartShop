# -*- coding: utf-8 -*-
"""
小铺智答 Web 服务：
  顾客端 —— 注册/登录、多会话聊天（SSE 实时工具进度、历史恢复）
  商家端 —— 概览统计、订单/工单/顾客/商品/会话审查
启动：python run_web.py
"""
import json
import threading
import uuid
from contextlib import closing
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from src.agent import AgentSession, load_config
from src.llm import build_llm
from src import memory
from src.tools import get_customer_profile
from web import auth
from web.db import connect, ensure_schema, next_customer_id, now_str, rows

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="小铺智答", docs_url=None, redoc_url=None)

_CFG = load_config()
_LLM, _BACKEND = build_llm(_CFG, "auto")
_AGENTS: dict[str, AgentSession] = {}          # session_id -> AgentSession
_AGENTS_LOCK = threading.Lock()


# ---------------------------------------------------------------- 认证
def current_user(request: Request) -> Optional[dict]:
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.startswith("Bearer ") else ""
    uid = auth.verify_token(token)
    if uid is None:
        return None
    with closing(connect()) as conn:
        found = rows(conn, "SELECT * FROM users WHERE id=?", (uid,))
    return found[0] if found else None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    return JSONResponse({"detail": f"服务器错误: {exc}"}, status_code=500)


# ---------------------------------------------------------------- 静态页
@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.get("/admin")
def admin_page():
    return FileResponse(STATIC / "admin.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------------------------------------------------------- 账号
def _user_payload(user: dict) -> dict:
    out = {"id": user["id"], "username": user["username"],
           "role": user["role"], "display_name": user["display_name"],
           "customer_id": user["customer_id"]}
    if user["customer_id"]:
        out["customer_profile"] = get_customer_profile(user["customer_id"], _CFG)
    return out


@app.post("/api/register")
async def register(request: Request):
    body = await request.json()
    username = str(body.get("username", "")).strip().lower()
    password = str(body.get("password", ""))
    display_name = str(body.get("display_name", "")).strip() or username
    phone = str(body.get("phone", "")).strip()

    if not (3 <= len(username) <= 20) or not username.replace("_", "").isalnum():
        return JSONResponse({"detail": "用户名需为3-20位字母/数字/下划线"}, status_code=400)
    if len(password) < 6:
        return JSONResponse({"detail": "密码至少6位"}, status_code=400)

    salt = auth.new_salt()
    with closing(connect()) as conn:
        dup = conn.execute("SELECT 1 FROM users WHERE username=?",
                           (username,)).fetchone()
        if dup:
            return JSONResponse({"detail": "用户名已存在"}, status_code=400)
        cid = next_customer_id(conn)
        conn.execute(
            "INSERT INTO customers (customer_id, name, phone, vip_level, "
            "points, created_at) VALUES (?,?,?,0,0,?)",
            (cid, display_name, phone or None, now_str()))
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, salt, role, "
            "customer_id, display_name, created_at) VALUES (?,?,?,?,?,?,?)",
            (username, auth.hash_password(password, salt), salt, "customer",
             cid, display_name, now_str()))
        conn.commit()
        uid = cur.lastrowid
    return {"token": auth.make_token(uid), "user": _user_payload(
        {"id": uid, "username": username, "role": "customer",
         "display_name": display_name, "customer_id": cid})}


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    username = str(body.get("username", "")).strip().lower()
    password = str(body.get("password", ""))
    with closing(connect()) as conn:
        found = rows(conn, "SELECT * FROM users WHERE username=?", (username,))
    if not found or not auth.verify_password(
            password, found[0]["salt"], found[0]["password_hash"]):
        return JSONResponse({"detail": "用户名或密码错误"}, status_code=401)
    return {"token": auth.make_token(found[0]["id"]), "user": _user_payload(found[0])}


@app.get("/api/me")
def me(request: Request):
    return _user_payload(require_user(request))


# ---------------------------------------------------------------- 聊天会话
@app.get("/api/sessions")
def list_sessions(request: Request):
    user = require_user(request)
    with closing(connect()) as conn:
        data = rows(
            conn,
            "SELECT s.id, s.title, s.updated_at, "
            "       (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=s.id) AS msg_count "
            "FROM chat_sessions s WHERE s.user_id=? "
            "ORDER BY s.updated_at DESC LIMIT 50", (user["id"],))
    return {"sessions": data}


@app.post("/api/sessions")
async def create_session(request: Request):
    user = require_user(request)
    sid = uuid.uuid4().hex[:12]
    with closing(connect()) as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, user_id, title, messages_json, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (sid, user["id"], "新对话", "[]", now_str(), now_str()))
        conn.commit()
    return {"session_id": sid}


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str, request: Request):
    user = require_user(request)
    with closing(connect()) as conn:
        own = conn.execute("SELECT 1 FROM chat_sessions WHERE id=? AND user_id=?",
                           (sid, user["id"])).fetchone()
        if not own:
            return JSONResponse({"detail": "会话不存在"}, status_code=404)
        conn.execute("DELETE FROM chat_messages WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM chat_sessions WHERE id=?", (sid,))
        conn.commit()
    _AGENTS.pop(sid, None)
    return {"ok": True}


@app.get("/api/sessions/{sid}")
def get_session(sid: str, request: Request):
    user = require_user(request)
    with closing(connect()) as conn:
        meta = rows(conn, "SELECT id, title FROM chat_sessions "
                          "WHERE id=? AND user_id=?", (sid, user["id"]))
        if not meta:
            return JSONResponse({"detail": "会话不存在"}, status_code=404)
        msgs = rows(conn, "SELECT role, content, tools, created_at "
                          "FROM chat_messages WHERE session_id=? ORDER BY id",
                    (sid,))
    for m in msgs:
        try:
            m["tools"] = json.loads(m.get("tools") or "[]")
        except json.JSONDecodeError:
            m["tools"] = []
    return {"session": meta[0], "messages": msgs}


def _get_agent(sid: str, user: dict) -> AgentSession:
    """取内存中的会话 Agent；没有则从数据库快照恢复完整上下文与记忆。"""
    with _AGENTS_LOCK:
        agent = _AGENTS.get(sid)
        if agent is not None:
            return agent
        with closing(connect()) as conn:
            snap = rows(conn, "SELECT messages_json, summary FROM chat_sessions "
                              "WHERE id=? AND user_id=?", (sid, user["id"]))
        messages, summary = [], ""
        if snap:
            try:
                messages = json.loads(snap[0]["messages_json"] or "[]")
            except json.JSONDecodeError:
                messages = []
            summary = snap[0]["summary"] or ""
        profile = memory.get_profile(user["id"]) if user["id"] else {}
        agent = AgentSession(_CFG, _LLM, customer_id=user["customer_id"],
                             profile=profile)
        if messages:
            agent.messages = messages
        agent.session_summary = summary
        _AGENTS[sid] = agent
        return agent


@app.post("/api/chat/stream")
async def chat_stream(request: Request):
    user = require_user(request)
    body = await request.json()
    sid = str(body.get("session_id", ""))
    message = str(body.get("message", "")).strip()
    if not message:
        return JSONResponse({"detail": "消息不能为空"}, status_code=400)
    with closing(connect()) as conn:
        own = conn.execute("SELECT 1 FROM chat_sessions WHERE id=? AND user_id=?",
                           (sid, user["id"])).fetchone()
    if not own:
        return JSONResponse({"detail": "会话不存在"}, status_code=404)

    agent = _get_agent(sid, user)
    turn_counter = {"n": 0}

    def event_gen():
        result = None
        try:
            for event in agent.run_iter(message):
                if event["type"] == "result":
                    result = event["result"]
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event["type"] == "compress":
                    turn_counter["n"] = 0  # 压缩后重新计数，立即刷新一次画像
        except Exception as e:  # noqa: BLE001 —— 出错也要让前端收到失败事件
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        # 持久化（失败也落盘）：消息、上下文快照、会话摘要、画像增量更新
        try:
            answer = (result or {}).get("answer") or "（本次回复失败，请重试或转人工）"
            tools = json.dumps((result or {}).get("tool_calls") or [],
                               ensure_ascii=False)
            trace = json.dumps((result or {}).get("trace") or [],
                               ensure_ascii=False)
            route = json.dumps((result or {}).get("route") or {},
                               ensure_ascii=False)
            with closing(connect()) as conn:
                conn.execute(
                    "INSERT INTO chat_messages (session_id, role, content, "
                    "tools, trace_json, route_json, created_at) VALUES (?,?,?,?,?,?,?)",
                    (sid, "user", message, "[]", "[]", "[]", now_str()))
                conn.execute(
                    "INSERT INTO chat_messages (session_id, role, content, "
                    "tools, trace_json, route_json, created_at) VALUES (?,?,?,?,?,?,?)",
                    (sid, "assistant", answer, tools, trace, route, now_str()))
                title = message[:18] + ("…" if len(message) > 18 else "")
                conn.execute(
                    "UPDATE chat_sessions SET messages_json=?, summary=?, "
                    "updated_at=? WHERE id=?",
                    (json.dumps(agent.messages, ensure_ascii=False),
                     agent.session_summary, now_str(), sid))
                conn.execute(
                    "UPDATE chat_sessions SET title=? WHERE id=? AND title='新对话'",
                    (title, sid))
                conn.commit()
            # L3 画像：每 N 轮增量更新一次（写库失败不影响对话）
            turn_counter["n"] += 1
            if user["id"] and turn_counter["n"] >= memory.PROFILE_UPDATE_EVERY:
                turn_counter["n"] = 0
                try:
                    agent.profile = memory.update_profile(
                        _LLM, user["id"], user["customer_id"], message, answer)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- 记忆（画像/摘要）查询
@app.get("/api/memory")
def get_memory(request: Request):
    user = require_user(request)
    profile = memory.get_profile(user["id"])
    with closing(connect()) as conn:
        summaries = rows(
            conn, "SELECT id, title, summary, updated_at FROM chat_sessions "
                  "WHERE user_id=? AND summary!='' ORDER BY updated_at DESC LIMIT 10",
            (user["id"],))
    return {"profile": profile, "summaries": summaries}


# ---------------------------------------------------------------- 商家端
@app.get("/api/admin/overview")
def admin_overview(request: Request):
    require_admin(request)
    with closing(connect()) as conn:
        order_status = rows(conn, "SELECT status, COUNT(*) n FROM orders "
                                  "GROUP BY status ORDER BY n DESC")
        ticket_status = rows(conn, "SELECT status, COUNT(*) n FROM tickets "
                                   "GROUP BY status")
        open_tickets = conn.execute(
            "SELECT COUNT(*) FROM tickets WHERE status!='已解决'").fetchone()[0]
        low_stock = rows(conn, "SELECT sku, name, stock FROM products "
                               "WHERE status='在售' AND stock<=10 ORDER BY stock")
        stats = {
            "customers": conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
            "products": conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
            "orders": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            "chat_sessions": conn.execute("SELECT COUNT(*) FROM chat_sessions").fetchone()[0],
            "open_tickets": open_tickets,
            "today_chats": conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE created_at>=date('now') "
                "AND role='user'").fetchone()[0],
        }
        recent_tickets = rows(
            conn, "SELECT t.ticket_id, t.type, t.priority, t.status, t.order_no, "
                  "t.description, t.created_at, u.display_name AS customer "
                  "FROM tickets t LEFT JOIN customers c ON t.customer_id=c.customer_id "
                  "LEFT JOIN users u ON u.customer_id=c.customer_id "
                  "ORDER BY t.rowid DESC LIMIT 6")
    return {"stats": stats, "order_status": order_status,
            "ticket_status": ticket_status, "low_stock": low_stock,
            "recent_tickets": recent_tickets, "model": _LLM.model_in_use}


@app.get("/api/admin/orders")
def admin_orders(request: Request):
    require_admin(request)
    with closing(connect()) as conn:
        orders = rows(
            conn, "SELECT o.*, c.name AS customer_name FROM orders o "
                  "LEFT JOIN customers c ON o.customer_id=c.customer_id "
                  "ORDER BY o.created_at DESC")
        items = rows(conn, "SELECT i.order_no, i.sku, i.qty, i.unit_price, "
                           "i.item_status, p.name FROM order_items i "
                           "LEFT JOIN products p ON i.sku=p.sku")
    by_order: dict[str, list] = {}
    for it in items:
        by_order.setdefault(it["order_no"], []).append(it)
    for o in orders:
        o["items"] = by_order.get(o["order_no"], [])
    return {"orders": orders}


@app.get("/api/admin/tickets")
def admin_tickets(request: Request):
    require_admin(request)
    with closing(connect()) as conn:
        data = rows(conn, "SELECT * FROM tickets ORDER BY "
                          "CASE status WHEN '新建' THEN 0 WHEN '处理中' THEN 1 ELSE 2 END, "
                          "CASE priority WHEN '高' THEN 0 WHEN '中' THEN 1 ELSE 2 END, "
                          "rowid DESC")
    return {"tickets": data}


@app.patch("/api/admin/tickets/{ticket_id}")
async def admin_update_ticket(ticket_id: str, request: Request):
    require_admin(request)
    body = await request.json()
    status = body.get("status")
    note = body.get("resolved_note")
    if status not in ("新建", "处理中", "已解决"):
        return JSONResponse({"detail": "非法状态"}, status_code=400)
    with closing(connect()) as conn:
        found = conn.execute("SELECT 1 FROM tickets WHERE ticket_id=?",
                             (ticket_id,)).fetchone()
        if not found:
            return JSONResponse({"detail": "工单不存在"}, status_code=404)
        conn.execute("UPDATE tickets SET status=?, resolved_note=? WHERE ticket_id=?",
                     (status, note, ticket_id))
        conn.commit()
    return {"ok": True}


@app.get("/api/admin/customers")
def admin_customers(request: Request):
    require_admin(request)
    with closing(connect()) as conn:
        data = rows(
            conn, "SELECT c.*, u.username FROM customers c "
                  "LEFT JOIN users u ON u.customer_id=c.customer_id "
                  "ORDER BY c.customer_id")
        for c in data:
            c["order_count"] = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE customer_id=?",
                (c["customer_id"],)).fetchone()[0]
    return {"customers": data}


@app.get("/api/admin/products")
def admin_products(request: Request):
    require_admin(request)
    with closing(connect()) as conn:
        data = rows(conn, "SELECT * FROM products ORDER BY category, sku")
    return {"products": data}


@app.get("/api/admin/chats")
def admin_chats(request: Request):
    require_admin(request)
    with closing(connect()) as conn:
        data = rows(
            conn, "SELECT s.id, s.title, s.updated_at, u.username, "
                  "u.display_name, u.role, "
                  "(SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=s.id) AS msg_count "
                  "FROM chat_sessions s LEFT JOIN users u ON s.user_id=u.id "
                  "ORDER BY s.updated_at DESC LIMIT 100")
    return {"chats": data}


@app.get("/api/admin/chats/{sid}")
def admin_chat_detail(sid: str, request: Request):
    require_admin(request)
    with closing(connect()) as conn:
        meta = rows(conn, "SELECT s.*, u.username, u.display_name FROM chat_sessions s "
                          "LEFT JOIN users u ON s.user_id=u.id WHERE s.id=?", (sid,))
        if not meta:
            return JSONResponse({"detail": "会话不存在"}, status_code=404)
        msgs = rows(conn, "SELECT role, content, tools, trace_json, route_json, "
                          "created_at FROM chat_messages WHERE session_id=? "
                          "ORDER BY id", (sid,))
    for m in msgs:
        for field in ("tools", "trace_json", "route_json"):
            try:
                m[field] = json.loads(m[field] or "[]")
            except json.JSONDecodeError:
                m[field] = []
    return {"session": meta[0], "messages": msgs}
