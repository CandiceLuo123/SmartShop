# -*- coding: utf-8 -*-
"""
Headless 批量评测（对应图片步骤 3/5）：
  tests/cases.json → 逐用例无界面运行 Agent → 自动评分
  → 每个用例完成即增量落盘 results/results.json（失败也更新状态）。

用法：
  python run_tests.py                    # 自动：有 Key 走 GLM，无 Key 走 Mock
  python run_tests.py --backend zhipu    # 强制真实 GLM
  python run_tests.py --only refund_progress
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.agent import AgentSession, load_config  # noqa: E402
from src.llm import build_llm, find_api_key  # noqa: E402
from src.tools import _rows  # noqa: E402
import sqlite3  # noqa: E402

DEFAULT_CASES = ROOT / "tests" / "cases.json"
DEFAULT_OUT = ROOT / "results" / "results.json"


# ================================================================
# 数据驱动样本解析：用例中的 {{占位符}} 启动时从真实数据库取值，
# 使评测不绑定具体演示数据（换库/换后端都不用改用例）。
# ================================================================
def resolve_samples(cfg: dict) -> dict:
    import src.tools as tools_mod
    from src.database import business_conn, q as dq, btable, ph as dph

    def one(sql_big, params=()):
        ot, it, pt = (btable(cfg, n) for n in ("orders", "order_items", "products"))
        ph_ = dph(cfg)
        sql = sql_big.format(o=ot, i=it, p=pt, ph=ph_)
        try:
            with business_conn(cfg) as conn:
                rows = dq(conn, sql, params)
            return rows[0] if rows else None
        except Exception:  # noqa: BLE001 —— 样本解析失败降级为占位符原样
            return None

    s = {}
    stalled = one("SELECT order_no, customer_id, tracking_no, carrier FROM {o} "
                  "WHERE logistics_status='停滞' LIMIT 1")
    if stalled:
        s["stalled_order"] = stalled["order_no"]
        s["stalled_customer"] = stalled["customer_id"]
        s["stalled_tracking"] = stalled["tracking_no"]

    recent = one("SELECT order_no, customer_id, tracking_no FROM {o} "
                 "WHERE status='已签收' AND receipt_time >= "
                 "DATE_SUB(CURDATE(), INTERVAL 13 DAY) LIMIT 1") \
        if backend_is_mysql(cfg) else \
        _sqlite_sample(cfg, "已签收", 5, 13)
    if recent:
        s["recent_signed"] = recent.get("order_no") if isinstance(recent, dict) else recent[0]
        if isinstance(recent, dict) and recent.get("customer_id"):
            s["recent_signed_customer"] = recent["customer_id"]

    old = one("SELECT order_no FROM {o} WHERE status='已签收' AND receipt_time < "
              "DATE_SUB(CURDATE(), INTERVAL 60 DAY) LIMIT 1") \
        if backend_is_mysql(cfg) else _sqlite_sample(cfg, "已签收", 60, 999)
    if old:
        s["old_signed"] = old.get("order_no") if isinstance(old, dict) else old[0]

    refund = one("SELECT order_no FROM {o} WHERE status='已退款' LIMIT 1")
    if refund:
        s["refund_done"] = refund["order_no"]

    pending = one("SELECT order_no, customer_id FROM {o} WHERE status='待发货' LIMIT 1")
    if pending:
        s["pending_ship"] = pending["order_no"]
        s["pending_customer"] = pending["customer_id"]

    sku_row = one("SELECT sku, name FROM {p} WHERE stock<=10 AND status='在售' LIMIT 1")
    if sku_row:
        s["low_stock_sku"] = sku_row["sku"]

    ear = one("SELECT sku FROM {p} WHERE status='在售' AND (name LIKE '%跑步%' "
              "OR tags LIKE '%运动%') AND category='音频' AND price<=200 LIMIT 1")
    if ear:
        s["sport_earphone_sku"] = ear["sku"]

    return s


def backend_is_mysql(cfg: dict) -> bool:
    from src.database import backend_name
    return backend_name(cfg) == "mysql"


def _sqlite_sample(cfg: dict, status: str, min_days: int, max_days: int):
    """SQLite 后端的日期窗口样本（演示库/业务库均可）。"""
    import sqlite3
    from contextlib import closing
    from src.database import btable
    bt = btable(cfg, "orders")
    with closing(sqlite3.connect(ROOT / cfg["paths"]["db"])) as main:
        main.row_factory = sqlite3.Row
        try:
            rows = _rows_dict(main, f"""
                SELECT order_no FROM {bt}
                WHERE status=? AND receipt_time IS NOT NULL
                  AND julianday('2026-10-25') - julianday(receipt_time) BETWEEN ? AND ?
                LIMIT 1""", (status, min_days, max_days))
            return rows[0] if rows else None
        except sqlite3.OperationalError:
            return None


def _rows_dict(conn, sql, params):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def apply_placeholders(cases: list[dict], samples: dict) -> list[dict]:
    """把 {{key}} 替换为样本值；无样本的用例跳过（报告中标记 skipped）。"""
    import copy
    out, skipped = [], []
    for c in cases:
        text = json.dumps(c, ensure_ascii=False)
        missing = [k for k in re.findall(r"\{\{(\w+)\}\}", text)
                   if k not in samples]
        if missing:
            skipped.append({"id": c["id"], "missing": missing})
            continue
        for k, v in samples.items():
            text = text.replace("{{" + k + "}}", str(v))
        out.append(json.loads(text))
    return out, skipped


# ================================================================


def _squeeze(text: str) -> str:
    """去除空白，用于中英文混排时的宽松包含匹配（如 '48 小时' vs '48小时'）。"""
    return re.sub(r"\s+", "", text or "")


def grade(case: dict, result: dict) -> tuple[bool, list[str]]:
    """按用例 checks 评分：路由、工具调用、关键词（组内任一）、禁用表述。"""
    checks = case.get("checks", {})
    reasons = []
    ok = True
    tools = result.get("tool_calls") or []
    answer = result.get("answer") or ""
    route = (result.get("route") or {}).get("specialist")

    for r in checks.get("expect_route", []):
        if route != r:
            ok = False
            reasons.append(f"路由错误: 期望 {r}，实际 {route}")
    for group in checks.get("expect_route_any", []):
        if route not in group:
            ok = False
            reasons.append(f"路由错误: 期望 {'/'.join(group)} 之一，实际 {route}")
    for t in checks.get("expect_tools", []):
        if t not in tools:
            ok = False
            reasons.append(f"缺少工具调用: {t}")
    for group in checks.get("expect_tools_any", []):
        if not any(t in tools for t in group):
            ok = False
            reasons.append("未调用任一工具: " + "/".join(group))
    for group in checks.get("expect_keywords_any", []):
        if not any(k in answer or _squeeze(k) in _squeeze(answer) for k in group):
            ok = False
            reasons.append("回答缺少关键词(任一): " + "/".join(group))
    for k in checks.get("forbid_keywords", []):
        if k in answer or _squeeze(k) in _squeeze(answer):
            ok = False
            reasons.append(f"出现禁用表述: {k}")

    if result.get("status") != "ok":
        ok = False
        reasons.append(
            f"运行状态异常: {result.get('status')} {result.get('error') or ''}".strip())
    return ok, reasons


# ================================================================
# 评测扩展：意图分类 F1 / LLM Judge 四维评分 / 滚动基线回归（借鉴 EchoMind 4.8）
# ================================================================
def intent_eval(cfg: dict, cases: list[dict], llm) -> dict:
    """意图识别评测：手写 Accuracy / Precision / Recall / F1 / Macro-F1（不依赖 sklearn）。"""
    from src import router
    predictions, truth_sets = [], []
    for c in cases:
        gt = c.get("checks", {}).get("expect_route") or \
             c.get("checks", {}).get("expect_route_any") or []
        if not gt:
            continue
        predictions.append(router.classify(llm, c["message"])["intent"])
        # expect_route_any 是"列表的列表"（[[...]]），展平为集合
        flat = gt[0] if gt and isinstance(gt[0], list) else gt
        truth_sets.append(set(flat) if isinstance(flat, list) else {flat})

    n = len(predictions)
    correct = sum(1 for p, ts in zip(predictions, truth_sets) if p in ts)
    accuracy = correct / n if n else 0.0
    labels = sorted({l for ts in truth_sets for l in ts} | set(predictions))
    per_class = {}
    for label in labels:
        tp = sum(1 for p, ts in zip(predictions, truth_sets) if p == label and label in ts)
        fp = sum(1 for p, ts in zip(predictions, truth_sets) if p == label and label not in ts)
        fn = sum(1 for p, ts in zip(predictions, truth_sets) if p != label and label in ts)
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        per_class[label] = {"precision": round(prec, 3), "recall": round(rec, 3),
                            "f1": round(f1, 3), "support": tp + fn}
    macro_f1 = round(sum(v["f1"] for v in per_class.values()) / len(per_class), 3) \
        if per_class else 0.0
    return {"accuracy": round(accuracy, 3), "macro_f1": macro_f1,
            "per_class": per_class, "n": n}


JUDGE_PROMPT = """你是一个电商客服质量评估专家。请对以下客服响应进行评分。
用户问题: {question}
Agent 响应: {response}
{context_section}
请从以下四个维度评分（0.0-1.0），返回 JSON（不要 markdown）：
{{"relevance": 0.0, "accuracy": 0.0, "completeness": 0.0, "helpfulness": 0.0}}
- relevance: 响应是否直接针对用户问题
- accuracy: 信息是否准确无误（与查询结果一致，无编造）
- completeness: 是否完整解决了用户需求
- helpfulness: 用户能否据此采取下一步行动"""


def llm_judge(llm, case_result: dict) -> dict:
    """LLM-as-Judge：四维 0-1 打分，overall 取均值（借鉴 EchoMind LLMJudge）。失败重试一次。"""
    prompt = JUDGE_PROMPT.format(
        question=case_result.get("message", "")[:300],
        response=case_result.get("answer", "")[:600], context_section="")
    last_err = None
    for _ in range(2):
        try:
            resp = llm.complete([{"role": "user", "content": prompt}],
                                tools=None, max_tokens=4096)
            text = (resp.get("content") or "").strip()
            # 容错提取 JSON 对象（小模型可能夹带解释文字/多行）
            m = re.search(r"\{[^{}]*\"relevance\"[^{}]*\}", text, re.S)
            if not m:
                raise ValueError(f"未找到评分 JSON: {text[:80]}")
            scores = json.loads(m.group())
            overall = sum(float(scores[k]) for k in
                          ("relevance", "accuracy", "completeness", "helpfulness")) / 4
            return {"scores": scores, "overall": round(overall, 3)}
        except Exception as e:  # noqa: BLE001 —— 评审失败不判死刑
            last_err = str(e)[:100]
    return {"scores": None, "overall": None, "error": last_err}


def load_baseline(path: Path) -> dict | None:
    """读取上次评测的滚动基线（results/baseline.json），无则返回 None。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def detect_regressions(current: dict, baseline: dict | None) -> list[str]:
    """滚动基线回归：任一指标较基线退化 >5% 即报告（借鉴 EchoMind）。"""
    out = []
    if not baseline:
        return out
    prev = baseline.get("avg_scores", {})
    for metric, value in current.items():
        old = prev.get(metric)
        if old and old > 0 and value is not None:
            delta = (value - old) / old
            if delta < -0.05:
                out.append(f"{metric}: {old} → {value} (退化 {abs(delta):.1%})")
    return out


def routes_ok(turn: dict, result: dict) -> bool:
    """单轮路由断言（多轮用例内部使用）。"""
    expect = turn.get("expect_route")
    if not expect:
        return True
    return (result.get("route") or {}).get("specialist") == expect


def grade_multi_turn(case: dict, result: dict) -> tuple[bool, list[str]]:
    """多轮 E2E 评分：逐轮路由 + 最终回答关键词/禁用词 + 运行状态。
    关键词检查覆盖多轮全部回答（记忆可能体现在任意一轮）。"""
    reasons = []
    ok = True
    for i, r in enumerate(result.get("routes") or []):
        if not r["match"]:
            ok = False
            reasons.append(f"第{i + 1}轮路由错误: 期望 {r['expect']}，实际 {r['route']}")
    # 多轮：从结果 trace/answer 聚合所有轮次的回答文本
    answers = result.get("multi_answers") or [result.get("answer") or ""]
    combined = " ".join(answers)
    checks = case.get("checks", {})
    for group in checks.get("expect_keywords_any", []):
        if not any(k in combined or _squeeze(k) in _squeeze(combined) for k in group):
            ok = False
            reasons.append("多轮回答缺少关键词(任一): " + "/".join(group))
    for k in checks.get("forbid_keywords", []):
        if k in combined or _squeeze(k) in _squeeze(combined):
            ok = False
            reasons.append(f"出现禁用表述: {k}")
    if result.get("status") != "ok":
        ok = False
        reasons.append(
            f"运行状态异常: {result.get('status')} {result.get('error') or ''}".strip())
    return ok, reasons


def summarize(records: list[dict]) -> dict:
    passed = sum(1 for r in records if r["pass"])
    return {"total": len(records), "done": len(records),
            "passed": passed, "failed": len(records) - passed,
            "pass_rate": f"{passed / len(records) * 100:.0f}%" if records else "-"}


def write_out(out_path: Path, doc: dict) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    tmp.replace(out_path)  # 原子替换，避免中断产生半截文件


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(DEFAULT_CASES))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--only", default=None, help="只跑指定 id 的用例（逗号分隔）")
    ap.add_argument("--backend", default="auto", choices=["auto", "zhipu", "mock"])
    args = ap.parse_args()

    cfg = load_config()
    cases_raw = json.loads(Path(args.cases).read_text(encoding="utf-8"))["cases"]
    if args.only:
        keep = {s.strip() for s in args.only.split(",") if s.strip()}
        cases_raw = [c for c in cases_raw if c["id"] in keep]
        missing = keep - {c["id"] for c in cases_raw}
        if missing:
            raise SystemExit(f"未知用例 id: {', '.join(sorted(missing))}")

    # 数据驱动：{{占位符}} → 真实数据库样本（MySQL/SQLite 均支持）
    samples = resolve_samples(cfg)
    cases, skipped_cases = apply_placeholders(cases_raw, samples)
    if samples:
        print(f"[样本解析] {len(samples)} 个占位符来自真实数据库: "
              + ", ".join(f"{k}={v}" for k, v in list(samples.items())[:6]) + " ...")
    if skipped_cases:
        print(f"[跳过] {len(skipped_cases)} 个用例缺少样本: "
              + ", ".join(s['id'] for s in skipped_cases))
    if not cases:
        raise SystemExit("没有可执行的用例")

    try:
        llm, backend = build_llm(cfg, args.backend)
    except SystemExit as e:
        print(e)
        sys.exit(2)

    key_found = find_api_key(cfg) is not None
    effective = backend if backend != "auto" else ("zhipu" if key_found else "mock")
    if effective == "mock":
        print("[提示] 未检测到 API Key → 离线 Mock 后端，仅验证管道连通，"
              "评分不代表 GLM 真实质量。\n")

    out_path = Path(args.out)
    doc = {
        "run_id": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "backend": effective,
        "model": None,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": None,
        "status": "running",
        "summary": summarize([]),
        "cases": [],
        "note": ("mock 后端仅验证工具与落盘管道" if effective == "mock" else None),
    }
    write_out(out_path, doc)  # 开跑先落盘（running 状态）

    print(f"{'用例':<28}{'结果':<6}{'路由':<13}{'轮次':<4}{'工具':<5}耗时ms")
    print("-" * 86)
    for case in cases:
        t0 = time.time()
        session = AgentSession(cfg, llm, customer_id=case.get("customer_id"))
        routes, last_result = [], None
        if case.get("type") == "multi_turn":
            # 端到端多轮：逐轮发送，收集每轮路由与回答，最后一轮参与评分
            multi_answers = []
            for turn in case["turns"]:
                last_result = session.run(turn["message"])
                multi_answers.append(last_result.get("answer") or "")
                routes.append({
                    "message": turn["message"],
                    "route": (last_result.get("route") or {}).get("specialist"),
                    "expect": turn.get("expect_route"),
                    "match": routes_ok(turn, last_result),
                })
            result = dict(last_result or {})
            result["routes"] = routes
            result["multi_answers"] = multi_answers
            elapsed_ms = int((time.time() - t0) * 1000)
            passed, reasons = grade_multi_turn(case, result)
        else:
            result = session.run(case["message"])
            elapsed_ms = int((time.time() - t0) * 1000)
            passed, reasons = grade(case, result)
        if result.get("model") and not doc["model"]:
            doc["model"] = result["model"]

        doc["cases"].append({
            "id": case["id"],
            "scenario": case.get("scenario", ""),
            "type": case.get("type", "single_turn"),
            "customer_id": case.get("customer_id"),
            "message": case.get("message") or " → ".join(
                t["message"] for t in case.get("turns", [])),
            "turns": (case.get("turns") if case.get("type") == "multi_turn" else None),
            "routes": result.get("routes"),
            "expect": case.get("expect", ""),
            "pass": passed,
            "reasons": reasons,
            "answer": result["answer"],
            "route": result.get("route"),
            "status": result["status"],
            "error": result["error"],
            "tool_calls": result["tool_calls"],
            "rounds": result["rounds"],
            "trace": result["trace"],
            "elapsed_ms": elapsed_ms,
        })
        doc["summary"] = summarize(doc["cases"])
        doc["updated_at"] = datetime.now().isoformat(timespec="seconds")
        write_out(out_path, doc)  # 每个用例完成即落盘，失败也更新

        mark = "PASS" if passed else "FAIL"
        tools = ",".join(dict.fromkeys(result["tool_calls"])) or "-"
        route = (result.get("route") or {}).get("specialist") or "-"
        multi = " multi×%d" % len(result["routes"]) if result.get("routes") else ""
        print(f"{case['id']:<28}{mark:<6}{route:<13}{result['rounds']:<4}"
              f"{len(result['tool_calls']):<5}{elapsed_ms}{multi}")
        if not passed:
            for r in reasons:
                print(f"    ✗ {r}")

    doc["status"] = "completed"
    doc["updated_at"] = datetime.now().isoformat(timespec="seconds")
    doc["summary"] = summarize(doc["cases"])
    write_out(out_path, doc)

    # ---------------- 扩展评测：意图 F1 + LLM Judge + 滚动基线 ----------------
    if effective != "mock":
        print("\n[扩展评测] 意图分类指标计算中...")
        try:
            ie = intent_eval(cfg, cases, llm)
            doc["intent_eval"] = ie
            print(f"  意图: accuracy={ie['accuracy']} macro_f1={ie['macro_f1']} (n={ie['n']})")
            for label, v in ie["per_class"].items():
                print(f"    {label:<14} P={v['precision']} R={v['recall']} "
                      f"F1={v['f1']} (n={v['support']})")
        except Exception as e:  # noqa: BLE001
            print(f"  意图评测失败: {e}")

        print("[扩展评测] LLM Judge 四维评分中（每例一次调用）...")
        PASS_THRESHOLD = 0.75
        dims = ("relevance", "accuracy", "completeness", "helpfulness")
        avg_scores = {d: [] for d in dims}
        judged = []
        for c in doc["cases"]:
            j = llm_judge(llm, c)
            if j["overall"] is not None:
                for d in dims:
                    avg_scores[d].append(j["scores"][d])
                c["judge"] = j
                judged.append(j)
                mark = "PASS" if j["overall"] >= PASS_THRESHOLD else "WEAK"
                print(f"  {c['id']:<28} overall={j['overall']} {mark}")
            else:
                print(f"  {c['id']:<28} judge 失败: {j.get('error')}")
        if judged:
            avg = {d: round(sum(v) / len(v), 3) for d, v in avg_scores.items() if v}
            overall_avg = round(sum(j["overall"] for j in judged) / len(judged), 3)
            doc["judge_summary"] = {"avg_scores": avg,
                                    "overall": overall_avg,
                                    "pass_threshold": PASS_THRESHOLD,
                                    "n": len(judged)}
            print(f"  Judge 总评: overall={overall_avg} "
                  f"(及格线 {PASS_THRESHOLD}) avg={avg}")

            # 滚动基线回归
            baseline_path = ROOT / "results" / "baseline.json"
            baseline = load_baseline(baseline_path)
            regressions = detect_regressions(avg, baseline)
            doc["regressions"] = regressions
            if regressions:
                print("  ⚠️ 回归检测（较上次基线退化 >5%）:")
                for r in regressions:
                    print(f"    {r}")
            else:
                print("  回归检测: 无退化（或首次运行建立基线）")
            write_out(out_path, doc)
            # 本次结果成为新基线（滚动基线策略）
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(json.dumps(
                {"updated_at": doc["updated_at"], "avg_scores": avg},
                ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  基线已更新: {baseline_path.name}")

    s = doc["summary"]
    print("-" * 86)
    print(f"完成: {s['total']} 个用例，通过 {s['passed']}，失败 {s['failed']}"
          f"（通过率 {s['pass_rate']}，backend={effective}）")
    print(f"结果已写入: {out_path}")

    sys.exit(0 if (s["failed"] == 0 or effective == "mock") else 1)


if __name__ == "__main__":
    main()
