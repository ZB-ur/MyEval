# MyEval

独立的本机 Agent 评测系统。首版共用执行、证据、A/B 配对、人工盲评与导出流程，已接入 **思间 Sijian** 的发现/复盘和 **pomegranagent-v2** 的多轮接话/整理。业务请求走产品真实入口；首轮材料均为明确标记的虚构样本。

界面设计与验稿以 [MyEval 设计指南](DESIGN.md) 为准。

## 打开与运行

```sh
cd /Users/lddmay/MyEval
scripts/service start
scripts/open
```

MyEval：<http://127.0.0.1:8310>。`scripts/open` 在本机注入管理员访问码，不打印它；直接访问网页时可用 `.local/data/admin.token` 中的访问码登录。匿名评审从实验详情的独立链接进入，不需要管理员权限，也不会取得配置映射。

```sh
scripts/service status
scripts/service stop
# 前台运行或指定独立数据目录：
scripts/run.sh --port 8310 --data-dir .local/data --concurrency 2
```

公共层只需 Python 3.11+ 和浏览器，无额外 Python/npm 依赖，无前端打包步骤。本机已实际使用 Python 3.14.3。`service` 的本机进程管理目前验收于 macOS；不声称 Windows 已通过。

可选观测平台已部署为本机 Langfuse 4.76.0：<http://127.0.0.1:3300>。

```sh
scripts/platform-start
scripts/platform-status
scripts/platform-stop
```

平台账号、运行时与资源说明见 [平台文档](docs/platform.md)。MyEval 先保存本地证据，再同步平台；平台失败不抹掉业务结果。停止命令保留所有数据。默认不对外开放、不启动付费订阅、不推送 GitHub。

## 使用

1. **新建实验**：选项目、版本化任务包、案例、基线和候选，设定重复次数与单 trial 超时。输入、配置、源码/提示词哈希在建立时冻结；实际执行前再次核对来源，变化则阻止调用并要求新实验。
2. **逐项查看**：进度、成功/失败/超时/停止/中断均保留。trial 展示实际交付、输入快照、可观察阶段、请求/有效配置、用量与未知费用。原始结果和每次尝试可展开。
3. **匿名评审**：独立评审席位随机分配 A/B，刷新与重启保持顺序。可选 A/B/平局/无法判断，填写理由与分项判断；提交该配对后可主动揭示配置。结果在页面打开后发生重试时，旧评价会被拒绝，需重新载入并核对。已看管理员结果的功能试填须选“验收试填”，不会计入人工偏好。新增席位保留独立评价和分歧。
4. **比较与导出**：按项目/场景筛选，导出 ZIP/JSON。包含案例、配置、trial、所有 attempt、阶段、错误、评价与来源；思间包含原生 prompt/boundary/schema 快照，pome 的模型请求提示词在白名单事件中。匿名入口只能导出不含映射的匿名投影。
5. **停止与恢复**：停止后续调度并请求在途任务取消。远端计算和计费是否停止保持未知。重启不自动重发，明确重试保留旧尝试，最多 3 次执行尝试；已评价结果不能重试覆盖。

模型评分与规则、人工评价分别存储。首版不自动调用独立模型评分器，UI 显示“未运行”；可通过管理员 `/api/trials/:id/assessments` 导入带 evaluator、rubric_version、reason、value、evidence 的 `kind=model` 记录，并绑定 attempt 与结果哈希。产品内部审校不算独立评分。没有真实人工评价时显示待评，无总分。

## 业务边界

| 项目 | 实际路径与配置 | 能力限制 |
| --- | --- | --- |
| Sijian | 真实 `/discover`、`/review`；`gpt-6-sol` low / medium；固定上下文重跑 | 开发期 Codex CLI 登录；有效 model/effort 未回显。独立 bridge 端口与 trace-root，不修改 V13，不包含 iOS/语音旅程 |
| pome | 正式草稿、`/api/chat` 候选→审校→最终回复、`/api/summary`、`/api/state` 保存；原 Flash low/high，新增全链路思考开/关 | 每配置沿用自己的实际 final 上下文；独立 SQLite，凭据仅内存注入。文本 HTTP 不等于儿童端自动保存/声音验收；远端取消 unsupported |

已有授权凭据仅由适配器在必要执行边界读取，不出现在 UI、导出、日志或 Git。pome 默认优先现有产品数据目录，当前本机使用已授权的历史隔离设置库；可用 `MYEVAL_POME_CREDENTIAL_DB` 指定授权来源。不得把真实儿童或个人材料自行加入首版执行。源码路径可通过 `MYEVAL_SIJIAN_ROOT` / `MYEVAL_POME_ROOT` 指定。

## 目录与证据

```text
myeval/                 执行、SQLite、HTTP、盲评、导出、平台薄连接
myeval/adapters/        产品共享入口适配
taskpacks/              版本化虚构样本与项目量表
web/                    中文基础 UI，无构建依赖
infra/                  固定版本的 Langfuse Compose
scripts/                本机启停与平台管理
.local/data/            SQLite、输入输出、逐尝试事件、平台回执（忽略）
.local/acceptance/      实际 UI 截图、浏览器导出、验收观察（忽略）
```

[首版验收](docs/acceptance/first-release.md) · [当前状态](docs/status.md) · [架构与决定](docs/architecture.md) · [适配契约](docs/adapter-contract.md) · [独立复核](docs/core-review.md)

实际模型输出只留本机忽略目录。合成样本中的成功执行不证明真实用户质量；已发现 pome 整理纠正冲突和无依据扩写，保留原结果供独立评审，未替产品修改提示词。旧轨迹重评分、真实用户偏好、真实音频/儿童、真机和生产部署仍未覆盖。没有新增单元测试或测试目标。
