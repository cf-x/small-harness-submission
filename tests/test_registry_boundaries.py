import pytest

from agent.tools import Registry, Tool, ToolContext
from conftest import call


@pytest.mark.parametrize("number", ["1e999", "-1e999", "NaN", "Infinity"])
async def test_nonfinite_numeric_arguments_never_reach_custom_handler(number):
    registry = Registry()
    received = []

    async def numeric_handler(args, context):
        received.append(args)
        return {"accepted": True}

    registry.register(Tool("numeric", "Numeric extension", {
        "type": "object", "properties": {"value": {"type": "number"}},
        "required": ["value"], "additionalProperties": False,
    }, numeric_handler))
    result = await registry.execute(call("numeric", '{"value":' + number + '}'),
                                    ToolContext("alice", "session", "run"))
    assert result["error"]["code"] == "invalid_json"
    assert received == []
    valid = await registry.execute(call("numeric", '{"value":1.25}'),
                                   ToolContext("alice", "session", "run"))
    assert valid["ok"] is True
    assert received == [{"value": 1.25}]
