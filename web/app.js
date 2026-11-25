import {esc,label,badge,json,content,outputContent,evidenceContent,associateFormLabels,makeEvidenceImageZoomable,closeEvidenceImageViewer,api,date,duration,download,performancePanel,icon,number,sourceLabel,requestHeaders,licenseText,attributionText} from '/view.js';

if(location.hash.startsWith('#access=')){
  sessionStorage.setItem('myeval.admin',location.hash.slice(8));
  history.replaceState(null,'','/#overview');
}
const $=id=>document.getElementById(id), app=$('app'), modal=$('modal');
const terminal=new Set(['succeeded','failed','timed_out','cancelled','interrupted','refused','unsupported']);
const state={catalog:[],datasets:[],capabilities:{},health:null,current:null,experiments:[],route:'',request:0,controller:null,lastFingerprint:'',toastTimer:null,wizard:null,compare:null,importValue:null};
const filters={project:'',status:'',search:'',case:'',trialStatus:''};
const rubricEvidence=new Map();
const rubricBindings=new Map();
const rubricDimensions=r=>Array.isArray(r)?r:Array.isArray(r?.dimensions)?r.dimensions:Object.entries(r?.dimensions||{}).map(([id,d])=>({id,...d}));
function bindRubric(version,hash,scope=''){if(!version||!/^[a-f0-9]{64}$/i.test(hash||''))return false;const key=JSON.stringify([scope,version]);if(!rubricBindings.has(key))rubricBindings.set(key,new Set());rubricBindings.get(key).add(hash);return true;}
function matchingRubric(version,hash,scope=''){const hashes=rubricBindings.get(JSON.stringify([scope,version]));if(hash===undefined){if(hashes?.size!==1)return null;hash=[...hashes][0];}if(!/^[a-f0-9]{64}$/i.test(hash||''))return null;return rubricEvidence.get(JSON.stringify([version,hash]));}
function assessmentRubricHash(a){const top=a.rubric_hash,nested=a.value?.rubric_hash;if(top&&nested&&top!==nested)return null;return top||nested||null;}
function rememberRubric(version,hash,rubric,source,jobId='',scope=''){
  if(!bindRubric(version,hash,scope))return;
  rubricEvidence.set(JSON.stringify([version,hash]),{hash,rubric,source,jobId});
}
async function loadRubricEvidence(ctx,e,knownRecords=[]){
  const scope=e?.id||'';
  const versions=new Set([...(e?.comparison?.model_dimensions||[]).map(d=>d.rubric_version),...knownRecords.map(a=>a.rubric_version)].filter(Boolean));
  for(const a of knownRecords)bindRubric(a.rubric_version,assessmentRubricHash(a),scope);
  if(!versions.size||[...versions].every(v=>matchingRubric(v,undefined,scope))&&knownRecords.every(a=>matchingRubric(a.rubric_version,assessmentRubricHash(a))))return;
  const [jobs,catalog]=await Promise.all([e?.id?optional(ctx,`/api/evaluation-jobs?experiment_id=${encodeURIComponent(e.id)}`):Promise.resolve({value:{jobs:[]}}),optional(ctx,'/api/rubrics')]);
  const summaries=[...new Map((jobs.value?.jobs||[]).filter(j=>j.rubric_hash).map(j=>[j.rubric_hash,j])).values()];
  const frozen=await Promise.all(summaries.map(j=>optional(ctx,'/api/evaluation-jobs/'+encodeURIComponent(j.id))));
  for(const result of frozen){const j=result.value;if(j?.rubric){const version=`${j.rubric.id}@${j.rubric_version}@${j.rubric_hash.slice(0,12)}`;rememberRubric(version,j.rubric_hash,j.rubric,'原评分作业的冻结量表',j.id,scope);}}
  const applyRecords=records=>{for(const a of records){const hash=assessmentRubricHash(a);bindRubric(a.rubric_version,hash,scope);if(matchingRubric(a.rubric_version,hash))continue;const exact=(catalog.value?.rubrics||[]).find(r=>r.rubric_hash===hash);if(exact)rememberRubric(a.rubric_version,hash,exact,'与本条评价完整哈希一致的量表目录','',scope);}};
  applyRecords(knownRecords);
  // A historical import may omit its source scoring jobs. Verify full hashes from
  // actual assessment records before using the current registry's definitions.
  const targets=e?.trials||[];
  for(let start=0;start<Math.min(targets.length,12)&&[...versions].some(v=>!matchingRubric(v,undefined,scope));start+=4){
    const trials=await Promise.all(targets.slice(start,start+4).map(t=>optional(ctx,'/api/trials/'+encodeURIComponent(t.id))));
    for(const trial of trials)applyRecords(trial.value?.assessment_records||[]);
  }
}
function rubricHelp(version,dimension,hash=undefined,scope='',compact=false){
  const evidence=matchingRubric(version,hash,scope),d=rubricDimensions(evidence?.rubric).find(v=>v.id===dimension),definition=d?.definition||d;
  if(!definition)return '<span class="scale-unavailable">量表范围未携带</span>';
  const anchorObject=definition.anchors&&typeof definition.anchors==='object'&&!Array.isArray(definition.anchors)?definition.anchors:definition;
  const anchors=Object.entries(anchorObject).filter(([key])=>key.trim()!==''&&Number.isFinite(Number(key))).sort(([a],[b])=>Number(a)-Number(b));
  const values=anchors.length?anchors.map(([key])=>Number(key)):Array.isArray(d.anchor_values)?d.anchor_values:Array.isArray(definition.scale)?definition.scale.filter(v=>typeof v==='number'):[];
  const low=values.length?Math.min(...values):definition.min??definition.scale?.min??d.min,high=values.length?Math.max(...values):definition.max??definition.scale?.max??d.max;
  const range=typeof low==='number'&&typeof high==='number'?`${low}–${high} 分`:'范围未声明';
  return `<details class="rubric-help"><summary>${compact?'':'量表 '}${esc(range)} · ${compact?'档位':'档位说明'}</summary><div class="rubric-anchors"><strong>${esc(d.name||definition.name||label(dimension))}</strong>${definition.description?`<p>${esc(definition.description)}</p>`:''}${anchors.length?`<dl>${anchors.map(([score,text])=>`<dt>${esc(score)} 分</dt><dd>${typeof text==='object'?content(text):esc(text)}</dd>`).join('')}</dl>`:'<p>当前记录未携带各档原文，不能由分值推断业务含义。</p>'}${definition.not_applicable||definition.NA?`<p>不适用：${esc(definition.not_applicable||definition.NA)}</p>`:''}<p class="source-note">${esc(evidence.source)} · ${esc(evidence.rubric.declared_id||evidence.rubric.id||'量表')} v${esc(evidence.rubric.version||'未声明')}</p><details><summary>量表版本与完整哈希</summary><p class="mono">${esc(version)}<br>${esc(evidence.hash)}</p>${evidence.jobId?`<a href="${routeUrl('job',evidence.jobId)}" data-action="close-route">查看冻结评分作业</a>`:''}</details></div></details>`;
}
const opt=(value,text,selected=false)=>`<option value="${esc(value)}" ${selected?'selected':''}>${esc(text)}</option>`;
const routeUrl=(route,id='',tab='')=>'#'+[route,id,tab].filter(Boolean).map(encodeURIComponent).join('/');
const countFamilies=cases=>new Set((cases||[]).map(c=>c.family_id||c.id)).size;
const countCases=e=>e.case_count??e.independent_cases??e.cases?.length??0;
const families=e=>e.family_count??e.independent_cases??countFamilies(e.cases||[]);
const configurationCount=e=>e.configuration_ids?.length??e.configuration_count??e.configurations?.length??2;
const sum=(rows,getter)=>rows.reduce((n,r)=>n+(Number(getter(r))||0),0);
const statusCounts=e=>e.counts||e.status_counts||{};
const issues=e=>Object.entries(statusCounts(e)).filter(([k])=>terminal.has(k)&&k!=='succeeded').reduce((n,[,v])=>n+v,0);
const experimentStatus=e=>e.stop_requested_at?'stopped':e.status;
const experimentBadge=e=>badge(experimentStatus(e))+(issues(e)?`<div class="subline">${issues(e)} 项异常终态</div>`:'');
const button=(text,action,type='',glyph='',attrs='')=>`<button type="button" class="${type}" data-action="${action}" ${attrs}>${glyph?icon(glyph):''}${esc(text)}</button>`;
const iconButton=(name,action,glyph,attrs='')=>button('',action,'icon-button',glyph,`aria-label="${esc(name)}" title="${esc(name)}" ${attrs}`);
const help=(name,text)=>`<details class="inline-help"><summary aria-label="${esc(name)}" title="${esc(name)}">${icon('info')}</summary><p>${esc(text)}</p></details>`;
const newButton=()=>button('新建实验','new','primary','plus');
const pageTitle=(title,subtitle='',actions='',meta='')=>`<div class="page-title"><div><h1>${esc(title)}</h1>${subtitle?`<p>${esc(subtitle)}</p>`:''}${meta?`<div class="page-meta">${meta}</div>`:''}</div><div class="action-row">${actions}</div></div>`;
const metric=(name,value,foot='',glyph='')=>`<div class="metric"><div class="label">${glyph?icon(glyph):''}${esc(name)}</div><div class="number">${value}</div>${foot?`<div class="foot">${esc(foot)}</div>`:''}</div>`;
const sectionHead=(title,description='',actions='')=>`<div class="section-head"><div><h2>${esc(title)}</h2>${description?`<p>${esc(description)}</p>`:''}</div><div class="toolbar">${actions}</div></div>`;
const note=(text,type='')=>`<div class="notice inline ${type}">${icon(type==='error'||type==='warning'?'warning':'info')}<div>${esc(text)}</div></div>`;
function hasReviewOutput(result){
  // Match the evaluator's delivery contract, not a visible-text heuristic.
  // Empty structures, 0 and false remain output; a missing/null projection is unavailable.
  return result!==null&&typeof result==='object'&&Object.prototype.hasOwnProperty.call(result,'review_output')&&result.review_output!==null&&result.review_output!==undefined;
}
function scoreAvailability(trial){
  if(!trial||!terminal.has(trial.status))return {available:false,reason:'运行尚未结束，等待交付后再独立评分。'};
  if(trial.status!=='succeeded')return {available:false,reason:'本次运行未成功，评分作业会将它保留为不可用，不产生质量分；已记录的输出投影仍可复核。'};
  if(!hasReviewOutput(trial.result))return {available:false,reason:'未记录可供评分的输出投影，或投影为 null；当前评分契约将其保留为不可用。'};
  return {available:true,reason:''};
}
function scoreButton(trial,text,action='rescore',className='',attrs=''){
  const availability=scoreAvailability(trial);
  return availability.available?button(text,action,className,'score',attrs):button(terminal.has(trial?.status)?'评分不可用':'等待交付',action,className,'score',`disabled title="${esc(availability.reason)}" ${attrs}`);
}
function executionErrorNotice(value,status=''){
  const error=typeof value==='object'&&value!==null?value:{message:String(value)},details=error.details;
  const candidateMessages=[error.message,error.detail,typeof details==='string'?details:null,details?.message,typeof details?.error==='object'?details.error?.message:details?.error,details?.detail];
  const messages=[...new Set(candidateMessages.filter(v=>typeof v==='string'&&v.trim()).map(v=>v.trim()))];
  const reason=messages.join('\n')||'未提供简短错误原因，请展开完整错误详情或查看阶段轨迹。';
  const preview=reason.length>400?reason.slice(0,400)+'…':reason;
  const neutral=['cancelled','unsupported'].includes(status),brief=failureReason(error,status);
  return `<div class="notice ${neutral?'':'error'} inline execution-error">${icon(neutral?'info':'warning')}<div class="execution-error-body"><div class="execution-error-head"><strong>${esc(brief||error.code||'执行异常')}</strong>${error.http_status!==undefined?`<span class="tag">HTTP ${esc(error.http_status)}</span>`:''}${error.endpoint?`<span class="mono">${esc(error.endpoint)}</span>`:''}</div><p class="execution-error-reason">${esc(preview)}</p><details><summary>${reason.length>400?'展开完整错误原因与详情':'原始错误与错误码'}</summary>${json(error)}</details></div></div>`;
}
const empty=(title,description,action='',glyph='experiments',compact=false)=>`<div class="empty ${compact?'compact':''}"><div class="empty-mark">${icon(glyph)}</div><h2>${esc(title)}</h2>${description?`<p>${esc(description)}</p>`:''}${action}</div>`;
const unavailable=(title,error)=>`<div class="api-unavailable">${icon('info')}<div><h3>${esc(title)}</h3><p>此项能力暂未连接，已保留当前工作区内容。</p><small>${esc(error?.message||error||'接口尚未返回可用信息')}</small></div></div>`;
const tabs=(items,current)=>`<nav class="tabs" aria-label="页面分区">${items.map(([id,text,glyph,url,count])=>`<a href="${url}" class="${current===id?'active':''}" ${current===id?'aria-current="page"':''}>${icon(glyph)}${esc(text)}${count!==undefined?`<span class="tab-count">${typeof count==='number'?number(count):esc(count)}</span>`:''}</a>`).join('')}</nav>`;
const progress=e=>`<div class="progress-line"><progress value="${Number(e.completed_trials)||0}" max="${Math.max(1,Number(e.total_trials)||1)}" aria-label="完成 ${Number(e.completed_trials)||0} / ${Number(e.total_trials)||0}"></progress><span>${number(e.completed_trials??0)} / ${number(e.total_trials??0)}</span></div>`;
const sourceTypes=cases=>[...new Set((cases||[]).map(c=>sourceLabel(c)))].join(' · ')||'来源待核对';
const fingerprint=rows=>JSON.stringify(rows.map(e=>[e.id,e.status,e.completed_trials,e.total_trials,e.counts,e.updated_at]));
function toast(message){clearTimeout(state.toastTimer);$('toast').textContent=message;$('toast').hidden=false;state.toastTimer=setTimeout(()=>$('toast').hidden=true,5000);}
function fillIcons(){document.querySelectorAll('[data-icon]').forEach(el=>el.innerHTML=icon(el.dataset.icon));}
function setService(connected){$('service-state').className='top-status '+(connected?'connected':'offline');$('service-state').innerHTML=`<i></i>${connected?'本地服务已连接':'连接中断'}`;}
function modalHead(title,description=''){return `<div class="dialog-head"><div><h2 id="modal-title">${esc(title)}</h2>${description?`<p>${esc(description)}</p>`:''}</div>${button('','close','icon-button','close','aria-label="关闭对话框"')}</div>`;}
const focusable=root=>[...root.querySelectorAll('a[href],button:not(:disabled),input:not(:disabled):not([type=hidden]),select:not(:disabled),textarea:not(:disabled),summary,[tabindex="0"]')].filter(el=>el.getClientRects().length&&!el.closest('[inert]'));
function trapFocus(event,root){
  if(event.key!=='Tab')return;
  const items=focusable(root),first=items[0],last=items.at(-1);
  if(!first){event.preventDefault();root.focus();return;}
  if(event.shiftKey&&(document.activeElement===first||!root.contains(document.activeElement))){event.preventDefault();last.focus();}
  else if(!event.shiftKey&&(document.activeElement===last||!root.contains(document.activeElement))){event.preventDefault();first.focus();}
}
function openModal(html,wide=false,{focusId='',focusAction='',bodyScroll=0}={}){
  if(!modal.open){state.modalTrigger=document.activeElement;state.modalTriggerAction=state.modalTrigger?.dataset?.action;}
  const version=state.modalVersion=(state.modalVersion||0)+1;
  modal.classList.toggle('wide',wide);modal.classList.toggle('comparison-dialog',Boolean(state.compare)&&wide);modal.classList.remove('frame-reader-dialog');
  modal.innerHTML=html;associateFormLabels(modal);if(!modal.open)modal.showModal();modal.scrollTop=0;
  queueMicrotask(()=>{if(!modal.open||state.modalVersion!==version)return;const requested=focusId?document.getElementById(focusId):focusAction?modal.querySelector(`[data-action="${CSS.escape(focusAction)}"]`):null;const first=requested&&modal.contains(requested)&&!requested.matches(':disabled')&&requested.getClientRects().length?requested:modal.querySelector('#wizard-step-heading')||focusable(modal).find(el=>!el.matches('[data-action=close]'))||focusable(modal)[0];first?.focus({preventScroll:true});if(bodyScroll)modal.querySelector('.dialog-body')?.scrollTo(0,bodyScroll);});
}
function closeModal(){closeEvidenceImageViewer();state.modalVersion=(state.modalVersion||0)+1;modal.close();state.wizard=null;state.compare=null;state.importValue=null;}
function setNavigation(open,{restore=true}={}){
  const sidebar=$('sidebar'),mobile=window.matchMedia('(max-width:760px)').matches;
  open=Boolean(open&&mobile);sidebar.classList.toggle('is-open',open);$('menu-toggle').setAttribute('aria-expanded',String(open));
  $('nav-scrim').hidden=!open;document.querySelector('.shell').inert=open;document.querySelector('.skip-link').inert=open;
  if(open){sidebar.setAttribute('role','dialog');sidebar.setAttribute('aria-modal','true');sidebar.setAttribute('aria-label','工作区导航');requestAnimationFrame(()=>sidebar.querySelector('nav a[aria-current=page],nav a')?.focus());}
  else{sidebar.removeAttribute('role');sidebar.removeAttribute('aria-modal');sidebar.removeAttribute('aria-label');if(restore&&mobile)$('menu-toggle').focus();}
}
function importSource(value){
  const source=value?.origin?.source||value?.source||{},system=typeof source==='string'?source:source.system||source.name||source.description;
  const kind=sourceLabel(source);
  return system?String(system):(kind!=='来源未标注'?kind:(value?.origin?.kind==='historical_import'?'历史证据导入':'本工作区建立'));
}
const observedValue=value=>value===true?'是':value===false?'否':value===null||value===undefined||value==='unknown'?'未知':String(value);
const evidenceFacts=rows=>`<dl class="definition-list evidence-facts">${rows.map(([key,value])=>`<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join('')}</dl>`;
function failureReason(error,status=''){
  const code=typeof error==='object'?error?.code:error;
  return ({cancelled:'运行已停止',timed_out:'超过时间限制',unsupported:'此能力不支持',rating_unsupported:'当前入口不支持评分请求',request_failed_before_native_result:'请求失败，尚未取得原生结果',source_changed_since_freeze:'冻结后的源码已变化',stopped_before_dispatch:'调度前停止，未发送产品请求',model_failed:'评分模型未完成有效响应',invalid_judgment_evidence:'评分结构或引文未通过核验'})[code]||(['cancelled','unsupported','timed_out'].includes(status)?label(status):'');
}
function sourceSnapshotSummary(snapshot){
  if(!snapshot)return '';
  const files=Array.isArray(snapshot.files)?snapshot.files:[],captured=files.filter(f=>f.status==='captured'),missing=files.filter(f=>f.status!=='captured');
  const stateName=({captured:'已保存声明范围内的文件',partial:'部分文件已保存',unavailable:'无法保存源码文件'})[snapshot.status]||observedValue(snapshot.status);
  return `<div class="source-snapshot-brief"><h3>源码保存范围</h3>${evidenceFacts([['保存状态',stateName],['文件记录',`${captured.length} / ${files.length} 条已保存文件内容（按方案分别计数）`],['重建验证',snapshot.rebuild_status==='not_attempted'?'尚未尝试重建':observedValue(snapshot.rebuild_status)]])}${missing.length?`<p class="source-note">未保存内容：${[...new Set(missing.map(f=>f.source_file))].map(esc).join('、')}。${missing.every(f=>f.expected_sha256)?'这些记录保留了声明哈希；哈希不能替代文件内容。':'原始记录保留了缺失原因。'}</p>`:''}<p class="source-note">仅覆盖声明的源码与提示词；不包含完整依赖、模型权重、凭据或可运行环境，不能据此确认可在空白机器重建。</p></div>`;
}
function provenanceSummary(provenance={},parameters={}){
  const p=provenance||{},source=p.source||{},files=p.source_files_sha256||source.files||{},requested=p.requested||{},effective=p.effective||{};
  const rows=[['适配入口声明',p.available===true?'可用':p.available===false?'不可用':'未记录'],['声明源码哈希',Object.keys(files).length?`${Object.keys(files).length} 个文件；内容保存情况见实验来源`:'未记录文件清单'],['请求模型',requested.model||parameters.model||'未声明'],['实际模型',observedValue(effective.model)]];
  if(source.build_id)rows.push(['构建标识',source.build_id]);
  if(source.build_matches_sources_and_artifacts!==undefined)rows.push(['构建与声明核对',source.build_matches_sources_and_artifacts===true?'记录为匹配':source.build_matches_sources_and_artifacts===false?'记录为不匹配':'未知']);
  return evidenceFacts(rows);
}
function cancellationSummary(value){
  const c=value||{};
  return `<div class="panel panel-pad cancellation-brief"><h3>停止过程的实际观察</h3>${evidenceFacts([['请求原因',label(c.requested_reason)||'未记录'],['运行时确认停止请求',observedValue(c.cooperative_acknowledged)],['原生调用已返回',observedValue(c.native_call_returned)],['原生返回状态',observedValue(c.native_status)],['本地进程已退出',observedValue(c.local_process_exited)],['本地资源已释放',observedValue(c.resource_released)]])}<p class="source-note">运行时确认仅说明当前运行时收到了停止请求。远端计算是否停止、是否继续计费，不能由本地退出推断。</p><details><summary>原始停止字段</summary>${json(c)}</details></div>`;
}
function stageSummary(event){
  const d=event.data||{},parts=[];
  if(event.kind==='source.verified')parts.push(d.matches===true?'当前来源哈希与冻结记录一致':d.matches===false?'当前来源与冻结记录不一致':'来源匹配状态未知');
  if(d.source_type)parts.push('材料来源：'+sourceLabel({type:d.source_type}));
  if(d.status!==undefined)parts.push('状态：'+observedValue(label(d.status)));
  if(d.operation)parts.push('操作：'+label(d.operation));
  if(d.request_sent!==undefined)parts.push('请求已发送：'+observedValue(d.request_sent));
  if(d.http_status!==undefined)parts.push('HTTP '+d.http_status);
  if(d.exit_code!==undefined)parts.push('进程退出码 '+d.exit_code);
  if(d.serialization)parts.push('串行资源锁：'+d.serialization);
  if(d.profile_id)parts.push('执行配置：'+d.profile_id);
  if(d.native_acknowledged_cancel!==undefined)parts.push('原生运行时确认停止请求：'+observedValue(d.native_acknowledged_cancel));
  if(d.native_status!==undefined)parts.push('原生返回状态：'+observedValue(d.native_status));
  if(d.resource_released!==undefined)parts.push('本地资源释放：'+observedValue(d.resource_released));
  if(d.parsed_report_available!==undefined)parts.push('可用结构化报告：'+observedValue(d.parsed_report_available));
  if(d.error)parts.push('错误：'+(failureReason(d.error)||d.error.code||d.error.message||String(d.error)));
  return parts.length?`<p class="stage-brief">${parts.map(esc).join(' · ')}</p>`:'';
}
function scoringRuntimeSummary(unit){
  const runtime=unit.provider?.runtime||{},error=unit.error,requested=runtime.requested||{},effective=runtime.effective||{};
  if(!error&&!Object.keys(runtime).length)return '';
  const rows=[['请求是否已发送',observedValue(unit.request_sent)],['本地评分进程已启动',observedValue(runtime.process_started)],['供应商返回状态',observedValue(label(unit.provider?.status))],['模型轮次完成状态',observedValue(runtime.turn_completed)],['请求 / 实际模型',`${requested.model||'未声明'} / ${observedValue(effective.model)}`]];
  if(runtime.exit_code!==undefined)rows.push(['本地进程退出码',runtime.exit_code]);
  return `<section class="scoring-runtime-summary"><h4>${esc(failureReason(error)||'评分请求与响应')}${error?.code?` <span class="mono muted">${esc(error.code)}</span>`:''}</h4>${evidenceFacts(rows)}${error?.code==='model_failed'?'<p class="source-note">本单元评分失败；轮次是否完成以以上运行时字段为准。原始错误可能提供排查方向，具体供应商原因仍未确认。</p>':''}<details><summary>原始错误、请求与实际配置</summary>${json({error:unit.error,request_sent:unit.request_sent,dispatch_state:unit.dispatch_state,runtime})}</details></section>`;
}
function stageEmpty(t){
  const code=t.result?.error?.code||t.error?.code;
  if(code==='stopped_before_dispatch')return empty('已停止，未发送产品请求','此尝试在调度前结束，因此没有阶段事件。尝试编号保留，不表示已调用模型。','','stop',true);
  if(terminal.has(t.status))return empty(t.origin?.kind==='historical_import'?'历史记录未包含阶段事件':'本次已结束，未记录阶段事件','结果与尝试已保留；没有轨迹不能推断未调用产品，也不能还原缺失过程。','','file',true);
  if(t.status==='queued'||t.status==='pending')return empty('等待调度，尚无阶段事件','运行进入执行后才写入可观察事件；暂停的实验或队列需要明确恢复。','','clock',true);
  return empty('执行中，暂未收到阶段事件','这只表示尚无可观察轨迹，不代表产品尚未发送请求。','','activity',true);
}
function lastFailure(events=[]){
  for(const match of [event=>/stage[._](?:error|fail)/i.test(event.kind),event=>/error|fail|timeout|refus/i.test(event.kind),event=>event.data?.error]){
    for(let i=events.length-1;i>=0;i--){const event=events[i];if(match(event))return {event,index:i};}
  }
  return null;
}
function stageHeading(event){
  const kind=String(event.kind||''),suffix=kind.split('.').at(-1),eventNames={model_request:'发起模型阶段调用',model_response:'收到模型阶段响应',http_request:'发出产品请求',http_response:'收到产品响应',http_complete:'产品请求完成',environment_started:'准备隔离环境',state_snapshot:'保存状态快照',verified:'来源核验完成',delivered:'本轮交付完成',stage_result:'阶段结果',terminal:'写入执行终态',stage_request:'发起模型阶段调用',stage_response:'收到模型阶段响应',stage_error:'模型阶段异常',request:'发出产品请求',response:'收到产品响应',error:'执行异常',started:'开始执行',completed:'执行完成',finished:'执行结束',retry:'阶段内重试',cancelled:'执行停止',state:'隔离状态记录',cleanup:'清理隔离环境'};
  const stageNames={candidate:'候选生成',draft:'草稿生成',review:'复核',verifier:'验证',verify:'验证',rewrite:'改写',summary:'整理',chat:'对话',discover:'发现联系',planner:'规划'};
  const tags=[event.execution_attempt!=null?`执行尝试 ${event.execution_attempt}`:null,event.turn!=null?`对话轮次 ${event.turn}`:null,event.attempt!=null?`阶段内调用第 ${event.attempt} 次（含重试）`:null].filter(Boolean);
  const heading=[event.stage!=null?(stageNames[event.stage]||event.stage):null,({'pome.input':'读取本轮输入','pome.persisted':'保存产品记录','execution.dispatched':'调度执行','worker.started':'开始执行尝试','worker.finished':'写入执行结果','source.authorization_verified':'材料授权核验','source.verified':'核对冻结来源','native.queue':'等待原生资源','native.started':'启动原生调用','native.exited':'原生进程退出','native.result':'接收原生结果'})[kind]||eventNames[suffix]||kind||'事件名称未记录'].filter(Boolean).join(' · ');
  return `<h4>${esc(heading)}</h4><div class="event-tags">${tags.map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div>`;
}
async function frozenMaterials(ctx,e){
  const unique=[...new Map((e.trials||[]).map(t=>[t.case_id,t])).values()];
  const rows=await Promise.all(unique.map(async t=>({trial:t,...await optional(ctx,'/api/trials/'+encodeURIComponent(t.id))})));
  const dataset=e.dataset||{},cases=rows.filter(r=>r.value).map(r=>r.value.case),splits={};
  for(const c of cases)splits[c.split||'未分组']=(splits[c.split||'未分组']||0)+1;
  return `<section class="section">${sectionHead('本实验的冻结材料',`${countCases(e)} 个已选案例 · ${families(e)} 个家族；逐项来源来自保存的运行快照`)}<div class="panel panel-pad"><dl class="definition-list"><dt>数据集 / 版本</dt><dd>${esc(dataset.name||e.taskpack?.name||'历史轨迹材料')} · v${esc(dataset.version||e.taskpack?.version||'未记录')}</dd><dt>原版本声明规模</dt><dd>${dataset.case_count!=null?`${number(dataset.case_count)} 案例 · ${number(dataset.family_count)} 家族`:'未保存完整数据集规模'}</dd><dt>本实验所选分组</dt><dd>${Object.entries(splits).map(([key,n])=>`${esc(label(key))} ${n}`).join(' · ')||'无法读取'}</dd><dt>原版本内容哈希</dt><dd class="mono">${esc(dataset.content_hash||'未记录')}</dd><dt>导入 / 建立来源</dt><dd>${esc(importSource(e))}${e.origin?.source_experiment_id?`<div class="subline mono">源实验 ${esc(e.origin.source_experiment_id)}</div>`:''}</dd></dl>${dataset.case_count!=null&&dataset.case_count!==countCases(e)?note(`原数据集声明包含 ${dataset.case_count} 个案例，本实验仅保存所选 ${countCases(e)} 个案例。未选材料不因历史导入自动加入本地数据集目录。`):''}${sourceSnapshotSummary(e.source_snapshot)}${e.source_snapshot==null?note(e.origin?.kind==='historical_import'?'本次历史导入未包含独立的 source_snapshot；已保存案例、配置和结果中的来源字段，不能据此补齐缺失的源码快照。':'未记录独立的 source_snapshot；可逐项核对案例和配置内已保存的来源字段。'):''}<details><summary>原版本声明、来源快照与预算</summary>${json({dataset:e.dataset,source_snapshot:e.source_snapshot,budget:e.budget,origin:e.origin})}</details></div><div class="panel frozen-materials mt-18">${rows.map(({trial,value,error})=>{const c=value?.case;if(!c)return `<div class="panel-pad"><strong>${esc(trial.case_name||trial.case_id)}</strong>${note('快照暂时无法读取：'+(error?.message||'未知原因'),'error')}</div>`;const source=c.source||{},url=safeWebUrl(source.url);return `<details class="frozen-case"><summary><span><strong>${esc(c.name||c.id)}</strong><small>${esc(c.family_id||c.id)} · 案例 v${esc(c.version||'未记录')}</small></span><span class="tag">${esc(label(c.split||'未分组'))}</span><span class="tag">${esc(sourceLabel(c))}</span></summary><div class="frozen-case-body"><dl class="definition-list"><dt>案例哈希</dt><dd class="mono">${esc(value.case_hash||'未记录')}</dd><dt>来源与许可</dt><dd>${esc(source.description||source.evidence||sourceLabel(c))}${licenseText(source)?` · ${esc(licenseText(source))}`:''}${attributionText(source)?` · ${esc(attributionText(source))}`:''}${url?`<br><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">打开来源记录 ${icon('external')}</a>`:''}</dd></dl><a class="button small" href="${routeUrl('trial',trial.id,'output')}">查看冻结输入与实际输出 ${icon('arrow')}</a><details><summary>完整案例快照</summary>${json(c)}</details></div></details>`;}).join('')}</div></section>`;
}
async function optional(ctx,url){try{return {value:await ctx.get(url)};}catch(error){if(error.name==='AbortError')throw error;return {error};}}
function safeWebUrl(value){try{const u=new URL(value);return ['https:','http:'].includes(u.protocol)?u.href:null;}catch{return null;}}
function readableInput(input){if(!input||typeof input!=='object'||Array.isArray(input))return input;return Object.fromEntries(Object.entries(input).filter(([key])=>!['media','image_path','image_sha256'].includes(key)));}
function mediaEvidence(caseValue,trialId,declaredMedia=null){
  const input=caseValue?.input||{},media=Array.isArray(declaredMedia)?declaredMedia:Array.isArray(input.media)?input.media:Array.isArray(caseValue?.media)?caseValue.media:input.image_path?[{sha256:input.image_sha256,source:caseValue.source}]:[];
  if(!media.length)return '';
  return `<div class="media-evidence">${media.map((m,i)=>{const source=m.source||caseValue.source||{},link=safeWebUrl(source.url),licenseUrl=safeWebUrl(typeof source.license==='object'?source.license?.url:source.license_url),url=m.display_url||m.media_url;const endpoint=(m.url&&m.url.startsWith('/api/trials/')?m.url:null)||(url&&/^\/api\//.test(url)?url:trialId?`/api/trials/${encodeURIComponent(trialId)}/media/${i}`:null);return `<figure>${endpoint?`<div class="media-loading" data-media-url="${esc(endpoint)}" data-media-alt="${esc(input.intent||caseValue.name||'本次评测原图')}"><div class="empty compact">${icon('file')}<p>正在读取本次评测图像…</p></div></div>`:`<div class="empty compact"><p>原图将在实际运行的受控证据入口中读取。</p></div>`}<figcaption>${esc(sourceLabel(source))}${licenseText(source)?` · ${esc(licenseText(source))}`:''}${attributionText(source)?` · ${esc(attributionText(source))}`:''}${licenseUrl?` · <a href="${esc(licenseUrl)}" target="_blank" rel="noopener noreferrer">许可条款</a>`:''}${link?` · <a href="${esc(link)}" target="_blank" rel="noopener noreferrer">查看许可来源 ${icon('external')}</a>`:''}${m.sha256?`<details class="media-identity"><summary>原图 SHA-256</summary><span class="mono">${esc(m.sha256)}</span></details>`:''}</figcaption></figure>`;}).join('')}</div>`;
}
const objectUrls=new Set();
function releaseMedia(){closeEvidenceImageViewer();for(const url of objectUrls)URL.revokeObjectURL(url);objectUrls.clear();}
async function hydrateMedia(root=app){for(const el of root.querySelectorAll('[data-media-url]')){const url=el.dataset.mediaUrl;delete el.dataset.mediaUrl;try{const response=await fetch(url,{headers:requestHeaders(url)});if(!response.ok)throw new Error(`图像接口 HTTP ${response.status}`);if(!(response.headers.get('content-type')||'').startsWith('image/'))throw new Error('证据接口没有返回图像');const blob=await response.blob();if(!el.isConnected)continue;const src=URL.createObjectURL(blob);objectUrls.add(src);const img=document.createElement('img');img.src=src;img.alt=el.dataset.mediaAlt||'评测图像';el.replaceChildren(img);makeEvidenceImageZoomable(img);}catch(error){if(el.isConnected)el.innerHTML=`<div class="empty compact"><p>图像暂时无法读取</p><small>${esc(error.message)}；文字与来源证据仍可查看。</small></div>`;}}}

function experimentTable(rows,{compact=false}={}){
  if(!rows.length)return empty('还没有实验记录','',newButton());
  return `<div class="table-wrap"><table class="experiment-table"><thead><tr><th>实验名称</th><th>项目</th><th>状态</th><th>进度</th>${compact?'':'<th>规模</th>'}<th>建立时间</th><th aria-label="打开"></th></tr></thead><tbody>${rows.map(e=>`<tr class="experiment-row" data-project="${esc(e.project)}" data-status="${esc(experimentStatus(e))}" data-issues="${issues(e)>0||e.status==='completed_with_issues'}" data-search="${esc((e.title+' '+e.id+' '+importSource(e)+' '+(e.taskpack?.name||'')+' '+label(e.project)).toLowerCase())}"><td><div class="row-icon-title"><span class="mini-symbol">${icon('experiments')}</span><div><a class="cell-title" href="${routeUrl('experiment',e.id)}">${esc(e.title)}</a><div class="subline">${e.origin?.kind==='historical_import'?'历史轨迹':'本机实验'}${e.taskpack?.version?' · 材料 v'+esc(e.taskpack.version):''} · ${countCases(e)} 案例 · ${families(e)} 家族</div></div></div></td><td data-label="项目"><span class="tag">${esc(label(e.project))}</span></td><td data-label="状态">${experimentBadge(e)}</td><td data-label="进度">${progress(e)}</td>${compact?'':`<td data-label="规模">${number(countCases(e))} 案例 · ${configurationCount(e)} 方案<div class="subline">${number(families(e))} 家族 × ${number(e.repeats||1)} 次</div></td>`}<td class="muted numeric" data-label="建立时间">${date(e.created_at)}</td><td><a class="row-arrow" href="${routeUrl('experiment',e.id)}" aria-label="打开 ${esc(e.title)}">${icon('chevron')}</a></td></tr>`).join('')}</tbody></table></div><div class="table-filter-empty" hidden>${empty('没有匹配的实验','',button('清除筛选','clear-filters','small'), 'search',true)}</div>`;
}
function projectMark(project) {
  const marks = {
    pome: '<path d="M5 17V8q4-2 8 1 4-3 8-1v9q-4-2-8 1-4-3-8-1Zm8-8v9"/>',
    sidelit: '<path d="m5 18 8-13 8 13H5Zm5-3 4-6m0 6 2-3"/>',
    sijian: '<path d="M5 7v13h15V6H11M6 4l3 3m1 4h6v5h-6z"/>'
  };
  return `<span class="project-mark" aria-hidden="true"><svg viewBox="0 0 26 26" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round">${marks[project] || '<path d="m13 4 9 9-9 9-9-9z"/>'}</svg></span>`;
}

function recentExperiments(rows) {
  if (!rows.length) return empty('还没有实验','',newButton());
  return rows.slice(0,6).map(e=>`<a class="recent-run" href="${routeUrl('experiment',e.id)}">
    ${projectMark(e.project)}<div class="recent-title"><strong>${esc(e.title)}</strong>
    <small>${esc(label(e.project))} · ${countCases(e)} 案例 · ${configurationCount(e)} 方案</small></div>
    <div class="recent-state"><span class="home-state ${issues(e) || experimentStatus(e)==='completed_with_issues'?'with-issues':''}">${esc(({completed:'已完成',completed_with_issues:'有异常'})[experimentStatus(e)]||label(experimentStatus(e)))}</span><small>${date(e.created_at)}</small></div>${icon('chevron')}</a>`).join('');
}

// Home project links also work when opened directly or in another tab.
function readHomeProjectLink() {
  const [,mode,project]=location.hash.split('/');
  if (location.hash.startsWith('#experiments/') && mode==='project' && project) {
    try { filters.project=decodeURIComponent(project); } catch { return; }
    filters.search=filters.status='';
  }
}
window.addEventListener('hashchange',readHomeProjectLink);
readHomeProjectLink();

async function overviewPage(ctx) {
  const {experiments:rows=[]}=await ctx.get('/api/experiments');
  ctx.experiments=rows;
  const projects=[...new Set([...state.catalog.map(p=>p.project),...rows.map(e=>e.project)])];
  const abnormal=rows.filter(e=>issues(e)>0 || e.status==='completed_with_issues');
  const total=sum(rows,e=>e.total_trials), success=sum(rows,e=>statusCounts(e).succeeded), failed=sum(rows,issues);
  const groups=projects.map(project=>{
    const experiments=rows.filter(e=>e.project===project);
    return {project, experiments, total:sum(experiments,e=>e.total_trials), success:sum(experiments,e=>statusCounts(e).succeeded), failed:sum(experiments,issues), packs:state.catalog.filter(p=>p.project===project).length};
  }).sort((a,b)=>b.total-a.total);
  const scale=Math.max(1,...groups.map(g=>g.total));
  const chart=groups.map(g=>{
    const remaining=g.total-g.success-g.failed;
    return `<a class="meter-row" href="${routeUrl('experiments','project',g.project)}" aria-label="${esc(label(g.project))}，成功 ${g.success}，异常终态 ${g.failed}，未结束 ${remaining}，查看实验">
      <span>${esc(label(g.project))}</span><span class="meter-track" aria-hidden="true"><svg viewBox="0 0 ${scale} 10" preserveAspectRatio="none"><rect width="${g.success}" height="10"/><rect class="meter-failed" x="${g.success}" width="${g.failed}" height="10"/><rect class="meter-open" x="${g.success+g.failed}" width="${remaining}" height="10"/></svg></span>
      <b>${number(g.total)}</b><b class="meter-issue">${number(g.failed)}</b></a>`;
  }).join('');
  const projectList=groups.map(g=>`<div class="agent-item">${projectMark(g.project)}<a class="home-project" href="${routeUrl('experiments','project',g.project)}"><strong>${esc(label(g.project))}</strong><small>${g.packs} 个任务包 · ${g.experiments.length} 个实验</small></a>${g.packs?iconButton(`为 ${label(g.project)} 建立比较`,'new','plus',`data-project="${esc(g.project)}"`):''}</div>`).join('');
  return `<div class="workspace-home">
    <header class="desk-heading"><div><h1>评测工作台</h1><p>${number(projects.length)} 个接入项目 <span>/</span> ${number(rows.length)} 个实验</p></div>${newButton()}</header>
    <section class="home-instrument" aria-label="运行分布"><div class="meter-reading"><h2>${icon('activity')} 累计运行</h2><strong>${number(total)}<small>项</small></strong><div class="meter-key"><span>成功 <b>${number(success)}</b></span><span>异常终态 <b>${number(failed)}</b></span><span>未结束 <b>${number(total-success-failed)}</b></span></div></div>
    <div class="meter-plot"><div class="meter-labels"><h2>各项目运行分布</h2><span>总数</span><span>异常</span></div>${chart||'<p>尚无项目记录</p>'}</div></section>
    <div class="desk-columns"><section class="desk-section home-register"><div class="desk-section-title"><h2>近期实验</h2><a href="#experiments" data-action="all-experiments" class="icon-button" aria-label="全部实验" title="全部实验">${icon('arrow')}</a></div><div class="desk-list">${recentExperiments(rows)}</div></section>
    <div class="home-side"><section class="home-alerts"><div class="desk-section-title"><h2>${icon('warning')} 异常实验 <b>${number(abnormal.length)}</b></h2><a href="#experiments/issues" data-action="abnormal-experiments" class="icon-button" aria-label="全部异常" title="全部异常">${icon('arrow')}</a></div>
    ${abnormal.slice(0,3).map(e=>`<a class="issue-jump" href="${routeUrl('experiment',e.id,'issues')}"><strong>${esc(e.title)}</strong><small>${issues(e)} 项</small></a>`).join('')||'<p>当前没有异常实验。</p>'}</section>
    <section class="desk-section"><div class="desk-section-title"><h2>接入项目</h2><a class="icon-button" href="#datasets/packs" aria-label="查看任务包" title="查看任务包">${icon('arrow')}</a></div><div class="desk-list">${projectList||'<p>尚无接入项目</p>'}</div><a class="review-shortcut" href="#jobs/review">${icon('score')} 复核模型评分 ${icon('arrow')}</a></section></div></div></div>`;
}
async function experimentsPage(ctx,mode=''){
  if(mode==='issues')filters.status='issues';
  const {experiments=[]}=await ctx.get('/api/experiments');ctx.experiments=experiments;
  const projects=[...new Set([...state.catalog.map(p=>p.project),...experiments.map(e=>e.project)])];
  return pageTitle('实验','',button('导入历史轨迹','import-traces','','upload')+newButton())+tabs([['experiments','实验记录','experiments','#experiments'],['campaigns','实验批次','layers','#campaigns']],'experiments')+
    `<div class="panel"><div class="filters"><div class="search-field">${icon('search')}<input id="experiment-search" type="search" placeholder="搜索实验、任务包或项目" value="${esc(filters.search)}" aria-label="搜索实验"><kbd>/</kbd></div><span class="spacer"></span><select id="project-filter" class="filter-select" aria-label="筛选项目">${opt('','全部项目',!filters.project)}${projects.map(p=>opt(p,label(p),p===filters.project)).join('')}</select><select id="experiment-status-filter" class="filter-select" aria-label="筛选实验状态">${opt('','全部状态',!filters.status)}${opt('issues','存在异常运行',filters.status==='issues')}${[...new Set(experiments.map(experimentStatus))].map(s=>opt(s,label(s),s===filters.status)).join('')}</select></div>${experimentTable(experiments)}<div class="table-foot"><span id="visible-count">${experiments.length} 个实验</span></div></div>${help('运行计数口径','实验按案例 × 方案 × 重复生成运行。重试属于同一运行的新尝试，原失败与旧评价保留。')}`;
}
function executionChart(rows,e,baseline) {
  const latency=rows.map(row=>row.mean_duration_ms).filter(v=>Number.isFinite(v)&&v>=0);
  const peak=Math.max(0,...latency)/1000;
  const step=10**Math.floor(Math.log10(peak||1));
  const limit=Math.max(step,Math.ceil(peak/step)*step);
  const rail=(amount,ceiling,caption)=>`<span class="plot-reading">${Number.isFinite(amount)?`<svg viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true"><rect width="${Math.max(0,Math.min(100,amount/ceiling*100))}" height="8"/></svg>`:''}<strong>${esc(caption)}</strong></span>`;
  const series=rows.map(row=>{
    const success=row.statuses?row.statuses.succeeded??0:null;
    const rate=Number.isFinite(success)&&row.trials>0?success/row.trials*100:null;
    const seconds=Number.isFinite(row.mean_duration_ms)?row.mean_duration_ms/1000:null;
    const isBase=row.id===baseline;
    return `<div class="bench-rank ${isBase?'is-base':''}"><div class="bench-identity"><small>${isBase?'基线':'候选'}</small><strong>${esc(row.name||row.id)}</strong></div>
      <div class="series-latency">${rail(seconds,limit,seconds===null?'未记录':duration(row.mean_duration_ms))}</div>
      <div class="series-success">${rail(rate,100,rate===null?'未知':rate.toFixed(1)+'%')}</div>
      <div class="bench-action">${!isBase&&e.trials?.length?button('对照基线','compare-output','config-compare small','compare',`data-candidate="${esc(row.id)}" aria-label="对照基线与${esc(row.name||row.id)}"`):''}</div></div>`;
  }).join('');
  return `<div class="bench-chart"><div class="bench-head"><h3>执行表现</h3><fieldset class="measure-switch"><legend class="sr-only">比较指标</legend><label><input type="radio" name="compare-measure" value="latency" checked>平均耗时</label><label><input type="radio" name="compare-measure" value="success">执行成功率</label></fieldset></div>
    <div class="bench-axis"><span>方案</span><span class="series-latency">0 秒 <b>${number(limit)} 秒</b></span><span class="series-success">0% <b>100%</b></span></div>${series}</div>`;
}

function intervalPlot(group,dims) {
  const valid=group.map(d=>d.effect?.family_equal_weight_mean_delta).filter(Number.isFinite);
  if(!valid.length)return '';
  const limits=group.flatMap(d=>[d.effect?.ci95?.low,d.effect?.ci95?.high]).filter(Number.isFinite);
  const extent=Math.max(0.25,...[...valid,...limits].map(Math.abs));
  const scale=Math.ceil(extent*4)/4;
  const position=n=>50+50*n/scale;
  const fmt=n=>Number.isFinite(n)?(n>0?'+':'')+n.toFixed(2):'—';
  const lines=group.map(d=>{
    const point=d.effect?.family_equal_weight_mean_delta,ci=d.effect?.ci95;
    const hasCI=Number.isFinite(ci?.low)&&Number.isFinite(ci?.high);
    return `<div class="interval-row"><span>${esc(dims[d.dimension]||label(d.dimension))}</span><div class="interval-track">
      <svg width="100%" height="14" aria-hidden="true" focusable="false">
        ${hasCI?`<line x1="${position(ci.low)}%" x2="${position(ci.high)}%" y1="7" y2="7"/><line x1="${position(ci.low)}%" x2="${position(ci.low)}%" y1="3" y2="11"/><line x1="${position(ci.high)}%" x2="${position(ci.high)}%" y1="3" y2="11"/>`:''}
        ${Number.isFinite(point)?`<circle cx="${position(point)}%" cy="7" r="4"/>`:''}</svg></div>
      <strong>${fmt(point)}</strong><small>${hasCI?`${fmt(ci.low)} ～ ${fmt(ci.high)}`:'区间无法估计'}</small></div>`;
  }).join('');
  return `<figure class="interval-plot"><figcaption>候选相对基线的变化 <span><svg aria-hidden="true" width="12" height="10" viewBox="0 0 12 10"><circle cx="6" cy="5" r="3" fill="currentColor"/></svg> 均值差　<svg aria-hidden="true" width="20" height="10" viewBox="0 0 20 10"><path d="M2 5h16M2 2v6m16-6v6" fill="none" stroke="currentColor" stroke-width="1.5"/></svg> 95% 区间</span></figcaption>
    <div class="interval-head"><span>质量维度</span><span class="interval-scale"><b>−${scale}</b><b>0</b><b>+${scale}</b></span><span>变化</span><span>95% 区间</span></div>${lines}</figure>`;
}

function comparisonRows(e){
  const baseline=e.baseline_id||e.configurations?.[0]?.id;
  const rows=Object.entries(e.comparison?.configurations||{}).map(([id,info])=>({...info,id})).sort((a,b)=>(b.id===baseline)-(a.id===baseline));
  const humanCount=sum(Object.values(e.comparison?.reviews?.human||{}),v=>v);
  if(!rows.length)return empty('比较数据尚未生成','','','compare',true);
  const rowMarkup=rows.map(c=>{
    const otherStates=Object.entries(c.statuses||{}).filter(([k,v])=>k!=='succeeded'&&v>0).map(([k,v])=>`${esc(label(k))} ${number(v)}`).join(' · ');
    const success=c.statuses?c.statuses.succeeded??0:null;
    const role=c.id===baseline?'基线':'候选';
    const rules=Object.entries(c.rules||{}).map(([k,v])=>`<span>${badge(k)} ${number(v)}</span>`).join('');
    const cost=c.unknown_cost_trials===0&&c.known_cost_total!=null?esc(c.known_cost_total):'未知';
    return `<tr><td class="configuration-name"><strong>${esc(c.name||c.id)}</strong><div class="config-row-actions"><span class="tag ${c.id===baseline?'baseline':''}">${role}</span>
      ${c.id!==baseline&&e.trials?.length?button('对照基线','compare-output','quiet small config-compare','compare',`data-candidate="${esc(c.id)}" aria-label="对照基线与${esc(c.name||c.id)}"`):''}</div></td>
      <td data-label="执行成功 / 全部"><span class="num">${number(success)} <span class="muted">/ ${number(c.trials)}</span></span>
      ${c.trials>0&&success!==null?`<progress class="config-completion" value="${success}" max="${c.trials}" aria-label="执行成功 ${success} / ${c.trials}"></progress>`:''}
      ${otherStates?`<div class="subline">${otherStates}</div>`:''}</td>
      <td data-label="平均耗时"><span class="num">${duration(c.mean_duration_ms)}</span></td>
      <td data-label="自动规则"><div class="rule-counts">${rules||'未运行'}</div></td>
      <td data-label="人工胜出"><span class="num">${humanCount?number(c.human_wins):'待评'}</span></td>
      <td data-label="费用合计">${cost}${c.unknown_cost_trials?`<div class="subline">${number(c.unknown_cost_trials)} 项缺少计价</div>`:''}</td></tr>`;
  }).join('');
  return `${executionChart(rows,e,baseline)}<details class="bench-values"><summary>完整指标与规则计数</summary><div class="panel table-wrap"><table class="comparison-table"><thead><tr><th>方案</th><th>执行成功 / 全部</th><th>平均耗时</th><th>自动规则</th><th>人工胜出</th><th>费用合计</th></tr></thead><tbody>${rowMarkup}</tbody></table></div></details>`;
}
const evaluationGroupName=row=>String(row.evaluator||'未注明评分器').split('@')[0]+(String(row.evaluator||'').includes('+rv1@')?' · 原判断重验证':' · 模型评分记录');
function evaluationIdentity(row,scope='',index=0){
  const matched=matchingRubric(row.rubric_version,undefined,scope),preset=evaluationGroupName(row);
  return `<div class="evaluation-group-head"><h4>${index?`证据组 ${index} · `:''}${esc(preset)}</h4><span>${esc(matched?.rubric?.name||'冻结量表')} · 分维度判断</span><details><summary>完整身份与版本</summary><dl class="definition-list"><dt>评分器 / 验证器标识</dt><dd class="mono">${esc(row.evaluator)}</dd><dt>量表标识</dt><dd class="mono">${esc(row.rubric_version)}</dd><dt>完整量表哈希</dt><dd class="mono">${esc(matched?.hash||'未取得唯一完整哈希，不推断量表内容')}</dd></dl></details></div>`;
}
function scoreDistribution(row){
  if(!row)return '<span class="muted">未评分</span>';
  return `<div class="distribution-counts">${Object.entries(row.distribution||{}).sort(([a],[b])=>Number(b)-Number(a)).map(([score,n])=>`<span>${esc(score)} 分 <strong>${n}</strong></span>`).join('')||'<span>无有效分值</span>'}</div><div class="distribution-total">${row.rated??sum(Object.values(row.distribution||{}),v=>v)} 条已评分记录</div><div class="subline">不适用 ${row.not_applicable??0} · 无法评估 ${row.unavailable??0} · 未评分 ${row.unrated??0}</div>`;
}
function modelComparison(rows,configurations,rubric,scope=''){
  if(!rows?.length)return empty('尚未取得独立模型评价','',button('创建评分作业','new-job','small','score'),'score',true);
  const names=Object.fromEntries((rubric||[]).map(r=>[r.id,r.name])),groups={};for(const row of rows)(groups[JSON.stringify([row.evaluator,row.rubric_version])]??=[]).push(row);
  return Object.values(groups).map((group,index)=>{const ids=[...new Set(group.map(r=>r.configuration_id))],dims=[...new Set(group.map(r=>r.dimension))],example=group.find(r=>r.records>r.covered_trials)||group[0];return `<details class="section evaluation-distribution evaluation-group" ${index===0?'open':''}><summary><strong>证据组 ${index+1} · ${esc(evaluationGroupName(group[0]))}</strong><span>${dims.length} 个维度 · ${ids.length} 个方案</span></summary>${evaluationIdentity(group[0],scope,index+1)}<div class="panel table-wrap"><table class="independent-scores"><thead><tr><th>方案</th><th>评价覆盖的输出</th>${dims.map(d=>`<th>${esc(names[d]||label(d))}${rubricHelp(group[0].rubric_version,d,undefined,scope,true)}</th>`).join('')}</tr></thead><tbody>${ids.map(id=>{const values=group.filter(r=>r.configuration_id===id),first=values[0],recordCounts=[...new Set(values.map(r=>r.records))],recordNote=recordCounts.length===1&&recordCounts[0]!=null?`每维 ${recordCounts[0]} 条评价记录`:'各维记录数见对应分布';return `<tr><td><strong>${esc(configurations[id]?.name||id)}</strong></td><td data-label="评价覆盖的输出"><strong>${first.covered_trials} / ${first.total_trials}</strong><div class="subline">${recordNote}<br>${first.unreviewed_trials} 个输出无此组评价</div></td>${dims.map(d=>`<td data-label="${esc(names[d]||label(d))}">${rubricHelp(group[0].rubric_version,d,undefined,scope,true)}${scoreDistribution(values.find(v=>v.dimension===d))}</td>`).join('')}</tr>`;}).join('')}</tbody></table></div><p class="source-note">${esc(configurations[example.configuration_id]?.name||example.configuration_id)} · ${esc(names[example.dimension]||label(example.dimension))} · ${example.covered_trials} 个输出 · ${example.records??'未知'} 条评价</p>${button('逐例核对评价关系','compare-output','quiet small','compare')}</details>`;}).join('')+`${help('评分分布口径','输出按运行去重，分布按当前结果绑定的评价记录计数。同一输出可在多个配对中获得评价。确定性重验证不增加独立评阅者或样本；评分器与验证器按完整标识成组。缺失不记零，不跨评阅者平均。重复与评价票数不增加独立情境家族。')}`;
}
function analysisDimensionsTable(dimensions,dims,e){
  const groups={};for(const d of dimensions)(groups[JSON.stringify([d.evaluator,d.rubric_version])]??=[]).push(d);
  const format=v=>typeof v==='number'&&Number.isFinite(v)?v.toFixed(2):'—',signed=v=>typeof v==='number'&&Number.isFinite(v)?(v>0?'+':'')+v.toFixed(2):'—';
  const coverage=d=>`${d.effect?.scored_families??0} 个已评家族 · ${d.paired_rated??0} / ${d.expected_pairs??0} 个有效配对`,excluded=d=>Object.entries(d.excluded_pairs||{}).map(([k,n])=>`${label(k)} ${n}`).join(' · ');
  return Object.values(groups).map((group,index)=>{const sameCoverage=new Set(group.map(d=>JSON.stringify([d.effect?.scored_families,d.paired_rated,d.expected_pairs,d.excluded_pairs]))).size===1;
    return `<details class="analysis-evaluation evaluation-group" ${index===0?'open':''}><summary><strong>证据组 ${index+1} · ${esc(evaluationGroupName(group[0]))}</strong><span>${group.length} 个维度</span></summary>${evaluationIdentity(group[0],e.id,index+1)}${sameCoverage?`<p class="coverage-summary">各维覆盖：${esc(coverage(group[0]))}${excluded(group[0])?' · 排除 '+esc(excluded(group[0])):''}</p>`:''}${intervalPlot(group,dims)}<details class="bench-values"><summary>逐维均值、量表与覆盖</summary><div class="panel table-wrap"><table class="analysis-table"><thead><tr><th>质量维度</th><th>基线均值</th><th>候选均值</th><th>变化</th><th>95% 区间</th>${sameCoverage?'':'<th>有效覆盖</th>'}</tr></thead><tbody>${group.map(d=>{const effect=d.effect||{},ci=effect.ci95,delta=effect.family_equal_weight_mean_delta;return `<tr><td><strong>${esc(dims[d.dimension]||label(d.dimension))}</strong>${rubricHelp(d.rubric_version,d.dimension,undefined,e.id,true)}</td><td class="numeric" data-label="基线均值">${format(effect.baseline_family_equal_weight_mean)}</td><td class="numeric" data-label="候选均值">${format(effect.candidate_family_equal_weight_mean)}</td><td data-label="变化 · 候选减基线" class="numeric ${delta>0?'delta-positive':delta<0?'delta-negative':''}"><strong>${signed(delta)}</strong></td><td data-label="95% 区间" class="numeric">${ci?`${signed(ci.low)} ～ ${signed(ci.high)}`:'无法估计'}</td>${sameCoverage?'':`<td data-label="有效覆盖">${esc(coverage(d))}${excluded(d)?`<div class="subline">排除 ${esc(excluded(d))}</div>`:''}</td>`}</tr>`;}).join('')}</tbody></table></div></details>${help('区间计算方法','区间按家族级 bootstrap 计算；至少需要 2 个已评家族。不同维度的缺失与排除仍分别保留。')}</details>`;
  }).join('');
}
function runTable(e, onlyIssues=false){
  const all=e.trials||[], abnormal=all.filter(t=>terminal.has(t.status)&&t.status!=='succeeded');
  const rows=onlyIssues?abnormal:all;
  const cases=[...new Map(rows.map(t=>[t.case_id,t.case_name])).entries()];
  const statuses=[...new Set(rows.map(t=>t.status))];
  const filterKey='myeval.runFilters.'+e.id+(onlyIssues?'.issues':'.runs');
  let saved={};
  try { saved=JSON.parse(sessionStorage.getItem(filterKey))||{}; } catch {}
  filters.case=cases.some(([id])=>id===saved.case)?saved.case:'';
  filters.trialStatus=statuses.includes(saved.status)?saved.status:'';
  const configs=new Map((e.configurations||[]).map(c=>[c.id,c.name]));
  const baseline=e.baseline_id||e.configurations?.[0]?.id;
  const rowMarkup=rows.map(t=>{
    const hasIssue=terminal.has(t.status)&&t.status!=='succeeded';
    const config=configs.get(t.configuration_id)||t.configuration_id;
    const reason=failureReason(t.error,t.status)||t.error?.message||t.error?.code||(typeof t.error==='string'?t.error:'');
    const href=routeUrl('trial',t.id,hasIssue?'evidence':'output')+(onlyIssues?'/from-issues':'');
    const terms=[t.case_name,t.case_id,t.id,config,t.configuration_id,t.error?.code,t.error?.message,reason].filter(Boolean).join(' ').toLowerCase();
    return `<tr class="trial-row" data-case="${esc(t.case_id)}" data-status="${esc(t.status)}" data-search="${esc(terms)}">
      <td><a class="cell-title" href="${href}">${esc(t.case_name||t.case_id)}</a><div class="subline mono run-identity">${esc(t.case_id)} · ${esc(t.id.slice(0,8))}</div>${reason?`<div class="run-reason">${esc(reason)}</div>`:''}</td>
      <td data-label="方案">${esc(config)}<div class="subline">${t.configuration_id===baseline?'基线':'候选'}</div></td>
      <td class="numeric" data-label="重复 / 尝试">${number(t.repeat_index)} / ${number(t.attempt)}</td>
      <td data-label="执行状态">${badge(t.status)}</td><td class="numeric" data-label="耗时">${duration(t.duration_ms)}</td>
      <td class="run-open"><a class="text-link" href="${href}">${hasIssue?'查阶段证据':'看交付结果'} ${icon('chevron')}</a></td>
    </tr>`;
  }).join('');
  const scopeLink=(tab,text,n,active)=>`<a href="${routeUrl('experiment',e.id,tab)}"${active?' aria-current="page"':''}>${text}<span>${number(Array.isArray(e.trials)?n:null)}</span></a>`;
  return `<section class="result-run-list" data-filter-key="${esc(filterKey)}"><nav class="run-scopes" aria-label="运行范围">${scopeLink('runs','全部运行',all.length,!onlyIssues)}${scopeLink('issues','异常终态',abnormal.length,onlyIssues)}</nav>
    <div class="panel"><div class="filters"><div class="search-field">${icon('search')}<input id="trial-search" type="search" value="${esc(saved.search||'')}" placeholder="搜索案例、方案或错误" aria-label="搜索运行"></div><span class="spacer"></span>
    <select id="case-filter" class="filter-select" aria-label="筛选案例">${opt('','全部案例')}${cases.map(([id,name])=>opt(id,name,id===filters.case)).join('')}</select>
    <select id="trial-status-filter" class="filter-select" aria-label="筛选运行状态">${opt('','全部状态')}${statuses.map(s=>opt(s,label(s),s===filters.trialStatus)).join('')}</select></div>
    ${rows.length?`<div class="table-wrap"><table class="result-runs"><thead><tr><th>案例</th><th>方案</th><th>重复 / 尝试</th><th>执行状态</th><th>耗时</th><th>证据</th></tr></thead><tbody>${rowMarkup}</tbody></table></div>`:empty(onlyIssues?'没有异常终态':'尚无运行记录',onlyIssues?'本实验当前没有以异常状态结束的运行。':'调度后，运行记录会显示在这里。','','activity',true)}
    <div class="table-filter-empty" hidden>${empty('没有匹配的运行','','','search',true)}</div>
    <div class="table-foot"><span id="visible-trial-count">${rows.length} 项运行</span></div></div></section>`;
}
async function experimentPage(ctx,id,tab='compare'){
  const e=await ctx.get('/api/experiments/'+encodeURIComponent(id));ctx.current=e;
  const currentTab=tab==='issues'?'runs':['compare','runs','scoring','reviews','sources','decisions'].includes(tab)?tab:'compare';
  if(['compare','scoring'].includes(currentTab))await loadRubricEvidence(ctx,e);
  const human=sum(Object.values(e.comparison?.reviews?.human||{}),v=>v),acceptance=sum(Object.values(e.comparison?.reviews?.acceptance||{}),v=>v);
  const stopped=Boolean(e.stop_requested_at),historical=e.origin?.kind==='historical_import'||e.queue?.read_only_history;const actions=(!stopped&&!historical&&['running','queued'].includes(e.status)?button('暂停调度','pause','','pause'):'')+(!stopped&&!historical&&(e.status==='paused'||e.queue?.paused&&['running','queued'].includes(e.status))?button('恢复调度','resume','primary','play'):'')+(!stopped&&!historical&&['running','queued','paused'].includes(e.status)?button('停止','stop','danger','stop'):'')+iconButton('导出证据','export','download');
  let html=`<a class="back" href="#experiments">${icon('back')}全部实验</a>`+pageTitle(e.title,`${label(e.project)} · ${e.origin?.kind==='historical_import'?'历史轨迹实验':'本机实验'}${e.taskpack?.version?' · 材料 v'+e.taskpack.version:''}`,actions,`${experimentBadge(e)}<span>${date(e.created_at)}</span><span class="tag">${countCases(e)} 案例 · ${families(e)} 家族</span><span class="tag">${configurationCount(e)} 方案 × ${e.repeats||1} 次</span>`);
  html+=tabs([['compare','结果比较','compare',routeUrl('experiment',id,'compare')],['runs','逐项运行','activity',routeUrl('experiment',id,'runs'),e.total_trials],['scoring','模型评价','score',routeUrl('experiment',id,'scoring')],['reviews','匿名评审','eye',routeUrl('experiment',id,'reviews')],['decisions','发布判定','shield',routeUrl('experiment',id,'decisions')],['sources','配置与来源','layers',routeUrl('experiment',id,'sources')]],currentTab);
  if(currentTab==='compare'){
    const decisionsResult=await optional(ctx,`/api/experiments/${id}/decisions`),decisions=decisionsResult.value?.decisions||[],latest=decisions[0];
    const runCounts=e.counts||e.status_counts;
    const abnormal=runCounts?issues(e):null;
    const summaryLink=(tab,title,value,detail,glyph)=>`<a class="result-signal" href="${routeUrl('experiment',id,tab)}"><span class="signal-label">${icon(glyph)}${esc(title)}</span><strong>${esc(value)}</strong>${detail?`<small>${esc(detail)}</small>`:''}<span class="signal-open">${icon('chevron')}</span></a>`;
    const compareAction=e.configurations?.length>1&&e.trials?.length
      ?button('逐例对照输出','compare-output','primary','compare')
      :`<a class="button primary" href="${routeUrl('experiment',id,'runs')}">${icon('file')} 查看运行证据</a>`;
    const modelCount=e.comparison?.current_model_assessments;
    html+=`<div class="result-signals">
      ${summaryLink(abnormal?'issues':'runs','已结束运行',number(e.completed_trials)+' / '+number(e.total_trials),abnormal==null?'异常数未知':abnormal?`查看 ${number(abnormal)} 项异常终态`:'无异常终态','activity')}
      ${summaryLink('scoring','模型评价',number(modelCount),'','score')}
      ${summaryLink('reviews','人工判断',number(e.comparison?.reviews?.human?human:null),e.comparison?.reviews?.acceptance?`${number(acceptance)} 份验收试填另计`:'验收试填数未知','eye')}
    </div>`;
    html+=`<section class="section result-configs">${sectionHead('方案比较',`${number(e.family_count??e.independent_cases)} 个独立情境家族 · 每案例重复 ${number(e.repeats)} 次`,compareAction)}${comparisonRows(e)}</section>`;
    if(modelCount){
      const analysis=await optional(ctx,`/api/experiments/${id}/analysis`);
      if(analysis.error)html+=unavailable('质量差异暂时无法读取',analysis.error);
      else {
        const quality=analysisPanel(analysis.value,e,{compact:true});
        html+=quality||note('当前评价尚未形成可用于比较的完整质量配对，可在模型评价中查看覆盖与缺失。');
      }
    }
    html+=decisionsResult.error?unavailable('发布判定暂时无法读取',decisionsResult.error):
      `<div class="decision-card">${icon('shield')}<div><h3>${latest?'最近判定 · '+esc(label(latest.status)):'尚未记录发布判定'}</h3></div><div class="action-row"><a class="button small" href="${routeUrl('experiment',id,'decisions')}">${latest?'查看判定依据':'设置判定'} ${icon('arrow')}</a></div></div>`;
    html+=performancePanel(e.performance);
    html+=`${help('耗时口径','耗时包含全部执行尝试；样本较小时 p95 可能接近最大值。配置、时段与缓存可能影响性能，业务质量需要独立评价。')}`;
  }
  if(currentTab==='runs')html+=(stopped?note('本实验已停止，后续调度不会恢复。需要再次执行时，从具体运行明确重试；已评价结果不能通过重试覆盖。'): '')+runTable(e,tab==='issues');
  if(currentTab==='scoring'){
    const [jobsResult,analysisResult,receiptsResult]=await Promise.all([optional(ctx,`/api/evaluation-jobs?experiment_id=${id}`),optional(ctx,`/api/experiments/${id}/analysis`),optional(ctx,`/api/evaluation-revalidations?experiment_id=${id}`)]);
    html+=sectionHead('独立评分作业','',button('创建评分作业','new-job','primary','plus'));
    html+=jobsResult.error?unavailable('评分作业暂未连接',jobsResult.error):`<div class="panel">${!(jobsResult.value?.jobs||[]).length&&e.comparison?.current_model_assessments?empty('当前工作区没有独立评分作业',`已有 ${e.comparison.current_model_assessments} 条模型评价记录随证据保存${historical?'并导入本工作区':''}；它们不是此处新建的评分作业。原作业若未一同导入，会继续显示缺失。`,`<a class="button small" href="${routeUrl('experiment',id,'sources')}">核对导入来源与冻结材料 ${icon('arrow')}</a>`,'score',true):jobsTable(jobsResult.value?.jobs||[])}</div>`;
    if(receiptsResult.value?.receipts?.length||receiptsResult.value?.invalid?.length)html+=`<section class="section">${sectionHead('原 AI 判断重新验证','')}${receiptList(receiptsResult)}</section>`;
    html+=modelComparison(e.comparison?.model_dimensions,Object.fromEntries((e.configurations||[]).map(c=>[c.id,c])),e.rubric,e.id);
    html+=analysisResult.error?unavailable('家族级分析暂未连接',analysisResult.error):analysisPanel(analysisResult.value,e);
  }
  if(currentTab==='reviews'){
    html+=sectionHead('独立匿名评审','',button('新增评审席位','reviewer','','plus'));
    html+=`<div class="panel">${(e.reviewers||[]).map(r=>`<div class="review-link"><div><strong>${esc(r.label)}</strong></div><a class="button small" href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">进入匿名评审 ${icon('external')}</a></div>`).join('')||empty('尚未建立评审席位','新增独立席位后，将入口交给未看过配置身份的评审者。',button('新增席位','reviewer','small','plus'),'eye',true)}</div>`;
    html+=`<div class="metric-strip">${metric('人工评价',number(human))}${metric('验收试填',number(acceptance))}${metric('有分歧的配对',number(e.comparison?.disagreement_pairs??0))}${metric('评审席位',number(e.reviewers?.length??0))}</div>`+help('评审用途','已看过管理员结果或正在验证功能时，选择“验收试填”。');
    if(Object.keys(e.comparison?.dimensions||{}).length)html+=`<details><summary>分项人工偏好与验收记录</summary>${json(e.comparison.dimensions)}</details>`;
  }
  if(currentTab==='sources'){
    html+=`<div class="configuration-grid">${(e.configurations||[]).map((c,i)=>`<article class="panel config-card"><span class="tag ${c.id===(e.baseline_id||e.configurations[0].id)?'baseline':''}">${c.id===(e.baseline_id||e.configurations[0].id)?'基线':'候选'}</span><h3>${esc(c.name)}</h3><p>${esc(label(c.mode))}</p>${provenanceSummary(c.provenance,c.parameters)}<details><summary>请求配置</summary>${content(c.parameters)}</details><details><summary>源码、提示词与能力快照</summary>${json(c.provenance)}</details></article>`).join('')}</div>`;
    html+=await frozenMaterials(ctx,e);
  }
  if(currentTab==='decisions'){
    const result=await optional(ctx,`/api/experiments/${id}/decisions`);
    html+=sectionHead('发布判定','',button('新增判定','new-decision','primary','shield'));
    html+=result.error?unavailable('发布判定暂未连接',result.error):decisionList(result.value?.decisions||[],e.configurations||[]);
  }
  return html;
}

function assessmentValue(value,rubric=[],version='',rubricHash=null,anchor=''){
  if(!value?.scores||typeof value.scores!=='object')return evidenceContent(value);
  const names=Object.fromEntries(rubric.map(r=>[r.id,r.name])),scores=value.scores,statuses=value.score_status||{},violations=Array.isArray(value.hard_violations)?value.hard_violations:[],dimensions=[...new Set([...Object.keys(scores),...Object.keys(statuses)])];
  const extra=Object.fromEntries(Object.entries(value).filter(([key])=>!['hard_violations','scores','score_status','provider_usage','media','schema_version','job_id','unit_id','evaluator_hash','rubric_hash','payload_hash','evaluator_id','rubric_id','pointer_normalizations','preference','confidence','operation_kind','original_job_id','original_unit_id','logical_unit_hash','original_job_created_at','original_evaluator_hash','validator','revalidation_engine_hash','typed_evidence','transform_log','new_model_requests','original_usage_reference'].includes(key)));
  return `<div class="score-list" aria-label="独立评价分项分值">${dimensions.map(d=>{const declared=statuses[d],score=scores[d],status=['not_applicable','unavailable'].includes(declared)?declared:([undefined,'rated'].includes(declared)&&typeof score==='number'&&Number.isFinite(score)?'rated':'unrated');return `<div class="score-row" data-read-anchor="${esc(anchor+'/scores/'+d)}" data-read-title="${esc(names[d]||label(d))}"><span>${esc(names[d]||label(d))}</span><strong>${status==='rated'?esc(score):'—'}</strong>${status!=='rated'?`<span class="score-state">${esc(label(status))}</span>`:''}${rubricHelp(version,d,rubricHash,'',true)}</div>`;}).join('')}</div>`+violations.map(v=>`<div class="violation-card"><div class="action-row"><strong>${esc(names[v.dimension]||label(v.dimension)||'事实或边界问题')}</strong><span class="tag">${esc(({major:'重大问题',critical:'严重问题',minor:'一般问题'})[v.severity]||v.severity||'待核对')}</span>${v.disputed?'<span class="tag">存在争议</span>':''}</div><p>${esc(v.explanation||v.reason||'')}</p>${v.quote?`<blockquote>${esc(v.quote)}</blockquote>`:''}${v.source_quote?`<p class="source-note">对应要求：${esc(v.source_quote)}</p>`:''}</div>`).join('')+(Object.keys(extra).length?evidenceContent(extra):'');
}
function assessmentOrigin(a){
  const value=a.value||{};if(value.operation_kind!=='deterministic_revalidation')return '';
  const original=a.provenance?.original||{},job=value.original_job_id||original.job_id,unit=value.original_unit_id||original.unit_id,receipt=a.provenance?.receipt_id||value.job_id||a.evaluation_job_id;
  return `<div class="revalidation-brief"><p><strong>原 AI 判断 · 确定性重验证</strong><br>沿用原响应，不增加独立评阅者或样本。新增模型请求 ${esc(value.new_model_requests??a.provenance?.new_model_requests??'未记录')} 次。</p><div class="action-row">${job&&!String(job).startsWith('rv1-')?`<a href="${routeUrl('job',job,unit||'')}" data-action="close-route">原作业${unit?'与单元':''} ${icon('arrow')}</a>`:''}${receipt&&String(receipt).startsWith('rv1-')?`<a href="${routeUrl('revalidation',receipt)}" data-action="close-route">重验证收据 ${icon('arrow')}</a>`:''}</div><details><summary>原响应、验证器与完整来源</summary><p class="source-note">收据包含无效、无响应和不满足条件的原单元；登记收据不等于评价已导入。原模型用量只引用、不再次累计。</p>${json({original:a.provenance?.original||null,original_job_id:job,original_unit_id:unit,receipt_id:receipt,validator:value.validator||a.provenance?.validator,revalidation_engine_hash:value.revalidation_engine_hash,typed_evidence:value.typed_evidence,transform_log:value.transform_log,original_usage_reference:value.original_usage_reference})}</details></div>`;
}
function assessmentCards(records,rubric=[],{compareSide=null}={}){
  const names=Object.fromEntries(rubric.map(r=>[r.id,r.name])),seen=new Map();
  return records.map((a,index)=>{
    const evidence=Array.isArray(a.evidence)?a.evidence:[],preference=a.value?.preference,identity=JSON.stringify([a.evaluator,a.rubric_version,a.execution_attempt,a.value?.original_job_id||a.value?.job_id||a.evaluation_job_id,a.value?.original_unit_id||a.value?.unit_id]),ordinal=seen.get(identity)||0;seen.set(identity,ordinal+1);
    const targetTrial=compareSide!==null?state.compare?.results[compareSide]:null,currentBinding=targetTrial&&a.current_result===true&&Number(a.execution_attempt)===Number(targetTrial.attempt);
    const anchor='/judgments/'+encodeURIComponent(identity)+'/'+ordinal,title=a.evaluator_snapshot?.name||String(a.evaluator||'模型评价').split('@')[0],rubricEvidence=matchingRubric(a.rubric_version,assessmentRubricHash(a));
    return `<article class="assessment-card" data-read-anchor="${esc(anchor)}" data-read-title="${esc(title)}" data-reader-heading="true"><div class="assessment-head"><strong>${esc(title)}</strong><span class="tag">${a.current_result?'当前结果':'历史结果'} · 尝试 ${esc(a.execution_attempt??'未记录')}</span></div><p class="assessment-reason">${esc(a.reason||'此条未提供评价摘要。')}</p>${assessmentValue(a.value,rubric,a.rubric_version,assessmentRubricHash(a),anchor)}<details class="assessment-identity"><summary>评分器与量表版本${rubricEvidence?.rubric?.name?' · '+esc(rubricEvidence.rubric.name):''}</summary><dl class="definition-list"><dt>评分器完整标识</dt><dd class="mono">${esc(a.evaluator)}</dd><dt>请求模型</dt><dd>${esc(a.evaluator_snapshot?.model||'此记录未携带')} · ${esc(a.evaluator_snapshot?.reasoning_effort||'推理强度未记录')}</dd><dt>量表完整标识</dt><dd class="mono">${esc(a.rubric_version)}<br>${esc(assessmentRubricHash(a)||'完整哈希未记录或冲突')}</dd></dl>${help('请求配置与生效值','此处是冻结的请求配置，不据此推断供应商实际生效值。各维独立呈现，不合成总分。')}</details>${assessmentOrigin(a)}${evidence.length?`<div class="assessment-evidence"><h4>引文与评分依据 · ${evidence.length} 条</h4>${evidence.map((item,i)=>`<details class="evidence-item" data-read-anchor="${esc(anchor+'/evidence/'+i)}" data-read-title="${esc(names[item.dimension]||label(item.dimension)||'评价依据')}" ${i===0?'open':''}><summary><strong>${esc(names[item.dimension]||label(item.dimension)||'评价依据')}</strong><span>${esc(item.quote?String(item.quote).slice(0,100):item.explanation||'展开完整依据')}</span></summary>${item.quote?`<blockquote>${esc(item.quote)}</blockquote>`:''}<p>${esc(item.explanation||'')}</p><div class="evidence-actions"><span class="source-note">${item.absence?'指出交付缺失 · ':''}${esc(({output:'交付正文',input:'本次输入',media:'原图'})[item.source]||item.source||'来源未注明')}</span>${currentBinding&&['output','input','media'].includes(item.source)?button(item.source==='output'?'定位正文':item.source==='media'?'核对原图':'核对输入','compare-evidence','small','arrow',`data-side="${compareSide}" data-attempt="${esc(a.execution_attempt)}" data-current="true" data-pointer="${esc(item.pointer||'')}" data-source="${esc(item.source)}" data-quote="${esc(item.quote||'')}"`):targetTrial?`<a class="text-link" href="${routeUrl('trial',targetTrial.id,'assessments')}" data-action="close-route">核对历史尝试 ${esc(a.execution_attempt??'未记录')}</a>`:''}</div>${item.pointer?`<details class="evidence-pointer"><summary>引文字段路径</summary><code>${esc(item.pointer)}</code></details>`:''}</details>`).join('')}</div>`:''}${preference?`<div class="preference-note"><strong>这条记录的配对偏好 · ${esc(({self:'当前结果更好',other:'另一结果更好',tie:'平局',unable:'无法判断'})[preference.choice]||label(preference.choice))}</strong><p>${esc(preference.reason||'')}</p><small>另一运行 ${esc(preference.other_trial_id?.slice(0,8)||'未映射')} · 尝试 ${esc(preference.other_attempt??'未记录')}</small></div>`:''}<details><summary>评价完整记录与输出绑定</summary>${json(a)}</details></article>`;
  }).join('');
}
function analysisPanel(analysis,e,{compact=false}={}){
  if(!analysis)return '';
  const configNames=Object.fromEntries((e.configurations||[]).map(c=>[c.id,c.name])),dims=Object.fromEntries((Array.isArray(e.rubric)?e.rubric:[]).map(r=>[r.id,r.name])),den=analysis.denominators||{};
  const format=value=>typeof value==='number'&&Number.isFinite(value)?value.toFixed(2):'—';
  const signed=value=>typeof value==='number'&&Number.isFinite(value)?(value>0?'+':'')+value.toFixed(2):'—';
  const countText=counts=>Object.entries(counts||{}).map(([key,value])=>`${label(key)} ${value}`).join(' · ')||'无';
  const comparisons=(analysis.comparisons||[]).filter(c=>!compact||(c.model_dimensions||[]).some(d=>d.paired_rated>0)||(c.model_preferences||[]).some(p=>p.eligible_pairs>0));
  if(compact&&!comparisons.length)return '';
  const rows=comparisons.map(c=>{
    const dimensions=(c.model_dimensions||[]).filter(d=>!compact||d.paired_rated>0),preferences=(c.model_preferences||[]).filter(p=>p.votes>0),human=c.human_preferences||{};
    return `<div class="analysis-block"><div class="section-head"><div><h3>${esc(configNames[c.candidate_id]||c.candidate_id)} <span class="muted">相对基线</span></h3><p>${esc(label(c.split))} · ${c.denominators?.families??0} 个独立家族 · ${c.denominators?.expected_pairs??0} 个预期配对</p></div><span class="tag ${c.split==='holdout'?'holdout':''}">${esc(label(c.split))}</span></div>${dimensions.length?analysisDimensionsTable(dimensions,dims,e):`<div class="panel">${empty('还没有完整的质量配对','取得同一评分器、量表与对应输出的两侧评分后，才计算差异。','','compare',true)}</div>`}${preferences.length?`<details><summary>匿名模型偏好 · 平局、无法判断与分歧</summary><div class="panel table-wrap"><table><thead><tr><th>评分器</th><th>候选更好</th><th>基线更好</th><th>平局</th><th>无法判断</th><th>未进入偏好</th></tr></thead><tbody>${preferences.map(p=>`<tr><td>${esc(p.evaluator)}<div class="subline">${esc(p.rubric_version)}</div></td><td>${p.choice_counts?.candidate||0}</td><td>${p.choice_counts?.baseline||0}</td><td>${p.choice_counts?.tie||0}</td><td>${p.choice_counts?.unable||0}</td><td>${esc(countText(p.excluded_pairs))}</td></tr>`).join('')}</tbody></table></div><p class="source-note">${c.model_preference_disagreements?.length||0} 个配对存在评分器分歧；偏好票数不是独立样本数。</p></details>`:''}${!compact?`<details><summary>执行覆盖、人工偏好与验收试填</summary><div class="panel-inset"><p class="source-note">执行配对：${esc(countText(c.execution_pairs))}</p><p class="source-note">真实人工 ${human.votes||0} 份 · 验收试填 ${c.acceptance_preferences?.votes||0} 份；不混合 AI 与人工。</p>${json({human_preferences:human,acceptance_preferences:c.acceptance_preferences,model_preference_disagreements:c.model_preference_disagreements})}</div></details>`:''}</div>`;
  }).join('');
  return `<section class="section">${sectionHead('质量差异与不确定性','',compact?`<a class="text-link" href="${routeUrl('experiment',e.id,'scoring')}">完整评分证据 ${icon('arrow')}</a>`:'')}<div class="analysis-summary"><span><strong>${den.families??families(e)}</strong> 情境家族</span><span><strong>${den.cases??countCases(e)}</strong> 案例</span><span><strong>${den.trials??e.total_trials}</strong> 运行</span><span><strong>${den.missing_trials??0}</strong> 缺失运行</span></div>${rows||`<div class="panel">${empty('等待可配对的质量证据','','','score',true)}</div>`}${analysis.warnings?.length?note(analysis.warnings.join(' '),'warning'):''}${help('差异与区间的含义','差值按候选减去基线计算，各情境家族等权。区间描述当前材料家族间的波动。小样本、选择过的材料与合成材料不能证明真实用户泛化；有序评分的均值仅作描述，未做多重比较校正。')}${!compact?`<details><summary>完整分析、重评选择规则与随机种子</summary>${json(analysis)}</details>`:''}</section>`;
}
function jobsTable(jobs){
  if(!jobs.length)return empty('还没有评分作业','',button('创建评分作业','new-job','small','plus'),'score',true);
  return `<div class="table-wrap"><table><thead><tr><th>评分作业</th><th>评分器</th><th>方式</th><th>状态</th><th>进度</th><th>建立时间</th><th></th></tr></thead><tbody>${jobs.map(j=>{const counts=j.counts||{},total=j.total_units??j.unit_count??sum(Object.values(counts),v=>v),done=j.completed_units??sum(Object.entries(counts).filter(([k])=>!['queued','running','pending'].includes(k)),([,v])=>v);return `<tr><td><a class="cell-title" href="${routeUrl('job',j.id)}">${esc(j.title||'独立评分 · '+String(j.id).slice(0,8))}</a><div class="subline">${esc(j.rubric?.name||j.rubric?.id||j.rubric_id||j.rubric_version||'冻结量表')}</div></td><td>${esc(j.evaluator?.name||j.evaluator?.id||j.evaluator_id||'—')}</td><td>${j.mode==='pairwise'?'匿名配对':'逐项评分'}</td><td>${badge(j.status)}</td><td class="numeric">${number(done)} / ${number(total)}</td><td class="muted numeric">${date(j.created_at)}</td><td><a class="row-arrow" href="${routeUrl('job',j.id)}" aria-label="查看评分作业">${icon('chevron')}</a></td></tr>`;}).join('')}</tbody></table></div>`;
}
async function jobsPage(ctx,tab='all'){
  const [result,queueResult,receiptsResult]=await Promise.all([optional(ctx,'/api/evaluation-jobs'),optional(ctx,'/api/review-queue'),optional(ctx,'/api/evaluation-revalidations')]);
  const jobs=result.value?.jobs||[],items=queueResult.value?.items||[],pending=items.filter(i=>!i.adjudicated).length;
  let html=pageTitle('评分作业','',button('创建评分作业','new-job','primary','plus'));
  html+=tabs([['all','模型评分作业','score','#jobs',jobs.length],['review','需要复核','eye','#jobs/review',pending],['revalidations','原判断重验证','shield','#jobs/revalidations',receiptsResult.value?.receipts?.length]],['review','revalidations'].includes(tab)?tab:'all');
  if(tab==='review')html+=queueResult.error?unavailable('评分复核队列暂未连接',queueResult.error):reviewQueueTable(items);
  else if(tab==='revalidations')html+=receiptList(receiptsResult);
  else html+=result.error?unavailable('评分服务暂未连接',result.error):`<div class="panel">${jobsTable(jobs)}</div>`;
  return html+`${help('取消与计费','评分与产品执行分开计量。取消会终止本地评分进程，远端是否停止计算与计费仍可能未知。')}`;
}
const revalidationStatuses=['valid','invalid','no_response','ineligible'];
function receiptCounts(counts){return `<dl class="receipt-counts">${revalidationStatuses.map(status=>`<div><dt>${esc(label(status))}</dt><dd>${number(counts?.[status]??0)}</dd></div>`).join('')}</dl>`;}
function receiptList(result){
  if(result.error)return unavailable('完整重验证收据暂未连接',result.error);
  const receipts=result.value?.receipts||[],invalid=result.value?.invalid||[];
  return help('重验证口径','沿用原 AI 响应重新核验。收据包含全部原始单元，不增加评阅者或独立样本。')+`<div class="receipt-list">${receipts.map(r=>`<article class="panel receipt-card"><div class="section-head"><div><h3><a href="${routeUrl('revalidation',r.id)}">${esc(r.original_job_title||'原评分作业 '+r.original_job_id?.slice(0,8))}</a></h3><p>${esc(r.evaluator?.name||r.evaluator?.id||'评分器未记录')} · ${esc(r.rubric?.id||'量表未记录')} v${esc(r.rubric?.version||'未记录')}</p></div><span class="tag">${r.unit_count} 个原单元</span></div>${receiptCounts(r.counts)}<div class="receipt-footer"><span>原作业：${Object.entries(r.original_counts||{}).map(([k,v])=>`${esc(label(k))} ${v}`).join(' · ')||'计数未记录'}</span><span>新增模型请求 ${number(r.usage?.model_requests)}</span></div><p class="source-note">验证器 ${esc(r.engine?.validator?.id||'未记录')} v${esc(r.engine?.validator?.version||'未记录')} · <span class="mono">${esc(r.engine?.hash?.slice(0,10)||'哈希未记录')}</span></p><p class="source-note">生成 ${number(r.assessment_count)} 条可导入评价；登记收据不代表已导入。${r.assessment_count===0?'零评价收据同样保留。':''}</p><div class="action-row"><a class="button small" href="${routeUrl('revalidation',r.id)}">完整收据与原单元 ${icon('arrow')}</a><span class="source-note mono">${esc(r.id?.slice(0,16))}</span></div></article>`).join('')||`<div class="panel">${empty('尚未登记完整重验证收据','已导入的评价可能引用外部收据；请回到对应原评分证据核对。登记与评价导入分别记录。','','shield',true)}</div>`}</div>${invalid.length?`<section class="section">${sectionHead('无法读取的收据',`${invalid.length} 份登记记录未通过读取校验；不会从列表中消失`)}<div class="panel panel-pad">${invalid.map(i=>`<article class="invalid-receipt">${note(i.error?.message||(typeof i.error==='string'?i.error:null)||i.message||i.reason||'收据校验未通过','error')}<details><summary>登记身份与校验结果</summary>${json(i)}</details></article>`).join('')}</div></section>`:''}`;
}
function validationIssueSummary(issues){
  if(!issues?.length)return '<span class="muted">未记录校验问题</span>';
  const names={json_value_invalid_literal:'引用值不符合所声明的数据格式',quote_not_found:'引文未在指定原文中找到',rated_without_valid_evidence:'已有评分，但缺少有效依据',invalid_json_pointer:'引用路径格式不正确',pointer_not_found:'引用路径对应的字段不存在',missing_evidence:'未提供证据'};
  const groups=new Map();for(const issue of issues){const text=names[issue.code]||'其他校验问题（见原始记录）',side=issue.side?`方案 ${issue.side} · `:'';const key=side+text;groups.set(key,(groups.get(key)||0)+1);}
  return [...groups].map(([text,count])=>`<p>${esc(text)}${count>1?` · ${count} 项`:''}</p>`).join('');
}
function validationIssueComparison(unit){return `<div class="validation-comparison"><div><strong>本次校验 · ${unit.validation_issues?.length||0} 项</strong>${validationIssueSummary(unit.validation_issues)}</div><div><strong>原校验 · ${unit.original_validation_issues?.length||0} 项</strong>${validationIssueSummary(unit.original_validation_issues)}</div></div><details><summary>原始错误、引用路径与维度</summary>${json({current:unit.validation_issues||[],original:unit.original_validation_issues||[]})}</details>`;}
async function revalidationPage(ctx,id){
  const r=await ctx.get('/api/evaluation-revalidations/'+encodeURIComponent(id));ctx.current={...r,_kind:'revalidation'};
  const originalCounts=Object.entries(r.original_counts||{}),units=r.units||[];
  return `<a class="back" href="#jobs/revalidations">${icon('back')}全部重验证收据</a>`+pageTitle('原 AI 判断重新验证',r.original_job_title||'',iconButton('导出完整收据','export-revalidation','download'),`${badge(r.status)}<span>${date(r.registered_at)}</span><span class="tag">${r.unit_count} 个原单元</span>`)+
    note('这是对已有模型响应的确定性重新验证。原作业、原失败与原用量保持原样，不作为新的独立 AI 判断。')+
    `<div class="two-columns section"><section class="panel panel-pad"><h2>原评分作业</h2><p>${esc(r.original_job_title||r.original_job_id)}</p><div class="original-counts">${originalCounts.map(([k,v])=>`<span>${badge(k)} <strong>${number(v)}</strong></span>`).join('')}</div><p class="source-note">原单元分母 ${number(r.unit_count)} · 原作业状态 ${esc(label(r.original_job_status))}</p><a class="text-link" href="${routeUrl('job',r.original_job_id)}">查看原作业与响应 ${icon('arrow')}</a></section><section class="panel panel-pad"><h2>本次验证结果</h2>${receiptCounts(r.counts)}${help('重验证口径','有效表示通过此版本的确定性验证，不代表新增模型调用成功。无响应保留原供应商失败。')}</section></div>`+
    `<div class="metric-strip section">${metric('新增模型请求',number(r.usage?.model_requests),'仅本次重新验证')}${metric('新增 tokens',number(r.usage?.total_tokens),'原作业用量只引用，不再次累计')}${metric('新增费用',r.usage?.cost===0?'0':r.usage?.cost==null?'未知':esc(r.usage.cost),'仅本次确定性处理')}${metric('可导入评价',number(r.assessment_count),'生成数量，不是实际导入确认')}</div>`+
    `<div class="notice inline">${icon('info')}<div><strong>收据登记与评价导入是两个动作。</strong><p>这份收据说明验证生成了 ${number(r.assessment_count)} 条可导入评价；不能凭登记数量确认它们已进入统计。${r.experiment_id?`<a class="text-link" href="${routeUrl('experiment',r.experiment_id,'scoring')}">查看实验当前评价记录 ${icon('arrow')}</a>`:''}</p></div></div>`+
    `<section class="section">${sectionHead('评分与验证身份','')}<div class="panel panel-pad"><dl class="definition-list"><dt>原评分器</dt><dd>${esc(r.evaluator?.name||r.evaluator?.id||'未记录')}</dd><dt>量表</dt><dd>${esc(r.rubric?.id||'未记录')} · v${esc(r.rubric?.version||'未记录')}</dd><dt>重验证引擎</dt><dd>${esc(r.engine?.id||'未记录')} · v${esc(r.engine?.version||'未记录')}</dd><dt>验证器</dt><dd>${esc(r.engine?.validator?.id||'未记录')} · v${esc(r.engine?.validator?.version||'未记录')}</dd></dl><details><summary>完整身份、哈希与校验范围</summary>${json({evaluator:r.evaluator,rubric:r.rubric,engine:r.engine,verification:r.verification,receipt_hash:r.receipt_hash,source_hash:r.source_hash})}<p class="source-note">读取核验检查收据完整性与分母；${r.verification?.semantic_validation==='not_recomputed'?'此读取操作没有重新执行历史语义验证。':'语义验证范围以以上原始声明为准。'}</p></details></div></section>`+
    `<section class="section">${sectionHead('全部原单元',`${units.length} / ${r.unit_count} 个单元，包括无响应与未通过`)}<div class="panel"><div class="filters"><label class="receipt-filter">验证状态<select id="receipt-status-filter">${opt('','全部验证结果')}${revalidationStatuses.map(s=>opt(s,`${label(s)} · ${r.counts?.[s]??0}`)).join('')}</select></label><span id="receipt-visible-count" class="source-note">${units.length} / ${units.length} 个单元</span></div>${units.map((u,i)=>`<details class="receipt-unit" data-receipt-status="${esc(u.status)}"><summary><span class="numeric muted">${String(i+1).padStart(2,'0')}</span><strong>${esc(u.original?.case_id||u.original?.unit_id||'原单元')}</strong>${badge(u.status)}</summary><div class="receipt-unit-body"><div class="action-row"><span>原状态 ${badge(u.original?.unit_status)}</span><span class="tag">${u.status==='valid'&&u.eligible_for_assessment?'验证有效，可生成评价':'本单元未生成可导入评价'}</span><a class="text-link" href="${routeUrl('job',u.original?.job_id||r.original_job_id,u.original?.unit_id)}">查看原评分单元 ${icon('arrow')}</a></div>${u.ineligibility_reason?note(typeof u.ineligibility_reason==='string'?u.ineligibility_reason:JSON.stringify(u.ineligibility_reason),'warning'):''}${u.status==='no_response'?note('原单元没有可供重新验证的模型响应；该失败仍在完整分母中。'):''}${validationIssueComparison(u)}<details><summary>原身份、响应哈希与转换记录</summary>${json(u)}</details></div></details>`).join('')}<div id="receipt-filter-empty" hidden>${empty('此状态没有单元','','','shield',true)}</div></div></section>`+
    `<details><summary>收据摘要、用量与限制的原始字段</summary>${json(Object.fromEntries(Object.entries(r).filter(([k])=>k!=='units')))}</details>`;
}
function jobUnitPreview(unit,rubric={}){
  const value=unit.public_result??unit.result??unit.output,issues=unit.validation_issues||[],groups=new Map();
  const dims=Array.isArray(rubric.dimensions)?rubric.dimensions:Object.entries(rubric.dimensions||{}).map(([id,d])=>({id,...d}));
  const names=Object.fromEntries(dims.map(d=>[d.id,d.name||d.id]));
  for(const issue of issues){const key=JSON.stringify([issue.code,issue.side,issue.pointer]);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(issue);}
  const issueTable=issues.length?`<div class="unit-validation"><h4>原评分的验证问题 · ${issues.length} 项</h4><div class="table-wrap"><table><thead><tr><th>校验问题</th><th>涉及结果与位置</th><th>数量</th></tr></thead><tbody>${[...groups.values()].map(items=>{const first=items[0],dimensions=[...new Set(items.map(i=>i.dimension).filter(Boolean))];return `<tr><td><strong>${esc(label(first.code))}</strong><div class="subline mono">${esc(first.code)}</div></td><td>${first.side?'结果 '+esc(first.side):'未限定一侧'}${first.pointer?`<div class="subline mono">${esc(first.pointer)}</div>`:''}${dimensions.length?`<div class="subline">${dimensions.map(d=>esc(names[d]||label(d))).join(' · ')}</div>`:''}</td><td>${items.length}</td></tr>`;}).join('')}</tbody></table></div>${help('原评分验证状态','保留原评分当时的验证状态。后续确定性重验证是独立的处理记录，不覆盖这些问题。')}</div>`:'';
  const sides=value?.sides&&typeof value.sides==='object'?Object.entries(value.sides):[];
  const preview=sides.length?`<div class="unit-response-summary"><h4>原响应摘要${unit.status==='invalid'?' · 原校验未通过':''}</h4>${sides.map(([side,result])=>`<article><strong>结果 ${esc(side)}</strong><p>${esc(result.reason||'未提供简短理由')}</p><div class="unit-score-summary">${Object.entries(result.scores||{}).map(([dimension,score])=>`<span><span>${esc(names[dimension]||label(dimension))}</span><strong>${score==null?esc(label(result.score_status?.[dimension]||'unavailable')):esc(score)}</strong></span>`).join('')}</div></article>`).join('')}${value.preference?.reason?`<p class="source-note">原配对理由：${esc(value.preference.reason)}</p>`:''}${unit.status==='invalid'?'<p class="source-note">以上为原响应中的判断，不能据此认定它已通过校验或进入当前质量统计。</p>':''}</div>`:'';
  return scoringRuntimeSummary(unit)+(unit.error&&unit.error.code!=='model_failed'?note(typeof unit.error==='string'?unit.error:unit.error.message||unit.error.code||JSON.stringify(unit.error),'error'):'')+issueTable+preview+(value!==undefined&&value!==null?`<details class="unit-full-response"><summary>展开完整模型响应</summary><div class="content-box">${outputContent(value)}</div></details>`:'');
}
async function jobPage(ctx,id,unitId=''){
  ctx.focusUnit=unitId;
  const j=await ctx.get('/api/evaluation-jobs/'+encodeURIComponent(id));ctx.current={...j,_kind:'job'};
  const calibration=j.calibration?await optional(ctx,`/api/evaluation-jobs/${encodeURIComponent(id)}/calibration`):null;
  const units=j.units||[],counts=j.counts||{},done=j.completed_units??sum(Object.entries(counts).filter(([k])=>!['queued','running','pending'].includes(k)),([,v])=>v),total=j.total_units??j.unit_count??units.length;
  return `<a class="back" href="#jobs">${icon('back')}全部评分作业</a>`+pageTitle(j.title||'独立评分作业',`${j.evaluator?.name||j.evaluator?.id||j.evaluator_id||'评分器'} · ${j.mode==='pairwise'?'匿名配对':'逐项评分'}`,['running','queued','pending'].includes(j.status)?button('取消剩余评分','cancel-job','danger','stop'):'',`${badge(j.status)}<span>${date(j.created_at)}</span>${j.experiment_id?`<a class="text-link" href="${routeUrl('experiment',j.experiment_id,'scoring')}">返回实验 ${icon('arrow')}</a>`:'<span class="tag">内部规范锚点</span>'}`)+
    `<div class="metric-strip">${metric('评分进度',`${number(done)}<small>/ ${number(total)}</small>`,'包含评分失败与不可用')}${metric('成功评分',number(counts.succeeded??0),'已取得有效结构化判断')}${metric('不可用 / 异常',number(sum(Object.entries(counts).filter(([k])=>!['succeeded','queued','running','pending'].includes(k)),([,v])=>v)),'保留失败原因与原始目标')}${metric('方式',j.mode==='pairwise'?'A / B':'逐项','冻结目标输出，不重跑产品')}</div>`+
    `<div class="two-columns"><div class="panel panel-pad"><h3>评分器与量表</h3><dl class="definition-list"><dt>评分器</dt><dd>${esc(j.evaluator?.id||j.evaluator_id||'未知')}</dd><dt>量表</dt><dd>${esc(j.rubric?.id||j.rubric_id||'未知')}</dd><dt>版本</dt><dd>${esc(j.rubric?.version||j.rubric_version||'未知')}</dd><dt>分组</dt><dd>${esc(label(j.split||'all'))}</dd></dl><details><summary>冻结的配置与评分量表</summary>${json({evaluator:j.evaluator,rubric:j.rubric})}</details></div><div class="panel panel-pad"><h3>预算与实际用量</h3>${content(j.usage||{status:'用量未返回'})}<details><summary>预算和限制</summary>${json({budget:j.budget,limitations:j.limitations})}</details></div></div>`+
    (calibration?.value?calibrationPanel(calibration.value):calibration?.error?unavailable('校准报告暂未连接',calibration.error):'')+
    `<section class="section">${sectionHead('评分单元','')}<div class="panel">${units.length?units.map((u,i)=>`<div class="panel-header block" id="scoring-unit-${esc(u.id)}"><details class="m-0" ><summary class="unit-summary" ><span class="muted numeric">${String(i+1).padStart(2,'0')}</span><strong class="flex-1" >${esc(u.case_name||u.case?.name||u.case_id||u.title||u.id||'评分单元')}</strong>${badge(u.status)}</summary>${jobUnitPreview(u,j.rubric)}<div class="action-row">${button('记录复核意见','adjudicate','small','eye',`data-job="${esc(j.id)}" data-unit="${esc(u.id)}"`)}${(u.validation_issues||[]).length?'<span class="tag">存在证据校验问题</span>':''}</div>${(j.adjudications||[]).filter(a=>a.unit_id===u.id).map(a=>`<div class="panel-inset section"><div class="action-row"><strong>${esc(label(a.disposition))}</strong><span class="tag">${esc(label(a.kind))}</span></div><p class="source-note">${esc(a.reason)}</p></div>`).join('')}<details><summary>评分单元完整证据</summary>${json(u)}</details></details></div>`).join(''):empty('等待生成评分单元','作业和冻结目标已保留，刷新可查看最新进度。','','score',true)}</div></section><details><summary>作业快照与全部计数</summary>${json(Object.fromEntries(Object.entries(j).filter(([key])=>key!=='units')))}</details>`;
}
async function evaluatorsPage(ctx){
  const [result,calibrations]=await Promise.all([optional(ctx,'/api/evaluators'),optional(ctx,'/api/calibrations')]);
  let html=pageTitle('评分器','',button('创建评分作业','new-job','primary','plus'));
  if(result.error)return html+unavailable('评分器目录暂未连接',result.error);
  const evaluators=result.value?.evaluators||[];
  html+=`<div class="dataset-grid">${evaluators.map(e=>`<article class="panel evaluator-card"><div class="icon-box">${icon('score')}</div><h3>${esc(e.name||e.id)}</h3><p>${esc(e.description||'对冻结结果进行独立评阅，输出分项判断与证据。')}</p><dl class="definition-list"><dt>标识</dt><dd class="mono">${esc(e.id)}</dd><dt>请求模型</dt><dd>${esc(e.model||e.requested?.model||e.parameters?.model||'由评分器声明')}</dd><dt>评分方式</dt><dd>${Array.isArray(e.modes)?e.modes.map(m=>m==='pairwise'?'匿名配对':'逐项评分').join(' · '):'逐项 / 匿名配对'}</dd></dl><details><summary>评分器版本与可观察能力</summary>${json(e)}</details><button class="small" data-action="new-job" data-evaluator="${esc(e.id)}">使用此评分器 ${icon('arrow')}</button></article>`).join('')||`<div class="panel">${empty('尚无注册评分器','','','score',true)}</div>`}</div>`;
  html+=`<div class="section">${note('可执行程序存在不代表已登录或调用成功。评分器的实际可用性由真实评分作业记录；内部 AI 校准不等同教师或用户金标。')}</div>`;
  html+=`<section class="section">${sectionHead('评分器校准','检查对公开规范锚点的符合程度，不替代业务专家标注')}${calibrations.error?unavailable('校准目录暂未连接',calibrations.error):`<div class="panel">${(calibrations.value?.calibrations||[]).map(c=>`<div class="review-link"><div><strong>${esc(c.name||c.id)}</strong><p>${esc(c.provenance)} · ${c.case_count} 案例 / ${c.family_count} 家族</p></div>${button('运行规范校准','new-calibration','small','score',`data-calibration="${esc(c.id)}"`)}</div>`).join('')||empty('尚未提供规范锚点','可通过API使用已有来源的校准材料。','','score',true)}</div>`}</section>`;
  return html;
}
async function trialPage(ctx,id,tab='output'){
  const t=await ctx.get('/api/trials/'+encodeURIComponent(id));ctx.current={...t,_kind:'trial'};
  const r=t.result||{},records=t.assessment_records||[],rules=r.assessments||[],delivered=hasReviewOutput(r),scoring=scoreAvailability(t),currentTab=['output','evidence','assessments','configuration'].includes(tab)?tab:'output';
  const actions=(terminal.has(t.status)&&t.status!=='succeeded'&&t.origin?.kind!=='historical_import'?button('重试运行','retry','','refresh'):'')+(terminal.has(t.status)?scoreButton(t,'独立重评'):'');
  const fromIssues=location.hash.split('/')[3]==='from-issues';
  const trialLink=tab=>routeUrl('trial',id,tab)+(fromIssues?'/from-issues':'');
  let html=`<a class="back" href="${routeUrl('experiment',t.experiment_id,fromIssues?'issues':'runs')}">${icon('back')}${fromIssues?'返回异常运行':'返回实验运行'}</a>`+pageTitle(t.case?.name||'逐项运行',`${t.configuration?.name||t.configuration_id} · 重复 ${t.repeat_index} · 尝试 ${t.attempt} · ${t.id.slice(0,8)}`,actions,`${badge(t.status)}<span class="tag">${esc(sourceLabel(t.case))}</span><span>耗时 ${duration(t.duration_ms)}</span><span>超时上限 ${t.timeout_seconds} 秒</span>`);
  html+=tabs([['output','交付结果','file',trialLink('output')],['evidence','阶段与尝试','activity',trialLink('evidence'),(t.attempts?.length||t.attempt||0)+' 次尝试'],['assessments','评价与限制','score',trialLink('assessments'),records.length],['configuration','配置与来源','layers',trialLink('configuration')]],currentTab);
  const error=r.error||t.error;
  if(error){html+=executionErrorNotice(error,t.status);const failure=lastFailure(t.events);if(failure)html+=`<p class="error-jump"><button class="small" data-action="failure-stage" data-trial="${esc(t.id)}" data-index="${failure.index}">定位最后失败阶段 · ${esc(failure.event.kind)} ${icon('arrow')}</button></p>`;}
  if(terminal.has(t.status)&&!scoring.available)html+=`<p class="score-unavailable">${icon('score')}<span>${esc(scoring.reason)}</span></p>`;
  if(currentTab==='output'){
    html+=`<div class="detail-grid"><div><section class="section mt-0" >${sectionHead(t.status==='succeeded'?'实际交付':'已记录的输出投影',delivered?(t.status==='succeeded'?'保留本次实际接受的输出；空结构同样保留':'保留已记录的输出投影；本次执行未成功'):terminal.has(t.status)?'本次未产生可评阅的交付内容':'等待运行交付')}<div class="panel content-box">${delivered?outputContent(r.review_output):empty(terminal.has(t.status)?'本次没有可交付结果':'等待交付结果',terminal.has(t.status)?'查看上方错误原因，或进入阶段与尝试复核过程。当前失败与输入快照会持续保留。':'阶段证据会随运行写入。失败与拒绝也会保留在当前运行。','','file',true)}</div></section><section class="section">${sectionHead('本次输入','')}<div class="panel content-box">${mediaEvidence(t.case,t.id,t.media)}${evidenceContent(readableInput(t.case?.input))}<details><summary>初始状态、期望边界与来源</summary>${content({initial_state:t.case?.initial_state,expected_behavior:t.case?.expected_behavior,source:t.case?.source})}</details></div></section></div><aside><div class="panel panel-pad"><h3>运行概况</h3><dl class="definition-list stacked"><dt>模式</dt><dd>${esc(label(t.configuration?.mode))}</dd><dt>开始</dt><dd>${date(t.started_at)}</dd><dt>结束</dt><dd>${date(t.ended_at)}</dd><dt>自动规则</dt><dd>${rules.length} 项可观察检查</dd><dt>模型评价记录</dt><dd>${records.length?records.length+' 条记录':'未运行'}</dd><dt>费用</dt><dd>${r.cost==null?'未知':esc(typeof r.cost==='object'?JSON.stringify(r.cost):r.cost)}</dd></dl></div><div class="section">${note('执行成功表示业务路径完成；质量仍需结合原文与独立评价判断。')}</div></aside></div>`;
  }
  if(currentTab==='evidence'){
    if(t.cancellation)html+=cancellationSummary(t.cancellation);
    html+=sectionHead('可观察阶段',`${t.events?.length||0} 条阶段事件；保留调用、响应、状态与错误，不记录模型隐藏思维链`);
    html+=note('一次运行包含执行尝试；一次执行尝试内可以多次调用或重试产品阶段。阶段内重试不会增加评测运行、重复样本或独立家族。');
    html+=`<div class="panel timeline">${(t.events||[]).map((event,index)=>`<article class="event ${lastFailure(t.events)?.index===index?'last-failure':''}" id="stage-${index}"><time>${date(event.timestamp).split(' ').at(-1)||''}</time><div>${stageHeading(event)}${stageSummary(event)}<details><summary>事件标识与原始证据</summary>${json(event)}</details></div></article>`).join('')||stageEmpty(t)}</div>`;
    html+=`<section class="section">${sectionHead('全部执行尝试','重试保留旧结果、失败与评价绑定')}<div class="panel">${(t.attempts||[]).map((a,i)=>`<div class="panel-header block" ><div class="section-head m-0" ><strong>尝试 ${a.attempt??a.execution_attempt??i+1}</strong><div class="action-row">${badge(a.status)}${terminal.has(a.status)?scoreButton(a,'重评此尝试','rescore-attempt','small',`data-attempt="${Number(a.attempt??a.execution_attempt??i+1)}"`):''}</div></div><p class="attempt-brief">${date(a.started_at)} → ${date(a.ended_at)}${a.result?.error?` · ${esc(failureReason(a.result.error,a.status)||a.result.error.code||'记录了错误')}`:''} · ${a.result?.review_output!=null?'保留可评阅输出':'未记录可评阅输出'}</p><details><summary>输出与完整尝试记录</summary>${json(a)}</details></div>`).join('')||empty('尚无完成的执行尝试','','','file',true)}</div></section>`;
  }
  if(currentTab==='assessments'){
    const e=await ctx.get('/api/experiments/'+t.experiment_id);
    await loadRubricEvidence(ctx,e,records);
    html+=sectionHead('自动规则','只检查声明的结构与可观察边界');
    html+=`<div class="panel">${rules.length?`<div class="table-wrap"><table><thead><tr><th>规则维度</th><th>结果</th><th>依据 / 限制</th></tr></thead><tbody>${rules.map(a=>`<tr><td>${esc(label(a.dimension||a.name))}</td><td>${badge(a.status)}</td><td>${esc(a.reason)}</td></tr>`).join('')}</tbody></table></div>`:empty('未产生规则结果','','','shield',true)}</div>`;
    html+=`<section class="section">${sectionHead('模型评价记录','绑定具体尝试与输出哈希；原判断的确定性重验证单独标注',scoreButton(t,'重新评分'))}${records.length?assessmentCards(records,e?.rubric||[]):`<div class="panel">${empty(scoring.available?'尚未运行独立评分':'本次质量评分不可用',scoring.available?'创建评分作业后，可在这里逐项核对理由和证据。':scoring.reason,scoring.available?button('创建评分作业','rescore','small','plus'):'','score',true)}</div>`}</section>`;
    if(r.limitations?.length)html+=`<div class="panel panel-pad"><h3>适配器随输出保存的声明</h3>${help('适配器限制','以下为原始适配器限制，可能描述适配器开发范围；不是本实验的材料清单，也不据此改写已保存的实验范围。')}<ul>${r.limitations.map(l=>`<li class="muted">${esc(l)}</li>`).join('')}</ul><p class="source-note">本实验实际选用 ${countCases(e)} 案例、${families(e)} 家族；当前案例属于 ${esc(label(t.case?.split||'未分组'))}。</p><a class="text-link" href="${routeUrl('experiment',t.experiment_id,'sources')}">核对本实验材料与版本 ${icon('arrow')}</a></div>`;
  }
  if(currentTab==='configuration'){
    html+=`<div class="two-columns"><div class="panel panel-pad"><h3>请求与实际观察</h3><details open><summary>请求配置</summary>${content(t.configuration?.parameters)}</details><details open><summary>供应商回显 / 实际观察</summary>${content(r.observed_configuration||'尚未返回，未知')}</details><details><summary>已知用量</summary>${json(r.usage??null)}</details></div><div class="panel panel-pad"><h3>冻结来源</h3><dl class="definition-list"><dt>案例版本</dt><dd>${esc(t.case?.version)}</dd><dt>案例哈希</dt><dd class="mono">${esc(t.case_hash)}</dd><dt>配置哈希</dt><dd class="mono">${esc(t.configuration_hash)}</dd></dl>${provenanceSummary(t.configuration?.provenance,t.configuration?.parameters)}<details><summary>源码、提示词与资源</summary>${json(t.configuration?.provenance)}</details>${t.platform?`<details><summary>平台同步 · ${esc(label(t.platform.status))}</summary>${json(t.platform)}${button('重新同步','publish','small','refresh')}</details>`:''}</div></div><details><summary>管理员原始输出</summary>${json(r.output)}</details>`;
  }
  return html;
}
function taskpackCards(){
  return `<div class="dataset-grid">${state.catalog.map(p=>`<article class="panel dataset-card"><div class="action-row"><span class="tag">${esc(label(p.project))}</span><span class="tag">v${esc(p.version)}</span></div><h3>${esc(p.name)}</h3><p>${esc(p.description||'共享真实产品边界的可执行任务包')}</p><div class="dataset-meta"><span><strong>${p.cases.length}</strong>案例</span><span><strong>${countFamilies(p.cases)}</strong>情境家族</span><span><strong>${p.configurations.length}</strong>方案</span></div><details><summary>材料、量表与适配能力</summary><p class="source-note">${esc(sourceTypes(p.cases))}</p><ul>${p.cases.map(c=>`<li class="text-xs" >${esc(c.name)} <span class="muted">${esc(label(c.split||'development'))}</span></li>`).join('')}</ul><div class="rubric">${(p.rubric||[]).map(r=>`<span title="${esc(r.description)}">${esc(r.name)}</span>`).join('')}</div>${p.configurations.map(c=>`<details><summary>${esc(c.name)} · ${esc(label(c.mode))}</summary>${provenanceSummary(c.provenance,c.parameters)}<details><summary>完整来源与请求参数</summary>${json(c.provenance||{parameters:c.parameters})}</details></details>`).join('')}</details><div class="dataset-card-foot"><span class="source-note m-0" >不可变实验快照</span><button class="small" data-action="new" data-project="${esc(p.project)}" data-pack="${esc(p.id)}">用于新实验 ${icon('arrow')}</button></div></article>`).join('')}</div>`;
}
async function datasetsPage(ctx,tab='versions'){
  const result=await optional(ctx,'/api/datasets');const rows=result.value?.datasets||[];
  let html=pageTitle('数据与任务包','',button('导入数据集版本','import-dataset','primary','upload'));
  const active=tab==='packs'?'packs':'versions';html+=tabs([['versions','版本化数据集','dataset','#datasets',rows.length],['packs','可执行任务包','layers','#datasets/packs',state.catalog.length]],active);
  if(active==='packs')return html+taskpackCards();
  if(result.error)return html+unavailable('数据集服务暂未连接',result.error)+`<section class="section">${sectionHead('已注册任务包','')}${taskpackCards()}</section>`;
  html+=rows.length?`<div class="dataset-grid">${rows.map(d=>`<article class="panel dataset-card"><div class="action-row"><span class="tag">${esc(label(d.project))}</span>${d.source?`<span class="tag">${esc(sourceLabel(d))}</span>`:''}</div><h3><a class="cell-title text-lg"  href="${routeUrl('dataset',d.id)}">${esc(d.name||d.id)}</a></h3><p>${esc(d.notes||d.description||'版本不可覆盖；新材料进入新版本。')}</p><div class="dataset-meta"><span><strong>${number(d.version_count??d.versions?.length)}</strong>版本</span><span><strong>${number(d.case_count??d.latest_version?.case_count)}</strong>案例</span><span><strong>${number(d.family_count??d.latest_version?.family_count)}</strong>家族</span></div><div class="dataset-card-foot"><span class="source-note m-0" >${date(d.updated_at||d.created_at)}</span><a class="button small" href="${routeUrl('dataset',d.id)}">查看版本 ${icon('arrow')}</a></div></article>`).join('')}</div>`:`<div class="panel">${empty('暂无数据集版本','',button('导入首个版本','import-dataset','primary','upload')+' <a class="button" href="#datasets/packs">浏览任务包</a>','dataset')}</div>`;
  return html+`<p class="intro-note">${icon('lock')}<span>同一版本内容不可覆盖。合成、许可公开与授权私有材料分别标注；家族变式不得跨开发与留出组泄漏。</span></p>`;
}
async function datasetPage(ctx,id,versionId=''){
  const response=await ctx.get('/api/datasets/'+encodeURIComponent(id)),d=response.dataset||response,versions=d.versions||response.versions||[];
  const selected=versionId||versions[0]?.version_id||versions[0]?.id||d.latest_version_id;
  const raw=selected?await ctx.get(`/api/datasets/${encodeURIComponent(id)}/versions/${encodeURIComponent(selected)}`):d;
  const v=raw.version_data||raw.dataset_version||raw,pack=v.taskpack||v.content||v, cases=pack.cases||[];
  ctx.current={...d,_kind:'dataset',selectedVersion:selected,version:raw};
  return `<a class="back" href="#datasets">${icon('back')}全部数据集</a>`+pageTitle(d.name||id,label(d.project),iconButton('导出此版本','export-dataset','download')+button('用于新实验','new','primary','plus',`data-dataset="${esc(id)}" data-version="${esc(selected||'')}"`))+
    `<div class="version-list">${versions.map(item=>{const key=item.version_id||item.id;return `<a class="button small ${key===selected?'primary':''}" href="${routeUrl('dataset',id,key)}">v${esc(item.version||key)}</a>`;}).join('')}</div>`+
    `<div class="metric-strip">${metric('案例',number(cases.length),'版本内材料条目')}${metric('情境家族',number(countFamilies(cases)),'变式不增加家族数')}${metric('开发案例',number(cases.filter(c=>c.split==='development').length),'仅按声明标签计数')}${metric('留出案例',number(cases.filter(c=>c.split==='holdout').length),'独立性仍需来源审计')}</div>`+
    `<div class="panel panel-pad dataset-source"><div class="section-head"><h3 class="m-0">版本来源</h3><span class="tag">v${esc(v.version||pack.version||'—')}</span></div><dl class="definition-list"><dt>材料来源</dt><dd>${Object.entries(v.source_types||d.source_types||{}).length?Object.entries(v.source_types||d.source_types).map(([type,n])=>`${esc(label(type))} · ${n} 案例`).join('；'):esc(sourceTypes(cases))}</dd>${pack.notes||v.notes?`<dt>说明</dt><dd>${esc(pack.notes||v.notes)}</dd>`:''}${pack.authorization||v.authorization?'<dt>授权</dt><dd>授权声明已记录</dd>':''}</dl><details><summary>版本哈希、详细来源与许可</summary>${json({version_id:v.version_id||v.id,content_hash:v.content_hash,source:pack.source||v.source,authorization:pack.authorization||v.authorization,source_types:v.source_types})}</details></div>`+
    `<section class="section">${sectionHead('材料清单',sourceTypes(cases))}<div class="panel table-wrap"><table><thead><tr><th>案例</th><th>情境家族</th><th>分组</th><th>来源</th><th>版本</th></tr></thead><tbody>${cases.map(c=>`<tr><td><details class="m-0" ><summary class="case-summary" >${esc(c.name||c.id)}</summary>${evidenceContent(readableInput(c.input))}<details><summary>边界与来源</summary>${json({expected_behavior:c.expected_behavior,source:c.source})}</details></details></td><td class="mono">${esc(c.family_id||c.id)}</td><td><span class="tag ${c.split==='holdout'?'holdout':''}">${esc(label(c.split||'未分组'))}</span></td><td>${esc(sourceLabel(c))}</td><td>${esc(c.version||'—')}</td></tr>`).join('')||'<tr><td colspan="5">当前版本没有返回案例明细</td></tr>'}</tbody></table></div></section>`;
}
const decisionCheckNames={observed_terminal_evidence:'已保存的执行终态',families:'独立情境家族',completion_rate:'执行完成率',success_rate:'执行成功率',holdout_only:'留出材料范围',holdout_independence:'留出独立性声明',rule_coverage:'自动规则覆盖',rule_failures:'自动规则失败',model_coverage:'模型评价覆盖',model_paired_coverage:'模型评价配对覆盖',model_paired_delta:'候选相对基线的变化',human_review_coverage:'真实人工评价覆盖',quality_criterion_required:'质量维度门槛声明',model_mean:'模型评价均值'};
function decisionValue(check,value,{threshold=false}={}){
  if(value==null)return '未记录';
  if(['completion_rate','success_rate','model_coverage','model_paired_coverage'].includes(check.code)&&typeof value==='number')return (value*100).toLocaleString('zh-CN',{maximumFractionDigits:2})+'%';
  if(check.code==='model_paired_delta'&&value&&typeof value==='object')return `${value.mean==null?'均值未记录':'均值 '+Number(value.mean).toLocaleString('zh-CN',{maximumFractionDigits:6})} · ${value.family_count??'未知'} 个家族 · ${value.trial_count??'未知'} 项运行记录`;
  if(check.code==='holdout_only'&&typeof value==='object')return Object.entries(value).map(([key,n])=>`${label(key)} ${n}`).join(' · ');
  if(check.code==='holdout_independence'&&typeof value==='object')return `${value.declaration==='declared_unexposed'?'声明未暴露':value.declaration||'声明未记录'} · 本次已暴露家族 ${value.selected_exposed_families?.length??'未知'}`;
  if(value==='all holdout')return '全部为留出材料';
  if(value==='selected holdout families declared unexposed')return '本次家族声明未向开发暴露';
  if(typeof value==='object')return Array.isArray(value)?`${value.length} 项，展开完整记录查看`:`${Object.keys(value).length} 个字段，展开完整记录查看`;
  return String(value);
}
function decisionList(decisions,configurations=[]){
  if(!decisions.length)return `<div class="panel">${empty('尚未作出发布判定','',button('建立判定','new-decision','primary','shield'),'shield')}</div>`;
  const names=Object.fromEntries(configurations.map(c=>[c.id,c.name]));
  return decisions.map(d=>{
    const groups=new Map();for(const check of d.checks||[]){const key=check.configuration_id||'';if(!groups.has(key))groups.set(key,[]);groups.get(key).push(check);}
    const unmet=(d.checks||[]).filter(c=>!['pass','passed'].includes(c.status)),unmetGroups=new Map();for(const c of unmet){const key=JSON.stringify([c.code,c.status,c.actual,c.threshold,c.message,c.evidence]);if(!unmetGroups.has(key))unmetGroups.set(key,{check:c,candidates:[]});unmetGroups.get(key).candidates.push(names[c.configuration_id]||c.configuration_id||'全局');}
    return `<article class="panel panel-pad section decision-report"><div class="section-head"><div><h2>${esc(d.policy?.name||'质量与执行门槛')}</h2><p>${date(d.created_at)} · ${d.scope==='execution'?'执行门槛':'质量门槛'}</p></div><div class="action-row">${badge(d.status)}${d.evidence_current===false?'<span class="tag">历史判定 · 证据已更新</span>':''}</div></div>${d.evidence_current===false?note('这份判定绑定旧证据。后续输出或评价已经变化，旧记录仍保留。','warning'):''}<div class="decision-outcome"><strong>${esc(label(d.status))}</strong><p>${d.status==='inconclusive'?'当前证据未满足冻结策略要求，暂不能据此作出通过结论。':d.status==='blocked'?'存在未通过的门槛，具体原因保留如下。':'判定按保存的策略与证据生成；不据此修改产品默认。'}</p>${unmetGroups.size?`<ul>${[...unmetGroups.values()].map(({check:c,candidates})=>`<li><strong>${esc(decisionCheckNames[c.code]||c.code)}</strong>：${esc(decisionValue(c,c.actual))} / 要求 ${esc(decisionValue(c,c.threshold,{threshold:true}))} · ${esc(label(c.status))}<span>${esc(candidates.join('、'))}</span><p>${esc(c.message||'')}</p></li>`).join('')}</ul>`:(d.checks||[]).length?'<p>本记录中的检查项均已通过。适用范围仍以冻结策略和完整记录为准。</p>':'<p>此记录未携带逐项检查值，请展开完整判定依据。</p>'}</div>${[...groups].map(([id,checks])=>{const candidate=(Array.isArray(d.candidates)?d.candidates:[]).find(c=>c.configuration_id===id),passed=checks.filter(c=>['pass','passed'].includes(c.status)).length;return `<details class="decision-candidate"><summary><strong>${esc(names[id]||id||'全局检查')}</strong><span>${passed} / ${checks.length} 项通过</span>${candidate?.status?badge(candidate.status):''}</summary><div class="decision-checks">${checks.map(c=>`<section class="decision-check"><div class="decision-check-head"><strong>${esc(decisionCheckNames[c.code]||c.code)}</strong>${badge(c.status)}</div><p><span>实际</span> ${esc(decisionValue(c,c.actual))}<br><span>门槛</span> ${esc(decisionValue(c,c.threshold,{threshold:true}))}</p><p class="source-note">${esc(c.message||'')}</p><details><summary>完整检查值与依据</summary>${json(c)}</details></section>`).join('')}</div></details>`;}).join('')}${!groups.size?evidenceContent(d.candidates||d.results||{}):''}<details><summary>冻结策略与完整判定依据</summary>${json(d)}</details></article>`;
  }).join('');
}

async function queuePage(ctx){
  const [queueResult,backupResult,importsResult]=await Promise.all([optional(ctx,'/api/queue'),optional(ctx,'/api/backups'),optional(ctx,'/api/imports')]);const q=queueResult.value;ctx.current={...q,_kind:'queue'};
  let html=pageTitle('运行与恢复','',button('创建备份','backup','','download'));
  if(q){
    html+=`<div class="panel panel-pad"><div class="section-head"><div><h2>${q.paused?'队列已暂停':'队列正在调度'}</h2><p>暂停不取消在途请求；继续调度由你明确触发。</p></div>${button(q.paused?'恢复队列':'暂停队列','toggle-queue',q.paused?'primary':'',q.paused?'play':'pause')}</div><div class="form-grid"><div><dl class="definition-list"><dt>当前并发</dt><dd>${number(q.concurrency)}</dd><dt>活跃运行</dt><dd>${number(Array.isArray(q.active)?q.active.length:q.counts?.running)}</dd></dl></div><form id="concurrency-form" class="action-row"><label class="inline-grow-label" >调整并发<input type="number" name="concurrency" value="${Number(q.concurrency)||1}" min="1" max="${q.limits?.max_concurrency||32}" required></label><button class="self-end" type="submit" >保存</button></form></div></div>`;
    html+=`<section class="section">${sectionHead('队列快照','')}<div class="panel panel-pad">${content(q.counts||{})}<details><summary>活跃运行与预算限制</summary>${json({active:q.active,experiments:q.experiments,limits:q.limits})}</details></div></section>`;
  }else html+=unavailable('运行队列暂未连接',queueResult.error);
  const backups=backupResult.value?.backups||[];
  html+=`<section class="section">${sectionHead('一致性备份','',button('创建备份','backup','small','plus'))}${backupResult.error?unavailable('备份暂未连接',backupResult.error):`<div class="panel">${backups.length?`<div class="table-wrap"><table><thead><tr><th>备份</th><th>建立时间</th><th>完整性</th><th></th></tr></thead><tbody>${backups.map(b=>`<tr><td><strong>${esc(b.label||b.name||b.id)}</strong><div class="subline mono">${esc(b.sha256||b.manifest_hash||'')}</div></td><td>${date(b.created_at)}</td><td><span class="tag">${b.verification?.manifest==='verified'?'清单已核对':'待核对'}</span><div class="subline">文件：${b.verification?.files==='verified'?'已校验':'恢复时校验'}</div></td><td><button class="small" data-action="restore" data-id="${esc(b.id)}">恢复到新目录 ${icon('arrow')}</button></td></tr>`).join('')}</tbody></table></div>`:empty('还没有备份','',button('创建首个备份','backup','small','plus'),'layers',true)}</div>`}</section>`;
  const imports=importsResult.value?.imports||[];
  html+=`<section class="section">${sectionHead('历史轨迹导入','',button('导入轨迹','import-traces','small','upload'))}${importsResult.error?unavailable('导入记录暂未连接',importsResult.error):`<div class="panel">${imports.length?`<div class="table-wrap"><table><thead><tr><th>导入记录</th><th>来源</th><th>建立时间</th><th></th></tr></thead><tbody>${imports.map(i=>`<tr><td>${esc(i.title||i.id)}</td><td>${esc(importSource(i))}<div class="subline mono">${esc(i.id?.slice(0,8)||'')}</div></td><td>${date(i.created_at)}</td><td>${i.experiment_id?`<a class="text-link" href="${routeUrl('experiment',i.experiment_id)}">查看实验 ${icon('arrow')}</a>`:`<details><summary>详情</summary>${json(i)}</details>`}</td></tr>`).join('')}</tbody></table></div>`:empty('尚无导入记录','','','upload',true)}</div>`}</section>`;
  return html;
}
async function guidePage(ctx){
  const steps=[['准备材料','选择版本化任务包或数据集，核对来源、许可和情境家族。开发集用于改方案，留出集在方案冻结后用于独立验证。'],['建立实验','选择唯一基线与多个候选，设定重复、超时和预算。核对规模后启动，也可以先建立并暂停。'],['查看交付与过程','先看实际交付，再追查输入、阶段与执行尝试。失败、拒绝、超时和取消始终进入运行分母。'],['独立评分与盲评','模型评分绑定具体输出；匿名评审不显示方案身份。验收试填、内部 AI 与真实人工评价分别记录。'],['比较并作出判定','分别看执行、质量与性能，按情境家族判断差异。覆盖不足、分歧或缺失时保留不确定性。'],['导出与恢复','导出完整证据；在大规模变更前备份。恢复只写到新目录，已有证据不会被覆盖。']];
  const playback=await playbookPanel(ctx);
  return pageTitle('操作手册','',newButton())+
    `<div class="guide-grid"><div>${playback}<section class="panel panel-pad guide"><h2>完成一次可信的比较</h2>${steps.map(([title,description],i)=>`<div class="guide-step"><span>0${i+1}</span><div><h3>${esc(title)}</h3><p>${esc(description)}</p></div></div>`).join('')}</section><section class="panel panel-pad guide section"><h2>如何理解数字</h2><p><strong>案例</strong>是材料条目；<strong>情境家族</strong>是独立问题来源。同一故事的变式和重复不会增加独立家族数。一次运行对应案例 × 方案 × 重复，重试只新增执行尝试。</p><p><strong>执行成功</strong>表示声明的业务路径完成。自动规则检查边界，独立模型评价判断内容，人工评价记录真实偏好；它们不合成一个总分。</p><p>未观测用量和费用显示未知。请求模型和思考强度与供应商回显分别保存，未回显不视为已验证生效。</p><h2>暂停、停止与重评</h2><p>暂停只停止后续调度。停止会请求取消在途任务；本地进程终止不证明供应商停止计费。重启后的中断保留，明确重试才会发起新请求。重评分读取历史输出，不重新调用产品。</p><h2>结果的适用边界</h2><p>文本 HTTP 结果不能替代原生 App、真实语音、相机与真人设备验收。数据标为留出不自动证明独立；来源、创建者与候选冻结记录仍需复核。内部 AI 校准不等同专家或用户金标。</p></section></div><aside><div class="panel guide-nav"><h3>常用入口</h3><a href="#experiments">${icon('experiments')} 查找实验</a><a href="#datasets">${icon('dataset')} 准备材料与版本</a><a href="#jobs">${icon('score')} 独立评分与重评</a><a href="#queue">${icon('activity')} 暂停、备份与恢复</a></div><div class="notice section">本机开发工作区。外部部署、真实用户流量和产品默认切换需要对应的明确动作与验收。</div></aside></div>`;
}

function wizardPack(){return state.wizard?.datasetId?state.wizard.datasetPack:state.catalog.find(p=>p.id===state.wizard?.packId&&p.project===state.wizard?.project);}
function wizardCases(){const w=state.wizard,p=wizardPack();return (p?.cases||[]).filter(c=>w.split==='all'||c.split===w.split);}
function wizardSelectedCases(){const ids=new Set(state.wizard.caseIds);return wizardCases().filter(c=>ids.has(c.id));}
function wizardScope(){const w=state.wizard,cases=wizardSelectedCases(),configs=1+w.candidates.filter(id=>id!==w.baseline).length;return {cases,families:countFamilies(cases),configs,trials:cases.length*configs*w.repeats};}
function resetWizardPack(){const w=state.wizard,p=wizardPack();w.caseIds=wizardCases().map(c=>c.id);w.baseline=p?.configurations?.[0]?.id||'';w.candidates=p?.configurations?.[1]?[p.configurations[1].id]:[];}
async function loadWizardDataset(id,versionId=''){
  const w=state.wizard,request=(w.sourceRequest||0)+1;w.sourceRequest=request;w.sourceLoading=true;
  const form=modal.querySelector('#wizard-form');if(form){form.setAttribute('aria-busy','true');form.querySelector('[type=submit]').disabled=true;if($('form-error'))$('form-error').textContent='正在读取所选材料，完成后才能进入下一步。';}
  try{
  const response=await api('/api/datasets/'+encodeURIComponent(id)),d=response.dataset||response,versions=d.versions||response.versions||[];
  if(state.wizard!==w||request!==w.sourceRequest||!modal.open)return false;
  const selected=versionId||versions[0]?.version_id||versions[0]?.id||d.latest_version_id;
  if(!selected)throw new Error('数据集没有可读取的版本');
  const raw=await api(`/api/datasets/${encodeURIComponent(id)}/versions/${encodeURIComponent(selected)}`);
  if(state.wizard!==w||request!==w.sourceRequest||!modal.open)return false;
  const v=raw.version_data||raw.dataset_version||raw;
  w.datasetVersions=versions;w.versionId=selected;w.datasetPack=v.taskpack||v.content||v;w.datasetId=id;w.project=w.datasetPack.project||d.project;w.packId=w.datasetPack.taskpack_id||w.datasetPack.id;resetWizardPack();return true;
  }finally{if(state.wizard===w&&request===w.sourceRequest){w.sourceLoading=false;const current=modal.querySelector('#wizard-form');if(current){current.removeAttribute('aria-busy');current.querySelector('[type=submit]').disabled=false;if($('form-error')?.textContent==='正在读取所选材料，完成后才能进入下一步。')$('form-error').textContent='';}}}
}
async function openCreate({project='',packId='',datasetId='',versionId=''}={}){
  openModal(modalHead('新建实验','正在读取材料与已注册方案…')+`<div class="dialog-body"><div class="page-loading loading-short" ><span class="spinner"></span></div></div>`);
  const modalTicket=state.modalVersion;
  try{
    const [catalogResult,datasetsResult]=await Promise.allSettled([api('/api/catalog'),api('/api/datasets')]);
    if(!modal.open||modalTicket!==state.modalVersion)return;
    if(catalogResult.status==='rejected')throw catalogResult.reason;
    state.catalog=catalogResult.value.taskpacks||[];state.capabilities=catalogResult.value.capabilities||{};state.limits=catalogResult.value.limits||{};state.datasets=datasetsResult.status==='fulfilled'?datasetsResult.value.datasets||[]:[];
    const p=state.catalog.find(p=>p.id===packId)||state.catalog.find(p=>p.project===project)||state.catalog[0];
    if(!p&&!datasetId)throw new Error('当前没有可执行任务包，请先接入项目和配置。');
    state.wizard={step:0,project:p?.project||project,packId:p?.id||'',datasetId:'',versionId:'',datasetPack:null,datasetVersions:[],split:'all',caseIds:[],baseline:'',candidates:[],title:'',repeats:1,timeout:300,budget:{},startPaused:false};
    const w=state.wizard;if(datasetId){if(!await loadWizardDataset(datasetId,versionId))return;}else resetWizardPack();
    if(state.wizard!==w||!modal.open||modalTicket!==state.modalVersion)return;
    renderWizard();
  }catch(error){if(!modal.open||modalTicket!==state.modalVersion)return;openModal(modalHead('暂时无法建立实验')+`<div class="dialog-body">${note(error.message,'error')}</div><div class="dialog-footer">${button('关闭','close')}</div>`);}
}
function captureWizard(){
  const w=state.wizard,form=$('wizard-form');if(!w||!form)return;
  const f=new FormData(form);
  if(w.step===0){w.caseIds=f.getAll('case_ids');w.split=f.get('split')||w.split;}
  if(w.step===1){w.baseline=f.get('baseline_id')||w.baseline;w.candidates=f.getAll('candidate_ids').filter(id=>id!==w.baseline);}
  if(w.step===2){w.title=String(f.get('title')||'').trim();w.repeats=Number(f.get('repeats'));w.timeout=Number(f.get('timeout_seconds'));w.startPaused=f.has('start_paused');w.budget={};for(const key of ['max_trials','max_attempts','max_elapsed_seconds','max_total_tokens','max_known_cost','max_concurrency']){const value=f.get(key);if(value!==null&&String(value).trim()!=='')w.budget[key]=Number(value);}}
}
function scopeMarkup(){const scope=wizardScope();return `<span><strong>${scope.cases.length}</strong>案例</span><span class="separator"></span><span><strong>${scope.families}</strong>家族</span><span class="separator"></span><span><strong>${scope.configs}</strong>方案</span><span class="separator"></span><span><strong>${scope.trials}</strong>运行</span>`;}
function updateWizardScope(){const el=$('wizard-scope');if(el)el.innerHTML=scopeMarkup();const footer=$('wizard-footer-scope');if(footer){const s=wizardScope();footer.textContent=`${s.cases.length} 案例 × ${s.configs} 方案 × ${state.wizard.repeats} 次`;}}
function renderWizard(){
  const w=state.wizard,p=wizardPack(),scope=wizardScope(),projects=[...new Set(state.catalog.map(p=>p.project))],steps=['选择材料','比较方案','执行设置','核对建立'];
  const sameStep=w.renderedStep===w.step,active=modal.contains(document.activeElement)?document.activeElement:null;
  const focus={focusId:sameStep&&active?.id?active.id:'wizard-step-heading',focusAction:sameStep&&!active?.id?active?.dataset.action||'':'',bodyScroll:sameStep?modal.querySelector('.dialog-body')?.scrollTop||0:0};
  if(focus.focusAction)focus.focusId='';w.renderedStep=w.step;
  let body='';
  if(w.step===0){
    body=`<div class="form-grid"><label>项目<select id="wizard-project" name="project" ${w.datasetId?'disabled':''}>${projects.map(project=>opt(project,label(project),project===w.project)).join('')}</select></label><label>材料来源<select id="wizard-source-kind"><option value="pack" ${!w.datasetId?'selected':''}>已注册任务包</option><option value="dataset" ${w.datasetId?'selected':''} ${state.datasets.length?'':'disabled'}>版本化数据集${state.datasets.length?'':'（暂无版本）'}</option></select></label></div>`;
    if(w.datasetId){body+=`<div class="form-grid"><label>数据集<select id="wizard-dataset">${state.datasets.map(d=>opt(d.id,d.name||d.id,d.id===w.datasetId)).join('')}</select></label><label>固定版本<select id="wizard-version">${w.datasetVersions.map(v=>opt(v.version_id||v.id,'v'+(v.version||v.version_id||v.id),(v.version_id||v.id)===w.versionId)).join('')}</select></label></div>`;}
    else body+=`<label>任务包<select id="wizard-pack" name="taskpack_id">${state.catalog.filter(p=>p.project===w.project).map(p=>opt(p.id,`${p.name} · v${p.version}`,p.id===w.packId)).join('')}</select></label>`;
    body+=`<div class="action-row"><span class="tag">${esc(sourceTypes(p?.cases))}</span>${p?.description?help('任务包说明',p.description):''}</div><div class="wizard-section"><div class="wizard-section-head"><h3>选择案例</h3><div class="action-row"><select id="wizard-split" name="split" class="filter-select" aria-label="选择开发或留出分组">${opt('all','全部分组',w.split==='all')}${['development','holdout'].map(s=>opt(s,label(s),s===w.split)).join('')}</select>${button('全选','select-all-cases','quiet small')}${button('清空','clear-cases','quiet small')}</div></div><div class="case-choices">${wizardCases().map(c=>`<label class="check-row"><input type="checkbox" name="case_ids" value="${esc(c.id)}" ${w.caseIds.includes(c.id)?'checked':''}><span>${esc(c.name||c.id)}<small>${esc(label(c.task||''))}${c.family_id?' · 家族 '+esc(c.family_id):''} · ${esc(label(c.split||'未分组'))}</small></span></label>`).join('')||'<div class="notice">当前分组没有案例，选择其他分组继续。</div>'}</div></div>`;
  }
  if(w.step===1){
    body=`<div class="wizard-section"><label>基线方案<select name="baseline_id" id="wizard-baseline">${(p?.configurations||[]).map(c=>opt(c.id,c.name,c.id===w.baseline)).join('')}</select></label></div><div class="wizard-section"><div class="wizard-section-head"><h3>选择候选方案</h3><span class="muted text-tiny" >至少 1 个，最多 15 个</span></div><div class="config-choices">${(p?.configurations||[]).filter(c=>c.id!==w.baseline).map(c=>`<label class="config-choice"><input type="checkbox" name="candidate_ids" value="${esc(c.id)}" ${w.candidates.includes(c.id)?'checked':''}><span><strong>${esc(c.name)}</strong><small>${esc(label(c.mode))}</small><small>${esc(Object.entries(c.parameters||{}).filter(([k])=>['model','reasoning_effort','profile','profile_id','thinking_level'].includes(k)).map(([k,v])=>`${k}: ${v}`).join(' · ')||'使用冻结的产品配置')}</small></span></label>`).join('')}</div></div><details><summary>查看所选方案的请求配置</summary>${(p?.configurations||[]).filter(c=>c.id===w.baseline||w.candidates.includes(c.id)).map(c=>`<h4>${esc(c.name)}</h4>${json(c.parameters)}`).join('')}</details>`;
  }
  if(w.step===2){
    body=`<label class="mt-0" >实验名称<input name="title" value="${esc(w.title)}" placeholder="如：理解当前需求 · 三方案对照" maxlength="160" required></label><div class="form-grid"><label>每个方案重复次数<input name="repeats" type="number" min="1" max="${state.limits?.max_repeats||20}" value="${w.repeats}" required></label><label>每项运行超时（秒）<input name="timeout_seconds" type="number" min="5" max="${state.limits?.max_timeout_seconds||86400}" value="${w.timeout}" required></label></div><label>本实验最大并发<input type="number" name="max_concurrency" min="1" max="${state.limits?.max_concurrency||32}" value="${w.budget.max_concurrency??1}" required></label><label class="checkbox-inline"><input type="checkbox" name="start_paused" ${w.startPaused?'checked':''}>先建立实验，暂不开始调度</label><details><summary>预算与保护上限（可选）</summary><p class="form-help">留空使用服务默认值。未知用量与费用不视为零；在途调用可能越过观察到的上限。</p><div class="form-grid"><label>运行总量上限<input type="number" min="1" name="max_trials" value="${w.budget.max_trials??''}" placeholder="默认由服务约束"></label><label>执行尝试总上限<input type="number" min="1" name="max_attempts" value="${w.budget.max_attempts??''}" placeholder="默认由服务约束"></label><label>实验耗时上限（秒）<input type="number" min="1" name="max_elapsed_seconds" value="${w.budget.max_elapsed_seconds??''}" placeholder="可选"></label><label>已观察 tokens 上限<input type="number" min="1" name="max_total_tokens" value="${w.budget.max_total_tokens??''}" placeholder="可选"></label><label>已知费用上限<input type="number" min="0" step="any" name="max_known_cost" value="${w.budget.max_known_cost??''}" placeholder="仅约束已知记录"><small>服务未声明统一币种或计价单位。按原始费用数值累计；缺失费用无法靠此上限控制，请同时设置运行、时间与 tokens 上限。</small></label></div></details>`;
  }
  if(w.step===3){
    const baseline=p?.configurations?.find(c=>c.id===w.baseline),candidates=(p?.configurations||[]).filter(c=>w.candidates.includes(c.id));
    body=`<p class="form-help">${w.startPaused?'恢复调度后才发送产品请求。':'建立后将发送真实产品请求。'}</p><div class="panel-inset my-20" ><dl class="review-summary"><dt>实验名称</dt><dd><strong>${esc(w.title)}</strong></dd><dt>项目与材料</dt><dd>${esc(label(w.project))} · ${esc(p?.name)}<br><span class="muted">${w.datasetId?'数据集版本 '+esc(w.versionId):'任务包 v'+esc(p?.version)} · ${w.split==='all'?'全部分组':esc(label(w.split))}中的 ${scope.cases.length} / ${(p?.cases||[]).length} 个已选案例</span></dd><dt>基线</dt><dd><span class="tag baseline">${esc(baseline?.name)}</span></dd><dt>候选</dt><dd>${candidates.map(c=>`<span class="tag">${esc(c.name)}</span>`).join(' ')}</dd><dt>材料来源</dt><dd>${esc(sourceTypes(scope.cases))}</dd><dt>执行设置</dt><dd>每方案重复 ${w.repeats} 次 · 每项上限 ${w.timeout} 秒<br>本实验并发上限 ${w.budget.max_concurrency??1}</dd><dt>预算上限</dt><dd>${Object.entries(w.budget).filter(([key])=>key!=='max_concurrency').map(([key,value])=>`${esc(({max_trials:'运行总量',max_attempts:'执行尝试总量',max_elapsed_seconds:'实验耗时（秒）',max_total_tokens:'已观察 tokens',max_known_cost:'已知费用（计价单位未声明）'})[key]||key)}：${number(value)}`).join('<br>')||'其余预算使用服务默认值'}</dd><dt>启动方式</dt><dd>${w.startPaused?'建立后保持暂停，等待你恢复调度':'建立后开始调度真实请求'}</dd></dl></div>${help('冻结与校验','输入与配置在建立时冻结。执行前若检测到源码变化，需新建实验。')}`;
  }
  const html=modalHead('新建实验')+
    `<div class="wizard-steps">${steps.map((title,i)=>`<div class="wizard-step ${i===w.step?'current':i<w.step?'done':''}" ${i===w.step?'aria-current="step"':''}><span>${i<w.step?icon('check'):i+1}</span>${title}</div>`).join('')}</div><form id="wizard-form"><div class="dialog-body">${body}<div class="scope-summary" id="wizard-scope">${scopeMarkup()}</div><p id="form-error" class="form-error" role="alert"></p></div><div class="dialog-footer"><span class="muted" id="wizard-footer-scope">${scope.cases.length} 案例 × ${scope.configs} 方案 × ${w.repeats} 次</span><div class="action-row">${w.step?button('上一步','wizard-back','','back','type="button"'):button('取消','close','','','type="button"')}<button class="primary" type="submit">${w.step===3?(w.startPaused?'建立并暂停':'建立并启动'):'下一步'}${icon(w.step===3?(w.startPaused?'pause':'play'):'arrow')}</button></div></div></form>`;
  openModal(html,false,focus);
  const bodyElement=modal.querySelector('.dialog-body'),heading=document.createElement('h3');heading.id='wizard-step-heading';heading.tabIndex=-1;heading.className='wizard-step-heading';heading.textContent=`第 ${w.step+1} 步 · ${steps[w.step]}`;bodyElement.prepend(heading);
  for(const [selector,title,description]of [['.case-choices','本次评测案例','选择后在核对页确认实际数量。'],['.config-choices','候选方案','至少选择1个，最多15个。']]){const group=modal.querySelector(selector);if(group){const id=selector.slice(1)+'-legend';const legend=document.createElement('p');legend.id=id;legend.className='sr-only';legend.textContent=title+'。'+description;group.before(legend);group.setAttribute('role','group');group.setAttribute('aria-labelledby',id);group.setAttribute('aria-describedby','form-error');}}
  if(w.sourceLoading){const form=modal.querySelector('#wizard-form');form.setAttribute('aria-busy','true');form.querySelector('[type=submit]').disabled=true;$('form-error').textContent='正在读取所选材料，完成后才能进入下一步。';}
  if(w.step===2&& !w.title){const input=modal.querySelector('[name=title]');input.value=`${label(w.project)} · ${new Date().toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit'})} 方案比较`;}
}
async function submitWizard(form){
  captureWizard();const w=state.wizard,p=wizardPack();if(w.sourceLoading)throw new Error('所选材料仍在读取，请完成后再继续。');$('form-error').textContent='';
  if(w.step===0&& !w.caseIds.length)throw new Error('请至少选择一个案例。');
  if(w.step===1&&(!w.baseline||!w.candidates.length||w.candidates.length>15))throw new Error('请选择一个基线和 1–15 个不同候选。');
  if(w.step===2&&(!w.title||!Number.isInteger(w.repeats)||w.repeats<1||!Number.isFinite(w.timeout)))throw new Error('请补全实验名称、有效重复次数与超时。');
  if(w.step===2&&state.limits?.max_trials_per_experiment&&wizardScope().trials>state.limits.max_trials_per_experiment)throw new Error(`计划 ${wizardScope().trials} 项运行，超过服务当前 ${state.limits.max_trials_per_experiment} 项上限，请减少案例或重复次数。`);
  if(w.step<3){w.step++;renderWizard();return;}
  const payload={title:w.title,purpose:w.split==='holdout'?'holdout_evaluation':w.split==='development'?'development_evaluation':'evaluation',project:w.project,taskpack_id:p.id,case_ids:w.caseIds,baseline_id:w.baseline,configuration_ids:[w.baseline,...w.candidates],repeats:w.repeats,timeout_seconds:w.timeout,split:w.split,start_paused:w.startPaused,...(Object.keys(w.budget).length?{budget:w.budget}:{})};
  if(w.datasetId){payload.dataset_id=w.datasetId;payload.dataset_version_id=w.versionId;}
  const e=await api('/api/experiments',payload);closeModal();filters.case='';filters.trialStatus='';location.hash=routeUrl('experiment',e.id);toast(w.startPaused?'实验已建立，调度保持暂停':'实验已建立，开始执行真实业务请求');
}
async function openCompare(caseId='',candidateId=''){
  const e=state.current;if(!e?.trials)throw new Error('请从实验详情进入结果对照。');
  const cases=Object.values(Object.fromEntries(e.trials.map(t=>[t.case_id,{id:t.case_id,name:t.case_name}]))),baseline=e.baseline_id||e.configurations[0].id,candidates=e.configurations.filter(c=>c.id!==baseline);
  state.compare={e,caseId:caseId||cases[0]?.id,candidateId:candidateId||candidates[0]?.id,repeat:1,request:0,view:'output',sync:true,results:[],side:0,positions:{},openDetails:{},referenceOpen:false,layoutMobile:compareMobile(),resizing:false,layoutRevision:0};
  openModal(modalHead('对照实际交付')+`<div class="compare-controls"><details class="compare-selector" ${window.matchMedia('(max-width:760px)').matches?'':'open'}><summary><span id="compare-selection-label">更换案例与方案</span><span>更换 ${icon('chevron')}</span></summary><div class="compare-toolbar"><label>案例<select id="compare-case" aria-label="选择对照案例">${cases.map(c=>opt(c.id,c.name,c.id===state.compare.caseId)).join('')}</select></label><label>候选<select id="compare-candidate" aria-label="选择候选">${candidates.map(c=>opt(c.id,c.name,c.id===state.compare.candidateId)).join('')}</select></label><label class="repeat-field">重复<select id="compare-repeat" aria-label="选择重复轮次">${Array.from({length:e.repeats||1},(_,i)=>opt(i+1,i+1)).join('')}</select></label></div></details><div class="compare-view-bar"><div class="segmented" role="group" aria-label="对照内容"><button data-action="compare-view" data-view="output" aria-pressed="true">交付正文</button><button data-action="compare-view" data-view="assessments" aria-pressed="false">分项评价与引文</button></div><div class="mobile-side-switch segmented" role="group" aria-label="切换阅读方案"><button data-action="compare-side" data-side="0" aria-pressed="true">基线</button><button data-action="compare-side" data-side="1" aria-pressed="false">候选</button></div></div><div class="reader-tools"><select id="compare-chapter" aria-label="跳转阅读章节"><option value="">阅读章节</option></select><label class="checkbox-inline" title="按同名字段对齐，列表按原始条目顺序，不自动匹配语义；关闭后各侧保留独立位置。"><input id="compare-sync" type="checkbox" checked>同段对照</label>${button('原图与输入','compare-reference','small','file','aria-expanded="false" aria-controls="compare-reference"')}</div><p id="compare-reading-status" class="sr-only" aria-live="polite"></p><div id="compare-quote-return" hidden></div></div><div class="comparison-workspace"><aside id="compare-reference" class="compare-reference" aria-label="本次输入与原图" hidden></aside><div class="dialog-body modal-reading" id="compare-content"><div class="page-loading loading-compare"><span class="spinner"></span></div></div></div>`,true);
  await loadComparison();
}
const compareMobile=()=>window.matchMedia('(max-width:760px)').matches;
function comparePane(side=state.compare?.side){return modal.querySelector(`[data-compare-side="${side}"] .reading-pane`);}
function compareScroller(side=state.compare?.side){return compareMobile()?modal.querySelector('.comparison-workspace'):comparePane(side);}
function comparePositionKey(c,side=c.side){return JSON.stringify([c.caseId,c.candidateId,c.repeat,c.view,side]);}
function compareAnchors(side){return [...(comparePane(side)?.querySelectorAll('[data-read-anchor]')||[])].filter(el=>el.getClientRects().length);}
function captureCompareSpot(side=state.compare?.side){
  const c=state.compare,scroller=compareScroller(side);if(!c||!scroller)return null;
  const aligned=c.alignedPosition;if(aligned?.key===comparePositionKey(c,side)&&Math.abs(aligned.actualTop-scroller.scrollTop)<1)return {...aligned.spot,top:scroller.scrollTop};
  const line=scroller.getBoundingClientRect().top+20,anchors=compareAnchors(side);let target=anchors.filter(el=>{const r=el.getBoundingClientRect();return r.top<=line&&r.bottom>line;}).at(-1)||anchors.find(el=>el.getBoundingClientRect().top>=line);
  if(!target)return {top:scroller.scrollTop};const r=target.getBoundingClientRect(),offset=line-r.top;
  return {top:scroller.scrollTop,path:target.dataset.readAnchor,title:target.dataset.readTitle,offset,fraction:offset/Math.max(1,r.height),inside:offset>=0&&offset<=r.height};
}
function applyCompareSpot(spot,side=state.compare?.side,{linked=false}={}){
  const c=state.compare,scroller=compareScroller(side);if(!c||!scroller)return false;
  let target=spot?.path?compareAnchors(side).find(el=>el.dataset.readAnchor===spot.path):null;
  if(linked&&!target)return false;
  if(target){const rect=target.getBoundingClientRect(),offset=linked&&spot.inside?spot.fraction*rect.height:spot.offset||0;scroller.scrollTop+=rect.top-scroller.getBoundingClientRect().top+offset-20;c.alignedPosition={key:comparePositionKey(c,side),actualTop:scroller.scrollTop,spot:{...spot,offset,fraction:offset/Math.max(1,rect.height),inside:offset>=0&&offset<=rect.height,title:spot.title||target.dataset.readTitle}};}
  else{scroller.scrollTop=spot?.top||0;c.alignedPosition=null;}
  if(!compareMobile()){c.programmaticScrolls??={};c.programmaticScrolls[side]=scroller.scrollTop;}
  // A same-position switch may emit no scroll event. Commit its semantic spot now.
  c.positions[comparePositionKey(c,side)]=captureCompareSpot(side);
  updateCompareChapters(side);return Boolean(target);
}
function rememberComparison(){
  const c=state.compare;if(!c||c.loading)return;
  for(const side of compareMobile()?[c.side]:[0,1]){const key=comparePositionKey(c,side),pane=comparePane(side);c.positions[key]=captureCompareSpot(side);c.openDetails[key]=[...(pane?.querySelectorAll('details')||[])].map((d,i)=>d.open?i:null).filter(i=>i!==null);}
}
function updateCompareChapters(side=state.compare?.side){
  const c=state.compare,select=$('compare-chapter'),pane=comparePane(side);if(!c||!select||!pane)return;
  let sections=[...pane.querySelectorAll('[data-reader-heading]')];if(!sections.length)sections=[...pane.querySelectorAll('.document-content>.document-field')];
  const spot=captureChapterSpot(side),paths=new Set(),unique=sections.filter(el=>{const path=el.dataset.readAnchor;if(paths.has(path))return false;paths.add(path);return true;});
  select.innerHTML=opt('','阅读章节')+unique.map(el=>opt(el.dataset.readAnchor,el.dataset.readTitle||'正文')).join('');
  const selected=unique.filter(el=>spot?.path===el.dataset.readAnchor||spot?.path?.startsWith(el.dataset.readAnchor+'/')).at(-1);select.value=selected?.dataset.readAnchor||'';select.disabled=!unique.length;
}
function captureChapterSpot(side){const scroller=compareScroller(side);if(!scroller)return null;const line=scroller.getBoundingClientRect().top+25,els=compareAnchors(side);const target=els.filter(el=>el.getBoundingClientRect().top<=line).at(-1)||els[0];return target?{path:target.dataset.readAnchor}:null;}
function renderComparison(){
  const c=state.compare;if(!c||!modal.open||c.loading)return;
  const results=c.results,first=results.find(Boolean),caseValue=first?.case;
  const selection=$('compare-selection-label');if(selection)selection.textContent=`${caseValue?.name||c.caseId} · ${c.e.configurations.find(v=>v.id===c.candidateId)?.name||'候选'} · 第 ${c.repeat} 次`;
  for(const b of modal.querySelectorAll('[data-action=compare-side]'))b.setAttribute('aria-pressed',String(Number(b.dataset.side)===c.side));
  for(const b of modal.querySelectorAll('[data-action=compare-view]'))b.setAttribute('aria-pressed',String(b.dataset.view===c.view));
  if(c.referenceKey!==first?.id){c.referenceKey=first?.id;$('compare-reference').innerHTML=caseValue?`<div class="reference-header"><strong>${esc(caseValue.name||'本次输入与原图')}</strong>${button('返回阅读','compare-reference-return','quiet small','back')}</div><div class="reference-body">${mediaEvidence(caseValue,first.id,first.media)}${evidenceContent(readableInput(caseValue.input))}</div>`:'';}
  $('compare-content').innerHTML=`<div class="paired-output">${results.map((t,i)=>{const allRecords=t?.assessment_records||[],other=results[1-i],records=allRecords.filter(a=>!a.value?.preference?.other_trial_id||a.value.preference.other_trial_id===other?.id);return `<article class="panel compare-side ${i!==c.side?'mobile-inactive':''}" data-compare-side="${i}"><div class="output-head"><span class="tag ${i===0?'baseline':''}">${i===0?'基线':'候选'}</span><div><h3>${esc(t?.configuration?.name||'运行不存在')}</h3><small>${t?'尝试 '+t.attempt:'缺少对应运行'}</small></div>${t?badge(t.status):''}</div><div class="content-box reading-pane ${c.view==='output'?'output-reading':''}" tabindex="0" aria-label="${i===0?'基线':'候选'}${c.view==='output'?'交付正文':'分项评价与引文'}">${c.view==='assessments'?(records.length?assessmentCards(records,c.e.rubric||[],{compareSide:i}):empty('尚无独立模型评价','','','score',true)):(hasReviewOutput(t?.result)?outputContent(t.result.review_output):`<div class="empty-output">尚无交付内容。${t?.result?.error?`<p class="error-text">${esc(t.result.error.message||t.result.error.code)}</p>`:''}</div>`)}</div>${t?`<div class="panel-footer"><span class="source-note">本对照 ${records.length} 条评价 · 运行共 ${allRecords.length} 条</span><a href="${routeUrl('trial',t.id,c.view==='assessments'?'assessments':'evidence')}" data-action="close-route">完整运行 ${icon('arrow')}</a></div>`:''}</article>`;}).join('')}</div>`;
  for(const side of [0,1]){const pane=comparePane(side),saved=c.openDetails[comparePositionKey(c,side)];if(saved)for(const [i,d]of [...pane.querySelectorAll('details')].entries())d.open=saved.includes(i);}
  for(const [i,pane]of [...modal.querySelectorAll('.reading-pane')].entries())pane.addEventListener('scroll',()=>{
    if(compareMobile()||c.resizing||c.layoutMobile!==compareMobile())return;
    const programmed=c.programmaticScrolls?.[i];
    if(programmed!==undefined){delete c.programmaticScrolls[i];if(Math.abs(pane.scrollTop-programmed)<1)return;}
    c.side=i;updateCompareChapters(i);c.positions[comparePositionKey(c,i)]=captureCompareSpot(i);if(!c.sync)return;
    const matched=applyCompareSpot(captureCompareSpot(i),1-i,{linked:true});if(!matched)$('compare-reading-status').textContent='另一侧没有对应字段，保留其阅读位置。';
  });
  modal.querySelector('.comparison-workspace').onscroll=()=>{if(state.compare===c&&!c.loading&&compareMobile()&&!c.resizing&&c.layoutMobile===true){c.positions[comparePositionKey(c)]=captureCompareSpot(c.side);updateCompareChapters(c.side);}};
  hydrateMedia(modal);
  const request=c.request;requestAnimationFrame(()=>{if(state.compare!==c||c.request!==request||!modal.open)return;for(const side of compareMobile()?[c.side]:[0,1]){const saved=c.positions[comparePositionKey(c,side)],first=compareAnchors(side)[0];applyCompareSpot(saved||(compareMobile()&&c.referenceOpen&&first?{path:first.dataset.readAnchor,offset:0}:null),side);}updateCompareChapters(c.side);});
}
function switchComparisonSide(side){
  const c=state.compare;if(!c||c.loading||side===c.side)return;const origin=captureCompareSpot();rememberComparison();c.side=side;
  for(const el of modal.querySelectorAll('[data-compare-side]'))el.classList.toggle('mobile-inactive',Number(el.dataset.compareSide)!==side);
  for(const b of modal.querySelectorAll('[data-action=compare-side]'))b.setAttribute('aria-pressed',String(Number(b.dataset.side)===side));
  const saved=c.positions[comparePositionKey(c)],matched=c.sync&&origin?.path&&compareAnchors(side).some(el=>el.dataset.readAnchor===origin.path);applyCompareSpot(matched?origin:saved,side,{linked:Boolean(matched)});
  $('compare-reading-status').textContent=matched?`已对齐${origin.title||'同一字段'}。`:(c.sync&&origin?.path?'此侧没有同一字段，恢复此侧阅读位置。':'已恢复此侧阅读位置。');
  if(c.sync&&origin?.path&&!matched)toast('此侧没有同一字段，已恢复此侧阅读位置');
}
function toggleCompareReference(force){
  const c=state.compare;if(!c||c.loading)return;const spot=captureCompareSpot(),request=c.request;
  if(compareMobile()){
    const referenceVisible=c.referenceOpen&&$('compare-reference').getBoundingClientRect().bottom>compareScroller().getBoundingClientRect().top+20,back=c.referenceReturn;
    if(!referenceVisible)c.referenceReturn={request,view:c.view,side:c.side,spot};
    else if(!back||back.request!==request||back.view!==c.view||back.side!==c.side){const first=compareAnchors(c.side)[0];c.referenceReturn={request,view:c.view,side:c.side,spot:first?{path:first.dataset.readAnchor,offset:0}:null};}
    c.referenceOpen=force??true;
  }else c.referenceOpen=force??!c.referenceOpen;
  $('compare-reference').hidden=!c.referenceOpen;modal.querySelector('.comparison-workspace').classList.toggle('reference-open',c.referenceOpen);modal.querySelector('[aria-controls="compare-reference"]').setAttribute('aria-expanded',String(c.referenceOpen));
  requestAnimationFrame(()=>{if(state.compare!==c||c.request!==request||!modal.open)return;if(compareMobile()&&c.referenceOpen)compareScroller().scrollTop=0;else applyCompareSpot(spot);});
}
function returnCompareReference(){
  const c=state.compare;if(!c||c.loading)return;if(!compareMobile()){toggleCompareReference(false);return;}
  const back=c.referenceReturn,first=compareAnchors(c.side)[0];
  applyCompareSpot(back?.request===c.request&&back.view===c.view&&back.side===c.side?back.spot:first?{path:first.dataset.readAnchor,offset:0}:null);
  modal.querySelector('[aria-controls="compare-reference"]')?.focus({preventScroll:true});
}

function readOutputPointer(value,pointer){
  if(pointer==='')return value;if(!pointer.startsWith('/')||/~(?:[^01]|$)/.test(pointer))return undefined;
  for(const part of pointer.slice(1).split('/')){const key=part.replace(/~1/g,'/').replace(/~0/g,'~');if(value===null||typeof value!=='object'||!Object.prototype.hasOwnProperty.call(value,key))return undefined;value=value[key];}return value;
}
function inspectCompareEvidence(b){
  const c=state.compare;if(!c||c.loading)return;const side=Number(b.dataset.side),source=b.dataset.source,pointer=b.dataset.pointer,quote=b.dataset.quote;
  const request=c.request,trial=c.results[side];if(b.dataset.current!=='true'||Number(b.dataset.attempt)!==Number(trial?.attempt)){toast('此评价绑定历史尝试，请在完整运行中核对对应输出。');return;}
  if(source!=='output'){toggleCompareReference(true);return;}
  rememberComparison();c.quoteReturn={view:c.view,side:c.side,position:c.positions[comparePositionKey(c)]};c.view='output';c.side=side;renderComparison();
  requestAnimationFrame(()=>{
    if(state.compare!==c||c.request!==request||!modal.open||c.view!=='output'||c.side!==side)return;const pane=comparePane(side),target=[...pane.querySelectorAll('[data-read-anchor]')].find(el=>el.dataset.readAnchor===pointer);
    if(!target){returnCompareEvidence();toast('当前阅读视图未提供此路径定位，已保留原评价位置；完整字段可从原始记录核对。');return;}
    for(let parent=target.parentElement;parent&&parent!==pane;parent=parent.parentElement)if(parent.tagName==='DETAILS')parent.open=true;
    pane.querySelectorAll('.quote-target').forEach(el=>el.classList.remove('quote-target'));target.classList.add('quote-target');
    applyCompareSpot({path:pointer,offset:0,top:0},side);target.setAttribute('tabindex','-1');target.focus({preventScroll:true});
    const raw=readOutputPointer(trial.result?.review_output,pointer),text=typeof raw==='string'?raw:JSON.stringify(raw),matched=Boolean(quote&&typeof text==='string'&&text.includes(quote));
    $('compare-quote-return').hidden=false;$('compare-quote-return').innerHTML=`<span>已打开${esc(target.dataset.readTitle||'所指字段')} · ${quote?(matched?'找到原文引文':'引文未直接匹配，请核对'):'未提供文字引文'}</span>${button('返回评价','compare-quote-return','small','back')}`;applyCompareSpot({path:pointer,offset:0,top:0},side);
    $('compare-reading-status').textContent='已定位引文所指字段，可返回原评价位置。';
  });
}
function returnCompareEvidence(){const c=state.compare,back=c?.quoteReturn;if(!back)return;rememberComparison();c.view=back.view;c.side=back.side;c.positions[comparePositionKey(c)]=back.position;c.quoteReturn=null;$('compare-quote-return').hidden=true;renderComparison();}
async function loadComparison(){
  const c=state.compare;if(!c)return;c.loading=true;c.results=[];c.alignedPosition=null;for(const b of modal.querySelectorAll('[data-action=compare-view],[data-action=compare-side],[data-action=compare-reference],#compare-chapter'))b.disabled=true;const ticket=++c.request,baseline=c.e.baseline_id||c.e.configurations[0].id;
  const rows=[baseline,c.candidateId].map(id=>c.e.trials.find(t=>t.configuration_id===id&&t.case_id===c.caseId&&Number(t.repeat_index)===Number(c.repeat)));
  $('compare-content').innerHTML=`<div class="page-loading loading-compare"><span class="spinner"></span><p>正在读取两侧实际输出…</p></div>`;$('compare-reference').innerHTML='';c.referenceKey=null;c.referenceReturn=null;$('compare-quote-return').hidden=true;c.quoteReturn=null;
  const results=await Promise.all(rows.map(t=>t?api('/api/trials/'+t.id):Promise.resolve(null)));
  if(state.compare!==c||ticket!==c.request||!modal.open)return;
  c.results=results;c.loading=false;for(const b of modal.querySelectorAll('[data-action=compare-view],[data-action=compare-side],[data-action=compare-reference],#compare-chapter'))b.disabled=false;await loadRubricEvidence({get:api},c.e,results.flatMap(t=>t?.assessment_records||[]));if(state.compare===c&&ticket===c.request&&modal.open)renderComparison();
}

async function openScoring({evaluatorId='',attempt=null}={}){
  const current=state.current,isTrial=current?._kind==='trial',experimentId=isTrial?current.experiment_id:current?._kind?null:current?.id;
  if(isTrial){const selected=attempt===null||Number(attempt)===Number(current.attempt)?current:(current.attempts||[]).find(a=>Number(a.attempt??a.execution_attempt)===Number(attempt));if(!selected)throw new Error('所选历史尝试尚未读取，请刷新后重试。');const availability=scoreAvailability(selected);if(!availability.available)throw new Error(availability.reason);}
  state.scoreTargets=isTrial?[{trial_id:current.id,execution_attempt:attempt??current.attempt}]:null;
  openModal(modalHead('创建独立评分作业','读取当前可用评分器与实验…')+`<div class="dialog-body"><div class="page-loading loading-compact" ><span class="spinner"></span></div></div>`);
  const modalTicket=state.modalVersion;
  const [{evaluators=[]},{experiments=[]},rubricResult]=await Promise.all([api('/api/evaluators'),api('/api/experiments'),api('/api/rubrics').catch(error=>({error:error.message,rubrics:[]}))]);
  if(!modal.open||modalTicket!==state.modalVersion)return;
  if(!evaluators.length)throw new Error('没有可用评分器，请先检查评分器目录。');
  if(!experiments.length)throw new Error('请先建立或导入一个实验，再对交付结果评分。');
  const targetName=isTrial?`${current.case?.name} · 尝试 ${attempt??current.attempt}`:'所选实验的冻结输出';
  const rubrics=rubricResult.rubrics||[],selectedExperiment=experiments.find(e=>e.id===experimentId)||experiments[0],priorVersion=current?.comparison?.model_dimensions?.[0]?.rubric_version;
  const preferred=rubrics.find(r=>r.id===priorVersion||r.declared_id===priorVersion||String(priorVersion||'').startsWith((r.declared_id||r.id)+'@')||String(priorVersion||'').startsWith((r.declared_id||r.id)+':'))||rubrics.find(r=>r.project&&r.project===selectedExperiment?.project)||rubrics.find(r=>r.id==='general-v2')||rubrics[0];
  const defaultEvaluator=evaluators.find(e=>e.validation_profile==='live-strict-json-evidence-v1')||evaluators[0],selectedEvaluatorId=evaluatorId||defaultEvaluator.id;
  state.scoring={experiments,rubrics,isTrial,priorVersion,evaluators,defaultEvaluatorId:defaultEvaluator.id,explicitEvaluator:Boolean(evaluatorId)};
  openModal(modalHead('创建评分作业')+`<form id="scoring-form"><div class="dialog-body"><label class="mt-0" >作业名称<input name="title" maxlength="160" placeholder="如：留出集 · 事实依据与意图理解"></label><div class="form-grid"><label>实验<select name="experiment_id" ${isTrial?'disabled':''}>${experiments.map(e=>opt(e.id,e.title,e.id===experimentId)).join('')}</select>${isTrial?`<input type="hidden" name="experiment_id" value="${esc(experimentId)}">`:''}</label><label>评分器<select name="evaluator_id">${evaluators.map(e=>opt(e.id,`${e.name||e.id} · ${e.id}`,e.id===selectedEvaluatorId)).join('')}</select></label></div><div id="scoring-evaluator-info" class="scoring-evaluator-info"></div><div class="form-grid"><label>评分方式<select name="mode"><option value="individual">逐项分维度评分</option><option value="pairwise" ${isTrial?'disabled':''}>基线与候选匿名配对</option></select></label><label>材料分组<select name="split"><option value="all">全部分组</option><option value="development">开发集</option><option value="holdout">留出集</option></select></label></div><label>评分量表<select name="rubric_id" id="scoring-rubric">${rubrics.length?rubrics.map(r=>opt(r.file_id||r.id,`${r.name||r.id} · ${r.version||'版本未声明'}`,(r.file_id||r.id)===(preferred?.file_id||preferred?.id))).join(''):opt('general-v2','通用交付质量 · general-v2')}<option value="__custom">自定义量表 JSON</option></select></label><p id="scoring-rubric-info" class="rubric-info">${esc(preferred?.scope||rubricResult.error||'请选择适合当前任务的量表')}</p><div class="notice">本次目标：${esc(targetName)}。${isTrial?'将保留当前选择的历史尝试编号，不替换为更新结果。':'可用结果进入评分，产品运行失败不会被计为质量零分。'}</div><div class="scoring-budget-summary" id="scoring-budget-summary" aria-live="polite"></div><details open><summary>评分预算</summary><p class="form-help">初始上限按目标规模预填，可调整。tokens 与费用可能缺失，在途请求可能越过已观察上限；不将未知费用视为零。</p><div class="form-grid"><label>单次评分超时（秒）<input name="timeout_seconds" type="number" min="5" max="1800" value="300" required></label><label>评分单元上限<input name="max_units" type="number" min="1" max="2000" required></label><label>作业耗时上限（秒）<input name="max_elapsed_seconds" type="number" min="5" max="86400" required></label><label>已观察 tokens 上限<input name="max_total_tokens" type="number" min="1" max="5000000" required></label></div></details><details id="scoring-custom"><summary>自定义量表 JSON</summary><label>完整版本化量表<textarea name="rubric_json" class="json-input" rows="5" placeholder="留空使用上方注册量表；填写后会作为本作业的冻结量表"></textarea></label></details><p id="form-error" class="form-error" role="alert"></p></div><div class="dialog-footer"><span class="muted">将发起独立模型评分请求</span><div class="action-row">${button('取消','close')}<button class="primary" type="submit">创建评分作业 ${icon('play')}</button></div></div></form>`);
  updateScoringPlan();
}
function updateScoringPlan(){
  const form=$('scoring-form'),plan=state.scoring;if(!form||!plan)return;
  const experiment=plan.experiments.find(e=>e.id===new FormData(form).get('experiment_id')),pairwise=form.elements.mode.value==='pairwise',split=form.elements.split.value;
  const units=plan.isTrial?1:pairwise?countCases(experiment||{})*Math.max(1,configurationCount(experiment||{})-1)*(experiment?.repeats||1):(experiment?.total_trials||1);
  const values={max_units:Math.min(2000,Math.max(1,units)),max_elapsed_seconds:Math.min(86400,Math.max(600,units*Number(form.elements.timeout_seconds.value||300))),max_total_tokens:Math.min(5000000,Math.max(12000,units*12000))};
  for(const [name,value] of Object.entries(values)){const input=form.elements[name];if(!input.dataset.edited)input.value=value;}
  $('scoring-budget-summary').innerHTML=`<p><strong>${plan.isTrial?'固定 1 个历史尝试':`预计最多 ${number(units)} 个${pairwise?'匿名配对':'逐项'}评分单元`}</strong></p><small>${split==='all'?'按当前所选实验规模预检。':'当前使用实验全量作为预算上界，服务将按所选分组冻结实际目标。'} ${units>2000?'超出服务每作业 2,000 单元上限，请缩小目标范围。':'初始预留每单元 300 秒、12,000 tokens，可自行调整；这不是用量或费用预测。'}</small>`;
  const selectedEvaluator=plan.evaluators?.find(e=>e.id===form.elements.evaluator_id.value),typed=plan.evaluators?.filter(e=>e.validation_profile)||[];
  if($('scoring-evaluator-info')){const e=selectedEvaluator||{},effective=v=>!v||v==='unknown'?'未知':String(v);$('scoring-evaluator-info').innerHTML=`<p class="source-note mt-0">${!plan.explicitEvaluator&&e.id===plan.defaultEvaluatorId?'新作业默认预设':'本次所选预设'}：${esc(e.name||e.id||'未声明')} · v${esc(e.version||'未声明')}。旧作业与旧评分保留原版本。</p><div class="evaluator-request"><span>本次请求模型</span><strong>${esc(e.model||'未声明')}</strong><span>推理强度 ${esc(e.reasoning_effort||'未声明')}</span></div><dl class="definition-list"><dt>实际生效模型 / 强度</dt><dd>${esc(effective(e.effective?.model))} / ${esc(effective(e.effective?.reasoning_effort))}</dd><dt>图像输入</dt><dd>${e.vision===true?'评分器声明支持':e.vision===false?'评分器声明不支持':'未声明'} · 生效情况以实际作业证据为准</dd></dl><details><summary>预设版本与验证配置</summary><dl class="definition-list"><dt>预设</dt><dd>${esc(e.id||'未选择')} · v${esc(e.version||'未声明')}</dd><dt>验证配置</dt><dd>${esc(e.validation_profile||'未声明')}</dd><dt>完整内容哈希</dt><dd class="mono">${esc(e.hash||'未记录')}</dd></dl><p class="source-note">新作业保留所选版本，旧评分不会自动换代。这里展示请求配置，实际生效值缺乏供应商证据时保留未知。</p></details>`;}

  const selected=plan.rubrics.find(r=>(r.file_id||r.id)===form.elements.rubric_id.value);
  $('scoring-rubric-info').textContent=selected?.scope||(form.elements.rubric_id.value==='__custom'?'请提供完整量表对象，包含 id、版本和维度。':'量表目录暂不可用，使用已注册的通用量表；也可填写自定义量表。');
  if(form.elements.rubric_id.value==='__custom')$('scoring-custom').open=true;
}
function openImport(kind){
  state.importKind=kind;state.importValue=null;const dataset=kind==='dataset';
  openModal(modalHead(dataset?'导入一个数据集版本':'导入历史运行轨迹',dataset?'同一版本内容不可覆盖；新材料请使用新版本。':'只保存已结束的历史证据，不调度产品请求。')+`<form id="import-form"><div class="dialog-body"><div class="file-drop">${icon('upload')}<p>选择本机 JSON 文件</p><input id="import-file" type="file" accept=".json,application/json" required aria-label="选择 JSON 文件"></div><div id="import-preview"></div><p class="form-help">${dataset?'文件包含 id、name、project、version、cases，配置可引用已注册 taskpack_id。':'文件包含带 source 的 MyEval bundle，或 project、title、source 与 traces 数组。'}材料来源必须声明为合成、许可公开或明确授权私有材料。</p><details><summary>查看格式与来源要求</summary>${json(dataset?{id:'example-dataset',name:'示例数据集',project:'已注册项目',version:'1',taskpack_id:'已注册任务包',source:{kind:'synthetic',description:'明确虚构材料'},cases:[{id:'case-1',name:'情境名称',family_id:'family-1',version:'1',split:'development',source:{kind:'synthetic',description:'虚构情境'},input:{}}]}:{project:'已注册项目',title:'历史轨迹',source:{kind:'synthetic',description:'合成材料的历史运行'},traces:[{case:{},configuration:{},result:{},status:'succeeded',events:[],attempts:[]}]})}<p class="form-help">公开材料需要许可和来源链接；私有材料需要已有授权证明。服务会校验内容，不会自行补授权。</p></details><p id="form-error" class="form-error" role="alert"></p></div><div class="dialog-footer"><span class="muted">文件只发送到当前本机服务</span><div class="action-row">${button('取消','close')}<button class="primary" type="submit">校验并导入 ${icon('upload')}</button></div></div></form>`);
}
function openDecision(){
  const e=state.current;if(!e?.trials)throw new Error('请从实验的发布判定页进入。');
  const dims=[...new Set((e.comparison?.model_dimensions||[]).map(r=>r.dimension))],groups=[...new Map((e.comparison?.model_dimensions||[]).map(r=>[JSON.stringify([r.evaluator,r.rubric_version]),r])).entries()];
  openModal(modalHead('设置判定门槛')+`<form id="decision-form"><div class="dialog-body"><div class="form-grid"><label class="mt-0" >策略名称<input name="name" value="质量与执行门槛" required></label><label class="mt-0" >判定范围<select name="scope"><option value="quality">质量判定</option><option value="execution">仅执行验收</option></select></label></div><div class="form-grid"><label>最少独立情境家族<input type="number" name="min_families" value="5" min="1" required></label><label>最低执行成功率（%）<input type="number" name="min_success_rate" value="100" min="0" max="100" required></label><label>最低执行完成率（%）<input type="number" name="min_completion_rate" value="100" min="0" max="100" required></label><label>最少真实人工评价配对<input type="number" name="min_human_review_pairs" value="0" min="0" required></label></div><label class="checkbox-inline"><input type="checkbox" name="require_holdout">要求本次材料全部来自留出组</label><label class="checkbox-inline"><input type="checkbox" name="rules_required" checked>要求可观察规则覆盖，严重失败不超过 0 项</label><details open><summary>质量维度门槛</summary><p class="form-help">选择已运行的评分器与量表。不设置质量维度时，质量判定保留“证据不足”。</p><div class="form-grid"><label class="full-width">使用哪组独立评价<select id="decision-evaluation"><option value="">暂不设置质量维度</option>${groups.map(([key,r])=>opt(key,`${r.evaluator} · ${r.rubric_version}`)).join('')}</select><small>只使用当前实验已有的评分记录，不跨评分器或量表混合。</small></label><label>质量维度<select name="dimension" id="decision-dimension"><option value="">先选择独立评价</option></select></label><input name="evaluator" type="hidden"><input name="rubric_version" type="hidden"><label>最低平均分（可选）<input type="number" step="any" name="min_mean" placeholder="依据该量表定义"></label><label>相对基线最小提升（可选）<input type="number" step="any" name="min_delta" placeholder="依据该量表定义"></label><label>最低评分覆盖率（%）<input type="number" name="min_coverage" value="100" min="0" max="100"></label></div></details><p id="form-error" class="form-error" role="alert"></p></div><div class="dialog-footer"><span class="muted">缺失或不足的证据不会自动通过</span><div class="action-row">${button('取消','close')}<button class="primary" type="submit">运行并保存判定 ${icon('shield')}</button></div></div></form>`);
}
function openRestore(id){state.restoreId=id;openModal(modalHead('恢复到独立的新目录','当前运行目录保持不变。目标目录必须为空或不存在，请填写绝对路径。')+`<form id="restore-form"><div class="dialog-body"><label class="mt-0" >目标目录<input name="destination" placeholder="如：/Users/你的账号/MyEval-restored" required autocomplete="off"></label><p class="form-help">服务会核对备份完整性，拒绝覆盖已有数据。恢复后需以新的目录独立启动工作区。</p><p id="form-error" class="form-error" role="alert"></p></div><div class="dialog-footer">${button('取消','close')}<button class="primary" type="submit">恢复副本 ${icon('arrow')}</button></div></form>`);}
function confirmStop(){openModal(modalHead('停止当前实验','未开始的调度将停止，在途运行会请求取消。')+`<div class="dialog-body">${note('所有已产生的结果和失败保留。远端计算与计费是否停止需要供应商证据；停止后不会自动重新发送请求。','warning')}</div><div class="dialog-footer">${button('返回','close')}${button('停止并保留证据','confirm-stop','danger','stop')}</div>`);}
function filterExperimentRows(){if(state.route!=='experiments')return;const search=filters.search.trim().toLowerCase();const rows=[...app.querySelectorAll('.experiment-row')];let visible=0;for(const row of rows){const show=(!filters.project||row.dataset.project===filters.project)&&(!filters.status||(filters.status==='issues'?row.dataset.issues==='true':row.dataset.status===filters.status))&&(!search||row.dataset.search.includes(search));row.hidden=!show;if(show)visible++;}if($('visible-count'))$('visible-count').textContent=`${visible} / ${rows.length} 个实验`;const blank=app.querySelector('.table-filter-empty');if(blank)blank.hidden=visible>0||!rows.length;}
function filterTrialRows(){const input=$('trial-search');if(!input)return;
  const key=input.closest('.result-run-list').dataset.filterKey;
  sessionStorage.setItem(key,JSON.stringify({case:filters.case,status:filters.trialStatus,search:input.value}));
  const search=input.value.toLowerCase(),rows=[...app.querySelectorAll('.trial-row')];let visible=0;for(const row of rows){const show=(!filters.case||row.dataset.case===filters.case)&&(!filters.trialStatus||row.dataset.status===filters.trialStatus)&&(!search||row.dataset.search.includes(search));row.hidden=!show;if(show)visible++;}if($('visible-trial-count'))$('visible-trial-count').textContent=`${visible} / ${rows.length} 项运行`;const blank=app.querySelector('.table-filter-empty');if(blank)blank.hidden=visible>0||!rows.length;}

async function render({refresh=false}={}){
  const ticket=++state.request;state.controller?.abort();$('route-loading')?.remove();const controller=new AbortController();state.controller=controller;
  const parts=location.hash.replace(/^#/,'').split('/').map(v=>{try{return decodeURIComponent(v);}catch{return v;}});let [route='overview',id='',tab='']=parts;route=route||'overview';if(route==='catalog'){route='datasets';id='packs';}
  if(route==='main-content'){app.focus();return;}
  state.route=route;const names={overview:'概览',experiments:'实验',experiment:'实验 / 实验详情',trial:'实验 / 逐项运行',datasets:'数据与任务包',dataset:'数据与任务包 / 版本详情',jobs:'评分作业',job:'评分作业 / 作业详情',revalidation:'评分作业 / 重验证收据',evaluators:'评分器',queue:'运行与恢复',guide:'操作手册',campaigns:'实验 / 实验批次',campaign:'实验 / 批次详情'};
  $('breadcrumb').textContent='工作区 / '+(names[route]||'概览');$('breadcrumb').title=$('breadcrumb').textContent;$('breadcrumb-full').textContent=$('breadcrumb').textContent;$('breadcrumb').closest('details').open=false;document.title=(names[route]?.split(' / ').at(-1)||'评测工作台')+' · MyEval';
  const nav=({experiment:'experiments',trial:'experiments',dataset:'datasets',job:'jobs',revalidation:'jobs',campaigns:'experiments',campaign:'experiments'}[route]||route);document.querySelectorAll('[data-nav]').forEach(a=>{a.classList.toggle('active',a.dataset.nav===nav);if(a.dataset.nav===nav)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
  setNavigation(false,{restore:false});$('refresh-notice').hidden=true;
  if(!sessionStorage.getItem('myeval.admin')){loginPage();return;}
  const scroll=window.scrollY;const openDetails=refresh?[...app.querySelectorAll('details[open]')].map(el=>el.querySelector('summary')?.textContent):[];
  if(!refresh){releaseMedia();app.innerHTML=`<div class="page-loading"><span class="spinner"></span><p>正在读取${esc(names[route]?.split(' / ').at(-1)||'工作区')}…</p></div>`;}
  else{app.setAttribute('aria-busy','true');document.body.insertAdjacentHTML('beforeend','<div class="loading-line" id="route-loading"></div>');}
  const ctx={get:url=>api(url,undefined,{signal:controller.signal}),current:null,experiments:null};
  try{
    let html;if(route==='experiment'&&id)html=await experimentPage(ctx,id,tab||'compare');else if(route==='trial'&&id)html=await trialPage(ctx,id,tab||'output');else if(route==='experiments')html=await experimentsPage(ctx,id);else if(route==='campaigns')html=await campaignsPage(ctx);else if(route==='campaign'&&id)html=await campaignPage(ctx,id);else if(route==='datasets')html=await datasetsPage(ctx,id);else if(route==='dataset'&&id)html=await datasetPage(ctx,id,tab);else if(route==='jobs')html=await jobsPage(ctx,id||'all');else if(route==='job'&&id)html=await jobPage(ctx,id,tab);else if(route==='revalidation'&&id)html=await revalidationPage(ctx,id);else if(route==='evaluators')html=await evaluatorsPage(ctx);else if(route==='queue')html=await queuePage(ctx);else if(route==='guide')html=await guidePage(ctx);else html=await overviewPage(ctx);
    if(ticket!==state.request)return;
    state.current=ctx.current;if(ctx.experiments){state.experiments=ctx.experiments;state.lastFingerprint=fingerprint(ctx.experiments);$('experiment-count').textContent=ctx.experiments.length;$('experiment-count').hidden=false;}
    releaseMedia();app.innerHTML=html;associateFormLabels(app);bindPlaybookPlayer();setService(true);filterExperimentRows();filterTrialRows();hydrateMedia();
    if(refresh){for(const detail of app.querySelectorAll('details'))if(openDetails.includes(detail.querySelector('summary')?.textContent))detail.open=true;window.scrollTo(0,scroll);}else if(!modal.open)app.focus({preventScroll:true});
    if(state.pendingStage&&route==='trial'&&id===state.pendingStage.trialId&&tab==='evidence'){const stage=$('stage-'+state.pendingStage.index);stage?.querySelector('details')?.setAttribute('open','');stage?.scrollIntoView({block:'center',behavior:'instant'});stage?.setAttribute('tabindex','-1');stage?.focus({preventScroll:true});state.pendingStage=null;}
    if(ctx.focusUnit){const unit=$('scoring-unit-'+ctx.focusUnit);if(unit){unit.querySelector('details').open=true;const header=unit.querySelector('.unit-summary');header.scrollIntoView({block:'start',behavior:'instant'});header.focus({preventScroll:true});}}
  }catch(error){if(error.name==='AbortError'||ticket!==state.request)return;if(error.status===403){sessionStorage.removeItem('myeval.admin');loginPage(error.message);}else{app.innerHTML=pageTitle('暂时无法读取此页','')+note(error.message,'error')+`<div class="action-row section">${button('重试读取','refresh','primary','refresh')}<a class="button" href="#overview">返回概览</a></div>`;if(!error.status)setService(false);}}
  finally{if(ticket===state.request){app.removeAttribute('aria-busy');$('route-loading')?.remove();}}
}
function loginPage(error=''){
  state.current=null;
  app.innerHTML=`<div class="login-layout"><div class="panel"><div class="icon-box">${icon('lock')}</div><h1 class="my-10" >进入本机工作区</h1><form id="login-form"><label>本机访问码<input type="password" name="access" autocomplete="off" required autofocus></label><p class="form-error" id="login-error" role="alert">${esc(error)}</p><button class="primary" type="submit">进入工作区 ${icon('arrow')}</button></form><details><summary>从哪里取得访问码？</summary><p>匿名评审使用独立的评审链接。</p><p>在本机数据目录查找 <span class="mono">admin.token</span>，或运行 <span class="mono">scripts/open</span> 直接打开管理员入口。</p></details></div></div>`;
  associateFormLabels(app);
}
async function loadCatalog(){const data=await api('/api/catalog');state.catalog=data.taskpacks||[];state.capabilities=data.capabilities||{};state.limits=data.limits||{};state.datasets=data.datasets||[];}

// Navigation is never gated by a background refresh. Stale requests cannot commit.
window.addEventListener('hashchange',()=>{if(modal.open)closeModal();filters.case='';filters.trialStatus='';window.scrollTo(0,0);render();});
$('menu-toggle').addEventListener('click',()=>setNavigation(!$('sidebar').classList.contains('is-open')));
$('nav-close').addEventListener('click',()=>setNavigation(false));
$('nav-scrim').addEventListener('click',()=>setNavigation(false));
window.matchMedia('(max-width:760px)').addEventListener('change',event=>{
  setNavigation(false,{restore:false});const c=state.compare;if(!c)return;
  // Resizing changes which element scrolls. Keep the last reading spots until
  // both panes have been restored; layout-generated scrolls are not user input.
  const positions={...c.positions},side=c.side,revision=++c.layoutRevision;
  c.resizing=true;c.layoutMobile=event.matches;
  const selector=modal.querySelector('.compare-selector');if(selector)selector.open=!event.matches;
  for(const el of modal.querySelectorAll('[data-compare-side]'))el.classList.toggle('mobile-inactive',Number(el.dataset.compareSide)!==side);
  for(const b of modal.querySelectorAll('[data-action=compare-side]'))b.setAttribute('aria-pressed',String(Number(b.dataset.side)===side));
  requestAnimationFrame(()=>{
    if(state.compare!==c||revision!==c.layoutRevision)return;
    if(!c.loading){for(const target of event.matches?[side]:[0,1]){const spot=positions[comparePositionKey(c,target)];if(spot)applyCompareSpot(spot,target);}updateCompareChapters(side);}
    requestAnimationFrame(()=>{if(state.compare===c&&revision===c.layoutRevision)c.resizing=false;});
  });
});
$('sidebar').addEventListener('keydown',event=>{if(!$('sidebar').classList.contains('is-open'))return;if(event.key==='Escape'){event.preventDefault();setNavigation(false);}else trapFocus(event,$('sidebar'));});
$('sidebar').addEventListener('click',event=>{if(event.target.closest('a'))setNavigation(false,{restore:false});});
modal.addEventListener('cancel',event=>{event.preventDefault();closeModal();});
modal.addEventListener('keydown',event=>{
  trapFocus(event,modal);
  const viewport=event.target.closest('.frame-viewport');if(!viewport||event.altKey||event.ctrlKey||event.metaKey)return;
  const page=Math.max(80,viewport.clientHeight*.85),step=80,keys={ArrowLeft:[-step,0],ArrowRight:[step,0],ArrowUp:[0,-step],ArrowDown:[0,step],PageUp:[0,-page],PageDown:[0,page]};
  if(keys[event.key]){event.preventDefault();viewport.scrollBy({left:keys[event.key][0],top:keys[event.key][1],behavior:'instant'});}
  else if(event.key==='Home'||event.key==='End'){event.preventDefault();viewport.scrollTo({left:event.key==='Home'?0:viewport.scrollWidth,top:event.key==='Home'?0:viewport.scrollHeight,behavior:'instant'});}
});
modal.addEventListener('close',()=>{const trigger=state.modalTrigger?.isConnected?state.modalTrigger:state.modalTriggerAction?document.querySelector(`[data-action="${CSS.escape(state.modalTriggerAction)}"]`):null;if(trigger&&!trigger.disabled)trigger.focus({preventScroll:true});else app.focus({preventScroll:true});});
modal.addEventListener('click',event=>{if(event.target===modal){const r=modal.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)closeModal();}});
document.querySelector('.skip-link').addEventListener('click',event=>{event.preventDefault();app.focus();app.scrollIntoView({block:'start'});});
document.addEventListener('keydown',event=>{if(event.key==='/'&&!modal.open&&!event.target.closest('input,textarea,select,[contenteditable]')){const search=$('experiment-search')||$('trial-search');if(search){event.preventDefault();search.focus();}}});
document.addEventListener('input',event=>{const el=event.target;if(el.id==='experiment-search'){filters.search=el.value;filterExperimentRows();}if(el.id==='trial-search')filterTrialRows();if(el.closest('#wizard-form')&&state.wizard){captureWizard();updateWizardScope();}});
document.addEventListener('change',async event=>{
  const el=event.target;
  try{
    if(el.id==='playbook-segment-select')selectPlaybookSegment(Number(el.value));
    if(el.id==='compare-sync'&&state.compare)state.compare.sync=el.checked;
    if(el.id==='compare-chapter'&&state.compare&&el.value){const c=state.compare;applyCompareSpot({path:el.value,offset:0,top:0},c.side);if(c.sync&&!compareMobile()&&!applyCompareSpot({path:el.value,offset:0,top:0},1-c.side,{linked:true}))$('compare-reading-status').textContent='另一侧没有对应字段，保留其阅读位置。';}
    if(el.id==='receipt-status-filter'){const units=[...app.querySelectorAll('[data-receipt-status]')];let visible=0;for(const unit of units){unit.hidden=Boolean(el.value&&el.value!==unit.dataset.receiptStatus);if(!unit.hidden)visible++;}$('receipt-visible-count').textContent=`${visible} / ${units.length} 个单元`;$('receipt-filter-empty').hidden=visible>0;}
    if(el.closest('#scoring-form')){if(['max_units','max_elapsed_seconds','max_total_tokens'].includes(el.name))el.dataset.edited='true';updateScoringPlan();}
    if(el.id==='decision-evaluation'){const selected=el.value?JSON.parse(el.value):[];modal.querySelector('[name=evaluator]').value=selected[0]||'';modal.querySelector('[name=rubric_version]').value=selected[1]||'';const dims=(state.current.comparison?.model_dimensions||[]).filter(r=>r.evaluator===selected[0]&&r.rubric_version===selected[1]).map(r=>r.dimension);$('decision-dimension').innerHTML=opt('','不设置质量维度')+[...new Set(dims)].map(d=>opt(d,label(d))).join('');}
    if(el.id==='project-filter'){
      filters.project=el.value;
      if(location.hash.startsWith('#experiments/project/'))history.replaceState(null,'',el.value?routeUrl('experiments','project',el.value):'#experiments');
      filterExperimentRows();
    }
    if(el.id==='experiment-status-filter'){filters.status=el.value;if(location.hash==='#experiments/issues'&&el.value!=='issues')history.replaceState(null,'','#experiments');filterExperimentRows();}
    if(el.id==='case-filter'){filters.case=el.value;filterTrialRows();}
    if(el.id==='trial-status-filter'){filters.trialStatus=el.value;filterTrialRows();}
    if(el.closest('#wizard-form')&&state.wizard){const w=state.wizard;captureWizard();if(['wizard-project','wizard-pack','wizard-split','wizard-baseline'].includes(el.id)){w.sourceRequest=(w.sourceRequest||0)+1;w.sourceLoading=false;}
      if(el.id==='wizard-project'){w.project=el.value;w.packId=state.catalog.find(p=>p.project===w.project)?.id;resetWizardPack();renderWizard();}
      else if(el.id==='wizard-pack'){w.packId=el.value;resetWizardPack();renderWizard();}
      else if(el.id==='wizard-split'){w.split=el.value;w.caseIds=wizardCases().map(c=>c.id);renderWizard();}
      else if(['wizard-source-kind','wizard-dataset','wizard-version'].includes(el.id)){
        const desired=el.value;w.sourceRequest=(w.sourceRequest||0)+1;w.sourceLoading=false;
        if(el.id==='wizard-source-kind'&&desired==='pack'){w.datasetId='';w.datasetPack=null;w.packId=state.catalog.find(p=>p.project===w.project)?.id||state.catalog[0]?.id;resetWizardPack();renderWizard();}
        else{const promise=loadWizardDataset(el.id==='wizard-source-kind'?state.datasets[0].id:el.id==='wizard-dataset'?desired:w.datasetId,el.id==='wizard-version'?desired:''),ticket=w.sourceRequest;try{const loaded=await promise;if(loaded&&state.wizard===w&&ticket===w.sourceRequest&&modal.open)renderWizard();}catch(error){if(state.wizard===w&&ticket===w.sourceRequest&&modal.open)throw error;}}
      }
      else if(el.id==='wizard-baseline'){w.candidates=w.candidates.filter(id=>id!==w.baseline);renderWizard();}
      else updateWizardScope();
    }
    if(['compare-case','compare-candidate','compare-repeat'].includes(el.id)&&state.compare){rememberComparison();const comparison=state.compare;comparison.caseId=$('compare-case').value;comparison.candidateId=$('compare-candidate').value;comparison.repeat=Number($('compare-repeat').value);await loadComparison();if(state.compare===comparison&&modal.open&&!comparison.loading&&window.matchMedia('(max-width:760px)').matches){const selector=modal.querySelector('.compare-selector');if(selector){const returnFocus=selector.contains(document.activeElement);selector.open=false;if(returnFocus)selector.querySelector('summary').focus({preventScroll:true});}}}
    if(el.id==='import-file'){state.importValue=null;$('import-preview').innerHTML='';const file=el.files[0];if(!file)return;if(file.size>50*1024*1024)throw new Error('文件超过 50 MB，请使用 CLI 或拆分版本导入。');const parsed=JSON.parse(await file.text());if(!parsed||typeof parsed!=='object'||Array.isArray(parsed))throw new Error('JSON 顶层需要是对象。');state.importValue=parsed;$('import-preview').innerHTML=`<div class="file-preview"><strong>${esc(file.name)}</strong><div class="subline">${(file.size/1024).toFixed(1)} KB · ${esc(parsed.name||parsed.title||parsed.bundle?.experiment?.title||'等待服务校验')}</div><p class="source-note">${parsed.cases?parsed.cases.length+' 个案例':parsed.traces?parsed.traces.length+' 条轨迹':'完整导出内容'} · ${esc(sourceLabel(parsed.source))}</p></div>`;$('form-error').textContent='';}
  }catch(error){if(['wizard-source-kind','wizard-dataset','wizard-version'].includes(el.id)&&state.wizard)renderWizard();if($('form-error'))$('form-error').textContent=error.message;else toast(error.message);}
});
document.addEventListener('click',async event=>{
  const b=event.target.closest('[data-action]');if(!b)return;const action=b.dataset.action;
  if(action!=='close-route')event.preventDefault();
  if(b.disabled)return;let ownsLock=false;
  try{
    if(action==='all-experiments'){filters.project=filters.search=filters.status='';location.hash='#experiments';return;}
    if(action==='abnormal-experiments'){filters.project=filters.search='';filters.status='issues';if(location.hash==='#experiments/issues')await render({refresh:true});else location.hash='#experiments/issues';return;}
    if(action==='close'||action==='close-route'){closeModal();return;}
    if(action==='failure-stage'){const index=Number(b.dataset.index),stage=$('stage-'+index);if(stage){stage.querySelector('details').open=true;stage.scrollIntoView({block:'center',behavior:'instant'});stage.tabIndex=-1;stage.focus({preventScroll:true});}else{state.pendingStage={trialId:b.dataset.trial,index};location.hash=routeUrl('trial',b.dataset.trial,'evidence');}return;}
    if(action==='compare-view'){if(state.compare&&!state.compare.loading){rememberComparison();state.compare.view=b.dataset.view;state.compare.quoteReturn=null;$('compare-quote-return').hidden=true;renderComparison();}return;}
    if(action==='compare-side'){switchComparisonSide(Number(b.dataset.side));return;}
    if(action==='compare-reference'){toggleCompareReference();return;}
    if(action==='compare-reference-return'){returnCompareReference();return;}
    if(action==='compare-evidence'){inspectCompareEvidence(b);return;}
    if(action==='compare-quote-return'){returnCompareEvidence();return;}
    if(action==='playbook-segment'){selectPlaybookSegment(Number(b.dataset.segment));return;}
    if(action==='playbook-chapter'){selectPlaybookChapter(Number(b.dataset.segment),Number(b.dataset.chapter));return;}
    if(action==='playbook-play'){await playPlaybook();return;}
    if(action==='playbook-fullscreen'){await fullscreenPlaybook();return;}
    if(action==='playbook-frame'){openPlaybookFrame();return;}
    if(action==='frame-zoom'){const viewport=modal.querySelector('.frame-viewport'),zoom=viewport.classList.toggle('is-zoomed');b.setAttribute('aria-pressed',String(zoom));b.innerHTML=icon('eye')+(zoom?'适合窗口':'原尺寸查看');modal.querySelector('.frame-tools>span').textContent=zoom?'原尺寸 · 可上下左右滚动':'适合窗口';return;}
    if(action==='new'){await openCreate({project:b.dataset.project||filters.project,packId:b.dataset.pack||'',datasetId:b.dataset.dataset||'',versionId:b.dataset.version||''});return;}
    if(action==='refresh'){await render({refresh:true});return;}
    if(action==='clear-filters'){filters.project=filters.status=filters.search='';if(location.hash==='#experiments/issues')location.hash='#experiments';else await render({refresh:true});return;}
    if(action==='wizard-back'){captureWizard();state.wizard.step--;renderWizard();return;}
    if(action==='select-all-cases'||action==='clear-cases'){state.wizard.caseIds=action==='select-all-cases'?wizardCases().map(c=>c.id):[];renderWizard();return;}
    if(action==='compare-output'){await openCompare('',b.dataset.candidate||'');return;}
    if(action==='new-job'||action==='rescore'||action==='rescore-attempt'){await openScoring({evaluatorId:b.dataset.evaluator,attempt:action==='rescore-attempt'?Number(b.dataset.attempt):null});return;}
    if(action==='import-dataset'||action==='import-traces'){openImport(action==='import-dataset'?'dataset':'traces');return;}
    if(action==='new-campaign'){await openCampaign();return;}
    if(action==='add-campaign-experiments'){await openCampaign(state.current?.id);return;}
    if(action==='new-decision'){openDecision();return;}
    if(action==='adjudicate'){openAdjudication(b.dataset.job,b.dataset.unit);return;}
    if(action==='new-calibration'){await openCalibration(b.dataset.calibration);return;}
    if(action==='restore'){openRestore(b.dataset.id);return;}
    if(action==='stop'){confirmStop();return;}
    b.disabled=true;ownsLock=true;
    const current=state.current;
    if(action==='confirm-stop'){await api(`/api/experiments/${current.id}/stop`,{});closeModal();toast('已停止后续调度，并请求在途任务取消');await render({refresh:true});}
    if(action==='pause'||action==='resume'){await api(`/api/experiments/${current.id}/${action}`,{});toast(action==='pause'?'已暂停后续调度，在途请求保留':'已恢复实验调度');await render({refresh:true});}
    if(action==='export'){await download(`/api/export/${current.id}?format=zip`+(filters.case?'&case_id='+encodeURIComponent(filters.case):''),`myeval-${current.id}.zip`);toast('证据已导出，包含失败、旧尝试与评价');}
    if(action==='export-revalidation'){await download(`/api/evaluation-revalidations/${encodeURIComponent(current.id)}/export`,`myeval-${current.id}.zip`);toast('完整收据与完整性清单已导出');}
    if(action==='export-dataset'){await download(`/api/datasets/${encodeURIComponent(current.id)}/versions/${encodeURIComponent(current.selectedVersion)}/export`,`dataset-${current.id}-${current.selectedVersion}.json`);toast('数据集版本已导出');}
    if(action==='retry'){await api(`/api/trials/${current.id}/retry`,{});toast('已建立新的执行尝试，原失败保留');await render({refresh:true});}
    if(action==='publish'){const result=await api(`/api/trials/${current.id}/publish`,{});toast('平台同步：'+label(result.status));await render({refresh:true});}
    if(action==='reviewer'){await api(`/api/experiments/${current.id}/reviewers`,{});toast('已建立独立匿名评审席位');await render({refresh:true});}
    if(action==='cancel-job'){await api(`/api/evaluation-jobs/${current.id}/cancel`,{});toast('已请求取消剩余评分；已完成判断保留');await render({refresh:true});}
    if(action==='toggle-queue'){await api('/api/queue',{paused:!current.paused});toast(current.paused?'队列已恢复调度':'队列已暂停');await render({refresh:true});}
    if(action==='backup'){
      const result=await api('/api/backups',{}),backup=result.backup||result;
      openModal(modalHead('恢复点已保存','数据库一致性快照与归属证据已写入本机备份目录。')+`<div class="dialog-body"><div class="backup-receipt"><span class="receipt-check">${icon('check')}</span><div><h3>${esc(backup.label||backup.name||'本机证据备份')}</h3><p>${number(backup.file_count)} 个文件 · ${backup.total_bytes!=null?(backup.total_bytes/1048576).toFixed(1)+' MB':'大小未返回'}</p></div></div><dl class="definition-list"><dt>清单完整性</dt><dd>${backup.verification?.manifest==='verified'?'已核对':'未返回校验结果'}</dd><dt>文件校验</dt><dd>${backup.verification?.files==='verified'?'已逐项核对':'恢复时再次校验'}</dd><dt>恢复方式</dt><dd>在备份列表选择“恢复到新目录”，保留当前工作区。</dd></dl><details><summary>存储位置、校验值与技术详情</summary>${json(result)}</details></div><div class="dialog-footer">${button('完成','close','primary')}</div>`);
      toast('已保存新的恢复点');await render({refresh:true});
    }
  }catch(error){if(modal.open&&$('form-error'))$('form-error').textContent=error.message;else if(modal.open&&modal.querySelector('.page-loading'))openModal(modalHead('暂时无法完成此操作')+`<div class="dialog-body">${note(error.message,'error')}</div><div class="dialog-footer">${button('关闭','close')}</div>`);else toast(error.message);}
  finally{if(ownsLock&&b.isConnected)b.disabled=false;}
});
document.addEventListener('submit',async event=>{
  const form=event.target;if(!['wizard-form','scoring-form','import-form','decision-form','restore-form','login-form','concurrency-form','adjudication-form','calibration-form','campaign-form'].includes(form.id))return;
  event.preventDefault();const submit=form.querySelector('[type=submit]');if(submit)submit.disabled=true;const values=new FormData(form),errorEl=form.querySelector('.form-error');if(errorEl)errorEl.textContent='';
  try{
    if(form.id==='wizard-form')await submitWizard(form);
    if(form.id==='login-form'){sessionStorage.setItem('myeval.admin',String(values.get('access')||'').trim());try{await loadCatalog();await render();}catch(error){sessionStorage.removeItem('myeval.admin');throw error;}}
    if(form.id==='scoring-form'){
      const payload={experiment_id:values.get('experiment_id'),evaluator_id:values.get('evaluator_id'),mode:values.get('mode'),rubric_id:values.get('rubric_id'),split:values.get('split'),timeout_seconds:Number(values.get('timeout_seconds')),budget:{max_units:Number(values.get('max_units')),max_elapsed_seconds:Number(values.get('max_elapsed_seconds')),max_total_tokens:Number(values.get('max_total_tokens'))}};
      if(values.get('title'))payload.title=values.get('title');if(state.scoreTargets)payload.targets=state.scoreTargets;
      if(String(values.get('rubric_json')||'').trim())payload.rubric=JSON.parse(values.get('rubric_json'));else if(payload.rubric_id==='__custom')throw new Error('请填写自定义量表 JSON，或选择一个已注册量表。');
      const job=await api('/api/evaluation-jobs',payload);closeModal();location.hash=routeUrl('job',job.id);toast('评分作业已建立，产品运行不会重新执行');
    }
    if(form.id==='import-form'){if(!state.importValue)throw new Error('请先选择有效的 JSON 文件。');const dataset=state.importKind==='dataset',result=await api(dataset?'/api/datasets/import':'/api/imports/traces',state.importValue);closeModal();if(dataset)location.hash=result.dataset_id||result.id?routeUrl('dataset',result.dataset_id||result.id):'#datasets';else location.hash=result.experiment_id?routeUrl('experiment',result.experiment_id):'#queue';toast('服务已校验并保存导入记录');await render();}
    if(form.id==='decision-form'){
      const policy={name:values.get('name'),version:'1',scope:values.get('scope'),min_families:Number(values.get('min_families')),min_success_rate:Number(values.get('min_success_rate'))/100,min_completion_rate:Number(values.get('min_completion_rate'))/100,min_human_review_pairs:Number(values.get('min_human_review_pairs')),require_holdout:values.has('require_holdout'),rules:{required:values.has('rules_required'),max_failures:0},model_criteria:[]};
      if(String(values.get('dimension')||'').trim()){const criterion={dimension:values.get('dimension').trim(),evaluator:String(values.get('evaluator')||'').trim(),rubric_version:String(values.get('rubric_version')||'').trim(),min_coverage:Number(values.get('min_coverage'))/100,min_paired_families:policy.min_families};for(const key of ['min_mean','min_delta'])if(String(values.get(key)||'').trim()!=='')criterion[key]=Number(values.get(key));if(!criterion.evaluator||!criterion.rubric_version)throw new Error('请填写该维度对应的评分器 ID 和量表版本。');if(criterion.min_mean===undefined&&criterion.min_delta===undefined)throw new Error('质量维度至少设置最低平均分或相对基线提升。');policy.model_criteria.push(criterion);}
      await api(`/api/experiments/${state.current.id}/decisions`,{policy});closeModal();toast('判定已保存，依据与不足均可复核');await render({refresh:true});
    }
    if(form.id==='restore-form'){const result=await api(`/api/backups/${state.restoreId}/restore`,{destination:values.get('destination')});openModal(modalHead('独立副本已恢复','原工作区未被覆盖。使用恢复目录另行启动以复核。')+`<div class="dialog-body">${content(result)}</div><div class="dialog-footer">${button('完成','close','primary')}</div>`);}
    if(form.id==='adjudication-form'){await api(`/api/evaluation-jobs/${state.adjudication.jobId}/units/${state.adjudication.unitId}/adjudications`,{kind:values.get('kind'),disposition:values.get('disposition'),reason:values.get('reason'),reviewer:values.get('reviewer')});closeModal();toast('复核意见已保存，原评分保留');await render({refresh:true});}
    if(form.id==='calibration-form'){const job=await api('/api/evaluation-jobs',{calibration_id:state.calibrationId,evaluator_id:values.get('evaluator_id'),title:values.get('title')});closeModal();location.hash=routeUrl('job',job.id);toast('规范校准作业已建立');}
    if(form.id==='campaign-form'){const ids=values.getAll('experiment_ids');if(!ids.length)throw new Error('至少选择一个实验。');const payload={experiment_ids:ids};if(!state.campaignEditId){payload.name=values.get('name');payload.purpose=values.get('purpose');}const result=await api(state.campaignEditId?`/api/campaigns/${state.campaignEditId}/experiments`:'/api/campaigns',payload);closeModal();location.hash=routeUrl('campaign',result.id);toast('批次关系已保存，实验运行状态未改变');await render();}
    if(form.id==='concurrency-form'){await api('/api/queue',{concurrency:Number(values.get('concurrency'))});toast('队列并发已更新');await render({refresh:true});}
  }catch(error){if(errorEl?.isConnected)errorEl.textContent=error.message;else toast(error.message);}
  finally{if(submit?.isConnected)submit.disabled=false;}
});

fillIcons();
const startup=await Promise.allSettled([api('/api/health'),sessionStorage.getItem('myeval.admin')?loadCatalog():Promise.resolve()]);
if(startup[0].status==='fulfilled'){state.health=startup[0].value;setService(true);$('app-version').textContent=state.health.version||'V2';}else setService(false);
await render();
let polling=false;
setInterval(async()=>{
  if(document.hidden||modal.open||polling||!sessionStorage.getItem('myeval.admin'))return;
  if(!['overview','experiments','experiment','trial','jobs','job'].includes(state.route))return;
  polling=true;const routeAtStart=state.route;
  try{
    // Poll only lightweight summaries. Reading and focused controls are never replaced.
    if(['jobs','job'].includes(state.route)){
      if(state.route==='job'&&state.current&&['running','queued','pending'].includes(state.current.status))$('refresh-notice').hidden=false;
    }else{const {experiments=[]}=await api('/api/experiments');if(state.route!==routeAtStart)return;const next=fingerprint(experiments);if(state.lastFingerprint&&next!==state.lastFingerprint)$('refresh-notice').hidden=false;else if(!state.lastFingerprint)state.lastFingerprint=next;}
    setService(true);
  }catch{setService(false);}finally{polling=false;}
},8000);

async function playbookPanel(ctx){
  const result=await optional(ctx,'/api/playbook'),payload=result.value;
  if(!payload?.available)return `<div class="guide-hero">${icon('video')}<div><h3>操作录屏与关键帧</h3><p>${esc(payload?.message||'操作记录尚未导入此工作区。录屏、章节和原片提取的关键帧就绪后，将在这里显示。')}</p><p class="mt-9"><span class="mono">docs/recording-playbook.md</span> · 本地录屏与生成方式</p>${result.error?`<p class="source-note">${esc(result.error.message)}</p>`:''}</div></div>`;
  const files=(payload.files||[]).filter(f=>typeof f.url==='string'&&f.url.startsWith('/api/playbook/files/'));
  let manifest=payload.manifest;
  if(!manifest?.segments){const index=files.find(f=>f.path==='evidence-index.json');if(index){try{manifest=await ctx.get(index.url);}catch(error){if(error.name==='AbortError')throw error;}}}
  const resolve=path=>{if(typeof path!=='string')return null;let decoded=path;try{decoded=decodeURIComponent(path);}catch{}const normalized=decoded.replace(/^\.\//,'');return files.find(f=>f.path===normalized)?.url||null;};
  const rawSegments=manifest?.segments||[];
  const segments=rawSegments.map(s=>({...s,streamUrl:resolve(s.video_url||s.path),chapters:(s.chapters||[]).map(c=>({...c,frameUrl:resolve(c.frame_url)}))}));
  if(!segments.length){for(const file of files.filter(f=>f.mime_type.startsWith('video/')))segments.push({id:file.path,title:file.path,streamUrl:file.url,state:'unknown',chapters:[],recording_note:'未提供章节清单，录制范围待核对。'});}
  state.playbook={segments,manifest,files};
  const first=segments.findIndex(s=>s.streamUrl),index=first>=0?first:0,segment=segments[index];
  if(!segment)return `<div class="guide-hero">${icon('video')}<div><h3>已导入记录文件</h3><p>当前没有可播放的真实视频。${files.length} 个证据文件已保留，不能据此声称录屏完成。</p></div></div>`;
  state.playbook.active=index;
  const coverage=manifest?.coverage?.note||'片段闭合只说明该原片已保存，不代表全部研发过程已录制。';
  state.playbook.chapter=segment.chapters.findIndex(c=>c.frameUrl);
  return `<section class="panel playbook-panel"><div class="panel-header"><div><h2>${esc(manifest?.title||'真实操作参考')}</h2><p class="source-note m-0">${esc(coverage)}</p></div>${icon('video')}</div><div class="playbook-player">${segment.streamUrl?`<video id="playbook-video" controls preload="metadata" src="${esc(segment.streamUrl)}" ${segment.chapters.find(c=>c.frameUrl)?`poster="${esc(segment.chapters.find(c=>c.frameUrl).frameUrl)}"`:''}></video>`:'<div class="empty compact"><p>本片段尚未提供可播放原片</p></div>'}${playbookPlayerTools(segment)}</div><label class="playbook-selector">录屏片段 · ${segments.length} 段<select id="playbook-segment-select" aria-label="选择录屏片段">${segments.map((s,i)=>opt(i,`${String(i+1).padStart(2,'0')} ${s.title}`,i===index)).join('')}</select></label><div class="playbook-segments">${segments.map((s,i)=>`<button type="button" class="small ${i===index?'primary':''}" data-action="playbook-segment" data-segment="${i}">${String(i+1).padStart(2,'0')} ${esc(s.title)}</button>`).join('')}</div><div id="playbook-detail">${playbookSegmentMarkup(segment,index)}</div><div class="panel-footer">${esc(manifest?.timestamp_note||'章节时间按实际记录映射；关键帧与操作起点可能不同。')}<details><summary>原片与覆盖范围</summary>${content({coverage:manifest?.coverage,warnings:manifest?.warnings,segments:segments.map(s=>({title:s.title,state:s.state,started_at:s.started_at,ended_at:s.ended_at,sha256:s.sha256,limitations:s.limitations}))})}</details></div></section>`;
}
function playbookPlayerTools(segment){
  return `<div class="playbook-player-tools">${button('播放此处','playbook-play','small','play')}${button('全屏观看','playbook-fullscreen','small','external')}${button('放大关键帧','playbook-frame','small','eye',segment.chapters.some(c=>c.frameUrl)?'':'disabled')}</div><p id="playbook-now" class="playbook-now" tabindex="-1" aria-live="polite">章节按钮只定位画面；点击“播放此处”继续。手机可全屏观看或放大原尺寸关键帧。</p>`;
}
function syncPlaybookPlayer(){
  const p=state.playbook,video=$('playbook-video'),status=$('playbook-now'),control=app.querySelector('[data-action="playbook-play"]');
  if(!p||!video||!status||!control)return;
  const ended=video.ended,playing=!video.paused&&!ended,title=p.segments[p.active]?.chapters[p.chapter]?.title||p.segments[p.active]?.title||'实际原片';
  const text=ended?'重新播放':playing?'暂停播放':'播放此处';control.innerHTML=icon(ended?'refresh':playing?'pause':'play')+esc(text);control.setAttribute('aria-pressed',String(playing));
  control.disabled=Boolean(video.error);
  if(video.error){status.textContent='原片暂时无法播放；章节记录与关键帧仍保留，可重新选择片段核对。';return;}
  if(p.seek?.video===video&&p.seek.pending){control.disabled=true;control.innerHTML=icon('video')+'正在定位…';return;}
  if(ended){status.textContent=(p.seek?.clamped?p.seek.boundary:'')+`播放已结束 · ${title}。可重新播放原片。`;return;}
  if(playing){status.textContent=(p.seek?.clamped?p.seek.boundary:'')+(video.readyState<3?'正在缓冲 · ':'正在播放 · ')+title;return;}
  const at=Math.floor(video.currentTime/60)+':'+(video.currentTime%60).toFixed(2).padStart(5,'0');
  status.textContent=p.seek?.settled&&Math.abs(video.currentTime-p.seek.target)<.25?`${p.seek.boundary}${title}。点击“播放此处”开始播放。`:`${p.seek?.clamped?p.seek.boundary:''}已暂停 ${at} · ${title}。点击“播放此处”继续。`;
}
function bindPlaybookPlayer(){
  const video=$('playbook-video'),p=state.playbook;if(!video||!p||video.dataset.eventsBound)return;
  video.dataset.eventsBound='true';const current=()=>video.isConnected&&$('playbook-video')===video&&state.playbook===p;let endedBefore=false;
  for(const event of ['play','playing','pause','ended','waiting','canplay','loadeddata','seeked','error'])video.addEventListener(event,()=>{if(current()){
    if(event==='ended')endedBefore=true;
    if((event==='play'||event==='seeked')&&endedBefore&&!video.ended){if(!p.seek?.pending)clearPlaybookChapter(video.currentTime<.25?'从原片片头重新播放；可选择下方章节定位。':'已使用播放器定位；选择下方章节可重新查看对应步骤与关键帧。');endedBefore=false;}
    if(event==='seeked'&&!p.seek?.pending&&p.seek?.settled&&Math.abs(video.currentTime-p.seek.target)>.25)clearPlaybookChapter('已使用播放器定位；选择下方章节可重新查看对应步骤与关键帧。');
    if(p.seek?.pending&&p.seek.video===video&&video.readyState>=2&&!video.seeking)p.seek.ready?.();syncPlaybookPlayer();
  }});
}
function clearPlaybookChapter(message){
  const p=state.playbook;if(!p)return;p.seek=null;p.chapter=-1;p.playSequence=(p.playSequence||0)+1;
  if($('playbook-step'))$('playbook-step').innerHTML=`<p class="source-note">${esc(message)}</p>`;
  const frame=app.querySelector('[data-action="playbook-frame"]');if(frame)frame.disabled=true;
  app.querySelectorAll('[data-action="playbook-chapter"]').forEach(b=>{b.classList.remove('selected');b.setAttribute('aria-pressed','false');});
}
function playbookSegmentMarkup(segment,index){
  const states={closed:'已闭合原片',partial:'部分片段',recording:'尚在录制',missing:'原片缺失',unknown:'覆盖范围待核对'};
  return `<div class="playbook-caption"><strong>${esc(segment.title)}</strong><span class="tag">${esc(states[segment.state]||'覆盖范围待核对')}</span><p>${esc(segment.recording_note||'实际原片；范围与限制以记录为准。')}</p></div><div id="playbook-step"></div><div class="playbook-chapters">${segment.chapters.map((c,i)=>`<button class="chapter-item" type="button" data-action="playbook-chapter" data-segment="${index}" data-chapter="${i}">${c.frameUrl?`<img src="${esc(c.frameUrl)}" alt="${esc(c.title)}的原片关键帧" loading="lazy">`:`<span class="chapter-placeholder">${icon('video')}</span>`}<span><small>${Number.isFinite(c.seconds)?Math.floor(c.seconds/60)+':'+String(Math.floor(c.seconds%60)).padStart(2,'0'):'时间待补'}</small><strong>${esc(c.title)}</strong></span><span class="chapter-seek">定位 ${icon('arrow')}</span></button>`).join('')||'<p class="source-note">此原片尚未提供章节与关键帧，可直接使用播放器核对。</p>'}</div>`;
}
function selectPlaybookSegment(index){
  const stateData=state.playbook,segment=stateData?.segments[index];if(!segment)return;stateData.active=index;stateData.seek=null;stateData.playSequence=(stateData.playSequence||0)+1;if($('playbook-segment-select'))$('playbook-segment-select').value=String(index);
  stateData.chapter=segment.chapters.findIndex(c=>c.frameUrl);$('playbook-video')?.pause();const player=app.querySelector('.playbook-player');player.innerHTML=(segment.streamUrl?`<video id="playbook-video" controls preload="metadata" src="${esc(segment.streamUrl)}"></video>`:'<div class="empty compact"><p>本片段没有可播放原片</p></div>')+playbookPlayerTools(segment);
  $('playbook-detail').innerHTML=playbookSegmentMarkup(segment,index);app.querySelectorAll('[data-action="playbook-segment"]').forEach(b=>b.classList.toggle('primary',Number(b.dataset.segment)===index));bindPlaybookPlayer();
}
function selectPlaybookChapter(segmentIndex,chapterIndex){
  if(state.playbook?.active!==segmentIndex)selectPlaybookSegment(segmentIndex);
  const segment=state.playbook?.segments[segmentIndex],chapter=segment?.chapters[chapterIndex];if(!chapter)return;state.playbook.chapter=chapterIndex;
  const at=Number.isFinite(chapter.seconds)?Math.floor(chapter.seconds/60)+':'+String(Math.floor(chapter.seconds%60)).padStart(2,'0'):'时间未记录',video=$('playbook-video');
  const p=state.playbook,sequence=p.playSequence=(p.playSequence||0)+1;
  const current=()=>video?.isConnected&&state.playbook===p&&p.playSequence===sequence&&p.active===segmentIndex&&p.chapter===chapterIndex;let targetTime=null;
  const ready=()=>{if(!current()||targetTime===null||video.seeking||video.readyState<2)return;if(Math.abs(video.currentTime-targetTime)>.25){const actual=Math.floor(video.currentTime/60)+':'+(video.currentTime%60).toFixed(2).padStart(5,'0');clearPlaybookChapter(`本次定位已被改变：原章节标记 ${at}，实际位置 ${actual}。旧章节绑定已取消，可重新选择章节。`);syncPlaybookPlayer();return;}const clamped=Math.abs(targetTime-(Number(chapter.seconds)||0))>.01,actualAt=Math.floor(video.currentTime/60)+':'+(video.currentTime%60).toFixed(2).padStart(5,'0'),boundary=clamped?`原章节标记 ${at} 超出原片范围，实际定位 ${actualAt}（片尾）。`:`已定位 ${at}。`;p.seek={video,target:targetTime,pending:false,settled:true,boundary,clamped};syncPlaybookPlayer();};
  p.seek={video,pending:true,ready};syncPlaybookPlayer();
  $('playbook-now').textContent=video?`正在定位 ${at} · ${chapter.title}…`:'此片段没有可播放原片，文字步骤和关键帧仍可核对。';
  const move=()=>{if(!current())return;video.pause();const target=Math.min(Math.max(0,Number(chapter.seconds)||0),Number.isFinite(video.duration)?Math.max(0,video.duration-.05):Number(chapter.seconds)||0);targetTime=target;if(Math.abs(video.currentTime-target)<.01&&!video.seeking&&video.readyState>=2){ready();return;}video.addEventListener('seeked',ready,{once:true});video.currentTime=target;};
  if(video){if(video.readyState>=1)move();else video.addEventListener('loadedmetadata',move,{once:true});}
  app.querySelector('[data-action="playbook-frame"]').disabled=!chapter.frameUrl;
  $('playbook-step').innerHTML=`<div class="playbook-step"><span class="eyebrow">STEP ${chapterIndex+1}</span><h3>${esc(chapter.title)}</h3>${chapter.note?`<p>${esc(chapter.note)}</p>`:''}${chapter.steps?.length?`<ol class="playbook-instructions">${chapter.steps.map(step=>`<li>${esc(step)}</li>`).join('')}</ol>`:'<p>操作步骤尚未记录。</p>'}<details><summary>预期、实际观察与错误恢复</summary><dl class="definition-list"><dt>预期结果</dt><dd>${esc(chapter.expected||'记录待补充')}</dd><dt>实际观察</dt><dd>${esc(chapter.observed||'记录待补充')}</dd><dt>错误与恢复</dt><dd>${esc(chapter.recovery||'记录未提供')}</dd></dl></details>${chapter.frame_note?`<p class="source-note">${esc(chapter.frame_note)}</p>`:''}</div>`;
  app.querySelectorAll('[data-action="playbook-chapter"]').forEach(b=>{const selected=Number(b.dataset.chapter)===chapterIndex;b.classList.toggle('selected',selected);b.setAttribute('aria-pressed',String(selected));});
  app.querySelector('.playbook-player').scrollIntoView({block:'start',behavior:'instant'});$('playbook-now').focus({preventScroll:true});
}
async function playPlaybook(){
  const video=$('playbook-video'),p=state.playbook;if(!video)throw new Error('此片段没有可播放原片。');if(p?.seek?.pending)return;if(!video.paused&&!video.ended){video.pause();return;}
  if(video.ended){clearPlaybookChapter('从原片片头重新播放；可选择下方章节定位。');video.currentTime=0;}
  const sequence=p.playSequence;try{await video.play();if(state.playbook===p&&p.playSequence===sequence&&$('playbook-video')===video)syncPlaybookPlayer();}catch(error){if(state.playbook===p&&p.playSequence===sequence&&$('playbook-video')===video)throw error;}
}
async function fullscreenPlaybook(){const video=$('playbook-video');if(!video)throw new Error('此片段没有可播放原片。');if(video.requestFullscreen)await video.requestFullscreen();else if(video.webkitEnterFullscreen)video.webkitEnterFullscreen();else toast('当前浏览器未提供全屏入口，可放大关键帧核对文字。');}
function openPlaybookFrame(){
  const p=state.playbook,segment=p?.segments[p.active],chapter=segment?.chapters[p.chapter];if(!chapter?.frameUrl)throw new Error('当前章节没有可放大的原片关键帧。');
  $('playbook-video')?.pause();syncPlaybookPlayer();openModal(modalHead(chapter.title)+`<div class="dialog-body frame-dialog"><div class="frame-tools">${button('原尺寸查看','frame-zoom','small','eye','aria-pressed="false" aria-controls="frame-viewport"')}<span>适合窗口</span><small id="frame-size">正在读取原图尺寸</small></div><p id="frame-keyboard-help" class="frame-keyboard-help">Tab 进入图像；方向键平移，Page Up / Down 翻页，Home / End 到首尾。</p><div id="frame-viewport" class="frame-viewport" tabindex="0" role="region" aria-label="原片关键帧可滚动区域" aria-describedby="frame-keyboard-help"><img id="playbook-frame-image" src="${esc(chapter.frameUrl)}" alt="${esc(chapter.title)} · 原片关键帧"></div><div class="frame-boundary"><p>关键帧来自原片；单帧不能证明完整操作过程。</p>${chapter.frame_note?`<details><summary>提帧记录</summary>${esc(chapter.frame_note)}</details>`:''}</div></div>`);
  modal.classList.add('frame-reader-dialog');const image=$('playbook-frame-image');const size=()=>{if(image.isConnected)$('frame-size').textContent=`原图 ${image.naturalWidth} × ${image.naturalHeight}`;};if(image.complete&&image.naturalWidth)size();else image.addEventListener('load',size,{once:true});
}

function reviewQueueTable(items){
  if(!items.length)return `<div class="panel">${empty('当前没有待复核项','','','eye',true)}</div>`;
  return `<div class="panel table-wrap"><table><thead><tr><th>案例 / 评分单元</th><th>评分状态</th><th>问题</th><th>复核情况</th><th></th></tr></thead><tbody>${items.map(i=>`<tr><td><a class="cell-title" href="${routeUrl('job',i.job_id)}">${esc(i.case_id)}</a><div class="subline mono">${esc(i.unit_id?.slice(0,8))}</div></td><td>${badge(i.status)}</td><td>${esc(i.error?.message||i.error?.code||'需人工核对证据')}<details><summary>查看标记与校验问题</summary>${content({flags:i.flags,validation_issues:i.validation_issues})}</details></td><td><span class="tag">${i.adjudicated?'已有复核意见':'待复核'}</span></td><td>${button('记录意见','adjudicate','small','eye',`data-job="${esc(i.job_id)}" data-unit="${esc(i.unit_id)}"`)}</td></tr>`).join('')}</tbody></table></div>${help('复核的影响','复核只追加意见，不覆盖模型评分，也不会把无效评分自动改为有效。')}`;
}
function openAdjudication(jobId,unitId){
  state.adjudication={jobId,unitId};openModal(modalHead('复核意见')+`<form id="adjudication-form"><div class="dialog-body"><div class="form-grid"><label class="mt-0">复核结论<select name="disposition"><option value="needs_followup">需要进一步核对</option><option value="accepted">接受此判断</option><option value="rejected">不接受此判断</option></select></label><label class="mt-0">评价用途<select name="kind"><option value="human">真实人工复核</option><option value="acceptance">验收试填</option></select></label></div><label>复核者<input name="reviewer" maxlength="100" placeholder="可选姓名或评审标识"></label><label>依据与理由<textarea name="reason" rows="5" required maxlength="10000" placeholder="引用具体证据，说明接受、拒绝或仍需核对的原因。"></textarea></label><p id="form-error" class="form-error" role="alert"></p></div><div class="dialog-footer"><span class="muted">不覆盖原评分或重跑模型</span><div class="action-row">${button('取消','close')}<button type="submit" class="primary">保存复核意见 ${icon('check')}</button></div></div></form>`);
}
async function openCalibration(id){
  const {evaluators=[]}=await api('/api/evaluators');if(!evaluators.length)throw new Error('没有可用评分器。');state.calibrationId=id;
  openModal(modalHead('运行评分规范校准')+`<form id="calibration-form"><div class="dialog-body"><label class="mt-0">作业名称<input name="title" value="通用规范锚点校准" required maxlength="160"></label><label>评分器<select name="evaluator_id">${evaluators.map(e=>opt(e.id,`${e.name||e.id} · ${e.id}`)).join('')}</select></label><div class="notice section">将实际发起独立评分请求。即使与锚点一致，也不能据此推断真实业务有效性；分歧会完整保留。</div><p id="form-error" class="form-error" role="alert"></p></div><div class="dialog-footer">${button('取消','close')}<button class="primary" type="submit">创建校准作业 ${icon('play')}</button></div></form>`);
}
function calibrationPanel(report){
  return `<section class="section">${sectionHead('规范锚点一致性','描述性校准，不等同真实用户或专家一致率')}<div class="panel"><div class="panel-header"><span>${report.rated_cells} / ${report.expected_cells} 个分项已评分</span><span class="tag">${report.exact_matches} 项与锚点一致 · ${report.missing_cells} 项缺失</span></div><div class="table-wrap"><table><thead><tr><th>规范案例</th><th>维度</th><th>锚点</th><th>实际</th><th>判断</th></tr></thead><tbody>${(report.comparisons||[]).map(c=>`<tr><td>${esc(c.case_id)}</td><td>${esc(label(c.dimension))}</td><td>${esc(c.expected)}</td><td>${c.available?esc(c.actual):'不可用'}</td><td>${c.matches===null?'无法判断':c.matches?'一致':'存在分歧'}</td></tr>`).join('')}</tbody></table></div><div class="panel-footer">${esc(report.provenance)}</div></div></section>`;
}

async function campaignsPage(ctx){
  const result=await optional(ctx,'/api/campaigns'),rows=result.value?.campaigns||[];
  return pageTitle('实验批次','',button('建立批次','new-campaign','primary','plus'))+tabs([['experiments','实验记录','experiments','#experiments'],['campaigns','实验批次','layers','#campaigns']],'campaigns')+(result.error?unavailable('实验批次暂未连接',result.error):`<div class="panel">${rows.length?`<div class="table-wrap"><table><thead><tr><th>批次</th><th>项目</th><th>实验数</th><th>进度</th><th>判定</th><th></th></tr></thead><tbody>${rows.map(c=>`<tr><td><a class="cell-title" href="${routeUrl('campaign',c.id)}">${esc(c.name)}</a><div class="subline">${esc(c.purpose||'')}</div></td><td>${(c.projects||[]).map(p=>`<span class="tag">${esc(label(p))}</span>`).join(' ')}</td><td>${c.experiment_count}</td><td>${Object.entries(c.progress||{}).map(([status,n])=>`${esc(label(status))} ${n}`).join(' · ')}</td><td>${Object.entries(c.decision_counts||{}).map(([status,n])=>`${esc(label(status))} ${n}`).join(' · ')||'尚未判定'}</td><td><a class="row-arrow" href="${routeUrl('campaign',c.id)}" aria-label="查看批次">${icon('chevron')}</a></td></tr>`).join('')}</tbody></table></div>`:empty('暂无实验批次','',button('建立首个批次','new-campaign','primary','plus'),'layers')}</div>`);
}
async function campaignPage(ctx,id){
  const c=await ctx.get('/api/campaigns/'+encodeURIComponent(id));ctx.current={...c,_kind:'campaign'};
  return `<a class="back" href="#campaigns">${icon('back')}全部批次</a>`+pageTitle(c.name,c.purpose||'',button('关联已有实验','add-campaign-experiments','','plus'),`${badge(c.status)}<span>${date(c.created_at)}</span>`)+
    `<div class="metric-strip">${metric('关联实验',number(c.experiment_count),'不重新发送产品请求')}${metric('项目',number(c.projects?.length),'不合并跨项目分数')}${metric('运行中',number(c.progress?.running||0),'按实验状态计数')}${metric('尚未判定',number(c.decision_counts?.not_evaluated||0),'待证据完整后作出判断')}</div>`+
    `<div class="panel table-wrap"><table><thead><tr><th>实验</th><th>项目</th><th>状态</th><th>进度</th><th>最近判定</th><th></th></tr></thead><tbody>${(c.entries||[]).map(entry=>`<tr><td>${entry.experiment_id?`<a class="cell-title" href="${routeUrl('experiment',entry.experiment_id)}">${esc(entry.title||entry.experiment_id)}</a>`:`<strong>未建立成功</strong><div class="subline error-text">${esc(entry.error?.message||entry.error||entry.kind)}</div>`}</td><td>${esc(label(entry.project||'未知'))}</td><td>${badge(entry.progress?.status||entry.kind)}</td><td>${entry.progress?`${entry.progress.terminal_trials} / ${entry.progress.total_trials}`:'—'}</td><td>${entry.latest_decision?`${badge(entry.latest_decision.status)}${entry.latest_decision.evidence_current===false?'<div class="subline">历史判定 · 证据已更新</div>':''}${entry.latest_decision.matches_campaign_policy===false?'<div class="subline">与批次策略不同</div>':''}`:'尚未判定'}</td><td>${entry.experiment_id?`<a class="text-link" href="${routeUrl('experiment',entry.experiment_id,'decisions')}">判定依据 ${icon('arrow')}</a>`:''}</td></tr>`).join('')}</tbody></table></div>${help('批次统计范围','批次只组织实验与追踪进度，不把不同项目、量表或评分器的结果平均为总分。')}<details><summary>批次策略、操作记录与部分失败</summary>${json({policy:c.policy,operations:c.operations,incomplete_operations:c.incomplete_operations,limitations:c.limitations})}</details>`;
}
async function openCampaign(campaignId=''){
  const {experiments=[]}=await api('/api/experiments'),existing=new Set(campaignId?(state.current?.entries||[]).map(e=>e.experiment_id):[]),available=experiments.filter(e=>!existing.has(e.id));
  if(!available.length)throw new Error('当前没有可关联的新实验，请先建立实验。');state.campaignEditId=campaignId;
  openModal(modalHead(campaignId?'关联已有实验':'建立一个实验批次','组织已有实验，不改变其调度状态，也不重跑模型。')+`<form id="campaign-form"><div class="dialog-body">${campaignId?'':`<label class="mt-0">批次名称<input name="name" required maxlength="160" placeholder="如：三项目 · 当前需求理解优化"></label><label>本轮目的<textarea name="purpose" rows="2" maxlength="2000" placeholder="这轮比较要回答什么问题？"></textarea></label>`}<fieldset><legend>选择相关实验</legend><div class="case-choices">${available.map(e=>`<label class="check-row"><input type="checkbox" name="experiment_ids" value="${esc(e.id)}"><span>${esc(e.title)}<small>${esc(label(e.project))} · ${esc(label(e.status))} · ${date(e.created_at)}</small></span></label>`).join('')}</div></fieldset><p id="form-error" class="form-error" role="alert"></p></div><div class="dialog-footer"><span class="muted">不同项目的结论分别保留</span><div class="action-row">${button('取消','close')}<button class="primary" type="submit">${campaignId?'保存关联':'建立批次'} ${icon('check')}</button></div></div></form>`);
}
