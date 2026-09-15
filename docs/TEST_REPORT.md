# 最终测试与交付验证报告

日期：2026-09-16。默认模型：`gpt-5.6-luna`，接口为用户提供的 Chat Completions 兼容服务。密钥仅在开发者本地配置中，交付包不含凭证。

## 结果

| 验证 | 结果 |
|---|---|
| 开发工作区离线测试 | 70 passed，4 live 测试未选中 |
| Luna 真实模型完整验收 | 4 passed，耗时 83.80 秒 |
| Terra 真实计算器与工具追问 | 1 passed，耗时 11.57 秒 |
| ZIP 解压后新建虚拟环境、按锁文件安装、安装项目 | 成功 |
| 干净环境离线测试 | 70 passed，4 live 测试未选中，耗时 1.93 秒 |
| JavaScript 语法与 Python 编译检查 | 通过 |
| 依赖一致性 `pip check` | 通过 |
| 本地网页布局、发送、会话记忆保存及隔离 | 浏览器实际验证 |

## 真实模型覆盖

1. calculator 计算 `17*23`，验证工具 trace 和结果 `391`，同一 session 追问加 9 得到 `400`。
2. read_docs 读取 handbook，返回 `Cedar`、`800` 和验收项；纯文本追问第二项解释；search 真实调用并明确标示模拟/离线资料。
3. 同用户两会话的不同代号互不串入；用户级记忆可以召回，修改预算后另一个会话取得最新值。
4. 注入 200 轮确定性的测试历史，真实模型在压缩后使用 read_history 找回最早代号；关闭并重新打开 Store/Runtime 后继续追问仍取得正确代号。

200 轮历史由测试构造，不代表实际付费生成了 200 轮。真实模型负责回查和续聊。测试未导入生产代码中的模拟模型，未为通过而修改预期答案。

记录：[离线 JUnit](../outputs/test-results.xml)、[Luna JUnit](../outputs/live-test-results.xml)、[Terra JUnit](../outputs/live-terra-test-results.xml)。

## 复现

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m pip install -e . --no-deps
.venv/bin/python -m pytest -q -m 'not live'
```

创建自己的 `.env` 后运行真实测试（会调用付费接口）：

```bash
RUN_LIVE_TESTS=1 .venv/bin/python -m pytest -q tests/test_live_provider.py
```

## 故障路径验证

离线测试覆盖：非法参数/重复 JSON 键/未知工具，工具超时与混合成功失败，Provider 限流重试与错误脱敏，输出截断不执行工具，预算停止，重复请求去重，同会话 FIFO、跨会话并发，取消及重启后补齐工具协议，跨用户访问拒绝，记忆更新与删除，压缩边界和原始工具数字回查。

## 实际服务诊断历史

- 原接口早期在 `gpt-5.4-mini` 上返回 429，在 `gpt-5.2` 与 `gpt-5.4` 上返回账户不支持；模型列表正常。不能由此推断所有模型不可用。
- 备用 Responses 域名返回 Cloudflare Tunnel HTTP 530，未完成该协议适配或真实验收。
- 用户指定 Luna/Terra 后生成及工具测试成功，现有默认配置为 Luna。

## 验证边界

这些是有限样本的功能验收，不是模型准确率排名、长期 SLA 或压力测试。基础摘要有损，原文支持回查。当前运行时只支持单进程和只读工具；不包含多机分布式调度、异步 Job 回调、定时任务、多模态或 Anthropic 原生协议。它们属于原题架构设计部分，五模块书面答案已提供，不是编程题必需实现项。
