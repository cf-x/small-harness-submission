import asyncio
from contextlib import asynccontextmanager

import httpx

from agent.app import create_app
from agent.config import Settings
from conftest import ScriptedProvider, answer


@asynccontextmanager
async def client_for(tmp_path, model, tokens=None):
    cfg=Settings(data_dir=tmp_path,auth_tokens=tokens or {},max_retries=0,run_token_budget=300000)
    app=create_app(cfg,model)
    async with app.router.lifespan_context(app):
        transport=httpx.ASGITransport(app=app,client=('127.0.0.1',12345))
        async with httpx.AsyncClient(transport=transport,base_url='http://127.0.0.1',headers={'X-Harness-Client':'web'}) as client:
            yield client,app


async def test_http_create_submit_poll_history_and_trace(tmp_path):
    model=ScriptedProvider([answer('你好')])
    async with client_for(tmp_path,model) as (client,app):
        assert (await client.get('/')).status_code==200
        assert (await client.get('/static/app.js')).status_code==200
        created=await client.post('/api/sessions',json={})
        assert created.status_code==201
        sid=created.json()['id']
        sent=await client.post(f'/api/sessions/{sid}/messages',json={'text':'你好','request_id':'req1'})
        assert sent.status_code==202
        rid=sent.json()['id'];await app.state.runtime.wait(rid)
        assert (await client.get(f'/api/runs/{rid}')).json()['status']=='completed'
        history=(await client.get(f'/api/sessions/{sid}')).json()
        assert len(history['messages'])==2
        assert (await client.get(f'/api/runs/{rid}/trace')).json()
        assert (await client.post(f'/api/sessions/{sid}/messages',json={'text':'different','request_id':'req1'})).status_code==409


async def test_authentication_ownership_and_no_user_spoofing(tmp_path):
    alice='a'*32;bob='b'*32
    async with client_for(tmp_path,ScriptedProvider(),{alice:'alice',bob:'bob'}) as (client,app):
        assert (await client.get('/api/sessions')).status_code==401
        client.headers['Authorization']='Bearer '+alice
        sid=(await client.post('/api/sessions',json={})).json()['id']
        assert (await client.post('/api/sessions',json={'user_id':'bob'})).status_code==422
        client.headers['Authorization']='Bearer '+bob
        assert (await client.get(f'/api/sessions/{sid}')).status_code==404
        assert (await client.post(f'/api/sessions/{sid}/messages',json={'text':'read','request_id':'x'})).status_code==404
        assert (await client.get('/api/sessions')).json()==[]


async def test_memory_api_and_cross_user_rejection(tmp_path):
    a,b='a'*32,'b'*32
    async with client_for(tmp_path,ScriptedProvider(),{a:'alice',b:'bob'}) as (client,app):
        client.headers['Authorization']='Bearer '+a
        mid=(await client.post('/api/memories',json={'content':'偏好Python'})).json()['id']
        assert (await client.patch('/api/memories/'+mid,json={'content':'偏好Rust'})).status_code==200
        assert (await client.get('/api/memories')).json()[0]['content']=='偏好Rust'
        client.headers['Authorization']='Bearer '+b
        assert (await client.delete('/api/memories/'+mid)).status_code==404
        client.headers['Authorization']='Bearer '+a
        assert (await client.delete('/api/memories/'+mid)).status_code==200
        assert (await client.get('/api/memories')).json()==[]


async def test_browser_boundary_and_redacted_reasoning(tmp_path):
    turn=answer('可展示答案');turn.message['reasoning_content']='DO NOT DISPLAY THIS'
    async with client_for(tmp_path,ScriptedProvider([turn])) as (client,app):
        del client.headers['X-Harness-Client']
        assert (await client.post('/api/sessions',json={})).status_code==403
        client.headers['X-Harness-Client']='web'
        sid=(await client.post('/api/sessions',json={})).json()['id']
        rid=(await client.post(f'/api/sessions/{sid}/messages',json={'text':'hi','request_id':'x'})).json()['id']
        await app.state.runtime.wait(rid)
        data=await client.get('/api/sessions/'+sid)
        assert 'DO NOT DISPLAY THIS' not in data.text
        assert 'no-store' in data.headers['cache-control']
        assert (await client.get('/api/status',headers={'Host':'evil.example'})).status_code==400


async def test_api_cancel_and_queue(tmp_path):
    started=asyncio.Event()
    async def slow(messages,tools):
        started.set();await asyncio.Event().wait()
    async with client_for(tmp_path,ScriptedProvider([slow,answer('继续')])) as (client,app):
        sid=(await client.post('/api/sessions',json={})).json()['id']
        r1=(await client.post(f'/api/sessions/{sid}/messages',json={'text':'slow','request_id':'1'})).json()['id']
        await started.wait()
        r2=(await client.post(f'/api/sessions/{sid}/messages',json={'text':'next','request_id':'2'})).json()['id']
        assert (await client.post('/api/runs/'+r1+'/cancel')).json()['status']=='cancelled'
        await app.state.runtime.wait(r2)
        assert (await client.get('/api/runs/'+r2)).json()['answer']=='继续'

