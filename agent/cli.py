import argparse
import asyncio
import json
import sys
import uuid

from .config import Settings
from .provider import ChatProvider
from .runtime import Runtime
from .session_store import Store
from .tools import build_registry


async def ask(args, cfg):
    store = Store(cfg.data_dir / "agent.sqlite3")
    provider = ChatProvider(cfg)
    runtime = Runtime(store, provider, build_registry(store, cfg.tool_timeout), cfg)
    try:
        sid = args.session or store.create_session(args.user)["id"]
        run = runtime.submit(args.user, sid, args.text, uuid.uuid4().hex)
        result = await runtime.wait(run["id"])
        print(json.dumps({"session_id": sid, **result}, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "completed" else 1
    finally:
        await runtime.close()
        await provider.close()
        store.close()


def main():
    parser = argparse.ArgumentParser(description="Small Harness — local persistent agent")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Start the local chat UI and API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    query = sub.add_parser("ask", help="Run one message; --session resumes a conversation")
    query.add_argument("text")
    query.add_argument("--session")
    query.add_argument("--user", default="local-user", help="Local CLI owner; CLI is a trusted local interface")
    sub.add_parser("check-config", help="Validate config without displaying credentials")
    args = parser.parse_args()
    try:
        cfg = Settings.from_env()
    except (ValueError, TypeError) as exc:
        print("配置格式错误，请检查 .env 中的 URL、JSON 和预算参数。", file=sys.stderr)
        raise SystemExit(2) from exc
    if args.command == "check-config":
        print(json.dumps({"configured": cfg.configured, "model": cfg.model or None,
                          "auth_enabled": bool(cfg.auth_tokens), "input_budget": cfg.input_budget}, ensure_ascii=False))
        raise SystemExit(0 if cfg.configured else 1)
    if args.command == "ask":
        raise SystemExit(asyncio.run(ask(args, cfg)))
    if args.host not in ("127.0.0.1", "localhost", "::1") and not cfg.auth_tokens:
        parser.error("非本机监听必须设置 AGENT_AUTH_TOKENS")
    import uvicorn
    from .app import create_app
    uvicorn.run(create_app(cfg), host=args.host, port=args.port, workers=1, proxy_headers=False)


if __name__ == "__main__":
    main()

