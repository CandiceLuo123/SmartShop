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

DEFAULT_CASES = ROOT / "tests" / "cases.json"
DEFAULT_OUT = ROOT / "results" / "results.json"


def _squeeze(text: str) -> str:
    """去除空白，用于中英文混排时的宽松包含匹配（如 '48 小时' vs '48小时'）。"""
    return re.sub(r"\s+", "", text or "")


def grade(case: dict, result: dict) -> tuple[bool, list[str]]:
    """按用例 checks 评分：工具调用、关键词（组内任一命中）、禁用表述。"""
    checks = case.get("checks", {})
    reasons = []
    ok = True
    tools = result.get("tool_calls") or []
    answer = result.get("answer") or ""

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
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))["cases"]
    if args.only:
        keep = {s.strip() for s in args.only.split(",") if s.strip()}
        cases = [c for c in cases if c["id"] in keep]
        missing = keep - {c["id"] for c in cases}
        if missing:
            raise SystemExit(f"未知用例 id: {', '.join(sorted(missing))}")

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

    print(f"{'用例':<28}{'结果':<6}{'轮次':<4}{'工具':<6}耗时ms")
    print("-" * 72)
    for case in cases:
        t0 = time.time()
        session = AgentSession(cfg, llm, customer_id=case.get("customer_id"))
        result = session.run(case["message"])
        elapsed_ms = int((time.time() - t0) * 1000)
        passed, reasons = grade(case, result)
        if result.get("model") and not doc["model"]:
            doc["model"] = result["model"]

        doc["cases"].append({
            "id": case["id"],
            "scenario": case.get("scenario", ""),
            "customer_id": case.get("customer_id"),
            "message": case["message"],
            "expect": case.get("expect", ""),
            "pass": passed,
            "reasons": reasons,
            "answer": result["answer"],
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
        print(f"{case['id']:<28}{mark:<6}{result['rounds']:<4}{len(result['tool_calls']):<6}{elapsed_ms}")
        if not passed:
            for r in reasons:
                print(f"    ✗ {r}")

    doc["status"] = "completed"
    doc["updated_at"] = datetime.now().isoformat(timespec="seconds")
    doc["summary"] = summarize(doc["cases"])
    write_out(out_path, doc)

    s = doc["summary"]
    print("-" * 72)
    print(f"完成: {s['total']} 个用例，通过 {s['passed']}，失败 {s['failed']}"
          f"（通过率 {s['pass_rate']}，backend={effective}）")
    print(f"结果已写入: {out_path}")

    sys.exit(0 if (s["failed"] == 0 or effective == "mock") else 1)


if __name__ == "__main__":
    main()
