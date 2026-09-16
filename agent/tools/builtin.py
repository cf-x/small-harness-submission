import ast
from decimal import Decimal, DecimalException, localcontext
from fractions import Fraction
import hashlib
import json
from pathlib import Path

from ..memory import relevance
from ..session_store import Store
from .registry import Registry, Tool, ToolContext, ToolError


def calculate(expression: str):
    if len(expression) > 200:
        raise ToolError("invalid_expression", "表达式过长")
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ToolError("invalid_expression", "请使用数字及 + - * / // % ** 括号") from exc
    if len(list(ast.walk(tree))) > 80:
        raise ToolError("invalid_expression", "表达式过于复杂")

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            # Parse the original literal, avoiding binary floating-point rounding.
            result = Decimal(ast.get_source_segment(expression, node).replace("_", ""))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            result = value if isinstance(node.op, ast.UAdd) else value.copy_negate()
        elif isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                result = left + right
            elif isinstance(node.op, ast.Sub):
                result = left - right
            elif isinstance(node.op, ast.Mult):
                result = left * right
            elif isinstance(node.op, ast.Div):
                result = left / right
            elif isinstance(node.op, (ast.FloorDiv, ast.Mod)):
                # Rounding before floor can change the quotient and even the
                # sign of the remainder. Work with the exact decimal operands.
                quotient = Fraction(left) // Fraction(right)
                if isinstance(node.op, ast.FloorDiv):
                    result = Decimal(quotient)
                else:
                    a, b = left.as_tuple(), right.as_tuple()
                    with localcontext() as exact:
                        exact.prec = max(len(a.digits), len(b.digits)) + abs(a.exponent - b.exponent) + 2
                        result = left - Decimal(quotient) * right
            elif isinstance(node.op, ast.Pow) and right == right.to_integral_value() and abs(right) <= 20:
                result = left ** int(right)
            else:
                raise ToolError("invalid_expression", "操作不允许；指数须为绝对值不超过 20 的整数")
        else:
            raise ToolError("invalid_expression", "只允许数字和算术运算")
        # Zero has no meaningful scale. Normalize before formatting or using its
        # exponent to size exact arithmetic (e.g. 0e-10000000).
        if result.is_zero():
            result = Decimal(0)
        if not result.is_finite() or abs(result) > Decimal("1e50") or (result and abs(result) < Decimal("1e-100")):
            raise ToolError("calculation_limit", "计算超出数值范围")
        return result

    try:
        with localcontext() as ctx:
            ctx.prec = 28
            ctx.Emax, ctx.Emin = 100, -100
            result = visit(tree.body)
            value = format(result, "f")
            if "." in value:
                value = value.rstrip("0").rstrip(".")
            return {"expression": expression, "value": value, "precision": "28 decimal digits"}
    except (DecimalException, ValueError, OverflowError, ZeroDivisionError) as exc:
        raise ToolError("arithmetic_error", "计算失败，请检查除零或数字格式") from exc


def obj(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def build_registry(store: Store, timeout=5, documents: dict | None = None):
    root = Path(__file__).resolve().parents[1] / "fixtures"
    # doc_id maps to a trusted path and optional owner set. No model-chosen paths.
    catalog = documents if documents is not None else {
        "handbook": (root / "handbook.md", None),
        "assignment": (root / "assignment.md", None),
    }
    registry = Registry()

    async def calculator(args, ctx):
        return calculate(args["expression"])

    async def search(args, ctx):
        fixtures = [
            {"title": "Agent Runtime 基础", "snippet": "模型提出工具调用，Runtime 验证参数、执行工具并把结果回传。", "source": "fixture://agent-runtime"},
            {"title": "Session 与 Memory", "snippet": "Session 隔离对话；显式用户记忆可按作用域召回。", "source": "fixture://session-memory"},
            {"title": "Context 压缩", "snippet": "保留完整工具交互和最新输入，早期历史形成有来源的摘录。", "source": "fixture://context"},
        ]
        hits = sorted(fixtures, key=lambda x: relevance(args["query"], x["title"] + x["snippet"]), reverse=True)
        return {"mock": True, "notice": "离线模拟检索，非实时网络信息", "results": [h for h in hits if relevance(args["query"], h["title"] + h["snippet"]) > 0][:args.get("limit", 3)]}

    async def read_docs(args, ctx):
        entry = catalog.get(args["doc_id"])
        if entry is None or (entry[1] is not None and ctx.user_id not in entry[1]):
            raise ToolError("document_not_found", "文档不存在或不可访问")
        path = entry[0]
        if path.is_symlink() or not path.is_file():
            raise ToolError("document_not_found", "文档文件不可用")
        # Only trusted, bounded fixture files are exposed.
        if path.stat().st_size > 1_000_000:
            raise ToolError("document_too_large", "文档超过本地读取上限")
        text = path.read_text(encoding="utf-8")
        offset, limit = args.get("offset", 0), args.get("limit", 2000)
        if offset > len(text):
            raise ToolError("invalid_offset", "offset 超过文档长度")
        end = min(len(text), offset + limit)
        return {"doc_id": args["doc_id"], "source": "local-doc:" + args["doc_id"],
                "version": hashlib.sha256(text.encode()).hexdigest()[:12], "content": text[offset:end],
                "offset": offset, "next_offset": end if end < len(text) else None, "total_chars": len(text)}

    async def read_history(args, ctx: ToolContext):
        store.session(ctx.user_id, ctx.session_id)
        rows = store.messages(ctx.session_id)
        hits = []
        tool_names = {}
        for row in rows:
            # Do not recursively recall the active tool chain or its previous history results.
            if row["run_id"] == ctx.run_id:
                continue
            message = row["message"]
            if message.get("tool_calls"):
                for c in message["tool_calls"]:
                    tool_names[(row["run_id"], c["id"])] = c["function"]["name"]
                continue
            if message["role"] == "tool" and tool_names.get((row["run_id"], message["tool_call_id"])) == "read_history":
                continue
            if args.get("seq") is not None and row["seq"] != args["seq"]:
                continue
            text = message.get("content") or ""
            score = relevance(args.get("query", ""), text)
            if args.get("query") and score <= 0:
                continue
            offset = args.get("offset", 0)
            if offset > len(text):
                raise ToolError("invalid_offset", "offset 超过该条历史内容长度")
            end = min(len(text), offset + 1200)
            hits.append((score, row["seq"], {"seq": row["seq"], "role": message["role"],
                         "content": text[offset:end], "truncated": offset > 0 or end < len(text),
                         "offset": offset, "next_offset": end if end < len(text) else None,
                         "total_chars": len(text)}))
        hits.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return {"scope": "current_session", "results": [h[2] for h in hits[:args.get("limit", 3)]]}

    registry.register(Tool("calculator", "精确算术计算，支持 + - * / // % **，表达式不超过 200 字符。", obj({"expression": {"type": "string", "minLength": 1, "maxLength": 200}}, ["expression"]), calculator, timeout))
    registry.register(Tool("search", "模拟搜索 Agent/Session/Memory/Context 示例资料；结果须注明 mock，不能用于实时查询。", obj({"query": {"type": "string", "minLength": 1, "maxLength": 300}, "limit": {"type": "integer", "minimum": 1, "maximum": 3}}, ["query"]), search, timeout))
    registry.register(Tool("read_docs", "读取预置文档：handbook（运行手册），assignment（原笔试题）。使用 offset 分页。", obj({"doc_id": {"type": "string", "minLength": 1, "maxLength": 100}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 4000}}, ["doc_id"]), read_docs, timeout))
    history_schema = obj({"query": {"type": "string", "maxLength": 200}, "seq": {"type": "integer", "minimum": 1}, "limit": {"type": "integer", "minimum": 1, "maximum": 3}, "offset": {"type": "integer", "minimum": 0, "description": "指定 seq 后按字符偏移读取，每页最多 1200 字符；使用上页 next_offset 继续。"}}, [])
    history_schema["dependentRequired"] = {"offset": ["seq"]}
    registry.register(Tool("read_history", "回查当前会话历史用户输入、答复和工具结果；按 query 检索全文或 seq 定位。长内容用返回的 seq 和 next_offset（作为 offset）分页，直到 next_offset 为 null。", history_schema, read_history, timeout))
    return registry
