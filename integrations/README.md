# 产品共享边界的最小改动

已在本机产品工作树实际应用，保持未提交，供各项目主脑整合。补丁仅保存代码，不含资料、凭据或运行输出。

2026-11-13 当前 pome 状态：用户授权将 `reference-companion-v5 / 5.0.1` 接为普通产品默认，保留显式 `baseline` 回退；新 C 提示词和八段示范不变，应用层另增有限的明确结束控制。普通入口、缺省服务选项和健康信息共用同一 profile 解析；本次修改不改旧实验冻结。补丁随当前源码更新，方案及验收见[落地记录](../docs/handoff/2026-11-13-pome-new-c.md)。下文为先前各轮接入经过，其中的旧默认描述只适用于当时版本。

- `sijian-isolation.patch`：在 Sijian HEAD `06ab87c530f29d56a8e19db30249999c5caccf51` 增加独立 trace-root、SIGTERM/退出回收。提示词、schema、V13 和历史评测参考不变。
- `pome-observation.patch`：在 pome HEAD `a9652405efa06d8036d3f86a0b06b15867898c37` 为 Store 增加可选仅内存设置、Handler 白名单观测及 chat/summary 的 thinking/effort；新增 `backend/evaluation.py`、`backend/agent_profiles.py`、`backend/dialogue_evidence.py`。普通入口仍 chat/review enabled+low、summary disabled，默认仍原 baseline 提示词；新增可选的 grounded/contextual 及 dialogue-act-v2 / evidence-ledger-v2 / selective-memory-v2 候选与启动参数 `--dialogue-profile`。事实表整理只读取本故事已审校的儿童事实与原话，按草稿/实际交付/模板哈希绑定；缓存仅内存、6小时过期，重启或过期后拒绝整理，不静默回退，仍属于实验方案。选择性历史只收窄新注入的历史栏，当前会话内此前 AI 回复仍保留。未改声音或用户未跟踪文档。

新环境先读相应产品 AGENTS 和当前状态，在目标仓库用 `git apply --check /path/to/patch` 检查，再按其协作边界整合。不要在已有相同改动的工作树重复应用，也不要覆盖用户修改。MyEval 任务包/描述会记录实际产品 HEAD、必要 dirty 哈希及逐文件 SHA。

同日核心重建补充：pome 补丁还包含 `backend/dialogue_v3.py`、三套 v3 profile（直接回应、理解卡再表达、只修明确问题）、儿童原话专用整理、相关历史词项选择、应用校验错误反馈与单调时钟观测。默认仍 baseline；完整限制见适配文档。`dialogue-act-v2` 历史格式冲突一并修正，但此前冻结结果保持原样。新增方案的正常调用数为 1 / 2 / 2，每轮共享一次失败重试；与旧流程的差异不能归因于提示词措辞一项。

2026-11-12 补充：补丁增加 `backend/friend_profiles.py` 与三套可选朋友回应 v4，将新候选聊天输入去重，并为 C 注入最多两条本地检索的虚构风格示范。原 A 和普通默认保持原样。冻结运行与性能结果见 [朋友回应验收](../docs/acceptance/pome-friend.md)；叫声与教师主持来源未改。

后续 A/C 迭代增加独立的 `backend/friend_profiles_v5.py`：A 以两段固定示范直接参与，C 提供八段完整精简参考供单次生成自行借鉴。v4 原配置仍保留，默认未切换。提供参考的数量与哈希可追溯，模型实际借鉴哪段不可观测；见[新旧 A/C 迭代](../docs/acceptance/pome-ac-iteration.md)。

真实适配验收见 [Sijian](../docs/adapter-sijian.md) 与 [pome](../docs/adapter-pome.md)。

## 2026-11-18 V2 可切换候选

本轮产品源码已在各工作树实现并经过真实执行；没有覆盖普通产品默认，也没有刷新上方旧轮补丁去混合归属。Pome 增加 `backend/eval_profiles_v6.py` 与注册入口；思间增加统一 profile 注册、发现/复盘候选提示词与进程参数；Sidelit 增加共享原生协议 V2、严格无分数契约与显式 V1 兼容构建入口。MyEval 通过真实共享边界选择它们。

当前选择、依赖及限制分别见 [pome](../docs/adapter-pome.md)、[思间](../docs/adapter-sijian.md)、[Sidelit](../docs/adapter-sidelit.md)、[无分数协议](../docs/adapter-sidelit-scorefree.md)、[兼容构建](../docs/adapter-sidelit-compat.md)。精确自有文件清单和冻结哈希在本机 `.local/coordination/20261118/` 的 `pome-final-delivery.json`、`sijian-final-delivery.json`、`sidelit-scorefree-ready.json`、`sidelit-compat-ready.json`。这些是当前源码交接，不把旧 `.patch` 文件当作完整 V2 安装包。

思间 V13、pome 当前声音与普通默认保持原样；Sidelit 无分数候选未过开发门槛，新确认集封存。候选质量结论、实际原生旅程与未覆盖场景以 [V2 验收记录](../docs/acceptance/2026-11-18-platform-v2.md) 为准。
