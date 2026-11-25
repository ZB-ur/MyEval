# pome 真实文字适配器

`myeval/adapters/pome.py` 与版本化任务包接入 `/api/chat`、`/api/summary`、`/api/state`。当前适配器 `pome-http-9`，支持原任务包 `pome-text-foundation / 1.3.0`、`pome-strategy-redesign / 1.0.0`、`pome-core-rebuild / 1.0.0` 及后文的朋友回应、新旧 A/C 与 V2 任务包。原有四个独立虚构 episode：添水纠正与背景隔离、危险动作与主动结束、不确定原因不补编、否定结束与第三方引语。原 1.0.0 的两个案例与配置保持原内容，已冻结实验不改写。

本仓库提供适配器，不包含 pome 产品、账号或模型凭据。默认源码路径为 `~/AiCoding/pomegranagent-v2`，也可设置 `MYEVAL_POME_ROOT`。旧 `integrations/` 补丁不是完整 V2 产品安装包；新用户应先运行独立演示，再按自己的产品版本接入。

2026-11-13 用户授权把新 C `reference-companion-v5 / 5.0.1` 接为普通产品默认，并在应用边界补充有限的明确结束控制。MyEval 仍按冻结配置显式指定 profile；适配器未指定 profile 的兼容缺省仍是 `baseline`，不会随产品默认自动改变评测含义。旧结果保留，源码变化后旧冻结须由来源漂移检查阻止继续执行。以下各日期的“默认未切换”描述当时状态；当前选择及新增控制的独立验收见[落地记录](handoff/2026-11-13-pome-new-c.md)。

适配器在每个 trial 的独立进程内启动产品原有 `backend.app.Handler` 和 `Store`，绑定 OS 分配的 `127.0.0.1` 端口。所有调用确实经过本机 HTTP；客户端显式禁用系统代理。产品提示词、背景筛选、二阶段审校、轮次控制、有限重试和校验没有复制到 MyEval。

## 执行与配置

1. 通过正式状态接口保存虚构档案、排班、历史记录及草稿，让产品生成背景快照。
2. 每一轮先保存儿童原话，再调用 `/api/chat`。按所选 profile 执行真实产品阶段，只有实际交付回复进入本 trial 的下一轮上下文。
3. 调用真实 `/api/summary`，再用状态接口将记录写入并同时移除草稿；重新 GET 并核对落盘。

首版只有 `isolated_full_run`。固定上下文重跑、旧轨迹重评分及音频标记为 `unsupported`。这里的完整执行指文字 HTTP 业务路径，没有执行儿童浏览器语音倒计时或自动保存。没有加载音频模型，暂停声音线保持未动。

原四种配置都使用 `deepseek-flash`：

| 配置 ID | 候选与审校 | 整理 |
| --- | --- | --- |
| `pome-flash-low`（原产品基线） | enabled / low | disabled，不发送 effort |
| `pome-flash-high`（原强度候选） | enabled / high | disabled，不发送 effort |
| `pome-flash-thinking-on`（全链路开启） | enabled / low | enabled / low |
| `pome-flash-thinking-off`（全链路关闭） | disabled，不发送 effort | disabled，不发送 effort |

所有配置的候选/审校各次请求上限为 4096 token，整理为 2048。原四种配置使用相同基线提示词，状态初始化、共享时间与重试预算相同。开启时显式发送 reasoning_effort，关闭时从实际 payload 省略该字段。适配器拒绝为 disabled 配置同时指定非空 effort。供应商字段是否出现和 reasoning token 数只作为非正文观察，不能据此声明参数全部生效；供应商 `model` 单列，effective thinking/effort 仍为 null。[官方思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)与 [Chat Completions 参数](https://api-docs.deepseek.com/api/create-chat-completion/)于 2026-11-09 核对。

## 共享产品改动

修改 pome 的 `backend/app.py`，新增 `backend/evaluation.py` 和 `backend/agent_profiles.py`：

- `Store(runtime_settings=...)` 支持仅内存的执行配置，普通入口没有传入，仍读取自己的 SQLite 设置。
- 服务器可选 `ai_options.chat_thinking / summary_thinking`（enabled/disabled）和对应 `*_reasoning_effort`（low/high/max）。普通入口仍是接话/审校 enabled+low、整理 disabled；不接受儿童浏览器任意覆盖，不写入产品设置。
- 可选 `ai_observer` 提供 input、model_request、model_response、stage_result、stage_error、delivered 事件及 episode/turn/stage/attempt/request 关联。普通产品入口不安装 sink，不新增原文日志。
- provider response 使用显式白名单，只保留可见 content/refusal、model、request ID、finish reason、数值用量；额外记录 `reasoning_field_present` / `reasoning_content_nonempty` 两个布尔值。隐藏思考正文从不写入 sink、日志或后续上下文。

产品代码当前未提交；可复核的差异副本在 `integrations/pome-observation.patch`。未修改 `docs/development-retrospective.md` 或声音相关代码。各项目整合时需同时保留上述共享接口文件。

## 凭据与隔离

`MYEVAL_POME_ROOT` 可指定产品源码目录。`MYEVAL_POME_CREDENTIAL_DB` 可明确指定已有授权的本机配置数据库。未指定时先找产品默认目录；本机默认目录不存在，因此使用已有隔离开发实例 `.acceptance/dialogue-optimization/runtime/diary.sqlite3`。

仅以 SQLite `mode=ro` 提取 `providerBaseUrl/apiKey/chatModel` 三个字段，密钥只在执行进程内存中。不会复制整个 settings、生产数据库或历史材料，不把凭据写进命令行、UI、轨迹或 trial 数据库。首版只接受 DeepSeek 官方 HTTPS 主机。通过 source state body 的 SHA256 前后比较记录源资料未被写入，正文不解码、不导出。

本地取消可停止当前等待和后续步骤；产品没有供应商取消协议。已送达的请求是否终止远端计算/计费未知，不能据本地 `cancelled` 推断。每个产品 chat 最多三次调用（两阶段共享一次重试），summary 最多两次，另受公共 runner 的 trial 截止约束。

## 数据接口

`review_output` 包含 `transcript`（实际 child/assistant text）、`summary` 与 `context_by_turn`（该侧每轮实际注入的允许历史/动物资料），没有配置名、模型、耗时或路径。历史记录 ID 仅用于核对来源。管理员 `output` 保留 stage_results、实际调用/重试次数、状态快照摘要和源数据完整性。未交付候选不会进入后续 child/assistant transcript。

`usage` 累加每次真实响应的 `prompt_tokens/completion_tokens/total_tokens/reasoning_tokens`，包含失败响应和重试；记录 requested/response/with_usage 三个调用计数。存在无响应调用或缺失字段时，相应总量为 null。单次响应的缓存 token 数与可用 reasoning token 计数留在事件中，不保留思考正文。费用未绑定价目版本，`cost=null`。

规则由适配器返回 `kind=rule`、evaluator/version、dimension、`status=pass/fail`、value 与 reason。范围包括背景选择、原话落盘、记录唯一性与草稿移除、实际回复上下文衔接、候选/审校可观察性、特定背景片段的字面筛查。规则不判断所有语义错误，不作为独立模型评分。人工盲评由公共 UI 另行保存。

## 2026-11-09 实际证据

无新增单元测试、测试目标或断言脚本。通过 `myeval.worker` 执行有限真实请求并手工查阅事件、结果及 SQLite；Python 语法和产品 `git diff --check` 通过。

| 本地 trial ID | 场景 / 配置 | 执行终态 | 模型调用 | 耗时 |
| --- | --- | --- | --- | --- |
| pome-adapter-28dc1d32f317 | 纠正 / low | succeeded | 7 | 17.792 s |
| pome-adapter-d701509e209b | 纠正 / high | succeeded | 8，含一次候选重试 | 19.738 s |
| pome-adapter-3baa7392f8dd | 安全与结束 / low | succeeded | 5 | 11.001 s |
| pome-adapter-d90519d0df51 | 安全与结束 / high | succeeded | 5 | 16.159 s |
| pome-final-30909b6b4495 | 最终代码 / 安全与结束 / low | succeeded | 6，含一次有限重试 | 15.255 s |
| pome-cancel-2567818b1531 | 候选请求发出后停止 | cancelled | 发起 1 次，未收到响应 | 停止请求至进程退出约 530 ms |

首四次结果在 `.local/adapter-pome/manifest.json`；每个 trial 下包含 `request.json`、`result.json`、公共 `events.jsonl`、`pome.events.jsonl`、状态快照和 `pome-data/diary.sqlite3`。额外证据为 `trace-audit.json`、`final-source-run.json`、`cancellation.json`。首四次规则使用初稿状态词 passed/failed，最终代码统一 pass/fail；原始运行记录未改写。

实际复核：两个配置的第二轮确实包含各自第一轮交付的 final，候选不入上下文；每 episode 只新增一条记录并移除草稿，原话一致；隔离 SQLite settings 均为空对象。读取的配置源 state SHA256 前后一致。首四次共 25 个模型调用、25,710 个供应商 total token，全部用量保留；成本未知。轨迹结构没有 `reasoning_content`、`apiKey` 或 `Authorization` 字段。high 纠正的第 2 轮候选第一次被产品拒绝后按共享额度重试，随后审校和整理成功，所有 attempt 均存在。

**执行成功不等于质量通过。** 手工内容复核发现，纠正场景两配置的整理均出现给水主体的否定与肯定冲突；high 的审校后回复仍扩写原话没有表达的位置/结果。结构规则通过没有发现这些语义问题。原始结果完整保留，未修改产品 prompt 或挑选重跑以掩盖结果；这两项应进入人工语义复核，不能据本轮小样本宣称配置优劣。此处是开发验收观察，不是用户盲评标签，也没有独立模型评分。

主脑已完成公共 UI 启动、盲评、重复采样身份、导出和重启恢复，见 [首版验收](acceptance/first-release.md)。真实儿童语音/设备、Windows、儿童端连续浏览器旅程未覆盖。

## 思考开关准备与独立 canary（2026-11-09）

为后续 UI 比较增加上述两个全链路配置及两例情境，正式计划为四案例 × 两配置 × 两次重复 = 16 trial。实现任务没有启动该矩阵；其 UI 操作、输出比较和截图由母会话独立完成。

仅执行一个短接口 canary `pome-thinking-toggle-canary-20261109`：一轮虚构原话，特意使用接话 disabled、整理 enabled+low，以一次隔离 episode 覆盖两个新增参数分支。实际三次调用成功，candidate/review 均记录 `thinking.type=disabled` 且不含 reasoning_effort，summary 记录 enabled+low；token 上限分别为 4096/4096/2048。前两次响应没有 reasoning 字段，整理响应记录该字段存在/非空及数值 reasoning_tokens；隐藏正文没有落盘，effective 仍为 unknown。该混合配置只作接口贯通证据，不在任务包中、不进入正式比较分母。

证据为 `.local/adapter-pome/thinking-toggle-canary/observation.json`，配置/来源复核在 `.local/coordination/thinking-toggle/`。编译、源码差异检查与集成补丁反向核对通过；原两个案例/配置、三份产品提示词和用户文档 hash 未变，源资料 hash 前后一致，隔离 settings 为空。未新增单元测试、未修改普通产品设置，也未恢复声音工作。MyEval 必须重启以加载新版适配器，正式 UI 创建前以 `thinking-toggle-ready.json` 中的实际服务状态为准；历史实验仍使用冻结快照，不能对旧实验直接重试以偷换源码。


## 教师反馈优化候选（2026-11-09）

新增六个虚构反馈情境，以及 `pome-response-grounded` / `pome-response-contextual`。两者保持原产品 Flash low 接话/审校、关闭思考整理；只比较候选与审校提示词策略。

- `grounded-v1`：抓住本轮新意思，通常一到两句；认可有原话依据的本次关心/细心，避免稳定人格判断和空泛夸奖。
- `contextual-v1`：在上述原则上，仅在相关且有帮助时自然联系允许历史；只使用明确对应的动物名字/类型，不补习性、心理或当前状态。
- 未指定 profile 时仍 `baseline`；原三份基线提示词哈希不变。可在产品启动时用 `--dialogue-profile grounded-v1` 或 `contextual-v1` 选择本次服务策略，不写入资料。普通启动的默认值未更改。

适配器保存 profile ID、版本、两段模板 SHA，逐次实际模型请求保存拼接主题/背景/轮次后的 system SHA。新文件纳入逐文件来源 SHA。候选与审校同步选择，整理、后台背景筛选、轮次、共享重试和隔离边界沿用产品实现。

本轮冻结量表、48 trial 主比较及因交付失败追加的24 trial 同模式对照、运行、外部匿名 AI 评阅与实际 UI 证据位于 `.local/acceptance/pome-feedback-20261109/`。真人评价独立记录，不用产品内置审校替代。结果见本轮验收报告。

补充配置 `pome-response-contextual-off` 仍使用同一 contextual-v1 模板，只关闭接话/审校思考；整理原已关闭。其基线为 `pome-flash-thinking-off`，案例不变。独立记录为第三个实验，不替换前两组失败。


## 三套结构方案（2026-11-11）

新任务包保留六个旧反馈案例，增加六组最小对照共十二个变式：总计18案例、12情境家族。与原版均使用Flash并请求关闭思考，候选/审校4096、整理2048 token上限。比较的是整套Agent方案，不能把结构传递的效果归因于提示词措辞。

- `dialogue-act-v2` 先输出有限交流动作标签，再生成与审校回复。
- `evidence-ledger-v2` 输出带儿童原话ID和逐字引文的事实表，审校重新核对；日记从绑定本故事、实际交付及模板版本的已审校事实表和儿童原话生成。服务只验证结构、来源、引文和覆盖，不能自动证明事实语义正确。缓存仅内存、6小时有效，重启、过期或绑定不一致将拒绝整理，无静默回退。
- `selective-memory-v2` 按明确交流用途选择最多一段历史，审校能放弃但不能另取历史。新注入历史栏只含所选片段；完整当前对话仍保留，先前回复提过的历史可能继续存在。

正常入口仍默认baseline，旧三份提示词字符串不变；没有增加儿童轮次或正常模型调用次数。六维评分明确分开聊天与日记忠实度，适用、无判断机会与运行不可用分别计数，见[冻结量表](../rubrics/pome-redesign-v2.json)。正式运行与结论见后续验收记录，不能从接口打通推断内容质量。

## 按核心要求重建（2026-11-11）

用户明确撤销逐轮鼓励、通常一两句、聊天只能忠实复述、每轮全面审校的旧约束。新增三套可选 v3，均使用全新核心提示词；允许合适一般知识、建议、开放理解和儿童开启的想象，日记仍只整理实际儿童原话。默认 baseline 未切换。

| Profile | 正常接话阶段 / 调用数 | 可观察契约 |
| --- | --- | --- |
| `direct-companion-v3` | candidate / 1 | 直接回应；三个开发者原创虚构示范，未经教师认证 |
| `perspective-then-speak-v3` | perspective → speaker / 2 | 有限重点、逐字儿童证据、最多一段相关历史、交流目的；不是隐藏思维链 |
| `selective-repair-v3` | candidate → check / 2 | 无明确问题则 exact accept；有问题只允许最多三个不重叠的精确片段修补 |

每次接话仍共用 75 秒与一次失败重试。格式失败将应用产生的错误码/短反馈传入下一请求，既有基线也使用这一失败反馈；旧正常请求模板不变。`dialogue-act-v2` 的历史改为真实对话资料容器，修复旧三字段 assistant 示例与新 schema 冲突。旧 216 次结果没有重跑替换。

三套 v3 共用 `backend/dialogue_v3.py`：先建立同儿童、截止时间前的允许文字池，再按当前/既往儿童文本的加权中文词项选择最多三条；不先截最近三条。记录选中 ID、分值、候选池哈希和缓存状态。词项匹配不保证语义相关。候选池只在首次 v3 请求时冻结于内存，最多 16 池/200 万字符；重启或驱逐会重建，不是持久化草稿快照。

三套共用 `child-only-json-v3` 整理提示词和 JSON 格式，实际整理输入只有 `child_sources`，不送助手文本或外部背景。聊天采用 system 控制与 user 资料容器，避免把历史助手输出伪装成格式示范。仅使用 Chat Completions 的 JSON mode，本地结构校验不等于供应商严格 schema 或语义证明。

元数据冻结各阶段模板、整理模板、响应 schema SHA 与预期阶段；规则按配置检查已交付轮次的阶段，不再强制所有方案有候选和审校。观测增加单调时钟的模型请求耗时、HTTP 完成耗时、选择性修补决定；不记录隐藏思考正文。MyEval 从实际事件汇总性能，包含失败用量；TTFT、语音首句和设备渲染不在本轮观测范围。

正式任务包 18 例（6 回归、12 新材料）、16 情境家族，每配置 2 次；三组旧基线/候选加一组 direct off/low 思考对照，共 288 次。主比较使用当前产品默认的 chat enabled/low，整理 disabled。质量按[冻结 v3 量表](../rubrics/pome-rebuild-v3.json)独立评阅，性能不进入盲评材料。正式运行中不修改冻结源码或评分规范，失败保留，不用补跑替换。

## 朋友回应候选（2026-11-12）

适配器 `pome-http-6` 新增 `warm-direct-v4`、`responsive-friend-v4`、`case-based-friend-v4`，实际实现位于共享产品的 `backend/friend_profiles.py`，当前版本均为 4.0.1。正常每轮仍一次模型请求，整理共用儿童来源专用协议。新候选聊天只传一次 transcript 与允许背景；C 在资料前增加最多两条本地线索检索的原创虚构示范，冻结示范来源、策略与哈希。没有复制提示词到 MyEval，也没有增加独立审校调用。

旧 A 与默认 baseline 的提示词、元数据、输入形态保持原样。新增模块纳入逐文件来源冻结；同组原 A 也在相同源码快照运行。主持主题沿用教师设置，动物身份独立；叫声逻辑不变。质量量表新增情绪共鸣与明确具体的鼓励，N/A 与未知分别展示，不按每轮夸奖配额评价。144 次正式执行、288 份独立评分、性能和实际 UI/停启/导出结果见[朋友回应验收](acceptance/pome-friend.md)；默认未切换。

## 2026-11-12 新旧 A/C 迭代

适配器 `pome-http-7` 注册 `warm-participant-v5` 与 `reference-companion-v5`（5.0.1），实现位于共享产品的 `backend/friend_profiles_v5.py`，纳入来源冻结。旧 A/C 指上节的 `warm-direct-v4` / `case-based-friend-v4`，其提示词和元数据保持原样。新 A 用两段固定示范强化自然参与，新 C 每轮提供八段精简虚构多轮参考，由一次生成自行借鉴，移除关键词预筛选。只能观测提供了哪些示范，不能声称观察到模型实际选择或借鉴哪段。

新候选允许适切拟人化和情感延伸，保留明确纠正、现实动作、安全与独立儿童原话整理边界。调用链、历史选择、教师主持来源与声音逻辑沿用当前实现。四组比较统一使用八维 v5 量表，旧量表分数不直接相减；协议与实际结果见[新旧 A/C 迭代](acceptance/pome-ac-iteration.md)。产品默认未切换。


## V2 候选与动态用户接入（2026-11-18）

本轮当前产品基线是 `reference-companion-v5 / 5.0.1`，普通默认保持不变。共享产品新增 `backend/eval_profiles_v6.py`，只在 `agent_profiles.py` 注册两个可显式选择的候选：

| Profile | 版本 | 改动与边界 |
| --- | --- | --- |
| `need-aware-companion-v6` | 6.0.0 | 保留 v5 八段完整参考，补充纠正、拒绝、结束和前轮已经完成的交流作用；不要求统一短句。 |
| `compact-reference-v6` | 6.0.1 | 压缩核心指令，把同八段全部示范发言紧凑序列化到系统模板；不删示范、不做关键词预筛选。开发 6.0.0 出现过多追问后，恢复被弱化的“不把关心变成查问任务”含义。 |

两个候选继续使用单次真实接话、`relevant-same-child-v3` 历史选择、`child-only-json-v3` 独立整理和既有结束控制。提示词只在产品共享模块；MyEval 不复制提示词。app.py、dialogue_v3.py、v5 原文、当前 Qwen 声音和用户未跟踪文档均保持原 hash。

适配器为 `pome-http-9`，逐文件来源包括新增候选模块及实际依赖的 interaction driver/provider/workspace。`describe` 在轻量独立进程解析产品 profile，按根目录、逐文件 hash 与 profile 缓存；避免长驻服务导入缓存造成“新源码 hash + 旧模板元数据”。读取期间发现源文件变化会拒绝描述。此过程不创建产品 Store、加载语音模型或读取模型凭据。

开发集 `pome-v2-development` 有 8 个原创虚构家族、3 个配置、每配置一次采样。首轮 24 episode 及 compact 修订的 8 episode 全部真实执行成功，无内部重试，136 项边界规则 pass；规则不证明内容质量。最终 compact 与基线在同家族配对的 prompt token 变化中位数为 -32.98%，total token 为 -26.69%；端到端时延中位数分别 11.57 秒和 9.23 秒，没有提速。need-aware 的结果混合，不预定为更好。

独立匿名 AI 对最终三配置 24 份交付按八维复核，保留并列偏好和有争议的严重问题；没有教师金标，也没有无争议 major/critical。该小样本仅是开发证据，不是留出泛化结论。旧 compact 的退化结果保留，候选作者未读取留出材料。产品候选冻结在 `.local/coordination/20261118/pome-candidates-frozen.json`；完整适配器/依赖最终交接以 `pome-ready.json` 为准。

### 可选用户交互

固定 `input.child_turns` 仍是默认。任务可改用显式 `input.interaction_policy`（契约见 `myeval/interactions/interactions-contract.md`），两者不能同时提供；策略最大轮数不得超过产品本次轮数。

- `script` 明确标为固定脚本，`rules` 只按最新真实产品回复选分支，不能称为 LLM 用户。
- `model` 使用 `CodexSimulatorProvider` 请求 gpt-6-sol/low，无工具、隔离临时目录；仅得到 goal、visible_facts、可见历史及生成限制，不得到产品配置、评分规范或未来脚本。请求参数与未知生效值分开。
- 每轮仍调用同一真实产品 Handler，只有实际交付进入下一轮上下文。产品结束时不再调用模拟器生成多余用户发言；最后真实助手回复在 `terminal_assistant_delivery` 单列，完整实际轨迹仍保留在 `output.transcript`。
- driver identity、用量、provider receipts 和停止原因仅在管理员 output/events；不放入匿名质量材料。产品 `usage` 不混入模拟用户消耗。达到最大轮数只是执行停止，不证明目标成功。模型/预算错误为失败，取消/截止分别报告取消与超时。
- 明确声明产品 JSON/JSONL 状态和每次模拟 provider 的九种安全证据文件用于导出，不递归导出数据库或任意目录。

真实 rules 验收分别触发有问号与无问号分支，各两轮加整理，共六次产品模型调用，保存/原话/上下文规则通过。model canary 首次因 10,000 token 限制在第二次模拟响应后停止，保留失败与已交付一轮；提高该独立开发 canary 上限到 20,000 后，两次模拟调用产生两轮实际产品交互和独立日记，模拟器使用 10,707 token，产品三次调用使用 5,384 token。两者未混算。这是单案例接口证据，不证明模拟儿童的真实性或普遍有效。默认固定脚本另做一条三轮加整理回归成功。

原始开发与交互证据在 `.local/v2/pome-dev/`，不提交 Git。pome 已显式接入公共交互组件，不能再将它列为“尚需接入”。组件自身的模型 canary 使用固定虚构 assistant 夹具；下列 pome 交互验收则由同一真实产品 Handler 交付回复，两者分开报告。

| 已验收路径 | 本机证据 | 可支持的结论 |
| --- | --- | --- |
| 规则用户 → 实际 pome 回复 → 依回复选分支 → 整理与持久化 | `.local/v2/pome-dev/interaction-rules-01/observation.json`，逐例 `rules-1/`、`rules-2/` | 实际触发 question-observed 和 no-question-observed；是规则用户，不是模型用户或真人反馈。 |
| 模型用户预算停止 → 保留已交付一轮 | `.local/v2/pome-dev/interaction-model-01/result.json` | 观测累计 token 越界被映射为失败，原始部分轨迹与两次 provider 回执保留。 |
| 模型用户 → 实际 pome 两轮 → 独立整理与持久化 | `.local/v2/pome-dev/interaction-model-02/result.json`、`events.jsonl`、`user-simulator/<run_id>/live/` | 已走通模型模拟用户与真实产品交替调用；请求只含可见事实和已发生对话，模拟器与产品用量分开。 |
| 默认固定脚本三轮 → 整理与持久化 | `.local/v2/pome-dev/fixed-regression-01/result.json` | 可选交互接入后的固定脚本回归成功，不混入动态开发分母。 |
| 产品浏览器的音频输入 → ASR → chat → TTS → summary → 保存后重新载入 | `.local/coordination/20261118/pome-product-ui-acceptance.json`；隔离资料 `.local/v2/pome-dev/ui-runtime/` | 主脑实际操作的独立产品旅程；最终读回一条记录、零草稿，不由 HTTP canary 代替。 |

产品浏览器旅程使用 `compact-reference-v6 / 6.0.1`。主脑报告已执行真实 ASR、接话、播音、整理和重新载入；上述验收记录保留最终原文与日记、音频 hash 及已知边界。输入是 macOS Tingting 的成人合成语音，没有使用物理麦克风或真实儿童声音；已记录“小栗 → 小丽”的识别差异，人工听感为 `not_reviewed`。因此该旅程证明此次链路执行和保存读回，不证明识别准确率、真实儿童适用性或声音质量。

主脑通过忽略目录的 `.local/v2/pome-dev/serve_product_ui.py` 启动 8791 隔离实例，凭据只在内存；正式产品也可用现有 `--dialogue-profile` 选择候选。本分工没有操作浏览器，产品旅程由主脑验收；正式留出质量与平台整合结果另行报告，不从以上开发或接口成功推出。候选、适配器、交互实现和量表继续保持冻结。
