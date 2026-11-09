# pome 真实文字适配器

`myeval/adapters/pome.py` 与 `taskpacks/pome.json` 接入 `/api/chat`、`/api/summary`、`/api/state`。当前适配器 `pome-http-3`、任务包 `pome-text-foundation / 1.3.0`。原有四个独立虚构 episode：添水纠正与背景隔离、危险动作与主动结束、不确定原因不补编、否定结束与第三方引语。原 1.0.0 的两个案例与配置保持原内容，已冻结实验不改写。

适配器在每个 trial 的独立进程内启动产品原有 `backend.app.Handler` 和 `Store`，绑定 OS 分配的 `127.0.0.1` 端口。所有调用确实经过本机 HTTP；客户端显式禁用系统代理。产品提示词、背景筛选、二阶段审校、轮次控制、有限重试和校验没有复制到 MyEval。

## 执行与配置

1. 通过正式状态接口保存虚构档案、排班、历史记录及草稿，让产品生成背景快照。
2. 每一轮先保存儿童原话，再调用 `/api/chat`。候选经审校后，只有实际交付回复进入本 trial 的下一轮上下文。
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

`review_output` 只有 `transcript`（实际 child/assistant text）和 `summary`，没有配置名、模型、ID、耗时或路径。管理员 `output` 保留 stage_results、实际调用/重试次数、状态快照摘要和源数据完整性。候选不会进入后续 child/assistant transcript。

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
