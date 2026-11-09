# 首版内部适配契约 v1

公共层使用 Python 标准库、SQLite、后台隔离 worker 与本机 HTTP UI。适配器只依赖标准库；产品自己的运行环境沿用已安装环境。产品调用是真实共享入口，不复制提示词。

- 文件 `myeval/adapters/<project>.py` 暴露 `describe(configuration)` 和 `run(case, configuration, ctx)`。
- `describe` 不运行模型，返回能力、来源文件哈希、代码 HEAD/dirty 哈希、请求配置、模式定义与限制，不含凭据。可按实现返回任意 JSON 字段。
- `run` 同步返回 JSON dict：`status`（succeeded/failed/refused/unsupported/cancelled/timed_out）、`output`、`observed_configuration`、`usage`、`cost`（未知用 null）、`assessments`（规则数组）、`limitations`、`error`。有失败也保留输出与阶段事实。不得记录隐藏思维链。
- `ctx.root: Path` 为 trial 独立工作区；`ctx.trial_id`、`ctx.experiment_id`、`ctx.timeout_seconds`、`ctx.cancel_requested`（bool property）。`ctx.emit(kind, data, **links)` 向事件文件立即追加时间戳事件，links 可含 episode/turn/stage/attempt。`ctx.check_cancelled()` 可抛 `CancelledError`。
- 每个 adapter 在独立 worker 进程运行；产品子进程不使用 `start_new_session=True`，便于 runner 在超时/取消宽限期后回收整组进程。适配器应在 finally 关闭自己启动的服务。
- 不连接产品默认端口。可由 OS 分配空闲本机端口；所有产品数据/轨迹留在 ctx.root。使用已有凭据只读取必要字段并在执行进程内注入，不复制生产 settings 或数据库。
- 首版配置模式：`isolated_full_run` / `fixed_context_rerun`；不支持的模式必须返回 unsupported。`trace_rescore` 暂 unsupported。
- 任务包 `taskpacks/<project>.json`：顶层 `id`, `project`, `name`, `version`, `description`, `rubric`（维度数组）, `cases`, `configurations`。case 至少有 `id`, `version`, `name`, `task`, `input`, `source`（synthetic、说明、as_of）, `expected_behavior`，可含 `initial_state`、`tags`。config 至少有 `id`, `name`, `version`, `mode`, `parameters`。
- 冻结样本用清晰标识的虚构内容。每项目至少 2 个有边界差异的用例及 2 个明确配置；避免用故意坏配置代替正式 A/B。模型有效值未知不可照抄请求值。
- 项目规则留在各自 adapter / taskpack。通用核心不硬编码业务判分。

主脑负责公共层、API、UI、整体验收；适配器作者自行跑真实小样本并报告证据，但不写 docs/acceptance/first-release.md，避免共享写冲突。
