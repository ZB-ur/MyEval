# MyEval 首版搭建交接

日期：2026-11-09。工作目录：`/path/to/MyEval`。

用户在“思间 · 主脑”（`01a1a951-fa22-7dc0-8ace-11a177c87a99`）的直接指令：

> 认可，已创建项目MyEval，允许你唤起允许完全访问的评测系统主脑子会话执行评测系统搭建，主脑模型使用astra+max，允许创建子智能体，预算不设上限，做好结果验收，需要有基础的ui

执行主脑：“MyEval · 评测系统主脑”，会话 `01a1be5f-6975-7352-8f1b-d1e8f9152abc`。它从用户在 MyEval 目录创建的空白会话派生。请先核实 cwd、模型、推理强度和实际权限，在 `.local/coordination/startup.json` 留下简短机器可读结果（不含凭据），然后持续执行到首版可验收。`.local` 应忽略。母会话通过会话读取和项目文件收取进度，无须为汇报另行发送跨会话消息。

## 已认可的方向

独立建设通用执行与证据层、项目适配器/任务包、可替换的开源观测与评测平台。首版必须同时接入 Sijian 与 pomegranagent-v2，不能只把另一个项目列为未来计划。

优先验证 Langfuse 自托管与薄适配层，Opik 是重要备选。根据实际部署、两项目接入、重复运行、人工复核和维护成本做最终选择并记录依据。不要同时安装多个大型平台；不要花长时间重新进行无边界选型研究。已有平台负责通用轨迹/实验视图，MyEval 只补执行编排、证据契约和必要的跨平台/盲评 UI。用例与证据以可导出的开放文件/数据契约保存，不仅存在于平台内部。

泛化的判断标准：第三个真实场景只需增加适配器、任务样本与项目量表，公共实验/记录/复核流程无需重写。首版先由两个差异明显的项目证明边界，不强求把任意未来 Agent 全部做完。

## 首版必须能让用户实际完成的旅程

1. 打开基础中文 UI，选择项目、版本化场景集、基线与候选配置，看到来源与执行模式，再启动实验。
2. 看到运行进度、逐项成功/失败/超时/取消和重试事实；失败时可定位原因，不以演示响应伪装真实运行。
3. 打开一个 trial，查看输入快照、输出、可观察的阶段轨迹、配置与版本、耗时、用量/费用及未知项。
4. 对相同案例的两组结果做匿名随机左右配对，选择 A/B/平局/无法判断并填写理由；保存、刷新、重启后仍在。未完成评价之前不能从页面或返回给浏览器的字段直接看到配置映射；此处是本地评审偏差控制，不声称对有源码权限的管理员提供强安全隔离。
5. 查看分项比较与失败分布，能按项目/场景筛选并导出案例、配置、逐项证据和评价。没有人工评价就显示待评，不伪造用户偏好或综合高分。
6. 在相同入口分别真实执行思间发现/复盘和 pome 多轮接话/整理，并能复核两者各自的边界。

基础 UI 可以与选定平台配合，但不是只有平台安装页或静态假数据看板。优先清楚的层级、读得懂的状态、可定位的失败、长内容阅读与可用的空状态；不做大而全的企业后台。

## 公共数据与执行边界

- Task/Case：稳定 ID、版本、任务包、输入、初始状态、材料来源/截止时间、期望行为、独立任务单位、隐私等级、真实/合成标记。
- Configuration：代码版本和必要的 dirty 内容哈希、提示词/资源哈希、模型请求/有效配置、工具与检索版本、环境、执行模式。不可把请求配置当成供应商真实生效配置。
- Trial：实验/案例/配置/重复次数身份、实际开始与结束、生命周期、失败和重试、取消状态。重复采样不等于独立样本数。
- Evidence：输入/输出快照、逐阶段观察事件、模型调用、工具动作、状态变化和最终产物；关联 episode/turn/stage/attempt，保留错误证据。
- Assessment：规则检查、模型评分和人工评价分开；评价器/量表版本、理由、无法判断、人工分歧均保留。

适配器显式声明多轮、音频、取消、环境重置、状态快照等能力。未实现的能力显示 unsupported，不静默降级成另一种运行。支持三种模式的清晰定义：旧轨迹重评分、固定上下文重跑、隔离环境完整执行；首版按实际完成范围标记。

成本记录真实用量与已知计价版本；未知不能写 0。运行要有超时、并发控制、停止和防失控机制；用户不设建设预算上限不意味着无限重试或无限模型循环。

## 两项目接入信息（2026-11-09 只读核对，实施时刷新）

### 思间 Sijian

路径 `~/AiCoding/Sijian`，当时工作树干净，HEAD `06ab87c`。先读其 AGENTS.md、README.md、docs/status.md、docs/product.md、docs/decisions.md、docs/evaluation.md。

- 真实入口 `services/agent/server.py` 的 `/discover`、`/review`，开发期 Codex CLI 桥接，默认请求 `gpt-6-sol` / `low`，不是上线架构。模型/effort 可由启动参数指定。
- `X-Sijian-Run-Mode: evaluation` 使用独立轨迹；已有请求、提示词/边界/schema 快照与哈希、可观察事件、时延和用量。实际模型/effort 可能 unknown。
- discover-v4 / boundary-v1；review-v2 / boundary-review-v2。通过真实入口运行，不能在 MyEval 复制提示词替代它。
- 请求 ID 与 `/cancel` 已有协议；取消请求成功不等于供应商停止计费，按终态证据报告。
- `evals/cases` 含规格与少量已运行的合成材料；规格、真实执行和人类标签必须区分。`reference/eval-spike` 为冻结历史参考，不更改或包装成已接入 runner。
- 当前不要求 iOS 真机语音验收作为文本评测基础设施的前置条件；该缺口仍应保留。

### pomegranagent-v2

路径 `~/AiCoding/pomegranagent-v2`。当时存在用户未跟踪文件 `docs/development-retrospective.md`，保护它。先读其 AGENTS.md、README.md、docs/product.md。

- `server.py` 调用 `backend/app.py`，可用独立 `--data-dir` 和端口启动，默认产品端口 8768。实际参数以代码为准。
- `/api/chat` 是候选生成 → 审校 → 最终交付的真实多阶段路径；通常 2 次模型请求，最多 3 次含共享一次重试，75 秒共享预算。审校器属于被评测系统，不是独立评分器。
- `/api/summary` 只应根据本次儿童原话整理。聊天背景允许同幼儿、活动日期前的有限既有记录；未来记录、其他幼儿和背景中的历史事实不能混入本次故事。
- 非 deviceCheck 请求要求已持久化草稿的 storyID/childID/activityDate 对应。必须在隔离数据目录建立虚构儿童/活动/草稿后走真实路径；deviceCheck 会跳过背景，不可充当正式接入。
- 完整多轮将每个配置实际产出的已交付回复带入其后续上下文；固定上下文比较与完整多轮评测分开。可以先用脚本化虚构儿童输入，再扩展经校准的模型扮演用户。
- chat/summary 本身不负责全部业务落库；浏览器写草稿/最终故事。HTTP 能力验收不能声称 UI 自动保存、语音倒计时或浏览器持久化已通过。涉及该路径需独立浏览器上下文/端口，避免 IndexedDB 音频串用。
- 当前日志缺少完整统一 episode/turn/stage/attempt 关联。必要时做最小、共享的生产/评测可观察接口；不复制业务逻辑，不改变产品默认行为。
- 本地 SenseVoice ASR、Kokoro TTS 是后续音频分层能力。合成/成人音频不证明真实儿童识别或真机听感。暂停的声音选型工作不恢复。
- 评测凭据只使用已有授权的配置/登录途径，最小必要读取并注入隔离进程，不复制整个生产数据库或 settings。若缺凭据，具体指出所缺项，同时继续完成不依赖它的真实接入和 UI，不能用 mock 结果声称通过。

## 平台调研结论与官方依据

以下是前一轮已浏览的官方资料，动态能力实施前按需复核：

- Langfuse 任意任务 SDK / 本地数据集 / 评价器：https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk
- Langfuse 实验数据模型：https://langfuse.com/docs/evaluation/experiments/data-model （一个 experiment 中同一 item 不支持原生重复执行，需 MyEval 明确 trial/run 组织）
- Langfuse 标注队列：https://langfuse.com/docs/evaluation/evaluation-methods/annotation-queues （普通比较/标注不自动等于随机匿名 A/B）
- Langfuse 自托管：https://langfuse.com/self-hosting/deployment/docker-compose ，https://langfuse.com/pricing-self-host （核心 MIT，企业 RBAC/审计等另论）
- Opik 代码与 Apache-2.0：https://github.com/comet-ml/opik
- Opik trial_count：https://www.comet.com/docs/opik/python-sdk-reference/evaluation/evaluate.html
- Opik 标注：https://www.comet.com/docs/opik/evaluation/annotation_queues （可自定义呈现，但仍需显式配对/匿名映射）
- Opik 自托管：https://www.comet.com/docs/opik/self-host/overview （本地模式不自动等于生产用户管理）
- MLflow 完整轨迹重评：https://mlflow.org/docs/latest/genai/eval-monitor/running-evaluation/traces/
- Inspect 的 Agent 与 sandbox：https://inspect.aisi.org.uk/agents.html ，https://inspect.aisi.org.uk/sandboxing.html （未来工具型 Agent 执行后端备选）
- 评测方法参考：https://www.anthropic.com/research/demystifying-evals-for-ai-agents （观察轨迹与环境最终结果分别判断）

平台不应决定业务质量标准。Sijian 看证据、范围、保留差异与联系价值；pome 看忠实原话、不诱导编造、轮次/追问边界和背景使用。规则通过、模型评价、人类偏好、实际 UI 旅程、时延与成本分别展示，不强造总分。

## 验收与完成条件

分阶段交付，主脑持续推进；先实现两项目共用入口，再完成 A/B、重复采样、人工复核与失败闭环。每个阶段保留实际命令、版本、运行 ID、观察结果和可复查文件路径。

首版完成至少需要：

- 有可重复启动/停止的本地系统与基础 UI，README 能让用户找到入口、运行实验和查看结果。
- 两项目至少各有一组冻结用例和两种明确配置，真实请求已运行并保留所有 trial（不可挑成功结果）。使用足够覆盖基础边界的小样本，不声称模型普遍优劣。
- 至少一个多轮 pome 真实 episode，能够区分候选、审校、最终回复、整理结果；不可观察的阶段明确列缺口。
- 浏览器实际走通启动实验 → 看进度/结果 → 查看失败 → 匿名评价 → 刷新/重启恢复 → 导出。保存的评价与运行不串项目/案例/配置。
- 有一条明确的失败/取消/超时恢复证据，状态与请求事实一致；恢复不重放生产写入。
- 规则、模型和人工评价分别保留；通过试填验证标注功能时标成验收试填，不冒充真实用户评价。
- 配置与用例可追溯、重复运行身份正确、导出后可独立查阅，业务原数据保持不变。
- 通过必要构建/语法检查和实际功能旅程；无新增单元测试。独立复核发现问题后修复再复验受影响路径。
- 提供实际 UI 截图、启动入口、架构和当前限制；报告可用系统证据与仍需用户真实材料/设备验证的内容，不把后者用假数据填平。

允许主脑根据现场技术事实调整实现，记录理由并保持上述行为目标。不要仅提交研究报告、脚手架、仅 mock 演示或等待用户逐项选择技术栈。
