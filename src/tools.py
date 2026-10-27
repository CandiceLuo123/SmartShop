# -*- coding: utf-8 -*-
"""
客服 Agent 工具集（相当于该场景下的 "MCP tools"）：
  query_order        查订单（状态/物流/售后/明细）
  query_product      查商品（价格/库存/参数/在售状态）
  search_knowledge   知识库检索（政策/FAQ/商品注意事项，按标题分块打分）
  recommend_products 商品推荐（场景/预算/类目）
  apply_after_sales  售后政策校验（签收天数计算 + 自动登记工单）
  create_ticket      人工工单登记
所有工具只读店铺数据或写入 tickets 表，均为安全操作（headless 全权限运行）。
"""
import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from contextlib import closing
from datetime import datetime
from pathlib import Path

from src import database, monitor
from src.breaker import CircuitBreaker
from src.cache import CacheManager

ROOT = Path(__file__).resolve().parents[1]

TICKET_TYPES = {"查件", "催发货", "退货", "换货", "维修", "仅退款",
                "改地址", "发票", "投诉", "转人工", "其他"}
PRIORITIES = {"低", "中", "高"}
ISSUE_TYPES = {"质量问题", "无理由", "其他"}
REQUEST_TYPES = {"退货", "换货", "维修", "仅退款"}
WARRANTY_DAYS = 365  # 数码类保修期（天）
QUALITY_WINDOW = 15  # 质量问题包退换（天）
NO_REASON_WINDOW = 7  # 无理由退货（天）


# ---------------------------------------------------------------- 基础设施
# 可缓存工具（读多写少的查询类）；工单/售后校验不缓存
CACHEABLE = {"query_order", "query_product", "search_knowledge",
             "recommend_products"}

_CACHE: CacheManager | None = None
_BREAKERS: dict[str, CircuitBreaker] = {}
_TOOL_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")
_RAG_LLM = None  # AgentSession 初始化时注入，供查询改写/重排使用


def init_runtime(cfg: dict) -> None:
    """初始化缓存与熔断器（幂等，进程级只做一次）。"""
    global _CACHE
    if _CACHE is None:
        _CACHE = CacheManager(cfg)
    for spec in TOOLS:
        _BREAKERS.setdefault(spec["name"], CircuitBreaker(
            spec["name"],
            cfg.get("breaker", {}).get("failure_threshold", 5),
            cfg.get("breaker", {}).get("recovery_seconds", 60)))


def set_rag_llm(llm) -> None:
    """AgentSession 启动时注入 LLM，供 search_with_rewrite 的改写/重排调用。"""
    global _RAG_LLM
    _RAG_LLM = llm


def _fallback_result(tool: str, reason: str) -> dict:
    """降级结果：熔断/超时/异常时给模型的兜底说明，而不是抛错。"""
    if tool == "search_knowledge":
        return {"found": False,
                "hint": f"知识库暂时不可用（{reason}），请基于已有信息谨慎回答，"
                        "并建议顾客稍后再试或转人工核实政策细节。"}
    return {"degraded": True, "reason": reason,
            "hint": f"工具 {tool} 暂时不可用（{reason}）。请告知顾客系统繁忙，"
                    "建议稍后重试或回复\"转人工\"。"}


def _bquery(cfg: dict, sql_big: str, sql_main: str, params: tuple) -> list[dict]:
    """业务查询双库回退：先查业务库（10 万行）；SQLite 后端查不到时回退主库（演示数据）。"""
    with database.business_conn(cfg) as conn:
        rows = database.q(conn, sql_big, params)
    if rows or database.backend_name(cfg) != "sqlite":
        return rows
    with closing(sqlite3.connect(ROOT / cfg["paths"]["db"])) as main:
        main.row_factory = sqlite3.Row
        return _rows(main, sql_main, params)
def _connect(cfg: dict) -> sqlite3.Connection:
    conn = sqlite3.connect(ROOT / cfg["paths"]["db"])
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    cur = conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def _today(cfg: dict) -> datetime:
    return datetime.strptime(cfg["today"], "%Y-%m-%d")


def _days_since(cfg: dict, dt: datetime | None):
    """按自然日计算距今天数（"自签收之日起N日内"：签收当天为第0天）。"""
    if dt is None:
        return None
    return (_today(cfg).date() - dt.date()).days


def _parse_dt(value: str):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _tokens(text: str) -> list[str]:
    """轻量中英混合分词：英文/数字词 + 中文二元组。"""
    text = (text or "").lower()
    toks = re.findall(r"[a-z0-9]+", text)
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    toks += [cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)]
    return [t for t in toks if len(t) >= 2]


def _parse_tags(raw: str) -> list:
    try:
        v = json.loads(raw) if raw else []
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# ---------------------------------------------------------------- 顾客档案（内部用，不暴露给模型）
def get_customer_profile(customer_id: str, cfg: dict) -> str:
    with closing(_connect(cfg)) as conn:
        rows = _rows(conn, "SELECT * FROM customers WHERE customer_id=?",
                     (customer_id,))
    if not rows:
        return f"（顾客ID {customer_id}，系统暂无档案，如需查订单请其提供订单号）"
    c = rows[0]
    vip = {0: "普通会员", 1: "银卡会员", 2: "金卡会员"}.get(c["vip_level"], "普通会员")
    return (f"{c['customer_id']} {c['name']}（{vip}，积分 {c['points']}，"
            f"注册于 {c['created_at'][:10]}）")


# ---------------------------------------------------------------- 工具实现
def query_order(cfg: dict, order_no: str = None, customer_id: str = None) -> dict:
    """查订单（业务库+主库回退）：支持订单号精确/后缀匹配，或按顾客列出名下订单。"""
    ph_ = database.ph(cfg)
    if order_no:
        order_no = re.sub(r"\s+", "", str(order_no)).upper()
        ot, it, pt = (database.btable(cfg, n)
                      for n in ("orders", "order_items", "products"))
        # 单号子句对两种后端通用（LIKE 通配符放在参数值里）
        sql_big = (f"SELECT * FROM {ot} WHERE order_no = {ph_} "
                   f"OR order_no LIKE {ph_} LIMIT 2")
        rows = _bquery(cfg, sql_big,
                       "SELECT * FROM orders WHERE order_no=? OR order_no LIKE ? LIMIT 2",
                       (order_no, f"%{order_no}"))
        if not rows:
            return {"found": False,
                    "hint": "未找到该订单号，请与顾客核对（格式如 SO2610120001）。"}
        out = []
        for o in rows:
            o["items"] = _bquery(
                cfg,
                f"SELECT i.sku, i.qty, i.unit_price, i.item_status, p.name, "
                f"p.category, p.tags FROM {it} i LEFT JOIN {pt} p ON i.sku=p.sku "
                f"WHERE i.order_no={ph_}",
                "SELECT i.sku, i.qty, i.unit_price, i.item_status, p.name, "
                "p.category, p.tags FROM order_items i LEFT JOIN products p "
                "ON i.sku=p.sku WHERE i.order_no=?",
                (o["order_no"],))
            for it_ in o["items"]:
                it_["tags"] = _parse_tags(it_.get("tags"))
            out.append(o)
        # 工单在平台主库（SQLite），跨库补充展示
        with closing(sqlite3.connect(ROOT / cfg["paths"]["db"])) as main:
            main.row_factory = sqlite3.Row
            for o in out:
                o["tickets"] = _rows(
                    main, "SELECT ticket_id, type, priority, status, created_at "
                          "FROM tickets WHERE order_no=?", (o["order_no"],))
        return {"found": True, "orders": out}
    if customer_id:
        ot = database.btable(cfg, "orders")
        rows = _bquery(
            cfg,
            f"SELECT order_no, status, total_amount, payment_time, logistics_status, "
            f"receipt_time, created_at FROM {ot} WHERE customer_id={ph_} "
            f"ORDER BY created_at DESC LIMIT 10",
            "SELECT order_no, status, total_amount, payment_time, logistics_status, "
            "receipt_time, created_at FROM orders WHERE customer_id=? "
            "ORDER BY created_at DESC LIMIT 10", (customer_id,))
        return {"found": bool(rows), "orders": rows,
                "hint": None if rows else "该顾客名下暂无订单"}
    return {"found": False, "error": "请提供 order_no 或 customer_id 之一。"}


def query_product(cfg: dict, sku: str = None, keyword: str = None,
                  category: str = None) -> dict:
    """查商品（业务库+主库回退）：sku 精确；keyword 模糊匹配名称/标签/描述。"""
    ph_ = database.ph(cfg)
    pt = database.btable(cfg, "products")
    def _search_cond(ph_: str) -> tuple[str, list]:
        """keyword 多列 OR + category AND，占位符与参数严格一一对应。"""
        conds, params = [], []
        if keyword:
            cols = ["name", "sku", "tags", "description", "category"]
            conds.append("(" + " OR ".join(f"{c} LIKE {ph_}" for c in cols) + ")")
            params += [f"%{keyword}%"] * len(cols)
        if category:
            conds.append(f"category LIKE {ph_}")
            params.append(f"%{category}%")
        return " AND ".join(conds), params

    if sku:
        rows = _bquery(cfg, f"SELECT * FROM {pt} WHERE sku={ph_}",
                       "SELECT * FROM products WHERE sku=?", (str(sku).upper(),))
    else:
        cond_big, params_big = _search_cond(ph_)
        cond_main, params_main = _search_cond("?")
        with database.business_conn(cfg) as conn:
            rows = database.q(
                conn, f"SELECT * FROM {pt} WHERE {cond_big} "
                      f"ORDER BY rating DESC LIMIT 5", tuple(params_big))
        if not rows and database.backend_name(cfg) == "sqlite":
            with closing(sqlite3.connect(ROOT / cfg["paths"]["db"])) as main:
                main.row_factory = sqlite3.Row
                rows = _rows(
                    main, f"SELECT * FROM products WHERE {cond_main} "
                          f"ORDER BY rating DESC LIMIT 5", tuple(params_main))
    for p in rows:
        p["tags"] = _parse_tags(p.get("tags"))
    if not rows:
        return {"found": False,
                "hint": "未匹配到商品，可放宽关键词或让顾客描述需求后改用推荐。"}
    return {"found": True, "products": rows}


def _recall(cfg: dict, query: str, top_k: int = 5) -> list[dict]:
    """词频召回（单路）：按 '## ' 标题分块，标题权重×3。"""
    kdir = ROOT / cfg["paths"]["knowledge_dir"]
    chunks = []
    for md in sorted(kdir.glob("*.md")):
        title, buf = md.name, []
        for line in md.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                if buf:
                    chunks.append((md.name, title, "\n".join(buf).strip()))
                title, buf = line[3:].strip(), [line]
            else:
                if not buf:
                    buf = [f"# {md.name}"]
                buf.append(line)
        if buf:
            chunks.append((md.name, title, "\n".join(buf).strip()))

    qtokens = _tokens(query)
    scored = []
    for fname, heading, body in chunks:
        hay_t, hay_b = _tokens(heading), _tokens(body)
        score = sum(3 * hay_t.count(t) + hay_b.count(t) for t in qtokens)
        if score > 0:
            scored.append({"score": score, "source": f"{fname} · {heading}",
                           "content": body[:600]})
    scored.sort(key=lambda x: -x["score"])
    return scored[:top_k]


def _rewrite_query(llm, query: str, n: int) -> list[str]:
    """查询改写：扩写成 n 个不同角度的子查询；失败兜底返回原始查询。"""
    try:
        resp = llm.complete([
            {"role": "system", "content":
                "你是检索查询改写助手。把用户查询改写成不同角度的子查询，"
                "每行一个，不要编号不要解释。"},
            {"role": "user", "content": f"原始查询：{query}\n改写为 {n} 个子查询："},
        ], tools=None, max_tokens=2048)
        subs = [s.strip().lstrip("0123456789.、- ") for s in
                (resp.get("content") or "").strip().splitlines() if s.strip()]
        out = [query] + [s for s in subs if s and s != query]
        return list(dict.fromkeys(out))[:n + 1]
    except Exception:  # noqa: BLE001 —— 改写失败用原始查询
        return [query]


def _rerank(llm, query: str, items: list[dict], top_k: int) -> list[dict]:
    """LLM 重排：候选多于 top_k 才调用；失败兜底保序截断。"""
    if len(items) <= top_k or llm is None:
        return items[:top_k]
    try:
        listing = "\n".join(f"{i}. [{x['source']}] {x['content'][:120]}"
                            for i, x in enumerate(items))
        resp = llm.complete([
            {"role": "system", "content":
                "你是检索结果重排助手，只输出 JSON 数组（按相关性降序的索引列表）。"},
            {"role": "user", "content":
                f"用户查询：{query}\n候选结果：\n{listing}\n"
                f"返回最相关的前 {top_k} 个索引，格式如 [2,0,5]："},
        ], tools=None, max_tokens=2048)
        order = json.loads((resp.get("content") or "[]").strip()
                           .removeprefix("```json").removeprefix("```")
                           .removesuffix("```").strip())
        picked = [items[i] for i in order
                  if isinstance(i, int) and 0 <= i < len(items)]
        return (picked + [x for x in items if x not in picked])[:top_k]
    except Exception:  # noqa: BLE001 —— 重排失败按原始顺序截断
        return items[:top_k]


def search_with_rewrite(cfg: dict, query: str, top_k: int = 3) -> dict:
    """
    检索优化链路（借鉴 EchoMind search_with_rewrite）：
      1. LLM 查询改写 → 原始 + 3 个子查询
      2. 多路并行召回（线程池）
      3. 按内容 md5 合并去重
      4. LLM 重排取 Top-K（候选不足直接跳过，省一次调用）
    任一环节失败都有兜底，不会让检索整体挂掉。
    """
    rag_cfg = cfg.get("rag", {})
    n_sub = rag_cfg.get("rewrite_subqueries", 3)
    recall_k = rag_cfg.get("recall_top_k", 5)

    queries = _rewrite_query(_RAG_LLM, query, n_sub) if _RAG_LLM else [query]
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        futs = [pool.submit(_recall, cfg, q, recall_k) for q in queries]
        all_results = [f.result() for f in futs]

    seen, merged = set(), []
    for results in all_results:
        for item in results:
            key = _CACHE.cache_key(item["source"], item["content"][:200]) \
                if _CACHE else str(item)
            if key not in seen:
                seen.add(key)
                merged.append(item)
    merged.sort(key=lambda x: -x["score"])

    reranked = _rerank(_RAG_LLM, query, merged, top_k)
    return {"found": bool(reranked),
            "results": reranked,
            "rewritten_queries": queries,
            "merged_candidates": len(merged),
            "reranked": _RAG_LLM is not None and len(merged) > top_k,
            "hint": None if reranked else
                    "知识库未命中，请换个说法检索；仍无则如实告知并登记人工。"}


def search_knowledge(cfg: dict, query: str) -> dict:
    """知识库检索：走改写+并行召回+重排的完整链路（结果由 execute_tool 缓存）。"""
    return search_with_rewrite(cfg, query, cfg.get("rag", {}).get("rerank_top_k", 3))


def recommend_products(cfg: dict, use_case: str = None, budget: float = None,
                       category: str = None) -> dict:
    """商品推荐（业务库，主库演示数据合并）：预算/类目过滤 + 场景标签匹配。"""
    ph_ = database.ph(cfg)
    pt = database.btable(cfg, "products")
    with database.business_conn(cfg) as conn:
        rows = database.q(conn, f"SELECT * FROM {pt} WHERE status={ph_}", ("在售",))
    if database.backend_name(cfg) == "sqlite":  # 合并主库演示商品
        with closing(sqlite3.connect(ROOT / cfg["paths"]["db"])) as main:
            main.row_factory = sqlite3.Row
            rows += _rows(main, "SELECT * FROM products WHERE status='在售'")
    wants = _tokens(use_case or "")
    filtered = []
    for p in rows:
        p["tags"] = _parse_tags(p.get("tags"))
        if category and category not in (p["category"] or ""):
            continue
        if budget is not None and p["price"] > float(budget):
            continue
        filtered.append(p)
    pool = filtered or rows
    scored = []
    for p in pool:
        text = _tokens(" ".join(p["tags"]) + " " + p["name"] + " " +
                       (p["description"] or ""))
        hits = sorted({w for w in wants if w in text})
        scored.append((len(hits), p["rating"], p, hits))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    if wants and scored and scored[0][0] > 0:
        # 有场景命中时只保留命中的商品，避免推荐不相关商品
        scored = [s for s in scored if s[0] > 0]
    top = scored[:3]
    out = [{"sku": p["sku"], "name": p["name"], "price": p["price"],
            "stock": p["stock"], "rating": p["rating"], "tags": p["tags"],
            "reason": (f"匹配需求点：{'、'.join(hits)}；评分 {p['rating']}"
                       if hits else f"高评分热销（{p['rating']} 分），符合预算")}
           for _, _, p, hits in top]
    return {"recommendations": out, "note": "推荐时请结合商品描述说明适用场景。"}


def create_ticket(cfg: dict, order_no: str = None, customer_id: str = None,
                  type: str = "其他", priority: str = "中",
                  description: str = "") -> dict:
    """登记人工工单（写入 tickets 表）。"""
    if type not in TICKET_TYPES:
        return {"error": f"type 必须是 {'/'.join(sorted(TICKET_TYPES))} 之一"}
    if priority not in PRIORITIES:
        return {"error": f"priority 必须是 低/中/高 之一"}
    now = datetime.now()
    with closing(_connect(cfg)) as conn:
        order_ok, customer_ok = True, True
        if order_no:
            order_ok = bool(_rows(conn, "SELECT 1 FROM orders WHERE order_no=?",
                                  (order_no,)))
        if customer_id:
            customer_ok = bool(_rows(
                conn, "SELECT 1 FROM customers WHERE customer_id=?", (customer_id,)))
        if not order_ok or not customer_ok:
            return {"error": "order_no 或 customer_id 不存在，请先核实"}
        prefix = "T" + now.strftime("%Y%m%d")
        count = conn.execute(
            "SELECT COUNT(*) FROM tickets WHERE ticket_id LIKE ?",
            (prefix + "%",)).fetchone()[0]
        ticket_id = f"{prefix}{count + 1:04d}"
        conn.execute(
            "INSERT INTO tickets (ticket_id, order_no, customer_id, type, "
            "priority, description, status, created_at) "
            "VALUES (?,?,?,?,?,?, '新建', ?)",
            (ticket_id, order_no, customer_id, type, priority,
             description or "(无描述)", now.strftime("%Y-%m-%d %H:%M")))
        conn.commit()
    return {"ticket_id": ticket_id, "type": type, "priority": priority,
            "order_no": order_no, "status": "新建",
            "message": f"工单已登记（{ticket_id}），人工将在服务时间(工作日9:00-21:00)跟进。"}


def apply_after_sales(cfg: dict, order_no: str, issue_type: str = "质量问题",
                      request: str = "退货", reason: str = "") -> dict:
    """
    售后政策校验：按签收天数判定（7天无理由 / 15天质量问题 / 12个月保修）。
    校验通过的退货/换货/维修自动登记工单；不通过则返回政策依据与替代方案，
    严禁向顾客做政策外承诺。
    """
    if issue_type not in ISSUE_TYPES:
        return {"error": f"issue_type 必须是 {'/'.join(sorted(ISSUE_TYPES))} 之一"}
    if request not in REQUEST_TYPES:
        return {"error": f"request 必须是 {'/'.join(sorted(REQUEST_TYPES))} 之一"}

    ot, it, pt = (database.btable(cfg, n)
                  for n in ("orders", "order_items", "products"))
    rows = _bquery(cfg, f"SELECT * FROM {ot} WHERE order_no = {database.ph(cfg)}",
                   "SELECT * FROM orders WHERE order_no=?",
                   (re.sub(r"\s+", "", str(order_no)).upper(),))
    if not rows:
        return {"error": f"未找到订单 {order_no}，请先与顾客核对订单号。"}
    order = rows[0]
    items = _bquery(
        cfg,
        f"SELECT i.sku, i.qty, i.item_status, p.name, p.tags "
        f"FROM {it} i LEFT JOIN {pt} p ON i.sku=p.sku WHERE i.order_no={database.ph(cfg)}",
        "SELECT i.sku, i.qty, i.item_status, p.name, p.tags "
        "FROM order_items i LEFT JOIN products p ON i.sku=p.sku WHERE i.order_no=?",
        (order["order_no"],))
    for it in items:
        it["tags"] = _parse_tags(it.get("tags"))
    receipt = _parse_dt(order.get("receipt_time"))
    days = _days_since(cfg, receipt)

    base = {
        "order_no": order["order_no"], "issue_type": issue_type,
        "request": request, "days_since_receipt": days,
        "order_status": order["status"], "items": items,
    }

    if order["status"] == "已退款":
        return {**base, "allowed": False,
                "policy_basis": "该订单退款已完成",
                "suggestion": "向顾客说明退款已完成，引导其查看支付渠道到账记录（银行卡最长3-7个工作日）。"}
    if order["status"] in ("待付款", "已取消"):
        return {**base, "allowed": True,
                "policy_basis": "订单尚未支付/已取消",
                "suggestion": "待付款订单可直接不支付，24小时后自动取消，无需退款。"}
    if days is None:
        return {**base, "allowed": False,
                "policy_basis": "政策期限自签收之日起算，该订单尚未签收",
                "suggestion": "已发货未签收可建议拒收；签收后再凭照片/视频申请售后。",
                "shipping_fee": None}

    def _ticket(t_type, desc):
        return create_ticket(cfg, order_no=order["order_no"],
                             customer_id=order["customer_id"], type=t_type,
                             priority="高", description=desc)

    if issue_type == "质量问题":
        if days <= QUALITY_WINDOW:
            t = _ticket(request, f"[售后校验通过] {reason or '质量问题'}；"
                        f"签收{days}天，质量包退换期内")
            return {**base, "allowed": True,
                    "policy_basis": f"签收 {days} 天，在 15 天质量问题包退换期内",
                    "shipping_fee": "商家承担（买家垫付，退款时一并返还）",
                    "ticket_id": t.get("ticket_id"), "ticket_status": t.get("status"),
                    "suggestion": "请顾客提供故障照片/视频；涉及电池鼓包/发烫/异味，"
                                  "先提醒停止使用与充电、远离易燃物。"}
        if request == "维修" and days <= WARRANTY_DAYS:
            t = _ticket("维修", f"[保修维修] {reason or '质量问题'}；签收{days}天，保修期内")
            return {**base, "allowed": True,
                    "policy_basis": f"签收 {days} 天，超过15天包退换期，在12个月保修期内",
                    "shipping_fee": "寄出运费买家承担，修好寄回运费商家承担",
                    "ticket_id": t.get("ticket_id"), "ticket_status": t.get("status"),
                    "suggestion": "引导顾客走免费维修；人为损坏/进水/私拆不保修。"}
        return {**base, "allowed": False,
                "policy_basis": f"签收已 {days} 天，超过 15 天质量问题包退换期限",
                "suggestion": "不可承诺退款/退换。可引导免费维修（保修期内），"
                              "或登记工单申请小额优惠券安抚（人工审核）。"}

    if issue_type == "无理由":
        if days <= NO_REASON_WINDOW:
            exception = any(
                any(t in ("定制", "贴身") or "食品" in t for t in (it["tags"] or []))
                for it in items)
            if exception:
                return {**base, "allowed": False,
                        "policy_basis": "商品属不支持7天无理由的例外类目",
                        "suggestion": "例外商品仅质量问题可退换。"}
            t = _ticket("退货", f"[7天无理由] 签收{days}天；{reason or '无理由退货'}")
            return {**base, "allowed": True,
                    "policy_basis": f"签收 {days} 天，在 7 天无理由退货期内",
                    "shipping_fee": "买家承担（退货时实际扣运费10元）",
                    "ticket_id": t.get("ticket_id"), "ticket_status": t.get("status"),
                    "suggestion": "请顾客保持商品未使用、包装完好。"}
        return {**base, "allowed": False,
                "policy_basis": f"签收已 {days} 天，超过 7 天无理由退货期限",
                "suggestion": "如有质量故障可按 15 天质量问题流程办理（需凭证）。"}

    # issue_type == 其他
    if days <= NO_REASON_WINDOW:
        return {**base, "allowed": True,
                "policy_basis": f"签收 {days} 天，可按7天无理由或质量问题流程办理",
                "shipping_fee": "视缘由：质量问题商家承担，无理由买家承担",
                "suggestion": "请顾客说明具体原因并补充凭证后办理。"}
    if days <= QUALITY_WINDOW:
        return {**base, "allowed": False,
                "policy_basis": f"签收 {days} 天：超出7天无理由；质量问题退换需凭证",
                "suggestion": "请顾客提供照片/视频证明质量故障，确认后按质量问题流程办理。"}
    return {**base, "allowed": False,
            "policy_basis": f"签收已 {days} 天，超出无理由与质量包退换期限",
            "suggestion": "保修期内可免费维修；不可承诺退款。"}


# ---------------------------------------------------------------- 工具注册表（OpenAI function calling 格式）
TOOL_IMPL = {
    "query_order": query_order,
    "query_product": query_product,
    "search_knowledge": search_knowledge,
    "recommend_products": recommend_products,
    "create_ticket": create_ticket,
    "apply_after_sales": apply_after_sales,
}

TOOLS = [
    {
        "name": "query_order",
        "description": "查询订单：状态、物流、退款/售后进度、订单明细、关联工单。"
                       "顾客提供订单号时优先用 order_no（支持后缀匹配）；"
                       "也可按 customer_id 查该顾客名下全部订单。",
        "parameters": {
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "订单号，如 SO2610120001"},
                "customer_id": {"type": "string", "description": "顾客ID，如 C001"},
            },
        },
    },
    {
        "name": "query_product",
        "description": "查询商品：价格、库存、评分、参数、在售状态。"
                       "支持 sku 精确查询或 keyword/category 模糊查询。",
        "parameters": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "商品SKU，如 SKU-1001"},
                "keyword": {"type": "string", "description": "关键词（名称/标签/描述）"},
                "category": {"type": "string",
                             "description": "类目：音频/充电/智能穿戴/家居/手机配件/生活电器"},
            },
        },
    },
    {
        "name": "search_knowledge",
        "description": "检索店铺知识库：退换货政策、配送与运费、发票/会员/优惠券FAQ、"
                       "价保规则、商品使用注意事项。凡政策规则类问题必须先检索。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词，如：价保 发货时效"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "recommend_products",
        "description": "按场景/预算/类目推荐在售商品（最多3个，附推荐理由）。",
        "parameters": {
            "type": "object",
            "properties": {
                "use_case": {"type": "string", "description": "使用场景，如：跑步 耳机"},
                "budget": {"type": "number", "description": "预算上限（元）"},
                "category": {"type": "string", "description": "类目"},
            },
        },
    },
    {
        "name": "apply_after_sales",
        "description": "售后申请政策校验（退货/换货/维修/仅退款）。自动计算签收天数并"
                       "给出是否符合政策；通过时自动登记工单。申请售后前必须先调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "订单号"},
                "issue_type": {"type": "string", "enum": ["质量问题", "无理由", "其他"],
                               "description": "问题类型"},
                "request": {"type": "string",
                            "enum": ["退货", "换货", "维修", "仅退款"],
                            "description": "顾客诉求（默认退货）"},
                "reason": {"type": "string", "description": "问题简述，如：电池鼓包"},
            },
            "required": ["order_no", "issue_type"],
        },
    },
    {
        "name": "create_ticket",
        "description": "登记人工工单：改地址、催发货、查件、投诉、发票、转人工等"
                       "需要人工处理的诉求。",
        "parameters": {
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "关联订单号（可空）"},
                "customer_id": {"type": "string", "description": "顾客ID（可空）"},
                "type": {"type": "string",
                         "enum": ["查件", "催发货", "退货", "换货", "维修", "仅退款",
                                  "改地址", "发票", "投诉", "转人工", "其他"]},
                "priority": {"type": "string", "enum": ["低", "中", "高"]},
                "description": {"type": "string", "description": "工单描述，改地址需含新地址"},
            },
            "required": ["type"],
        },
    },
]


def tools_schema() -> list[dict]:
    return [{"type": "function", "function": spec} for spec in TOOLS]


def _is_empty_result(result) -> bool:
    """业务空结果判定：命中则走短 TTL 空值缓存（防穿透）。"""
    if not isinstance(result, dict):
        return False
    if result.get("found") is False:
        return True
    if result.get("recommendations") == []:
        return True
    if result.get("results") == [] and "hint" in result:
        return True
    return False


def execute_tool(name: str, arguments: dict, cfg: dict) -> tuple[dict, str | None]:
    """
    工具调用统一管道（借鉴 EchoMind MCPToolManager.call）：
      参数校验 → 熔断检查 → 缓存命中 → 线程池执行（超时控制）→ 写缓存 → 统计
    熔断打开 / 超时 / 执行异常 三种情况走 fallback 降级结果，绝不抛出中断 Agent 循环。
    """
    init_runtime(cfg)
    fn = TOOL_IMPL.get(name)
    if fn is None:
        return {"error": f"未知工具: {name}"}, "unknown_tool"
    breaker = _BREAKERS[name]

    # 1) 参数校验：字符串参数合法性（防恶意查询穿透缓存与数据库）
    for key, val in (arguments or {}).items():
        if isinstance(val, str):
            err = _CACHE.validate_query(val) if _CACHE else None
            if err:
                return {"error": f"参数 {key} 非法：{err}"}, "bad_arguments"

    # 2) 熔断检查
    if not breaker.allow():
        return _fallback_result(name, "熔断保护中"), "breaker_open"

    # 3) 缓存
    cacheable = name in CACHEABLE and _CACHE
    cache_key = _CACHE.cache_key(name, json.dumps(arguments, sort_keys=True,
                                                 ensure_ascii=False)) if cacheable else ""
    if cacheable:
        hit, cached = _CACHE.get(cache_key)
        if hit:
            monitor.STATS.record_tool(name, True, 0)
            return cached, None

    # 4) 线程池执行 + 超时（per-tool 覆盖：RAG 链路含改写+重排两次 LLM 调用，需更长预算）
    t0 = time.time()
    args = {k: v for k, v in (arguments or {}).items() if v is not None}
    default_timeout = cfg.get("breaker", {}).get("tool_timeout_seconds", 30)
    timeout_s = {"search_knowledge": 75}.get(name, default_timeout)
    try:
        future = _TOOL_POOL.submit(fn, cfg, **args)
        result = future.result(timeout=timeout_s)
    except FuturesTimeout:
        future.cancel()
        breaker.record_failure()
        monitor.STATS.record_tool(name, False, 99999)
        return _fallback_result(name, "执行超时"), "timeout"
    except TypeError as e:
        return {"error": f"参数错误: {e}"}, "bad_arguments"
    except Exception as e:  # noqa: BLE001 —— 工具层兜底
        breaker.record_failure()
        monitor.STATS.record_tool(name, False, int((time.time() - t0) * 1000))
        return _fallback_result(name, f"执行异常: {type(e).__name__}"), "tool_error"

    # 5) 成功 → 写缓存 + 熔断计数恢复 + 统计
    breaker.record_success()
    monitor.STATS.record_tool(name, True, int((time.time() - t0) * 1000))
    if cacheable:
        _CACHE.set(cache_key, result, is_null=_is_empty_result(result))
    return result, None
