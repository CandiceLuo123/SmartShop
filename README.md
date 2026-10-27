# 小铺智答 —— 智谱 GLM 驱动的多专家电商客服 Agent 平台

多 Agent 架构：**意图识别路由 + 7 个专家客服 + 三层记忆**，顾客注册登录后与 AI 实时对话
（可看到"正在分派专家→工具调用→回复"全过程），商家后台查看经营数据与会话审查。

## 核心架构

### 生产级可靠性层（借鉴多 Agent 平台设计）

| 模块 | 防护的问题 | 实现 |
| --- | --- | --- |
| `src/cache.py` | 缓存**穿透**：查询不存在数据每次都打到库 | 空结果短 TTL 缓存（含 `found:false` 语义）+ 查询合法性校验（拒畸形/超长/注入特征） |
| | 缓存**击穿**：热点 key 过期瞬间并发回源 | per-key 互斥锁，同 key 只有一个线程回源，其余 double-check 直接拿结果 |
| | 缓存**雪崩**：大量 key 同时过期 | TTL = base + random(0, jitter) 打散过期时间 |
| `src/breaker.py` | 依赖故障时持续浪费流量 | 三态熔断器 CLOSED→OPEN→HALF_OPEN：连续失败 5 次熔断，60s 半开探测；打开期间返回降级结果（工具不抛错，Agent 循环不中断） |
| `src/tools.py` execute_tool | 慢调用拖死会话 | 线程池 + 超时控制（默认 30s），超时/异常/熔断统一走 fallback 降级文案 |
| `src/monitor.py` | 某专家持续失败继续接流量 | 后台 10s 周期采集成功率/延迟 → 阈值告警 → 路由降权（penalty≥0.8 自动 fallback general）→ `/api/admin/monitor` |
| `src/skills.py` + `skills/` | 业务规则改动要改代码重部署 | SKILL.md front matter（手写解析，不依赖 PyYAML）→ 按角色+关键词动态注入 system prompt（预算 5000 字符）→ `POST /api/admin/skills/reload` 热加载 |
| `run_tests.py` 扩展评测 | 质量回归无量化 | 意图分类手写 Accuracy/P/R/F1/Macro-F1（不依赖 sklearn）+ LLM Judge 四维评分（及格线 0.75）+ 滚动基线回归检测（退化 >5% 告警，`results/baseline.json`） |

### RAG 检索优化链路（`search_with_rewrite`）

```
原查询 → LLM 改写 3 个子查询 → 4 路并行召回（线程池）→ 内容 md5 合并去重
      → LLM 重排取 Top3 → 结果缓存
```
每步都有兜底：改写失败用原始查询；重排失败保序截断；候选 ≤ top_k 跳过重排调用。
实测："东西坏了想退钱" 改写出 4 路查询，7 条候选重排后精确命中 15 天质保/7 天无理由政策。

### 数据层：10 万行业务库 + 双后端

```bash
python data/generate_big_data.py          # 生成 10 万条到 SQLite 业务库（shop.db-big）
python data/generate_big_data.py --mysql  # 或直接建库建表灌入 MySQL（需 .env 配 MYSQL_PASSWORD）
```

- 规模：商品 800 / 顾客 2 万 / 订单 **10 万** / 明细约 16 万，含真实感的时间分布、状态分布、物流停滞率
- `config.json → database.backend` 可切 `sqlite` / `mysql`（pymysql），查询层方言差异集中封装
- **双库回退**：业务库查不到的订单/商品自动回退主库（演示数据），新旧单号都能查
- 实测 10 万行订单精确查询约 100ms，命中缓存后 <1ms
- 平台数据（账号/会话/工单/记忆）与业务数据分离：业务库可随时重灌，平台数据稳定

### 多专家路由（src/router.py）
每轮消息先由轻量 LLM 分类器（失败时规则兜底）识别意图，路由到对应专家接管：

| 意图 | 专家 | 工具白名单 |
| --- | --- | --- |
| complaint 投诉 | 🧡 投诉处理专员（安抚+高优工单） | 查订单 / 知识库 / 工单 |
| after_sales 售后 | 🛠️ 售后专员（政策校验引擎） | 售后校验 / 查订单 / 知识库 |
| billing 账单 | 💰 账单专员（退款/价保/发票） | 查订单 / 知识库 / 工单 |
| logistics 物流 | 🚚 物流专员（查件/改地址） | 查订单 / 工单 / 知识库 |
| shopping 购物 | 🛍️ 导购顾问（推荐/库存） | 推荐 / 查商品 |
| policy 政策 | 📋 政策顾问（规则咨询） | 知识库 |
| general | 🤖 综合客服（兜底） | 全部工具 |

专家共享同一份对话历史与记忆（切换专家 = 切换人设 + 工具白名单），意图与专家徽章在聊天界面实时可见。

### 三层记忆（src/memory.py）
- **L1 会话记忆**：messages 全量保留最近对话。
- **L2 会话摘要**：user 轮数超过 15 时，自动把最早 10 轮用 LLM 压缩成摘要
  （保留订单号/承诺/未解决问题），替换进上下文并持久化到 `chat_sessions.summary`。
- **L3 用户画像**：跨会话长期记忆（身份/偏好/历史诉求），每 3 轮 LLM 增量更新到
  `memory_profiles` 表，注入 system prompt，换会话也认识这位顾客。

### 端到端测评（run_tests.py + tests/cases.json）
单轮用例断言路由正确性（`expect_route`）+ 工具调用 + 关键词 + 禁用承诺；
`multi_turn` 用例逐轮发送（投诉→记忆延续→账单切换），逐轮断言路由、
在最终回答中验证跨轮记忆。`python run_tests.py` 一键回归。

## Web 平台

```bash
pip install fastapi uvicorn   # Web 层唯一依赖
python run_web.py             # 启动：http://127.0.0.1:8000
```

对外访问：双击 `start_all.bat`（一键起服务+Cloudflare 公网隧道，GBK 编码），
公网地址见"小铺智答-隧道"窗口；停止用 `stop_all.bat`。

**接入真实数据**：见 [真实数据接入指南.md](真实数据接入指南.md)——
`python data/make_templates.py` 生成 Excel 模板，填入真实商品/订单后
`python data/import_real_data.py 文件.xlsx` 导入，AI 立即可查，无需改代码。

| 页面 | 地址 | 说明 |
| --- | --- | --- |
| 登录/注册 | `/login` | 顾客自助注册；内置演示账号见下 |
| 顾客聊天 | `/` | Gemini 式界面：会话侧栏 + **意图路由徽章** + 工具轨迹 chips + **AI 记忆抽屉**（画像/摘要可视化） |
| 商家后台 | `/admin` | 经营概览、订单、售后工单、顾客档案、商品库存、会话审查（含每轮意图路由记录） |

演示账号：顾客 `c001 / demo1234`（张伟）、`c003 / demo1234`（王芳）；商家 `admin / admin123`。

**技术要点**：聊天走 SSE 流式接口，逐轮推送
`route（意图分派） → tool_start → tool_end → compress（记忆压缩） → answer` 事件；
认证为 PBKDF2 密码哈希 + HMAC 签名 Token；会话快照 + 摘要 + 画像全部落 SQLite，
刷新/换设备后上下文与记忆无缝恢复。

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
│   ├── agent.py           # Agent 循环（路由切换专家 + run/run_iter 双接口）
│   ├── router.py          # 意图分类器（LLM+规则兜底）+ 7 专家定义（persona/工具白名单）
│   ├── memory.py          # 三层记忆：会话压缩摘要 + 用户画像
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
