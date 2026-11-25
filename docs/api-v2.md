# MyEval V2 HTTP 与 CLI

本文描述当前 V2 核心实现；先用[图文操作手册](walkthrough.md)或[可运行的 CLI 示例](../examples/README.md)了解完整流程，验收边界见[发布记录](acceptance/2026-11-25-release.md)。服务默认只监听本机，管理员请求使用 `X-MyEval-Admin`。凭据从所选 data-dir/admin.token 在请求边界读取，不放 URL、日志或导出。普通请求 JSON 对象上限 50 MB，历史媒体导入上限 512 MB，失败返回 `{error,code?}`；过期评价 409。

首次启动原子创建仅当前用户可读写的管理员令牌。已有 admin.token 必须为可安全读取的普通文件，内容为 32–128 位 ASCII 字母、数字、下划线或连字符（允许文件末尾换行）；空白、损坏或链接文件阻止启动，且不会自动覆盖原文件。校验在数据库初始化和任务恢复之前完成；管理员请求缺少、留空或提供无效 header 均返回 403，即使运行时服务端令牌异常也不会放行。

## 实验与队列

既有 `/api/experiments`、`/api/trials/:id`、`/api/blind/...`、`/api/export/:id?format=zip` 保持兼容。

`GET /api/catalog` 返回 `{taskpacks,datasets,capabilities,limits}`。`GET /api/experiments?project=...` 返回轻量 `{experiments}`：状态、数量、家族、配置 ID、任务包、队列与预算、评价数量；不遍历轨迹或计算性能。完整比较在详情。列表 `comparison.scope=list_summary`。

`POST /api/experiments` 示例：

```json
{"title":"候选对照","project":"sijian","taskpack_id":"sijian-v1","case_ids":["case-id"],"baseline_id":"baseline-id","configuration_ids":["baseline-id","candidate-a","candidate-b"],"repeats":2,"timeout_seconds":900,"start_paused":true,"budget":{"max_trials":500,"max_attempts":1500,"max_concurrency":2,"max_elapsed_seconds":86400}}
```

任务包/案例/配置 ID 以 catalog 为准；旧 `baseline_id + candidate_id` 仍可用。2–16 个不同配置，基线放在冻结 configurations 首位；每候选分别与基线建立匿名配对，随机左右及顺序，重复不增加家族数。来源冻结同时保存注册适配器声明的源码文件；`source_snapshot` 明确 captured/partial/unavailable，未重建依赖或 checkout 产品树。worker 仍在产品请求前核验当前来源。

可替换 taskpack_id 为 `dataset_id + dataset_version_id`，指定 `split=development|holdout|all`。case_ids 可省略以选该划分全部案例；明确选择时不能越过划分。返回详情新增 baseline_id、configuration_ids、candidate_ids、dataset、split、budget、budget_usage、queue、source_snapshot、origin。

- `POST /api/experiments/:id/pause {}`：停止新调度，在途任务继续；`/resume {}` 恢复暂停。
- `/stop {}` 保留原行为：未发送项 cancelled，在途请求取消；已停止必须显式 `/api/trials/:id/retry`，形成新 attempt。
- `GET /api/queue` 返回 `{paused,concurrency,limits,counts,active,experiments,dispatch,limitations}`。
- `POST /api/queue {paused?,concurrency?,limits?}` 持久化设置。并发 1–32；当前默认 2。默认单实验 5000、队列 20000、重复 20、单 trial 86400 秒、每 trial 3 次 attempt；可显式调整有界上限。
- 实验 budget 支持 max_trials、max_attempts、max_elapsed_seconds、max_known_cost、max_total_tokens、max_concurrency。计划数超限不创建；观测值达限将未调度项取消并记录 budget_exhausted。在途请求与缺失费用不能提供绝对计费上限；unknown 不记零。本机调度不是分布式执行。
- 服务重启保留尚未被领取的 queued 项、原 attempt 及实验/全局暂停设置。两层暂停均解除后才继续调度；这不是重发已执行请求。只有已领取的 running/cancelling 项恢复为 interrupted 或已落盘终态，未确认的执行不会自动重放；明确重试仍保留旧 attempt。

## 数据集与历史回流

- `GET /api/datasets` → `{datasets}`。
- `POST /api/datasets/import` → 完整版本。body `{id,name,project,version,taskpack_id?,configuration_ids?,configurations?,cases,rubric?,source?,authorization?,notes?,used_by?}`。配置只接受已注册定义，不能注入执行模块。
- `GET /api/datasets/:id` → 数据集与版本摘要；`GET /api/datasets/:id/versions/:version_id[/export]` → 完整不可变版本；version_id 可为内部 ID 或版本标签。
- source 支持 legacy `synthetic:true` 或 `type/kind: synthetic|licensed_public|authorized_private`。公开媒体保留 license、url、attribution、SHA-256。私有资料需要 `authorization:{granted_by,granted_at,evidence,scope:{project,case_ids或source_ids,operations}}`；import 权限只准导入，执行需要 evaluate/evaluation。声明不等于系统独立核实法律授权。
- family_id 同一版本不能跨开发/留出；家族从开发不能重新标新留出。留出转开发必须在 used_by 披露暴露。`dataset.holdout_independence` 与 exposed_holdout_families 继续进入实验/导出。
- `POST /api/imports/traces`：`{bundle:<MyEval导出>,source:{系统/位置说明},authorization?}` 或 `{project,title,source,authorization?,traces:[{id?,case,configuration,status,result,repeat_index?,attempts?,events?}]}`。仅导入终态；每条尝试和原 ID/hash 进入 origin。新本地 ID 避免覆盖，失败/拒绝/unsupported 不过滤。导入历史不启动产品请求，不能点击重试重放；可选择历史 attempt 重评。模型评价必须绑定其原始输出 hash；隐藏推理与凭据字段拒绝导入。
- 导入同时将原 `evaluation`（全部 jobs/units，包括失败、无效及无响应）和 `decisions` 原值保存为 `historical_evidence`，不依赖是否存在 revalidation receipt。普通 JSON/ZIP 再导出包含完整归档及 SHA-256，`binding_status=original_provenance_only`、`current_result=false`、`registered_for_current_experiment=false`；原 job/trial/decision 身份保持不变，不进入新作业队列或当前判定统计。再次导入继续保存前一代归档；来源未提供、结构或单位分母不完整、归档丢失或哈希不符均显式报告，未知不能视为零。case_id 不裁剪这些原实验来源档案。较早版本已丢失的原评测不能自动补回，会明确 unavailable。
- `GET /api/imports` → `{imports}`。

## 独立评分与比较

评分组件由 `myeval.evaluation` 提供，尚未安装时明确 unavailable。完整提供者与量表格式见 [evaluation-v2.md](evaluation-v2.md)。

- `GET /api/evaluators` → `{evaluators,available}`。
- `GET /api/rubrics` → `{schema_version,registry,available,rubrics,unavailable,custom_rubric_supported}`。管理员读取固定注册目录，不接受任何路径或筛选参数。每项返回 `id`（创建评分作业时传给 `rubric_id` 的注册文件 ID）、`declared_id`（量表内声明 ID）、`version`、`name`（未声明时回退声明 ID）、`scope/project`（未声明为 `unknown`）、`rubric_hash`、`dimension_count` 及 `dimensions:[{id,name,description,min,max,anchor_values,anchors,definition}]`。anchors 保留原数字键及档位原文，definition 为完整归一化维度对象；无档位原文时 anchors 为空，不猜含义。只有完整 rubric_hash 与历史评价一致时才用于解释其分值。摘要复用评分组件的规范校验，hash 与冻结评分量表一致；不根据文件名前缀猜测项目。符号链接和无效量表只记入 unavailable 的通用原因，不返回任意文件路径。现有自定义 `rubric` 对象入口保持可用。
- `POST /api/evaluation-jobs` → 作业详情。核心转交 `{experiment_id,evaluator_id,mode,rubric_id/rubric,targets?,split?,title?,timeout_seconds?,budget?,calibration?}`。
- `GET /api/evaluation-jobs?experiment_id=...` → `{jobs}`；`GET /api/evaluation-jobs/:id` 详情；`POST .../:id/cancel {}`；`GET .../:id/calibration` 校准报告。
- `GET /api/evaluation-revalidations?experiment_id=...&original_job_id=...` → `{receipts,invalid}`，只读展示完整重验收分母；`/api/revalidations` 是同一接口别名。每份 receipt 保留原作业 counts，另外列出 valid/invalid/no_response/ineligible 四项（含零值）、unit_count、生成的 assessment_count、原 job 链接、原 evaluator/rubric 与新 engine/validator 身份。`assessment_count_kind=planned_not_import_confirmation` 明确登记不等于评分导入已确认；增量 usage 为零，原用量不重复计入。不同 receipts 不自动汇总为新评审者或新样本。
- `GET /api/evaluation-revalidations/:id` 返回摘要及全部单位状态、原身份和校验问题；`/:id/export` 下载包含 manifest.json 与原样 receipt.json 的 ZIP。需要管理员权限，无任意路径参数、无 HTTP 写入入口。完整性失败进入列表 invalid，不作为已验证记录展示，也不静默消失。原 job 不被修改。
- `GET /api/review-queue?experiment_id=...` → `{items}`。
- `GET /api/experiments/:id/analysis` 返回 family 统计分析。模型、人工、验收试填保持独立。
- 旧 `POST /api/trials/:id/assessments` 增加 execution_attempt 与 result_hash；历史重评必须提供匹配 hash；旧评价保留但不评分新结果。

## 判定、批次与恢复

- `POST /api/experiments/:id/decisions {policy:{...}}` 返回不可变 `status=pass|fail|inconclusive`，检查理由与证据身份；`GET` 同路径 → `{decisions}`。缺少质量证据不能 pass。后续重试/评价改变时 `evidence_current=false`。
- policy 可指定 scope=quality|execution、candidate_ids、min_families、min_completion_rate、min_success_rate、require_holdout、rules、model_criteria、min_human_review_pairs。model_criteria 明确 dimension/evaluator/rubric_version 与 min_mean/min_delta、min_coverage、min_paired_families。描述性差值不是显著性承诺。
- `GET/POST /api/campaigns`；`GET /api/campaigns/:id`；`POST /api/campaigns/:id/experiments`。批次可链接既有实验或建立新实验（默认暂停），保留部分失败和显式 campaign_key 幂等身份，不合并跨项目总分。
- `GET/POST /api/backups` → `{backups}` / 新备份 manifest。等待在途与评分结束；SQLite backup + 白名单证据文件，排除管理员令牌和业务运行库，逐文件 SHA-256。
- `POST /api/backups/:id/restore {destination:"独立空目录绝对路径"}`：先校验后恢复，拒绝覆盖、符号链接和损坏；不启动服务。恢复后独立启动生成新管理员令牌。

## 图片与操作参考

`GET /api/trials/:id` 的 `media` 返回 `{index,sha256,mime_type,source,url}` 数组。以管理员 header fetch 该 url 然后创建 blob URL 展示；`GET /api/trials/:id/media/:index` 仅读取案例已声明的图片、在注册项目或评测材料目录内，验证 SHA-256、实际格式和 30 MB 限制。绝不接受请求传入本机路径。公开来源、作者与许可随媒体保留。

普通 JSON/ZIP 的 `artifact_manifest` 保留已声明但缺失、不可安全读取或哈希漂移的 attempt 附件，返回 `availability=unavailable`、`omitted`、`error.code/message`、原 `trial_id/attempt/declared_path` 及已声明的 `declared_sha256`；哈希漂移另保留 `observed_sha256`。不可用条目没有 text/content_base64，也不会伪造 ZIP 文件。正常可读附件仍携带实际字节与 SHA-256；声明为空的轨迹目录明确 unavailable，未声明的可选文件不推定为缺失。

历史导入的原路径声明保留在 result 中，文本归档可能存于 `origin-artifacts/`。只有 `imported_artifacts` 明确匹配原 source trial、attempt 和完整 source_path，且归档实际字节通过声明 SHA-256 时，导出才将原声明标为 `availability=relocated`、`kind=archived_alias`，通过 `archive_path` 引用同一 ZIP 内真实归档文件；别名不重复携带字节，也不改 result 或其哈希。同名文件、相同 basename、跨 attempt 引用都不能替代映射。归档缺失、漂移或声明冲突仍显式 unavailable/拒绝；缺失别名的声明 SHA 在后续导入导出中持续保留。再次导入仅将同 attempt 已提供且哈希一致的归档登记为别名映射。轨迹目录按精确来源子文件映射显示 `archived_directory_alias`，范围仅为 `declared_children_only`；任一已声明子文件不可用时目录仍为 unavailable，不猜测未声明文件的完整性。

目录父项的 `declared_children` 独立持久化，后续导出采用已声明集合与实际映射集合的并集。即使子文件字节和子别名条目同时丢失，也不能缩小父集合或声称目录完整；缺项为 `declared_archive_child_missing`，父目录为 `archived_directory_incomplete`。此时同名新文件没有原始归档身份，不能替代已知缺项。

Playbook 放在专用 data-dir/playbook。管理员 `GET /api/playbook` 返回 `{available,files:[{path,bytes,mime_type,url}],manifest,read_token_expires_at,access_scope}`；manifest 来自同目录 manifest.json。files 的 url 带 4 小时临时只读 token，可直接用 `<video>` / `<img>`，支持 HTTP Range 拖动；它只能读取该目录白名单文件，不授予管理员或其他证据权限。无目录时 available=false。只允许真实视频与关键帧，录屏覆盖范围由 manifest 如实声明。

## CLI

`scripts/eval --help` 可查看命令。默认 URL 8310，可用 `--url http://127.0.0.1:8321 --data-dir .local/v2/core-acceptance`。命令包括 health、catalog、list/start/wait/export、评分与判定、备份恢复；start 从 JSON 文件读取完整实验规格。凭据不打印。wait 超时只结束等待，不取消实验。判定退出码 pass=0、fail=1、inconclusive=2、操作失败=3。

产品执行与独立评分使用不同的并发限制。服务启动参数 `--concurrency` 控制产品 trial；`--evaluation-concurrency` 只控制评分 worker，接受 1–4，默认 2。评分并发在启动时确定，调整需等待在途任务结束后重启。批次大小、评分单位预算和并发各自独立。

```sh
# 当前产品运行结束后，由服务负责人使用原数据目录重启；保留必要的授权凭据/媒体环境变量。
scripts/run.sh --port 8320 --data-dir .local/v2/data --concurrency 5 --evaluation-concurrency 4
# 也可使用 python3 -m myeval，参数完全相同。
scripts/eval --data-dir .local/v2/data health
```

`GET /api/health` 与 `health` 命令返回实际产品 `concurrency`、评分 `evaluation_concurrency` 和请求值 `evaluation_concurrency_requested`；评分组件未就绪时实际值为 null。启动时同样写入 data-dir/server.json。现有 `scripts/service start` 生命周期封装不透传自定义评分参数，会采用默认 2；需要 4 时使用上述 `scripts/run.sh`/`python -m myeval` 启动命令，勿在实例仍运行时重复启动。`scripts/eval queue --concurrency ...` 仍只调整产品队列。

评分持久化还支持管理员 `POST /api/assessments/batch {items:[{trial_id,body}]}`。整批先验证再单事务提交。body 的 `evaluation_job_id + idempotency_key` 保证中断后重送同一已接受评分不会重复，键复用于其他内容或结果会拒绝。该接口保存已提供评价，不调用评分模型；来源与实际模型调用需分别记录。

`GET /api/calibrations` 返回 `{calibrations}` 内部规范锚点目录。`POST /api/evaluation-jobs/:job/units/:unit/adjudications` 保存复核意见 `{kind:"human|acceptance",disposition:"accepted|rejected|needs_followup",reason,reviewer?}`；不改原评分、不把无效评分自动变成有效，不将功能试填当真人评价。

完整重验收 receipt 先由其原版本验证器执行 `verify`，再用 `python3 -m myeval.receipts register --data-dir <data-dir> --receipt <receipt.json>` 显式登记。登记只检查结构、来源/内容哈希与完整单位分母，不重新解释语义，不调用模型、不写 assessment。使用固定 `evaluation/revalidations/<rv1-64hex>/manifest.json + receipt.json` 原子发布；已存在只接受相同字节。此最终存储契约替代早期建议的单个扁平 JSON 文件，不能直接把扁平文件复制后视为登记完成。HTTP 读取拒绝符号链接、超限文件和不符 ID，保留历史 engine 的原判断，显示 `semantic_validation=not_recomputed`。原确认文件继续独立保留，不能把导入状态写回已哈希 receipt。现有备份的 evaluation/ 白名单包含这些文件；恢复不会执行重验收或评分。

普通 `GET /api/export/:experiment_id` JSON/ZIP 同时包含 `revalidations` 索引与完整 manifest/receipt 附件，路径为 `revalidations/<receipt_id>/manifest.json|receipt.json`。scope 始终为 whole_experiment；case_id 只过滤执行轨迹，不能裁掉原作业中的 invalid/no_response/ineligible。每个文件保留原字节 SHA-256，登记不等于导入确认。材料损坏时在索引 invalid 或 availability=unavailable 明示，不以有效 assessment 代替完整分母；归属无法核实的损坏记录也列出，并说明分母未知。

历史导入将这些附件保存在 import 的来源归档中，`binding_status=original_provenance_only`，不改 receipt 内部 job/trial/result 身份、不把 receipt 注册为新实验评测、不调用模型。随后普通导出使用 revalidation-history/ 附件前缀保留原字节；缺少附件、SHA 不同与已有 unavailable 状态持续保留。既有历史 assessment 的输出身份映射与 receipt 原来源归档分别报告。直接调用 Store.export 取得索引，HTTP 导出装配完整附件并标记 attachments_included。

完整导出包含 attempt 内 PNG/JPEG/WebP/GIF 字节与逐图 SHA-256。JSON 使用 content_base64 和重复媒体的 content_ref_sha256；ZIP 放置原始图片文件。导入先验证字节格式、hash、attempt 媒体清单及路径，再保存；读取优先使用冻结 attempt 快照。备份恢复保留这些图片，外部原图移走后仍可读。图片最大30MiB，累计导入附件500MB。源外部路径未归档的历史记录会保持缺失，不能假称原图已存在。
