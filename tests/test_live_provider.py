"""Opt in with RUN_LIVE_TESTS=1. Makes paid requests to the configured provider."""
import json
import os
import uuid

import pytest

from agent.config import Settings
from agent.provider import ChatProvider
from agent.runtime import Runtime
from agent.session_store import Store
from agent.tools import build_registry


@pytest.mark.live
async def test_real_llm_tools_and_followup(tmp_path):
    if os.getenv('RUN_LIVE_TESTS')!='1':
        pytest.skip('Set RUN_LIVE_TESTS=1 after configuring .env; this test uses paid API calls')
    cfg=Settings.from_env()
    assert cfg.configured, 'LLM_BASE_URL / LLM_MODEL / LLM_API_KEY required'
    cfg.data_dir=tmp_path
    cfg.run_token_budget=max(cfg.run_token_budget,150000)
    provider=ChatProvider(cfg);store=Store(tmp_path/'live.sqlite3')
    runtime=Runtime(store,provider,build_registry(store),cfg)
    try:
        sid=store.create_session('live-test')['id']
        run=runtime.submit('live-test',sid,'请务必使用 calculator 计算 17*23，给出结果。',uuid.uuid4().hex)
        result=await runtime.wait(run['id'])
        assert result['status']=='completed',result['error_code']
        assert '391' in result['answer']
        assert any(e['kind']=='tool_completed' and e['data']['name']=='calculator' for e in store.traces(run['id']))
        run=runtime.submit('live-test',sid,'把刚才结果加9，用 calculator 验证。',uuid.uuid4().hex)
        result=await runtime.wait(run['id'])
        assert result['status']=='completed',result['error_code']
        assert '400' in result['answer']
    finally:
        await runtime.close();await provider.close();store.close()


@pytest.mark.live
@pytest.mark.parametrize('scenario', ['docs_search', 'sessions_memory', 'compaction_resume'])
async def test_real_llm_assignment_scenarios(tmp_path, scenario):
    if os.getenv('RUN_LIVE_TESTS') != '1':
        pytest.skip('Set RUN_LIVE_TESTS=1 to run paid API tests')
    cfg = Settings.from_env()
    assert cfg.configured
    cfg.data_dir = tmp_path
    cfg.run_token_budget = max(cfg.run_token_budget, 150000)
    provider = ChatProvider(cfg)
    path = tmp_path / 'scenario.sqlite3'
    store = Store(path)
    runtime = Runtime(store, provider, build_registry(store), cfg)

    async def send(sid, text):
        run = runtime.submit('live-test', sid, text, uuid.uuid4().hex)
        result = await runtime.wait(run['id'])
        assert result['status'] == 'completed', result['error_code']
        return result

    def used(result, tool):
        return any(t['kind'] == 'tool_completed' and t['data']['name'] == tool
                   and t['data']['ok'] for t in store.traces(result['id']))

    try:
        sid = store.create_session('live-test')['id']
        if scenario == 'docs_search':
            result = await send(sid, '请使用 read_docs 读取 handbook，列出演示代号、预算和三个验收项。')
            assert used(result, 'read_docs')
            assert 'Cedar' in result['answer'] and '800' in result['answer']
            followup = await send(sid, '把你刚才列出的第二个验收项展开解释。')
            assert '会话' in followup['answer'] and '隔离' in followup['answer']
            result = await send(sid, '请使用 search 搜索 Context，说明这些资料是否是真实联网搜索。')
            assert used(result, 'search')
            assert any(word in result['answer'].lower() for word in ('模拟', 'mock', '离线'))
        elif scenario == 'sessions_memory':
            other = store.create_session('live-test')['id']
            await send(sid, '请记住本会话的临时代号是 ALPHA731，回复收到。')
            await send(other, '请记住本会话的临时代号是 BETA942，回复收到。')
            a = await send(sid, '本会话的临时代号是什么？')
            b = await send(other, '本会话的临时代号是什么？')
            assert 'ALPHA731' in a['answer'] and 'BETA942' not in a['answer']
            assert 'BETA942' in b['answer'] and 'ALPHA731' not in b['answer']
            mid = store.add_memory('live-test', None, '项目 Orion 的预算上限为 650 元，用户已明确确认。')
            result = await send(sid, '根据我的记忆，项目 Orion 预算上限是多少？')
            assert '650' in result['answer']
            store.update_memory('live-test', mid, '项目 Orion 的预算上限已改为 720 元，旧的 650 元失效。')
            result = await send(other, '根据最新保存的记忆，项目 Orion 预算上限是多少？仅回答当前金额。')
            assert '720' in result['answer'] and '650' not in result['answer']
        else:
            # Seed a deterministic long transcript; only the recovery question uses the real LLM.
            for i in range(200):
                text = ('例行记录，无需作为档案代号。' * 120 + '最早确认的档案代号为 ARCHIVE731。') if i == 0 else f'第{i}轮记录了无关的例行检查。' * 8
                old, _ = store.enqueue('live-test', sid, text, f'seed-{i}')
                store.begin(old['id'])
                store.append_model(old['id'], {'role':'assistant', 'content':'记录收到。'})
                store.finish(old['id'], 'completed', '记录收到。', append=False)
            result = await send(sid, '请使用 read_history 按 seq=1 查阅最早的用户消息；如果内容截断，请继续分页。然后告诉我当时确认的档案代号。')
            assert used(result, 'read_history') and 'ARCHIVE731' in result['answer']
            calls = [call for row in store.messages(sid) if row['run_id'] == result['id']
                     for call in row['message'].get('tool_calls', [])]
            assert any(call['function']['name'] == 'read_history'
                       and json.loads(call['function']['arguments']).get('offset', 0) > 0 for call in calls)
            assert store.summary(sid) is not None
            await runtime.close()
            store.close()
            store = Store(path)
            runtime = Runtime(store, provider, build_registry(store), cfg)
            result = await send(sid, '我们刚才从历史里查出的档案代号是什么？')
            assert 'ARCHIVE731' in result['answer']
    finally:
        await runtime.close()
        await provider.close()
        store.close()
