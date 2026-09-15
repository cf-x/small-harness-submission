from contextlib import asynccontextmanager
from pathlib import Path
import secrets

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .provider import ChatProvider
from .runtime import Runtime
from .session_store import Store, NotFound, Conflict
from .tools import build_registry


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionInput(InputModel):
    title: str = Field(default="新会话", max_length=80)


class MessageInput(InputModel):
    text: str = Field(min_length=1, max_length=20000)
    request_id: str = Field(min_length=1, max_length=128)


class MemoryInput(InputModel):
    content: str = Field(min_length=1, max_length=1000)
    session_id: str | None = None


class MemoryEdit(InputModel):
    content: str = Field(min_length=1, max_length=1000)


def create_app(settings: Settings | None = None, provider=None):
    cfg = settings or Settings.from_env()
    cfg.validate()

    @asynccontextmanager
    async def lifespan(app):
        store = Store(cfg.data_dir / "agent.sqlite3")
        model = provider or ChatProvider(cfg)
        app.state.store = store
        app.state.runtime = Runtime(store, model, build_registry(store, cfg.tool_timeout), cfg)
        try:
            yield
        finally:
            await app.state.runtime.close()
            if hasattr(model, "close"):
                await model.close()
            store.close()

    app = FastAPI(title="Small Harness", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    # No wildcard hostname in local mode: rejects DNS rebinding to loopback.
    if not cfg.auth_tokens:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("x-harness-client") != "web":
                return JSONResponse({"detail": "Missing X-Harness-Client: web"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        return response

    def user(request: Request):
        if cfg.auth_tokens:
            value = request.headers.get("authorization", "")
            token = value.removeprefix("Bearer ") if value.startswith("Bearer ") else ""
            for known, owner in cfg.auth_tokens.items():
                if secrets.compare_digest(known, token):
                    return owner
            raise HTTPException(401, "需要有效的访问令牌")
        if not request.client or request.client.host not in ("127.0.0.1", "::1"):
            raise HTTPException(403, "未启用认证时只允许本机访问")
        return "local-user"

    @app.exception_handler(NotFound)
    async def missing(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/api/status")
    def status():
        return {"configured": cfg.configured, "model": cfg.model or None,
                "auth_required": bool(cfg.auth_tokens), "max_steps": cfg.max_steps,
                "context_tokens": cfg.context_tokens, "search_mode": "mock"}

    @app.get("/api/sessions")
    def sessions(request: Request, owner=Depends(user)):
        return request.app.state.store.sessions(owner)

    @app.post("/api/sessions", status_code=201)
    def create_session(body: SessionInput, request: Request, owner=Depends(user)):
        return request.app.state.store.create_session(owner, body.title)

    @app.get("/api/sessions/{sid}")
    def session(sid: str, request: Request, owner=Depends(user)):
        store = request.app.state.store
        info = store.session(owner, sid)
        rows = store.messages(sid)
        for row in rows:
            row["message"].pop("reasoning_content", None)
        with store.db() as db:
            runs = [dict(r) for r in db.execute("SELECT * FROM runs WHERE session_id=? ORDER BY created_at DESC LIMIT 20", (sid,))]
        return {"session": info, "messages": rows, "runs": runs, "summary": store.summary(sid)}

    @app.post("/api/sessions/{sid}/messages", status_code=202)
    async def submit(sid: str, body: MessageInput, request: Request, owner=Depends(user)):
        try:
            return request.app.state.runtime.submit(owner, sid, body.text, body.request_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/runs/{rid}")
    def run(rid: str, request: Request, owner=Depends(user)):
        return request.app.state.store.authorized_run(owner, rid)

    @app.get("/api/runs/{rid}/trace")
    def trace(rid: str, request: Request, owner=Depends(user)):
        request.app.state.store.authorized_run(owner, rid)
        return request.app.state.store.traces(rid)

    @app.post("/api/runs/{rid}/cancel")
    async def cancel(rid: str, request: Request, owner=Depends(user)):
        return await request.app.state.runtime.cancel(owner, rid)

    @app.get("/api/memories")
    def memories(request: Request, session_id: str | None = None, owner=Depends(user)):
        store = request.app.state.store
        if session_id:
            store.session(owner, session_id)
        return store.memories(owner, session_id)

    @app.post("/api/memories", status_code=201)
    def add_memory(body: MemoryInput, request: Request, owner=Depends(user)):
        return {"id": request.app.state.store.add_memory(owner, body.session_id, body.content)}

    @app.patch("/api/memories/{mid}")
    def edit_memory(mid: str, body: MemoryEdit, request: Request, owner=Depends(user)):
        request.app.state.store.update_memory(owner, mid, body.content)
        return {"ok": True}

    @app.delete("/api/memories/{mid}")
    def delete_memory(mid: str, request: Request, owner=Depends(user)):
        request.app.state.store.delete_memory(owner, mid)
        return {"ok": True}

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app
