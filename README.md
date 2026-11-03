# 🚀 智能数据分析 Agent

面向企业 MySQL 多库 + 上传文件的数据分析对话助手。用自然语言提问，Agent 自动查库、算数、出图。

## ✨ 功能特性

- **自然语言 → SQL**:Agent 自动完成库/表感知、SQL 生成与执行、报错自修正(最多 3 次重试),全程只读(SELECT)。
- **上传文件真实分析**:上传 CSV/Excel 后,Agent 用 `query_file`(duckdb SQL)真实读取文件数据,不再靠预览猜测;支持分组统计、多图表可视化。
- **多图表输出**:一轮对话可同时生成多张 ECharts 交互图 + matplotlib 静态图(此前只保留最后一张的问题已修复)。
- **流式状态**:SSE 实时推送计划和工具轨迹；答复文字在最终质量门禁后发送，避免先展示未经验证的候选结论。
- **Markdown 渲染**:AI 回复以规范排版显示(标题/列表/代码块/表格),原始 `# * -` 符号不裸露。
- **配置中心**:
  - LLM 配置:支持 OpenAI / Anthropic / 通义千问 三家,选提供商自动填充接口地址,API Key 加密存储,保存后自动测试连接。
  - 数据库配置:MySQL 连接参数加密保存。
- **会话管理**:历史会话列表、加载、删除、**内联重命名**。
- **用户账号与数据隔离**:开放注册/登录(JWT 认证),每个用户的会话与上传文件互相隔离;LLM/DB 配置全局共享(管理员配置一次即可)。
- **可观测性**:结构化 JSON 日志(每个请求记录 method/path/status/耗时)、`/api/metrics` 请求数/错误率/耗时分位数(P50/P95/P99)、健康检查分 liveness(`/api/health`)与 readiness(`/api/health/ready`,检查 SQLite 与数据目录)。
- **审计日志(合规留痕)**:SQL 查询(含上传文件查询)、LLM/DB 配置变更、用户操作(注册/登录/改密/角色变更/注销)、会话操作、文件上传全部留痕,只追加不可修改,detail 不含明文密钥;`/api/audit` 仅管理员可查(支持按 action/username 过滤)。
- **token 用量计量(省钱)**:规划、嵌套角色、记忆维护及晚到 Worker 的 input/output token 与成本估算自动落库(内置 openai/anthropic/qwen 价格表,未知模型兜底估算);管理端报表 `/api/usage`(按天/按用户/按模型/明细);日/月预算配置与告警(超 80% 日志告警),chat 入口超限自动拒绝(BUDGET_LIMIT)。
- **上下文压缩(省钱+丝滑)**:token 预算制截断替代纯轮数截断(默认 8k token 预算,按近似估算从头部裁剪历史,保留最近对话);长工具输出压缩(query 结果 markdown 最多展示 50 行并提示省略,前端表格仍完整)。
- **显式 Agent 图编排(核心能力)**:手写 LangGraph StateGraph 替代 create_react_agent 黑盒——`agent`(LLM 决策,逐 token 流式)/`tools`(执行工具调用)节点 + 条件边;状态含 `sql_attempts` 轻量反思(查询工具连续失败达上限自动注入提示强制收敛,防死循环);节点/状态全显式可观测,为轨迹可视化打基础。
- **按需 Agent 编排(核心能力)**:支持 `auto/single/multi`；简单分析由单 Agent 执行，多数据源或计划中的复杂统计按需启用 Supervisor 与数据执行、统计验证、洞察表达角色。两种模式共用证据门禁和模型/工具/查询预算，响应返回可解释的路由原因。
- **记忆与上下文(核心能力)**:SQLite 保留原始历史；摘要累计合并未覆盖消息并保存断点，工具调用与完整响应按原子组裁剪。相关记忆在规划前注入；长期偏好按数据域和版本管理，明确纠正可及时更新。统一估算系统规则、历史、当前问题、工具定义与输出预留。
- **Agent 轨迹可视化(核心能力)**:每条回复下方展示本轮完整工具调用链(主管→专家→工具 层级时间线,含入参/结果摘要、状态、耗时);SSE 实时增量推送 + SQLite 落库,重开会话可回放;基于显式图"节点埋点"实现,不侵入工具。
- **UI**:白黑主题、通栏布局(会话栏贴左、对话区贴右)。

- **指标契约与局部修复**:已启用的服务端指标定义约束公式、过滤、粒度、单位和数据源；SQL AST 校验失败后进入有上限的局部修复，缺少必要数据时澄清。统计和图表按证据 ID 读取数据。默认注册表为空，配置方式见下文。

## 🧱 技术栈

| 层 | 技术 |
|---|---|
| 前端 | Vue 3 + Vite + Pinia + Vue Router + Axios + ECharts + marked/DOMPurify |
| 后端 | Python 3.9 + FastAPI + LangChain / LangGraph + SQLAlchemy + Pandas + duckdb |
| 存储 | SQLite(会话/上传元信息)、JSON 加密文件(LLM/DB 配置)、文件系统(上传文件本体) |

## 📁 目录结构

```
DataAnalysis_Agent/
├── backend/
│   ├── app/
│   │   ├── main.py            # FastAPI 入口、CORS、路由挂载
│   │   ├── api/
│   │   │   ├── chat_api.py    # 对话(含 SSE 流式)、上传、会话管理
│   │   │   └── config_api.py  # LLM / 数据库配置与测试连接
│   │   ├── agent/
│   │   │   ├── graph.py       # LangGraph Agent 图
│   │   │   ├── prompts.py     # System Prompt(工作流/可视化/回复格式约束)
│   │   │   └── state.py
│   │   ├── tools/
│   │   │   ├── agent_tools.py # list_schemas / get_schema / query_mysql / make_chart
│   │   │   ├── chart_tool.py  # generate_chart / auto_analyze_and_visualize(8 种图表)
│   │   │   └── file_tool.py   # list_files / query_file(duckdb) / file_stats
│   │   ├── core/factories.py  # 按提供商构建 LLM、构建数据库引擎
│   │   ├── meta/              # 配置加密存储(store / crypto)
│   │   ├── memory/            # 内存会话存储(LRU)
│   │   ├── persistence/       # SQLite 会话/上传持久化
│   │   ├── errors/            # 错误分类
│   │   └── db/schema.py       # 多库表结构读取
│   ├── data/                  # 运行时数据(见下)
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── views/             # ChatView(对话) / ConfigView(配置中心)
│   │   ├── components/        # ChartView / DataTable / MarkdownContent / 表单等
│   │   ├── api/               # 接口封装(含 SSE 流式)
│   │   ├── stores/            # Pinia
│   │   └── router/
│   └── package.json
└── README.md
```

### backend/data/ 说明

| 文件 | 用途 |
|---|---|
| `chat_history.db` | SQLite:会话、消息、上传元信息 |
| `uploads/` | 上传文件本体(服务重启不丢) |
| `llm_configs.json` / `db_configs.json` | 配置(Key 加密) |
| `.master_key` | 加密密钥文件 |

## 🐳 Docker 部署

```bash
# 构建并启动（前端 http://localhost:8080，后端 http://localhost:8000）
docker compose up -d --build
# 查看日志
docker compose logs -f
# 停止
docker compose down
```

- 后端镜像：`python:3.9-slim`，非 root 运行，内置中文字体，`backend/data/` 以卷挂载持久化。
- 前端镜像：多阶段构建（node 构建 → nginx 托管），nginx 代理 `/api` 到后端并关闭缓冲以支持 SSE 流式输出，内置 Vue Router history 路由 fallback。
- 首次启动后在「配置中心」填写 LLM/DB 配置即可使用。

## 🚀 快速开始

> **Clone 后注意**:`backend/data/`(运行时数据)不会随仓库分发——首次启动会自动创建 `data/` 目录,LLM/DB 配置请在页面「配置中心」里填写(密钥加密保存),无需任何环境变量。

### 1. 后端(端口 8000)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # 首次
pip install -r requirements.txt                     # 首次
python app/main.py
```

启动后自检会打印已注册路由;若缺少依赖(如 matplotlib/duckdb/langchain-anthropic),按 `requirements.txt` 补齐。

### 2. 前端(端口 5173)

```bash
cd frontend
npm install      # 首次
npm run dev
```

访问 http://localhost:5173 ,`/api` 已代理到后端 8000。

### 3. 首次使用

1. 打开「配置中心」,新增 **LLM 配置**(选提供商→自动填接口地址→填 API Key→模型名→保存并测试)。
2. 新增**数据库配置**(MySQL 主机/端口/账号,建议用只读账号)。
3. 回到「分析对话」,提问或上传 CSV/Excel 分析。

## 🧪 自动化测试

```bash
# 后端 pytest（核心逻辑单测和接口集成）
cd backend
pip install -r requirements-dev.txt   # 首次
python -m pytest tests/

# 前端 vitest（拦截器 / SSE 解析 / 组件 / 路由守卫 / store，34 例）
cd frontend
npm install          # 首次
npm test
```

> 测试全程隔离：后端把 SQLite / 配置 JSON / JWT 密钥重定向到临时目录，不触碰真实 `backend/data/`；前端在 happy-dom 环境运行，不启动后端。CI（GitHub Actions）已包含两端测试步骤。

## Agent 质量评测

[默认任务集 v1.4.0](backend/app/evaluation/suites/agent_quality_v1.json) 包含 18 道合成任务，分为开发、回归和验收三组。运行器从自然语言调用正式聊天处理函数，每次试验使用独立进程、临时 SQLite 和合成上传文件，保留规划、人工确认、工具执行与结果展示逻辑。真实模型实验覆盖上传文件和无数据场景，MySQL 与 HTTP/SSE 传输需要单独验收。

- **结果**　按任务校验展示表的列、顺序和数值，以及图表数据或应有的澄清、拒绝等行为。下方对照的 23 份预期业务表由独立 DuckDB 查询重算。
- **轨迹与安全**　按任务核对数据源、工具调用约束、审批与故障处理。跨文件回归检查是否成功读取所需文件、是否重复执行同一条失败 SQL。超时保留已记录的轨迹，缺失证据记为未知。数据库配置授权另有隔离接口测试，升级步骤见[数据库配置授权](backend/config/DB_ACCESS.md)。
- **开销与回归**　报告记录模型调用次数、可观测 token、延迟和失败原因。默认门禁要求所选任务每次都通过，失败、缺失或重复的记录会阻止放行。

### 真实模型对照

最新完整对照使用冻结的 `paired-result-columns-v1` 合成题集。对照组仅撤销结果表交付改动，两组共用 `deterministic-v2` 评分器、`glm-5.3-Flash`（温度 0.7，低推理强度）和预算（每次最多 9 次模型调用、85 秒）。主批次每版各跑 24 题一次，另选 4 题各补跑一次。

| 指标 | 改动前 | 改动后 |
| --- | ---: | ---: |
| 完整通过（结果及任务约束） | 12/24（50.0%） | 16/24（66.7%） |
| 正确交付业务表格 | 12/23 | 16/23 |
| 重复题两次均通过 | 1/4 | 3/4 |
| 主批次模型调用尝试 | 87 | 96 |
| 主批次观测输入 / 输出 token | 182,205 / 14,471 | 214,057 / 13,063 |
| 主批次用量记录不完整 | 2/24 | 2/24 |
| 主批次端到端 P50 / P95 | 35.254 秒 / 85.018 秒 | 37.214 秒 / 85.018 秒 |

24 道配对题中 5 道由失败变为通过，1 道退化，两版均未通过全题门禁。两道跨文件题都达到截止时间，故障注入题两版均未正确说明超时，新版的 220 行题仍未完整交付。P95 受到 85 秒试验截止影响，token 仅统计已观测用量。模型采样会改变规划，四题补跑也只反映这四题的重复表现。

此后用 `deterministic-v3.1` 对 5 条定点观测重评分。分页未完整交付提示和注入超时提示各有 1 次符合预期，3 道跨文件题仍未完成业务。定点结果单独计数，当前默认套件尚无完整真实模型批次。历史配对题集、原始轨迹和内部报告保存在本地。

### 运行评测

在 `backend/` 安装 `requirements-dev.txt` 后运行。真实模型试验使用本机默认模型配置，也可用 `--config-id` 选择已有配置。每次试验使用新的报告路径。

```bash
python -m app.evaluation                         # 用预填预测检查静态评分器
python -m app.evaluation.quality_runner validate # 校验默认合成任务集
python -m app.evaluation.quality_runner run --split dev --repeat 2 \
  --out data/eval/dev-001.json
```

常规 CI 执行测试、套件校验和静态评分器回归。**Agent quality (real model)** 是手动工作流，使用 `agent-quality` 环境的 `DATAPILOT_EVAL_MODEL_CONFIG`，全套每题跑两次，并在任务摘要发布门禁状态。部署由 `main` 推送后的常规 CI 成功触发。

## 💬 使用示例

- 数据库分析:`上个月各状态的订单数?` → Agent 查库返回表格 + 洞察
- 上传文件:`上传成绩单.csv`,`各科平均分并画柱状图` → Agent 真实统计文件数据并出图
- 一轮多图:可要求同时生成柱状图与饼图,前端全部展示

## 🛠 Agent 工具一览

| 工具 | 作用 |
|---|---|
| `list_schemas` / `get_schema` | 多库发现、表结构读取 |
| `query_mysql` | 只读 SQL 查询(自动 LIMIT 500) |
| `make_chart` | 基础 ECharts 图(bar/line/pie) |
| `generate_chart` | 高级图表(8 种:bar/line/pie/scatter/histogram/boxplot/area/heatmap) |
| `auto_analyze_and_visualize` | 自动选型 + 统计摘要 |
| `list_files` / `query_file` / `file_stats` | 上传文件结构、SQL 查询(duckdb)、统计摘要 |

## 指标契约配置

服务端从 `backend/config/metric_contracts.json` 加载可信定义。默认未启用指标，旧请求保留原行为；示例位于 `backend/config/metric_contracts.example.json`。业务方确认公式、成本税费口径、字段、单位及适用数据源后，配置 `contracts`、`enabled_contract_ids` 和 `source_bindings`。聊天 API 可通过 `metric_contract_ids` 选择已启用的 ID，不能从请求覆盖公式。

文件源使用 `df` 并绑定上传文件名，当前契约模式每请求支持一个明确文件；MySQL 源使用 `schema.table` 并绑定数据库配置 ID。定义变更会使未消费的旧审批失效。支持平面 SELECT 及具备受信约束与唯一键证明的 LEFT JOIN；未支持的 SQL 结构保守拒绝或澄清，不保证任意 SQL 等价。

执行轨迹包含结构化校验错误和 `local_repair` 节点；统计、图表通过 `calculate_from_evidence` / `chart_from_evidence` 消费有效证据，过期和截断证据会被拦截。

## ⚠️ 注意事项

- **LLM 密钥**:需有效 API Key(免费额度耗尽会返回 403),在「配置中心」填写,加密保存在 `backend/data/` 中。
- **数据库安全**:系统强制只读 SELECT,但请仍使用**只读账号**连接,避免权限过大。
- **数据路径**:所有运行时数据固定在 `backend/data/`(绝对路径),从任意目录启动后端都不会分裂数据。
- **图表中文**:后端使用 matplotlib `Agg` 无界面后端,避免 macOS 下后台线程崩溃;首次启动会构建字体缓存。
- **会话上限**:内存会话默认最多 200 个、单会话 50 轮,超出自动淘汰/截断(历史仍保留在 SQLite)。

## 📄 License

内部项目,无开源许可。
