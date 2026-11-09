# 公共层独立复核

2026-11-09。复核范围：`myeval/store.py`、`runner.py`、`server.py`、`worker.py`、`context.py`、`catalog.py`、`util.py` 与 Sijian 适配器。复核者是本会话内部的独立子任务，不是用户人工验收。公共层修复由主脑完成；本任务只改 Sijian 本机 HTTP 客户端的代理设置及本报告。

复现通过独立 `.local/core-review/` 数据目录、系统分配的 loopback 端口、真实 MyEval HTTP 服务、真实 worker 和真实 Sijian 服务进行。没有新增单元测试或断言脚本；故障注入仅作用于本任务记录的隔离 worker。临时服务均已停止。

## 已发现并复验修复的问题

| 问题与初次位置 | 实际复现 | 修复后的观察 |
| --- | --- | --- |
| 未评价即能从管理员 API 还原左右配置。初次 `server.py:86` 至 `111` 没有角色边界；`store.py:334` 完整导出包含 A/B trial 映射。 | 不带认证头或 Cookie，匿名 blind 显示 completed=0；`/api/experiments` 与 `/api/export/<id>` 都为 200，可由映射直接得到两侧 configuration_id。没有读取源码或本机凭据。 | 管理员 catalog、列表、trial、完整导出不带 capability 均为 403；匿名 blind 和匿名导出仍为 200；进程内读取本机 token 后管理员导出为 200。token 未打印。 |
| Worker leader 异常退出后，产品进程仍在运行。初次 `runner.py:115` 至 `116` 直接移除 active，未回收仍存在的自有进程组。 | 仅向记录的 worker PID 发送 SIGKILL 后，trial 已 failed/worker_exit，但 worker 进程组仍存在，独立思间服务 `/health` 仍 ready。初次探针只确认服务存活，不能据 dispatch 事件单独声称供应商已收到模型请求。 | 修复后的实际请求确认 CLI 已启动；再次仅杀 worker 后，产生 descendant_cleanup，思间 run `23b48134-f814-408b-a043-30686d03d92f` 写 cancelled、SIGTERM 回收记录。CLI 组与 worker 组均消失，产品 health 不再可达。 |
| 冻结配置未限制排队或重试后的源码漂移。初次 `worker.py:18` 直接执行当前适配器；catalog 保存的 provenance 未用于执行检查。 | 源码路径明确：配置保存旧 hash，执行仍 import 当前源码。 | 将早先真实成功 run 保存的旧 provenance 交给实际 worker，返回 failed/source_changed_since_freeze；事件只有 worker.started、source.verified、worker.finished，无产品请求事件，usage/cost 均为 null。 |

复核证据：

- 修复前匿名接口：`.local/core-review/blind-api-observation.json`，实验 `de0e1c7e-07c9-4539-9390-80d30e216e36`。两个 trial 在调度前停止，未发生模型请求。
- 修复前进程存活：`.local/core-review/worker-exit/observation.json`，实验 `f2a1989b-ee31-422a-a17b-b954edecc9ab`。观察后显式清理该已知进程组。
- 修复后接口与进程：`.local/core-review/fixes/http-and-process-observation.json`，实验 `53b1bc25-1d7c-4b4a-8343-af4eb6091c31`。
- 来源漂移阻断：`.local/core-review/fixes/source-drift/observation.json` 及同目录 request/result/events。旧 provenance 来自 `.local/adapter-sijian/discover-low-01/result.json`，不是伪造模型响应。

以上复验确认 HTTP/执行边界，不替代主脑的浏览器连续操作，也不声称对拥有本机文件权限的管理员形成安全隔离。

## 补充修复与最终复核

1. **旧尝试的观测平台回执写入身份已修复。** 原路径在平台 I/O 后仅凭 trial ID 更新当前行，存在 N 的回执落到 N+1 的竞争窗口。现 `Runner.publish(identifier, attempt)` 选择对应历史尝试，`Store.update_trial(expected_attempt=...)` 将迟到回执写回历史行，runner 错误兜底也传递 attempt。代码复核通过。实际把前述真实失败 trial `06a01975-3eda-4157-a112-1b5619f902e2` 从 attempt 1 重试为 queued attempt 2，然后调用历史 attempt 1 的发布入口；隔离环境的真实返回为 unconfigured，只写到历史行，current attempt 2 的 platform 仍为 null。没有伪造网络成功或导入虚构模型评分。证据：`.local/core-review/fixes/historical-publication-observation.json`。此项验证了存储身份保护，不声称重放了真实慢网络竞争。
2. **外部模型评价身份字段已修复。** `Store.add_assessment` 现在要求终态，导入记录绑定 `execution_attempt` 与该真实 result 的 `result_hash`。代码复核通过，重试后仍能指向原输出。本任务没有实际外部评分器产物，因此未制造模型评分填入接口；外部模型评价导入的完整产品旅程仍未实测。

## 已落盘真实结果的崩溃恢复

在独立端口 61134、独立 `.local/core-review/recovery-final/data` 启动实际 MyEval 服务。真实思间请求进入后，仅 SIGSTOP MyEval 服务，worker 继续调用真实模型；等 worker 成功写入 result.json，再 SIGKILL 服务并用同一数据目录重启。这样实际复现“结果已落盘而 SQLite 仍 running”的窗口，没有模拟输出或修改数据库状态。

- 实验：`58a06990-f3ad-467b-b568-4762325b0b3d`；trial：`5a48a4e8-0fbe-4082-873d-c0fbda6d9825`。
- 真实思间 run：`d9dd5a0d-8524-4dcf-9ae3-5943b11938e8`，原 worker 结果 succeeded，实际用量 input 5876 / output 54，费用 null。
- 重启前数据库状态 running；重启后 succeeded，并记录 `recovery.phase=recovered_terminal_artifact`、`replayed=false`。
- 恢复后的 result 对象与原文件相同，文件 SHA 未变；产品请求快照数为 1 → 1，内容 hash 未变。另一未派发的 queued trial 变 interrupted，没有自动请求或自动重试。
- 完整观察：`.local/core-review/recovery-final/observation.json`；实际请求/结果/事件在同目录 data 内。该临时服务已停止，未操作主脑的 8310 服务。

`Runner.recover` 也已改为检查记录 pgid 中命令与该 trial 工作区相符的进程，避免仅看 leader；此分支完成代码复核。前述 worker 异常退出回收有实际证据，但本轮没有额外声称已覆盖“主服务崩溃且 leader 已消失、子组仍运行”的全部组合。

## 其他已核对边界

- 盲评返回采用白名单投影；`review_output` 不含配置、模型、trial/run ID、来源路径或用量；显式 reveal 在保存该配对评价后才开放。
- 案例和参数快照入库，重复编号独立；retry 新增 attempt，不直接覆盖旧 attempt 表行；已有配对评价会阻止重试改变被评价的输出。
- `TERMINAL` 保留 succeeded、failed、timed_out、cancelled、interrupted、refused、unsupported；未知 usage/cost 使用 null。比较接口中的 `known_cost_total` 是已知费用小计，UI 必须同时显示未知数量，不能把全未知显示成总费用零。
- Sijian 本机 HTTP 现用 `ProxyHandler({})`；在显式错误的 HTTP/HTTPS 代理与空 no_proxy 环境下，真实本机 health 仍为 200。不改变产品访问模型服务的外网代理行为。
- 不记录隐藏思维文本；Sijian 原生过滤轨迹保留可观察事件，适配器进一步跳过 reasoning item。真实产品的 prompt/schema 快照保留在本地 trial 目录；主脑后续已实际从 UI 导出并核对这些原生附件，见 [首版验收](acceptance/first-release.md)。

本报告不宣告首版完成；UI、完整 A/B 矩阵、重启恢复、人工试填与导出独立可读性仍由主脑整合验收。

## 2026-11-09 晚到停止意图增量验收

本项只验证 `Runner.execute` 的 `late_cancel_intent` 分支。使用 `.local/core-review/late-cancel/` 独立数据和端口 63952、64008，不操作主服务 8310，不重跑已通过的 A/B 矩阵，不修改公共源码。每项只产生一次真实 Sijian 模型请求；同实验未派发的另一配置被正常取消。

为了可控地安排 OS 信号，验收将未修改的生产 `Handler`/`Store` HTTP 控制进程，与未修改的生产 `Runner` 调度进程分开运行。SIGSTOP 只暂停调度进程，真实 worker 和产品桥接继续执行；控制进程仍能通过真实 HTTP `/api/experiments/<id>/stop` 调用原 `Store.stop`。HTTP 写入停止意图后才 SIGCONT 调度进程。此为隔离的调度信号注入，不能冒充标准合并 CLI 拓扑或浏览器 UI 旅程验收。没有 mock 产品响应或填入模拟终态。

| 情形 | 实际身份 | 时序与公开结果 |
| --- | --- | --- |
| 已有真实 terminal artifact | 实验 `ac1c2882-7197-4d36-81eb-44fb363f4a25`；trial `b0d3e647-b628-4e58-89f8-1d4984ce646a`；Sijian run `6241cb95-5c74-4ae7-8fdf-dcc7f26f75f2` | 北京时间 13:34:40.063 worker 写入 succeeded；观察到 worker 为 Z 状态后，13:34:40.123 才收到真实 HTTP 停止请求，随后恢复调度。最终仍为 succeeded，结果对象与原 result.json SHA 均保持，cancellation 为 user_cancel / completed_before_cancel_confirmation；公开事件 `execution.cancel_intent_after_terminal` 的 product_terminal_available=true。 |
| worker 已退出但没有 result.json | 实验 `b8d42537-9565-4576-a723-424c90eae912`；trial `408670fc-b9f8-4e57-b256-013a7ab925c7`；Sijian run `9f19fe23-bb19-4c3c-a767-92d701a23a08` | 真实产品请求已经启动 CLI 后，仅对本项记录的 worker PID 发送 SIGKILL，确认 Z 状态且无结果文件；13:34:42.205 才经 HTTP 请求停止，然后恢复调度。最终为 interrupted / worker_interrupted，cancellation 为 user_cancel / worker_exited_before_cancel_observation；公开事件的 product_terminal_available=false。没有误报为普通 failed，也没有虚构已交付结果。 |

两项的请求快照数均为 1 → 1，hash 未变，没有重复模型请求或自动重试；结束时 worker 进程组及产品 CLI PID 均不存在。无产物项的真实产品桥接另有 cancelled、SIGTERM 回收、group_present_after_reap=false 的原生轨迹；公共层保守保留 interrupted，因为 worker 没有交付自身终态文件。该项的 usage/cost 为 null，远端计算及计费是否停止仍为 unknown。`cancellation.sigkill_sent=false` 表示 runner 未为取消发出 SIGKILL；外部故障注入和后续 descendant cleanup 都在独立时序/事件中注明。

证据入口为 `.local/core-review/late-cancel/summary.json`，每项子目录均保存 `operator-timeline.jsonl`、`observation.json`、公开 API 返回的 `public-trial.json` 及原生产品轨迹。第一项进行期间主脑更新了无产物 phase 命名和其他公共层内容，起止文件 SHA 已分别保留；第一项验证已载入的有产物分支，第二项新启动调度进程并实际观察到修正后的无产物 phase。两项临时服务均已停止。

必要的最小建议：本次覆盖“worker 已退出，而停止标记在 runner 最终检查前到达”。源码仍有“最终标记读取之后、trial 终态写入之前”这一更晚的窗口；若 HTTP 停止在该窗口写入 requested，后续 `cancellation=None` 仍可能覆盖它。该更晚窗口仅作源码提示，未扩大本轮实测。彻底保留意图应在最终 Store 事务锁内合并已经记录的停止状态，并让公开终态事件使用实际提交后的 cancellation；实现权留给主脑。

### 后续锁边界修复的只读复核

主脑随后将 `runner.py:157` 至 `183` 的最终标记读取、cancellation/result 构造、`execution.terminal` 和 `Store.update_trial` 全部置于 `with self.store.lock`。`Store.lock` 为可重入的 `threading.RLock`；`Store.stop` 在同一锁内更新状态并写停止标记；标准 `server.main` 将同一个 Store 实例同时交给 Runner 和 Handler。`publish` 仍在锁外，不因平台网络 I/O 扩大这一临界区。

只读复核结论：上述更晚的 **标准同进程 HTTP stop 与终态提交窗口已经闭合**。停止先取得锁时，终态路径必能看到标记；终态路径先取得锁时，HTTP stop 之后读取到的已经是终态，不会再改该 trial。嵌套调用 `update_trial` 使用同一 RLock，不引入自锁死锁。

这部分结论来自代码复核，记录和当前文件 SHA 在 `.local/core-review/late-cancel/locking-code-review.json`。先前两次实际 late-cancel 结果继续作为分支行为证据；拆分进程的信号注入拓扑不共享线程锁，不能用于证明本次锁竞争结论。未增加模型请求、未重跑矩阵，也不外推跨进程直接改文件/SQLite 或其他取消路径。
