# 提交说明

代码链接：[cf-x/small-harness-submission](https://github.com/cf-x/small-harness-submission)（公开仓库）

本项目按原题交付两部分：最小可用 Agent 代码，以及五个模块各选一题的架构设计答案。

## 给评阅者

1. 阅读根目录 README，按锁文件安装依赖。
2. 复制 `.env.example` 为 `.env`，填入自己的模型接口、模型 ID 和 Key。
3. 运行 `small-harness serve`，打开本机页面。未填密钥时会明确提示，不会伪造模型回复。
4. 运行离线测试；需要实际验收模型时再显式运行 live 测试。
5. 架构题答案见 `docs/ARCHITECTURE.md`，AI 使用记录见 `AI_WORKLOG.md`，验证证据见 `docs/TEST_REPORT.md`。

## 需求与证据

| 原题要求 | 实现/证据 |
|---|---|
| 核心循环自研，真实模型 API | runtime.py / provider.py；Luna 与 Terra 实测 |
| 三个工具与 Schema 自主调用 | calculator、search(mock)、read_docs；注册表及真实 trace 断言 |
| 输出解析 | 原生 tool_calls、最终文本、停止原因、reasoning 续传 |
| 独立 session 与持续对话 | SQLite 所有权、独立 URL；离线及真实双会话测试 |
| 普通追问与工具追问 | 文档第二项追问；391→400 工具追问 |
| 最大轮数、基础压缩 | 预算限制、完整 run 归档、200 轮回查测试 |
| 异常与工具日志 | 错误分类、超时、取消、持久化 trace |
| Memory 召回时机与放置 | README 专节；显式记忆作用域、每次推理前检索 |
| 运行方式、系统设计 | README、架构说明 |
| AI Prompt 与问题解决记录 | AI_WORKLOG.md，如实说明 AI 协助与验证 |
| 五模块各选一题 | ARCHITECTURE.md 共五题 |

## 包内与包外

ZIP 包含源码、测试、文档、示例配置、锁文件、JUnit 证据、文件哈希清单。它不包含真实密钥、`.env`、聊天/记忆数据库、虚拟环境或 `.git`。

运行环境为 Python 3.11+、macOS/Linux。凭证不能随包转交；接收者使用自己的账户。本地 8765 预览地址仅供开发者当前机器使用。

代码仓库已公开，无需登录或额外授权即可查看代码与文档。提交时直接分享上述 GitHub 链接即可，也可交付 ZIP。
