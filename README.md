# Small Harness

代码仓库：[cf-x/small-harness-submission](https://github.com/cf-x/small-harness-submission)（公开，无需登录即可查看）。

一个从零实现核心循环的最小 Agent。支持真实 Chat Completions 工具调用、独立会话、SQLite 持久化、基础上下文压缩、显式记忆、取消和执行日志；附本地聊天界面与命令行。

依据：[飞书笔试题](https://ailoha-inc.feishu.cn/wiki/XtViwTulHi0ORHk1HGocgxaqnrh)（revision 14），设计推导见[架构说明](docs/ARCHITECTURE.md)。核心流程未使用 LangGraph、OpenHands、OpenClaw 或其他 Agent 框架。

## 当前交付与验证边界

- 题目要求的主循环、三个工具、session、追问、压缩、异常处理、trace 和测试均已实现。
- 额外提供 `read_history`、HTTP API、网页会话切换、显式记忆管理、消息排队、取消、请求去重和重启恢复。
- 应用仅使用真实 Provider，没有在缺少密钥时伪装成 LLM 的演示逻辑。`search` 是原题允许的模拟工具。
- 当前工作区已配置用户提供的兼容接口，默认 `gpt-5.6-luna`。`gpt-5.6-luna` 和 `gpt-5.6-terra` 均已通过真实计算器调用及连续追问测试；Luna 还通过文档/搜索、会话/记忆和压缩/重启场景验收，详见测试报告。
- 分布式执行、后台工具 Job/回调、每日定时调度、多模态、流式模型输出、Anthropic 原生协议属于架构扩展，未纳入本次最小实现。五模块选题答案见 [架构说明](docs/ARCHITECTURE.md)。

## 快速开始

要求 Python 3.11+，macOS 或 Linux（单进程锁使用 `fcntl`）。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
cp .env.example .env
```

如果已有 `.env`，保留已有配置。开发工作区已配置本地 `.env`；交付包只提供 `.env.example`，接收者需自行创建 `.env` 并填入自己的凭证。

在 `.env` 填写三个值：

```dotenv
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
LLM_MODEL=你的工具调用模型名称或接入点ID
LLM_API_KEY=你的密钥
```

Base URL 填 API 前缀，代码会追加 `/chat/completions`。智谱常规 API 的前缀仅作为示例；若使用 Coding Plan、区域端点、代理或豆包，请填写账户实际提供的兼容端点，不能假定不同产品凭证通用。

```bash
.venv/bin/small-harness check-config
.venv/bin/small-harness serve --port 8000
```

浏览器打开 [本地工作台](http://127.0.0.1:8000)。本次交付预览使用端口 **8765**：[当前预览](http://127.0.0.1:8765)。修改 `.env` 后重启服务。

`check-config` 只校验配置完整性及格式，不验证密钥是否有效。绝不输出密钥。不要把 `.env` 或 `data/` 提交到 Git。

### 可选：可复现依赖

仓库附 `requirements.lock.txt`，记录本次测试使用的精确依赖版本。新环境可先安装它，再安装项目：

```bash
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m pip install -e . --no-deps
```

### 网页使用

1. 新建会话，输入“使用 calculator 计算 17*23”，再追问“把刚才结果加9”。
2. 打开另一个窗口，新建另一条会话。两个窗口使用不同 `session` URL；同一个 URL 表示继续同一会话。
3. 输入“阅读 handbook，告诉我项目代号和预算”，再追问“有哪些验收项”。
4. 展开工具调用/结果，或点击“查看执行日志”。
5. 通过“记忆”显式保存、编辑、删除事实；可以选择当前会话或当前用户所有会话。
6. busy 时继续发送消息会排队；“停止当前执行”取消当前 run，后面的消息继续处理。

### 命令行

```bash
.venv/bin/small-harness ask '请计算 17*23'
.venv/bin/small-harness ask '把刚才结果加9' --session <上一步返回的session_id>
```

CLI 与服务使用同一个默认数据库；**不能同时打开**。先停止服务再使用 CLI，或通过 `AGENT_DATA_DIR=./another-data` 使用独立数据目录。CLI 是可信本地入口，`--user` 仅用于本地身份选择；HTTP 身份由服务端认证绑定。

## 核心实现

```text
用户消息 → 持久化 queued run → 当前 session 的 FIFO 锁
         → 写入 user 消息 → 组装 Context → 调用真实模型
         → 验证响应
             ├─ 最终文本：保存并结束
             └─ tool_calls：注册表查找 → JSON/Schema 验证 → 限时执行
                           → 保存 assistant/tool 消息对 → 下一轮
```

| 文件 | 职责 |
|---|---|
| `agent/runtime.py` | 手写主循环、预算、排队、取消、错误终止 |
| `agent/provider.py` | 原生 HTTP、模型消息解析、有限重试、reasoning 续传 |
| `agent/session_store.py` | SQLite 事务、会话所有权、工具账本、trace、恢复 |
| `agent/context.py` | 保守预算估计、按完整 run 压缩、历史摘录 |
| `agent/memory.py` | 显式记忆的作用域过滤、关键词与中文二元词检索 |
| `agent/tools/` | 工具元数据、Schema、校验和四个具体工具 |
| `agent/app.py` | 本机 HTTP API、身份绑定、网页静态资源 |
| `agent/static/` | 无外部 CDN 的原生网页 |

每次模型响应都先检查停止原因。截断/拒绝响应中的工具不执行。有效调用的参数错误会作为同一 `call_id` 的错误结果返回模型，允许模型修正。一个批次里部分工具失败，也会补齐其他调用的结果。

使用非流式模型请求，避免把半段参数 JSON 提前执行。保留 Provider 返回的 `reasoning_content` 以支持交错思考续传；不把它展示为用户答案或记录到 trace。HTTP 历史接口同样移除该字段。其他 Provider 特有字段需新增显式适配。

### 工具

| 名称 | 示例参数 | 行为 |
|---|---|---|
| `calculator` | `{"expression":"(17*23)+9"}` | AST 白名单、一般运算使用 28 位十进制精度；整除和取余对当前操作数精确求商，避免先舍入导致错误；限制表达式、节点数、数值与指数 |
| `search` | `{"query":"Context","limit":3}` | 预置资料搜索；返回 `mock:true` 和 `fixture://` 来源 |
| `read_docs` | `{"doc_id":"handbook","offset":0,"limit":2000}` | 仅访问注册文档，带版本、来源、分页位置 |
| `read_history` | `{"query":"预算","limit":3}` | 仅回查当前 session 已结束的历史；支持精确 `seq`，长内容用 `seq` 和 `offset` 分页，每页最多 1200 字符 |

另一个可读文档是 `assignment`，来源为飞书原题的文字快照。扩展文档需修改可信 catalog，不能传任意路径。`read_docs` 支持按文档绑定允许的用户集合；默认两个示例文档对已认证用户开放。

四个工具均只读。同步本地计算/读取有严格大小限制；新增耗时工具必须遵守异步协作取消，不能在事件循环里执行长时间阻塞代码。进程中的 async 超时不能强行终止任意不合作代码，CPU 密集任务应使用独立进程。

## Session 与恢复

- `sessions` 归属用户；所有 HTTP session/run/trace 读取均校验所有权。
- `runs` 表示一次用户消息触发的执行。唯一 `(session_id, request_id)` 避免重复提交；同一 ID 换内容返回 409。
- 排队输入保存在 run 中，取得 session 锁后才进入对话历史，因此不会插入未闭合的工具链。
- 同一 session 串行，不同 session 并发；模型/工具 await 期间不持有数据库写事务。
- 多实例同时打开数据库会明确失败，避免进程内锁被误用为分布式锁。服务只支持一个 worker。
- 取消会停止本次协程并补齐未确认工具的错误结果；已完成结果保留。这里只读工具，无外部写入补偿承诺。
- 重启将未结束 run 标为 interrupted，把没有确认结果的工具标为 unknown，保持消息协议合法。**不会自动重放**未完成动作，用户可继续或重新发送。

## Context 与 Memory

### 哪些信息进入 Context

固定系统规则和工具 Schema；单独标示的历史摘录与相关记忆；最近完整交互；当前 run 的用户输入及完整工具链。最新消息只出现一次。

当前输入预算为 `AGENT_CONTEXT_TOKENS - LLM_MAX_OUTPUT_TOKENS - 512`。使用 UTF-8 字节数加消息开销作保守估算，**不是模型 tokenizer 的精确 token 数**。超出输入预算约 80% 时逐个归档旧的完整 run；保留当前 run。单条输入或当前工具链仍超限就明确停止，要求缩小范围，避免摘要死循环。

### 压缩方式

基础压缩使用**确定性摘录**，不额外调用 LLM：从旧历史保留最近的用户陈述、答复与成功工具结果片段，附原始 `seq`，注明有损和可能存在后续修正。原始消息不删除，`read_history` 可按关键词/序号回查；精确工具结果也可查到，回查结果本身不会再次递归召回。

回查结果包含 `seq`、`offset`、`next_offset` 和 `total_chars`。若 `next_offset` 非空，继续调用例如 `{"seq":1,"offset":1200}`，直至取完需要的内容。`offset` 必须和 `seq` 一起使用，无法跨会话读取。

当前版本不声称长对话压缩完全无损，也没有强事实抽取、自动矛盾合并或语义摘要。200 轮测试证明预算受控、当前修正保留、历史可查及协议成对；真实模型是否稳定正确利用摘录需要端到端评估。

### Memory 的召回时机与放置方式

1. 用户在界面/API 显式保存记忆；不会把所有聊天自动晋升为长期事实。
2. 每次模型调用前，先按可信 user 和当前 session 过滤记忆，再按当前用户输入的英文词/中文二元词相关性排序；最多选 4 条，附时间、作用域和来源。
3. 记忆放在模型输入的独立运行时资料区，不伪装成系统规则，不覆盖当前用户修正。
4. 当前 session 记忆不会出现在另一个 session；用户级记忆只在该用户的会话中可用。
5. 编辑直接更新记忆内容和更新时间；删除从记忆表移除，下一次召回立即生效，没有独立向量索引或召回缓存。

删除记忆不会删除已发生的聊天、以前模型复述过的内容或已发送给 Provider 的数据。需要全面遗忘时必须另行定义历史删除与 Provider 数据策略；本实现不对该场景作全链路删除承诺。

## 预算与异常

默认值可在 `.env` 修改：

| 配置 | 默认值 | 含义 |
|---|---|---|
| `AGENT_MAX_STEPS` | 8 | 一条用户输入的最大模型调用轮次 |
| `AGENT_RUN_TIMEOUT_SECONDS` | 120 | 取得执行权后的 run 总超时，不含排队 |
| `LLM_TIMEOUT_SECONDS` | 30 | 每次 HTTP 模型请求总超时 |
| `LLM_MAX_RETRIES` | 2 | 仅网络、超时、408、429 和 5xx 的有限重试 |
| `AGENT_TOOL_TIMEOUT_SECONDS` | 5 | 单工具协作超时 |
| `AGENT_RUN_TOKEN_BUDGET` | 80000 | 请求前保守预留，成功后计量的 run token 预算 |
| `LLM_MAX_OUTPUT_TOKENS` | 2048 | 单次生成输出上限 |
| `AGENT_CONTEXT_TOKENS` | 16384 | 应用配置的模型上下文上限 |

计量优先使用 Provider `usage.total_tokens`；缺失时使用保守预留值，重试缺少 usage 时也计入保守预留。该值控制资源使用，不是精确账单或价格计算。Provider 因异常实际发生的计费仍需以供应商账单为准。

同一工具与参数重复失败三次停止；到达步数/预算/超时后返回明确停止状态，保留历史。无结果搜索属于正常空结果；非法参数、未知工具、超时和内部异常有不同错误码。

## API 与身份

OpenAPI：[接口 Schema](http://127.0.0.1:8000/openapi.json)。所有写请求需加 `X-Harness-Client: web`，跨站请求未启用 CORS。为保持页面无需外部 CDN，未启用 Swagger 前端。

```bash
curl -X POST http://127.0.0.1:8000/api/sessions \
  -H 'Content-Type: application/json' -H 'X-Harness-Client: web' -d '{}'

curl -X POST http://127.0.0.1:8000/api/sessions/<session_id>/messages \
  -H 'Content-Type: application/json' -H 'X-Harness-Client: web' \
  -d '{"text":"计算17*23","request_id":"first-message"}'

curl http://127.0.0.1:8000/api/runs/<run_id>
```

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/status` | 配置状态，无密钥 |
| GET / POST | `/api/sessions` | 列表、新建 |
| GET | `/api/sessions/{sid}` | 历史、摘要、全部活动 run 和最近 20 条已结束 run |
| POST | `/api/sessions/{sid}/messages` | 202 受理后返回 run_id |
| GET | `/api/runs/{rid}` | 完成/失败/取消状态 |
| GET | `/api/runs/{rid}/trace` | 工具名、ID、耗时、错误与 usage |
| POST | `/api/runs/{rid}/cancel` | 取消指定执行 |
| GET / POST | `/api/memories` | 列出/新增显式记忆 |
| PATCH / DELETE | `/api/memories/{mid}` | 更新/删除记忆 |

默认只接受 loopback 来源，绑定单个 `local-user`；不同浏览器用户不会自动变成不同身份。Host 校验防止本机无认证模式的 DNS 重绑定。

若需要多用户，在 `.env` 配置 `AGENT_AUTH_TOKENS` 为“随机令牌 → 用户名”的 JSON；每个令牌至少 24 个 ASCII 字符，然后重启。HTTP 带 `Authorization: Bearer <token>`，网页在连接窗口输入**服务访问令牌**。模型 API Key 只保留在服务端。非本机监听必须启用令牌；面向公网还需要 TLS、部署限流和完整身份管理，本示例不自带这些生产设施。

## 测试

```bash
.venv/bin/python -m pytest -q
```

离线测试使用 HTTP MockTransport 和脚本化 Provider，覆盖控制流而不假装验证模型智力。详见 [测试报告](docs/TEST_REPORT.md)。

配置真实模型后，显式运行付费 API 测试：

```bash
RUN_LIVE_TESTS=1 .venv/bin/python -m pytest -q -m live
```

它会用独立临时数据库验证真实计算与追问、文档/搜索、会话隔离/记忆更新，以及 200 轮压缩后的分页回查和重启续聊，不修改日常会话。模型选择具有随机性，测试失败需检查实际协议和 trace，不能为了“通过”把线上模型替换成固定答案。

## 提交材料

- 核心代码与测试：`agent/`、`tests/`
- [五模块架构说明](docs/ARCHITECTURE.md)
- [AI Prompt 与问题解决记录](AI_WORKLOG.md)
- [测试与验证记录](docs/TEST_REPORT.md)
- [对抗式审查与修复记录](docs/ADVERSARIAL_REVIEW.md)
- 原题文字快照：`agent/fixtures/assignment.md`
- 打包脚本：`scripts/package_submission.py`（白名单选取文件并检查已知凭证）

交付包不包含 `.env`、备用凭证、虚拟环境、聊天数据库或完整原始飞书响应。接收者应安装依赖、填写自己的模型配置后启动；无需访问开发者的本机服务。

交付入口：[提交说明](docs/SUBMISSION.md)。
