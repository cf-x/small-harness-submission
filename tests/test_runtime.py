import asyncio
import json
import uuid

import pytest

from agent.provider import ProviderError
from agent.tools import Tool
from agent.session_store import Conflict, NotFound
from conftest import answer, call, tool_turn, assert_protocol


async def send(setup, text="计算", request_id=None, sid=None):
    store, cfg, model, runtime, default_sid = setup
    run = runtime.submit("alice", sid or default_sid, text, request_id or uuid.uuid4().hex)
    return await runtime.wait(run["id"])


async def test_direct_answer(setup):
    store, cfg, model, runtime, sid = setup
    model.turns.append(answer("你好"))
    result = await send(setup, "你好")
    assert result["status"] == "completed" and result["answer"] == "你好"
    assert len(store.messages(sid)) == 2
    assert len(model.requests[0]["tools"]) == 4


async def test_tool_loop_and_followup(setup):
    store, cfg, model, runtime, sid = setup
    model.turns.extend([tool_turn(call(), content="开始计算"), answer("391"),
                        tool_turn(call(arguments='{"expression":"391+9"}', cid="call_2")), answer("400")])
    first = await send(setup, "17乘23")
    second = await send(setup, "把刚才结果加9")
    assert first["answer"] == "391" and second["answer"] == "400"
    results = [json.loads(r["message"]["content"])["data"]["value"] for r in store.messages(sid) if r["message"]["role"] == "tool"]
    assert results == ["391", "400"]
    for req in model.requests:
        assert_protocol(req["messages"])
    assert any(e["kind"] == "tool_completed" for e in store.traces(first["id"]))


async def test_mixed_tools_close_all_calls(setup):
    store, cfg, model, runtime, sid = setup
    model.turns.extend([tool_turn(call(), call("missing", "{}", "c2"), call(arguments="bad json", cid="c3")), answer()])
    result = await send(setup)
    assert result["status"] == "completed"
    rows = store.messages(sid)
    values = [json.loads(r["message"]["content"]) for r in rows if r["message"]["role"] == "tool"]
    assert values[0]["ok"] is True
    assert [v["error"]["code"] for v in values[1:]] == ["unknown_tool", "invalid_json"]
    assert_protocol(model.requests[-1]["messages"])


async def test_pure_followup_sees_prior_answer(setup):
    store, cfg, model, runtime, sid = setup
    model.turns.extend([answer("第一点工具闭环，第二点会话隔离。"), answer("会话隔离按 session_id。")])
    await send(setup, "介绍两点")
    await send(setup, "解释第二点")
    assert any("第二点会话隔离" in (m.get("content") or "") for m in model.requests[-1]["messages"])


async def test_step_limit_closed_protocol_and_resume(setup):
    store, cfg, model, runtime, sid = setup
    cfg.max_steps = 1
    model.turns.extend([tool_turn(call()), answer("继续完成")])
    result = await send(setup)
    assert result["error_code"] == "step_budget"
    assert await send(setup, "继续")
    assert_protocol(model.requests[-1]["messages"])


async def test_repeated_failures_stop(setup):
    store, cfg, model, runtime, sid = setup
    model.turns.extend(tool_turn(call("missing", "{}", str(i))) for i in range(3))
    result = await send(setup)
    assert result["error_code"] == "repeated_tool_error" and len(model.requests) == 3


@pytest.mark.parametrize("reason", ["length", "content_filter", "unsupported"])
async def test_nonfinal_responses_never_execute(setup, reason):
    store, cfg, model, runtime, sid = setup
    model.turns.append(tool_turn(call(), reason=reason))
    result = await send(setup)
    assert result["status"] == "stopped"
    assert not any(r["message"]["role"] == "tool" for r in store.messages(sid))


async def test_provider_failure_and_next_run(setup):
    store, cfg, model, runtime, sid = setup
    model.turns.extend([ProviderError("network_error", "连接失败"), answer()])
    assert (await send(setup))["error_code"] == "network_error"
    assert (await send(setup, "重试"))["status"] == "completed"


async def test_empty_response_stops(setup):
    setup[2].turns.append(answer(""))
    assert (await send(setup))["error_code"] == "empty_response"


async def test_duplicate_call_id_is_not_reexecuted(setup):
    store, cfg, model, runtime, sid = setup
    model.turns.extend([tool_turn(call()), tool_turn(call())])
    result = await send(setup)
    assert result["error_code"] == "protocol_conflict"
    assert len([r for r in store.messages(sid) if r["message"]["role"] == "tool"]) == 1
    assert_protocol([r["message"] for r in store.messages(sid)])


async def test_budget_blocks_request(setup):
    store, cfg, model, runtime, sid = setup
    cfg.run_token_budget = 1
    assert (await send(setup))["error_code"] == "token_budget"
    assert model.requests == []


async def test_same_request_is_idempotent(setup):
    store, cfg, model, runtime, sid = setup
    model.turns.append(answer())
    first = await send(setup, request_id="same")
    second = await send(setup, request_id="same")
    assert first["id"] == second["id"] and len(model.requests) == 1
    with pytest.raises(Conflict):
        runtime.submit("alice", sid, "different", "same")


async def test_two_windows_isolate_history(setup):
    store, cfg, model, runtime, sid = setup
    other = store.create_session("alice")["id"]
    model.turns.extend([answer("记住了ALPHA"), answer("记住了BETA"), answer("ALPHA"), answer("BETA")])
    await send(setup, "代号ALPHA")
    await send(setup, "代号BETA", sid=other)
    await send(setup, "代号？")
    await send(setup, "代号？", sid=other)
    assert "BETA" not in json.dumps(model.requests[2])
    assert "ALPHA" not in json.dumps(model.requests[3])
    with pytest.raises(NotFound):
        runtime.submit("bob", sid, "窥探", "x")


async def test_busy_queue_is_fifo_without_interleaving(setup):
    store, cfg, model, runtime, sid = setup
    entered, release = asyncio.Event(), asyncio.Event()
    async def block(messages, tools):
        entered.set()
        await release.wait()
        return answer("第一条完成")
    model.turns.extend([block, answer("第二条完成")])
    first = runtime.submit("alice", sid, "第一条", "1")
    await entered.wait()
    second = runtime.submit("alice", sid, "第二条", "2")
    await asyncio.sleep(0)
    assert store.run(second["id"])["status"] == "queued"
    assert not any(r["message"].get("content") == "第二条" for r in store.messages(sid))
    release.set()
    await runtime.wait(first["id"]); await runtime.wait(second["id"])
    assert [r["message"]["content"] for r in store.messages(sid)] == ["第一条", "第一条完成", "第二条", "第二条完成"]


async def test_other_session_is_not_blocked(setup):
    store, cfg, model, runtime, sid = setup
    entered, release = asyncio.Event(), asyncio.Event()
    async def block(messages, tools):
        entered.set(); await release.wait(); return answer()
    model.turns.extend([block, answer("other")])
    first = runtime.submit("alice", sid, "slow", "1")
    await entered.wait()
    other = store.create_session("alice")["id"]
    second = await send(setup, "fast", sid=other)
    assert second["status"] == "completed"
    release.set(); await runtime.wait(first["id"])


async def test_cancel_closes_pending_tools_and_continues(setup):
    store, cfg, model, runtime, sid = setup
    entered = asyncio.Event()
    async def slow(args, context):
        entered.set(); await asyncio.Event().wait()
    runtime.registry.register(Tool("slow", "test", {"type": "object"}, slow))
    model.turns.extend([tool_turn(call("slow", "{}"), call(cid="second")), answer("新目标")])
    run = runtime.submit("alice", sid, "旧目标", "1")
    await entered.wait()
    result = await runtime.cancel("alice", run["id"])
    assert result["status"] == "cancelled"
    assert_protocol([r["message"] for r in store.messages(sid)])
    await send(setup, "新目标")
    assert_protocol(model.requests[-1]["messages"])


async def test_cancel_before_task_started(setup):
    store, cfg, model, runtime, sid = setup
    run = runtime.submit("alice", sid, "不要开始", "1")
    result = await runtime.cancel("alice", run["id"])
    assert result["status"] == "cancelled" and store.messages(sid) == []


async def test_run_deadline(setup):
    store, cfg, model, runtime, sid = setup
    cfg.run_timeout = .02
    async def blocked(messages, tools):
        await asyncio.sleep(1); return answer()
    model.turns.append(blocked)
    assert (await send(setup))["error_code"] == "run_timeout"
