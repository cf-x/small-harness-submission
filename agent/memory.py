"""Explicit memories only. Current scope is filtered before relevance scoring."""
import re

from .session_store import Store


def terms(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9_]+", text.lower()))
    for chunk in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(chunk[i:i + 2] for i in range(max(1, len(chunk) - 1)))
    return words


def relevance(query: str, content: str) -> float:
    a, b = terms(query), terms(content)
    return len(a & b) / max(1, len(a))


class Memory:
    def __init__(self, store: Store):
        self.store = store

    def recall(self, user, sid, query, max_chars=1200):
        candidates = self.store.memories(user, sid)
        # "What did I ask you to remember" is an explicit memory browsing intent.
        browse = any(x in query.lower() for x in ("记住了什么", "我的偏好", "my preferences"))
        scored = [(relevance(query, m["content"]), m) for m in candidates]
        scored.sort(key=lambda x: (x[0], x[1]["updated_at"]), reverse=True)
        selected, size = [], 0
        for score, mem in scored:
            if score <= 0 and not browse:
                continue
            excerpt = mem["content"][:500]
            if size + len(excerpt) > max_chars or len(selected) >= 4:
                continue
            selected.append({"id": mem["id"], "scope": "session" if mem["session_id"] else "user",
                             "content": excerpt, "updated_at": mem["updated_at"], "source": "用户显式保存"})
            size += len(excerpt)
        return selected

