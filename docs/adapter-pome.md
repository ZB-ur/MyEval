# pome 真实文字适配器

`myeval/adapters/pome.py` 与 `taskpacks/pome.json` 接入 `/api/chat`、`/api/summary`、`/api/state`。任务包 `pome-text-foundation / 1.0.0` 包含两个独立虚构 episode：添水纠正与背景隔离、危险动作与主动结束。

适配器在每个 trial 的独立进程内启动产品原有 `backend.app.Handler` 和 `Store`，绑定 OS 分配的 `127.0.0.1` 端口。所有调用确实经过本机 HTTP；客户端显式禁用系统代理。产品提示词、背景筛选、二阶段审校、轮次控制、有限重试和校验没有复制到 MyEval。

## 执行与配置

1. 通过正式状态接口保存虚构档案、排班、历史记录及草稿，让产品生成背景快照。
2. 每一轮先保存儿童原话，再调用 `/api/chat`。候选经审校后，只有实际交付回复进入本 trial 的下一轮上下文。
3. 调用真实 `/api/summary`，再用状态接口将记录写入并同时移除草稿；重新 GET 并核对落盘。

首版只有 `isolated_full_run`。固定上下文重跑、旧轨迹重评分及音频标记为 `unsupported`。这里的完整执行指文字 HTTP 业务路径，没有执行儿童浏览器语音倒计时或自动保存。没有加载音频模型，暂停声音线保持未动。

两种配置都使用 `deepseek-flash`。基线的候选和审校请求 `thinking=enabled / reasoning_effort=low / max_tokens=4096`；候选配置只把 effort 改为 `high`。整理均为 `thinking=disabled / max_tokens=2048`。这两个 effort 是 [DeepSeek 官方接口支持的请求值](https://api-docs.deepseek.com/guides/thinking_mode/)，并不据此声明实际生效：结果单独保留供应商返回的 `model`，effective thinking/effort 保持 null。

## 共享产品改动

仅修改 pome 的 `backend/app.py`，新增 `backend/evaluation.py`：

- `Store(runtime_settings=...)` 支持仅内存的执行配置，普通入口没有传入，仍读取自己的 SQLite 设置。
- 服务器可选 `ai_options.chat_reasoning_effort`；未传时仍为 low，不接受浏览器任意覆盖。
- 可选 `ai_observer` 提供 input、model_request、model_response、stage_result、stage_error、delivered 事件及 episode/turn/stage/attempt/request 关联。普通产品入口不安装 sink，不新增原文日志。
- provider response 使用显式白名单，只保留可见 content/refusal、model、request ID、finish reason、数值用量。隐藏思考字段从未写入 sink。

产品代码当前未提交；可复核的差异副本在 `integrations/pome-observation.patch`。未修改 `docs/development-retrospective.md` 或声音相关代码。各项目整合时需同时保留这两个共享接口文件。

## 凭据与隔离

`MYEVAL_POME_ROOT` 可指定产品源码目录。`MYEVAL_POME_CREDENTIAL_DB` 可明确指定已有授权的本机配置数据库。未指定时先找产品默认目录；本机默认目录不存在，因此使用已有隔离开发实例 `.acceptance/dialogue-optimization/runtime/diary.sqlite3`。

仅以 SQLite `mode=ro` 提取 `providerBaseUrl/apiKey/chatModel` 三个字段，密钥只在执行进程内存中。不会复制整个 settings、生产数据库或历史材料，不把凭据写进命令行、UI、轨迹或 trial 数据库。首版只接受 DeepSeek 官方 HTTPS 主机。通过 source state body 的 SHA256 前后比较记录源资料未被写入，正文不解码、不导出。

本地取消可停止当前等待和后续步骤；产品没有供应商取消协议。已送达的请求是否终止远端计算/计费未知，不能据本地 `cancelled` 推断。每个产品 chat 最多三次调用（两阶段共享一次重试），summary 最多两次，另受公共 runner 的 trial 截止约束。

## 数据接口

`review_output` 只有 `transcript`（实际 child/assistant text）和 `summary`，没有配置名、模型、ID、耗时或路径。管理员 `output` 保留 stage_results、实际调用/重试次数、状态快照摘要和源数据完整性。候选不会进入后续 child/assistant transcript。

`usage` 累加每次真实响应的 `prompt_tokens/completion_tokens/total_tokens`，包含失败响应和重试；记录 requested/response/with_usage 三个调用计数。存在无响应调用或缺失字段时，相应总量为 null。单次响应的缓存 token 数与可用 reasoning token 计数留在事件中，不保留思考正文。费用未绑定价目版本，`cost=null`。

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
