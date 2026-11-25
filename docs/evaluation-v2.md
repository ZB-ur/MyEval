# 独立评分与历史重评

`myeval.evaluation.EvaluationService` 在产品执行之外维护独立 SQLite 作业队列。它读取具体 trial 的终态 attempt 和 `result.review_output`，冻结输出哈希、匿名材料、量表、评分器来源及媒体字节。重评不会调用产品，也不会覆盖旧评分。不存在安全交付投影时保留 `unavailable`，不把完整运行包交给模型。

## 使用与集成

管理员可通过 `GET /api/evaluators` 选择评分器，再向 `POST /api/evaluation-jobs` 提交：

```json
{
  "experiment_id": "已完成实验的 ID",
  "evaluator_id": "codex-astra-v1",
  "mode": "pairwise",
  "rubric_id": "general-v2",
  "split": "development",
  "timeout_seconds": 180,
  "budget": {"max_units": 200, "max_elapsed_seconds": 3600, "max_total_tokens": 200000}
}
```

`individual` 对每个交付独立评分；`pairwise` 按相同案例和重复序号，将每个候选分别与冻结基线比较。A/B 顺序每个作业随机产生，之后持久保存。模型只看到任务所需材料、可见交付、量表和明确附上的图片。管理员导出包含映射，不是匿名分享包。

可用 `targets:[{trial_id,execution_attempt,result_hash}]` 精确选定历史尝试。未显式选择时使用实验当前尝试；不可混入其他实验或运行中结果。自定义量表可用 `rubric` 对象，需稳定 `id`、`dimensions` 和清晰分值锚点。`rubric_id` 仅允许仓库 `rubrics/` 内文件，不能执行任意模块。

`GET /api/evaluation-jobs?experiment_id=...` 返回轻量列表，单个作业详情包含单位、失败、用量、校验与证据；`POST /api/evaluation-jobs/:id/cancel` 停止排队并取消本地进程。已完成评分保留。取消远端计算和计费是否成功保持未知。

独立本机命令适合没有服务占用的隔离数据目录：

```sh
python3 -m myeval.evaluation --data-dir .local/my-review calibrations
python3 -m myeval.evaluation --data-dir .local/my-review calibrate
python3 -m myeval.evaluation --data-dir .local/my-review create request.json --wait
python3 -m myeval.evaluation --data-dir .local/my-review job JOB_ID
python3 -m myeval.evaluation --data-dir .local/my-review analysis EXPERIMENT_ID
```

单个目录仅允许一个评分调度器持有文件锁。没有 `--wait` 的 create 只持久排队，需已有服务调度；查询不会恢复或重发在途工作。当前文件锁和进程组实现面向 macOS/Linux，Windows 未覆盖。

## 实时结构证据 v2

2026-11-18：`codex-astra-v2`、`codex-sol-v2` 已在独立源码与数据副本的 8340 服务完成验收，实测的 service 改动已合入主源码。主服务统一重启、原库一致性与最终 UI 验收仍由主脑完成，当前不据源码合入宣称主服务已启用。新任务在新版 UI 或 API 中应明确选择 v2；API 默认仍为 `codex-astra-v1`，v1 与历史作业入口保留，旧快照和评分身份不覆盖。

v2 在原逐字文本与实际媒体证据之外，接受 pointer 处完整、类型一致的 JSON 字面量，例如 `[]`、`8`、`null`、`false`。空引文、部分对象片段、missing/null 或 false/0 混同仍被拒绝。`typed_evidence`、转换记录和冻结 profile 单独保存，原模型判断不改写。实时 v2 标记为 `model_judgment` 并保留真实用量；离线重验证标记为 `deterministic_revalidation`，只引用原调用身份，本操作新增模型用量为零。新旧模型与验证版本分别统计。

实际验收共 **4 次模型调用、44,013 tokens、0 次产品重放**：两位评分器分别完成空数组配对与带图整数配对，全部成功并写入 8 条双侧评分；旧 510 条记录及 7 个作业的 321 个单位保持不变。验收源码与原运行源码、数据、端口独立；新增 helper 的来源漂移能在模型请求前阻止创建或派发。请求模型为 `gpt-6-astra` / `gpt-6-sol`、请求 effort 为 `medium`，effective 模型版本与 effort 仍为 `unknown`。这些结果证明功能执行链路，产品质量与人工一致率另行评估。原始证据位于 `.local/v2/evaluation-live-v2/`，交付和合入哈希见 `.local/coordination/20261118/evaluation-live-v2-ready.json`、`evaluation-live-v2-merge-ready.json`。

## 评分身份、证据与失败

模型记录绑定 `execution_attempt` 和 `result_hash`。独立的 evaluator、rubric、payload 与实现来源哈希进入记录；请求的模型、reasoning effort 与未知生效值分开。评分代码在排队期间变化会阻止后续模型请求，需新建作业。

每个维度明确为 `rated`、`not_applicable` 或 `unavailable`，后两者分值必须 null。分数范围、返回结构、逐字引文与 JSON Pointer 都由服务校验；未通过的原评分保留在作业，标记 invalid，不进入有效质量统计。逐字核验只能确认引用存在，不能证明模型对语义、图片或严重程度的判断正确。major/critical、争议和低信心进入复核队列。

`review_queue()` 返回待复核记录。`adjudicate(job_id,unit_id,{kind,disposition,reason,reviewer?})` 分开保存 human 或 acceptance 意见，不覆盖模型记录，也不会把无效评分自动变成有效。模型复核不能冒充真实人工评价。

失败、超时、取消、中断、评分不可用和预算停止均保留。产品任一配对侧没有成功时不调用评分模型，标记 unavailable；这种失败仍在执行分母中。调度器重启将此前 running 单位标记 interrupted，不自动重发；未派发单位继续排队。明确新建作业才会重评。

## 无工具执行与媒体

评分器使用独立的 Codex CLI 调用，关闭项目文档、用户配置、规则、shell、浏览器、插件、MCP、记忆、子智能体、hook 和技能；使用临时只读工作区与 ephemeral 模式。模型输出和案例文字均视为不可信数据。出现工具事件立即拒绝结果并终止该进程组。只保存公开最终 JSON、允许的生命周期事件、用量和来源哈希，原始 reasoning、stderr 与原始 JSONL 不落盘。凭据只由 CLI 在调用边界读取，不进入提示词或证据。

图片使用 `--image` 实际附上，保留原始字节 SHA-256 和中立附件 ID。支持最多 4 张 PNG/JPEG/GIF/WebP，每张最多 20 MB。`case.input.media` 或 `case.media` 格式为 `{path,sha256,source?}` 数组；单图兼容 `input.image_path/image_sha256`。相对路径使用冻结配置的 `provenance.source.project_root`。每图声明单独校验，缺少 source 时明确继承案例来源；来源类型冲突拒绝，混合公开/私有/合成材料目前需要拆分案例。私有材料须具体授权，公开图片须许可、来源与署名；只验证声明和字节，不冒充法律授权审查。模型不能自行下载图片或访问原始路径。

## 校准与统计

`list_calibrations()` 提供 `general-v2-internal-anchors-v1`：两条 AI 作者编写的合成规范锚点，特意区分“表达清楚”和“事实正确”。`create_job({calibration_id:"general-v2-internal-anchors-v1",evaluator_id:"codex-astra-v1"})` 可真实运行；`calibration_report(job_id)` 显示逐项一致、分歧与缺失。锚点不是真人金标，一致率不代表评分器业务有效性。

`summarize(store.export(id))` 是纯读取分析。每个候选、原始 split、评分器、量表和维度分别统计；相同 case 的重复先求均值，同家族变式再求均值，最后家族等权。bootstrap 以家族为抽样单位，固定哈希种子、2000 次重采样；少于两个有效家族不输出区间。顺序分值均值仅作描述，不宣称显著性或真实用户效果。

多候选配对评分必须使用同一单位的双边记录，不能把基线对候选 B 的评分拿来比较候选 A。重评选择每个 comparator/evaluator/rubric 的最新完整配对；individual 选择每个输出身份的最新评分。旧尝试、重复记录、失败和缺失仍在分母及历史中显示。AI 偏好、人工偏好与验收试填分开，分歧保留，没有合成总分。

## 运行验收记录

本轮组件与服务真实调用的记录位于忽略目录 `.local/v2/evaluation-acceptance/`；交付状态详见 `.local/coordination/20261118/evaluation-delivery.md`。HTTP、浏览器、三项目正式留出集和最终操作录像由主脑整合验收，不能由模块编译或两次模型调用替代。

执行参数核对依据：[Codex 非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)、[配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。

本轮增强的持久边界：发送前保存 launch intent。若进程在模型返回前中断，请求是否送达与用量保留 unknown，不记为零。已通过校验的评分先进入 committing，再由核心 `add_assessments` 原子幂等写入整组；重启可以仅恢复提交，不重新调用模型。取消和接纳在同一锁下决出先后。默认并发 2、上限 4，每个作业还有独立 1–4 上限；已观测 token 限额可能被在途请求超出，因此 UI 和导出需显示实际并发与未知量。

历史媒体重评优先使用所选 attempt 的 `result.media`（兼容 `review_output.media`）不可变快照，路径必须在该 attempt 内，逐图重新核对授权、来源和 SHA-256。原始外部图片路径只有没有快照的旧数据才会使用；不会因为旧图缺失就悄悄换用新图。配对两侧的媒体哈希和顺序必须一致。备份通过 `backup_lock` 与评分采用同一 evaluation→store 锁顺序，避免同时提交与备份相互等待。

模型有时会给出 `/output/text` 而非约定的 `/text`。服务仅对可唯一解析且逐字引文不变的包装层路径做规范化；每次规范化都记录前后值，原始 provider 输出仍保留。无法定位的引文仍使评分 invalid。保存的 evaluator 和 rubric 身份包含内容哈希后缀，避免复用同一展示名称的不同评分器实现混入同一统计组。
