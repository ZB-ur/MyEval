export const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const names = {sijian:'思间 Sijian',pome:'鸭鸭日记本 pome',queued:'待调度',running:'运行中',cancelling:'请求停止中',succeeded:'执行成功',failed:'失败',timed_out:'超时',cancelled:'已停止',interrupted:'执行中断',refused:'拒绝',unsupported:'不支持',completed:'运行完成',completed_with_issues:'完成 · 有异常',published:'已同步',unconfigured:'未配置',pass:'通过',passed:'通过',fail:'未通过',unknown:'未知',human:'人工评价',acceptance:'验收试填',fixed_context_rerun:'固定上下文重跑',isolated_full_run:'隔离环境完整执行',trace_rescore:'旧轨迹重评分',user:'儿童原话',child:'儿童原话',child_id:'虚构身份',child_turns:'本次儿童原话（逐轮）',activity_date:'活动日期',created_at:'建立时间',theme_id:'日记本主题',conversation_rounds:'最多轮数',duck:'鸭鸭',assistant:'AI 交付',transcript:'逐轮对话',summary:'整理结果',text:'正文',role:'角色',answer:'回应',citations:'引用',quote:'引文',answer_kind:'回应类型',relationships:'发现的联系',center_quote:'中心原文证据',candidate_quote:'候选原文证据',explanation:'联系说明',rationale:'理由',connection:'联系',materials:'选定材料',question:'问题',history:'历史对话',center:'中心想法',candidates:'候选材料',input:'输入',parameters:'请求参数',reason:'理由',dimension:'维度',status:'状态'};
export const label = value => names[value] || value;
export const badge = value => `<span class="badge ${esc(value)}">${esc(label(value) || '未知')}</span>`;
export const json = value => `<pre>${esc(JSON.stringify(value, null, 2))}</pre>`;
export const date = value => value ? new Date(value).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}) : '—';
export const duration = value => value === null || value === undefined ? '—' : `${(value/1000).toFixed(1)} s`;
export function content(value, depth=0) {
  if (value === null || value === undefined) return '<p class="muted">未记录 / 未知</p>';
  if (typeof value !== 'object') return `<p>${esc(typeof value === 'boolean' ? (value ? '是' : '否') : label(value))}</p>`;
  if (Array.isArray(value)) return value.length ? value.map((v,i) => `<div class="tree-item">${value.length>1 ? `<div class="tree-title">${i+1}</div>` : ''}${content(v,depth+1)}</div>`).join('') : '<p class="muted">无</p>';
  if (value.role && (value.text || value.content)) return `<div class="tree"><div class="tree-title">${esc(label(value.role))}</div><p>${esc(value.text || value.content)}</p></div>`;
  return Object.entries(value).map(([k,v]) => `<div class="${depth?'tree':'tree-item'}"><div class="tree-title">${esc(label(k))}</div>${content(v,depth+1)}</div>`).join('');
}
export async function api(url, body) {
  const response = await fetch(url, {headers:requestHeaders(url,body!==undefined),...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)})});
  const value = await response.json();
  if (!response.ok) {const error=new Error(value.error || `请求失败 (${response.status})`);error.code=value.code;error.status=response.status;throw error;}
  return value;
}
export function requestHeaders(url, jsonBody=false){
  const headers=jsonBody?{'Content-Type':'application/json'}:{};
  if(!url.startsWith('/api/blind/')&&url!=='/api/health'){
    const token=sessionStorage.getItem('myeval.admin');if(token)headers['X-MyEval-Admin']=token;
  }
  return headers;
}
export async function download(url,filename){
  const response=await fetch(url,{headers:requestHeaders(url)});
  if(!response.ok)throw new Error((await response.json()).error||'导出失败');
  const blob=await response.blob(),href=URL.createObjectURL(blob),a=document.createElement('a');
  a.href=href;a.download=filename;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(href),1000);
}
