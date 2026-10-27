# -*- coding: utf-8 -*-
"""
LLM 后端：
  ZhipuLLM —— 智谱 GLM（OpenAI 兼容 chat/completions，urllib 零第三方依赖），
              支持模型回退列表、429/5xx 重试、Key 从环境变量或 .env 读取。
  MockLLM   —— 离线自检后端（不联网），用于在没有 Key 时验证 Agent 管道。
"""
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ModelNotAvailable(Exception):
    """模型名不存在或无权限，应尝试回退列表中的下一个模型。"""


class LLMError(Exception):
    """调用失败（认证/网络/服务端错误）。"""


def load_env_file(path: Path) -> None:
    """简易 .env 加载：不覆盖已有环境变量。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def find_api_key(config: dict):
    """按 config.api.key_env 顺序寻找 API Key，找不到返回 None。"""
    load_env_file(ROOT / ".env")
    for name in config["api"]["key_env"]:
        value = os.environ.get(name, "").strip()
        if value and not value.lower().startswith("your_"):
            return value
    return None


class ZhipuLLM:
    """智谱 GLM 后端。模型按 config.api.models 顺序尝试，成功后粘性复用。"""

    def __init__(self, api_key: str, config: dict):
        self.api_key = api_key
        self.base_url = config["api"]["base_url"].rstrip("/")
        self.models = list(config["api"]["models"])
        self.timeout = config["api"]["timeout_seconds"]
        self.max_retries = config["api"]["max_retries"]
        self.temperature = config["agent"]["temperature"]
        self.max_tokens = config["agent"]["max_tokens"]
        self.model_in_use = None

    # ---------- HTTP ----------
    def _post(self, model: str, payload: dict) -> dict:
        url = f"{self.base_url}/chat/completions"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_err: Exception = LLMError("未知错误")
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")
                except Exception:
                    pass
                # 1113/1211 等：模型不存在或无权限 → 触发回退
                if e.code == 400 and re.search(r"model|模型", body, re.I):
                    raise ModelNotAvailable(f"{model}: HTTP 400 {body[:300]}")
                if e.code in (401, 403):
                    raise LLMError(f"认证失败(HTTP {e.code})，请检查 API Key：{body[:300]}")
                if e.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    # 429 限流需要更长退避（等待配额窗口），其余网络错误短退避
                    last_err = LLMError(f"HTTP {e.code}: {body[:200]}")
                    time.sleep(6.0 * (attempt + 1) if e.code == 429
                               else 1.5 * (attempt + 1))
                    continue
                raise LLMError(f"HTTP {e.code}: {body[:300]}")
            except (urllib.error.URLError, ConnectionError, OSError,
                    http.client.IncompleteRead) as e:
                # RemoteDisconnected/IncompleteRead/超时等网络层错误也重试
                if attempt < self.max_retries:
                    last_err = LLMError(f"网络错误: {e}")
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise LLMError(f"网络错误: {e}")
        raise last_err

    # ---------- 对外接口 ----------
    def complete(self, messages: list, tools: list | None = None,
                 max_tokens: int | None = None) -> dict:
        order = ([self.model_in_use] if self.model_in_use else []) + [
            m for m in self.models if m != self.model_in_use
        ]
        last_exc: Exception | None = None
        for model in order:
            payload = {
                "model": model,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": max_tokens or self.max_tokens,
            }
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"
            try:
                data = self._post(model, payload)
            except ModelNotAvailable as e:
                last_exc = e
                continue
            self.model_in_use = model
            return self._normalize(data, model)
        raise last_exc or LLMError("所有候选模型均不可用")

    @staticmethod
    def _normalize(data: dict, model: str) -> dict:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = msg.get("content")
        if content:
            # 思考型模型可能泄漏 <think> 段落或残留 </think> 标签，只取其后正文
            if "</think>" in content:
                content = content.rsplit("</think>", 1)[-1]
            content = content.replace("<think>", "").strip()
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    args = {"_raw": args}
            tool_calls.append({
                "id": tc.get("id") or f"call_{len(tool_calls)}",
                "name": fn.get("name"),
                "arguments": args if isinstance(args, dict) else {},
            })
        return {
            "content": msg.get("content"),
            "tool_calls": tool_calls,
            "finish_reason": choice.get("finish_reason"),
            "model": model,
            "usage": data.get("usage", {}),
        }


class MockLLM:
    """
    离线自检后端（不联网）：
      首轮按关键词模拟一次工具调用，收到工具结果后输出摘要答复。
      仅用于验证 Agent 循环、工具执行与结果落盘是否正常，评分不代表模型质量。
    """

    _ORDER_RE = re.compile(r"(SO\d{8,})")
    _SKU_RE = re.compile(r"(SKU-\d+)", re.I)

    def __init__(self, config: dict):
        self.config = config
        self.model_in_use = "mock"

    def complete(self, messages: list, tools: list | None = None) -> dict:
        made_calls = sum(
            1 for m in messages if m.get("role") == "assistant" and m.get("tool_calls"))
        if made_calls == 0:
            user = next((m.get("content") or "" for m in messages
                         if m.get("role") == "user"), "")
            tool, args = self._decide(user)
            if tool:
                return {"content": None, "tool_calls": [
                    {"id": "mock_1", "name": tool, "arguments": args}],
                    "finish_reason": "tool_calls", "model": "mock", "usage": {}}
            return {"content": "【MOCK】您好，我是小铺智答，请问有什么可以帮您？"
                    "（离线自检模式，仅验证管道）",
                    "tool_calls": [], "finish_reason": "stop",
                    "model": "mock", "usage": {}}
        last_tool = next((m.get("content") or "" for m in reversed(messages)
                          if m.get("role") == "tool"), "")
        return {"content": "【MOCK】查询结果："
                + last_tool[:180].replace("\n", " ")
                + "（离线自检模式，仅验证管道）",
                "tool_calls": [], "finish_reason": "stop",
                "model": "mock", "usage": {}}

    def _decide(self, text: str):
        m = self._ORDER_RE.search(text)
        if m:
            return "query_order", {"order_no": m.group(1)}
        m = self._SKU_RE.search(text)
        if m:
            return "query_product", {"sku": m.group(1).upper()}
        if any(k in text for k in ("退货", "换货", "退款", "维修", "鼓包")):
            return "search_knowledge", {"query": "退换货"}
        if any(k in text for k in ("发货", "快递", "发票", "价保", "会员", "优惠券")):
            return "search_knowledge", {"query": text[:20]}
        return None, {}


def build_llm(config: dict, backend: str = "auto"):
    """backend: auto（有 Key 用智谱，否则 Mock）/ zhipu / mock。"""
    if backend == "mock":
        return MockLLM(config), "mock"
    key = find_api_key(config)
    if backend == "zhipu" and not key:
        raise SystemExit("未找到 API Key：请在 .env 配置 ZHIPUAI_API_KEY 或设置环境变量")
    if key:
        return ZhipuLLM(key, config), "zhipu"
    return MockLLM(config), "mock"
