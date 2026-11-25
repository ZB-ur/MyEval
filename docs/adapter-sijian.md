# 思间适配器

2026-11-18。`myeval/adapters/sijian.py` 为 `sijian-http-v2`。每个 trial 启动独立本机端口的真实 `services/agent/server.py`，发送 `X-Sijian-Run-Mode: evaluation` 与唯一请求 UUID，调用 `/discover` 或 `/review`。业务提示词由产品白名单选择；MyEval 不复制提示词或模拟模型响应。

## 版本化方案

新增开发任务包 `taskpacks/sijian-v2-development.json`。6个独立虚构家族覆盖多候选干扰、单侧条件、无实质联系、对话命名、历史事实边界和当前版本修订。这些材料已向候选作者公开，必须按开发集报告。独立留出由主脑封存，候选作者不读取。

| 配置 ID | 产品 profile | 固定请求 |
| --- | --- | --- |
| `sijian-v2-baseline-v1` | `baseline-v1`，现行 discover-v4 / review-v2 | gpt-6-sol / low |
| `sijian-v2-evidence-tension-v1` | `evidence-tension-v1`，证据与张力 | gpt-6-sol / low |
| `sijian-v2-concise-complete-v1` | `concise-complete-v1`，精简但完整 | gpt-6-sol / low |

两个候选分别检验有据联系/思考推进与阅读负担/用量，不预承诺胜出。schema、边界提示词、模型和推理强度相同；旧任务包 `sijian-text-boundaries-v1` 和既有 low/medium 配置继续存在。配置省略 profile 时选择 baseline-v1，产品默认也保持现行提示词。基线与证据候选版本1.0.0，精简候选经过一次开发收敛为1.1.0；第一轮提示词和输出均保留。内容哈希以具体实验冻结值为准。

产品新增 `services/agent/evaluation_profiles.py` 注册表及四份版本化候选提示词。调用者只能传白名单 profile ID，不能传任意路径或模块。命令示例（开发用途，独立目录）：

```sh
python3 services/agent/server.py \
  --port 8792 --model gpt-6-sol --effort low --timeout 120 \
  --profile evidence-tension-v1 \
  --trace-root /path/to/MyEval/.local/v2/sijian-isolated-traces
```

MyEval 自动选独立空闲端口，不依赖示例8792，也不访问原生用户库。`--profile` 是进程级选择；HTTP正文和原生客户端契约保持不变。运行中的不同 trial 不需要修改共享产品默认。

## 来源与实际配置

`describe(configuration)` 保存 adapter SHA、Sijian HEAD、相关 dirty diff SHA、bridge/registry/选中提示词/边界/schema 的内容 SHA。profile 选择和资源声明来自同一产品模块。`MYEVAL_SIJIAN_ROOT` 可指向另一份本机 checkout，默认 `~/AiCoding/Sijian`。冻结是记录来源并拒绝漂移，不是重建历史 checkout。

产品 `run.json` 回显 `prompt_profile`（id/version/资源hash）、`prompt_version`、实际 `prompt_template_sha256` 与带输入的 `prompt_sha256`。`observed_configuration.product_prompt_profile` 传回该记录；这能证明本地选择的提示词，不能证明供应商实际模型/effort/内部版本。模型实际生效字段和费用仍保留 unknown/null，不凭CLI成功推定。

已有 Codex 登录仅由产品 CLI 在请求边界自行使用，适配器不读认证文件。每次独立目录保留输入、来源、服务日志、请求receipt、过滤后的事件、提示词/边界/schema 快照、run metadata、接受结果或拒收的模型 JSON。只记录可观察业务事件，不保存隐藏推理。原始输出与凭据不进入Git。

`review_output` 仅保留业务交付，移除run ID；匿名评阅不得包含profile、来源路径、用量和胜负映射。配置与性能保存在管理员证据字段。

## 隔离、取消与不支持的范围

本机 HTTP 使用 `ProxyHandler({})` 显式绕过系统代理并拒绝跳转。每trial独立进程/端口/trace-root/request UUID；结束通过已有协作取消与进程回收清理。显式取消、超时、失败和拒收均保留，不自动重试。新attempt不算独立案例。供应商远端计算/计费是否停止仍未知。

`fixed_context_rerun` 和 `isolated_full_run` 均覆盖一次真实发现/复盘调用；后者重置进程和轨迹，不含原生 UI 选材与保存。复盘仅接受明确的固定history，不宣称动态用户或跨轮环境。音频、真机、客户端分批发现和原生阅读旅程未纳入本适配器。平台可对历史输出另建评分作业，adapter自身不重放产品来冒充重评。

## 质量与验收

`rubrics/sijian-v2.json` 提供材料忠实、范围来源、差异不确定性、回应任务、思考推进、阅读负担六维，每维0/1/2及 rated / not_applicable / unavailable。合理空联系或材料不足可充分完成任务；不以关系数量、更短、更长或引用数量判优。AI判断须有输出/来源引文，且不能称为真人金标。

自动规则仅检查精确引文、用例明确声明的 answer_kind 和可观察工具边界。规则通过不证明联系价值、原意或用户价值。运行、规则、独立AI、主脑复核、真人评价与产品旅程分别报告。

本轮已完成24次真实HTTP/CLI调用：首轮6家族×3配置共18次，精简候选仅作一次修订后复跑6次。24次均成功交付，60项自动规则通过；实际提示词hash均与对应来源声明一致。默认profile的真实health和任意profile拒绝路径也已观察，无额外模型请求。证据位于 `.local/coordination/20261118/sijian-development/`，所有初版输出、负结果、计划、来源、时间和轨迹保留。

| 开发配置 | 次数 | 产品耗时中位 | input tokens 合计 | output tokens 合计 |
| --- | ---: | ---: | ---: | ---: |
| 现行基线 1.0.0 | 6 | 15.13秒 | 34985 | 1069 |
| 证据与张力 1.0.0 | 6 | 15.95秒 | 34677 | 1604 |
| 精简但完整 1.0.0（首轮保留） | 6 | 16.95秒 | 33374 | 1587 |
| 精简但完整 1.1.0（冻结候选） | 6 | 15.40秒 | 33632 | 1381 |

首轮精简方案降低了输入量，但输出仍重复，未达到易读/速度收益。最终版本针对正文逐段复述后重复总结作一次收敛，相比自身首轮输出量减少约13%，仍比基线高约29%；候选覆盖的关系也不同，不能据用量直接判优。请求只重复一次且缓存、时段不同，不作因果速度结论。

首轮独立匿名AI复核18份业务输出：未报告major/critical，保留4个有争议的轻微问题（三处“更深入”推断、一处附加建议）。对部分候选的轻微偏好来自具体辨别角度，不来自关系数量；这不是人类金标或正式留出结论。最终版本另由未读首轮评分的独立评阅者完成18份复核，仍无确定major/critical，4条minor均有争议；冻结精简候选在命名例不再作“更深”推断，获得其全部适用维度2/2。两位对基线通知例的建议/比较推断扣分不同，原分歧保留，不通过平均抹平。报告为同级blind-review-final.json及review-handoff.md；小开发样本有天花板，不能把高分称为泛化验收。

候选已在读取留出前冻结，15项来源hash见同级 `sijian-freeze.json`，正式入口 `sijian-ready.json`。浏览器/录屏、正式留出和发布判定由主脑整合，适配开发不代替UI、iOS或真人价值验收。

既有2026-11-09适配级证据继续保留：发现low真实完成（run `d32808a1-c7d9-48b3-930d-8bf72a336bcb`，17802ms）；材料不足复盘medium真实完成（`e27d85a2-d73b-416c-b56d-3954ca8ab9c0`，12586ms）；显式取消与bridge SIGTERM均观察到本机CLI回收、无接受结果，供应商停止未知。原始目录 `.local/adapter-sijian/`；首版UI与持久化记录见[首版验收](acceptance/first-release.md)。这些历史结果不作为新候选胜出证据。

本轮保留 server.py 原有 trace-root 与SIGTERM协作取消改动，没有修改V13、历史评测或原生App。`make verify-baseline` 实际确认56个V13文件与7个历史评测文件匹配；语法与diff检查只是开发检查。后续正式结果仍需注明真实用户材料、真人价值、稳定时延、真机声音与供应商配置不可见等限制。
