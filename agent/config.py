from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


@dataclass
class Settings:
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    extra_body: dict = field(default_factory=dict)
    data_dir: Path = Path("data")
    auth_tokens: dict[str, str] = field(default_factory=dict, repr=False)
    llm_timeout: float = 30
    max_retries: int = 2
    max_output_tokens: int = 2048
    context_tokens: int = 16384
    max_steps: int = 8
    run_timeout: float = 120
    run_token_budget: int = 80000
    tool_timeout: float = 5

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)

    @property
    def input_budget(self) -> int:
        return self.context_tokens - self.max_output_tokens - 512

    def validate(self):
        if self.base_url:
            u = urlsplit(self.base_url)
            if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
                raise ValueError("LLM_BASE_URL must be an HTTP(S) URL without credentials/query/fragment")
            if u.scheme == "http" and u.hostname not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("Remote LLM endpoints require HTTPS")
        if self.input_budget < 2048:
            raise ValueError("Context must leave at least 2048 input tokens")
        if not 1 <= self.max_steps <= 100 or not 0 <= self.max_retries <= 5:
            raise ValueError("Invalid step/retry budget")
        if min(self.llm_timeout, self.run_timeout, self.tool_timeout, self.run_token_budget, self.max_output_tokens) <= 0:
            raise ValueError("Budgets and timeouts must be positive")
        if not isinstance(self.extra_body, dict):
            raise ValueError("LLM_EXTRA_BODY must be an object")
        reserved = {"model", "messages", "tools", "tool_choice", "stream", "max_tokens", "n"}
        if reserved & self.extra_body.keys():
            raise ValueError("LLM_EXTRA_BODY cannot override core protocol fields")
        if not isinstance(self.auth_tokens, dict) or any(
            not isinstance(k, str) or len(k) < 24 or not isinstance(v, str) or not v
            for k, v in self.auth_tokens.items()
        ):
            raise ValueError("AGENT_AUTH_TOKENS must map tokens of at least 24 characters to user names")

    @classmethod
    def from_env(cls):
        load_dotenv(override=False)
        result = cls(
            base_url=os.getenv("LLM_BASE_URL", "").strip().rstrip("/"),
            model=os.getenv("LLM_MODEL", "").strip(),
            api_key=os.getenv("LLM_API_KEY", "").strip(),
            extra_body=json.loads(os.getenv("LLM_EXTRA_BODY", "{}")),
            data_dir=Path(os.getenv("AGENT_DATA_DIR", "data")),
            auth_tokens=json.loads(os.getenv("AGENT_AUTH_TOKENS", "{}")),
            llm_timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
            max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
            max_output_tokens=int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "2048")),
            context_tokens=int(os.getenv("AGENT_CONTEXT_TOKENS", "16384")),
            max_steps=int(os.getenv("AGENT_MAX_STEPS", "8")),
            run_timeout=float(os.getenv("AGENT_RUN_TIMEOUT_SECONDS", "120")),
            run_token_budget=int(os.getenv("AGENT_RUN_TOKEN_BUDGET", "80000")),
            tool_timeout=float(os.getenv("AGENT_TOOL_TIMEOUT_SECONDS", "5")),
        )
        result.validate()
        return result

