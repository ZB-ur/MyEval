export const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const evidenceImageHandles=new WeakMap();
let evidenceImageSequence=0,activeEvidenceImageClose=null;
export function closeEvidenceImageViewer(){activeEvidenceImageClose?.();}
export function makeEvidenceImageZoomable(img){
  if(!(img instanceof HTMLImageElement)||!img.parentNode)return ()=>{};
  if(evidenceImageHandles.has(img))return evidenceImageHandles.get(img);
  const trigger=document.createElement('button'),hint=document.createElement('span'),lifetime=new AbortController(),owner=img.closest('dialog')||img.closest('main'),description=img.alt||'本次评测原图';
  trigger.type='button';trigger.className='evidence-image-trigger';trigger.setAttribute('aria-haspopup','dialog');trigger.setAttribute('aria-label',`查看原图：${description}`);trigger.title='点击或按 Enter 查看原图';
  hint.className='evidence-image-hint';hint.textContent='查看原图 ↗';hint.setAttribute('aria-hidden','true');img.replaceWith(trigger);trigger.append(img,hint);
  const ready=()=>{trigger.disabled=!img.naturalWidth;hint.textContent=img.naturalWidth?'查看原图 ↗':'图像未能显示';};
  trigger.disabled=!img.complete||!img.naturalWidth;if(img.complete)ready();img.addEventListener('load',ready,{signal:lifetime.signal});img.addEventListener('error',ready,{signal:lifetime.signal});
  let closeOwnViewer=null;
  function restoreFocus(){
    const visible=el=>el?.isConnected&&!el.disabled&&el.getClientRects().length&&!el.closest('[inert]');
    if(visible(trigger)){trigger.focus({preventScroll:true});return;}
    const scope=owner?.isConnected&&(owner.tagName!=='DIALOG'||owner.open)?owner:[...document.querySelectorAll('dialog[open]')].at(-1)||document.getElementById('blind-app')||document.getElementById('app')||document.querySelector('main');
    const replacement=[...(scope?.querySelectorAll('.evidence-image-trigger')||[])].find(el=>el.getAttribute('aria-label')===trigger.getAttribute('aria-label')&&visible(el));
    const fallback=replacement||[...(scope?.querySelectorAll('button:not(:disabled),a[href],[tabindex="0"],summary')||[])].find(visible)||scope;
    if(fallback){if(!fallback.matches('button,a[href],input,select,textarea,[tabindex],summary'))fallback.tabIndex=-1;fallback.focus({preventScroll:true});}
  }
  function open(){
    if(trigger.disabled||!img.naturalWidth)return;closeEvidenceImageViewer();
    const dialog=document.createElement('dialog'),id=`evidence-image-${++evidenceImageSequence}`,events=new AbortController();let original=false,finished=false;
    dialog.className='evidence-image-viewer';dialog.setAttribute('aria-labelledby',id+'-title');dialog.setAttribute('aria-describedby',id+'-caption');
    dialog.innerHTML=`<header class="evidence-image-header"><div><h2 id="${id}-title">查看原图</h2><p id="${id}-caption"></p></div><button type="button" class="evidence-image-close" aria-label="关闭原图查看器">${icon('close')}</button></header><div class="evidence-image-tools"><button type="button" class="evidence-image-mode" aria-pressed="false" aria-controls="${id}-viewport">原尺寸查看</button><span class="evidence-image-size" role="status"></span></div><p class="evidence-image-help" id="${id}-help">方向键平移 · Page Up / Down 翻页 · Home / End 到首尾 · Esc 关闭</p><div class="evidence-image-viewport" id="${id}-viewport" tabindex="0" role="region" aria-label="原图可滚动区域" aria-describedby="${id}-help"></div>`;
    dialog.querySelector('#'+id+'-caption').textContent=description;
    const viewport=dialog.querySelector('.evidence-image-viewport'),toggle=dialog.querySelector('.evidence-image-mode'),closeButton=dialog.querySelector('.evidence-image-close'),size=dialog.querySelector('.evidence-image-size'),draggable=img.getAttribute('draggable'),bounds=trigger.getBoundingClientRect(),placeholder=document.createElementNS('http://www.w3.org/2000/svg','svg');
    placeholder.setAttribute('class','evidence-image-placeholder');placeholder.setAttribute('viewBox',`0 0 ${bounds.width||1} ${bounds.height||1}`);placeholder.setAttribute('width',String(bounds.width||1));placeholder.setAttribute('height',String(bounds.height||1));placeholder.setAttribute('aria-hidden','true');trigger.insertBefore(placeholder,img);
    const setMode=()=>{viewport.classList.toggle('is-original',original);toggle.setAttribute('aria-pressed',String(original));toggle.textContent=original?'适合窗口':'原尺寸查看';size.textContent=`${original?'原尺寸':'适合窗口'} · ${img.naturalWidth} × ${img.naturalHeight}`;viewport.scrollTo({left:0,top:0,behavior:'instant'});};
    function finish(){if(finished)return;finished=true;events.abort();trigger.insertBefore(img,hint);placeholder.remove();if(draggable===null)img.removeAttribute('draggable');else img.setAttribute('draggable',draggable);dialog.remove();if(activeEvidenceImageClose===close)activeEvidenceImageClose=null;closeOwnViewer=null;restoreFocus();}
    function close(){if(dialog.open)dialog.close();finish();}
    activeEvidenceImageClose=close;closeOwnViewer=close;img.draggable=false;viewport.append(img);document.body.append(dialog);setMode();
    closeButton.addEventListener('click',close,{signal:events.signal});toggle.addEventListener('click',()=>{original=!original;setMode();},{signal:events.signal});
    dialog.addEventListener('cancel',event=>{event.preventDefault();event.stopPropagation();close();},{signal:events.signal});dialog.addEventListener('close',finish,{signal:events.signal});
    dialog.addEventListener('keydown',event=>{
      if(event.key==='Escape'){event.preventDefault();event.stopPropagation();close();return;}
      if(event.key==='Tab'){const items=[closeButton,toggle,viewport],position=items.indexOf(document.activeElement);if(event.shiftKey&&position<=0){event.preventDefault();viewport.focus();}else if(!event.shiftKey&&(position===items.length-1||position<0)){event.preventDefault();closeButton.focus();}return;}
      if(event.target!==viewport||event.altKey||event.ctrlKey||event.metaKey)return;
      const page=Math.max(80,viewport.clientHeight*.85),step=80,keys={ArrowLeft:[-step,0],ArrowRight:[step,0],ArrowUp:[0,-step],ArrowDown:[0,step],PageUp:[0,-page],PageDown:[0,page]};
      if(keys[event.key]){event.preventDefault();viewport.scrollBy({left:keys[event.key][0],top:keys[event.key][1],behavior:'instant'});}
      else if(event.key==='Home'||event.key==='End'){event.preventDefault();viewport.scrollTo({left:event.key==='Home'?0:viewport.scrollWidth,top:event.key==='Home'?0:viewport.scrollHeight,behavior:'instant'});}
    },{signal:events.signal});
    try{dialog.showModal();viewport.focus({preventScroll:true});}catch{finish();}
  }
  trigger.addEventListener('click',open,{signal:lifetime.signal});
  const dispose=()=>{closeOwnViewer?.();lifetime.abort();if(trigger.parentNode)trigger.replaceWith(img);evidenceImageHandles.delete(img);};evidenceImageHandles.set(img,dispose);return dispose;
}
export function associateFormLabels(root){
  for(const [index,label]of [...root.querySelectorAll('label')].entries()){
    const controls=[...label.querySelectorAll('input:not([type=hidden]),select,textarea')];
    const control=label.htmlFor?document.getElementById(label.htmlFor):controls.length===1?controls[0]:null;
    if(!control)continue;
    if(!control.id){const scope=control.closest('form')?.id||root.id||'field',key=control.name||String(index),value=['checkbox','radio'].includes(control.type)?'-'+control.value:'';control.id=scope+'-'+encodeURIComponent(key+value);}
    label.htmlFor=control.id;
    const help=[...label.querySelectorAll('small,.form-help')];
    help.forEach((el,i)=>{el.id||=control.id+'-help-'+i;});
    const described=[...(control.getAttribute('aria-describedby')||'').split(/\s+/),...help.map(el=>el.id)].filter(Boolean);
    if(described.length)control.setAttribute('aria-describedby',[...new Set(described)].join(' '));
    if(control.hasAttribute('aria-label')||control.hasAttribute('aria-labelledby'))continue;
    const names=[];
    const wrap=parent=>{for(const child of [...parent.childNodes]){
      if(child.nodeType===Node.TEXT_NODE&&child.textContent.trim()){
        const text=document.createElement('span');text.id=control.id+'-label-'+names.length;text.className='field-label';child.replaceWith(text);text.append(child);names.push(text.id);
      }else if(child.nodeType===Node.ELEMENT_NODE&&!child.matches('input,select,textarea,button,small,.form-help,svg,[aria-hidden=true]'))wrap(child);
    }};
    wrap(label);if(names.length)control.setAttribute('aria-labelledby',names.join(' '));
  }
}
export const names = {sijian:'思间 Sijian',pome:'日记本 pome',queued:'待调度',running:'运行中',cancelling:'请求停止中',succeeded:'执行成功',failed:'失败',timed_out:'超时',cancelled:'已停止',interrupted:'执行中断',refused:'拒绝',unsupported:'不支持',completed:'运行完成',completed_with_issues:'完成 · 有异常',published:'已同步',unconfigured:'未配置',pass:'通过',passed:'通过',fail:'未通过',unknown:'未知',human:'人工评价',acceptance:'验收试填',fixed_context_rerun:'固定上下文重跑',isolated_full_run:'隔离环境完整执行',trace_rescore:'旧轨迹重评分',user:'儿童原话',child:'儿童原话',child_id:'虚构身份',child_turns:'本次儿童原话（逐轮）',activity_date:'活动日期',created_at:'建立时间',theme_id:'日记本主题',conversation_rounds:'最多轮数',duck:'鸭鸭',assistant:'AI 交付',transcript:'逐轮对话',summary:'整理结果',text:'正文',role:'角色',answer:'回应',citations:'引用',quote:'引文',answer_kind:'回应类型',relationships:'发现的联系',center_quote:'中心原文证据',candidate_quote:'候选原文证据',explanation:'联系说明',rationale:'理由',connection:'联系',materials:'选定材料',question:'问题',history:'历史对话',center:'中心想法',candidates:'候选材料',input:'输入',parameters:'请求参数',reason:'理由',dimension:'维度',status:'状态'};
Object.assign(names,{responsive:'回应关键意思',encouragement:'有据的鼓励',context_use:'历史与档案运用',concise:'简洁自然',faithfulness:'事实与整理忠实',scores:'各维评分',pair_preference:'匿名配对偏好',hard_violations:'事实或安全问题',context_opportunity:'相关背景机会',children:'幼儿档案',animals:'动物档案',records:'既有记录',reviewedAt:'教师核对时间',activityDate:'活动日期',createdAt:'建立时间',animalType:'动物种类',name:'名称',note:'备注',budgerigar:'虎皮鹦鹉',hamster:'金丝熊'});
Object.assign(names,{dialogue_faithfulness:'聊天事实忠实',summary_faithfulness:'日记事实忠实',score_status:'评分状态',rated:'已评分',not_applicable:'不适用',unavailable:'无法评估',unrated:'未评分',case_count:'案例数',family_count:'情境家族数',family_id:'情境家族',variant:'案例变式',split:'材料分组',expected_behavior:'期望边界',allowed_facts:'允许依据的事实',forbidden_inferences:'不得推断的内容',turn_focus:'逐轮关注点',expected_background_texts:'预期可用背景',summary_forbidden_fragments:'整理中不得加入的内容',source:'材料来源'});
Object.assign(names,{context_by_turn:'各轮实际提供的背景',turn:'轮次',background:'背景资料',recentStories:'已选历史文字',animals:'动物名字与种类',reviewStatus:'历史核对状态',history_id:'历史记录编号',sourceKind:'历史文字来源',allowed_background_texts:'允许的历史候选（不代表已注入）',expected_history_ids:'预期有价值的历史候选编号'});
export const label = value => names[value] || value;
export const badge = value => `<span class="badge ${esc(value)}">${esc(label(value) || '未知')}</span>`;
export const json = value => `<pre>${esc(JSON.stringify(value, null, 2))}</pre>`;
export const date = value => value ? new Date(value).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:'Asia/Shanghai'}) : '—';
export const duration = value => value === null || value === undefined ? '—' : `${(value/1000).toFixed(1)} s`;
const performanceNumber = value => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN') : '未知';
const timingCell = value => value ? `<span class="performance-value">${duration(value.p50_ms)} / ${duration(value.p95_ms)}</span><div class="subline">n=${value.n} · 缺失 ${value.missing}</div>` : '<span class="muted">未观测</span>';
const operationName = value => ({chat:'聊天 · 每轮交付',summary:'整理 · 请求到响应',state:'状态读写',discover:'发现 · 请求到响应',review:'复盘 · 请求到响应'}[value]||value);
const usageCell = value => value ? `<span class="performance-value">${performanceNumber(value.known_total)}</span><div class="subline">缺失 ${value.missing_call_records} 次调用${value.missing_attempt_records?` / ${value.missing_attempt_records} 次整次用量`:''}${value.failed_attempt_known_total!==null?`<br>异常执行中已知 ${performanceNumber(value.failed_attempt_known_total)}`:''}</div>` : '未知';
function performanceEvidencePanel(performance){
  if(!performance)return '';
  const configurations=Object.values(performance.configurations||{});
  const tokenNames={input_tokens:'输入 tokens',output_tokens:'输出 tokens',total_tokens:'总 tokens',reasoning_tokens:'推理 tokens（输出子集）',cache_hit_input_tokens:'输入缓存命中 tokens',cache_miss_input_tokens:'输入缓存未命中 tokens'};
  return `<section class="section performance"><div class="section-head"><div><h2>耗时与模型用量</h2><p class="muted">整个实验 · 全部执行尝试，包含旧失败；不随上方逐项筛选改变</p></div><span class="tag">TTFT 不可用</span></div>
    <div class="panel table-wrap"><table><caption class="performance-caption">耗时 p50 / p95 · 每格列出有效样本 n 和缺失数</caption><thead><tr><th>配置 / 执行分母</th><th>观察范围</th><th>全部</th><th>成功</th><th>异常</th><th>未闭合请求</th></tr></thead><tbody>${configurations.map(c=>{
      const modelOperations=new Set(c.model_stages.map(stage=>stage.operation));
      const primaryOperations=Object.entries(c.operations).filter(([name])=>!modelOperations.size||modelOperations.has(name));
      const rows=[['完整 episode',c.episode],...primaryOperations.map(([name,timing])=>[operationName(name),timing])];
      return rows.map(([name,timing],index)=>`<tr>${index===0?`<td rowspan="${rows.length}" class="performance-config"><strong>${esc(c.name)}</strong><div class="subline">当前 trial：成功 ${c.trials.succeeded} / 异常 ${c.trials.failed_terminal} / 未终态 ${c.trials.active}<br>共 ${c.trials.total} 个 trial · ${c.attempts.total} 次执行尝试<br>额外执行重试 ${c.attempts.execution_retries} 次<br>未开始 ${c.attempts.not_started} · 未终态 ${c.attempts.active}${c.attempts.missing_event_files?`<br>缺少事件文件 ${c.attempts.missing_event_files} 次`:''}</div></td>`:''}<td>${esc(name)}</td><td>${timingCell(timing.all)}</td><td>${timingCell(timing.succeeded)}</td><td>${timingCell(timing.failed)}</td><td>${timing.unfinished.count}</td></tr>`).join('');
    }).join('')}</tbody></table></div>
    <p class="source-note">完整 episode 只统计已开始且已终态的执行，含启动、保存与清理。主表展示包含模型阶段的操作；全部 HTTP 操作在下方可展开。聊天从 HTTP 发送到完整回复读取/解析，包含内部多阶段与重试，不含设备渲染、语音或用户输入时间。没有操作事件时不推算聊天/整理耗时。成功与异常按各自执行或请求的结果分组。</p>
    <details class="performance-details"><summary>全部 HTTP 操作耗时（含状态读写）</summary>${configurations.map(c=>`<h3>${esc(c.name)}</h3><div class="table-wrap"><table><thead><tr><th>操作</th><th>全部 p50 / p95</th><th>成功</th><th>异常</th><th>未闭合</th></tr></thead><tbody>${Object.entries(c.operations).map(([name,timing])=>`<tr><td>${esc(operationName(name))}</td><td>${timingCell(timing.all)}</td><td>${timingCell(timing.succeeded)}</td><td>${timingCell(timing.failed)}</td><td>${timing.unfinished.count}</td></tr>`).join('')}</tbody></table></div>`).join('')}</details>
    <div class="panel table-wrap"><table><caption class="performance-caption">已知模型用量 · 包含异常执行中已发生的调用</caption><thead><tr><th>配置</th><th>实际模型请求</th><th>内部重试</th><th>输入 tokens</th><th>输出 tokens</th><th>总 tokens</th><th>用量覆盖</th></tr></thead><tbody>${configurations.map(c=>`<tr><td>${esc(c.name)}</td><td>${performanceNumber(c.model.observed_requests+c.model.reported_requests_without_events)}<div class="subline">事件观测 ${c.model.observed_requests}${c.model.reported_requests_without_events?`<br>整次记录报告 ${c.model.reported_requests_without_events}`:''}<br>请求数未知 ${c.model.request_count_unknown_attempts} 次执行</div></td><td>${c.model.internal_retries}<div class="subline">实际发出<br>无法判断：${c.model.retry_unclassified_calls} 次调用 / ${c.model.retry_unknown_attempts} 次执行</div></td><td>${usageCell(c.usage.fields.input_tokens)}</td><td>${usageCell(c.usage.fields.output_tokens)}</td><td>${usageCell(c.usage.fields.total_tokens)}</td><td>${c.usage.call_records} 条调用用量记录<div class="subline">完全缺失 ${c.usage.calls_missing_usage} 条<br>另有整次汇总 ${c.usage.fallback_attempt_records} 条<br>调用与用量均缺失 ${c.usage.attempts_without_usage_or_calls} 次执行</div></td></tr>`).join('')}</tbody></table></div>
    <p class="source-note">这里只相加已知用量，缺失不是零；没有逐调用事件时才采用整次用量。输出 tokens 可能包含推理 tokens，不等于可见回复长度，不计算 tokens/s。内部重试与 trial 的额外执行重试分别统计。</p>
    <details class="performance-details"><summary>分阶段耗时、缓存与计数口径</summary>${configurations.map(c=>`<h3>${esc(c.name)}</h3><div class="table-wrap"><table><thead><tr><th>操作 / 模型阶段</th><th>全部 p50 / p95</th><th>响应 p50 / p95</th><th>错误 p50 / p95</th><th>未闭合 / 无请求配对</th></tr></thead><tbody>${c.model_stages.map(s=>`<tr><td>${esc(s.operation)} / ${esc(s.stage)}</td><td>${timingCell(s.timing.all)}</td><td>${timingCell(s.timing.succeeded)}</td><td>${timingCell(s.timing.failed)}</td><td>${s.timing.unfinished.count} / ${s.timing.unmatched_response.count}</td></tr>`).join('')||'<tr><td colspan="5" class="muted">没有可配对的模型请求事件，阶段耗时不可用。</td></tr>'}</tbody></table></div><p class="source-note">阶段错误 ${c.model.stage_errors} 次；声明将重试 ${c.model.retry_intents} 次（仅意图）；实际内部重试 ${c.model.internal_retries} 次。响应后的结构校验错误不会改写已完成的模型响应耗时。</p><div class="table-wrap"><table><thead><tr><th>用量字段</th><th>已知合计 / 缺失</th><th>有值记录数</th></tr></thead><tbody>${Object.entries(tokenNames).map(([key,name])=>`<tr><td>${esc(name)}</td><td>${usageCell(c.usage.fields[key])}</td><td>${c.usage.fields[key].known_records}${c.usage.fields[key].derived_records?`<div class="subline">其中 ${c.usage.fields[key].derived_records} 条由输入＋输出推算</div>`:''}</td></tr>`).join('')}</tbody></table></div>`).join('')}<p class="source-note">${esc(performance.quantiles.definition)}</p><p class="source-note">${esc(performance.ttft.reason)}</p><ul class="source-note">${performance.notes.map(note=>`<li>${esc(note)}</li>`).join('')}</ul></details>
    <p class="source-note">分位算法：nearest-rank，升序后取第 ceil(p × n) 项，不插值；小样本 p95 常为最大值。变式与重复不增加独立情境家族；并发、缓存和供应商时段可能影响时延。</p></section>`;
}
export function performancePanel(performance){
  if(!performance)return '';
  const configurations=Object.values(performance.configurations||{});
  return `<section class="section performance"><div class="section-head"><div><h2>性能摘要</h2></div></div><div class="panel table-wrap"><table><thead><tr><th>方案</th><th>完整执行 p50 / p95</th><th>已知总 tokens</th><th>用量覆盖</th></tr></thead><tbody>${configurations.map(c=>`<tr><td><strong>${esc(c.name)}</strong><div class="subline">${c.trials?.total??'—'} 项运行 · ${c.attempts?.total??'—'} 次尝试，其中未开始 ${c.attempts?.not_started??'—'}</div></td><td>${timingCell(c.episode?.all)}</td><td>${usageCell(c.usage?.fields?.total_tokens)}</td><td>${c.usage?.call_records??0} 条调用用量<div class="subline">完全缺失 ${c.usage?.calls_missing_usage??0} 条<br>另有整次汇总 ${c.usage?.fallback_attempt_records??0} 条</div></td></tr>`).join('')}</tbody></table></div><details class="performance-details"><summary>性能证据与统计口径</summary><p class="source-note">尝试记录包含未发送与异常。耗时只使用可观测区间，小样本 p95 常为最大值。缺失用量单列，已知 tokens 仅表示已记录部分。</p>${performanceEvidencePanel(performance)}</details></section>`;
}
export function content(value, depth=0, {preserveValues=false,compactMetadata=false}={}) {
  if (value === undefined || value === null&&!preserveValues) return '<p class="muted">未记录 / 未知</p>';
  if (value === null) return '<p class="stored-empty"><code>null</code><span>显式空值</span></p>';
  if (typeof value !== 'object') {
    if(preserveValues&&value==='')return '<p class="stored-empty"><code>""</code><span>空字符串</span></p>';
    return `<p>${esc(typeof value === 'boolean' ? (preserveValues?String(value):(value ? '是' : '否')) : value)}</p>`;
  }
  if (Array.isArray(value)) return value.length ? value.map((v,i) => `<div class="tree-item">${value.length>1 ? `<div class="tree-title">${i+1}</div>` : ''}${content(v,depth+1,{preserveValues,compactMetadata})}</div>`).join('') : preserveValues?'<p class="stored-empty"><code>[]</code><span>空数组 · 0 项</span></p>':'<p class="muted">无</p>';
  const messageKeys=Object.keys(value),messageText=typeof value.text==='string'?value.text:typeof value.content==='string'?value.content:null;
  if (value.role&&messageKeys.length===2&&messageText!==null) return `<div class="tree"><div class="tree-title">${esc(label(value.role))}</div>${content(messageText,depth+1,{preserveValues,compactMetadata})}</div>`;
  if(preserveValues&&!Object.keys(value).length)return '<p class="stored-empty"><code>{}</code><span>空对象</span></p>';
  const metaKeys=new Set(['id','version','author','material_id','material_version','candidate_id','candidate_version','center_id','center_version','created_at','createdAt','updated_at','sha256','hash']);
  const entries=Object.entries(value),metadata=compactMetadata?entries.filter(([key,v])=>metaKeys.has(key)&&(v===null||typeof v!=='object')):[],keys=new Set(metadata.map(([key])=>key));
  const meta=metadata.length?`<details class="compact-metadata"><summary>标识与版本 · ${metadata.length} 项</summary><dl>${metadata.map(([key,v])=>`<dt>${esc(label(key))}</dt><dd>${esc(v===null?'null':v)}</dd>`).join('')}</dl></details>`:'';
  return entries.filter(([key])=>!keys.has(key)).map(([k,v]) => `<div class="${depth?'tree':'tree-item'}"><div class="tree-title">${esc(label(k))}</div>${content(v,depth+1,{preserveValues,compactMetadata})}</div>`).join('')+meta;
}
const pointerPart=value=>String(value).replace(/~/g,'~0').replace(/\//g,'~1');
const documentMetadata=new Set(['id','version','author','material_id','material_version','candidate_id','candidate_version','center_id','center_version','created_at','createdAt','updated_at','sha256','hash','content_hash','source_hash','authorization_hash']);
function documentValue(value,path='',depth=0){
  if(value===null||typeof value!=='object')return content(value,0,{preserveValues:true});
  if(Array.isArray(value))return value.length?value.map((item,i)=>`<section class="document-item" data-read-anchor="${esc(path+'/'+i)}" data-read-title="第 ${i+1} 项">${value.length>1?`<h4 class="document-item-label">${i+1}</h4>`:''}${documentValue(item,path+'/'+i,depth+1)}</section>`).join(''):content(value,0,{preserveValues:true});
  const entries=Object.entries(value);if(!entries.length)return content(value,0,{preserveValues:true});
  const metadata=entries.filter(([key,v])=>documentMetadata.has(key)&&(v===null||typeof v!=='object')),metadataKeys=new Set(metadata.map(([key])=>key));
  return entries.filter(([key])=>!metadataKeys.has(key)).map(([key,v])=>{
    const field=path+'/'+pointerPart(key),structured=v!==null&&typeof v==='object',fact=v!==null&&!structured&&(typeof v!=='string'||['status','score_status','turn','date','activity_date','role','type','kind','split','variant'].includes(key)&&v.length<55&&!v.includes('\n'));
    return `<section class="document-field ${structured?'document-section':fact?'document-fact':'document-prose'}" data-read-anchor="${esc(field)}" data-read-title="${esc(label(key))}" ${structured?'data-reader-heading="true"':''}><h${depth<2?'3':'4'}>${esc(label(key))}</h${depth<2?'3':'4'}>${documentValue(v,field,depth+1)}</section>`;
  }).join('')+(metadata.length?`<details class="compact-metadata"><summary>标识与版本 · ${metadata.length} 项</summary><dl>${metadata.map(([key,v])=>`<dt>${esc(label(key))}</dt><dd>${esc(v===null?'null':v)}</dd>`).join('')}</dl></details>`:'');
}
export const evidenceContent=value=>`<div class="document-content">${documentValue(value)}</div>`;
export function outputContent(value){
  if(!value||typeof value!=='object'||Array.isArray(value))return evidenceContent(value);
  const technical=new Set(['raw_output','raw_response','media','metadata','provider_usage','usage','observed_configuration']);
  const readable=Object.fromEntries(Object.entries(value).filter(([key])=>!technical.has(key)));
  const evidence=Object.fromEntries(Object.entries(value).filter(([key])=>technical.has(key)));
  if(!Object.keys(readable).length)return evidenceContent(value);
  return evidenceContent(readable)+(Object.keys(evidence).length?`<details class="output-technical"><summary>原始输出与技术记录</summary><p class="source-note">保留原始正文、附件与运行字段，供核对；可能与上方结构化交付重复。</p>${Object.entries(evidence).map(([key,v])=>`<section data-read-anchor="/${esc(pointerPart(key))}" data-read-title="${esc(label(key))}"><h4>${esc(label(key))}</h4>${json(v)}</section>`).join('')}</details>`:'');
}
export async function api(url, body, options={}) {
  const response = await fetch(url, {headers:requestHeaders(url,body!==undefined),...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)}),signal:options.signal});
  let value;
  try {value = await response.json();} catch {throw new Error(`服务未返回可读取的数据（HTTP ${response.status}）`);}
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

Object.assign(names,{demo:'办公演示',sidelit:'Sidelit',paused:'已暂停',pending:'待处理',stopped:'已停止',inconclusive:'证据不足',blocked:'未满足',synthetic:'合成材料',licensed_public:'许可公开材料',authorized_private:'已授权私有材料',development:'开发集',holdout:'留出集',all:'全部材料',cancel_requested:'请求取消中',not_run:'未运行',complete:'已完成',error:'异常',trace_import:'历史轨迹导入'});
export const number = value => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN') : '—';
export function sourceLabel(value){
  const source = value?.source || value;
  const kind = typeof source === 'string' ? source : source?.kind || source?.type || (source?.synthetic===true?'synthetic':null);
  return label(kind) || '来源未标注';
}
export function icon(name, className='') {
  const paths={
    overview:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    experiments:'<path d="M9 3h6m-5 0v6l-5.5 9a2 2 0 0 0 1.7 3h11.6a2 2 0 0 0 1.7-3L14 9V3M8 14h8"/>',
    dataset:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    score:'<path d="M9 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-4M9 12l3 3L22 5M8 7h5M8 18h6"/>',
    sliders:'<path d="M4 7h5m4 0h7M4 17h9m4 0h3"/><circle cx="11" cy="7" r="2"/><circle cx="15" cy="17" r="2"/>',
    activity:'<path d="M2 12h5l3-9 4 18 3-9h5"/>',
    book:'<path d="M12 5v16m0-16C9 2 5 2 2 3v16c3-1 7-1 10 2 3-3 7-3 10-2V3c-3-1-7-1-10 2Z"/>',
    plus:'<path d="M12 5v14M5 12h14"/>', arrow:'<path d="M5 12h14m-5-5 5 5-5 5"/>',back:'<path d="M19 12H5m5-5-5 5 5 5"/>',
    chevron:'<path d="m9 5 7 7-7 7"/>', down:'<path d="m6 9 6 6 6-6"/>',
    external:'<path d="M14 3h7v7M21 3l-9 9M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5"/>',
    search:'<circle cx="10.8" cy="10.8" r="6.8"/><path d="m16 16 5 5"/>',refresh:'<path d="M20 7a9 9 0 1 0 1 8M20 3v5h-5"/>',
    check:'<path d="m5 12 4 4L19 6"/>',close:'<path d="m6 6 12 12M18 6 6 18"/>',clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    pause:'<path d="M8 5v14M16 5v14"/>',play:'<path d="m8 4 12 8-12 8Z"/>',stop:'<rect x="5" y="5" width="14" height="14" rx="1"/>',
    download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',upload:'<path d="M12 16V4m-5 5 5-5 5 5M4 16v5h16v-5"/>',
    compare:'<path d="M12 3v18M5 6h2M5 12h2M5 18h2M17 6h2M17 12h2M17 18h2"/>',
    shield:'<path d="m12 2 8 3v7c0 5-8 10-8 10S4 17 4 12V5Z"/><path d="m8 11 3 3 5-6"/>',
    info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7v.1"/>',warning:'<path d="M10.3 3.8 2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.8a2 2 0 0 0-3.4 0ZM12 9v4M12 17v.1"/>',
    menu:'<path d="M4 6h16M4 12h16M4 18h16"/>',layers:'<path d="m12 3 10 6-10 6L2 9Zm-10 12 10 6 10-6M2 12l10 6 10-6"/>',
    file:'<path d="M14 2H5v20h14V7Zm0 0v6h5M8 12h8M8 16h8"/>',eye:'<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
    lock:'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V6a4 4 0 0 1 8 0v4M12 14v3"/>',
    spark:'<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5ZM20 2v4M18 4h4"/>',video:'<rect x="2" y="5" width="14" height="14" rx="2"/><path d="m16 10 6-4v12l-6-4"/>'
  };
  return `<svg class="icon ${esc(className)}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name]||paths.file}</svg>`;
}

Object.assign(names,{committing:'保存评价中',completed_with_errors:'完成 · 评分有异常',budget_exhausted:'预算已用尽',invalid_evidence:'证据校验失败',provider_failed:'评分器失败',current_output_only_for_quality:'质量仅使用当前输出',unknown_units:'用量未知单元',known_total_tokens:'已知总 tokens',model_requests:'模型请求',cost:'费用'});

export function licenseText(source={}){
  const license=source.license;
  if(typeof license==='string')return license;
  if(license&&typeof license==='object')return license.name||license.id||license.spdx_id||'许可记录';
  return '';
}
export function attributionText(source={}){
  const value=source.attribution||source.author||source.credit;
  if(typeof value==='string')return value;
  return value&&typeof value==='object'?(value.text||value.name||value.author||''):'';
}

Object.assign(names,{accepted:'接受此判断',rejected:'不接受此判断',needs_followup:'需要进一步核对',invalid:'证据无效',commit_pending:'待保存评价',missing_assessment:'缺少评价',missing_dimension:'缺少维度',missing_trial:'缺少运行',invalid_score:'无效分值',incomplete_pair_score:'配对不完整',candidate:'候选',baseline:'基线',tie:'平局',unable:'无法判断'});

Object.assign(names,{notes:'说明',authorization:'授权声明',description:'说明',privacy:'材料属性',as_of:'材料时间',license:'许可',attribution:'署名',author:'作者',source_types:'材料来源分类',expected_pairs:'预期配对',missing_trials:'缺失运行',known_total_tokens:'已知总 tokens',unknown_units:'用量未知单元',effective:'实际生效',requested:'请求配置'});

Object.assign(names,{created:'已建立',partial:'部分完成',not_evaluated:'尚未判定',stale:'证据已更新',different_policy:'使用不同策略',creation_failed:'建立失败'});

Object.assign(names,{report:'交付报告',composition:'构图',lighting:'光线',color:'色彩',technical:'技术表现',feedback:'点评',score:'输出中的分值',overall_score:'输出中的整体分值',suggestions:'建议',strengths:'亮点',weaknesses:'不足',evidence:'评价引文与依据',quote:'原文引文',reason:'理由',dimension:'维度',raw_output:'原始输出',historical_import:'历史证据导入'});

Object.assign(names,{quote_not_found:'引文未匹配',rated_without_valid_evidence:'评分缺少有效引文',invalid_judgment_evidence:'判断结构或引文校验未通过'});

Object.assign(names,{id:'标识',version:'版本',author:'作者',material_id:'材料标识',material_version:'材料版本',candidate_id:'候选材料标识',candidate_version:'候选材料版本',reply:'回应正文',intent:'明确意图',difference:'保留的差异',valid:'验证有效',invalid:'验证未通过',no_response:'无原响应',ineligible:'不满足验证条件',verified_artifact:'收据已核验'});

Object.assign(names,{request:'请求内容',kind:'类别',url:'来源链接',source_type:'来源类型',source_url:'来源链接',license_url:'许可链接',family_means:'各家族均值',mean:'均值',trial_count:'运行数'});
