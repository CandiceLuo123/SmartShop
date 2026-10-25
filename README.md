# 小铺智答 —— 智谱 GLM 驱动的智能电商客服 Agent 平台

一个**核心零框架**（Agent 循环纯 Python 标准库，Web 层仅用 FastAPI）的智能电商客服平台：
顾客注册登录后与 GLM Agent 实时对话（可看到工具调用进度），商家后台查看经营数据与会话审查。

## Web 平台（推荐入口）

```bash
pip install fastapi uvicorn   # Web 层唯一依赖
python run_web.py             # 启动：http://127.0.0.1:8000
```

| 页面 | 地址 | 说明 |
| --- | --- | --- |
| 登录/注册 | `/login` | 顾客自助注册；内置演示账号见下 |
| 顾客聊天 | `/` | ZCode 风格：深色侧边栏会话列表 + 气泡 + **实时工具轨迹 chips**（"🔧 查询订单"→"🔧 检索知识库"）+ Markdown 渲染 |
| 商家后台 | `/admin` | 经营概览（订单分布/低库存预警）、订单、售后工单（可改状态）、顾客档案、商品库存、**客服会话审查**（完整对话+Agent 工具轨迹） |

演示账号：顾客 `c001 / demo1234`（张伟）、`c003 / demo1234`（王芳）；商家 `admin / admin123`。
新注册顾客自动生成 `U0001` 型顾客ID并建档。

**技术要点**：聊天走 SSE 流式接口（`POST /api/chat/stream`），逐轮推送
`llm_thinking → tool_start → tool_end → answer` 事件，前端实时显示 Agent 每一步在做什么；
认证为 PBKDF2 密码哈希 + HMAC 签名 Token（`web/auth.py`，零第三方依赖）；
会话完整 messages 快照存 SQLite，刷新/换设备后上下文无缝恢复。

## 与原方法（图片五步）的对应关系

| 原步骤（Claude Code 版） | 本项目落地（智谱 GLM 版） |
| --- | --- |
| 1. 给 Claude Code 绑卡 | 注册 [open.bigmodel.cn](https://open.bigmodel.cn) 获取 API Key，填入 `.env` |
| 2. 建文件夹放全部资料 | `data/`：SQLite 店铺库（顾客/商品/订单/工单）+ `data/knowledge/` 知识库（退换货/配送/FAQ/商品注意事项）+ `prompts/` 提示词 + `config.json` |
| 3. 写脚本终端 headless 调用、默认全权限、结果存 JSON | `src/agent.py`（GLM 工具调用循环，工具免审批）+ `run_tests.py`（批量无界面评测，逐用例增量落盘 `results/results.json`，失败也更新状态） |
| 4. 配置认证与 MCP | `.env`/环境变量认证 + 模型回退列表（glm-4.7 → 4.6 → 4-plus → 4-flash）；6 个业务工具（订单/商品/知识库/推荐/售后校验/工单）即本场景的 "MCP" |
| 5. 读 JSON 进行下一步 | `python -c "import json; ..."` 读取 `results/results.json` 的 summary 与逐用例结果 |

## 快速开始

```bash
# 0) 环境：Python 3.10+，无任何第三方依赖

# 1) 配置 Key（等同"绑卡"）
cp .env.example .env        # Windows: copy .env.example .env
# 编辑 .env 填入 ZHIPUAI_API_KEY=你的Key

# 2) （可选）重建演示数据库
python data/build_db.py

# 3) headless 批量评测（有 Key 自动走真实 GLM；无 Key 自动退化为离线管道自检）
python run_tests.py

# 单轮 headless 咨询（打印回答+工具轨迹，会话落盘 results/sessions/）
python -m src.agent -m "我的订单SO2610180003到哪了？" --customer C003 --trace

# 交互模式
python -m src.agent -i --customer C001

# 5) 读取结果 JSON
python -c "import json;d=json.load(open('results/results.json',encoding='utf-8'));print(json.dumps(d['summary'],ensure_ascii=False))"
```

## 目录结构

```
小铺智答/
├── config.json            # 店铺/模型/回退列表/日期/路径（today 可固定以便复现）
├── run_web.py             # Web 服务启动入口（python run_web.py）
├── run_tests.py           # headless 批量评测 → results/results.json
├── web/                   # Web 平台层
│   ├── server.py          # FastAPI：认证/聊天SSE/会话/商家管理接口
│   ├── auth.py            # PBKDF2 密码哈希 + HMAC Token（零依赖）
│   ├── db.py              # users/chat_sessions/chat_messages 表 + 演示账号
│   └── static/            # 前端（原生 HTML/CSS/JS，无构建步骤）
│       ├── login.html     # 登录/注册页
│       ├── index.html     # 顾客聊天页（ZCode 风格）
│       ├── admin.html     # 商家后台（6 个模块）
│       ├── css/app.css    # 设计系统
│       └── js/common.js   # API 封装/轻量 Markdown/组件
├── data/
│   ├── build_db.py        # 重建演示数据库
│   ├── shop.db            # SQLite：业务表 + Web 表（users/chat_sessions/chat_messages）
│   └── knowledge/         # 知识库 markdown（按 ## 分块检索）
│       ├── return_policy.md       # 退换货/保修/价保政策
│       ├── shipping_policy.md     # 发货/运费/时效/物流异常
│       ├── faq.md                 # 发票/会员/优惠券/人工客服
│       └── products_extra.md      # 商品使用注意事项（鼓包处置等）
├── prompts/system_prompt.md  # 客服人设/工具规范/政策红线/安全边界
├── src/
│   ├── agent.py           # Agent 循环（run 同步 / run_iter 事件流，Web SSE 复用）
│   ├── llm.py             # GLM 调用（模型回退/重试/.env）+ 离线 Mock 自检后端
│   └── tools.py           # 6 个工具 + 售后政策引擎（签收天数自动计算）
├── tests/cases.json       # 12 个真实场景用例（含自动评分断言）
└── results/               # results.json（批量结果）、sessions/（会话记录）
```

## 测试用例覆盖（tests/cases.json）

物流停滞投诉、超期退款拒绝、质量问题（电池鼓包安全处置）、场景化推荐、库存查询、
发货时效 FAQ、待发货改地址、退款进度、电子发票、超期价保婉拒、无关闲聊引导、系统提示词安全探测。

评分规则：`expect_tools`（必须调用的工具）、`expect_tools_any`（组内任一）、
`expect_keywords_any`（回答关键词组内任一命中）、`forbid_keywords`（禁止出现的政策外承诺）。

## 已完成的质量验证

- 工具层单元验证：售后政策天数计算（7天无理由/15天质量/12个月保修边界）、知识库检索命中、
  订单后缀匹配、推荐相关性过滤、工单落库（见 `results/mock_pipeline_check.json` 的 Mock 全量跑）。
- 管道验证：Agent 循环、工具执行、逐用例增量落盘、评分与退出码，全部在离线 Mock 后端跑通。
- 真实 GLM 评测：配置 Key 后执行 `python run_tests.py --backend zhipu` 即可（需要你完成"步骤1"）。

## 扩展指引

- **接入真实店铺数据**：替换 `data/shop.db`（保持表结构）或改 `src/tools.py` 中的 SQL。
- **新增政策/FAQ**：在 `data/knowledge/` 加 markdown（用 `##` 分节）即可被检索。
- **新增工具**：在 `src/tools.py` 实现函数并加入 `TOOLS` 注册表。
- **换模型/供应商**：`config.json` 的 `api.base_url` + `models` 兼容任何 OpenAI 风格端点。
