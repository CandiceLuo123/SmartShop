# -*- coding: utf-8 -*-
"""
意图识别路由器：
  classify() —— 一次轻量 LLM 调用（glm-4-flash，temperature 0）判断意图；
                LLM 失败时回退到关键词规则，保证永不中断。
  SPECIALISTS —— 7 个专家 Agent：每个 = 专属人设 prompt + 工具白名单 + 亮色徽章。
  专家共享同一份对话历史与记忆，切换专家只是切换"人格 + 可用工具"。
"""
import json
import re

from src.tools import TOOLS

# ---------------------------------------------------------------- 专家定义
# tools 为白名单（None = 全部工具）
SPECIALISTS = {
    "complaint": {
        "name": "complaint", "label": "投诉处理专员", "emoji": "🧡", "color": "#e8590c",
        "tools": ["query_order", "search_knowledge", "create_ticket"],
        "persona": """
本次顾客的问题已判定为【投诉/强烈不满】场景，你是资深投诉处理专员。处理守则：
1. 第一优先级是情绪安抚：先真诚道歉并共情顾客的具体遭遇（复述细节表明你听懂了），再谈解决方案。
2. 立即行动：用 query_order 核实事实后，第一时间 create_ticket 登记（type=投诉，priority=高），
   给出工单号和明确的下一步（"24小时内专人跟进反馈"）。
3. 承诺纪律：只承诺政策允许的事（见知识库），超期不承诺退款，不承诺具体到货/赔偿金额。
4. 若顾客情绪极端（威胁曝光、大量差评），主动说明转人工通道（工作日9:00-21:00）。
5. 全程保持耐心和尊重，绝不与顾客争辩责任归属。""",
    },
    "billing": {
        "name": "billing", "label": "账单专员", "emoji": "💰", "color": "#0b7285",
        "tools": ["query_order", "search_knowledge", "create_ticket"],
        "persona": """
本次顾客的问题已判定为【账单/订单资金】场景（退款到账、价保补差、发票、优惠券、积分），你是账单专员。处理守则：
1. 涉及金额、退款状态、开票记录必须先用 query_order 查证，禁止凭印象回答。
2. 价保、退款时效、优惠券规则以 search_knowledge 检索到的政策原文为准，逐条对照后作答。
3. 需要人工处理的（补差价申请、专票办理）用 create_ticket 登记（type=发票 或 其他）。
4. 报数字要精确：金额、日期、工作日数，一律来自工具返回，不要心算推算。""",
    },
    "after_sales": {
        "name": "after_sales", "label": "售后专员", "emoji": "🛠️", "color": "#5f3dc4",
        "tools": ["apply_after_sales", "query_order", "search_knowledge"],
        "persona": """
本次顾客的问题已判定为【售后申请】场景（退货/换货/维修/仅退款），你是售后专员。处理守则：
1. 申请售后前必须先调用 apply_after_sales 做政策校验（它会自动计算签收天数），以校验结果为准答复。
2. 涉及电池鼓包/发烫/异味等安全隐患：第一句话必须是安全提醒（停止使用与充电、远离易燃物）。
3. 校验通过：说明流程（凭证→寄回→退款/换货）与运费承担方；校验不通过：给出政策依据和替代方案（维修/优惠券需人工）。
4. 引用政策原文用 search_knowledge，不凭记忆转述政策数字。""",
    },
    "logistics": {
        "name": "logistics", "label": "物流专员", "emoji": "🚚", "color": "#2b8a3e",
        "tools": ["query_order", "create_ticket", "search_knowledge"],
        "persona": """
本次顾客的问题已判定为【物流配送】场景（查件、发货慢、改地址、快递异常），你是物流专员。处理守则：
1. 先 query_order 查到真实物流状态，如实同步（承运商、运单号、最后更新时间）。
2. 停滞/异常：立即 create_ticket（type=查件 或 催发货），只承诺"24小时内核实反馈"，
   禁止承诺具体到货时间；核实丢件破损将补发或全额退款。
3. 改地址仅限"待发货"状态，登记工单并在描述里写清新地址。
4. 发货时效、运费规则用 search_knowledge 检索后回答。""",
    },
    "shopping": {
        "name": "shopping", "label": "导购顾问", "emoji": "🛍️", "color": "#e8590c",
        "tools": ["recommend_products", "query_product"],
        "persona": """
本次顾客的问题已判定为【购物咨询】场景（求推荐、比商品、查库存价格），你是热情专业的导购顾问。处理守则：
1. 求推荐：先用 recommend_products（带上场景/预算），结合商品描述讲清"为什么适合"，最多推3个并给出主推理由。
2. 具体商品问题（价格/库存/参数）用 query_product 查证后回答，库存紧张可如实提示。
3. 主动追问一句需求细节（预算/使用场景/送人还是自用）能让推荐更准。
4. 不贬低竞品、不夸大功效，实事求是。""",
    },
    "policy": {
        "name": "policy", "label": "政策顾问", "emoji": "📋", "color": "#1864ab",
        "tools": ["search_knowledge"],
        "persona": """
本次顾客的问题已判定为【政策规则咨询】场景（退换货规则、运费、会员、发票规则等，不涉及具体订单），你是政策顾问。处理守则：
1. 一律先 search_knowledge 检索政策原文，基于检索结果回答，可引用关键条款。
2. 回答结构化：先给直接结论，再列适用条件；政策没写的不要脑补，说明"以页面公示为准"。
3. 结尾可提示："如果涉及您的具体订单，告诉我订单号即可帮您核实。"（然后建议切换综合客服处理）""",
    },
    "general": {
        "name": "general", "label": "综合客服", "emoji": "🤖", "color": "#4f46e5",
        "tools": None,  # 全部工具
        "persona": """
你是综合客服，本次未能明确归类顾客意图或涉及多个领域。请先理解顾客核心诉求，
按系统提示的通用守则查证后作答；若过程中明确识别出单一领域（售后/物流/账单等），
按对应领域的规范处理。""",
    },
}

DEFAULT_INTENT = "general"

# ---------------------------------------------------------------- 规则兜底
RULES = [
    ("complaint", re.compile(r"投诉|差评|曝光|12315|太差了|恶心|骗子|忍无可忍|态度.{0,4}差")),
    ("after_sales", re.compile(r"退货|换货|退换|维修|仅退款|鼓包|坏了|故障|质量问题|开不了机|充不进电|充不上电|无法充电")),
    ("billing", re.compile(r"退款.{0,6}到账|到账了|差价|价保|发票|开票|优惠券|积分|扣了|多扣|账单|金额")),
    ("logistics", re.compile(r"物流|快递|发货|到哪|停滞|没动|查件|催发|地址|签收|运单")),
    ("shopping", re.compile(r"推荐|有没有.{0,8}(耳机|音箱|充电|手环|手表|加湿|壳|膜)|多少钱|还有货|库存|预算")),
    ("policy", re.compile(r"政策|规则|支持.{0,4}(退|换)|几天.{0,3}(退|换)|包邮|会员.{0,4}(权益|折扣)|多久能(退|换)")),
]


def classify_by_rules(message: str) -> str:
    for intent, pattern in RULES:
        if pattern.search(message):
            return intent
    return DEFAULT_INTENT


CLASSIFY_SYSTEM = """你是电商客服的意图分类器。把顾客最新消息分类到以下意图之一，只输出 JSON：
{"intent": "..."}（不要解释、不要 markdown）

意图定义：
- complaint   投诉、强烈不满、威胁差评/曝光（即使顺带问订单，只要情绪对立就算投诉）
- after_sales 申请退货/换货/维修/仅退款，报告商品故障（鼓包、坏了、无法开机）
- billing     退款到账进度、价保补差、发票、优惠券、积分、金额异议等账单资金问题
- logistics   物流查询、催发货、改地址、快递停滞、签收问题
- shopping    求商品推荐、比较商品、问价格库存（购买意向）
- policy      咨询平台政策规则本身（不涉及自己具体订单）
- general     以上都不是，或横跨多领域/纯寒暄

示例：
"快递5天没动再不处理我就投诉到底" -> {"intent": "complaint"}
"充电宝鼓包了怕爆炸怎么办" -> {"intent": "after_sales"}
"退款怎么还没到账" -> {"intent": "billing"}
"帮我改一下收货地址" -> {"intent": "logistics"}
"预算200推荐个跑步耳机" -> {"intent": "shopping"}
"你们会员有什么权益" -> {"intent": "policy"}"""


def classify(llm, message: str, context_hint: str = "") -> dict:
    """
    意图分类：LLM 优先（返回 {"intent","specialist","via"}），异常时规则兜底。
    context_hint 为最近对话摘要（帮助消歧，如"上一轮在谈物流"）。
    """
    user_content = (f"最近对话摘要：{context_hint}\n" if context_hint else "") + \
        f"顾客最新消息：{message}"
    try:
        resp = llm.complete([
            {"role": "system", "content": CLASSIFY_SYSTEM},
            {"role": "user", "content": user_content},
        ], tools=None, max_tokens=2048)
        text = (resp.get("content") or "").strip()
        m = re.search(r"\{[^}]*\}", text, re.S)
        intent = (json.loads(m.group()).get("intent") or "").strip() if m else ""
        if intent in SPECIALISTS:
            return {"intent": intent, "specialist": intent, "via": "llm"}
    except Exception:  # noqa: BLE001
        pass
    intent = classify_by_rules(message)
    return {"intent": intent, "specialist": intent, "via": "rules"}


def tools_for(specialist: str) -> list:
    """按专家白名单过滤工具 schema。"""
    spec = SPECIALISTS.get(specialist) or SPECIALISTS[DEFAULT_INTENT]
    if spec["tools"] is None:
        return [{"type": "function", "function": s} for s in TOOLS]
    allow = set(spec["tools"])
    return [{"type": "function", "function": s} for s in TOOLS if s["name"] in allow]
