import {esc,label,badge,outputContent,evidenceContent,api,download,icon,sourceLabel,licenseText,attributionText,makeEvidenceImageZoomable,closeEvidenceImageViewer} from '/view.js';
const token=location.pathname.split('/').at(-1),base='/api/blind/'+encodeURIComponent(token),app=document.getElementById('blind-app');
let data=null,index=0,loading=false,loadGeneration=0,loadController=null,saving=false,toastTimer=null,dirty=false,pickerOpen=false,pairQuery='',pairFilter='all';
const drafts=new Map(),mediaUrls=new Set(),disabledBeforeSave=new WeakMap();
const choices={A:'A 更好',B:'B 更好',tie:'平局',unable:'无法判断'};
const opts='<option value="unable">无法判断</option><option value="A">A 更好</option><option value="B">B 更好</option><option value="tie">平局</option>';
function toast(message){const t=document.getElementById('toast');clearTimeout(toastTimer);t.textContent=message;t.hidden=false;toastTimer=setTimeout(()=>t.hidden=true,4500);}
function sourceLink(value){try{const url=new URL(value);return ['http:','https:'].includes(url.protocol)?url.href:null;}catch{return null;}}
function inputWithoutPaths(input){if(!input||typeof input!=='object'||Array.isArray(input))return input;return Object.fromEntries(Object.entries(input).filter(([key])=>!['media','image_path','image_sha256'].includes(key)));}
function anonymousMedia(pair){return (pair.media||[]).map(m=>{const source=m.source||{},link=sourceLink(source.url),licenseUrl=sourceLink(typeof source.license==='object'?source.license?.url:source.license_url),safe=typeof m.url==='string'&&m.url.startsWith(base+'/pairs/');return `<div class="media-evidence"><figure>${safe?`<div data-media="${esc(m.url)}" data-alt="${esc(pair.case?.input?.intent||pair.case?.name||'评测原图')}"><div class="empty compact"><p>正在读取本次评测原图…</p></div></div>`:'<div class="empty compact"><p>此图像没有可用的匿名证据入口</p></div>'}<figcaption>${esc(sourceLabel(source))}${licenseText(source)?' · '+esc(licenseText(source)):''}${attributionText(source)?' · '+esc(attributionText(source)):''}${licenseUrl?` · <a href="${esc(licenseUrl)}" target="_blank" rel="noopener noreferrer">许可条款</a>`:''}${link?` · <a href="${esc(link)}" target="_blank" rel="noopener noreferrer">来源与许可 ${icon('external')}</a>`:''}${m.sha256?`<details class="media-identity"><summary>原图 SHA-256</summary><span class="mono">${esc(m.sha256)}</span></details>`:''}</figcaption></figure></div>`;}).join('');}
async function loadMedia(){for(const el of app.querySelectorAll('[data-media]')){const url=el.dataset.media;delete el.dataset.media;try{const response=await fetch(url);if(!response.ok||!(response.headers.get('content-type')||'').startsWith('image/'))throw new Error('原图暂不可用');const blob=await response.blob();if(!el.isConnected)continue;const src=URL.createObjectURL(blob);mediaUrls.add(src);const img=document.createElement('img');img.src=src;img.alt=el.dataset.alt||'评测原图';el.replaceChildren(img);makeEvidenceImageZoomable(img);}catch{if(el.isConnected)el.innerHTML='<div class="notice warning">原图读取失败。视觉相关判断请选择“无法判断”，或重新载入后再评阅。</div>';}}}
function hasDraft(pair){return drafts.get(pair.id)?.revision===pair.revision;}
function pairState(pair){return pair.review?'已保存':!pair.ready?'待输出':hasDraft(pair)?'有未保存草稿':'未评';}
function updateDraftNotice(){const pair=data?.pairs[index];if(!pair)return;const status=document.getElementById('current-pair-state');if(status)status.textContent=pairState(pair);const notice=document.getElementById('review-draft-note');if(notice)notice.textContent=hasDraft(pair)?'有未保存的草稿':'';}
function captureDraft(){const form=document.getElementById('review-form'),pair=data?.pairs[index];if(!form||!pair)return;const values=new FormData(form),entries=[...values.entries()],hasContent=entries.some(([key,value])=>key==='choice'||key==='reason'&&String(value).trim()||key==='kind'&&value!=='human'||key.startsWith('dimension:')&&value!=='unable');if(hasContent)drafts.set(pair.id,{revision:pair.revision,entries});else drafts.delete(pair.id);dirty=drafts.size>0;updateDraftNotice();}
function restoreDraft(pair){const draft=drafts.get(pair.id),form=document.getElementById('review-form');if(!draft||draft.revision!==pair.revision||!form)return;for(const [key,value] of draft.entries){const elements=[...form.elements].filter(el=>el.name===key);for(const el of elements){if(el.type==='radio')el.checked=el.value===value;else el.value=value;}}}
function invalidateLoad(){loadGeneration++;loadController?.abort();loadController=null;loading=false;}
async function load({preserve=true,force=false}={}){
  if((loading||saving)&&!force)return false;if(force)invalidateLoad();const generation=++loadGeneration,controller=new AbortController();loadController=controller;loading=true;if(preserve)captureDraft();
  try{const nextData=await api(base,undefined,{signal:controller.signal});if(generation!==loadGeneration)return false;if(preserve)captureDraft();const currentId=data?.pairs[index]?.id || history.state?.reviewPair;
    data=nextData;
    const restored=data.pairs.findIndex(pair=>pair.id===currentId);
    index=restored>=0?restored:Math.min(index,Math.max(0,data.pairs.length-1));render();return true;}
  catch(error){if(generation!==loadGeneration||controller.signal.aborted)return false;app.innerHTML=`<div class="empty"><div class="empty-mark">${icon('lock')}</div><h2>暂时无法读取评审入口</h2><p>${esc(error.message)}</p><button data-action="reload" type="button">重试读取 ${icon('refresh')}</button></div>`;return false;}
  finally{if(generation===loadGeneration){loading=false;loadController=null;}}
}
function pairPicker(){return `<section class="pair-picker" id="pair-picker" aria-labelledby="pair-picker-heading" ${pickerOpen?'':'hidden'}><div class="pair-picker-head"><div><h2 id="pair-picker-heading">配对目录</h2></div><button type="button" class="quiet" data-action="close-picker" aria-label="收起配对目录">${icon('close')}</button></div><div class="pair-search-fields"><div><label for="pair-search">搜索序号或案例</label><input id="pair-search" type="search" value="${esc(pairQuery)}" placeholder="例如：12、公开图片" autocomplete="off" aria-controls="pair-results"></div><div><label for="pair-filter">评审状态</label><select id="pair-filter" aria-controls="pair-results">${[['all','全部配对'],['unreviewed','未评'],['saved','已保存'],['waiting','待输出']].map(([value,text])=>`<option value="${value}" ${pairFilter===value?'selected':''}>${text}</option>`).join('')}</select></div></div><p id="pair-results-count" class="pair-results-count" role="status" aria-live="polite"></p><nav id="pair-results" class="pair-results" aria-label="配对搜索结果"></nav></section>`;}
function renderPairResults(){const results=document.getElementById('pair-results');if(!results)return;const query=pairQuery.trim().toLocaleLowerCase();const matches=data.pairs.map((pair,originalIndex)=>({pair,originalIndex})).filter(({pair,originalIndex})=>{const stateMatch=pairFilter==='all'||pairFilter==='unreviewed'&&!pair.review||pairFilter==='saved'&&Boolean(pair.review)||pairFilter==='waiting'&&!pair.ready;const terms=[String(originalIndex+1),pair.case?.name,pair.case?.id,pair.case?.family_id].filter(Boolean).join(' ').toLocaleLowerCase();return stateMatch&&(!query||terms.includes(query));});
  document.getElementById('pair-results-count').textContent=`显示 ${matches.length} / ${data.pairs.length} 个配对`;
  results.innerHTML=matches.length?matches.map(({pair:p,originalIndex:i})=>`<button type="button" class="pair-result ${i===index?'current':''}" data-index="${i}" ${i===index?'aria-current="step"':''} ${saving?'disabled':''} aria-label="第 ${i+1} 个配对，${esc(p.case?.name||'未命名案例')}，${esc(pairState(p))}"><span class="pair-result-number">${i+1}</span><span class="pair-result-main"><strong>${esc(p.case?.name||'未命名案例')}</strong><small>重复 ${esc(p.repeat_index)} · ${esc(pairState(p))}</small></span><span class="pair-result-indicator" aria-hidden="true">${p.review?icon('check'):icon('arrow')}</span></button>`).join(''):`<div class="pair-search-empty"><p>没有匹配的配对。</p><button type="button" data-action="clear-pair-search">清除搜索与筛选</button></div>`;
}
function setPicker(open,{restore=false}={}){pickerOpen=open;const picker=document.getElementById('pair-picker'),toggle=document.getElementById('pair-picker-toggle');if(!picker||!toggle)return;picker.hidden=!open;toggle.setAttribute('aria-expanded',String(open));if(open){renderPairResults();document.getElementById('pair-search').focus({preventScroll:true});picker.scrollIntoView({block:'nearest',behavior:'instant'});}else if(restore)toggle.focus({preventScroll:true});}
function navigatePair(next){if(saving||!Number.isInteger(next)||next<0||next>=data.pairs.length)return;captureDraft();index=next;history.replaceState({reviewPair:data.pairs[index].id},'',location.pathname+location.search);pickerOpen=false;render();window.scrollTo({top:0,behavior:'instant'});document.getElementById('blind-heading')?.focus({preventScroll:true});}
function nextReadyIndex() {
  for (let offset = 1; offset <= data.pairs.length; offset++) {
    const position = (index + offset) % data.pairs.length;
    const pair = data.pairs[position];
    if (pair.ready && !pair.review) return position;
  }
  return -1;
}

function reviewNavigation(pair) {
  if (!pair.ready) return '';
  return `<nav class="review-waypoints" aria-label="本次配对内容">
    <a href="#blind-evidence">${icon('dataset')}原始材料</a>
    <a href="#blind-results" class="wide-waypoint">${icon('compare')}两侧输出</a>
    <a href="#blind-result-A" class="narrow-waypoint">结果 A</a>
    <a href="#blind-result-B" class="narrow-waypoint">结果 B</a>
    <a href="#review-outcome">${icon('eye')}${pair.review?'已存评价':'填写评价'}</a>
  </nav>`;
}

let navigationFrame = 0;
function updateReadingPosition() {
  navigationFrame = 0;
  const nav = app.querySelector('.review-waypoints');
  if (!nav) return;
  const links = [...nav.querySelectorAll('a')].filter(link => link.getClientRects().length);
  const targetLine = nav.getBoundingClientRect().bottom + 36;
  let current = links[0];
  for (const link of links) {
    const section = document.getElementById(link.hash.slice(1));
    if (section?.getBoundingClientRect().top <= targetLine) current = link;
  }
  if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2) current = links.at(-1);
  for (const link of links) {
    if (link === current) link.setAttribute('aria-current', 'location');
    else link.removeAttribute('aria-current');
  }
}

function scheduleReadingPosition() {
  if (!navigationFrame) navigationFrame = requestAnimationFrame(updateReadingPosition);
}
window.addEventListener('scroll', scheduleReadingPosition, {passive:true});
window.addEventListener('resize', scheduleReadingPosition);

function reviewReceipt(pair, rubric) {
  const review = pair.review;
  const byId = new Map(rubric.map(dimension => [dimension.id, dimension.name]));
  const dimensions = Object.entries(review.dimensions || {});
  const next = nextReadyIndex();
  return `<section class="review-saved" id="review-outcome" tabindex="-1" aria-labelledby="review-saved-title">
    <div class="receipt-heading"><span class="receipt-mark">${icon('check')}</span>
      <div><span class="receipt-status">${esc(label(review.kind))} · 已保存</span><h2 id="review-saved-title">${esc(choices[review.choice] || review.choice)}</h2></div>
    </div>
    <p class="receipt-reason">${esc(review.reason)}</p>
    ${dimensions.length?`<details><summary>分维度判断 · ${dimensions.length} 项</summary><dl class="receipt-dimensions">${dimensions.map(([key,value])=>`<div><dt>${esc(byId.get(key)||key)}</dt><dd>${esc(choices[value]||value)}</dd></div>`).join('')}</dl></details>`:''}
    <div class="receipt-actions"><button type="button" data-action="reveal">揭示此配对方案</button>
      ${next>=0?`<button type="button" class="primary" data-action="next-unreviewed">下一项可评配对 ${icon('arrow')}</button>`:`<span>${data.pairs.every(item=>item.review)?'本席位已完成全部配对':'其余配对待输出'}</span>`}
    </div><div id="reveal-content"></div></section>`;
}

function render(){
  closeEvidenceImageViewer();
  for(const url of mediaUrls)URL.revokeObjectURL(url);mediaUrls.clear();
  document.getElementById('reviewer-label').textContent=data.reviewer_label;
  const pair=data.pairs[index];if(!pair){app.innerHTML=`<div class="panel"><div class="empty"><div class="empty-mark">${icon('compare')}</div><h2>还没有可评配对</h2></div></div>`;return;}
  const rubric=Array.isArray(data.rubric)?data.rubric:[],basis=Object.fromEntries(['family_id','variant','split','expected_behavior','allowed_facts','forbidden_inferences','turn_focus','expected_background_texts','allowed_background_texts','expected_history_ids','summary_forbidden_fragments','source'].filter(key=>pair.case[key]!==undefined).map(key=>[key,pair.case[key]]));
  const total=data.total??data.pairs.length,completed=data.completed??0;
  app.innerHTML=`<section class="blind-intro"><div class="blind-context"><span class="eyebrow">${esc(label(data.project))} · 匿名评审</span><details class="blind-protocol"><summary aria-label="评审规则" title="评审规则">${icon('info')}</summary><div><p>方案身份已隐藏。相同案例的两侧顺序固定，不随刷新变化。</p><p>${esc(data.case_count??'未知')} 个案例 · ${esc(data.family_count??'未知')} 个情境家族；变式与重复不增加家族数。</p><p>依据原始材料和任务边界判断；平局与无法判断都是有效结果。保存后可主动揭示方案身份。已看过管理员结果或正在验收时，请选“验收试填”。草稿仅暂存在本页。</p></div></details></div><h1 id="blind-heading" tabindex="-1" aria-describedby="blind-task-meta">${esc(pair.case.name)}</h1><p id="blind-task-meta">配对 ${index+1} / ${esc(total)} · 重复 ${esc(pair.repeat_index)} · ${esc(sourceLabel(pair.case))}</p></section><section class="review-toolbar" aria-label="评审进度与配对导航"><div class="review-progress-row"><div class="review-progress"><span><strong>${esc(completed)}</strong> / ${esc(total)} 已保存</span><progress value="${esc(completed)}" max="${esc(Math.max(1,Number(total)||1))}" aria-label="已保存评价进度"></progress><span id="current-pair-state" class="current-pair-state">${pairState(pair)}</span></div><button class="quiet next-unreviewed" data-action="next-unreviewed" type="button" ${saving||nextReadyIndex()<0||nextReadyIndex()===index?'disabled':''}>下一项可评 ${icon('arrow')}</button></div><nav class="pair-stepper" aria-label="切换匿名配对"><button type="button" data-action="previous" ${index===0||saving?'disabled':''} aria-label="上一个配对">${icon('back')}<span>上一个</span></button><button id="pair-picker-toggle" type="button" data-action="toggle-picker" aria-expanded="${pickerOpen}" aria-controls="pair-picker" ${saving?'disabled':''}><span class="numeric">${index+1} / ${esc(total)}</span><span>配对目录</span>${icon('chevron')}</button><button type="button" data-action="next" ${index===data.pairs.length-1||saving?'disabled':''} aria-label="下一个配对"><span>下一个</span>${icon('arrow')}</button></nav></section>${pairPicker()}${reviewNavigation(pair)}<section id="blind-evidence" tabindex="-1" class="blind-evidence panel" aria-labelledby="blind-evidence-heading"><div class="blind-evidence-heading"><h2 id="blind-evidence-heading">输入与证据</h2></div><div class="blind-evidence-body">${anonymousMedia(pair)}<div class="blind-input">${evidenceContent(inputWithoutPaths(pair.case.input))}</div><details class="evidence-context"><summary>期望边界与材料来源</summary>${evidenceContent(basis)}</details><details class="evidence-context"><summary>初始资料与历史</summary>${evidenceContent(pair.case.initial_state)}</details></div></section>`;
  if(!pair.ready){app.innerHTML+=`<section id="pair-readiness" class="panel"><div class="empty"><div class="empty-mark">${icon('clock')}</div><h2>这个配对仍在运行</h2></div></section>`;renderPairResults();loadMedia();return;}
  app.innerHTML+=`<section id="blind-results" tabindex="-1" class="blind-results" aria-labelledby="blind-results-heading"><div class="blind-results-heading"><h2 id="blind-results-heading">输出对照</h2></div><div class="rubric" aria-label="评价维度">${rubric.map(r=>`<span title="${esc(r.description)}">${esc(r.name)}</span>`).join('')}</div><div class="two-columns">${['A','B'].map(side=>`<article id="blind-result-${side}" tabindex="-1" class="pair-card" aria-labelledby="result-${side}-heading"><div class="pair-card-head"><span class="pair-letter" aria-hidden="true">${side}</span><h3 id="result-${side}-heading">结果 ${side}</h3>${badge(pair[side].status)}</div><div class="content-box">${outputContent(pair[side].output)}</div></article>`).join('')}</div></section>`;
  if(pair.review)app.innerHTML+=reviewReceipt(pair,rubric);
  else app.innerHTML+=`<section id="review-outcome" tabindex="-1"><form class="review-form" id="review-form" aria-labelledby="review-form-title"><h2 id="review-form-title">配对评价</h2><p class="review-draft-note" id="review-draft-note"></p><fieldset><legend>整体判断</legend><div class="choice-grid">${Object.entries(choices).map(([value,text])=>`<label class="choice" for="review-choice-${value}"><input id="review-choice-${value}" type="radio" name="choice" value="${value}" required>${text}</label>`).join('')}</div></fieldset><details class="dimension-review"><summary>分维度评价（默认无法判断）</summary><div class="dim-grid">${rubric.map((r,i)=>`<div class="dimension-choice"><label for="review-dimension-${i}">${esc(r.name)}</label><select id="review-dimension-${i}" name="dimension:${esc(r.id)}" aria-describedby="review-dimension-help-${i}">${opts}</select><span class="sr-only" id="review-dimension-help-${i}">${esc(r.description||'依据本次材料判断，信息不足可保持无法判断。')}</span></div>`).join('')}</div></details><label for="review-reason">判断理由</label><textarea id="review-reason" name="reason" rows="4" maxlength="10000" required placeholder="引用原文或填写证据位置"></textarea><div class="review-purpose"><label for="review-kind">评价用途</label><select id="review-kind" name="kind"><option value="human">真实人工判断</option><option value="acceptance">验收试填（不计入人工偏好）</option></select></div><p class="form-error" id="review-error" role="alert"></p><div class="dialog-footer"><button class="primary" type="submit">保存评价 ${icon('check')}</button></div></form></section>`;
  restoreDraft(pair);updateDraftNotice();renderPairResults();loadMedia();scheduleReadingPosition();
}
app.addEventListener('input',event=>{if(event.target.id==='pair-search'){pairQuery=event.target.value;renderPairResults();}else if(event.target.closest('#review-form'))captureDraft();});
app.addEventListener('change',event=>{if(event.target.id==='pair-filter'){pairFilter=event.target.value;renderPairResults();}else if(event.target.closest('#review-form'))captureDraft();});
app.addEventListener('keydown',event=>{if(event.key==='Escape'&&pickerOpen&&event.target.closest('#pair-picker')){event.preventDefault();setPicker(false,{restore:true});}});
function setSavingBusy(busy){saving=busy;for(const control of app.querySelectorAll('[data-index],[data-action="previous"],[data-action="next"],[data-action="next-unreviewed"],#pair-picker-toggle')){control.disabled=busy||(control.dataset.action==='next-unreviewed'&&(nextReadyIndex()<0||nextReadyIndex()===index))||(control.dataset.action==='previous'&&index===0)||(control.dataset.action==='next'&&index===data.pairs.length-1);}const form=document.getElementById('review-form');if(form){form.setAttribute('aria-busy',String(busy));for(const control of form.elements){if(busy){if(!disabledBeforeSave.has(control))disabledBeforeSave.set(control,control.disabled);control.disabled=true;}else if(disabledBeforeSave.has(control)){control.disabled=disabledBeforeSave.get(control);disabledBeforeSave.delete(control);}}const button=form.querySelector('[type=submit]');if(button)button.innerHTML=busy?'正在保存…':`保存评价 ${icon('check')}`;}}
app.addEventListener('submit',async event=>{
  if(event.target.id!=='review-form')return;event.preventDefault();if(saving)return;captureDraft();const form=event.target,values=new FormData(form),pair=data.pairs[index],notice=form.querySelector('#review-error');if(notice)notice.textContent='';invalidateLoad();setSavingBusy(true);
  const dimensions=Object.fromEntries((Array.isArray(data.rubric)?data.rubric:[]).map(r=>[r.id,values.get('dimension:'+r.id)]));
  try{await api(`${base}/${pair.id}/review`,{revision:pair.revision,choice:values.get('choice'),reason:values.get('reason'),kind:values.get('kind'),dimensions});drafts.delete(pair.id);dirty=drafts.size>0;const loaded=await load({preserve:false,force:true});setSavingBusy(false);if(loaded){const saved=document.getElementById('review-outcome');saved?.scrollIntoView({block:'center',behavior:'instant'});saved?.focus({preventScroll:true});toast('评价已保存，进度已更新');}else toast('评价已保存，请重试读取更新');}
  catch(error){setSavingBusy(false);const notice=document.getElementById('review-error');if(!notice){toast(error.message);return;}notice.textContent=error.message;if(error.code==='stale_presentation'){drafts.delete(pair.id);dirty=drafts.size>0;const reload=document.createElement('button');reload.type='button';reload.dataset.action='reload';reload.textContent='重新载入并核对结果';notice.after(reload);}notice.scrollIntoView({block:'nearest',behavior:'instant'});}
});
app.addEventListener('click',async event=>{
  const waypoint=event.target.closest('.review-waypoints a');
  if(waypoint&&!event.metaKey&&!event.ctrlKey&&!event.shiftKey&&!event.altKey){
    const section=document.getElementById(waypoint.hash.slice(1));
    if(section){
      event.preventDefault();
      section.scrollIntoView({block:'start',behavior:'instant'});
      section.focus({preventScroll:true});
    }
    return;
  }
  const nav=event.target.closest('[data-index]');if(nav){navigatePair(Number(nav.dataset.index));return;}
  const button=event.target.closest('[data-action]');if(!button)return;
  const action=button.dataset.action;
  if(action==='previous'){navigatePair(index-1);return;}
  if(action==='next'){navigatePair(index+1);return;}
  if(action==='toggle-picker'){setPicker(!pickerOpen,{restore:pickerOpen});return;}
  if(action==='close-picker'){setPicker(false,{restore:true});return;}
  if(action==='clear-pair-search'){pairQuery='';pairFilter='all';document.getElementById('pair-search').value='';document.getElementById('pair-filter').value='all';renderPairResults();document.getElementById('pair-search').focus();return;}
  if(action==='next-unreviewed'){
    const next=nextReadyIndex();
    if(!saving&&next>=0&&next!==index)navigatePair(next);
    return;
  }
  if(action==='reload'){const loaded=await load({preserve:false});if(loaded)toast('已重新载入，请核对当前结果后评价');return;}
  if(action==='reveal'){button.disabled=true;const pair=data.pairs[index],identity={id:pair.id,revision:pair.revision};try{const result=await api(`${base}/${pair.id}/reveal`,{});if(data.pairs[index]?.id!==identity.id||data.pairs[index]?.revision!==identity.revision)return;const destination=document.getElementById('reveal-content');if(!destination)return;destination.innerHTML=`<div class="reveal">${Object.entries(result).map(([side,r])=>`<p><strong>${esc(side)}</strong>：${esc(r.configuration.name)} · ${esc(label(r.configuration.mode))}</p>`).join('')}</div>`;}catch(error){toast(error.message);button.disabled=false;}}
});
document.getElementById('anonymous-export').innerHTML=icon('download');
document.getElementById('anonymous-export').addEventListener('click',async event=>{const button=event.currentTarget;button.disabled=true;try{await download(base+'/export','anonymous-review.json');toast('匿名证据已导出，不含方案映射');}catch(error){toast(error.message);}finally{button.disabled=false;}});
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
await load();
setInterval(()=>{if(!document.hidden&&data&&!data.pairs[index]?.ready&&!dirty&&!saving&&!pickerOpen)load();},8000);
