# 思间适配器

2026-11-09。实现为 `myeval/adapters/sijian.py`，任务包为 `taskpacks/sijian.json`。每个 trial 启动独立本机端口的真实 `services/agent/server.py`，发送 `X-Sijian-Run-Mode: evaluation` 和唯一请求 UUID，调用 `/discover` 或 `/review`；没有复制提示词，也没有预设模型响应。

任务包有三份新编虚构文字输入：预约便利与容量的发现、两种会议安排的有限证据复盘、不能由历史助手陈述补齐未选日期。正式 A/B 配置均请求 `gpt-6-sol`，分别为 `low` 与 `medium`，唯一变更因素是请求的 reasoning effort。实际生效模型、effort、供应商版本及费用保留未知，不能凭 CLI 成功推定。

## 接口与隔离

`describe(configuration)` 给出适配器 SHA、Sijian HEAD、相关 dirty diff SHA、当前源码/提示词/schema SHA 与能力；`run(case, configuration, ctx)` 遵循公共适配契约。`MYEVAL_SIJIAN_ROOT` 可指向另一份本机 checkout，默认 `/Users/lddmay/AiCoding/Sijian`。已有 Codex 登录由产品 CLI 自行使用，适配器不读认证文件。

每次独立目录保存输入、来源信息、服务日志及产品原生轨迹；产品轨迹包括请求、可见提示词/边界/schema 快照、已过滤事件、run metadata、接受结果或拒收的模型 JSON。所有运行证据只留忽略的 `.local`。`review_output` 仅含业务交付内容，去掉 run ID；配置、来源、路径、耗时和用量留在管理员证据字段中。

本机 HTTP 使用 `ProxyHandler({})` 显式绕过系统代理；在错误的代理地址和空 no_proxy 环境下，真实本机 health 已复验为 200。产品到模型供应商的连接方式没有改变。

`fixed_context_rerun` 和 `isolated_full_run` 都明确覆盖一次真实桥接调用；后者重置桥接进程/轨迹，并不包含原生 UI 选材和保存。复盘支持显式固定 history，不宣称自动多轮环境；音频、旧轨迹重评分、供应商端取消确认均为 unsupported。没有自动模型重试。

规则只检查精确引用、指定 answer_kind 和可观察工具边界；联系价值、原意、差异和回答质量仍需人工评阅。规则通过不代表语义质量通过。

## 必要产品改动

仅修改 Sijian `services/agent/server.py`：

- 增加可选 `--trace-root`，默认目录不变，MyEval 指向 trial 独立目录。
- SIGTERM/正常关闭时向已登记请求发送协作取消，等待本机 CLI 进程组回收及轨迹写入。因为 CLI 自建进程组，不能仅杀上层 worker 就推定它退出。

原生客户端、提示词、材料/结果校验、V13 与历史评测基线未改。产品 HEAD 为 `06ab87c530f29d56a8e19db30249999c5caccf51`；本次 bridge 文件 SHA 为 `059886ec07884ca7d15f5ef86fc22804263d56b93e99258b645a53149f1cd2e6`，实际每个 trial 也独立记录来源。

## 适配级真实验收

以下是直接执行适配器/服务的运行证据，尚不是主脑的整合 UI 验收。首两项通过临时执行上下文调用 `sijian.run`，原始返回和事件保存在相应目录；两次取消均在真实请求开始后约 2 秒触发。未新增单元测试或断言验收脚本。

| 场景 | 真实产品 run ID | 观察结果 | 本地证据 |
| --- | --- | --- | --- |
| 发现 / Sol low | `d32808a1-c7d9-48b3-930d-8bf72a336bcb` | completed；精确引文规则通过；单次用量 input 5608 / output 217，产品耗时 17802 ms | `.local/adapter-sijian/discover-low-01/` |
| 材料不足复盘 / Sol medium | `e27d85a2-d73b-416c-b56d-3954ca8ab9c0` | completed；answer_kind 为 insufficient_evidence，未引用历史日期；input 5874 / output 83，产品耗时 12586 ms | `.local/adapter-sijian/review-medium-01/` |
| 适配器显式取消 | `e4c0bc0f-cd55-4915-8e1e-5624d4cacaae` | /cancel 返回 202；原请求终态 409 request_cancelled；轨迹 cancelled；CLI 已启动后经 SIGTERM 回收，group_present_after_reap=false，无接受结果 | `.local/adapter-sijian/cancel-low-01/` |
| 运行中桥接 SIGTERM | `d0fc8d6b-a6c9-4a04-8b7f-b92ed34fcf60` | 服务退出码 0；原请求 409；轨迹 cancelled；CLI 进程已回收，独立读取 PID 也已不存在 | `.local/adapter-sijian/sigterm-01/observation.json` |

取消运行的用量与供应商是否停止计算/计费均为未知，没有推算为零。上述四次都保留终态，不删除失败或取消。发现的语义输出仅作开发观察，未作为真实用户评价或模型优劣结论。

开发检查：`python3 -m py_compile myeval/adapters/sijian.py`；Sijian `python3 -m py_compile services/agent/server.py`、`git diff --check`；`make verify-baseline` 实际确认 56 个 V13 文件与 7 个历史评测文件逐项 SHA 匹配。基础 UI、A/B 六项矩阵、匿名评阅、重启恢复与导出的主脑集成结果见 [首版验收](acceptance/first-release.md)。

尚未覆盖：iOS 连续旅程、真机声音、客户端批次发现、真实用户材料/联系价值、稳定时延、供应商远端停止和有效配置证明。当前小样本不能证明普遍质量。
