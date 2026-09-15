"""Single-process SQLite store. No DB transaction spans a model/tool await."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import sqlite3
import time
import uuid


def new_id():
    return uuid.uuid4().hex


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self._lock = open(str(path) + ".lock", "a")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock.close()
            raise RuntimeError("Database already in use; run a single server worker") from exc
        with self.db() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
                created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                request_id TEXT NOT NULL, input TEXT NOT NULL, status TEXT NOT NULL,
                answer TEXT, error_code TEXT, created_at REAL NOT NULL, finished_at REAL,
                steps INTEGER NOT NULL DEFAULT 0, used_tokens INTEGER NOT NULL DEFAULT 0,
                UNIQUE(session_id, request_id));
            CREATE TABLE IF NOT EXISTS messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id), run_id TEXT NOT NULL REFERENCES runs(id),
                payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, seq);
            CREATE TABLE IF NOT EXISTS tool_calls (
                run_id TEXT NOT NULL REFERENCES runs(id), call_id TEXT NOT NULL,
                name TEXT NOT NULL, arguments TEXT NOT NULL, status TEXT NOT NULL,
                result TEXT, PRIMARY KEY(run_id, call_id));
            CREATE TABLE IF NOT EXISTS traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
                kind TEXT NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS summaries (
                session_id TEXT PRIMARY KEY REFERENCES sessions(id), through_seq INTEGER NOT NULL,
                content TEXT NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT,
                content TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
            """)
        path.chmod(0o600)
        self.recover()

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def close(self):
        self._lock.close()

    def create_session(self, user_id, title="新会话"):
        sid = new_id()
        with self.db() as db:
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (sid, user_id, title[:80], time.time()))
        return self.session(user_id, sid)

    def session(self, user_id, session_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=? AND user_id=?", (session_id, user_id)).fetchone()
        if row is None:
            raise NotFound("会话不存在或不可访问")
        return dict(row)

    def sessions(self, user_id):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM sessions WHERE user_id=? ORDER BY created_at DESC", (user_id,))]

    def enqueue(self, user_id, sid, text, request_id):
        self.session(user_id, sid)
        with self.db() as db:
            row = db.execute("SELECT * FROM runs WHERE session_id=? AND request_id=?", (sid, request_id)).fetchone()
            if row:
                if row["input"] != text:
                    raise Conflict("同一 request_id 不能用于不同消息")
                return dict(row), False
            if db.execute("SELECT count(*) FROM runs WHERE session_id=? AND status IN ('queued','running')", (sid,)).fetchone()[0] >= 20:
                raise Conflict("当前会话等待队列已满")
            rid = new_id()
            db.execute("INSERT INTO runs(id,session_id,request_id,input,status,created_at) VALUES(?,?,?,?,?,?)",
                       (rid, sid, request_id, text, "queued", time.time()))
            return dict(db.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()), True

    def run(self, rid):
        with self.db() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise NotFound("执行不存在")
        return dict(row)

    def authorized_run(self, user_id, rid):
        run = self.run(rid)
        self.session(user_id, run["session_id"])
        return run

    def begin(self, rid):
        with self.db() as db:
            run = db.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
            if run["status"] != "queued":
                return False
            db.execute("UPDATE runs SET status='running' WHERE id=?", (rid,))
            self._message(db, run, {"role": "user", "content": run["input"]})
            db.execute("UPDATE sessions SET title=? WHERE id=? AND title='新会话'", (run["input"][:40], run["session_id"]))
        return True

    @staticmethod
    def _message(db, run, payload):
        db.execute("INSERT INTO messages(session_id,run_id,payload) VALUES (?,?,?)", (run["session_id"], run["id"], encode(payload)))

    def append_model(self, rid, message):
        with self.db() as db:
            run = db.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
            try:
                for call in message.get("tool_calls") or []:
                    db.execute("INSERT INTO tool_calls VALUES(?,?,?,?,?,NULL)", (rid, call["id"], call["function"]["name"], call["function"]["arguments"], "pending"))
                self._message(db, run, message)
            except sqlite3.IntegrityError as exc:
                raise Conflict("模型重复使用已提交的工具调用 ID") from exc

    def result(self, rid, cid, result):
        with self.db() as db:
            row = db.execute("SELECT status FROM tool_calls WHERE run_id=? AND call_id=?", (rid, cid)).fetchone()
            if not row or row[0] != "pending":
                return False
            run = db.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
            db.execute("UPDATE tool_calls SET status=?,result=? WHERE run_id=? AND call_id=?", ("succeeded" if result.get("ok") else "failed", encode(result), rid, cid))
            self._message(db, run, {"role": "tool", "tool_call_id": cid, "content": encode(result)})
        return True

    def finish(self, rid, status, answer, code=None, append=True):
        with self.db() as db:
            run = db.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
            if run["status"] not in ("queued", "running"):
                return
            # Close every open call so future Provider history remains valid.
            for call in db.execute("SELECT * FROM tool_calls WHERE run_id=? AND status='pending'", (rid,)).fetchall():
                result = {"ok": False, "error": {"code": code or status, "message": "执行中断；此调用没有已确认结果"}}
                db.execute("UPDATE tool_calls SET status='unknown',result=? WHERE run_id=? AND call_id=?", (encode(result), rid, call["call_id"]))
                self._message(db, run, {"role": "tool", "tool_call_id": call["call_id"], "content": encode(result)})
            if append and run["status"] == "running":
                self._message(db, run, {"role": "assistant", "content": answer})
            db.execute("UPDATE runs SET status=?,answer=?,error_code=?,finished_at=? WHERE id=?", (status, answer, code, time.time(), rid))
            db.execute("INSERT INTO traces(run_id,kind,data,created_at) VALUES(?,?,?,?)", (rid, "run_finished", encode({"status": status, "error_code": code}), time.time()))

    def recover(self):
        with self.db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM runs WHERE status IN ('running','queued')")]
        for rid in ids:
            self.finish(rid, "interrupted", "服务重启，上一执行已中断。已保存历史；请重新发送或继续，未确认的工具不会自动重放。", "process_restarted")

    def messages(self, sid):
        with self.db() as db:
            return [{"seq": r["seq"], "run_id": r["run_id"], "message": json.loads(r["payload"])} for r in db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY seq", (sid,))]

    def progress(self, rid, steps, used_tokens):
        with self.db() as db:
            db.execute("UPDATE runs SET steps=?,used_tokens=? WHERE id=?", (steps, used_tokens, rid))

    def trace(self, rid, kind, data):
        with self.db() as db:
            db.execute("INSERT INTO traces(run_id,kind,data,created_at) VALUES(?,?,?,?)", (rid, kind, encode(data), time.time()))

    def traces(self, rid):
        with self.db() as db:
            return [{**dict(r), "data": json.loads(r["data"])} for r in db.execute("SELECT * FROM traces WHERE run_id=? ORDER BY id", (rid,))]

    def summary(self, sid):
        with self.db() as db:
            row = db.execute("SELECT * FROM summaries WHERE session_id=?", (sid,)).fetchone()
        return dict(row) if row else None

    def save_summary(self, sid, through, content):
        with self.db() as db:
            db.execute("INSERT INTO summaries VALUES(?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET through_seq=excluded.through_seq,content=excluded.content,updated_at=excluded.updated_at WHERE excluded.through_seq >= summaries.through_seq", (sid, through, content, time.time()))

    def add_memory(self, user, sid, content):
        if sid:
            self.session(user, sid)
        mid, now = new_id(), time.time()
        with self.db() as db:
            db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?)", (mid, user, sid, content, now, now))
        return mid

    def memories(self, user, sid=None):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM memories WHERE user_id=? AND (session_id IS NULL OR session_id=?) ORDER BY updated_at DESC", (user, sid))]

    def update_memory(self, user, mid, content):
        with self.db() as db:
            if not db.execute("UPDATE memories SET content=?,updated_at=? WHERE id=? AND user_id=?", (content, time.time(), mid, user)).rowcount:
                raise NotFound("记忆不存在")

    def delete_memory(self, user, mid):
        with self.db() as db:
            if not db.execute("DELETE FROM memories WHERE id=? AND user_id=?", (mid, user)).rowcount:
                raise NotFound("记忆不存在")
