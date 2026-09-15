"""Hand-written bounded loop, FIFO per session, concurrent across sessions."""
import asyncio
from collections import defaultdict
import hashlib
import time

from .config import Settings
from .context import ContextBuilder, ContextLimit
from .provider import Provider, ProviderError
from .session_store import Store, Conflict
from .tools import Registry, ToolContext


class Runtime:
    def __init__(self, store: Store, provider: Provider, registry: Registry, settings: Settings):
        self.store, self.provider, self.registry, self.settings = store, provider, registry, settings
        self.context = ContextBuilder(store, settings)
        self.tasks: dict[str, asyncio.Task] = {}
        self.locks = defaultdict(asyncio.Lock)
        self.closed = False

    def submit(self, user, sid, text, request_id):
        if self.closed:
            raise Conflict("服务正在停止")
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise ValueError("消息须为 1—20000 字符")
        if not request_id or len(request_id) > 128:
            raise ValueError("request_id 长度须为 1—128")
        run, created = self.store.enqueue(user, sid, text.strip(), request_id)
        if created:
            task = asyncio.create_task(self._drive(user, run))
            self.tasks[run["id"]] = task
            task.add_done_callback(lambda _, rid=run["id"]: self.tasks.pop(rid, None))
        return run

    async def wait(self, rid):
        task = self.tasks.get(rid)
        if task:
            # Client disconnect must not cancel an accepted run.
            await asyncio.shield(task)
        return self.store.run(rid)

    async def cancel(self, user, rid):
        run = self.store.authorized_run(user, rid)
        task = self.tasks.get(rid)
        if task:
            # Mark queued runs too: tasks can be cancelled before entering _drive.
            self.store.finish(rid, "cancelled", "已停止本次执行。已完成的只读工具结果保留，可继续对话。", "user_cancelled")
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self.store.run(rid)

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.items())
        for rid, task in tasks:
            self.store.finish(rid, "interrupted", "服务停止，执行已中断；历史已保存。", "service_stopped")
            task.cancel()
        await asyncio.gather(*(t for _, t in tasks), return_exceptions=True)

    async def _drive(self, user, run):
        rid, sid = run["id"], run["session_id"]
        try:
            async with self.locks[sid]:
                if not self.store.begin(rid):
                    return
                self.store.trace(rid, "run_started", {"max_steps": self.settings.max_steps})
                async with asyncio.timeout(self.settings.run_timeout):
                    await self._loop(user, run)
        except asyncio.CancelledError:
            self.store.finish(rid, "cancelled", "执行已取消，可继续会话。", "cancelled")
        except TimeoutError:
            self._stop(rid, "run_timeout", "本次执行达到总时间上限")
        except ContextLimit as exc:
            self._stop(rid, "context_limit", str(exc))
        except ProviderError as exc:
            self._stop(rid, exc.code, str(exc))
        except Conflict as exc:
            self._stop(rid, "protocol_conflict", str(exc))
        except Exception:
            # No raw exception body: might contain remote response secrets.
            self._stop(rid, "internal_error", "执行遇到内部错误，请查看运行状态或重新尝试")

    def _stop(self, rid, code, message):
        self.store.finish(rid, "stopped", message + "。已确认的工具结果保存在历史中；未完成部分可继续处理。", code)

    async def _loop(self, user, run):
        cfg = self.settings
        rid, sid = run["id"], run["session_id"]
        spent, repeated_errors = 0, defaultdict(int)
        schemas = self.registry.schemas()
        for step in range(1, cfg.max_steps + 1):
            messages, estimate = self.context.build(user, sid, rid, run["input"], schemas)
            reservation = (estimate + cfg.max_output_tokens) * (cfg.max_retries + 1)
            if spent + reservation > cfg.run_token_budget:
                self._stop(rid, "token_budget", "本次执行达到 token 预算上限")
                return
            start = time.monotonic()
            turn = await self.provider.generate(messages, schemas)
            usage = turn.usage.get("total_tokens")
            if usage is None:
                usage = estimate + cfg.max_output_tokens
            # Retry usage is unreported; charge conservative reservations for retries.
            spent += usage + (turn.attempts - 1) * (estimate + cfg.max_output_tokens)
            self.store.progress(rid, step, spent)
            self.store.trace(rid, "model_completed", {"step": step, "duration_ms": round((time.monotonic() - start) * 1000),
                "stop_reason": turn.stop_reason, "usage": turn.usage, "charged_tokens": spent,
                "request_id": turn.request_id, "attempts": turn.attempts,
                "has_reasoning": bool(turn.message.get("reasoning_content"))})
            # Do not execute even valid-looking calls in a truncated/refused response.
            if turn.stop_reason in ("length", "content_filter") or turn.message.get("refusal"):
                self._stop(rid, "model_" + turn.stop_reason, "模型输出被截断或拒绝，未执行该响应的工具")
                return
            if turn.stop_reason not in ("stop", "tool_calls"):
                self._stop(rid, "unknown_stop_reason", "模型返回了不支持的停止状态")
                return
            self.store.append_model(rid, turn.message)
            if turn.tool_calls:
                stop_after_batch = False
                for call in turn.tool_calls:
                    start = time.monotonic()
                    self.store.trace(rid, "tool_started", {"step": step, "call_id": call["id"], "name": call["function"]["name"]})
                    result = await self.registry.execute(call, ToolContext(user, sid, rid))
                    self.store.result(rid, call["id"], result)
                    self.store.trace(rid, "tool_completed", {"step": step, "call_id": call["id"], "name": call["function"]["name"],
                        "ok": result["ok"], "error_code": result.get("error", {}).get("code"),
                        "duration_ms": round((time.monotonic() - start) * 1000)})
                    if not result["ok"]:
                        signature = hashlib.sha256((call["function"]["name"] + call["function"]["arguments"] + result["error"]["code"]).encode()).hexdigest()
                        repeated_errors[signature] += 1
                        stop_after_batch |= repeated_errors[signature] >= 3
                if stop_after_batch:
                    self._stop(rid, "repeated_tool_error", "同一工具操作连续失败三次，已停止重试")
                    return
                continue
            if turn.stop_reason == "stop" and (turn.message.get("content") or "").strip():
                self.store.finish(rid, "completed", turn.message["content"], append=False)
                return
            self._stop(rid, "empty_response", "模型没有返回可用答案或工具调用")
            return
        self._stop(rid, "step_budget", "达到最大模型轮次，任务尚未完整完成")
