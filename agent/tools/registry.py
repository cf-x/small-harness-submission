import asyncio
from dataclasses import dataclass
import json
import math
from typing import Awaitable, Callable

from jsonschema import Draft202012Validator, ValidationError


class ToolError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ToolContext:
    user_id: str
    session_id: str
    run_id: str


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict, ToolContext], Awaitable[dict]]
    timeout: float = 5
    max_result_chars: int = 7000


class Registry:
    def __init__(self):
        self.tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        if tool.name in self.tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        Draft202012Validator.check_schema(tool.parameters)
        self.tools[tool.name] = tool

    def schemas(self):
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                "parameters": t.parameters}} for t in self.tools.values()]

    async def execute(self, call: dict, context: ToolContext):
        try:
            name = call["function"]["name"]
            if name not in self.tools:
                raise ToolError("unknown_tool", "工具未注册")
            tool = self.tools[name]
            raw = call["function"]["arguments"]
            if len(raw) > 16000:
                raise ToolError("invalid_arguments", "工具参数过长")
            try:
                def reject_constant(_):
                    raise ValueError("Non-finite JSON value")
                def finite_float(value):
                    parsed = float(value)
                    if not math.isfinite(parsed):
                        raise ValueError("Non-finite JSON value")
                    return parsed
                def unique_pairs(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError("Duplicate JSON key")
                        result[key] = value
                    return result
                args = json.loads(raw, parse_constant=reject_constant,
                                  parse_float=finite_float, object_pairs_hook=unique_pairs)
            except (ValueError, RecursionError) as exc:
                raise ToolError("invalid_json", "工具参数需要合法 JSON 对象，不能包含重复键或非有限数值") from exc
            try:
                Draft202012Validator(tool.parameters).validate(args)
            except ValidationError as exc:
                raise ToolError("invalid_arguments", "参数不符合工具 Schema，请核对必填字段、类型和范围") from exc
            async with asyncio.timeout(tool.timeout):
                data = await tool.handler(args, context)
            encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
            if len(encoded) > tool.max_result_chars:
                raise ToolError("output_too_large", "工具结果超过上限，请缩小读取范围")
            return {"ok": True, "data": data}
        except ToolError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        except TimeoutError:
            return {"ok": False, "error": {"code": "tool_timeout", "message": "工具执行超时"}}
        except Exception:
            # User data and exception internals are not forwarded to the model or trace.
            return {"ok": False, "error": {"code": "tool_error", "message": "工具执行失败"}}
