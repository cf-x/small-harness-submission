# 五模块架构作答与实现映射

每个模块选择一题。本文收敛了第一性原理分析中的设计推导。以下区分当前实现和生产扩展。

## 1. Context：200 轮对话如何压缩

Context 是有限的模型输入视图，原始历史是可回查的持久化记录。压缩按完整交互边界进行，保留当前输入、最近对话和工具调用/结果对；旧记录形成带序号、角色和有损标记的摘录。预算以模型窗口减输出预留和安全余量计算。单条输入仍超限时明确停止，不能循环摘要到丢失原意。

本实现：`ContextBuilder` 按完整 run 归档，SQLite 保存原始记录，`read_history` 按关键词或序号恢复细节。没有额外模型摘要费用；代价是压缩不够语义化，不保证所有旧事实同时可见。当前修正始终保留在活跃 run 内。

验证：200 轮历史不被删除，输入预算达标，当前新预算保留，所有工具调用闭合；原文可通过回查工具取得。真实模型对复杂指代的理解仍需多样化端到端评估。

## 2. Memory：半个月后再次问同一问题

首先判断是回顾旧结论还是重评当前情况。用户与作用域过滤在相似性评分之前执行；相关性、有效时间和来源共同决定是否使用。实时事实必须重新查询，重复问题不能直接复读旧答案。

本实现：用户显式保存记忆，可设为 session 或 user 范围；每次调用模型前按当前问题关键词召回，最多四条，附作用域与更新时间，置于独立资料区。编辑和删除立即影响下一次召回。未命中时允许不召回。

取舍：关键词实现简单、可审计；对语义改写能力有限。将来可增加语义检索、事实版本和冲突处理，但不应通过放松用户隔离来增加召回率。删除记忆与删除全部历史是不同接口契约。

## 3. Task：长程执行如何维持目标

把目标、约束、验收项、进展、未决事项与预算保存为任务契约，每次恢复从契约和最新证据重建执行视图。以可验证步骤推进，目标变化有来源，完成状态必须绑定验收证据。仅凭模型说“完成”不足以关闭高风险或长程任务。

本实现只覆盖单个有界 run：用户输入及步骤持久化、预算停止、重启标记、结果可追溯；简单问答以合法最终文本结束。跨 run 的结构化任务契约、自动恢复规划、独立验收器属于扩展，不能把本实现声称为多日自主执行系统。

取舍：持久清单与证据可降低目标漂移；规划器和评估器增加成本与新的错误来源。应先用任务失败案例证明额外机制的收益。参考：[Anthropic 长程 harness 实践](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)。

## 4. Runtime：busy 时新消息或工具事件到达

必须区分受理、执行和完成。普通新消息持久化为 queued run，当前 session 只有一个执行者；在取得执行权前不把新输入插入工具消息链。不同 session 使用不同锁，可并行处理。取消是独立接口，停止当前协程并闭合待确认调用。

本实现采用单进程 FIFO 锁和协作式取消。明确拒绝多个进程共享该数据库，避免把进程内锁误当成分布式一致性。没有异步 Job 回调，也未提供自然语言自动中断；普通补充默认排队。

生产扩展：完成事件携带 session/run/job/call ID，先持久化与去重再投递协调器。旧模型响应通过 epoch 拒绝，已发生工具结果仍入账；外部副作用需幂等或查询确认。至少一次投递配合幂等，不无条件承诺 exactly-once。

## 5. Runtime 比较：Claude Code 与兼容式 Function Calling

Claude Code 是完整 harness，Function Calling 是 API 协议，比较时应分开模型消息、工具调度和界面显示。

Anthropic Messages 通常在 assistant 内容块中返回 `tool_use`，在 user 内容块中接收关联 ID 的 `tool_result`；兼容 Chat Completions 的接口使用 `tool_calls` 和独立 tool 消息，`arguments` 为 JSON 字符串。两种表示都需要参数验证、结果配对、预算和异常处理，消息形状不代表整体 Agent 能力强弱。[Anthropic 协议](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)、[智谱工具调用](https://docs.bigmodel.cn/cn/guide/capabilities/function-calling)

本实现明确只适配后者，保留 `reasoning_content` 供需要交错思考的 Provider 续传，不显示内部推理。对实际模型的并行调用、流式参数、特殊字段必须做契约测试，不能仅因“compatible”就保证所有功能。参考：[智谱思考模式](https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode)。

未来接入 Anthropic 时应新增 Provider Adapter，将调用和结果转成合法块结构，保留必要的不透明协议数据，复用 Runtime 的工具与状态逻辑。

