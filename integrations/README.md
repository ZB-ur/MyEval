# 产品共享边界的最小改动

2026-11-09 已在本机产品工作树实际应用，保持未提交，供各项目主脑整合。补丁仅保存代码，不含资料、凭据或运行输出。

- `sijian-isolation.patch`：在 Sijian HEAD `06ab87c530f29d56a8e19db30249999c5caccf51` 增加独立 trace-root、SIGTERM/退出回收。提示词、schema、V13 和历史评测参考不变。
- `pome-observation.patch`：在 pome HEAD `a9652405efa06d8036d3f86a0b06b15867898c37` 为 Store 增加可选仅内存设置、Handler 白名单观测及 chat/summary 的 thinking/effort；新增 `backend/evaluation.py`、`backend/agent_profiles.py`。普通入口仍 chat/review enabled+low、summary disabled，默认仍原 baseline 提示词；新增可选的 grounded/contextual 候选与启动参数 `--dialogue-profile`。未改声音或用户未跟踪文档。

新环境先读相应产品 AGENTS 和当前状态，在目标仓库用 `git apply --check /path/to/patch` 检查，再按其协作边界整合。不要在已有相同改动的工作树重复应用，也不要覆盖用户修改。MyEval 任务包/描述会记录实际产品 HEAD、必要 dirty 哈希及逐文件 SHA。

真实适配验收见 [Sijian](../docs/adapter-sijian.md) 与 [pome](../docs/adapter-pome.md)。
