import asyncio
import json

import pytest

from agent.context import ContextBuilder, ContextLimit, estimate_tokens
from agent.memory import Memory
from agent.tools import Tool, ToolContext, ToolError, build_registry
from agent.tools.builtin import calculate
from conftest import call, assert_protocol


@pytest.mark.parametrize("expression, expected", [
    ("17*23", "391"), ("0.1+0.2", "0.3"), ("(2+3)**2", "25"),
    ("-5//2", "-3"), ("-5%2", "1"), ("1/4", "0.25"), ("2**-2", "0.25")])
def test_calculator(expression, expected):
    assert calculate(expression)["value"] == expected


@pytest.mark.parametrize("expression", ["__import__('os').system('ls')", "2**10000000", "1/0", "1e100", "1e-101", "True+1", "[1][0]", "x+1", "1+"*200, "2**(2**20)"])
def test_rejects_unsafe_math(expression):
    with pytest.raises(ToolError):
        calculate(expression)


@pytest.mark.parametrize("raw, code", [
    ('{"expression":"2+2","extra":true}', "invalid_arguments"),
    ('{"expression":true}', "invalid_arguments"), ('{}', "invalid_arguments"),
    ('{"expression":"1","expression":"2"}', "invalid_json"),
    ('{"expression":NaN}', "invalid_json"), ('[]', "invalid_arguments")])
async def test_argument_validation(setup, raw, code):
    result = await setup[3].registry.execute(call(arguments=raw), ToolContext("alice", setup[4], "r"))
    assert result["error"]["code"] == code


async def test_mock_search_and_empty_results(setup):
    registry = setup[3].registry; ctx = ToolContext("alice", setup[4], "r")
    result = await registry.execute(call("search", '{"query":"Context"}'), ctx)
    assert result["data"]["mock"] is True and result["data"]["results"]
    result = await registry.execute(call("search", '{"query":"zzzzxyzz"}'), ctx)
    assert result["data"]["results"] == []


async def test_docs_paging_and_access_control(setup, tmp_path):
    store, cfg, model, runtime, sid = setup
    path = tmp_path / "private.md"; path.write_text("a" * 5000)
    registry = build_registry(store, documents={"private": (path, {"alice"})})
    result = await registry.execute(call("read_docs", '{"doc_id":"private","limit":100}'), ToolContext("alice", sid, "r"))
    assert result["data"]["next_offset"] == 100
    for owner, doc in [("bob", "private"), ("alice", "../../etc/passwd")]:
        result = await registry.execute(call("read_docs", json.dumps({"doc_id": doc})), ToolContext(owner, sid, "r"))
        assert result["error"]["code"] == "document_not_found"
    link = tmp_path / "link.md"; link.symlink_to(path)
    registry = build_registry(store, documents={"link": (link, None)})
    result = await registry.execute(call("read_docs", '{"doc_id":"link"}'), ToolContext("alice", sid, "r"))
    assert result["error"]["code"] == "document_not_found"


async def test_timeout_and_handler_error(setup):
    registry = setup[3].registry
    async def slow(args, ctx):
        await asyncio.sleep(1)
    async def broken(args, ctx):
        raise RuntimeError("SECRET SHOULD NOT LEAK")
    registry.register(Tool("slow", "test", {"type":"object"}, slow, timeout=.01))
    registry.register(Tool("broken", "test", {"type":"object"}, broken))
    ctx = ToolContext("alice", setup[4], "r")
    assert (await registry.execute(call("slow", "{}"), ctx))["error"]["code"] == "tool_timeout"
    result = await registry.execute(call("broken", "{}"), ctx)
    assert result["error"]["code"] == "tool_error" and "SECRET" not in str(result)


def seed_run(store, sid, text, result, index):
    run, _ = store.enqueue("alice", sid, text, str(index)); store.begin(run["id"])
    store.append_model(run["id"], {"role": "assistant", "content": result})
    store.finish(run["id"], "completed", result, append=False)
    return run


async def test_200_round_compaction_keeps_corrections_and_archive(setup):
    store, cfg, model, runtime, sid = setup
    cfg.context_tokens = 9000
    for i in range(200):
        text = f"第{i}轮讨论，代号 Cedar，预算{1000+i}元。" * 3
        seed_run(store, sid, text, f"已记录第{i}轮预算", i)
    run, _ = store.enqueue("alice", sid, "最新预算改为800元，请以此为准", "current"); store.begin(run["id"])
    tools = runtime.registry.schemas()
    messages, cost = runtime.context.build("alice", sid, run["id"], run["input"], tools)
    assert cost <= cfg.input_budget
    assert messages[-1]["content"] == run["input"]
    assert len(store.messages(sid)) == 401
    assert store.summary(sid)["through_seq"] > 0
    assert_protocol(messages)
    result = await runtime.registry.execute(call("read_history", '{"query":"第0轮"}'), ToolContext("alice", sid, run["id"]))
    assert result["data"]["results"]


def test_compaction_never_splits_tool_pairs(setup):
    store, cfg, model, runtime, sid = setup
    cfg.context_tokens = 10000
    for i in range(8):
        seed_run(store, sid, "旧话题"*200, "旧答案"*200, i)
    run, _ = store.enqueue("alice", sid, "当前问题", "current");store.begin(run["id"])
    store.append_model(run["id"], {"role":"assistant", "tool_calls":[call()], "content":None})
    store.result(run["id"], "call_1", {"ok":True,"data":{"value":"391"}})
    messages, _ = runtime.context.build("alice",sid,run["id"],"当前问题",runtime.registry.schemas())
    assert_protocol(messages)
    assert messages[-1]["role"] == "tool"


def test_single_oversized_input_fails_without_erasing_history(setup):
    store,cfg,model,runtime,sid=setup
    cfg.context_tokens=6000
    run,_=store.enqueue("alice",sid,"中"*10000,"huge");store.begin(run["id"])
    with pytest.raises(ContextLimit):
        runtime.context.build("alice",sid,run["id"],run["input"],runtime.registry.schemas())
    assert len(store.messages(sid)[0]["message"]["content"])==10000


def test_memory_scope_update_delete(setup):
    store,cfg,model,runtime,sid=setup
    other=store.create_session("alice")["id"]
    a=store.add_memory("alice",sid,"项目 Cedar 预算800元")
    shared=store.add_memory("alice",None,"项目 Cedar 使用Python")
    store.add_memory("bob",None,"项目 Cedar 预算900元")
    mem=Memory(store)
    assert {m["id"] for m in mem.recall("alice",sid,"项目 Cedar")}=={a,shared}
    assert {m["id"] for m in mem.recall("alice",other,"项目 Cedar")}=={shared}
    store.update_memory("alice",a,"项目 Cedar 预算600元")
    assert any("600" in m["content"] for m in mem.recall("alice",sid,"项目 Cedar"))
    store.delete_memory("alice",a)
    assert {m["id"] for m in mem.recall("alice",sid,"项目 Cedar")}=={shared}
    assert mem.recall("alice",sid,"qzxyqz") == []


async def test_archive_can_recover_original_tool_number(setup):
    store,cfg,model,runtime,sid=setup
    run,_=store.enqueue("alice",sid,"算一下","old");store.begin(run["id"])
    store.append_model(run["id"],{"role":"assistant","content":None,"tool_calls":[call()]})
    store.result(run["id"],"call_1",{"ok":True,"data":{"value":"391"}})
    store.finish(run["id"],"completed","已计算")
    tool_row=next(r for r in store.messages(sid) if r["message"]["role"]=="tool")
    result=await runtime.registry.execute(call("read_history",json.dumps({"seq":tool_row["seq"]})),ToolContext("alice",sid,"new"))
    assert "391" in result["data"]["results"][0]["content"]


def test_summary_commit_cannot_regress_its_boundary(setup):
    store,cfg,model,runtime,sid=setup
    store.save_summary(sid,100,"latest")
    store.save_summary(sid,50,"stale")
    assert store.summary(sid)["content"]=="latest"
