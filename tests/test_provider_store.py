import json

import httpx
import pytest

from agent.config import Settings
from agent.provider import ChatProvider, ProviderError, normalize
from agent.session_store import Store
from conftest import call, assert_protocol


@pytest.mark.parametrize("data", [{}, {"choices":[]}, {"choices":[{"message":{"role":"user"},"finish_reason":"stop"}]}, {"choices":[{"message":{"role":"assistant","tool_calls":[call(),call()]},"finish_reason":"tool_calls"}]}])
def test_protocol_validation(data):
    with pytest.raises(ProviderError):
        normalize(data)


def test_reasoning_preserved_for_provider_continuation():
    value=normalize({"choices":[{"message":{"role":"assistant","content":None,"reasoning_content":"opaque provider reasoning", "tool_calls":[call()]},"finish_reason":"tool_calls"}]})
    assert value.message["reasoning_content"] == "opaque provider reasoning"


async def test_http_adapter_sends_schema_and_retries(tmp_path):
    requests=[]
    def handle(request):
        requests.append(request)
        if len(requests)==1:return httpx.Response(429,json={"error":"rate limit"})
        return httpx.Response(200,json={"id":"p1","choices":[{"message":{"role":"assistant","content":"hello"},"finish_reason":"stop"}],"usage":{"total_tokens":42}})
    cfg=Settings(base_url="https://provider.example/v1",model="test-model",api_key="secret",max_retries=1)
    p=ChatProvider(cfg,httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    result=await p.generate([{"role":"user","content":"hi"}],[{"type":"function"}])
    await p.close()
    assert result.attempts==2 and result.usage["total_tokens"]==42
    assert str(requests[-1].url)=="https://provider.example/v1/chat/completions"
    body=json.loads(requests[-1].content)
    assert body["tools"]==[{"type":"function"}] and body["stream"] is False
    assert requests[-1].headers["authorization"]=="Bearer secret"


@pytest.mark.parametrize("status,body",[(401,"secret key leaked"),(302,"redirect"),(200,"invalid json")])
async def test_http_error_redacts_body(status,body):
    client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(status,text=body)))
    provider=ChatProvider(Settings(base_url="https://provider.example",model="test",api_key="secret"),client)
    with pytest.raises(ProviderError) as e:
        await provider.generate([],[])
    assert body not in str(e.value)
    await provider.close()


async def test_unconfigured_model_fails_honestly():
    provider=ChatProvider(Settings())
    with pytest.raises(ProviderError) as e:
        await provider.generate([],[])
    assert e.value.code=="not_configured"
    await provider.close()


def test_restart_recovery_closes_pending_pair(tmp_path):
    path=tmp_path/'db.sqlite3'
    store=Store(path);sid=store.create_session('alice')['id']
    run,_=store.enqueue('alice',sid,'计算','r');store.begin(run['id'])
    store.append_model(run['id'],{'role':'assistant','content':None,'tool_calls':[call()]})
    store.close()
    recovered=Store(path)
    try:
        assert recovered.run(run['id'])['status']=='interrupted'
        assert_protocol([r['message'] for r in recovered.messages(sid)])
        with recovered.db() as db:
            assert db.execute('SELECT status FROM tool_calls').fetchone()[0]=='unknown'
    finally:recovered.close()


def test_single_process_enforced(tmp_path):
    path=tmp_path/'db.sqlite3';first=Store(path)
    try:
        with pytest.raises(RuntimeError):Store(path)
    finally:first.close()


def test_config_rejects_unsafe_remote_http_and_overrides():
    with pytest.raises(ValueError):Settings(base_url='http://remote.example').validate()
    with pytest.raises(ValueError):Settings(extra_body={'messages':[]}).validate()
    with pytest.raises(ValueError):Settings(auth_tokens={'short':'alice'}).validate()
