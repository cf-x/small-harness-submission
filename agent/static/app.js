const $ = id => document.getElementById(id);
let sid = new URL(location.href).searchParams.get('session');
let selectedData = null, config = null, lastRender = '', polling = false;
const labels = {queued:'等待上一条消息完成', running:'正在处理', completed:'已完成', stopped:'已停止', cancelled:'已取消', interrupted:'已中断'};
function notice(text='') { $('notice').textContent=text; $('notice').hidden=!text; }
async function api(path, method='GET', body) {
  const token=sessionStorage.getItem('harness-token');
  const res=await fetch('/api'+path,{method,headers:{'Content-Type':'application/json','X-Harness-Client':'web',...(token?{'Authorization':'Bearer '+token}:{})},body:body===undefined?undefined:JSON.stringify(body)});
  const data=await res.json();
  if(!res.ok) { if(res.status===401&&!$('config-dialog').open)$('config-dialog').showModal(); throw new Error(typeof data.detail==='string'?data.detail:'请求失败，请检查输入'); }
  return data;
}
function node(tag, cls, text) {const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;}
async function sessions() {
  const list=await api('/sessions'); $('sessions').replaceChildren();
  for(const s of list){const b=node('button',s.id===sid?'selected':'',s.title);b.title=s.title;b.onclick=()=>select(s.id).catch(e=>notice(e.message));$('sessions').append(b);}
}
async function select(id) {sid=id;lastRender='';const u=new URL(location);u.searchParams.set('session',sid);history.replaceState(null,'',u);await refresh();await sessions();}
async function createSession() {const s=await api('/sessions','POST',{});await select(s.id);$('input').focus();return s.id;}
function render(data) {
  selectedData=data;$('session-title').textContent=data.session.title;
  const fingerprint=JSON.stringify(data);
  if(fingerprint===lastRender)return;lastRender=fingerprint;
  const viewport=$('conversation'),nearBottom=viewport.scrollHeight-viewport.scrollTop-viewport.clientHeight<120;
  $('messages').replaceChildren();$('welcome').hidden=data.messages.length>0;
  for(const row of data.messages){const m=row.message;
    if(m.tool_calls||m.role==='tool'){
      const d=node('details');d.append(node('summary',null,m.tool_calls?'工具调用 · '+m.tool_calls.map(t=>t.function.name).join(' / '):'工具执行结果'));
      d.append(node('pre',null,JSON.stringify(m.tool_calls||parseContent(m.content),null,2)));$('messages').append(d);
      if(m.tool_calls&&m.content){const p=node('div','message-body',m.content);d.prepend(p);}continue;
    }
    const block=node('article','message '+m.role);block.append(node('div','message-label',m.role==='user'?'YOU':'SMALL HARNESS'));block.append(node('div','message-body',m.content||''));$('messages').append(block);
  }
  const active=data.runs.filter(r=>['queued','running'].includes(r.status));
  $('run-status').replaceChildren();
  if(active.length){$('run-status').append(node('span',null,active.some(r=>r.status==='running')?'◌ 正在处理 · '+active.length+' 条执行中/排队':'◌ 消息排队中'));
    const actions=node('span');for(const r of active.slice().reverse()){const b=node('button',null,r.status==='running'?'停止当前执行':'取消排队');b.onclick=async()=>{try{await api('/runs/'+r.id+'/cancel','POST');await refresh();}catch(e){notice(e.message);}};actions.append(b);}$('run-status').append(actions);
  }else if(data.runs.length){const r=data.runs[0];$('run-status').append(node('span',null,`${labels[r.status]||r.status} · ${r.steps} 轮模型调用`));const b=node('button',null,'查看执行日志');b.onclick=async()=>{try{const trace=await api('/runs/'+r.id+'/trace');const d=node('details');d.open=true;d.append(node('summary',null,'执行日志'));d.append(node('pre',null,JSON.stringify(trace,null,2)));$('messages').append(d);viewport.scrollTop=viewport.scrollHeight;}catch(e){notice(e.message);}};$('run-status').append(b);}
  for(const r of active.filter(r=>r.status==='queued'))$('messages').append(node('div','message user','排队中：'+r.input));
  if(nearBottom)viewport.scrollTop=viewport.scrollHeight;
}
function parseContent(text){try{return JSON.parse(text);}catch{return text;}}
async function refresh(){if(!sid)return;const requested=sid;const data=await api('/sessions/'+sid);if(requested===sid)render(data);}
async function send(event){event?.preventDefault();if($('send').disabled)return;const text=$('input').value.trim();if(!text)return;$('send').disabled=true;
  try{const targetSid=sid||await createSession();await api('/sessions/'+targetSid+'/messages','POST',{text,request_id:crypto.randomUUID()});if($('input').value.trim()===text)$('input').value='';notice();await refresh();await sessions();$('conversation').scrollTop=$('conversation').scrollHeight;}catch(e){notice(e.message);}finally{$('send').disabled=false;$('input').focus();}}
$('composer').addEventListener('submit',send);
$('input').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();send();}});
$('new-session').onclick=()=>createSession().catch(e=>notice(e.message));
document.querySelectorAll('[data-prompt]').forEach(b=>b.onclick=()=>{$('input').value=b.dataset.prompt;$('input').focus();});
$('connection').onclick=()=>$('config-dialog').showModal();
$('auth-form').onsubmit=async e=>{e.preventDefault();sessionStorage.setItem('harness-token',$('access-token').value);$('access-token').value='';try{await sessions();$('config-dialog').close();notice();if(sid)await refresh();}catch(e){notice(e.message);}};
async function loadMemories(){const memories=await api('/memories'+(sid?'?session_id='+sid:''));$('memory-list').replaceChildren();if(!memories.length)$('memory-list').append(node('div','empty','还没有保存记忆。'));
  for(const m of memories){const n=node('div','memory-item');n.append(node('div',null,m.content));n.append(node('small',null,m.session_id?'当前会话 · ':'当前用户 · '));
    const edit=node('button',null,'编辑');edit.onclick=async()=>{const value=prompt('编辑这条记忆',m.content);if(value?.trim()){try{await api('/memories/'+m.id,'PATCH',{content:value});await loadMemories();}catch(e){notice(e.message);}}};n.append(edit);
    const del=node('button',null,'删除');del.onclick=async()=>{try{await api('/memories/'+m.id,'DELETE');await loadMemories();}catch(e){notice(e.message);}};n.append(del);$('memory-list').append(n);}}
$('memory-button').onclick=async()=>{try{await loadMemories();$('memory-dialog').showModal();}catch(e){notice(e.message);}};
$('memory-form').onsubmit=async e=>{e.preventDefault();try{if($('memory-scope').value==='session'&&!sid)await createSession();await api('/memories','POST',{content:$('memory-input').value,session_id:$('memory-scope').value==='session'?sid:null});$('memory-input').value='';await loadMemories();}catch(e){notice(e.message);}};
async function boot(){try{config=await api('/status');$('connection').textContent=config.configured?config.model:'○ 未连接模型';$('auth-form').hidden=!config.auth_required;$('auth-hint').textContent=config.auth_required?'请输入服务访问令牌，身份由服务端绑定。':'本机模式，无需访问令牌。';if(!config.configured)notice('模型尚未配置。填写项目 .env 并重启后，即可开始真实对话。');await sessions();if(sid)await refresh();}catch(e){notice(e.message);}}
setInterval(async()=>{if(polling||document.hidden||!sid)return;polling=true;try{await refresh();}catch(e){notice(e.message);}finally{polling=false;}},1200);
boot();
