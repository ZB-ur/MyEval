# 多轮交互与本地状态

2026-11-18。公共组件位于 `myeval/interactions/`；完整接口约定见 [interactions-contract.md](../myeval/interactions/interactions-contract.md)。本模块不调用产品、不读取凭据，也不是容器沙箱。

## 已实现

- `TurnDriver` 提供固定脚本、依实际回复分支的规则用户、显式 callback 注入的模型模拟用户。三类来源分别标记，不能计作真人评价。
- 策略包含目标、可见事实、禁止知识类别、版本与哈希、最大轮数、时间、模型调用数、输出和已观测 token 限额。模型请求仅携带白名单字段及已发生的公开对话；模型模式拒绝未来脚本、分支配置与未知字段。
- 每次用户生成、实际 Agent 交付、分支选择、模型请求/公开响应和停止原因写入现有 `RunContext` 事件，并关联 episode/turn。附加供应商字段和隐藏思考不写入事件。
- 模型回调须遵守 `ProviderControl` 的取消、截止时间和输出上限。主调用有界等待；未知用量会禁止下一次请求。超时/取消后的远端执行及计费保持未知，观测 token 限额不是精确金额上限。
- `EpisodeWorkspace` 管理隔离目录的真实文件。读写逐级使用目录描述符与 `O_NOFOLLOW`；拒绝绝对路径、`..`、符号链接和特殊文件；不接管已有非空未归属目录。
- 快照保存真实字节与 SHA-256；reset 先校验快照、保留旧 live 目录，再切换状态并读回。存在本地重置恢复日志。状态可在同一 workspace 的多个 episode 中延续。
- 最终产物校验支持存在、SHA-256、UTF-8 原文、JSON Pointer 值；返回逐项 pass/fail/unsupported、字节与哈希。这些是客观文件规则，不替代产品语义评分。

## 适配器接法

```python
from myeval.interactions import TurnDriver, EpisodeWorkspace, CodexSimulatorProvider

# policy 由适配器从已授权的可见场景构造；不可传入完整 case、量表或未来台词。
simulator_provider = CodexSimulatorProvider(ctx.root / "simulator-provider")
driver = TurnDriver(policy, ctx, episode_id, provider=simulator_provider)
user = driver.next_user()
while user["text"] is not None:
    delivered = real_product_call(user["text"])
    user = driver.next_user(delivered)

with EpisodeWorkspace(ctx.root / "episode-state") as state:
    state.write("artifact.json", actual_product_artifact_bytes)
    receipt = state.snapshot("after_delivery")
    evidence = state.verify(expected_file_facts)
```

`simulator_provider(request, control)` 由执行边界注入，返回 `text`、`stop`、`stop_reason`、`model`、`usage`；模型配置与凭据归 provider 实现方管理。`stop_reason=goal_complete` 只表示模拟器自行宣告完成，独立结果验证须另行运行。真实产品失败时调用 `driver.finish("product_failed")` 并保留该产品错误，不制造下一句成功回复。

状态工具只操作自己的文件；产品数据库/浏览器的重置仍由产品适配器负责。写入产物必须来自实际产品读回，不能用预期值写出“合格”结果。脚本/规则模式不传 `provider`。

## 实际本地旅程

可运行 CLI 是组件的真实文件工作流示例，无模型、无产品、无凭据：

```sh
python3 -m myeval.interactions demo --directory .local/coordination/20261118/interactions-demo-new
```

目录必须不存在，避免覆盖。它运行一个明确标记的确定性本地记录程序：规则用户开场 → 程序请求正文 → 用户提交虚构原话 → 程序请求确认 → 用户确认 → 实际 JSON 文件保存 → 最终产物核验。随后重置到初始快照、确认产物缺失、恢复完成快照，再在第二个 episode 读取上一段保存结果。另有调度前取消、轮数限制和非法路径拒绝的实际操作记录。

本轮最终版本已执行 `.local/coordination/20261118/interactions-demo-2/`：首段三次真实文件应用交付后按结束规则停止，三项产物检查通过；第二段继承恢复后的状态，读回原记录后脚本用尽。独立新进程重新打开 workspace 后，保存状态与原话两项检查通过，字节哈希与前一进程一致。具体字节、哈希、取消/拒绝及每步事件保留在 `observation.json`、`reopen-observation.json` 与 `episode-1/events.jsonl`，不随源码提交。初次实现旅程保留在 `interactions-demo-1/`。使用实际流程记录和 Python 编译核验，没有新增单元测试或断言脚本。

## 真实模型模拟用户 provider

`CodexSimulatorProvider` 通过独立、无工具的 Codex CLI 执行。已核对本机版本 `codex-cli 0.156.0` 与实际命令参数；复用已有 CLI 登录，不复制或输出认证文件。使用独立临时 cwd、`--ignore-user-config`、`--ignore-rules`、`--ephemeral` 和只读模式；显式关闭 shell、搜索、插件、apps、浏览器、计算机、子智能体、记忆、skills 与 MCP。仅传当前可见事实和已发生对话，不接触 `.local/holdouts`。原始 stdout 在内存逐行过滤，隐藏 reasoning 不落盘；出现工具事件会拒绝结果。具体执行/来源收据与 schema、prompt 哈希均保留。

官方依据：[非交互执行、结构化输出及事件流](https://learn.chatgpt.com/docs/non-interactive-mode)、[配置项及 shell 工具开关](https://learn.chatgpt.com/docs/config-file/config-reference)。实际可用参数仍以本机 `codex exec --help` 和真实执行为准。

```sh
python3 -m myeval.interactions model-canary --directory .local/coordination/20261118/interactions-model-canary-new
# 确认真实 CLI 进程已启动后取消；不声称供应商已收到请求：
python3 -m myeval.interactions model-canary --directory .local/coordination/20261118/interactions-model-cancel-new --cancel-after-start
```

这些 canary 的 assistant 回复是明确声明的虚构固定夹具，不是产品响应。2026-11-18 本轮结果：

| 记录 | 实际结果 | 边界 |
| --- | --- | --- |
| `interactions-model-canary-1` | 两次真实完成，10413 已知 tokens，工具事件 0 | v1 第二轮补出未提供的偏好和过去情绪；传输成功但此项内容忠实度失败，原件保留 |
| `interactions-model-canary-2` | v1.1 两次真实完成，10632 已知 tokens，工具事件 0；最大两轮停止 | 增强封闭事实边界后，同案例第二轮明确“还说不上最喜欢哪里”，未重现该扩写；一例不能证明模拟用户稳定可靠 |
| `interactions-model-cancel-1` | 真实 CLI 启动后取消，driver/provider 均 cancelled，返回时进程组已消失 | 未获得供应商用量，unknown_usage_calls=1；是否已送达供应商及是否停止计费未知 |

三组位于 `.local/coordination/20261118/`，各有 `observation.json`、逐调用证据及过滤后的事件。四次完整模型响应合计 21045 已知 tokens；取消尝试未知用量单列，不能算总成本零。请求配置为 `gpt-6-sol / low`，有效 model/effort 没有供应商证据，保留 null。未保存隐藏思维链；数值 reasoning token 计数与隐藏文本不同。

当前 CLI 的上游输出 token 硬限制未获证实：provider 将限额写入请求并在响应后检查，`output_limit_enforcement` 明示这一点。运行时间、调用次数、返回字节和累计已知用量有本地上限；单个在途请求仍可能超出累计 token 阈值，金额/远端停止不能保证。取消处理注册有界进程组回收，并等待短暂的收据落盘，避免 driver 返回后遗留本机 CLI。

## 尚未证明

真实供应商 provider 的文本生成与本机取消已执行，但不证明模拟用户有效性、供应商远端取消或产品质量。上面的组件级模型 canary 使用固定虚构 assistant 夹具，没有调用产品；pome 适配器已另外显式接入 `TurnDriver` 的 rules/model 模式，并实际执行规则分支、模型模拟用户与真实产品 Handler 交替调用、独立整理和持久化，详见 [pome 接入与验收路径](adapter-pome.md#可选用户交互)。模型用户的一次预算越界失败与随后独立完成的两轮 canary 均保留，不能把动态开发结果混入固定脚本评测分母。其他产品的接入状态以各自适配器验收记录为准，不从组件存在推断。

重置恢复分支属于实现/代码审阅范围，尚未制造进程崩溃窗口。文件状态只覆盖文件，不覆盖空目录、数据库事务、网络服务或进程；callback 是可信本机代码，字段白名单不能阻止它自行访问外部文件。上述组件开发及 pome 候选/交互开发均未访问 `.local/holdouts`；这与主脑另行封存、解封和执行的正式评测范围分开。

## 评测工作台的阅读交互

前端的长文同字段对照、评价引文往返、原图参考、模型请求信息和录屏章节操作，见 [工作台设计与第四轮验收](design-v2.md#第四轮连续阅读与判断依据)。这些是证据阅读交互，未改变本文件的模拟用户、episode状态或产品执行契约。

第五轮的标签与键盘、匿名目录与保存恢复、来源摘要、实际媒体状态及覆盖缺口，见 [首次使用与可读证据](design-v2.md#第五轮首次使用与可读证据)。仍仅属于工作台阅读与录入交互。
