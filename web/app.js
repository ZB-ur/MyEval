import {esc,label,badge,json,content,api,date,duration,download} from '/view.js';

if(location.hash.startsWith('#access=')){
  sessionStorage.setItem('myeval.admin',location.hash.slice(8));
  history.replaceState(null,'','/');
}

const $ = id => document.getElementById(id);
let catalog=[], projectFilter='', caseFilter='', statusFilter='', current=null, busy=false;
const terminal = new Set(['succeeded','failed','timed_out','cancelled','interrupted','refused','unsupported']);
const option = (value,text,selected=false) => `<option value="${esc(value)}" ${selected?'selected':''}>${esc(text)}</option>`;
function toast(message){$('toast').textContent=message;$('toast').hidden=false;setTimeout(()=>$('toast').hidden=true,4500);}
function metric(name,value,foot=''){return `<div class="metric"><div class="label">${name}</div><div class="number">${value}</div><div class="foot">${foot}</div></div>`;}
function pageTitle(eye,title,subtitle,actions=''){return `<div class="page-title"><div><span class="eyebrow">${eye}</span><h1>${esc(title)}</h1><p>${esc(subtitle)}</p></div><div class="action-row">${actions}</div></div>`;}
const createButton='<button class="primary" data-action="new">＋ 新建实验</button>';
function exportLink(id,format='zip'){return `/api/export/${id}?format=${format}${caseFilter?'&case_id='+encodeURIComponent(caseFilter):''}`;}

async function listPage(){
  const {experiments}=await api('/api/experiments'+(projectFilter?'?project='+projectFilter:''));
  current=null;
  const trialCount=experiments.reduce((n,e)=>n+e.total_trials,0);
  const active=experiments.filter(e=>e.status==='running').length;
  const human=experiments.reduce((n,e)=>n+Object.values(e.comparison.reviews.human).reduce((a,b)=>a+b,0),0);
  $('app').innerHTML=pageTitle('EXPERIMENTS','让每次改动，有据可比。','在真实业务路径中比较配置，保留结果、失败和判断的依据。',createButton)+
    `<div class="metrics">${metric('实验',experiments.length,'已持久化的运行')}${metric('执行 trial',trialCount,'包含失败、超时与取消')}${metric('正在运行',active,'全局最多 2 个并发')}${metric('人工评价',human,'验收试填单独计数')}</div>`+
    `<section class="section"><div class="section-head"><h2>实验记录</h2><div class="toolbar"><select id="project-filter" aria-label="筛选项目">${option('','全部项目',!projectFilter)}${catalog.map(p=>option(p.project,label(p.project),p.project===projectFilter)).join('')}</select><button class="small" data-action="refresh">刷新</button></div></div><div class="panel">`+
    (experiments.length?`<div class="table-wrap"><table><thead><tr><th>实验 / 任务包</th><th>项目</th><th>状态</th><th>进度</th><th>规模</th><th>建立时间</th><th></th></tr></thead><tbody>${experiments.map(e=>`<tr class="clickable" data-route="experiment/${e.id}"><td><a class="cell-title" href="#experiment/${e.id}">${esc(e.title)}</a><div class="subline">${esc(e.taskpack.name)} · v${esc(e.taskpack.version)}</div></td><td>${esc(label(e.project))}</td><td>${badge(e.status)}</td><td><div class="subline">${e.completed_trials} / ${e.total_trials}</div><progress value="${e.completed_trials}" max="${e.total_trials}" aria-label="完成进度"></progress></td><td>${e.independent_cases} 案例 × ${e.repeats} 次<div class="subline">2 种配置</div></td><td>${date(e.created_at)}</td><td>↗</td></tr>`).join('')}</tbody></table></div>`:
      `<div class="empty"><div class="empty-mark">◫</div><h2>从第一组真实对比开始</h2><p>选择一个任务包，用相同的虚构材料运行两种配置。结果出现后，就可以进入匿名评审。</p>${createButton}<div class="foot">思间发现 / 复盘　·　pome 多轮接话 / 整理</div></div>`)+`</div></section><div class="notice">首版只执行明确标记的虚构材料。执行成功表示业务路径完成；内容质量仍需结合原文和分项评价判断。</div>`;
}

async function experimentPage(id){
  const e=await api('/api/experiments/'+id);current=e;
  const human=Object.values(e.comparison.reviews.human).reduce((a,b)=>a+b,0), acceptance=Object.values(e.comparison.reviews.acceptance).reduce((a,b)=>a+b,0);
  const trialRows=e.trials.filter(t=>(!caseFilter||t.case_id===caseFilter)&&(!statusFilter||t.status===statusFilter));
  const cases=Object.values(Object.fromEntries(e.trials.map(t=>[t.case_id,{id:t.case_id,name:t.case_name}])));
  const configMap=Object.fromEntries(e.configurations.map(c=>[c.id,c]));
  const failures=e.total_trials-(e.counts.succeeded||0)-(e.counts.queued||0)-(e.counts.running||0)-(e.counts.cancelling||0);
  $('app').innerHTML=`<a class="back" href="#">← 所有实验</a>`+pageTitle('EXPERIMENT · '+e.id.slice(0,8),e.title,`${label(e.project)} / ${e.taskpack.name} · v${e.taskpack.version}`,`${e.status==='running'?'<button class="danger" data-action="stop">停止实验</button>':''}<button data-action="export">导出证据 ↓</button>`)+
    `<div class="action-row">${badge(e.status)}<span class="tag">虚构材料</span><span class="muted">${date(e.created_at)}</span></div><div class="metrics">${metric('执行进度',`${e.completed_trials}<span class="muted"> / ${e.total_trials}</span>`,'全部 trial 进入比较分母')}${metric('独立案例',e.independent_cases,`每配置重复 ${e.repeats} 次`)}${metric('异常终态',failures,'失败 / 超时 / 取消 / 中断')}${metric('人工评价',human,`${acceptance} 份验收试填 · 单独记录`)}</div>`+
    `<div class="two-columns">${e.configurations.map((c,i)=>`<div class="panel config-card"><span class="eyebrow">${i?'CANDIDATE':'BASELINE'}</span><h3>${esc(c.name)}</h3><p>${esc(label(c.mode))}</p><div class="mono">${esc(JSON.stringify(c.parameters))}</div><details><summary>来源、能力与配置快照</summary>${json(c.provenance)}</details></div>`).join('')}</div>`+
    `<section class="section"><div class="section-head"><h2>逐项运行</h2><div class="toolbar"><select id="case-filter" aria-label="筛选场景">${option('','全部场景',!caseFilter)}${cases.map(c=>option(c.id,c.name,c.id===caseFilter)).join('')}</select><select id="status-filter" aria-label="筛选状态">${option('','全部状态',!statusFilter)}${Object.keys(e.counts).map(s=>option(s,label(s),s===statusFilter)).join('')}</select></div></div><div class="panel table-wrap"><table><thead><tr><th>案例</th><th>配置</th><th>重复 / 尝试</th><th>状态</th><th>耗时</th><th>平台同步</th><th></th></tr></thead><tbody>${trialRows.map(t=>`<tr><td><a class="cell-title" href="#trial/${t.id}">${esc(t.case_name)}</a>${t.error?`<div class="subline error-text">${esc(t.error.code||t.error)}</div>`:''}</td><td>${esc(configMap[t.configuration_id]?.name||t.configuration_id)}</td><td>${t.repeat_index} / ${t.attempt}</td><td>${badge(t.status)}</td><td>${duration(t.duration_ms)}</td><td>${t.platform?badge(t.platform.status):'<span class="muted">待同步</span>'}</td><td><a href="#trial/${t.id}">查看 →</a></td></tr>`).join('')||'<tr><td colspan="7" class="empty">没有符合筛选条件的 trial</td></tr>'}</tbody></table></div><p class="source-note">重试沿用 trial 身份并新增执行尝试。导出按当前场景筛选；原失败与每次尝试都保留。</p></section>`+
    `<section class="section"><div class="section-head"><div><h2>匿名评审</h2><p class="muted">单独入口随机分配 A / B；本页为管理员视图。</p></div><button class="small" data-action="reviewer">＋ 新增评审席位</button></div><div class="panel">${e.reviewers.map(r=>`<div class="review-link"><div><strong>${esc(r.label)}</strong><p>提交该配对评价后才可揭示配置映射</p></div><a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer" class="button small">进入匿名评审 ↗</a></div>`).join('')}</div><p class="source-note">已看过管理员结果后的试填仅用于功能验收。人工分歧保留；当前 ${e.comparison.disagreement_pairs} 个配对有分歧。</p></section>`+
    `<section class="section"><div class="section-head"><h2>分项比较</h2><span class="muted">不合成总分</span></div><div class="panel table-wrap"><table><thead><tr><th>配置</th><th>执行成功 / 全部</th><th>所有执行尝试</th><th>平均耗时</th><th>自动规则</th><th>人工胜出</th><th>费用</th></tr></thead><tbody>${Object.values(e.comparison.configurations).map(c=>`<tr><td>${esc(c.name)}</td><td>${c.statuses.succeeded||0} / ${c.trials}</td><td>${Object.entries(c.attempt_statuses||{}).map(([k,v])=>`${esc(label(k))} ${v}`).join(" · ")}</td><td>${duration(c.mean_duration_ms)}</td><td>${Object.entries(c.rules).map(([k,v])=>`${esc(label(k))} ${v}`).join(' · ')||'未运行'}</td><td>${human?c.human_wins:'待评'}</td><td>${c.unknown_cost_trials?'未知（'+c.unknown_cost_trials+' 项）':c.known_cost_total}</td></tr>`).join('')}</tbody></table></div><p class="source-note">独立模型评价：${e.comparison.model_assessments?'已导入 '+e.comparison.model_assessments+' 条':'未运行'}。产品审校器属于执行链，不计入独立模型评分。验收试填不计入人工偏好。</p>${Object.keys(e.comparison.dimensions).length?`<details><summary>分项人工偏好及验收试填</summary>${json(e.comparison.dimensions)}</details>`:''}</section>`;
}

async function trialPage(id){
  const t=await api('/api/trials/'+id);current=t;
  const r=t.result||{}, rules=r.assessments||[];
  $('app').innerHTML=`<a class="back" href="#experiment/${t.experiment_id}">← 返回实验</a>`+pageTitle('TRIAL · '+t.id.slice(0,8),t.case.name,`${t.configuration.name} · 重复 ${t.repeat_index} · 执行尝试 ${t.attempt}`,terminal.has(t.status)&&t.status!=='succeeded'?'<button data-action="retry">重试此 trial</button>':'')+
    `<div class="action-row">${badge(t.status)}<span class="tag">${esc(label(t.configuration.mode))}</span><span class="tag">虚构材料</span></div><div class="metrics">${metric('本次耗时',duration(t.duration_ms),`上限 ${t.timeout_seconds} 秒`)}${metric('执行尝试',t.attempt,'旧失败永久保留')}${metric('规则检查',rules.length,'只检查声明的可观察边界')}${metric('费用',r.cost===null||r.cost===undefined?'未知':esc(JSON.stringify(r.cost)),'没有计价依据时不记为零')}</div>`+
    (r.error?`<div class="notice error"><strong>${esc(r.error.code||'执行异常')}${r.error.http_status?` · HTTP ${esc(r.error.http_status)}`:''}${r.error.endpoint?` · ${esc(r.error.endpoint)}`:''}</strong><br>${esc(r.error.message||r.error.detail||r.error.details?.error||'请展开阶段轨迹和尝试记录定位。')}</div>`:'')+
    (t.cancellation?`<div class="notice warning section">停止事实：${esc(t.cancellation.phase)}。本地停止、产品终态、供应商计算是否终止分别记录。${json(t.cancellation)}</div>`:'')+
    `<section class="section"><div class="section-head"><h2>实际交付结果</h2><span class="muted">长内容可滚动阅读</span></div><div class="panel content-box">${r.review_output?content(r.review_output):'<p class="muted">尚未取得交付结果。阶段证据会随运行写入。</p>'}</div></section>`+
    `<div class="two-columns"><div class="panel panel-pad"><h3>输入快照</h3><p class="source-note">${esc(t.case.source?.description||'')} · 截止 ${esc(t.case.source?.as_of||'未知')}</p><details open><summary>本次输入</summary><div class="content-box">${content(t.case.input)}</div></details><details><summary>初始状态与期望边界</summary>${json({initial_state:t.case.initial_state,expected_behavior:t.case.expected_behavior})}</details></div><div class="panel panel-pad"><h3>配置与实际观察</h3><dl class="definition-list"><dt>模式</dt><dd>${esc(label(t.configuration.mode))}</dd><dt>案例版本</dt><dd>${esc(t.case.version)}</dd><dt>案例 SHA</dt><dd class="mono">${esc(t.case_hash)}</dd><dt>配置 SHA</dt><dd class="mono">${esc(t.configuration_hash)}</dd><dt>开始 / 结束</dt><dd>${date(t.started_at)} / ${date(t.ended_at)}</dd></dl><details open><summary>请求值与有效值</summary>${json({requested:t.configuration.parameters,observed:r.observed_configuration||'尚未返回；未知'})}</details><details><summary>真实用量 / 未知项</summary>${json(r.usage??null)}</details><details><summary>代码、提示词与资源来源</summary>${json(t.configuration.provenance)}</details>${t.platform?`<details><summary>平台同步 ${esc(label(t.platform.status))}</summary>${json(t.platform)}${t.platform.url?`<a target="_blank" rel="noopener noreferrer" href="${esc(t.platform.url)}">在本机 Langfuse 查看 ↗</a>`:''}<button class="small" data-action="publish">重新同步本地证据</button></details>`:''}</div></div>`+
    `<section class="section"><div class="section-head"><h2>评价与限制</h2></div><div class="panel panel-pad">${rules.length?`<table><thead><tr><th>自动规则</th><th>结果</th><th>依据 / 限制</th></tr></thead><tbody>${rules.map(a=>`<tr><td>${esc(a.dimension||a.name)}</td><td>${badge(a.status)}</td><td>${esc(a.reason)}</td></tr>`).join('')}</tbody></table>`:'<p class="muted">未产生规则结果。</p>'}<p>独立模型评价：${t.assessment_records.length?`${t.assessment_records.length} 条外部导入`:'未运行'}</p>${t.assessment_records.length?json(t.assessment_records):''}<p class="muted">人工偏好在实验的匿名评审入口记录。</p><ul>${(r.limitations||[]).map(l=>`<li class="muted">${esc(l)}</li>`).join('')}</ul></div></section>`+
    `<section class="section"><div class="section-head"><h2>阶段轨迹</h2><span class="muted">${t.events.length} 个可观察事件 · 无隐藏思维链</span></div><div class="panel timeline">${t.events.map(event=>`<div class="event"><time>${date(event.timestamp).split(' ').at(-1)||''}</time><div><h4>${esc(event.kind)}</h4><div>${['execution_attempt','episode','turn','stage','attempt'].filter(k=>event[k]!==undefined).map(k=>`<span class="tag">${esc(k)} ${esc(event[k])}</span>`).join('')}</div><details><summary>查看事件</summary>${json(event.data)}</details></div></div>`).join('')||'<p class="muted">等待开始</p>'}</div></section>`+
    `<details><summary>全部执行尝试（${t.attempts.length}）及管理员原始结果</summary>${json({attempts:t.attempts,output:r.output})}</details>`;
}

function catalogPage(){
  current=null;
  $('app').innerHTML=pageTitle('TASK PACKS','任务包与适配能力','公共流程保持一致，项目量表和业务边界由适配器声明。',createButton)+catalog.map(p=>`<div class="panel pack-card"><span class="eyebrow">${esc(p.project.toUpperCase())} · V${esc(p.version)}</span><h2>${esc(p.name)}</h2><p class="muted">${esc(p.description)}</p><div class="rubric">${p.rubric.map(r=>`<span title="${esc(r.description)}">${esc(r.name)}</span>`).join('')}</div><ul>${p.cases.map(c=>`<li><strong>${esc(c.name)}</strong> <span class="tag">${esc(c.task)}</span> <span class="tag">虚构材料</span></li>`).join('')}</ul><details><summary>能力矩阵与模式定义</summary>${json(p.configurations[0]?.provenance)}</details><details><summary>版本化完整任务包</summary>${json(p)}</details></div>`).join('');
}

function guidePage(){
  current=null;
  $('app').innerHTML=pageTitle('GETTING STARTED','从运行到判断','首版范围：本机、虚构文字材料、两个真实业务适配器。')+`<div class="panel panel-pad guide"><h2>一次完整对比</h2><ol><li>新建实验，选择项目、案例与两个配置。输入和配置会在建立实验时冻结。</li><li>查看运行进度。执行成功、失败、超时、取消和中断都保留；打开 trial 看交付结果、阶段、来源和未知项。</li><li>把单独的匿名评审入口交给评审者。左右顺序随机且持久化，页面不加载配置映射；提交后可主动揭示。</li><li>按维度记录理由。真实人工判断与验收试填分开，已有评价不能被覆盖，可新建评审席位保留分歧。</li><li>按场景筛选并导出 ZIP。里面包括案例、配置、每次尝试、轨迹及评价，不依赖平台在线。</li></ol><h2>如何理解结果</h2><p>重复 2 次意味着对同一案例采样两次，独立样本数仍为 1。规则检查只验证声明的结构和边界；产品审校属于执行链；独立模型评价默认未运行，人工偏好默认待评。</p><p>费用没有计价依据时显示未知。请求的模型和思考强度与供应商回显分开，未回显的字段不视为已生效。</p><h2>停止与恢复</h2><p>停止实验先停止未开始的调度，再请求在途任务取消。进程中断不等于供应商停止计费。重启服务不会自动重发请求；重试保留原失败，最多三次执行尝试。</p><h2>仍未覆盖</h2><p>旧轨迹重评分暂不支持。文本 HTTP 旅程不替代思间 iOS 操作、pome 浏览器自动保存、ASR/TTS、真实儿童或真人设备验收。当前本机权限和存储方式属于开发阶段。</p><p class="muted">启动入口和证据说明见仓库 README 与 docs/acceptance/first-release.md。</p></div>`;
}

async function render(){
  if(busy||$('create-dialog').open)return;
  if(!sessionStorage.getItem('myeval.admin')){loginPage();return;}
  busy=true;
  const hash=location.hash.slice(1),[route,id]=hash.split('/');
  $('breadcrumb').textContent=route==='catalog'?'工作区 / 任务包与能力':route==='guide'?'工作区 / 使用说明':route==='trial'?'实验 / Trial 详情':route==='experiment'?'实验 / 实验详情':'工作区 / 实验';
  document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active',a.id===('nav-'+(route==='catalog'?'catalog':route==='guide'?'guide':'experiments'))));
  try{if(route==='experiment'&&id)await experimentPage(id);else if(route==='trial'&&id)await trialPage(id);else if(route==='catalog')catalogPage();else if(route==='guide')guidePage();else await listPage();}
  catch(error){$('app').innerHTML=`<div class="notice error">${esc(error.message)}</div><button data-action="refresh">重试读取</button>`;}
  finally{busy=false;}
}
function loginPage(){
  $('app').innerHTML=pageTitle('LOCAL WORKSPACE','进入本机工作区','匿名评审不需要管理员访问码。')+`<div class="panel panel-pad guide"><form id="login-form"><h2>管理员访问</h2><p class="muted">访问码保存在本机数据目录的 admin.token。也可运行 scripts/open 直接打开管理员入口。</p><label>本机访问码<input type="password" name="access" autocomplete="off" required></label><p class="error-text" id="login-error" role="alert"></p><button class="primary" type="submit">进入工作区</button></form></div>`;
  $('login-form').addEventListener('submit',async event=>{event.preventDefault();sessionStorage.setItem('myeval.admin',new FormData(event.target).get('access').trim());try{catalog=(await api('/api/catalog')).taskpacks;await render();}catch(error){sessionStorage.removeItem('myeval.admin');$('login-error').textContent=error.message;}});
}

function selectedPack(){return catalog.find(p=>p.project===$('project-select').value);}
function updateForm(){
  const p=selectedPack();if(!p)return;
  $('pack-info').innerHTML=`<div class="source-note">${esc(p.name)} · v${esc(p.version)} · 所有材料均为明确标记的虚构样本</div>`;
  $('case-choices').innerHTML=p.cases.map(c=>`<label class="check-row"><input type="checkbox" name="case_ids" value="${esc(c.id)}" checked><span>${esc(c.name)}<small>${esc(c.task)} · v${esc(c.version)}</small></span></label>`).join('');
  $('baseline-select').innerHTML=p.configurations.map((c,i)=>option(c.id,c.name,i===0)).join('');
  $('candidate-select').innerHTML=p.configurations.map((c,i)=>option(c.id,c.name,i===1)).join('');
  updateConfig();
}
function updateConfig(){
  const p=selectedPack();if(!p)return;
  const configs=[$('baseline-select').value,$('candidate-select').value].map(id=>p.configurations.find(c=>c.id===id));
  $('config-info').innerHTML=configs.map(c=>`<div><strong>${esc(c.name)}</strong> · ${esc(label(c.mode))}<br><span class="mono">${esc(JSON.stringify(c.parameters))}</span></div>`).join('<hr class="divider">');
  const form=$('create-form');const count=form.querySelectorAll('[name=case_ids]:checked').length,repeats=Number(form.elements.repeats.value);
  $('trial-count').textContent=`${count} 个独立案例 · ${count*2*repeats} 个 trial`;
}
async function openCreate(){catalog=(await api('/api/catalog')).taskpacks;$('project-select').innerHTML=catalog.map(p=>option(p.project,label(p.project),p.project===projectFilter)).join('');$('create-error').textContent='';updateForm();$('create-dialog').showModal();}

document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-action]');
  if(!button){const row=event.target.closest('[data-route]');if(row&&!event.target.closest('a'))location.hash=row.dataset.route;return;}
  try{
    const action=button.dataset.action;
    if(action==='new')await openCreate();
    if(action==='refresh')await render();
    if(action==='export'){await download(exportLink(current.id),`myeval-${current.id}.zip`);toast('已导出当前场景证据；包含原失败与全部尝试');}
    if(action==='stop'){button.disabled=true;await api(`/api/experiments/${current.id}/stop`,{});toast('已停止后续调度，并请求在途任务取消');await render();}
    if(action==='retry'){button.disabled=true;await api(`/api/trials/${current.id}/retry`,{});toast('已建立新的执行尝试，原失败保留');await render();}
    if(action==='publish'){button.disabled=true;const p=await api(`/api/trials/${current.id}/publish`,{});toast('平台同步：'+label(p.status));await render();}
    if(action==='reviewer'){await api(`/api/experiments/${current.id}/reviewers`,{});toast('已建立独立评审席位');await render();}
  }catch(error){toast(error.message);button.disabled=false;}
});
document.addEventListener('change',event=>{if(event.target.id==='project-filter'){projectFilter=event.target.value;render();}if(event.target.id==='case-filter'){caseFilter=event.target.value;render();}if(event.target.id==='status-filter'){statusFilter=event.target.value;render();}});
$('project-select').addEventListener('change',updateForm);
$('create-form').addEventListener('change',updateConfig);
$('close-dialog').onclick=()=>$('create-dialog').close();
$('create-form').addEventListener('submit',async event=>{
  event.preventDefault();const form=event.currentTarget,submit=form.querySelector('[type=submit]');submit.disabled=true;$('create-error').textContent='';
  try{const data=new FormData(form),e=await api('/api/experiments',{title:data.get('title'),project:data.get('project'),case_ids:data.getAll('case_ids'),baseline_id:data.get('baseline_id'),candidate_id:data.get('candidate_id'),repeats:Number(data.get('repeats')),timeout_seconds:Number(data.get('timeout_seconds'))});$('create-dialog').close();caseFilter='';statusFilter='';location.hash='experiment/'+e.id;toast('实验已建立，正在执行真实业务请求');}
  catch(error){$('create-error').textContent=error.message;}
  finally{submit.disabled=false;}
});
window.addEventListener('hashchange',()=>{caseFilter='';statusFilter='';window.scrollTo(0,0);render();});
const startup=await Promise.allSettled([api('/api/catalog'),api('/api/health')]);
if(startup[0].status==='fulfilled')catalog=startup[0].value.taskpacks;
$('service-state').textContent=startup[1].status==='fulfilled'?'● 本地服务已连接':'服务未连接';
await render();
setInterval(()=>{const h=location.hash;if(!document.hidden&&!$('create-dialog').open&&(!h||(h.startsWith('#experiment/')&&current?.status==='running')||(h.startsWith('#trial/')&&!terminal.has(current?.status))))render();},2500);
