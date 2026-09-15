from collections import deque
import copy

import pytest

from agent.config import Settings
from agent.provider import normalize
from agent.runtime import Runtime
from agent.session_store import Store
from agent.tools import build_registry


def answer(text="完成"):
    return normalize({"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]})


def call(name="calculator", arguments='{"expression":"17*23"}', cid="call_1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": arguments}}


def tool_turn(*calls, content=None, reason="tool_calls"):
    return normalize({"choices": [{"message": {"role": "assistant", "content": content,
                      "tool_calls": list(calls)}, "finish_reason": reason}]})


class ScriptedProvider:
    """Deterministic test double; never imported by application code."""
    def __init__(self, turns=()):
        self.turns = deque(turns)
        self.requests = []

    async def generate(self, messages, tools):
        self.requests.append(copy.deepcopy({"messages": messages, "tools": tools}))
        value = self.turns.popleft()
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return await value(messages, tools)
        return value


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "agent.sqlite3")
    cfg = Settings(data_dir=tmp_path, max_retries=0, run_token_budget=300000)
    model = ScriptedProvider()
    registry = build_registry(store)
    runtime = Runtime(store, model, registry, cfg)
    sid = store.create_session("alice")["id"]
    yield store, cfg, model, runtime, sid
    store.close()


def assert_protocol(messages):
    pending = set()
    for message in messages:
        if message["role"] == "tool":
            assert message["tool_call_id"] in pending
            pending.remove(message["tool_call_id"])
        else:
            assert not pending, f"Unclosed tool calls before {message['role']}: {pending}"
            pending.update(c["id"] for c in message.get("tool_calls") or [])
    assert not pending

