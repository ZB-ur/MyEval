import {esc,label,badge,json,content,api,download} from '/view.js';
const token=location.pathname.split('/').at(-1),base='/api/blind/'+encodeURIComponent(token),app=document.getElementById('blind-app');
let data=null,index=0,loading=false;
const choiceName={A:'A 更好',B:'B 更好',tie:'平局',unable:'无法判断'};
function toast(message){const t=document.getElementById('toast');t.textContent=message;t.hidden=false;setTimeout(()=>t.hidden=true,4000);}
const opts='<option value="unable">无法判断</option><option value="A">A 更好</option><option value="B">B 更好</option><option value="tie">平局</option>';
async function load(){if(loading)return;loading=true;try{data=await api(base);render();}catch(error){app.innerHTML=`<div class="notice error">${esc(error.message)}</div>`;}finally{loading=false;}}
function render(){
  document.getElementById('reviewer-label').textContent=data.reviewer_label;
  const pair=data.pairs[index];
  if(!pair){app.innerHTML='<div class="empty">没有可评配对。</div>';return;}
  const rubric=data.rubric||[];
  app.innerHTML=`<div class="blind-top"><div><span class="eyebrow">${esc(label(data.project))} · ANONYMOUS COMPARISON</span><h1>对照原文，留下你的判断。</h1><p>配置身份已隐藏。相同案例的两侧顺序随机，刷新不会改变。</p></div><div><strong>${data.completed} / ${data.total}</strong><div class="muted">已保存评价</div></div></div><div class="pair-nav">${data.pairs.map((p,i)=>`<button class="small ${i===index?'current':''} ${p.review?'done':''}" data-index="${i}" aria-label="第 ${i+1} 个配对">${i+1}</button>`).join('')}</div><div class="section-head"><div><h2>${esc(pair.case.name)}</h2><p class="muted">配对 ${index+1} / ${data.total} · 重复 ${pair.repeat_index} · 虚构材料</p></div></div><div class="panel panel-pad"><details open><summary>本次输入与评价依据</summary><div class="content-box">${content(pair.case.input)}</div><details><summary>期望边界与材料来源</summary>${content(pair.case.expected_behavior)}${content(pair.case.source)}</details></details></div><div class="rubric">${rubric.map(r=>`<span title="${esc(r.description)}">${esc(r.name)}</span>`).join('')}</div>`;
  if(!pair.ready){app.innerHTML+='<div class="empty">该配对仍在运行。两侧进入终态后会显示全部结果，包括失败、超时和取消。</div>';return;}
  app.innerHTML+=`<div class="two-columns">${['A','B'].map(side=>`<article class="pair-card"><div class="pair-card-head"><span class="pair-letter">${side}</span><strong>结果 ${side}</strong>${badge(pair[side].status)}</div><div class="content-box">${content(pair[side].output)}</div></article>`).join('')}</div>`;
  if(pair.review){
    const r=pair.review;
    app.innerHTML+=`<div class="review-saved"><strong>✓ 已保存 · ${esc(choiceName[r.choice])}</strong><span class="tag">${esc(label(r.kind))}</span><p>${esc(r.reason)}</p><p class="source-note">这份评价与当前配对绑定，刷新或重启后保留。</p><div class="action-row"><button class="small" data-action="reveal">揭示此配对配置</button>${index<data.pairs.length-1?'<button class="primary small" data-action="next">下一个配对 →</button>':''}</div><div id="reveal-content"></div></div>`;
  }else{
    app.innerHTML+=`<form class="review-form" id="review-form"><h2>哪一侧更符合这次任务？</h2><p class="muted">结合原话和边界判断，无法判断也是有效结果。</p><div class="choice-grid">${Object.entries(choiceName).map(([value,text])=>`<label class="choice"><input type="radio" name="choice" value="${value}" required>${text}</label>`).join('')}</div><details><summary>分维度评价（默认无法判断）</summary><div class="dim-grid">${rubric.map(r=>`<label title="${esc(r.description)}">${esc(r.name)}<select name="dimension:${esc(r.id)}">${opts}</select></label>`).join('')}</div></details><label>判断理由<textarea name="reason" rows="4" maxlength="10000" required placeholder="引用具体内容说明差别；如果信息不足，写明缺少什么。"></textarea></label><div class="form-grid"><label>评价用途<select name="kind"><option value="human">真实人工判断</option><option value="acceptance">验收试填（不计入人工偏好）</option></select></label><div class="source-note">如果已看过管理员页或只在验证功能，请选“验收试填”。</div></div><p class="error-text" id="review-error" role="alert"></p><div class="dialog-footer"><span class="muted">提交后可主动揭示配置</span><button class="primary" type="submit">保存评价</button></div></form>`;
  }
  app.innerHTML+='<p class="source-note">本地盲评减少配置身份带来的偏见；不对持有源码或本机管理员入口的人提供强隔离。</p>';
}
app.addEventListener('submit',async event=>{
  if(event.target.id!=='review-form')return;
  event.preventDefault();const form=event.target,values=new FormData(form),button=form.querySelector('[type=submit]');button.disabled=true;
  const dimensions=Object.fromEntries((data.rubric||[]).map(r=>[r.id,values.get('dimension:'+r.id)]));
  const pair=data.pairs[index];
  try{await api(`${base}/${pair.id}/review`,{revision:pair.revision,choice:values.get('choice'),reason:values.get('reason'),kind:values.get('kind'),dimensions});await load();toast('评价已保存');}
  catch(error){const notice=document.getElementById('review-error');notice.textContent=error.message;if(error.code==='stale_presentation'){const reload=document.createElement('button');reload.type='button';reload.dataset.action='reload';reload.textContent='重新载入配对';notice.after(reload);}else{button.disabled=false;}}
});
app.addEventListener('click',async event=>{
  const nav=event.target.closest('[data-index]');if(nav){index=Number(nav.dataset.index);render();window.scrollTo(0,0);return;}
  const button=event.target.closest('[data-action]');if(!button)return;
  if(button.dataset.action==='next'){index++;render();window.scrollTo(0,0);}
  if(button.dataset.action==='reload'){await load();toast('已重新载入，请核对当前结果后重新评价');}
  if(button.dataset.action==='reveal'){
    try{const result=await api(`${base}/${data.pairs[index].id}/reveal`,{});document.getElementById('reveal-content').innerHTML=`<div class="reveal">${Object.entries(result).map(([side,r])=>`<p><strong>${side}</strong>：${esc(r.configuration.name)} · ${esc(label(r.configuration.mode))}</p>`).join('')}</div>`;button.disabled=true;}
    catch(error){toast(error.message);}
  }
});
document.getElementById('anonymous-export').onclick=async()=>{try{await download(base+'/export','anonymous-review.json');toast('匿名导出不含配置映射');}catch(error){toast(error.message);}};
await load();
setInterval(()=>{if(!document.hidden&&data&&!data.pairs[index]?.ready)load();},2500);
